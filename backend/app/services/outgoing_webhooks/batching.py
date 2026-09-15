"""SDK-scoped coalescing for outgoing webhook events."""

from __future__ import annotations

import logging
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator
from uuid import UUID

logger = logging.getLogger(__name__)

MAX_REALTIME_EVENTS_PER_SDK_BATCH = 64


@dataclass
class PendingWebhook:
    event_type: str
    payload: dict[str, Any]
    channels: list[str] | None
    idempotency_key: str | None


@dataclass
class TimeseriesAggregate:
    user_id: UUID
    provider: str
    series_type: str
    sample_count: int = 0
    start_time: str | None = None
    end_time: str | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)


class SDKWebhookBatch:
    """Collect and bound product events produced by one mobile SDK upload."""

    def __init__(self, batch_id: str, *, historical: bool) -> None:
        self.batch_id = batch_id
        self.historical = historical
        self._events: OrderedDict[str, PendingWebhook] = OrderedDict()
        self._timeseries: dict[tuple[UUID, str, str], TimeseriesAggregate] = {}
        self.coalesced = 0
        self.suppressed = 0

    def add(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        channels: list[str] | None,
        idempotency_key: str | None,
        coalesce_key: str | None,
    ) -> None:
        if self.historical:
            self.suppressed += 1
            return
        key = coalesce_key or idempotency_key or f"{event_type}:{len(self._events)}"
        if key in self._events:
            self.coalesced += 1
            self._events[key] = PendingWebhook(event_type, payload, channels, idempotency_key)
            return
        if len(self._events) + len(self._timeseries) >= MAX_REALTIME_EVENTS_PER_SDK_BATCH:
            self.suppressed += 1
            return
        self._events[key] = PendingWebhook(event_type, payload, channels, idempotency_key)

    def add_timeseries(
        self,
        *,
        user_id: UUID,
        provider: str,
        series_type: str,
        sample_count: int,
        start_time: str | None,
        end_time: str | None,
        samples: list[dict[str, Any]],
    ) -> None:
        if self.historical:
            self.suppressed += 1
            return
        key = (user_id, provider, series_type)
        aggregate = self._timeseries.get(key)
        if aggregate is None:
            if len(self._events) + len(self._timeseries) >= MAX_REALTIME_EVENTS_PER_SDK_BATCH:
                self.suppressed += 1
                return
            aggregate = TimeseriesAggregate(user_id=user_id, provider=provider, series_type=series_type)
            self._timeseries[key] = aggregate
        else:
            self.coalesced += 1
        aggregate.sample_count += sample_count
        aggregate.samples.extend(samples)
        if start_time is not None and (aggregate.start_time is None or start_time < aggregate.start_time):
            aggregate.start_time = start_time
        if end_time is not None and (aggregate.end_time is None or end_time > aggregate.end_time):
            aggregate.end_time = end_time

    def flush(self) -> dict[str, int | bool]:
        """Enqueue the bounded aggregate after ingestion commits successfully."""
        # Lazy imports avoid a module cycle: events consults the active batch.
        from app.services.outgoing_webhooks.events import _emit_timeseries_batch_now, _enqueue_dispatch

        emitted = 0
        if not self.historical:
            for pending in self._events.values():
                if _enqueue_dispatch(
                    pending.event_type,
                    pending.payload,
                    channels=pending.channels,
                    idempotency_key=pending.idempotency_key,
                ):
                    emitted += 1
            for aggregate in self._timeseries.values():
                emitted += _emit_timeseries_batch_now(
                    user_id=aggregate.user_id,
                    provider=aggregate.provider,
                    series_type=aggregate.series_type,
                    sample_count=aggregate.sample_count,
                    start_time=aggregate.start_time,
                    end_time=aggregate.end_time,
                    samples=aggregate.samples,
                )
        summary: dict[str, int | bool] = {
            "historical": self.historical,
            "emitted": emitted,
            "coalesced": self.coalesced,
            "suppressed": self.suppressed,
        }
        logger.info("SDK webhook batch completed", extra={"batch_id": self.batch_id, **summary})
        return summary


_CURRENT_SDK_BATCH: ContextVar[SDKWebhookBatch | None] = ContextVar("current_sdk_webhook_batch", default=None)


def current_sdk_webhook_batch() -> SDKWebhookBatch | None:
    return _CURRENT_SDK_BATCH.get()


@contextmanager
def collect_sdk_webhooks(batch_id: str, *, historical: bool) -> Iterator[SDKWebhookBatch]:
    batch = SDKWebhookBatch(batch_id, historical=historical)
    token = _CURRENT_SDK_BATCH.set(batch)
    try:
        yield batch
    finally:
        _CURRENT_SDK_BATCH.reset(token)
