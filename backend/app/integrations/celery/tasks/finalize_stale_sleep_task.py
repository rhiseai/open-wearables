from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import cast
from uuid import UUID

from celery import shared_task

from app.config import settings
from app.constants.celery_tasks import CeleryTask
from app.database import SessionLocal
from app.integrations.redis_client import get_redis_client
from app.services.event_record_service import event_record_service
from app.services.sdk.sleep_service import (
    active_users_key,
    delete_sleep_state,
    finish_sleep,
    key,
    load_sleep_state,
    load_source_sleep_states,
    prune_sleep_state_registry,
    release_sleep_lock,
)
from app.utils.sentry_helpers import log_and_capture_error

logger = getLogger(__name__)


@shared_task(name=CeleryTask.FINALIZE_STALE_SLEEPS)
def finalize_stale_sleeps() -> None:
    """Housekeeping: close Redis sleep sessions that have gone quiet.

    Sessions are already flushed to Postgres on every SDK batch. This task only
    needs to persist+close Redis state once ``sleep_end_gap_minutes`` of quiet
    time has elapsed (or re-flush if an earlier write failed).
    """
    now = datetime.now(timezone.utc)
    redis_client = get_redis_client()

    for user_id in cast(set[str], redis_client.smembers(active_users_key())):
        if isinstance(user_id, bytes):
            user_id = user_id.decode()
        with SessionLocal() as db:
            try:
                # Skip users whose upload is currently in progress.
                lock = redis_client.lock(f"sleep:lock:{user_id}", timeout=30, blocking_timeout=0)
                if not lock.acquire(blocking=False):
                    continue

                try:
                    event_record_service.lock_sleep_user(db, UUID(user_id))
                    states = load_source_sleep_states(user_id)
                    legacy = load_sleep_state(user_id)
                    if legacy is not None:
                        states[key(user_id)] = legacy
                    closed_keys = []
                    for storage_key, state in states.items():
                        end_time = state.end_time
                        if end_time.tzinfo is None:
                            end_time = end_time.replace(tzinfo=timezone.utc)
                        if now - end_time >= timedelta(minutes=settings.sleep_end_gap_minutes):
                            finish_sleep(db, user_id, state, commit=False)
                            closed_keys.append(storage_key)
                    db.commit()
                    for storage_key in closed_keys:
                        delete_sleep_state(user_id, storage_key=storage_key)
                    prune_sleep_state_registry(user_id)
                finally:
                    release_sleep_lock(lock, user_id)

            except Exception as e:
                db.rollback()
                log_and_capture_error(
                    e,
                    logger,
                    "Error finalizing stale sleep",
                    extra={"user_id": user_id},
                )
