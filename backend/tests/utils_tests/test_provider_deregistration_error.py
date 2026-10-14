"""Report provider failures safely at their source and at the Sentry envelope boundary."""

import json
import logging
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import quote, quote_plus

import httpx
import pytest
import sentry_sdk
from fastapi import HTTPException
from sentry_sdk.envelope import Envelope
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.transport import Transport
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.sentry import init_sentry
from app.services.providers.deregistration_error import ProviderDeregistrationError, safe_deregistration_error
from app.services.providers.oura.oauth import OuraOAuth
from app.services.user_connection_service import user_connection_service
from app.utils.sentry_helpers import log_and_capture_error
from tests.factories import UserConnectionFactory, UserFactory

SECRET = "synthetic-rhise5582/access+token?value=#tail"
ENDPOINT = "https://api.ouraring.com/oauth/revoke"
REVOKE_URL = f"{ENDPOINT}?access_token={quote(SECRET, safe='')}&access_token={quote_plus(SECRET)}#private"
OPAQUE_MESSAGE = f"Opaque provider error: raw {SECRET}; quoted '{SECRET}' and \"{SECRET}\"; Bearer {SECRET}"
logger = logging.getLogger("app.tests.provider_deregistration")


def _assert_credentials_absent(text: str) -> None:
    for credential in {SECRET, quote(SECRET, safe=""), quote_plus(SECRET)}:
        assert credential not in text


class RecordingTransport(Transport):
    """Record the actual serialized wire payload, including envelope headers."""

    def __init__(self) -> None:
        super().__init__()
        self.envelopes: list[bytes] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope.serialize())

    def payloads(self, item_type: str = "event") -> list[dict[str, Any]]:
        return [
            json.loads(item.payload.get_bytes())
            for data in self.envelopes
            for item in Envelope.deserialize(data).items
            if item.headers.get("type") == item_type
        ]

    def assert_credentials_absent(self) -> None:
        assert self.envelopes, "Expected telemetry to remain observable"
        _assert_credentials_absent(b"\n".join(self.envelopes).decode())


@pytest.fixture
def sentry_transport(monkeypatch: pytest.MonkeyPatch) -> Generator[RecordingTransport, None, None]:
    """Exercise the application initializer with the locked SDK and local transport."""
    transport = RecordingTransport()
    real_init = sentry_sdk.init
    previous_client = sentry_sdk.get_global_scope().client

    def initialize_locally(**options: Any) -> Any:
        options.update(
            transport=transport,
            default_integrations=False,
            auto_enabling_integrations=False,
        )
        options["integrations"] = [
            *options["integrations"],
            HttpxIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ]
        return real_init(**options)

    monkeypatch.setattr(settings, "SENTRY_ENABLED", True)
    monkeypatch.setattr(settings, "SENTRY_DSN", "https://synthetic@example.invalid/1")
    monkeypatch.setattr(settings, "SENTRY_SAMPLES_RATE", 1.0)
    monkeypatch.setattr(sentry_sdk, "init", initialize_locally)
    monkeypatch.setattr(logger, "level", logging.INFO)

    with sentry_sdk.isolation_scope() as scope:
        scope.clear()
        init_sentry()
        client = sentry_sdk.get_client()
        try:
            yield transport
        finally:
            client.close()
            sentry_sdk.get_global_scope().set_client(previous_client)


def _mock_revoke(monkeypatch: pytest.MonkeyPatch, outcome: int | str) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout(OPAQUE_MESSAGE, request=request)
        if outcome == "network":
            raise httpx.ConnectError(OPAQUE_MESSAGE, request=request)
        if outcome == "generic":
            raise RuntimeError(OPAQUE_MESSAGE)
        return httpx.Response(int(outcome), request=request, text=OPAQUE_MESSAGE)

    def post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", post)
    return requests


def _oura() -> OuraOAuth:
    return OuraOAuth(
        user_repo=MagicMock(),
        connection_repo=MagicMock(),
        provider_name="oura",
        api_base_url="https://api.ouraring.com",
    )


