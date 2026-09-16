"""Convenience helpers for emitting outgoing webhook events.

Call these functions after data is committed to the database.
Each schedules a Celery task and returns immediately — Svix delivery
happens in the worker process.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any
from uuid import UUID

from app.constants.devices_map import resolve_device_name
from app.constants.webhooks.events import SERIES_TYPE_TO_GROUP_EVENT
from app.schemas.webhooks.event_types import WebhookEventType
from app.services.outgoing_webhooks import svix as svix_service
from app.services.outgoing_webhooks.batching import current_sdk_webhook_batch
from app.services.outgoing_webhooks.payloads import split_samples_by_payload_bytes, validate_webhook_payload

logger = logging.getLogger(__name__)

# Svix eventId must match [a-zA-Z0-9\-_.] — colons, plus-signs, and other
# characters in ISO 8601 timestamps are not allowed.
_SVIX_ID_SAFE = re.compile(r"[^a-zA-Z0-9\-_.]")
_SVIX_EVENT_ID_MAX_LENGTH = 128


def _safe_key(raw: str) -> str:
    """Return a bounded key containing only characters allowed by Svix."""
    safe = _SVIX_ID_SAFE.sub("_", raw)
    if len(safe) <= _SVIX_EVENT_ID_MAX_LENGTH:
        return safe
    digest = hashlib.sha256(safe.encode()).hexdigest()[:20]
    return f"{safe[: _SVIX_EVENT_ID_MAX_LENGTH - len(digest) - 1]}.{digest}"


def _enqueue_dispatch(
    event_type: str,
    payload: dict[str, Any],
    *,
    channels: list[str] | None = None,
    idempotency_key: str | None = None,
) -> bool:
    """Validate and enqueue one bounded event. Returns whether it was accepted."""
    if not svix_service.is_enabled():
        return False
    try:
        validate_webhook_payload(event_type, payload)
    except ValueError as exc:
        logger.warning("Dropping invalid webhook event %s: %s", event_type, exc)
        return False
    try:
        from app.integrations.celery.tasks.emit_webhook_event_task import emit_webhook_event

        emit_webhook_event.delay(event_type, payload, channels=channels, idempotency_key=idempotency_key)
        return True
    except Exception:
        logger.warning("Could not enqueue webhook event %s", event_type, exc_info=True)
        return False


def _dispatch(
    event_type: str,
    payload: dict[str, Any],
    *,
    channels: list[str] | None = None,
    idempotency_key: str | None = None,
    coalesce_key: str | None = None,
) -> None:
    """Schedule the Celery emit task.

    Silently drops the event when Svix is not configured or the broker
    (Redis) is unreachable so that data ingestion is never blocked by
    webhook infrastructure.
    """
    if not svix_service.is_enabled():
        return
    try:
        validate_webhook_payload(event_type, payload)
    except ValueError as exc:
        logger.warning("Dropping invalid webhook event %s: %s", event_type, exc)
        return
    batch = current_sdk_webhook_batch()
    if batch is not None:
        batch.add(
            event_type,
            payload,
            channels=channels,
            idempotency_key=idempotency_key,
            coalesce_key=coalesce_key,
        )
        return
    _enqueue_dispatch(
        event_type,
        payload,
        channels=channels,
        idempotency_key=idempotency_key,
    )


def on_workout_created(
    *,
    record_id: UUID,
    user_id: UUID,
    provider: str,
    device: str | None,
    workout_type: str | None,
    start_time: str,
    end_time: str,
    zone_offset: str | None,
    duration_seconds: float | None,
    calories_kcal: float | None = None,
    distance_meters: float | None = None,
    avg_heart_rate_bpm: int | None = None,
    max_heart_rate_bpm: int | None = None,
    elevation_gain_meters: float | None = None,
    avg_pace_sec_per_km: int | None = None,
) -> None:
    _dispatch(
        WebhookEventType.WORKOUT_CREATED,
        {
            "type": WebhookEventType.WORKOUT_CREATED,
            "data": {
                "id": str(record_id),
                "user_id": str(user_id),
                "type": workout_type,
                "start_time": start_time,
                "end_time": end_time,
                "zone_offset": zone_offset,
                "duration_seconds": duration_seconds,
                "source": {"provider": provider, "device": device},
                "calories_kcal": calories_kcal,
                "distance_meters": distance_meters,
                "avg_heart_rate_bpm": avg_heart_rate_bpm,
                "max_heart_rate_bpm": max_heart_rate_bpm,
                "avg_pace_sec_per_km": avg_pace_sec_per_km,
                "elevation_gain_meters": elevation_gain_meters,
            },
        },
        idempotency_key=f"workout.created.{record_id}",
        coalesce_key=f"workout.{record_id}",
        channels=[f"user.{user_id}"],
    )


def on_menstrual_cycle_created(
    *,
    record_id: UUID,
    user_id: UUID,
    provider: str,
    device: str | None,
    start_time: str,
    end_time: str,
    zone_offset: str | None,
    current_phase_type: str | None = None,
    day_in_cycle: int | None = None,
    cycle_length: int | None = None,
    is_predicted_cycle: bool | None = None,
    pregnancy_snapshot: list[dict] | None = None,
) -> None:
    _dispatch(
        WebhookEventType.MENSTRUAL_CYCLE_CREATED,
        {
            "type": WebhookEventType.MENSTRUAL_CYCLE_CREATED,
            "data": {
                "id": str(record_id),
                "user_id": str(user_id),
                "start_time": start_time,
                "end_time": end_time,
                "zone_offset": zone_offset,
                "source": {"provider": provider, "device": device},
                "current_phase_type": current_phase_type,
                "day_in_cycle": day_in_cycle,
                "cycle_length": cycle_length,
                "is_predicted_cycle": is_predicted_cycle,
                "pregnancy_snapshot": pregnancy_snapshot,
            },
        },
        idempotency_key=f"menstrual_cycle.created.{record_id}",
        coalesce_key=f"menstrual_cycle.{record_id}",
        channels=[f"user.{user_id}"],
    )


def _emit_sleep(
    event_type: str,
    *,
    record_id: UUID,
    user_id: UUID,
    provider: str,
    device: str | None,
    start_time: str,
    end_time: str,
    zone_offset: str | None,
    duration_seconds: float | None,
    efficiency_percent: float | None = None,
    stages: dict[str, int | None] | None = None,
    is_nap: bool | None = None,
    source_app: str | None = None,
    device_type: str | None = None,
    sleep_duration_seconds: float | None = None,
    sleep_stage_intervals: list[dict[str, Any]] | None = None,
) -> None:
    # Content-aware idempotency: a provider re-sends the same session as it is
    # finalized (Oura sends the stub early, then updates duration/stages/score).
    # Keying on record_id alone would dedup those updates away, so consumers
    # would keep the first partial reading forever. Include a revision derived
    # from the mutable fields so each distinct sleep state delivers exactly once
    # while true retries (identical content) still dedup.
    revision = hashlib.sha1(
        f"{start_time}|{end_time}|{duration_seconds}|{efficiency_percent}|{stages}".encode()
    ).hexdigest()[:12]
    _dispatch(
        event_type,
        {
            "type": event_type,
            "data": {
                "id": str(record_id),
                "user_id": str(user_id),
                "start_time": start_time,
                "end_time": end_time,
                "zone_offset": zone_offset,
                "duration_seconds": duration_seconds,
                "sleep_duration_seconds": sleep_duration_seconds,
                "source": {
                    "provider": provider,
                    "source": source_app,
                    "device": device,
                    "device_type": device_type,
                    "device_name": resolve_device_name(device),
                },
                "efficiency_percent": efficiency_percent,
                "stages": stages,
                "sleep_stage_intervals": sleep_stage_intervals,
                "is_nap": is_nap,
            },
        },
        idempotency_key=f"{event_type}.{record_id}.{revision}",
        coalesce_key=f"sleep.{record_id}",
        channels=[f"user.{user_id}"],
    )


def on_sleep_created(**kwargs: Any) -> None:
    """A new sleep session was saved (first ingestion or a fresh merged session)."""
    _emit_sleep(WebhookEventType.SLEEP_CREATED, **kwargs)


def on_sleep_updated(**kwargs: Any) -> None:
    """An existing sleep session was updated in place (finalized duration/stages/score)."""
    _emit_sleep(WebhookEventType.SLEEP_UPDATED, **kwargs)


def on_timeseries_batch_saved(
    *,
    user_id: UUID,
    provider: str,
    series_type: str,
    sample_count: int,
    start_time: str | None = None,
    end_time: str | None = None,
    samples: list[dict[str, Any]] | None = None,
) -> None:
    """Aggregate this write into the current SDK batch, or emit one group event."""
    samples = samples or []
    batch = current_sdk_webhook_batch()
    if batch is not None:
        batch.add_timeseries(
            user_id=user_id,
            provider=provider,
            series_type=series_type,
            sample_count=sample_count,
            start_time=start_time,
            end_time=end_time,
            samples=samples,
        )
        return
    _emit_timeseries_batch_now(
        user_id=user_id,
        provider=provider,
        series_type=series_type,
        sample_count=sample_count,
        start_time=start_time,
        end_time=end_time,
        samples=samples,
    )


def _emit_timeseries_batch_now(
    *,
    user_id: UUID,
    provider: str,
    series_type: str,
    sample_count: int,
    start_time: str | None,
    end_time: str | None,
    samples: list[dict[str, Any]],
) -> int:
    """Emit the canonical group event, split by exact serialized byte size."""
    event_type = SERIES_TYPE_TO_GROUP_EVENT.get(series_type)
    if event_type is None:
        return 0
    base_data: dict[str, Any] = {
        "user_id": str(user_id),
        "provider": provider,
        "series_type": series_type,
        "sample_count": sample_count,
        "start_time": start_time,
        "end_time": end_time,
    }
    try:
        chunks = split_samples_by_payload_bytes(event_type, base_data, samples)
    except ValueError as exc:
        logger.warning("Dropping oversized/invalid timeseries webhook %s: %s", series_type, exc)
        return 0

    total_chunks = len(chunks)
    emitted = 0
    for chunk_index, chunk in enumerate(chunks):
        data = {
            **base_data,
            "start_time": chunk[0].get("timestamp", start_time) if chunk else start_time,
            "end_time": chunk[-1].get("timestamp", end_time) if chunk else end_time,
            "samples": chunk,
        }
        if total_chunks > 1:
            data.update({"chunk_index": chunk_index, "total_chunks": total_chunks})
        base_key = (
            f"timeseries.{user_id}.{provider}.{series_type}.{start_time or ''}.{end_time or ''}.chunk{chunk_index}"
        )
        if _enqueue_dispatch(
            event_type,
            {"type": event_type, "data": data},
            idempotency_key=_safe_key(f"{base_key}.{event_type}"),
            channels=[f"user.{user_id}"],
        ):
            emitted += 1
    return emitted


def on_connection_created(
    *,
    user_id: UUID,
    provider: str,
    connection_id: UUID,
    connected_at: str,
) -> None:
    _dispatch(
        WebhookEventType.CONNECTION_CREATED,
        {
            "type": WebhookEventType.CONNECTION_CREATED,
            "data": {
                "user_id": str(user_id),
                "provider": provider,
                "connection_id": str(connection_id),
                "connected_at": connected_at,
            },
        },
        idempotency_key=_safe_key(f"connection.created.{user_id}.{provider}.{connected_at}"),
        channels=[f"user.{user_id}"],
    )


def on_connection_revoked(
    *,
    user_id: UUID,
    provider: str,
    connection_id: UUID,
    reason: str,
    revoked_at: str,
) -> None:
    """Emit when a connection becomes invalid and the user must re-authorize.

    ``reason`` is a short cause, e.g. ``"refresh_failed"`` or
    ``"deregistration"``.
    """
    _dispatch(
        WebhookEventType.CONNECTION_REVOKED,
        {
            "type": WebhookEventType.CONNECTION_REVOKED,
            "data": {
                "user_id": str(user_id),
                "provider": provider,
                "connection_id": str(connection_id),
                "reason": reason,
                "revoked_at": revoked_at,
            },
        },
        idempotency_key=_safe_key(f"connection.revoked.{user_id}.{provider}.{revoked_at}"),
        channels=[f"user.{user_id}"],
    )


def on_sync_started(
    *,
    user_id: UUID,
    provider: str,
    source: str,
    run_id: str,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    _dispatch(
        WebhookEventType.SYNC_STARTED,
        {
            "type": WebhookEventType.SYNC_STARTED,
            "data": {
                "user_id": str(user_id),
                "provider": provider,
                "source": source,
                "run_id": run_id,
                "message": message,
                "metadata": metadata or {},
            },
        },
        idempotency_key=f"sync.started.{run_id}",
        channels=[f"user.{user_id}"],
    )


def on_sync_completed(
    *,
    user_id: UUID,
    provider: str,
    source: str,
    run_id: str,
    status: str,
    message: str | None = None,
    items_processed: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    _dispatch(
        WebhookEventType.SYNC_COMPLETED,
        {
            "type": WebhookEventType.SYNC_COMPLETED,
            "data": {
                "user_id": str(user_id),
                "provider": provider,
                "source": source,
                "run_id": run_id,
                "status": status,
                "message": message,
                "items_processed": items_processed,
                "metadata": metadata or {},
            },
        },
        idempotency_key=f"sync.completed.{run_id}",
        channels=[f"user.{user_id}"],
    )


def on_sync_failed(
    *,
    user_id: UUID,
    provider: str,
    source: str,
    run_id: str,
    error: str,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    _dispatch(
        WebhookEventType.SYNC_FAILED,
        {
            "type": WebhookEventType.SYNC_FAILED,
            "data": {
                "user_id": str(user_id),
                "provider": provider,
                "source": source,
                "run_id": run_id,
                "error": error,
                "message": message,
                "metadata": metadata or {},
            },
        },
        idempotency_key=f"sync.failed.{run_id}",
        channels=[f"user.{user_id}"],
    )
