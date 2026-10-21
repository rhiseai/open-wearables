"""Source isolation and failure recovery across SDK sleep batches."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from redis.exceptions import LockNotOwnedError
from sqlalchemy.orm import Session

from app.integrations.celery.tasks.finalize_stale_sleep_task import finalize_stale_sleeps
from app.integrations.redis_client import get_redis_client
from app.models import DataSource, EventRecord
from app.schemas.providers.mobile_sdk import SyncRequest
from app.services.sdk.import_service import import_service
from app.services.sdk.sleep_service import (
    active_users_key,
    handle_sleep_data,
    key,
    load_source_sleep_states,
    save_sleep_state,
    source_states_key,
)
from tests.factories import UserFactory


def _request(*samples: dict, provider: str = "apple") -> SyncRequest:
    return SyncRequest.model_validate(
        {
            "provider": provider,
            "sdkVersion": "0.15.0",
            "syncTimestamp": datetime.now(timezone.utc).isoformat(),
            "data": {"sleep": samples, "workouts": [], "records": []},
        }
    )


def _sample(
    start: datetime, end: datetime, *, source: str = "Watch", device: str = "Watch7,1", stage: str = "light"
) -> dict:
    return {
        "id": str(uuid4()),
        "stage": stage,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "source": {"name": source, "device_model": device},
    }


@patch("app.services.sdk.sleep_service.celery_app")
def test_sources_and_devices_have_separate_states_and_records(mock_finalize: MagicMock, db: Session) -> None:
    user = UserFactory()
    now = datetime.now(timezone.utc)
    handle_sleep_data(
        db,
        _request(
            _sample(now - timedelta(hours=1), now, source="Watch", device="Watch7,1"),
            _sample(now - timedelta(hours=1), now, source="Watch", device="Watch8,1"),
            _sample(now - timedelta(hours=1), now, source="Oura", device="Watch7,1"),
        ),
        str(user.id),
    )

    records = db.query(EventRecord).join(DataSource).filter(DataSource.user_id == user.id).all()
    assert len(records) == 3
    for record in records:
        assert record.sleep_detail is not None
        assert record.sleep_detail.sleep_total_duration_minutes == 60
    states = load_source_sleep_states(str(user.id))
    assert {(state.source_name, state.device_model) for state in states.values()} == {
        ("Watch", "Watch7,1"),
        ("Watch", "Watch8,1"),
        ("Oura", "Watch7,1"),
    }


@patch("app.services.sdk.sleep_service.celery_app")
def test_expired_lease_and_stale_redis_cannot_shrink_committed_sleep(mock_finalize: MagicMock, db: Session) -> None:
    user = UserFactory()
    user_id = str(user.id)
    now = datetime.now(timezone.utc)
    first = _sample(now - timedelta(hours=2), now - timedelta(hours=1))
    handle_sleep_data(db, _request(first), user_id)
    stale = next(iter(load_source_sleep_states(user_id).values())).model_copy(deep=True)
    handle_sleep_data(db, _request(_sample(now - timedelta(hours=1), now, stage="deep")), user_id)
    handle_sleep_data(db, _request(_sample(now - timedelta(hours=1), now, source="Oura")), user_id)

    # A previous lease owner publishes an older snapshot after a newer owner committed.
    save_sleep_state(user_id, stale)
    lease = MagicMock()
    lease.acquire.return_value = True
    lease.release.side_effect = LockNotOwnedError("a new worker owns this lease")
    redis_client = get_redis_client()
    with patch.object(redis_client, "lock", return_value=lease):
        handle_sleep_data(db, _request(first), user_id)

    records = db.query(EventRecord).join(DataSource).filter(DataSource.user_id == user.id).all()
    assert len(records) == 2
    details = []
    for record in records:
        assert record.sleep_detail is not None
        assert record.sleep_detail.sleep_total_duration_minutes is not None
        details.append(record.sleep_detail)
    assert {detail.sleep_total_duration_minutes for detail in details} == {60, 120}
    watch = next(detail for detail in details if detail.sleep_total_duration_minutes == 120)
    assert watch.sleep_light_minutes == 60
    assert watch.sleep_deep_minutes == 60


@patch("app.services.sdk.sleep_service.celery_app")
def test_provider_score_failure_rolls_back_sleep_and_keeps_cache(mock_finalize: MagicMock, db: Session) -> None:
    user_id = str(UserFactory().id)
    now = datetime.now(timezone.utc)
    handle_sleep_data(db, _request(_sample(now - timedelta(hours=2), now - timedelta(hours=1))), user_id)
    before = next(iter(load_source_sleep_states(user_id).values()))
    before_json = get_redis_client().get(key(user_id, before))
    failure = RuntimeError("provider score write failed")
    with (
        patch("app.services.sdk.sleep_service._save_provider_sleep_score", side_effect=failure),
        pytest.raises(RuntimeError) as caught,
    ):
        handle_sleep_data(db, _request(_sample(now - timedelta(hours=1), now, stage="deep")), user_id)
    assert caught.value is failure
    assert get_redis_client().get(key(user_id, before)) == before_json
    record = db.query(EventRecord).filter(EventRecord.external_id == before.uuid).one()
    assert record.sleep_detail is not None
    assert record.sleep_detail.sleep_total_duration_minutes == 60


@pytest.mark.parametrize("failure_stage", ["write", "commit"])
def test_database_failure_never_publishes_state(failure_stage: str) -> None:
    db = MagicMock()
    redis_client = MagicMock()
    redis_client.get.return_value = None
    redis_client.smembers.return_value = set()
    failure = RuntimeError(f"{failure_stage} failed")
    now = datetime.now(timezone.utc)
    with (
        patch("app.services.sdk.sleep_service.get_redis_client", return_value=redis_client),
        patch("app.services.sdk.sleep_service.event_record_service") as service,
    ):
        if failure_stage == "write":
            service.create_or_merge_sleep.side_effect = failure
        else:
            db.commit.side_effect = failure
        with pytest.raises(RuntimeError) as caught:
            handle_sleep_data(db, _request(_sample(now - timedelta(hours=1), now)), str(uuid4()))
    assert caught.value is failure
    db.rollback.assert_called()
    redis_client.set.assert_not_called()
    redis_client.delete.assert_not_called()


def test_lock_miss_is_an_import_failure_and_captures_original_error() -> None:
    db = MagicMock()
    redis_client = MagicMock()
    redis_client.lock.return_value.acquire.return_value = False
    now = datetime.now(timezone.utc)
    request = _request(_sample(now - timedelta(hours=1), now))
    with (
        patch("app.services.sdk.sleep_service.get_redis_client", return_value=redis_client),
        patch("app.services.sdk.import_service.log_and_capture_error") as capture,
    ):
        result = import_service.import_data_from_request(
            db, request.model_dump_json(), "application/json", str(uuid4()), batch_id="failed-lock-batch"
        )
    assert result.status_code == 400
    assert result.sleep_saved == 0
    assert "Could not acquire sleep processing lock" in result.response
    failure = capture.call_args[0][0]
    assert isinstance(failure, RuntimeError)
    db.rollback.assert_called()
    redis_client.set.assert_not_called()
    redis_client.lock.return_value.release.assert_not_called()


@patch("app.services.sdk.sleep_service.celery_app")
def test_legacy_state_keeps_original_identity_while_new_provider_is_separate(
    mock_finalize: MagicMock, db: Session
) -> None:
    user_id = str(UserFactory().id)
    now = datetime.now(timezone.utc)
    handle_sleep_data(db, _request(_sample(now - timedelta(hours=1), now)), user_id)
    legacy = next(iter(load_source_sleep_states(user_id).values()))
    redis_client = get_redis_client()
    redis_client.set(key(user_id), legacy.model_dump_json())
    redis_client.delete(key(user_id, legacy))
    redis_client.delete(source_states_key(user_id))

    handle_sleep_data(
        db, _request(_sample(now - timedelta(hours=1), now, source="Galaxy Watch"), provider="samsung"), user_id
    )
    states = load_source_sleep_states(user_id)
    assert {state.provider for state in states.values()} == {"apple", "samsung"}
    assert not redis_client.exists(key(user_id))
    apple = next(state for state in states.values() if state.provider == "apple")
    assert apple.uuid == legacy.uuid
    assert apple.source_name == "Watch"


def test_finalizer_prunes_expired_state_registry(db: Session) -> None:
    user_id = str(uuid4())
    redis_client = get_redis_client()
    redis_client.sadd(active_users_key(), user_id)
    redis_client.sadd(source_states_key(user_id), f"sleep:active:{user_id}:expired")
    with patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal") as factory:
        factory.return_value.__enter__.return_value = db
        finalize_stale_sleeps()
    assert not redis_client.sismember(active_users_key(), user_id)
    assert not redis_client.smembers(source_states_key(user_id))


def test_importer_rolls_back_and_reports_original_database_error() -> None:
    db = MagicMock()
    failure = RuntimeError("original integrity failure")
    with (
        patch.object(import_service, "load_data", side_effect=failure),
        patch("app.services.sdk.import_service.log_and_capture_error") as capture,
    ):
        result = import_service.import_data_from_request(
            db, json.dumps({"provider": "apple", "data": {}}), "application/json", str(uuid4())
        )
    assert result.status_code == 400
    assert result.response == "Import failed: original integrity failure"
    assert capture.call_args[0][0] is failure
    db.rollback.assert_called_once()


@patch("app.services.sdk.sleep_service.celery_app")
def test_enclosing_sample_expands_both_cached_boundaries(mock_finalize: MagicMock, db: Session) -> None:
    user_id = str(UserFactory().id)
    now = datetime.now(timezone.utc)
    handle_sleep_data(db, _request(_sample(now - timedelta(hours=2), now - timedelta(hours=1))), user_id)
    original = next(iter(load_source_sleep_states(user_id).values()))
    handle_sleep_data(db, _request(_sample(now - timedelta(hours=3), now, stage="in_bed")), user_id)
    state = next(iter(load_source_sleep_states(user_id).values()))
    assert state.uuid == original.uuid
    assert state.start_time == now - timedelta(hours=3)
    assert state.end_time == now


@patch("app.services.sdk.sleep_service.celery_app")
def test_owner_lock_precedes_reads_and_commit_precedes_cache_publication(mock_finalize: MagicMock) -> None:
    events = []
    db = MagicMock()
    db.commit.side_effect = lambda: events.append("commit")
    redis_client = MagicMock()
    redis_client.get.side_effect = lambda _: events.append("read")
    redis_client.smembers.return_value = set()
    redis_client.set.side_effect = lambda *_: events.append("publish")
    now = datetime.now(timezone.utc)
    with (
        patch("app.services.sdk.sleep_service.get_redis_client", return_value=redis_client),
        patch("app.services.sdk.sleep_service.event_record_service") as service,
    ):
        service.lock_sleep_user.side_effect = lambda *_: events.append("owner")
        service.create_or_merge_sleep.side_effect = lambda *_args, **_kwargs: events.append("write")
        handle_sleep_data(db, _request(_sample(now - timedelta(hours=1), now)), str(uuid4()))
    assert events.index("owner") < events.index("read") < events.index("write")
    assert events.index("write") < events.index("commit") < events.index("publish")
    db.commit.assert_called_once()


def test_finalizer_busy_lease_defers_without_reporting_an_error() -> None:
    redis_client = MagicMock()
    redis_client.smembers.return_value = {str(uuid4())}
    redis_client.lock.return_value.acquire.return_value = False
    with (
        patch("app.integrations.celery.tasks.finalize_stale_sleep_task.get_redis_client", return_value=redis_client),
        patch("app.integrations.celery.tasks.finalize_stale_sleep_task.SessionLocal"),
        patch("app.integrations.celery.tasks.finalize_stale_sleep_task.load_sleep_state") as load,
        patch("app.integrations.celery.tasks.finalize_stale_sleep_task.log_and_capture_error") as capture,
    ):
        finalize_stale_sleeps()
    load.assert_not_called()
    capture.assert_not_called()
    redis_client.lock.return_value.release.assert_not_called()