@pytest.mark.parametrize("status_code", [401, 500])
def test_http_error_reports_only_safe_diagnostics_and_preserves_original(status_code: int) -> None:
    request = httpx.Request("POST", REVOKE_URL)
    response = httpx.Response(status_code, request=request, text=OPAQUE_MESSAGE)
    with pytest.raises(httpx.HTTPStatusError) as captured:
        response.raise_for_status()
    original = captured.value
    original_text, original_traceback = str(original), original.__traceback__
    safe = safe_deregistration_error(original)

    assert isinstance(safe, ProviderDeregistrationError)
    assert safe.context == {
        "error_type": "HTTPStatusError",
        "status_code": status_code,
        "method": "POST",
        "endpoint": ENDPOINT,
    }
    assert safe.__traceback__ is original_traceback
    assert safe.__cause__ is None
    assert safe.__context__ is None
    assert str(original) == original_text
    assert original.request is request
    assert original.response is response
    assert original.response.text == OPAQUE_MESSAGE
    _assert_credentials_absent(str(safe))
    _assert_credentials_absent(json.dumps(safe.context))


def test_endpoint_diagnostics_strip_userinfo_query_and_fragment() -> None:
    request = httpx.Request(
        "DELETE",
        f"https://synthetic-user:{quote(SECRET, safe='')}@api.ouraring.com/oauth/revoke?token=private#private",
    )
    safe = safe_deregistration_error(httpx.ConnectError(OPAQUE_MESSAGE, request=request))

    assert isinstance(safe, ProviderDeregistrationError)
    assert safe.context == {
        "error_type": "ConnectError",
        "status_code": None,
        "method": "DELETE",
        "endpoint": ENDPOINT,
    }
    _assert_credentials_absent(str(safe))


@pytest.mark.parametrize("error", [RuntimeError(OPAQUE_MESSAGE), httpx.ReadTimeout(OPAQUE_MESSAGE)])
def test_errors_without_requests_discard_opaque_messages(error: Exception) -> None:
    safe = safe_deregistration_error(error)

    assert isinstance(safe, ProviderDeregistrationError)
    assert safe.context == {"error_type": type(error).__name__, "status_code": None, "method": None, "endpoint": None}
    assert str(error) == OPAQUE_MESSAGE
    assert not hasattr(safe, "request")
    assert not hasattr(safe, "response")
    _assert_credentials_absent(str(safe))


def test_original_exception_chain_is_not_carried_into_safe_error() -> None:
    def fail_with_chain() -> None:
        try:
            raise ValueError(OPAQUE_MESSAGE)
        except ValueError as cause:
            raise RuntimeError(OPAQUE_MESSAGE) from cause

    with pytest.raises(RuntimeError) as captured:
        fail_with_chain()
    original = captured.value
    safe = safe_deregistration_error(original)

    assert original.__cause__ is not None
    assert original.__context__ is not None
    assert safe.__cause__ is None
    assert safe.__context__ is None
    assert safe.__traceback__ is original.__traceback__
    assert isinstance(safe, ProviderDeregistrationError)
    assert safe.error_type == "RuntimeError"


def test_initializer_disables_frame_locals_and_uses_sdk_recursive_scrubbing(
    sentry_transport: RecordingTransport,
) -> None:
    options = sentry_sdk.get_client().options

    assert options["include_local_variables"] is False
    assert options["event_scrubber"] is not None
    sentry_sdk.capture_event(
        {
            "message": "Structured provider diagnostics",
            "request": {
                "query_string": SECRET,
                "headers": {"Authorization": SECRET},
                "data": {"oauth": {"access_token": SECRET, "refresh_token": SECRET}},
            },
            "contexts": {"oauth": {"access_token": SECRET, "refresh_token": SECRET, "provider": "oura"}},
            "extra": {
                "nested": [{"client_secret": SECRET}],
                "password": "sdk-default-scrubber-secret",
                "provider": "oura",
            },
        }
    )

    sentry_transport.assert_credentials_absent()
    assert "sdk-default-scrubber-secret" not in b"\n".join(sentry_transport.envelopes).decode()
    assert sentry_transport.payloads()[-1]["extra"]["provider"] == "oura"
    assert sentry_transport.payloads()[-1]["contexts"]["oauth"]["provider"] == "oura"


