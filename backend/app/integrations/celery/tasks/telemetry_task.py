"""Anonymous usage telemetry Celery task.

Runs hourly via beat (plus once on worker startup) and delivers a ping only
when one is due - see TelemetryService for the debounce rules. Best-effort by
design: a failed delivery is logged and retried naturally on the next hourly
run, never raised.
"""

from logging import getLogger

from celery import shared_task

from app.constants.celery_tasks import CeleryTask
from app.database import SessionLocal
from app.services.telemetry_service import telemetry_service
from app.utils.sentry_helpers import log_and_capture_error

logger = getLogger(__name__)


@shared_task(
    name=CeleryTask.SEND_TELEMETRY_PING,
    soft_time_limit=120,
    time_limit=180,
)
def send_telemetry_ping(event: str = "daily") -> str:
    with SessionLocal() as db:
        try:
            return telemetry_service.send_ping(db, event=event)
        except Exception as exc:
            log_and_capture_error(
                exc,
                logger,
                "Telemetry ping delivery failed",
                level="warning",
                extra={"event": event},
            )
            return "failed"
