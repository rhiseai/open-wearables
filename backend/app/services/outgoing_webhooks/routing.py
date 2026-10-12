"""Queue routing for outgoing webhook events.

Product events (a new connection, a finished night, a workout, a completed sync) go to
``webhook_priority``, which has a worker of its own, so they never wait behind the bulk of
per-type timeseries events on ``webhook_outgoing``.
"""

from __future__ import annotations

from typing import Any

from app.schemas.webhooks.event_types import WebhookEventType

EMIT_WEBHOOK_TASK_NAME = "app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event"

WEBHOOK_PRIORITY_QUEUE = "webhook_priority"
WEBHOOK_BULK_QUEUE = "webhook_outgoing"
WEBHOOK_QUEUES = (WEBHOOK_PRIORITY_QUEUE, WEBHOOK_BULK_QUEUE)

# Matched on the resource part, so future actions (workout.updated, connection.*) route the same way.
_PRIORITY_RESOURCES = frozenset({"connection", "sleep", "workout", "body_composition"})
_PRIORITY_EVENT_TYPES = frozenset({WebhookEventType.SYNC_COMPLETED.value})


def is_priority_event(event_type: str) -> bool:
    return event_type in _PRIORITY_EVENT_TYPES or event_type.split(".", 1)[0] in _PRIORITY_RESOURCES


def webhook_queue_for(event_type: str) -> str:
    return WEBHOOK_PRIORITY_QUEUE if is_priority_event(event_type) else WEBHOOK_BULK_QUEUE


def route_webhook_task(
    name: str,
    args: tuple[Any, ...] | list[Any] | None,
    kwargs: dict[str, Any] | None,
    options: dict[str, Any],
    task: Any = None,
    **_: Any,
) -> dict[str, str] | None:
    """Celery ``task_routes`` router: pick the queue from the task's ``event_type`` argument."""
    if name != EMIT_WEBHOOK_TASK_NAME:
        return None
    event_type = args[0] if args else (kwargs or {}).get("event_type")
    if not isinstance(event_type, str):
        return {"queue": WEBHOOK_BULK_QUEUE}
    return {"queue": webhook_queue_for(event_type)}
