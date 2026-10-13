"""PostgreSQL regressions for atomic, idempotent sleep component reconciliation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, delete, text
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, HealthScore, SleepDetails, User
from app.schemas.enums import HealthScoreCategory, ProviderName
from app.schemas.model_crud.activities import EventRecordCreate, EventRecordDetailCreate, SleepStage
from app.services.event_record_service import event_record_service
from tests.factories import DataSourceFactory, EventRecordFactory, SleepDetailsFactory, UserFactory


def _dt(hour: int) -> datetime:
    return datetime(2026, 9, 30, tzinfo=timezone.utc) + timedelta(hours=hour)


def _record(source: DataSource, start: datetime, end: datetime, external_id: str) -> EventRecordCreate:
    return EventRecordCreate(
        id=uuid4(),
        external_id=external_id,
        user_id=source.user_id,
        data_source_id=source.id,
        provider=source.provider,
        source=source.source,
        source_name=source.source or "unknown",
        device_model=source.device_model,
        category="sleep",
        type="sleep_session",
        start_datetime=start,
        end_datetime=end,
        zone_offset="+00:00",
        duration_seconds=int((end - start).total_seconds()),
    )


def _detail(record: EventRecordCreate, *, stages: bool = True) -> EventRecordDetailCreate:
    minutes = int((record.end_datetime - record.start_datetime).total_seconds() // 60)
    return EventRecordDetailCreate(
        record_id=record.id,
        sleep_total_duration_minutes=minutes,
        sleep_time_in_bed_minutes=minutes,
        sleep_light_minutes=minutes,
        sleep_deep_minutes=0,
        sleep_rem_minutes=0,
        sleep_awake_minutes=0,
        sleep_efficiency_score=Decimal("100"),
        is_nap=False,
        sleep_stages=[SleepStage(stage="light", start_time=record.start_datetime, end_time=record.end_datetime)]
        if stages
        else None,
    )


def _store(source: DataSource, start: datetime, end: datetime, external_id: str) -> EventRecord:
    record = _record(source, start, end, external_id)
    stored = EventRecordFactory(
        mapping=source,
        external_id=external_id,
        category="sleep",
        type_="sleep_session",
        start_datetime=start,
        end_datetime=end,
        zone_offset="+00:00",
        duration_seconds=int((end - start).total_seconds()),
    )
    SleepDetailsFactory(
        event_record=stored,
        **_detail(record).model_dump(exclude={"record_id"}, mode="json", exclude_none=True),
    )
    return stored


def test_same_id_expansion_frees_exact_target_before_update(db: Session) -> None:
    """RHISE-4915: update A used to collide with B and poison the session."""
    source = DataSourceFactory()
    start = datetime(2026, 9, 29, 21, 31, 14, tzinfo=timezone.utc)
    end = datetime(2026, 9, 30, 5, 38, tzinfo=timezone.utc)
    occupied = _store(source, start, end, "full-night")
    own = _store(source, _dt(2), end, "expanded-sample")
    incoming = _record(source, start, end, own.external_id)

    result = event_record_service.create_or_merge_sleep(db, source.user_id, incoming, _detail(incoming), 120)

    assert result.id == own.id
    assert result.external_id == "expanded-sample"
    assert result.duration_seconds == 29206
    assert db.get(EventRecord, occupied.id) is None
    assert db.query(EventRecord).filter_by(data_source_id=source.id).count() == 1
    assert result.sleep_detail.sleep_total_duration_minutes == 486
    assert db.execute(text("SELECT 1")).scalar_one() == 1


def test_reconciles_all_transitively_adjacent_records(db: Session) -> None:
    source = DataSourceFactory()
    first = _store(source, _dt(0), _dt(1), "first")
    _store(source, _dt(2), _dt(3), "second")
    _store(source, _dt(4), _dt(5), "third")
    incoming = _record(source, _dt(6), _dt(7), "fourth")

    result = event_record_service.create_or_merge_sleep(db, source.user_id, incoming, _detail(incoming), 90)

    assert result.id == first.id
    assert result.external_id == "first"
    assert (result.start_datetime, result.end_datetime) == (_dt(0), _dt(7))
    assert result.sleep_detail.sleep_total_duration_minutes == 240
    assert result.sleep_detail.sleep_time_in_bed_minutes == 240
    assert len(result.sleep_detail.sleep_stages) == 4
    assert db.query(EventRecord).filter_by(data_source_id=source.id).count() == 1


def test_exact_data_source_keeps_other_device_separate(db: Session) -> None:
    user = UserFactory()
    source = DataSourceFactory(user=user, source="Apple Watch", device_model="Watch7,1")
    other = DataSourceFactory(user=user, source="Apple Watch", device_model="Watch6,2")
    existing = _store(other, _dt(0), _dt(8), "other-device")
    incoming = _record(source, _dt(0), _dt(8), "this-device")

    result = event_record_service.create_or_merge_sleep(db, user.id, incoming, _detail(incoming), 120)

    assert result.data_source_id == source.id
    assert db.get(EventRecord, existing.id) is not None
    assert db.query(EventRecord).count() == 2


def test_cloud_same_id_shortening_remains_authoritative(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.OURA)
    own = _store(source, _dt(0), _dt(8), "oura-session")
    incoming = _record(source, _dt(1), _dt(7), own.external_id)

    result = event_record_service.create_or_merge_sleep(db, source.user_id, incoming, _detail(incoming), 120)

    assert result.id == own.id
    assert (result.start_datetime, result.end_datetime) == (_dt(1), _dt(7))
    assert result.sleep_detail.sleep_total_duration_minutes == 360


def test_cloud_moved_night_replaces_internal_score_on_old_wake_date(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.OURA)
    own = _store(source, _dt(-2), _dt(6), "oura-session")
    old_score = HealthScore(
        id=uuid4(),
        user_id=source.user_id,
        data_source_id=source.id,
        provider=ProviderName.INTERNAL,
        category=HealthScoreCategory.SLEEP,
        value=50,
        recorded_at=_dt(6),
        event_record_id=own.id,
    )
    db.add(old_score)
    db.flush()
    incoming = _record(source, _dt(70), _dt(78), own.external_id)

    result = event_record_service.create_or_merge_sleep(db, source.user_id, incoming, _detail(incoming), 120)

    scores = db.query(HealthScore).filter_by(event_record_id=result.id, provider=ProviderName.INTERNAL).all()
    assert len(scores) == 1
    assert scores[0].recorded_at == incoming.end_datetime


def test_apple_replay_of_absorbed_sample_id_does_not_shrink_or_emit(db: Session) -> None:
    source = DataSourceFactory()
    early = _record(source, _dt(0), _dt(4), "early")
    late = _record(source, _dt(4), _dt(8), "late")
    canonical = event_record_service.create_or_merge_sleep(db, source.user_id, early, _detail(early), 120)
    event_record_service.create_or_merge_sleep(db, source.user_id, late, _detail(late), 120)

    with (
        patch("app.services.event_record_service.on_sleep_created") as created,
        patch("app.services.event_record_service.on_sleep_updated") as updated,
    ):
        result = event_record_service.create_or_merge_sleep(db, source.user_id, late, _detail(late), 120)

    assert result.id == canonical.id
    assert result.external_id == "early"
    assert (result.start_datetime, result.end_datetime) == (_dt(0), _dt(8))
    assert result.sleep_detail.sleep_total_duration_minutes == 480
    assert result.sleep_detail.sleep_time_in_bed_minutes == 480
    assert len(result.sleep_detail.sleep_stages) == 2
    created.assert_not_called()
    updated.assert_not_called()


def test_equal_cumulative_stage_less_snapshot_is_idempotent(db: Session) -> None:
    source = DataSourceFactory()
    initial = _record(source, _dt(0), _dt(8), "initial")
    event_record_service.create_or_merge_sleep(db, source.user_id, initial, _detail(initial, stages=False), 120)
    replay = initial.model_copy(update={"id": uuid4(), "external_id": "another-sample"})

    with patch("app.services.event_record_service.on_sleep_updated") as updated:
        result = event_record_service.create_or_merge_sleep(
            db, source.user_id, replay, _detail(replay, stages=False), 120
        )

    assert result.sleep_detail.sleep_total_duration_minutes == 480
    assert result.sleep_detail.sleep_time_in_bed_minutes == 480
    updated.assert_not_called()


def test_contained_apple_replay_preserves_weighted_efficiency(db: Session) -> None:
    source = DataSourceFactory()
    early = _record(source, _dt(0), _dt(4), "early")
    late = _record(source, _dt(4), _dt(8), "late")
    early_detail = _detail(early).model_copy(update={"sleep_efficiency_score": Decimal("85")})
    late_detail = _detail(late).model_copy(update={"sleep_efficiency_score": Decimal("75")})
    event_record_service.create_or_merge_sleep(db, source.user_id, early, early_detail, 120)
    combined = event_record_service.create_or_merge_sleep(db, source.user_id, late, late_detail, 120)
    assert combined.sleep_detail.sleep_efficiency_score == Decimal("80")

    with patch("app.services.event_record_service.on_sleep_updated") as updated:
        replayed = event_record_service.create_or_merge_sleep(db, source.user_id, late, late_detail, 120)

    assert replayed.sleep_detail.sleep_efficiency_score == Decimal("80")
    updated.assert_not_called()


def test_partial_stages_preserve_legacy_unrepresented_sleep_total(db: Session) -> None:
    source = DataSourceFactory()
    full = _record(source, _dt(0), _dt(8), "legacy")
    event_record_service.create_or_merge_sleep(db, source.user_id, full, _detail(full, stages=False), 120)
    tail = _record(source, _dt(6), _dt(8), "tail")

    for _ in range(2):
        partial = event_record_service.create_or_merge_sleep(db, source.user_id, tail, _detail(tail), 120)
        assert partial.sleep_detail.sleep_total_duration_minutes == 480
        assert partial.sleep_detail.sleep_time_in_bed_minutes >= 480

    restored = event_record_service.create_or_merge_sleep(db, source.user_id, full, _detail(full), 120)
    assert restored.sleep_detail.sleep_total_duration_minutes == 480
    assert restored.sleep_detail.sleep_light_minutes == 480
    assert len(restored.sleep_detail.sleep_stages) == 1


def test_out_of_order_fragment_fills_gap_inside_cumulative_window(db: Session) -> None:
    source = DataSourceFactory()
    for start, end, external_id in ((0, 1, "early"), (2, 3, "late"), (1, 2, "gap")):
        incoming = _record(source, _dt(start), _dt(end), external_id)
        result = event_record_service.create_or_merge_sleep(db, source.user_id, incoming, _detail(incoming), 120)

    assert result.sleep_detail.sleep_total_duration_minutes == 180
    assert result.sleep_detail.sleep_time_in_bed_minutes == 180
    assert result.sleep_detail.sleep_efficiency_score == Decimal("100")


def test_stored_duplicate_timelines_are_cleaned_without_duplicate_minutes(db: Session) -> None:
    source = DataSourceFactory()
    stored = _store(source, _dt(0), _dt(8), "old")
    original = stored.sleep_detail.sleep_stages
    stored.sleep_detail.sleep_stages = original * 10
    db.flush()
    incoming = _record(source, _dt(0), _dt(8), "replayed")

    result = event_record_service.create_or_merge_sleep(db, source.user_id, incoming, _detail(incoming), 120)

    assert len(result.sleep_detail.sleep_stages) == 1
    assert result.sleep_detail.sleep_total_duration_minutes == 480


def test_failure_after_duplicate_delete_restores_original_rows(db: Session) -> None:
    source = DataSourceFactory()
    full = _store(source, _dt(0), _dt(8), "full")
    own = _store(source, _dt(2), _dt(8), "own")
    full_id, own_id = full.id, own.id
    db.commit()
    incoming = _record(source, _dt(0), _dt(8), "own")

    with (
        patch.object(
            event_record_service.event_record_detail_repo, "create_and_flush", side_effect=RuntimeError("fail")
        ),
        patch("app.services.event_record_service.on_sleep_updated") as updated,
        pytest.raises(RuntimeError, match="fail"),
    ):
        event_record_service.create_or_merge_sleep(db, source.user_id, incoming, _detail(incoming), 120)

    assert db.get(EventRecord, full_id) is not None
    assert db.get(EventRecord, own_id).start_datetime == _dt(2)
    assert db.get(SleepDetails, full_id) is not None
    assert db.get(SleepDetails, own_id) is not None
    assert db.execute(text("SELECT 1")).scalar_one() == 1
    updated.assert_not_called()


def test_absorbed_provider_score_is_transferred_to_survivor(db: Session) -> None:
    source = DataSourceFactory()
    full = _store(source, _dt(0), _dt(8), "full")
    own = _store(source, _dt(2), _dt(8), "own")
    score = HealthScore(
        id=uuid4(),
        user_id=source.user_id,
        data_source_id=source.id,
        provider=ProviderName.APPLE,
        category=HealthScoreCategory.SLEEP,
        value=87,
        recorded_at=_dt(8),
        event_record_id=full.id,
    )
    db.add(score)
    db.flush()
    incoming = _record(source, _dt(0), _dt(8), "own")

    result = event_record_service.create_or_merge_sleep(db, source.user_id, incoming, _detail(incoming), 120)

    db.refresh(score)
    assert score.event_record_id == result.id == own.id
    assert score.value == 87


def test_failure_during_score_recompute_restores_scores_and_rows(db: Session) -> None:
    source = DataSourceFactory()
    full = _store(source, _dt(0), _dt(8), "full")
    own = _store(source, _dt(2), _dt(8), "own")
    full_id, own_id = full.id, own.id
    score = HealthScore(
        id=uuid4(),
        user_id=source.user_id,
        data_source_id=source.id,
        provider=ProviderName.APPLE,
        category=HealthScoreCategory.SLEEP,
        value=87,
        recorded_at=_dt(8),
        event_record_id=full_id,
    )
    db.add(score)
    db.commit()
    score_id = score.id
    incoming = _record(source, _dt(0), _dt(8), "own")
    with (
        patch.object(event_record_service, "_recompute_sleep_scores", side_effect=RuntimeError("score failure")),
        pytest.raises(RuntimeError, match="score failure"),
    ):
        event_record_service.create_or_merge_sleep(db, source.user_id, incoming, _detail(incoming), 120)

    assert db.get(HealthScore, score_id).event_record_id == full_id
    assert db.get(EventRecord, full_id) is not None
    assert db.get(EventRecord, own_id).start_datetime == _dt(2)


def test_commit_false_queues_until_outer_commit_and_rollback_discards(db: Session) -> None:
    source = DataSourceFactory()
    db.commit()
    incoming = _record(source, _dt(0), _dt(8), "new")
    with patch("app.services.event_record_service.on_sleep_created") as created:
        event_record_service.create_or_merge_sleep(db, source.user_id, incoming, _detail(incoming), 120, commit=False)
        created.assert_not_called()
        # Even an unrelated nested savepoint commit must not dispatch the snapshot.
        with db.begin_nested():
            db.execute(text("SELECT 1"))
        created.assert_not_called()
        db.rollback()
        db.commit()
        created.assert_not_called()
        event_record_service.create_or_merge_sleep(db, source.user_id, incoming, _detail(incoming), 120, commit=False)
        created.assert_not_called()
        db.commit()
        created.assert_called_once()


def test_new_data_source_does_not_commit_inside_reconciliation(db: Session) -> None:
    user = UserFactory()
    db.commit()
    record = EventRecordCreate(
        id=uuid4(),
        external_id="new",
        user_id=user.id,
        source="new-writer",
        provider="apple",
        source_name="new-writer",
        device_model="Watch-new",
        category="sleep",
        type="sleep_session",
        start_datetime=_dt(0),
        end_datetime=_dt(8),
        duration_seconds=28800,
    )
    with patch.object(db, "commit", wraps=db.commit) as commit:
        event_record_service.create_or_merge_sleep(db, user.id, record, _detail(record), 120, commit=False)
        commit.assert_not_called()
        db.commit()
        commit.assert_called_once()


def test_simultaneous_writers_hold_user_lock_through_creation_and_commit(engine: Engine) -> None:
    """Independent connections exercise the real transaction lock, not a Redis mock."""
    user_id = uuid4()
    with Session(engine) as setup:
        setup.add(User(id=user_id, email=f"sleep-{user_id}@example.com"))
        setup.commit()
    first_ready, second_entered, release_first = Event(), Event(), Event()

    def write(first: bool) -> UUID:
        with Session(engine, expire_on_commit=False) as session:
            if not first:
                second_entered.set()
            event_record_service.lock_sleep_user(session, user_id)
            record = EventRecordCreate(
                id=uuid4(),
                external_id="first" if first else "second",
                user_id=user_id,
                provider="apple",
                source="same-writer",
                source_name="same-writer",
                device_model="Watch",
                category="sleep",
                type="sleep_session",
                start_datetime=_dt(0 if first else 4),
                end_datetime=_dt(4 if first else 8),
                duration_seconds=14400,
            )
            result = event_record_service.create_or_merge_sleep(
                session, user_id, record, _detail(record), 120, commit=False
            )
            if first:
                first_ready.set()
                assert release_first.wait(timeout=10)
            session.commit()
            return result.id

    try:
        with (
            patch.object(event_record_service, "_recompute_sleep_scores"),
            ThreadPoolExecutor(max_workers=2) as workers,
        ):
            first = workers.submit(write, True)
            assert first_ready.wait(timeout=10)
            with Session(engine) as probe:
                acquired = probe.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"sleep_merge:{user_id}"},
                ).scalar_one()
                assert acquired is False, "creating the data source must not release the user lock"
            second = workers.submit(write, False)
            assert second_entered.wait(timeout=10)
            assert not second.done()
            release_first.set()
            assert first.result(timeout=10) == second.result(timeout=10)
        with Session(engine) as verify:
            records = verify.query(EventRecord).join(DataSource).filter(DataSource.user_id == user_id).all()
            assert len(records) == 1
            assert records[0].sleep_detail.sleep_total_duration_minutes == 480
    finally:
        release_first.set()
        with Session(engine) as cleanup:
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()
