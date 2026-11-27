from datetime import datetime, timezone
from typing import cast
from uuid import UUID

from celery import shared_task

from app.constants.celery_tasks import CeleryTask
from app.database import SessionLocal
from app.integrations.redis_client import get_redis_client
from app.services.event_record_service import event_record_service
from app.services.sdk.sleep_service import (
    active_users_key,
    delete_sleep_state,
    finish_sleep,
    is_ready_to_finish,
    key,
    load_sleep_state,
    load_source_sleep_states,
    prune_sleep_state_registry,
    release_sleep_lock,
)
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)


@shared_task(name=CeleryTask.FINALIZE_STALE_SLEEPS)
def finalize_stale_sleeps() -> None:
    """Housekeeping: close Redis sleep sessions that have gone quiet.

    Sessions are already flushed to Postgres on every SDK batch. This task only
    needs to persist+close Redis state once :func:`is_ready_to_finish` says the
    source's sleep gap has passed (or re-flush if an earlier write failed), and
    to drop users whose states expired from the active set.
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
                        if is_ready_to_finish(state, now):
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
                logger.error("Error finalizing stale sleep", exc_info=e, user_id=user_id)
