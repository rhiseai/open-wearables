"""Regression test for #2069: SDK auth must not hold the request-scoped session."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from threading import get_ident
from unittest.mock import patch

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from sqlalchemy.orm import Session

from app.api.routes.v1.sdk_logs import submit_sdk_logs
from app.api.routes.v1.sdk_sync import get_sdk_sync_status
from app.database import _get_db_dependency
from app.utils.auth import get_sdk_auth
from app.utils.security import create_access_token
from tests.factories import ApiKeyFactory


def _dependency_calls(endpoint: Callable) -> list:
    """Every dependency an endpoint resolves, nested ones included, built as FastAPI builds it."""
    calls: list = []

    def walk(dependant: Dependant) -> None:
        for sub in dependant.dependencies:
            calls.append(sub.call)
            walk(sub)

    walk(get_dependant(path="/", call=endpoint))
    return calls


class TestSdkAuthHoldsNoDbSession:
    def test_sdk_logs_auth_declares_no_request_session(self) -> None:
        """DbSession is a yield dependency, released only once the response completes."""
        deps = _dependency_calls(submit_sdk_logs)
        assert get_sdk_auth in deps
        assert _get_db_dependency not in deps
        assert _get_db_dependency in _dependency_calls(get_sdk_sync_status)

    @pytest.mark.asyncio
    async def test_sdk_token_needs_no_database(self) -> None:
        user_id = "123e4567-e89b-12d3-a456-426614174000"
        token = create_access_token(subject=user_id, scope="sdk", app_id="app_123")

        with patch("app.utils.auth.SessionLocal", side_effect=AssertionError("SDK token auth opened a session")):
            context = await get_sdk_auth(token=token, x_open_wearables_api_key=None)

        assert (context.auth_type, str(context.user_id)) == ("sdk_token", user_id)

    @pytest.mark.asyncio
    async def test_api_key_is_checked_off_the_event_loop_on_a_session_closed_before_return(self, db: Session) -> None:
        """get_sdk_auth stays async; the key lookup runs in a worker on a session of its own."""
        api_key = ApiKeyFactory()
        opened_in: list[int] = []
        closed_in: list[int] = []

        @contextmanager
        def open_auth_session() -> Generator[Session]:
            opened_in.append(get_ident())
            try:
                yield db
            finally:
                closed_in.append(get_ident())

        with patch("app.utils.auth.SessionLocal", side_effect=open_auth_session):
            context = await get_sdk_auth(token=None, x_open_wearables_api_key=api_key.plain_key)

        assert (context.auth_type, context.api_key_id) == ("api_key", str(api_key.id))
        # Opened and closed once, in the same worker thread, before the request goes on.
        assert len(opened_in) == 1
        assert closed_in == opened_in
        assert opened_in[0] != get_ident()
