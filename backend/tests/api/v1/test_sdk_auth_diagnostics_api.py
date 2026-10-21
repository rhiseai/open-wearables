"""Real HTTP/DB auth outcomes keep their contracts and bounded diagnostics."""

import json
import logging
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from jose import jwt
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.config import settings
from app.models import RefreshToken
from app.schemas.auth import SDKTokenOrigin
from app.services.refresh_token_service import refresh_token_service
from app.services.sdk_token_service import create_sdk_user_token
from app.utils.config_utils import LogFormat, LogLevel
from app.utils.context import trace_id_var
from tests.factories import ApiKeyFactory, DeveloperFactory, UserFactory

USER_ID = "123e4567-e89b-12d3-a456-426614174000"
OTHER_USER_ID = "123e4567-e89b-12d3-a456-426614174001"
PRIVATE_TRACE = "private-client-request-context"
ENDPOINTS = [
    ("sync", {"provider": "apple", "data": {"records": [], "sleep": [], "workouts": []}}),
    (
        "logs",
        {
            "sdkVersion": "private-sdk-version",
            "provider": "apple",
            "events": [{"eventType": "device_state", "timestamp": "2026-01-02T03:04:05Z"}],
        },
    ),
]
EVENT_KEYS = {
    "timestamp",
    "level",
    "message",
    "provider",
    "action",
    "auth_boundary",
    "auth_reason",
    "auth_outcome",
}


@pytest.fixture(autouse=True)
def _private_json_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "log_format", LogFormat.JSON)
    monkeypatch.setattr(settings, "log_level", LogLevel.DEBUG)
    token = trace_id_var.set(PRIVATE_TRACE)
    try:
        yield
    finally:
        trace_id_var.reset(token)


def _decisions(output: str) -> list[dict]:
    entries = []
    for line in output.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("action") == "auth_decision":
            entries.append(entry)
    for entry in entries:
        assert set(entry) == EVENT_KEYS
        assert entry["message"] == "auth_decision"
        assert entry["provider"] is None
        assert PRIVATE_TRACE not in json.dumps(entry)
        assert USER_ID not in json.dumps(entry)
        assert OTHER_USER_ID not in json.dumps(entry)
    return entries


def _assert_decision(entry: dict, boundary: str, reason: str, outcome: str = "rejected") -> None:
    assert (entry["auth_boundary"], entry["auth_reason"], entry["auth_outcome"]) == (boundary, reason, outcome)
    assert entry["level"] == ("warning" if outcome == "rejected" else "info")