@pytest.mark.parametrize("outcome", [401, 500, "timeout", "network", "generic"])
def test_shared_deregistration_reports_safe_errors_to_logs_stdout_and_sentry(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    sentry_transport: RecordingTransport,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    outcome: int | str,
) -> None:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="oura", access_token=SECRET)
    requests = _mock_revoke(monkeypatch, outcome)
    caplog.set_level(logging.ERROR, logger=user_connection_service.logger.name)

    user_connection_service.deregister_from_provider(db, user.id, "oura", _oura())

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.params["access_token"] == SECRET
    sentry_transport.assert_credentials_absent()
    _assert_credentials_absent(caplog.text)
    output = capsys.readouterr()
    _assert_credentials_absent(output.out + output.err)
    assert "oura" in output.out
    records = [record for record in caplog.records if record.name == user_connection_service.logger.name]
    assert records
    reported = records[-1].exc_info[1]
    assert isinstance(reported, ProviderDeregistrationError)
    assert (
        reported.error_type
        == {
            401: "HTTPStatusError",
            500: "HTTPStatusError",
            "timeout": "ReadTimeout",
            "network": "ConnectError",
            "generic": "RuntimeError",
        }[outcome]
    )
    assert reported.status_code == (outcome if isinstance(outcome, int) else None)
    events = sentry_transport.payloads()
    exceptions = [entry for event in events for entry in event.get("exception", {}).get("values", [])]
    assert exceptions
    assert all(entry["type"] == "ProviderDeregistrationError" for entry in exceptions)
    assert all("vars" not in frame for entry in exceptions for frame in entry["stacktrace"]["frames"])
    assert any(
        frame["function"] == "deregister_user" for entry in exceptions for frame in entry["stacktrace"]["frames"]
    )
    if outcome != "generic":
        assert reported.method == "POST"
        assert reported.endpoint == ENDPOINT
        assert ENDPOINT in json.dumps(events)
    if isinstance(outcome, int):
        assert str(outcome) in json.dumps(events)


def test_successful_revoke_breadcrumbs_and_transaction_spans_are_safe_on_later_errors(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    sentry_transport: RecordingTransport,
) -> None:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="oura", access_token=SECRET)
    requests = _mock_revoke(monkeypatch, 204)

    with sentry_sdk.start_transaction(name="oura.revoke", op="provider.revoke"):
        user_connection_service.deregister_from_provider(db, user.id, "oura", _oura())
        sentry_sdk.capture_exception(RuntimeError("Unrelated error after successful revoke"))

    assert len(requests) == 1
    assert requests[0].url.params["access_token"] == SECRET
    sentry_transport.assert_credentials_absent()
    events = sentry_transport.payloads()
    crumbs = [crumb for crumb in events[-1]["breadcrumbs"]["values"] if crumb.get("type") == "http"]
    assert crumbs[-1]["data"]["url"] == ENDPOINT
    assert crumbs[-1]["data"]["http.response.status_code"] == 204
    transactions = sentry_transport.payloads("transaction")
    assert len(transactions) == 1
    http_span = next(span for span in transactions[0]["spans"] if span["op"] == "http.client")
    assert http_span["data"]["http.response.status_code"] == 204
    assert ENDPOINT in http_span["description"]
    assert http_span["data"]["url"] == ENDPOINT


def test_helper_logs_supplied_exception_while_unsafe_original_is_active(
    sentry_transport: RecordingTransport, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger=logger.name)
    try:
        raise RuntimeError(OPAQUE_MESSAGE)
    except RuntimeError:
        safe = ValueError("Safe caller diagnostic")
        log_and_capture_error(safe, logger, "Safe caller failure")

    sentry_transport.assert_credentials_absent()
    _assert_credentials_absent(caplog.text)
    assert caplog.records[-1].exc_info[1] is safe
    exceptions = [
        entry for event in sentry_transport.payloads() for entry in event.get("exception", {}).get("values", [])
    ]
    assert exceptions
    assert all(entry["type"] == "ValueError" for entry in exceptions)


def test_safe_fastapi_client_error_preserves_suppression_and_safe_warning(
    sentry_transport: RecordingTransport, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=logger.name)
    try:
        raise HTTPException(status_code=401, detail=OPAQUE_MESSAGE)
    except HTTPException as original:
        unsafe = original
        safe = safe_deregistration_error(original)
        log_and_capture_error(safe, logger, "Provider rejected token refresh")

    assert isinstance(safe, HTTPException)
    assert safe.status_code == unsafe.status_code == 401
    assert safe.detail != unsafe.detail
    assert unsafe.detail == OPAQUE_MESSAGE
    assert safe.__traceback__ is unsafe.__traceback__
    assert sentry_transport.payloads() == []
    _assert_credentials_absent(caplog.text)
    assert caplog.records[-1].levelno == logging.WARNING
    assert caplog.records[-1].exc_info[1] is safe
    sentry_sdk.capture_exception(RuntimeError("Later safe error"))
    sentry_transport.assert_credentials_absent()
