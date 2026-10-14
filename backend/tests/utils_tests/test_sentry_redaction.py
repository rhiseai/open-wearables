"""Credential redaction at the real Sentry envelope boundary, without network I/O."""

import json
import logging
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import quote, quote_plus

import httpx
import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.transport import Transport

from app.config import settings
from app.integrations.sentry import init_sentry
from app.services.providers.oura.oauth import OuraOAuth
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.sentry_redaction import before_breadcrumb, before_send, redact_telemetry

SECRET = "synthetic-rhise5582/access+token?value=#tail"
ENDPOINT = "https://api.ouraring.com/oauth/revoke"
REVOKE_URL = f"{ENDPOINT}?access_token={quote(SECRET, safe='')}&access_token={quote_plus(SECRET)}#private"
logger = logging.getLogger("app.tests.sentry_redaction")


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
        serialized = b"\n".join(self.envelopes).decode()
        for credential in {SECRET, quote(SECRET, safe=""), quote_plus(SECRET)}:
            assert credential not in serialized


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
            raise httpx.ReadTimeout(f"Timed out calling {request.url}", request=request)
        if outcome == "network":
            raise httpx.ConnectError(f"Could not connect to {request.url}", request=request)
        return httpx.Response(int(outcome), request=request)

    def post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", post)
    return requests


@pytest.mark.parametrize(
    "key",
    [
        "access_token",
        "AcCeSs_ToKeN",
        "refresh-token",
        "ID_TOKEN",
        "client_secret",
        "code",
        "code_verifier",
        "Authorization",
        "query_string",
        "query",
        "query_params",
        "fragment",
        "http.query",
        "http.fragment",
        "url.query",
        "url.fragment",
    ],
)
def test_sensitive_fields_are_redacted_recursively_without_mutating_input(key: str) -> None:
    original = {"contexts": [{"oauth": {key: SECRET, "provider": "oura", "status_code": 401}}]}

    redacted = redact_telemetry(original)

    assert redacted["contexts"][0]["oauth"] == {key: "[Filtered]", "provider": "oura", "status_code": 401}
    assert original["contexts"][0]["oauth"][key] == SECRET
    assert redact_telemetry(redacted) == redacted


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (REVOKE_URL, ENDPOINT),
        (
            f"401 for url '{REVOKE_URL}'\nFor more information see https://httpstatuses.com/401",
            f"401 for url '{ENDPOINT}'\nFor more information see https://httpstatuses.com/401",
        ),
        (f"GET {ENDPOINT}#access_token={SECRET}", f"GET {ENDPOINT}"),
        (f"{ENDPOINT}?access_token=truncated-secret", ENDPOINT),
        (f"{ENDPOINT}?access_token=truncated%2", ENDPOINT),
        ("https://example.invalid/path%3Fsafe", "https://example.invalid/path%3Fsafe"),
        (f"{ENDPOINT}?access%5Ftoken={quote(SECRET, safe='')}&ACCESS_TOKEN={SECRET}", ENDPOINT),
        ("safe status=401 provider=oura", "safe status=401 provider=oura"),
    ],
)
def test_embedded_urls_drop_queries_and_fragments_even_when_truncated(message: str, expected: str) -> None:
    assert redact_telemetry(message) == expected
    assert redact_telemetry(redact_telemetry(message)) == expected


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_bearer_credentials_are_filtered_in_arbitrary_strings(scheme: str) -> None:
    redacted = redact_telemetry(f"Authorization: {scheme} synthetic-secret-token")

    assert "synthetic-secret-token" not in redacted
    assert "[Filtered]" in redacted
    assert redact_telemetry(redacted) == redacted


@pytest.mark.parametrize(
    "message",
    [
        f"OAuth response {{'access_token': '{SECRET}', 'provider': 'oura'}}",
        f'client_secret="{SECRET}" provider=oura',
        f'{{"Refresh-Token": "{SECRET}", "status_code": 401}}',
    ],
)
def test_quote_delimited_credentials_in_formatted_text_are_filtered(message: str) -> None:
    redacted = redact_telemetry(message)

    assert SECRET not in redacted
    assert "[Filtered]" in redacted
    assert redact_telemetry(redacted) == redacted


