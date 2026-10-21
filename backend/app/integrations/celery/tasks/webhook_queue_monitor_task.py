"""Periodic depth report for the outgoing webhook queues, for log-based alarms."""

from __future__ import annotations

from logging import getLogger

from celery import shared_task

from app.config import settings
from app.constants.celery_tasks import CeleryTask
from app.integrations.redis_client import get_redis_client
from app.services.outgoing_webhooks.routing import WEBHOOK_QUEUES
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)


@shared_task(name=CeleryTask.REPORT_WEBHOOK_QUEUE_DEPTH, ignore_result=True)
def report_webhook_queue_depth() -> dict[str, int]:
    """Log ``LLEN`` of each outgoing webhook queue (one ``webhook_queue_depth`` line per queue).

    The Celery Redis transport keeps a queue's pending messages in a list named after it.
    """
    client = get_redis_client()
    depths: dict[str, int] = {}
    for queue in WEBHOOK_QUEUES:
        depth = int(client.llen(queue))
        depths[queue] = depth
        over = depth > settings.webhook_queue_depth_warn_threshold
        log_structured(
            logger,
            "warning" if over else "info",
            "Outgoing webhook queue depth",
            action="webhook_queue_depth",
            queue=queue,
            depth=depth,
            threshold=settings.webhook_queue_depth_warn_threshold,
            over_threshold=over,
        )
    return depths
