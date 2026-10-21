import random
from logging import getLogger
from typing import Any

from celery import Task, shared_task

from app.database import SessionLocal
from app.schemas.providers.mobile_sdk import SyncRequest as SDKSyncRequest
from app.services.outgoing_webhooks.batching import collect_sdk_webhooks
from app.services.sdk.sleep_service import SleepLockBusyError, handle_sleep_data
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

_LOCK_RETRY_BASE_SECONDS = 15
_LOCK_RETRY_MAX_SECONDS = 300
_LOCK_RETRY_JITTER_SECONDS = 15


@shared_task(bind=True, queue="sdk_sync", max_retries=8)
def process_sdk_sleep(
    task: Task,
    request_json: str,
    user_id: str,
    batch_id: str,
    historical: bool,
) -> dict[str, Any]:
    """Process the sleep of an SDK batch whose upload found the user's sleep lock busy.

    A burst of batches for one user (a historical export) queues on the per-user sleep
    lock, and the tail of that queue outlasts the lock wait. The batch's other data is
    already committed, so only its sleep comes here and waits in the broker, not in a
    worker thread, until the lock frees up. Exhausted retries raise ``SleepLockBusyError``.
    """
    request = SDKSyncRequest.model_validate_json(request_json)
    with SessionLocal() as db, collect_sdk_webhooks(batch_id, historical=historical) as webhook_batch:
        try:
            handle_sleep_data(db, request, user_id)
        except SleepLockBusyError as exc:
            countdown = min(_LOCK_RETRY_BASE_SECONDS * (2**task.request.retries), _LOCK_RETRY_MAX_SECONDS)
            countdown += random.uniform(0, _LOCK_RETRY_JITTER_SECONDS)
            log_structured(
                logger,
                "warning",
                "Retrying deferred SDK sleep while the sleep lock is busy",
                action="sdk_sleep_lock_retry",
                batch_id=batch_id,
                user_id=user_id,
                retry_number=task.request.retries + 1,
                countdown_seconds=round(countdown, 1),
            )
            raise task.retry(exc=exc, countdown=countdown)
        webhook_summary = webhook_batch.flush()

    sleep_saved = len(request.data.sleep)
    log_structured(
        logger,
        "info",
        "Deferred SDK sleep processed",
        action="sdk_sleep_deferred_complete",
        batch_id=batch_id,
        user_id=user_id,
        sleep_saved=sleep_saved,
        retries=task.request.retries,
    )
    return {"batch_id": batch_id, "sleep_saved": sleep_saved, "webhooks": webhook_summary}