def test_non_sensitive_scalars_and_nested_sequences_are_preserved() -> None:
    value = {"extra": (None, True, 401, [{"provider": "oura", "url": REVOKE_URL}])}

    assert redact_telemetry(value) == {"extra": (None, True, 401, [{"provider": "oura", "url": ENDPOINT}])}


@pytest.mark.parametrize(
    "credential",
    [
        'https://example.invalid?synthetic"private-suffix',
        'Bearer synthetic"private-suffix',
        'synthetic"private-suffix',
        "synthetic'private-suffix",
        'synthetic\\"private-suffix',
        "synthetic'\"private-suffix",
    ],
)
def test_escaped_quotes_in_formatted_credentials_do_not_leave_suffixes(credential: str) -> None:
    for message in (json.dumps({"access_token": credential}), repr({"access_token": credential})):
        redacted = redact_telemetry(message)
        assert "private-suffix" not in redacted
        assert redact_telemetry(redacted) == redacted


@pytest.mark.parametrize(
    "message",
    [
        'access_token="synthetic\\"private-suffix',
        "refresh_token='synthetic\\'private-suffix",
        'client_secret="synthetic\\',
    ],
)
def test_truncated_escaped_credential_strings_are_filtered(message: str) -> None:
    assert redact_telemetry(message).endswith("[Filtered]")
    assert "synthetic" not in redact_telemetry(message)


def test_initializer_registers_all_hooks_and_disables_frame_locals(sentry_transport: RecordingTransport) -> None:
    options = sentry_sdk.get_client().options

    assert options["before_send"] is before_send
    assert options["before_send_transaction"] is before_send
    assert options["before_breadcrumb"] is before_breadcrumb
    assert options["include_local_variables"] is False
    assert options["event_scrubber"] is not None


@pytest.mark.parametrize("outcome", [204, 401, 500, "timeout", "network"])
def test_oura_revoke_envelopes_keep_diagnostics_without_credentials(
    monkeypatch: pytest.MonkeyPatch, sentry_transport: RecordingTransport, outcome: int | str
) -> None:
    requests = _mock_revoke(monkeypatch, outcome)
    oauth = OuraOAuth(
        user_repo=MagicMock(),
        connection_repo=MagicMock(),
        provider_name="oura",
        api_base_url="https://api.ouraring.com",
    )
    sentry_sdk.set_tag("provider", "oura")

    try:
        oauth.deregister_user(SECRET)
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError) as error:
        log_and_capture_error(error, logger, f"Failed Oura deregistration: {error}", extra={"provider": "oura"})
    else:
        sentry_sdk.capture_exception(RuntimeError("Unrelated error after successful revoke"))

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.params["access_token"] == SECRET
    sentry_transport.assert_credentials_absent()
    events = sentry_transport.payloads()
    assert events
    assert all(event["tags"]["provider"] == "oura" for event in events)
    if outcome == 204:
        http_breadcrumbs = [crumb for crumb in events[-1]["breadcrumbs"]["values"] if crumb.get("type") == "http"]
        assert http_breadcrumbs[-1]["data"]["url"] == ENDPOINT
        assert http_breadcrumbs[-1]["data"]["http.response.status_code"] == 204
        assert http_breadcrumbs[-1]["data"]["http.query"] == "[Filtered]"
    else:
        expected_type = {
            401: "HTTPStatusError",
            500: "HTTPStatusError",
            "timeout": "ReadTimeout",
            "network": "ConnectError",
        }[outcome]
        exceptions = [entry for event in events for entry in event.get("exception", {}).get("values", [])]
        assert any(entry["type"] == expected_type for entry in exceptions)
        assert all("vars" not in frame for entry in exceptions for frame in entry["stacktrace"]["frames"])
        assert any(
            frame["function"] == "deregister_user" for entry in exceptions for frame in entry["stacktrace"]["frames"]
        )
        assert ENDPOINT in json.dumps(events)
        if isinstance(outcome, int):
            assert str(outcome) in json.dumps(events)


