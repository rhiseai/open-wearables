"""Concurrent refreshes of one refresh token converge on one successor.

The regular `db` fixture keeps a test on one connection, so it cannot show a race.
These tests commit their data and refresh it from separate sessions in parallel threads.
"""

import threading
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, or_
from sqlalchemy.orm import Session

from app.models import Developer, RefreshToken, User
from app.repositories.refresh_token_repository import refresh_token_repository
from app.schemas.auth import SDKTokenOrigin, TokenResponse, TokenType
from app.services.refresh_token_service import refresh_token_service

CONCURRENT_REQUESTS = 8


@dataclass
class Owners:
    user_id: UUID
    developer_id: UUID


@pytest.fixture
def owners(session_factory: Any) -> Generator[Owners, None, None]:
    """Commit a user and a developer for real, and remove everything they own afterwards."""
    owners = Owners(user_id=uuid4(), developer_id=uuid4())
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        session.add(User(id=owners.user_id, created_at=now, email=f"{owners.user_id}@example.com"))
        session.add(
            Developer(
                id=owners.developer_id,
                created_at=now,
                updated_at=now,
                email=f"{owners.developer_id}@example.com",
                hashed_password="hashed_test_password",
            )
        )
        session.commit()

    yield owners

    with session_factory() as session:
        session.execute(
            delete(RefreshToken).where(
                or_(
                    RefreshToken.user_id == owners.user_id,
                    RefreshToken.developer_id == owners.developer_id,
                    RefreshToken.issued_by_developer_id == owners.developer_id,
                )
            )
        )
        session.execute(delete(User).where(User.id == owners.user_id))
        session.execute(delete(Developer).where(Developer.id == owners.developer_id))
        session.commit()


def _run_concurrently(session_factory: Any, call: Callable[[Session], object]) -> list[object]:
    """Run `call` from CONCURRENT_REQUESTS threads at once, each with its own session."""
    barrier = threading.Barrier(CONCURRENT_REQUESTS, timeout=10)

    def worker() -> object:
        with session_factory() as session:
            session.connection()  # connect up front, as a pooled server connection would be
            barrier.wait()
            try:
                return call(session)
            except HTTPException as exc:
                return exc

    with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as pool:
        futures = [pool.submit(worker) for _ in range(CONCURRENT_REQUESTS)]
        return [future.result() for future in futures]


@pytest.mark.parametrize("token_type", [TokenType.SDK, TokenType.DEVELOPER])
def test_concurrent_refreshes_of_one_token_converge_on_one_successor(
    session_factory: Any, owners: Owners, token_type: TokenType
) -> None:
    with session_factory() as session:
        if token_type == TokenType.SDK:
            old_token = refresh_token_service.create_sdk_refresh_token(
                session,
                user_id=owners.user_id,
                origin=SDKTokenOrigin.INVITE,
                issued_by_developer_id=owners.developer_id,
            )
        else:
            old_token = refresh_token_service.create_developer_refresh_token(session, developer_id=owners.developer_id)

    results = _run_concurrently(session_factory, lambda s: refresh_token_service.refresh_token(s, old_token))

    assert all(isinstance(r, TokenResponse) for r in results), results
    successors = {r.refresh_token for r in results if isinstance(r, TokenResponse)}
    assert len(successors) == 1
    with session_factory() as session:
        owned = or_(RefreshToken.user_id == owners.user_id, RefreshToken.developer_id == owners.developer_id)
        live = session.query(RefreshToken).filter(owned, RefreshToken.revoked_at.is_(None)).all()
        assert [token.id for token in live] == list(successors)
        assert live[0].replaces_id == old_token


def test_a_claim_waits_for_an_unfinished_one_and_wins_only_if_it_rolls_back(
    session_factory: Any, owners: Owners
) -> None:
    """There is never a token revoked by rotation without its successor stored."""
    with session_factory() as session:
        token = refresh_token_service.create_sdk_refresh_token(
            session,
            user_id=owners.user_id,
            origin=SDKTokenOrigin.INVITE,
            issued_by_developer_id=owners.developer_id,
        )

    with session_factory() as first, session_factory() as second:
        assert refresh_token_repository.claim_valid_token(first, token) is not None

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(refresh_token_repository.claim_valid_token, second, token)
            with pytest.raises(TimeoutError):
                pending.result(timeout=0.5)  # blocked on the row the first session holds
            first.rollback()
            assert pending.result(timeout=5) is not None
        second.rollback()
