"""Tests for access-log level derivation and the access-log middleware."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings, settings
from app.main import http_exception_handler_with_body_log
from app.middlewares import add_access_log_middleware
from app.utils.auth_diagnostics import AuthReason, SDKAuthenticationError
from app.utils.config_utils import AccessLogLevel, EnvironmentType, LogFormat


class TestAccessLogLevelDerivation:
    @pytest.mark.parametrize(
        ("environment", "expected"),
        [
            (EnvironmentType.PRODUCTION, AccessLogLevel.ERRORS),
            (EnvironmentType.STAGING, AccessLogLevel.ALL),
            (EnvironmentType.LOCAL, AccessLogLevel.ALL),
            (EnvironmentType.TEST, AccessLogLevel.ALL),
        ],
    )
    def test_default_is_derived_from_environment(self, environment: EnvironmentType, expected: AccessLogLevel) -> None:
        assert Settings(environment=environment, access_log_level=None).access_log_level == expected

    def test_explicit_value_overrides_derivation(self) -> None:
        settings = Settings(environment=EnvironmentType.PRODUCTION, access_log_level=AccessLogLevel.ALL)
        assert settings.access_log_level == AccessLogLevel.ALL


def _build_client(level: AccessLogLevel) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, http_exception_handler_with_body_log)

    @app.get("/expired-access-token")
    async def expired_access_token() -> None:
        raise SDKAuthenticationError(AuthReason.SDK_TOKEN_EXPIRED)

    @app.get("/ordinary-401")
    async def ordinary_401() -> None:
        raise HTTPException(status_code=401, detail="Authentication required: provide SDK token or API key")

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/client-error")
    async def client_error() -> None:
        raise HTTPException(status_code=404)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    with patch.object(settings, "access_log_level", level):
        add_access_log_middleware(app)

    return TestClient(app, raise_server_exceptions=False)


def _logged_statuses(mock: MagicMock) -> list[int]:
    return [call.kwargs["status"] for call in mock.call_args_list]


class TestAccessLogMiddleware:
    def test_errors_mode_keeps_expiry_as_warning_without_matching_path_or_detail(self) -> None:
        client = _build_client(AccessLogLevel.ERRORS)
        with patch("app.middlewares.log_structured") as mock:
            response = client.get("/expired-access-token")
            assert response.status_code == 401
            client.get("/ordinary-401")
            client.get("/boom")
        assert _logged_statuses(mock) == [401, 401, 500]
        assert [call.args[1] for call in mock.call_args_list] == ["warning", "error", "error"]
        assert mock.call_args_list[0].kwargs["auth_reason"] == "sdk_token_expired"
        assert "auth_reason" not in mock.call_args_list[1].kwargs
        assert "auth_reason" not in mock.call_args_list[2].kwargs

    def test_all_level_logs_every_status(self) -> None:
        client = _build_client(AccessLogLevel.ALL)
        with patch("app.middlewares.logger.log") as mock:
            client.get("/ok")
            client.get("/client-error")
            client.get("/boom")
        assert _logged_statuses(mock) == [200, 404, 500]

    def test_errors_level_drops_2xx_keeps_4xx_5xx(self) -> None:
        client = _build_client(AccessLogLevel.ERRORS)
        with patch("app.middlewares.logger.log") as mock:
            client.get("/ok")
            client.get("/client-error")
            client.get("/boom")
        assert _logged_statuses(mock) == [404, 500]

    def test_off_level_logs_nothing(self) -> None:
        client = _build_client(AccessLogLevel.OFF)
        with patch("app.middlewares.logger.log") as mock:
            client.get("/ok")
            client.get("/client-error")
        mock.assert_not_called()

    def test_log_level_is_error_for_4xx_and_5xx(self) -> None:
        client = _build_client(AccessLogLevel.ALL)
        with patch("app.middlewares.logger.log") as mock:
            client.get("/ok")
            client.get("/client-error")
        levels = [call.args[0] for call in mock.call_args_list]
        assert levels == [logging.INFO, logging.ERROR]

    def test_query_string_is_preserved_in_path(self) -> None:
        client = _build_client(AccessLogLevel.ALL)
        with patch("app.middlewares.logger.log") as mock:
            client.get("/ok?limit=5&sort_by=created_at")
        assert mock.call_args_list[0].kwargs["path"] == "/ok?limit=5&sort_by=created_at"

    @pytest.mark.parametrize(
        "key",
        ["token", "verification_token", "hub.verify_token"],
    )
    def test_secret_query_values_are_redacted(self, key: str) -> None:
        # These authenticate unsigned provider webhooks, so a logged value is a
        # usable credential — Withings' grants a forced disconnect.
        client = _build_client(AccessLogLevel.ALL)
        with patch("app.middlewares.logger.log") as mock:
            client.get(f"/ok?{key}=s3cret&appli=1")
        path = mock.call_args_list[0].kwargs["path"]
        assert "s3cret" not in path
        assert f"{key}=REDACTED" in path
        assert "appli=1" in path

    def test_webhook_callback_token_is_redacted(self) -> None:
        # The secret path segment is the only credential of an unsigned Strava event.
        client = _build_client(AccessLogLevel.ALL)
        with patch("app.middlewares.logger.log") as mock:
            client.post("/api/v1/providers/strava/webhooks/s3cret?hub.verify_token=v3rify")
        path = mock.call_args_list[0].kwargs["path"]
        assert path == "/api/v1/providers/strava/webhooks/redacted?hub.verify_token=REDACTED"

    def test_garmin_webhook_callback_token_is_redacted(self) -> None:
        # Garmin does not sign its notifications either; the operator sets this URL in its portal.
        client = _build_client(AccessLogLevel.ALL)
        with patch("app.middlewares.logger.log") as mock:
            client.post("/api/v1/providers/garmin/webhooks/s3cret")
        assert mock.call_args_list[0].kwargs["path"] == "/api/v1/providers/garmin/webhooks/redacted"

    def test_webhook_callback_token_in_a_query_value_is_redacted(self) -> None:
        # Subscription endpoints take the callback URL as ?callback_url=.
        client = _build_client(AccessLogLevel.ALL)
        callback = "https://example.com/api/v1/providers/strava/webhooks/s3cret"
        with patch("app.middlewares.logger.log") as mock:
            client.get("/ok", params={"callback_url": callback})
        path = mock.call_args_list[0].kwargs["path"]
        assert "s3cret" not in path
        assert "callback_url=https%3A%2F%2Fexample.com%2Fapi%2Fv1%2Fproviders%2Fstrava%2Fwebhooks%2Fredacted" in path

    def test_sdk_attribution_headers_are_logged(self) -> None:
        client = _build_client(AccessLogLevel.ALL)
        with patch("app.middlewares.logger.log") as mock:
            client.get(
                "/ok",
                headers={
                    "X-Open-Wearables-SDK-Version": "0.15.0",
                    "X-Open-Wearables-SDK-Platform": "ios",
                    "X-Request-Id": "req-1",
                    "X-Open-Wearables-Outbox-Item": "combined_item_abc",
                    "User-Agent": "OpenWearablesHealthSDK/0.15.0 (iOS 18.1.0; iPhone15,2)",
                },
            )
        attributes = mock.call_args_list[0].kwargs
        assert attributes["sdk_version"] == "0.15.0"
        assert attributes["sdk_platform"] == "ios"
        assert attributes["request_id"] == "req-1"
        assert attributes["outbox_item_id"] == "combined_item_abc"
        assert attributes["user_agent"] == "OpenWearablesHealthSDK/0.15.0 (iOS 18.1.0; iPhone15,2)"

    def test_absent_sdk_headers_add_no_fields(self) -> None:
        client = _build_client(AccessLogLevel.ALL)
        with patch("app.middlewares.logger.log") as mock:
            client.get("/ok", headers={"User-Agent": ""})
        attributes = mock.call_args_list[0].kwargs
        for key in ("sdk_version", "sdk_platform", "request_id", "outbox_item_id", "user_agent"):
            assert key not in attributes

    def test_lines_are_written_by_the_access_logger(self, capsys: pytest.CaptureFixture[str]) -> None:
        client = _build_client(AccessLogLevel.ALL)
        with patch.object(settings, "log_format", LogFormat.TEXT):
            client.get("/ok")

        line = capsys.readouterr().out.strip().splitlines()[-1]
        assert " app.access  http_request  " in line

    def test_server_errors_do_not_reach_sentry_as_log_events(self) -> None:
        """The access log carries the traceback as text; the exception itself reaches
        Sentry through the integrations, not as a second event from the log line."""
        client = _build_client(AccessLogLevel.ALL)
        with patch("app.middlewares.logger.log") as mock:
            client.get("/boom")

        assert "exc_info" not in mock.call_args.kwargs
        assert "RuntimeError: kaboom" in mock.call_args.kwargs["traceback"]
