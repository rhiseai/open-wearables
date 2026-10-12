"""Opt-in gating for the per-type timeseries webhook events.

Timeseries group events (``heart_rate.created``, ``steps.created``, ...) are most of the
outgoing traffic. Svix accepts a message even when no endpoint receives its type, so
emitting a type nobody subscribes to only costs a queue slot and a Svix call. With
``OUTGOING_WEBHOOKS_TIMESERIES_EVENTS=subscribed`` they are emitted only while some enabled
endpoint receives them. Each process caches the subscribed set for
``OUTGOING_WEBHOOKS_SUBSCRIPTION_CACHE_SECONDS``, so a new subscription takes effect within
that window.
"""

from __future__ import annotations

import logging
import threading
import time

from app.config import settings
from app.constants.webhooks.events import SERIES_TYPE_TO_GROUP_EVENT
from app.schemas.webhooks.event_types import WebhookEventType
from app.services.outgoing_webhooks import svix as svix_service
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

# body_composition.created is rare and consumed as a product event, so it is never gated.
GATED_TIMESERIES_EVENT_TYPES: frozenset[str] = frozenset(
    str(event_type) for event_type in SERIES_TYPE_TO_GROUP_EVENT.values()
) - {WebhookEventType.BODY_COMPOSITION_CREATED.value}


class _SubscriptionCache:
    """Process-wide, TTL-bounded view of which event types some endpoint receives."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._expires_at = 0.0
        # None = every type is received. Also the value until Svix was read once, so a
        # failed first read keeps the pre-gating behaviour instead of dropping events.
        self._value: frozenset[str] | None = None

    def get(self) -> frozenset[str] | None:
        if time.monotonic() < self._expires_at:
            return self._value
        with self._lock:
            if time.monotonic() < self._expires_at:
                return self._value
            try:
                self._value = svix_service.subscribed_event_types()
            except Exception as exc:
                log_structured(
                    logger,
                    "warning",
                    "Could not read webhook endpoint subscriptions; keeping the previous view",
                    action="webhook_subscriptions_refresh_failed",
                    error=str(exc),
                )
            self._expires_at = time.monotonic() + settings.outgoing_webhooks_subscription_cache_seconds
            return self._value

    def clear(self) -> None:
        with self._lock:
            self._expires_at = 0.0
            self._value = None


_subscriptions = _SubscriptionCache()


def timeseries_event_enabled(event_type: str) -> bool:
    """Whether a timeseries group event of this type should be emitted at all."""
    if event_type not in GATED_TIMESERIES_EVENT_TYPES:
        return True
    mode = settings.outgoing_webhooks_timeseries_events
    if mode == "all":
        return True
    if mode == "none":
        return False
    subscribed = _subscriptions.get()
    return subscribed is None or event_type in subscribed


def clear_subscription_cache() -> None:
    _subscriptions.clear()
