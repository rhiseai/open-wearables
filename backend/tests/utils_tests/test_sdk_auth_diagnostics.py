"""Bounded SDK auth decisions retain credential behavior without inheriting private context."""

import json
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from jose import jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.services.api_key_service import api_key_service
from app.services.sdk_token_service import create_sdk_user_token
from app.utils.auth import get_combined_auth, get_sdk_auth
from app.utils.auth_diagnostics import AuthBoundary, AuthOutcome, AuthReason, log_auth_decision
from app.utils.config_utils import LogFormat, LogLevel
from app.utils.context import trace_id_var
from app.utils.structured_logging import log_structured
from tests.factories import ApiKeyFactory

USER_ID = "123e4567-e89b-12d3-a456-426614174000"
PRIVATE_TRACE = "private-trace-must-not-be-auth-diagnostic-data"
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
BEARER_FAILURES = [
    ("expired", AuthReason.SDK_TOKEN_EXPIRED),
    ("invalid", AuthReason.SDK_TOKEN_INVALID),
    ("wrong_scope", AuthReason.SDK_TOKEN_WRONG_SCOPE),
]


@pytest.fixture(autouse=True)
def _diagnostic_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "log_format", LogFormat.JSON)
    monkeypatch.setattr(settings, "log_level", LogLevel.DEBUG)
    token = trace_id_var.set(PRIVATE_TRACE)
    try:
        yield
    finally:
        trace_id_var.reset(token)


def _events(output: str) -> list[dict]:
    entries = []
    for line in output.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("action") == "auth_decision":
            entries.append(entry)
    return entries


def _assert_event(event: dict, reason: AuthReason, outcome: AuthOutcome) -> None:
    assert set(event) == EVENT_KEYS
    assert event["message"] == event["action"] == "auth_decision"
    assert event["provider"] is None
    assert event["auth_boundary"] == AuthBoundary.SDK_CREDENTIALS.value
    assert event["auth_reason"] == reason.value
    assert event["auth_outcome"] == outcome.value
    assert event["level"] == ("warning" if outcome is AuthOutcome.REJECTED else "info")
    assert PRIVATE_TRACE not in json.dumps(event)
    assert USER_ID not in json.dumps(event)


def _bearer(kind: str) -> str:
    if kind == "invalid":
        return "private-invalid-bearer-fixture"
    claims = {
        "sub": USER_ID,
        "app_id": "private-app-fixture",
        "scope": "sdk" if kind == "expired" else "developer",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=-5 if kind == "expired" else 5),
    }
    return jwt.encode(claims, settings.secret_key, algorithm=settings.algorithm)


@pytest.mark.parametrize("log_format", [LogFormat.LEGACY, LogFormat.JSON, LogFormat.TEXT])
def test_auth_event_omits_context_in_stdout_and_export(
    log_format: LogFormat,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "log_format", log_format)
    with patch("app.utils.structured_logging.export_structured") as export:
        log_auth_decision(AuthReason.CREDENTIALS_MISSING, AuthOutcome.REJECTED)

    output = capsys.readouterr().out
    assert PRIVATE_TRACE not in output
    assert "trace_id" not in output
    assert "credentials_missing" in output
    assert trace_id_var.get() == PRIVATE_TRACE
    export.assert_called_once()
    args = export.call_args.args
    assert args[1:4] == (logging.WARNING, "auth_decision", None)
    assert args[4] == {
        "action": "auth_decision",
        "auth_boundary": "sdk_credentials",
        "auth_reason": "credentials_missing",
        "auth_outcome": "rejected",
    }
    if log_format is not LogFormat.TEXT:
        [event] = _events(output)
        _assert_event(event, AuthReason.CREDENTIALS_MISSING, AuthOutcome.REJECTED)


def test_existing_structured_trace_inheritance_and_explicit_values_are_preserved(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = logging.getLogger(__name__)
    log_structured(logger, "info", "ordinary event", trace_id=None)
    log_structured(logger, "info", "explicit event", trace_id="explicit-context")
    log_structured(logger, "info", "opted out", inherit_trace_id=False)
    inherited, explicit, omitted = map(json.loads, capsys.readouterr().out.splitlines())
    assert inherited["trace_id"] == PRIVATE_TRACE
    assert explicit["trace_id"] == "explicit-context"
    assert "trace_id" not in omitted
    assert "inherit_trace_id" not in omitted
    assert trace_id_var.get() == PRIVATE_TRACE


@pytest.mark.parametrize(
    ("reason", "outcome", "boundary", "level"),
    [
        (AuthReason.SUBJECT_MISMATCH, AuthOutcome.REJECTED, AuthBoundary.SDK_SUBJECT, "warning"),
        (AuthReason.SDK_TOKEN_EXPIRED, AuthOutcome.RECOVERED, AuthBoundary.SDK_CREDENTIALS, "info"),
        (AuthReason.REFRESH_ROTATED, AuthOutcome.ACCEPTED, AuthBoundary.REFRESH, "info"),
    ],
)
def test_fixed_decision_fields_and_severity(
    reason: AuthReason,
    outcome: AuthOutcome,
    boundary: AuthBoundary,
    level: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_auth_decision(reason, outcome, boundary=boundary)
    [event] = _events(capsys.readouterr().out)
    assert set(event) == EVENT_KEYS
    assert (event["auth_reason"], event["auth_outcome"], event["auth_boundary"], event["level"]) == (
        reason.value,
        outcome.value,
        boundary.value,
        level,
    )


def test_auth_events_respect_existing_log_level(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "log_level", LogLevel.WARNING)
    with patch("app.utils.structured_logging.export_structured") as export:
        log_auth_decision(AuthReason.SDK_TOKEN_EXPIRED, AuthOutcome.RECOVERED)
    assert capsys.readouterr().out == ""
    export.assert_not_called()


def test_unavailable_stdout_does_not_change_auth_decisions() -> None:
    with patch("app.utils.auth_diagnostics.log_structured", side_effect=BrokenPipeError("private fixture")):
        log_auth_decision(AuthReason.CREDENTIALS_MISSING, AuthOutcome.REJECTED)
    with (
        patch("app.utils.auth_diagnostics.log_structured", side_effect=TypeError("programming error")),
        pytest.raises(TypeError, match="programming error"),
    ):
        log_auth_decision(AuthReason.CREDENTIALS_MISSING, AuthOutcome.REJECTED)


@pytest.mark.asyncio
async def test_closed_stdout_preserves_the_original_auth_401(db: Session) -> None:
    closed = StringIO()
    closed.close()
    with (
        patch("app.utils.structured_logging.sys.stdout", closed),
        pytest.raises(HTTPException) as raised,
    ):
        await get_sdk_auth(db, None, None)
    assert raised.value.status_code == 401
    assert raised.value.detail == "Authentication required: provide SDK token or API key"
    assert raised.value.headers is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "reason"), BEARER_FAILURES)