@pytest.mark.parametrize(("endpoint", "payload"), ENDPOINTS)
@pytest.mark.parametrize("has_invalid_key", [False, True])
def test_sdk_401_responses_and_safe_final_reasons(
    client: TestClient,
    endpoint: str,
    payload: dict,
    has_invalid_key: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    headers = {"X-Open-Wearables-API-Key": "private-invalid-api-key"} if has_invalid_key else {}
    headers["X-Request-Id"] = PRIVATE_TRACE
    response = client.post(f"/api/v1/sdk/users/{USER_ID}/{endpoint}", headers=headers, json=payload)
    assert response.status_code == 401
    assert response.json() == {
        "detail": (
            "Invalid or missing API key" if has_invalid_key else "Authentication required: provide SDK token or API key"
        )
    }
    assert "www-authenticate" not in response.headers
    output = capsys.readouterr().out
    [event] = _decisions(output)
    _assert_decision(event, "sdk_credentials", "api_key_invalid" if has_invalid_key else "credentials_missing")
    assert "private-invalid-api-key" not in output


@pytest.mark.parametrize(("endpoint", "payload"), ENDPOINTS)
def test_subject_mismatch_stays_403_and_cannot_enqueue_or_store(
    client: TestClient,
    endpoint: str,
    payload: dict,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = create_sdk_user_token("private-app-fixture", OTHER_USER_ID)
    with (
        patch("app.api.routes.v1.sdk_sync.process_sdk_upload") as task,
        patch("app.api.routes.v1.sdk_sync.store_raw_payload") as sync_store,
        patch("app.api.routes.v1.sdk_sync.put_payload_to_s3") as offload,
        patch("app.api.routes.v1.sdk_logs.store_raw_payload") as logs_store,
    ):
        response = client.post(
            f"/api/v1/sdk/users/{USER_ID}/{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "Token does not match user_id"}
    assert "www-authenticate" not in response.headers
    task.delay.assert_not_called()
    sync_store.assert_not_called()
    offload.assert_not_called()
    logs_store.assert_not_called()
    output = capsys.readouterr().out
    [event] = _decisions(output)
    _assert_decision(event, "sdk_subject", "subject_mismatch")
    assert token not in output


def test_real_api_key_recovers_invalid_bearer_once_and_still_accepts_sync(
    client: TestClient,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = ApiKeyFactory()
    monkeypatch.setattr(settings, "sdk_payload_s3_offload", False)
    capsys.readouterr()
    with (
        patch("app.api.routes.v1.sdk_sync.process_sdk_upload") as task,
        patch("app.api.routes.v1.sdk_sync.store_raw_payload"),
    ):
        response = client.post(
            f"/api/v1/sdk/users/{USER_ID}/sync",
            headers={"Authorization": "Bearer private-invalid-bearer", "X-Open-Wearables-API-Key": api_key.plain_key},
            json=ENDPOINTS[0][1],
        )
    assert response.status_code == 202
    assert response.json()["user_id"] == USER_ID
    task.delay.assert_called_once()
    output = capsys.readouterr().out
    [event] = _decisions(output)
    _assert_decision(event, "sdk_credentials", "sdk_token_invalid", "recovered")
    assert "private-invalid-bearer" not in output
    assert api_key.plain_key not in output
    assert str(api_key.id) not in json.dumps(event)


@pytest.mark.parametrize("revoked", [False, True])
def test_rejected_refresh_credentials_preserve_401_and_bearer_challenge(
    client: TestClient,
    db: Session,
    revoked: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "rt-private-invalid-refresh-fixture"
    if revoked:
        user = UserFactory()
        token = refresh_token_service.create_sdk_refresh_token(db, user.id, SDKTokenOrigin.ADMIN)
        refresh_token_service.revoke_token(db, token)
    capsys.readouterr()
    response = client.post("/api/v1/token/refresh", json={"refresh_token": token})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or revoked refresh token"}
    assert response.headers["www-authenticate"] == "Bearer"
    output = capsys.readouterr().out
    [event] = _decisions(output)
    _assert_decision(event, "refresh", "refresh_invalid_or_revoked")
    assert token not in output


@pytest.mark.parametrize("token_kind", ["sdk", "developer"])
def test_rotation_logs_accepted_not_cross_request_recovery_and_replay_is_rejected(
    client: TestClient,
    db: Session,
    token_kind: str,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    identity = UserFactory() if token_kind == "sdk" else DeveloperFactory()
    identity_id = str(identity.id)
    old = (
        refresh_token_service.create_sdk_refresh_token(db, identity.id, SDKTokenOrigin.ADMIN)
        if token_kind == "sdk"
        else refresh_token_service.create_developer_refresh_token(db, identity.id)
    )
    capsys.readouterr()
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    response = client.post("/api/v1/token/refresh", json={"refresh_token": old})
    assert response.status_code == 200
    data = response.json()
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == settings.access_token_expire_minutes * 60
    assert data["refresh_token"] != old
    claims = jwt.decode(data["access_token"], settings.secret_key, algorithms=[settings.algorithm])
    assert claims["sub"] == identity_id
    assert claims.get("scope") == ("sdk" if token_kind == "sdk" else None)
    saved_old = db.get(RefreshToken, old)
    assert saved_old is not None
    assert saved_old.revoked_at is not None
    replay = client.post("/api/v1/token/refresh", json={"refresh_token": old})
    assert replay.status_code == 401
    assert replay.json() == {"detail": "Invalid or revoked refresh token"}
    assert replay.headers["www-authenticate"] == "Bearer"
    next_response = client.post("/api/v1/token/refresh", json={"refresh_token": data["refresh_token"]})
    assert next_response.status_code == 200
    output = capsys.readouterr().out
    first, failed_replay, second = _decisions(output)
    _assert_decision(first, "refresh", "refresh_rotated", "accepted")
    _assert_decision(failed_replay, "refresh", "refresh_invalid_or_revoked")
    _assert_decision(second, "refresh", "refresh_rotated", "accepted")
    for private in (identity_id, old, old[:10], data["access_token"], data["refresh_token"]):
        assert private not in output
        assert private not in caplog.text


def test_unavailable_auth_stdout_preserves_http_401(client: TestClient) -> None:
    with patch("app.utils.auth_diagnostics.log_structured", side_effect=OSError("unavailable output")):
        response = client.post(f"/api/v1/sdk/users/{USER_ID}/sync", json=ENDPOINTS[0][1])
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required: provide SDK token or API key"}
