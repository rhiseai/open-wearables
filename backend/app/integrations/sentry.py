from __future__ import annotations

from typing import TYPE_CHECKING

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

from app import __version__
from app.config import settings

if TYPE_CHECKING:
    from sentry_sdk._types import Event


class _CredentialScrubber(EventScrubber):
    def scrub_event(self, event: Event) -> None:
        super().scrub_event(event)
        # The SDK skips contexts and top-level request metadata, including query_string.
        self.scrub_dict(event.get("contexts"))
        self.scrub_dict(event.get("request"))


def init_sentry() -> None:
    if settings.SENTRY_ENABLED:
        release = f"{__version__}+{settings.GIT_SHA[:12]}" if settings.GIT_SHA else __version__
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.SENTRY_ENV,
            server_name=settings.SENTRY_SERVER_NAME,
            release=release,
            traces_sample_rate=settings.SENTRY_SAMPLES_RATE,
            include_local_variables=False,
            event_scrubber=_CredentialScrubber(
                denylist=[
                    *DEFAULT_DENYLIST,
                    "access_token",
                    "refresh_token",
                    "id_token",
                    "client_secret",
                    "code_verifier",
                    "query_string",
                    "http.query",
                    "http.fragment",
                    "url.query",
                    "url.fragment",
                ],
                recursive=True,
            ),
            integrations=[
                CeleryIntegration(
                    monitor_beat_tasks=True,
                    propagate_traces=True,
                ),
            ],
        )
