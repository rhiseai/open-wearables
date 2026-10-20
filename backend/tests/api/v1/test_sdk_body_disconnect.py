"""Distinguish an incomplete SDK body from JSON validation, auth, and server failures.

The ASGI send collector records what the server attempts to send. It does not model
TCP delivery and cannot prove an already disconnected client received that response.
"""

import asyncio
import json
from collections import deque
from collections.abc import Iterator
from functools import wraps
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session
from starlette.types import Message, Scope

from app.database import _get_db_dependency
from app.main import api
from app.schemas.auth import SDKAuthContext
from app.utils.auth import get_sdk_auth

SDK_PATHS = [
    "/api/v1/sdk/users/123e4567-e89b-12d3-a456-426614174000/sync",
    "/api/v1/sdk/users/123e4567-e89b-12d3-a456-426614174000/logs",
]


@pytest.fixture
def request_guards(db: Session) -> Iterator[dict[str, MagicMock]]:
    """Keep the real router/exception stack and observe the auth/handler boundaries."""
    original_overrides = dict(api.dependency_overrides)
    auth = AsyncMock(wraps=get_sdk_auth)
    handler = AsyncMock(side_effect=AssertionError("SDK endpoint handler must not run"))

    def test_database() -> Iterator[Session]:
        yield db

    @wraps(get_sdk_auth)
    async def observed_auth(*args: Any, **kwargs: Any) -> SDKAuthContext:
        return await auth(*args, **kwargs)

    api.dependency_overrides[_get_db_dependency] = test_database
    api.dependency_overrides[get_sdk_auth] = observed_auth
    try:
        with (
            patch("app.middlewares.log_structured") as access_log,
            patch("fastapi.routing.run_endpoint_function", handler),
            patch("app.api.routes.v1.sdk_sync.process_sdk_upload") as task,
            patch("app.api.routes.v1.sdk_sync.store_raw_payload") as sync_store,
            patch("app.api.routes.v1.sdk_sync.put_payload_to_s3") as offload,
            patch("app.api.routes.v1.sdk_logs.store_raw_payload") as logs_store,
        ):
            yield {
                "auth": auth,
                "handler": handler,
                "access_log": access_log,
                "task": task,
                "sync_store": sync_store,
                "offload": offload,
                "logs_store": logs_store,
            }
    finally:
        api.dependency_overrides.clear()
        api.dependency_overrides.update(original_overrides)


async def _exchange(
    path: str,
    body: bytes,
    *,
    disconnect_mid_body: bool = False,
    attempted_sends: list[Message] | None = None,
) -> tuple[list[Message], list[str]]:
    messages: deque[Message] = deque(
        [
            {"type": "http.request", "body": body, "more_body": disconnect_mid_body},
        ]
    )
    if disconnect_mid_body:
        messages.append({"type": "http.disconnect"})
    received_types = []
    attempted = attempted_sends if attempted_sends is not None else []
    still_connected = asyncio.Event()
    declared_length = len(body) + 512 if disconnect_mid_body else len(body)
    scope: Scope = {
        "type": "http",
        # ASGI 2.4 avoids a streaming-response task independently consuming a
        # disconnect after the request body has already been handled.
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(declared_length).encode()),
        ],
        "client": ("192.0.2.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    }

    async def receive() -> Message:
        if messages:
            message = messages.popleft()
        elif disconnect_mid_body:
            message = {"type": "http.disconnect"}
        else:
            # A complete request remains connected while the server responds.
            # Extra receives must wait, rather than inventing a body disconnect.
            await still_connected.wait()
            raise AssertionError("Connected receive unexpectedly resumed")
        received_types.append(message["type"])
        return message

    async def send(message: Message) -> None:
        attempted.append(dict(message))

    await asyncio.wait_for(api(scope, receive, send), timeout=5)
    return attempted, received_types


def _status(attempted: list[Message]) -> int:
    starts = [message["status"] for message in attempted if message["type"] == "http.response.start"]
    assert len(starts) == 1
    return starts[0]


def _body(attempted: list[Message]) -> dict:
    content = b"".join(message.get("body", b"") for message in attempted if message["type"] == "http.response.body")
    return json.loads(content)


def _assert_no_handler_side_effects(guards: dict[str, MagicMock]) -> None:
    guards["handler"].assert_not_called()
    guards["task"].delay.assert_not_called()
    guards["sync_store"].assert_not_called()
    guards["offload"].assert_not_called()
    guards["logs_store"].assert_not_called()


def _access_fields(guards: dict[str, MagicMock], status: int) -> dict:
    guards["access_log"].assert_called_once()
    call = guards["access_log"].call_args
    assert call.args[1:] == ("error", "http_request")
    assert call.kwargs["status"] == status
    assert call.kwargs["method"] == "POST"
    return call.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("path", SDK_PATHS)
async def test_partial_sdk_body_disconnect_attempts_400_before_auth_or_handler(
    path: str,
    request_guards: dict[str, MagicMock],
) -> None:
    fragment = b'{"provider":"apple","data":{"fixture":"partial-body"'
    attempted, received = await _exchange(path, fragment, disconnect_mid_body=True)

    assert received[:2] == ["http.request", "http.disconnect"]
    assert _status(attempted) == 400
    assert _body(attempted) == {"detail": "There was an error parsing the body"}
    fields = _access_fields(request_guards, 400)
    assert fields["error_cause_type"] == "ClientDisconnect"
    assert fields["error_cause_msg"] == ""
    assert fields["request_bytes"] == len(fragment) + 512
    assert fields["content_type"] == "application/json"
    assert "error_type" not in fields
    request_guards["auth"].assert_not_called()
    _assert_no_handler_side_effects(request_guards)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", SDK_PATHS)
async def test_complete_invalid_json_is_validation_not_client_disconnect(
    path: str,
    request_guards: dict[str, MagicMock],
) -> None:
    attempted, received = await _exchange(path, b'{"provider":')

    assert received == ["http.request"]
    assert _status(attempted) == 400
    assert "JSON decode error" in _body(attempted)["detail"]
    fields = _access_fields(request_guards, 400)
    assert fields["error_cause_type"] == "RequestValidationError"
    assert "error_type" not in fields
    request_guards["auth"].assert_not_called()
    _assert_no_handler_side_effects(request_guards)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", SDK_PATHS)
async def test_complete_empty_json_reaches_real_auth_and_returns_401(
    path: str,
    request_guards: dict[str, MagicMock],
) -> None:
    attempted, received = await _exchange(path, b"{}")

    assert received == ["http.request"]
    assert _status(attempted) == 401
    assert _body(attempted) == {"detail": "Authentication required: provide SDK token or API key"}
    fields = _access_fields(request_guards, 401)
    assert "error_cause_type" not in fields
    assert "error_type" not in fields
    request_guards["auth"].assert_awaited_once()
    _assert_no_handler_side_effects(request_guards)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", SDK_PATHS)
async def test_server_exception_after_complete_body_is_500_not_transport_400(
    path: str,
    request_guards: dict[str, MagicMock],
) -> None:
    request_guards["auth"].side_effect = RuntimeError("synthetic server failure")
    attempted: list[Message] = []
    with pytest.raises(RuntimeError, match="synthetic server failure"):
        await _exchange(path, b"{}", attempted_sends=attempted)

    assert _status(attempted) == 500
    fields = _access_fields(request_guards, 500)
    assert fields["error_type"] == "RuntimeError"
    assert "error_cause_type" not in fields
    request_guards["auth"].assert_awaited_once()
    _assert_no_handler_side_effects(request_guards)