@pytest.mark.parametrize("has_api_key", [False, True])
async def test_bearer_failure_has_one_final_rejection_or_recovered_fallback(
    db: Session,
    kind: str,
    reason: AuthReason,
    has_api_key: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api_key = ApiKeyFactory() if has_api_key else None
    bearer = _bearer(kind)
    capsys.readouterr()
    if api_key is not None:
        result = await get_sdk_auth(db, bearer, api_key.plain_key)
        assert result.auth_type == "api_key"
        assert result.api_key_id == str(api_key.id)
        outcome = AuthOutcome.RECOVERED
    else:
        with pytest.raises(HTTPException) as raised:
            await get_sdk_auth(db, bearer, None)
        assert raised.value.status_code == 401
        assert raised.value.detail == "Authentication required: provide SDK token or API key"
        assert raised.value.headers is None
        outcome = AuthOutcome.REJECTED
    output = capsys.readouterr().out
    [event] = _events(output)
    _assert_event(event, reason, outcome)
    assert bearer not in output
    if api_key is not None:
        assert api_key.plain_key not in output
        assert str(api_key.id) not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [None, "expired"])
async def test_real_invalid_api_key_is_the_final_rejection_reason(
    db: Session,
    kind: str | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(HTTPException) as raised:
        await get_sdk_auth(db, _bearer(kind) if kind else None, "private-invalid-api-key")
    assert raised.value.status_code == 401
    assert raised.value.detail == "Invalid or missing API key"
    assert raised.value.headers is None
    [event] = _events(capsys.readouterr().out)
    _assert_event(event, AuthReason.API_KEY_INVALID, AuthOutcome.REJECTED)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 503])
async def test_api_key_exception_is_preserved_and_only_401_is_an_auth_rejection(
    db: Session,
    status_code: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = HTTPException(status_code, "original private detail", headers={"X-Fixture": "preserved"})
    with (
        patch.object(api_key_service, "validate_api_key", side_effect=error),
        pytest.raises(HTTPException) as raised,
    ):
        await get_sdk_auth(db, None, "private-api-key-fixture")
    assert raised.value is error
    output = capsys.readouterr().out
    assert "original private detail" not in output
    assert "private-api-key-fixture" not in output
    if status_code == 401:
        [event] = _events(output)
        _assert_event(event, AuthReason.API_KEY_INVALID, AuthOutcome.REJECTED)
    else:
        assert _events(output) == []


@pytest.mark.asyncio
async def test_sdk_precedence_and_optional_developer_probe_emit_no_rejections(
    db: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = create_sdk_user_token("private-app-fixture", USER_ID)
    with patch.object(api_key_service, "validate_api_key", side_effect=AssertionError("fallback must not run")):
        result = await get_sdk_auth(db, token, "invalid-unused-api-key")
        combined = await get_combined_auth(db, token, "invalid-unused-api-key")
    assert result.auth_type == combined.auth_type == "sdk_token"
    assert str(result.user_id) == str(combined.user_id) == USER_ID
    assert _events(capsys.readouterr().out) == []


@pytest.mark.asyncio
async def test_api_key_without_bearer_is_ordinary_success_not_recovery(
    db: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api_key = ApiKeyFactory()
    capsys.readouterr()
    result = await get_sdk_auth(db, None, api_key.plain_key)
    assert result.auth_type == "api_key"
    assert result.api_key_id == str(api_key.id)
    assert _events(capsys.readouterr().out) == []


@pytest.mark.asyncio
async def test_malformed_sdk_subject_does_not_start_a_new_fallback_path(
    db: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = jwt.encode(
        {"scope": "sdk", "sub": "private-malformed-subject", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        settings.secret_key,
        algorithm=settings.algorithm,
    )
    with (
        patch.object(api_key_service, "validate_api_key", side_effect=AssertionError("fallback must not run")),
        pytest.raises(ValueError, match="UUID"),
    ):
        await get_sdk_auth(db, token, "unused-api-key")
    assert _events(capsys.readouterr().out) == []
