"""
Unit tests for refresh token service.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from jose import jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.models import RefreshToken
from app.repositories.refresh_token_repository import refresh_token_repository
from app.schemas.auth import SDKTokenOrigin, TokenType
from app.services.refresh_token_service import refresh_token_service
from tests.factories import ApplicationFactory, DeveloperFactory, UserFactory


class TestCreateSDKRefreshToken:
    """Tests for create_sdk_refresh_token."""

    def test_create_sdk_refresh_token_format(self, db: Session) -> None:
        """SDK refresh token should have rt- prefix and be 35 characters total."""
        # Arrange
        user = UserFactory()

        # Act
        token = refresh_token_service.create_sdk_refresh_token(db, user.id, SDKTokenOrigin.ADMIN)

        # Assert
        assert token.startswith("rt-")
        assert len(token) == 35  # "rt-" (3) + 32 hex chars

    def test_create_sdk_refresh_token_stored_in_db(self, db: Session) -> None:
        """SDK refresh token should be stored in database with correct metadata."""
        # Arrange
        user = UserFactory()
        application = ApplicationFactory(developer=DeveloperFactory())

        # Act
        token = refresh_token_service.create_sdk_refresh_token(
            db, user.id, SDKTokenOrigin.APPLICATION, application_id=application.id
        )

        # Assert
        db_token = db.query(RefreshToken).filter(RefreshToken.id == token).first()
        assert db_token is not None
        assert db_token.token_type == TokenType.SDK
        assert db_token.user_id == user.id
        assert db_token.origin == SDKTokenOrigin.APPLICATION
        assert db_token.application_id == application.id
        assert db_token.issued_by_developer_id is None
        assert db_token.developer_id is None
        assert db_token.revoked_at is None


class TestCreateDeveloperRefreshToken:
    """Tests for create_developer_refresh_token."""

    def test_create_developer_refresh_token_format(self, db: Session) -> None:
        """Developer refresh token should have rt- prefix and be 35 characters total."""
        # Arrange
        developer = DeveloperFactory()

        # Act
        token = refresh_token_service.create_developer_refresh_token(db, developer.id)

        # Assert
        assert token.startswith("rt-")
        assert len(token) == 35  # "rt-" (3) + 32 hex chars

    def test_create_developer_refresh_token_stored_in_db(self, db: Session) -> None:
        """Developer refresh token should be stored in database with correct metadata."""
        # Arrange
        developer = DeveloperFactory()

        # Act
        token = refresh_token_service.create_developer_refresh_token(db, developer.id)

        # Assert
        db_token = db.query(RefreshToken).filter(RefreshToken.id == token).first()
        assert db_token is not None
        assert db_token.token_type == TokenType.DEVELOPER
        assert db_token.developer_id == developer.id
        assert db_token.user_id is None
        assert db_token.origin is None
        assert db_token.application_id is None
        assert db_token.revoked_at is None


class TestRefreshToken:
    """Tests for refresh_token method."""

    def test_refresh_sdk_token_success(self, db: Session) -> None:
        """Refreshing SDK token should return new access token and rotated refresh token."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, SDKTokenOrigin.ADMIN)

        # Act
        result = refresh_token_service.refresh_token(db, refresh_token)

        # Assert
        assert result.access_token is not None
        assert result.token_type == "bearer"
        # Refresh token should be rotated
        assert result.refresh_token != refresh_token
        assert result.refresh_token.startswith("rt-")

    def test_refresh_developer_token_success(self, db: Session) -> None:
        """Refreshing developer token should return new access token and rotated refresh token."""
        # Arrange
        developer = DeveloperFactory()
        refresh_token = refresh_token_service.create_developer_refresh_token(db, developer.id)

        # Act
        result = refresh_token_service.refresh_token(db, refresh_token)

        # Assert
        assert result.access_token is not None
        assert result.token_type == "bearer"
        # Refresh token should be rotated
        assert result.refresh_token != refresh_token
        assert result.refresh_token.startswith("rt-")

    def test_refresh_invalid_token_raises_401(self, db: Session) -> None:
        """Refreshing invalid token should raise 401."""
        # Arrange
        invalid_token = "rt-invalidtoken12345678901234567890"

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, invalid_token)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid or revoked refresh token"

    def test_refresh_revoked_token_raises_401(self, db: Session) -> None:
        """Refreshing revoked token should raise 401."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, SDKTokenOrigin.ADMIN)
        refresh_token_service.revoke_token(db, refresh_token)

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.refresh_token(db, refresh_token)

        assert exc_info.value.status_code == 401

    def test_refresh_revokes_old_token(self, db: Session) -> None:
        """Refreshing token should revoke the old token (rotation)."""
        # Arrange
        user = UserFactory()
        old_refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, SDKTokenOrigin.ADMIN)

        # Verify not revoked initially
        db_token = db.query(RefreshToken).filter(RefreshToken.id == old_refresh_token).first()
        assert db_token is not None
        assert db_token.revoked_at is None

        # Act
        result = refresh_token_service.refresh_token(db, old_refresh_token)

        # Assert - old token is revoked
        db.refresh(db_token)
        assert db_token.revoked_at is not None

        # Assert - new token exists and is not revoked
        new_db_token = db.query(RefreshToken).filter(RefreshToken.id == result.refresh_token).first()
        assert new_db_token is not None
        assert new_db_token.revoked_at is None
        assert new_db_token.replaces_id == old_refresh_token

    @pytest.mark.parametrize("token_type", [TokenType.SDK, TokenType.DEVELOPER])
    def test_refresh_keeps_old_token_usable_when_issuing_tokens_fails(self, db: Session, token_type: TokenType) -> None:
        """A refresh that fails to issue the new pair must not burn the old token."""
        # Arrange
        if token_type == TokenType.SDK:
            old_refresh_token = refresh_token_service.create_sdk_refresh_token(
                db, UserFactory().id, SDKTokenOrigin.ADMIN
            )
            failing_step = "create_sdk_refresh_token"
        else:
            old_refresh_token = refresh_token_service.create_developer_refresh_token(db, DeveloperFactory().id)
            failing_step = "create_developer_refresh_token"

        # Act
        with (
            patch.object(refresh_token_service, failing_step, side_effect=RuntimeError("db down")),
            pytest.raises(RuntimeError),
        ):
            refresh_token_service.refresh_token(db, old_refresh_token)
        db.rollback()  # what the request's session dependency does on an error

        # Assert
        old_row = db.get(RefreshToken, old_refresh_token)
        assert old_row is not None
        assert old_row.revoked_at is None
        result = refresh_token_service.refresh_token(db, old_refresh_token)
        db.refresh(old_row)
        assert old_row.revoked_at is not None
        assert db.get(RefreshToken, result.refresh_token) is not None


TOKEN_KINDS = ["sdk-admin", "sdk-application", "developer"]


def _issue(db: Session, kind: str) -> tuple[str, dict[str, str]]:
    """Issue a refresh token of `kind` and return it with the claims its access tokens carry."""
    if kind == "sdk-admin":
        user, developer = UserFactory(), DeveloperFactory()
        token = refresh_token_service.create_sdk_refresh_token(
            db, user.id, SDKTokenOrigin.ADMIN, issued_by_developer_id=developer.id
        )
        return token, {"sub": str(user.id), "scope": "sdk", "app_id": f"admin:{developer.id}"}
    if kind == "sdk-application":
        user, application = UserFactory(), ApplicationFactory(developer=DeveloperFactory())
        token = refresh_token_service.create_sdk_refresh_token(
            db, user.id, SDKTokenOrigin.APPLICATION, application_id=application.id
        )
        return token, {"sub": str(user.id), "scope": "sdk", "app_id": application.app_id}
    developer = DeveloperFactory()
    return refresh_token_service.create_developer_refresh_token(db, developer.id), {"sub": str(developer.id)}


def _claims(access_token: str) -> dict[str, str]:
    payload = jwt.decode(access_token, settings.secret_key, algorithms=[settings.algorithm])
    return {key: payload[key] for key in ("sub", "scope", "app_id") if key in payload}


def _assert_rejected(db: Session, token: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        refresh_token_service.refresh_token(db, token)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or revoked refresh token"


class TestReplayRotation:
    """A rotated token presented again gets its successor back while the successor is unused."""

    @pytest.mark.parametrize("kind", TOKEN_KINDS)
    def test_replay_returns_the_same_successor_and_a_fresh_access_token(self, db: Session, kind: str) -> None:
        old, claims = _issue(db, kind)
        first = refresh_token_service.refresh_token(db, old)

        replay = refresh_token_service.refresh_token(db, old)

        assert replay.refresh_token == first.refresh_token
        assert _claims(replay.access_token) == claims
        assert replay.expires_in == first.expires_in
        successor = db.get(RefreshToken, first.refresh_token)
        assert successor is not None
        assert successor.revoked_at is None

    @pytest.mark.parametrize("kind", TOKEN_KINDS)
    def test_replay_after_the_window_is_rejected(self, db: Session, kind: str) -> None:
        old, _ = _issue(db, kind)
        refresh_token_service.refresh_token(db, old)
        old_row = db.get(RefreshToken, old)
        assert old_row is not None
        old_row.revoked_at = datetime.now(timezone.utc) - timedelta(
            seconds=settings.refresh_token_replay_window_seconds + 1
        )
        db.commit()

        _assert_rejected(db, old)

    @pytest.mark.parametrize("kind", TOKEN_KINDS)
    def test_replay_ends_once_the_successor_is_used(self, db: Session, kind: str) -> None:
        old, _ = _issue(db, kind)
        first = refresh_token_service.refresh_token(db, old)
        second = refresh_token_service.refresh_token(db, first.refresh_token)

        _assert_rejected(db, old)
        assert refresh_token_service.refresh_token(db, first.refresh_token).refresh_token == second.refresh_token

    @pytest.mark.parametrize("kind", TOKEN_KINDS)
    def test_replay_ends_once_the_successor_is_revoked(self, db: Session, kind: str) -> None:
        old, _ = _issue(db, kind)
        first = refresh_token_service.refresh_token(db, old)
        refresh_token_service.revoke_token(db, first.refresh_token)

        _assert_rejected(db, old)

    def test_a_password_change_ends_replays(self, db: Session) -> None:
        developer = DeveloperFactory()
        old = refresh_token_service.create_developer_refresh_token(db, developer.id)
        first = refresh_token_service.refresh_token(db, old)

        refresh_token_repository.revoke_all_for_developer(db, developer.id)

        _assert_rejected(db, old)
        _assert_rejected(db, first.refresh_token)

    def test_revoking_every_token_of_a_user_ends_replays(self, db: Session) -> None:
        user = UserFactory()
        old = refresh_token_service.create_sdk_refresh_token(db, user.id, SDKTokenOrigin.ADMIN)
        first = refresh_token_service.refresh_token(db, old)

        refresh_token_repository.revoke_all_for_user(db, user.id)

        _assert_rejected(db, old)
        _assert_rejected(db, first.refresh_token)

    def test_replay_builds_claims_from_the_rotated_token(self, db: Session) -> None:
        """An admin token whose issuing developer is gone keeps the bare origin as its app_id."""
        user, developer = UserFactory(), DeveloperFactory()
        old = refresh_token_service.create_sdk_refresh_token(
            db, user.id, SDKTokenOrigin.ADMIN, issued_by_developer_id=developer.id
        )
        refresh_token_service.refresh_token(db, old)
        db.delete(developer)
        db.commit()
        db.expire_all()

        replay = refresh_token_service.refresh_token(db, old)

        assert _claims(replay.access_token) == {"sub": str(user.id), "scope": "sdk", "app_id": "admin"}


class TestRevokeToken:
    """Tests for revoke_token method."""

    def test_revoke_token_success(self, db: Session) -> None:
        """Revoking token should set revoked_at timestamp."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, SDKTokenOrigin.ADMIN)

        # Act
        result = refresh_token_service.revoke_token(db, refresh_token)

        # Assert
        assert result is True
        db_token = db.query(RefreshToken).filter(RefreshToken.id == refresh_token).first()
        assert db_token is not None
        assert db_token.revoked_at is not None

    @pytest.mark.parametrize("kind", TOKEN_KINDS)
    def test_revoking_a_replayable_rotated_token_revokes_its_successor(self, db: Session, kind: str) -> None:
        """Within the window a rotated token gives access to its successor, so revoking it ends that."""
        old, _ = _issue(db, kind)
        first = refresh_token_service.refresh_token(db, old)

        assert refresh_token_service.revoke_token(db, old) is True

        successor = db.get(RefreshToken, first.refresh_token)
        assert successor is not None
        assert successor.revoked_at is not None
        _assert_rejected(db, old)
        _assert_rejected(db, first.refresh_token)

    def test_revoking_a_rotated_token_whose_successor_was_used_raises_404(self, db: Session) -> None:
        old, _ = _issue(db, "sdk-admin")
        first = refresh_token_service.refresh_token(db, old)
        second = refresh_token_service.refresh_token(db, first.refresh_token)

        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.revoke_token(db, old)

        assert exc_info.value.status_code == 404
        live = db.get(RefreshToken, second.refresh_token)
        assert live is not None
        assert live.revoked_at is None

    def test_revoke_nonexistent_token_raises_404(self, db: Session) -> None:
        """Revoking non-existent token should raise 404."""
        # Arrange
        invalid_token = "rt-nonexistent123456789012345678"

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.revoke_token(db, invalid_token)

        assert exc_info.value.status_code == 404

    def test_revoke_already_revoked_token_raises_404(self, db: Session) -> None:
        """Revoking already revoked token should raise 404."""
        # Arrange
        user = UserFactory()
        refresh_token = refresh_token_service.create_sdk_refresh_token(db, user.id, SDKTokenOrigin.ADMIN)
        refresh_token_service.revoke_token(db, refresh_token)

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            refresh_token_service.revoke_token(db, refresh_token)

        assert exc_info.value.status_code == 404
