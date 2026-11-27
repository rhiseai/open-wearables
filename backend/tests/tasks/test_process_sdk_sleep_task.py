"""Deferred SDK sleep: stored once the per-user sleep lock frees up, retried while it is busy."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from celery.exceptions import Retry
from sqlalchemy.orm import Session

from app.integrations.celery.tasks.process_sdk_sleep_task import process_sdk_sleep
from app.integrations.redis_client import get_redis_client
from app.models import DataSource, EventRecord
from app.schemas.providers.mobile_sdk import SyncRequest
from app.services.sdk.sleep_service import SleepLockBusyError
from tests.factories import UserFactory

_MODULE = "app.integrations.celery.tasks.process_sdk_sleep_task"


def _sleep_json(start: datetime, end: datetime) -> str:
    return SyncRequest.model_validate(
        {
            "provider": "apple",
            "sdkVersion": "0.15.0",
            "syncTimestamp": end.isoformat(),
            "data": {
                "sleep": [
                    {
                        "id": str(uuid4()),
                        "stage": "light",
                        "startDate": start.isoformat(),
                        "endDate": end.isoformat(),
                        "source": {"name": "Watch", "device_model": "Watch7,1"},
                    }
                ]
            },
        }
    ).model_dump_json()


def _sleep_records(db: Session, user_id: object) -> list[EventRecord]:
    return db.query(EventRecord).join(DataSource).filter(DataSource.user_id == user_id).all()


@patch("app.services.sdk.sleep_service.celery_app")
@patch(f"{_MODULE}.SessionLocal")
def test_deferred_sleep_is_stored_when_the_lock_is_free(
    mock_session_local: MagicMock, mock_finalize: MagicMock, db: Session
) -> None:
    mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
    mock_session_local.return_value.__exit__ = MagicMock(return_value=False)
    user = UserFactory()
    now = datetime.now(timezone.utc)

    result = process_sdk_sleep(_sleep_json(now - timedelta(hours=1), now), str(user.id), "deferred-batch", False)

    assert result["sleep_saved"] == 1
    [record] = _sleep_records(db, user.id)
    assert record.sleep_detail is not None
    assert record.sleep_detail.sleep_total_duration_minutes == 60


@patch(f"{_MODULE}.SessionLocal")
def test_deferred_sleep_is_retried_while_the_lock_is_busy(mock_session_local: MagicMock, db: Session) -> None:
    mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
    mock_session_local.return_value.__exit__ = MagicMock(return_value=False)
    user = UserFactory()
    now = datetime.now(timezone.utc)
    holder = get_redis_client().lock(f"sleep:lock:{user.id}", timeout=30)
    assert holder.acquire(blocking=False)
    try:
        with (
            patch("app.services.sdk.sleep_service.SLEEP_LOCK_WAIT_SECONDS", 0.1),
            patch.object(process_sdk_sleep, "retry", side_effect=Retry()) as retry,
            pytest.raises(Retry),
        ):
            process_sdk_sleep(_sleep_json(now - timedelta(hours=1), now), str(user.id), "deferred-batch", False)
    finally:
        holder.release()

    assert isinstance(retry.call_args.kwargs["exc"], SleepLockBusyError)
    assert 15 <= retry.call_args.kwargs["countdown"] <= 30
    assert _sleep_records(db, user.id) == []


def _run_with_lock_busy(retries: int) -> tuple[object, MagicMock]:
    """One attempt of the task at ``retries`` with the lock busy for the whole wait."""
    with (
        patch(f"{_MODULE}.SessionLocal", MagicMock()),
        patch(f"{_MODULE}.handle_sleep_data", side_effect=SleepLockBusyError("busy")),
        patch.object(process_sdk_sleep, "retry", side_effect=Retry()),
        patch(f"{_MODULE}.logger") as logger,
    ):
        process_sdk_sleep.push_request(retries=retries)
        try:
            now = datetime.now(timezone.utc)
            return process_sdk_sleep.run(
                _sleep_json(now - timedelta(hours=1), now), "user-1", "deferred-batch", False
            ), logger
        finally:
            process_sdk_sleep.pop_request()


def test_a_busy_lock_before_the_last_attempt_is_retried() -> None:
    with pytest.raises(Retry):
        _run_with_lock_busy(process_sdk_sleep.max_retries - 1)


def test_after_the_last_attempt_the_dropped_sleep_is_reported() -> None:
    result, logger = _run_with_lock_busy(process_sdk_sleep.max_retries)

    assert result == {"batch_id": "deferred-batch", "sleep_saved": 0, "sleep_dropped": 1}
    assert logger.error.call_args.kwargs["alert"] is True