def test_formatted_logging_arguments_and_nested_event_data_are_filtered(sentry_transport: RecordingTransport) -> None:
    logger.error("Oura revoke rejected %s", REVOKE_URL)
    logger.error("Request authorization: %s", "Bearer synthetic-secret-token")
    logger.error("OAuth response %s", {"access_token": SECRET, "provider": "oura"})
    sentry_sdk.capture_event(
        {
            "message": "Structured revoke diagnostics",
            "request": {"url": REVOKE_URL, "query_string": SECRET, "headers": {"Authorization": SECRET}},
            "contexts": {"oauth": {"AcCeSs_ToKeN": SECRET, "refresh_token": SECRET, "provider": "oura"}},
            "extra": {"nested": [{"client_secret": SECRET}], "password": "sdk-default-scrubber-secret"},
            "logentry": {"message": "structured params", "params": [{"id_token": SECRET, "url": REVOKE_URL}]},
        }
    )

    sentry_transport.assert_credentials_absent()
    serialized = b"\n".join(sentry_transport.envelopes).decode()
    assert "synthetic-secret-token" not in serialized
    assert "sdk-default-scrubber-secret" not in serialized
    events = sentry_transport.payloads()
    assert any(event.get("logentry", {}).get("formatted") == f"Oura revoke rejected {ENDPOINT}" for event in events)
    assert events[-1]["contexts"]["oauth"]["provider"] == "oura"


def test_exception_chains_keep_types_and_stack_locations(sentry_transport: RecordingTransport) -> None:
    try:
        try:
            request = httpx.Request("POST", REVOKE_URL)
            httpx.Response(401, request=request).raise_for_status()
        except httpx.HTTPStatusError as cause:
            raise RuntimeError(f"Oura revoke failed: {REVOKE_URL}") from cause
    except RuntimeError as error:
        sentry_sdk.capture_exception(error)

    sentry_transport.assert_credentials_absent()
    exceptions = sentry_transport.payloads()[-1]["exception"]["values"]
    assert [exception["type"] for exception in exceptions] == ["HTTPStatusError", "RuntimeError"]
    assert "401" in exceptions[0]["value"]
    assert ENDPOINT in exceptions[0]["value"]
    assert all(exception["stacktrace"]["frames"] for exception in exceptions)
    assert all("vars" not in frame for exception in exceptions for frame in exception["stacktrace"]["frames"])


def test_sdk_string_truncation_does_not_leave_partial_query_credentials(
    monkeypatch: pytest.MonkeyPatch, sentry_transport: RecordingTransport
) -> None:
    monkeypatch.setitem(sentry_sdk.get_client().options, "max_value_length", 96)
    try:
        raise RuntimeError(f"Revoke failed: {REVOKE_URL}{'x' * 2048}")
    except RuntimeError as error:
        sentry_sdk.capture_exception(error)

    sentry_transport.assert_credentials_absent()
    assert SECRET.split("/")[0] not in b"\n".join(sentry_transport.envelopes).decode()
    assert sentry_transport.payloads()[-1]["exception"]["values"][0]["value"] == f"Revoke failed: {ENDPOINT}"


def test_transactions_filter_automatic_httpx_query_data_and_nested_span_data(
    monkeypatch: pytest.MonkeyPatch, sentry_transport: RecordingTransport
) -> None:
    _mock_revoke(monkeypatch, 204)

    with sentry_sdk.start_transaction(name="oura.revoke", op="provider.revoke"):
        httpx.post(ENDPOINT, params={"access_token": SECRET})
        with sentry_sdk.start_span(op="provider.response", description=f"POST {REVOKE_URL}") as span:
            span.set_data("response", {"refresh_token": SECRET, "query_string": SECRET, "status_code": 204})

    sentry_transport.assert_credentials_absent()
    transactions = sentry_transport.payloads("transaction")
    assert len(transactions) == 1
    spans = transactions[0]["spans"]
    http_span = next(span for span in spans if span["op"] == "http.client")
    assert http_span["data"]["http.query"] == "[Filtered]"
    assert http_span["data"]["http.response.status_code"] == 204
    assert ENDPOINT in http_span["description"]
    response_span = next(span for span in spans if span["op"] == "provider.response")
    assert response_span["description"] == f"POST {ENDPOINT}"
    assert response_span["data"]["response"]["status_code"] == 204
