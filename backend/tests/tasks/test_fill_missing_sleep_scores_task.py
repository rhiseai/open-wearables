"""Score filling must use the committed sleep state after waiting for ingestion."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import delete, event
from sqlalchemy.orm import Session

from app.integrations.celery.tasks import fill_missing_sleep_scores_task as task_module
from app.models import DataSource, EventRecord, HealthScore, SleepDetails, User
from app.schemas.enums import ProviderName
from app.services.event_record_service import event_record_service


def _seed(session_factory: Any, user_id: UUID, windows: list[tuple[int, int]]) -> list[UUID]:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    record_ids = []
    with session_factory() as db:
        db.add(User(id=user_id))
        db.flush()
        source_id = uuid4()
        db.add(DataSource(id=source_id, user_id=user_id, provider=ProviderName.APPLE, source="Watch"))
        db.flush()
        for start_hour, end_hour in windows:
            record_id = uuid4()
            record_ids.append(record_id)
            db.add(
                EventRecord(
                    id=record_id,
                    data_source_id=source_id,
                    category="sleep",
                    type="sleep_session",
                    source_name="Watch",
                    start_datetime=start + timedelta(hours=start_hour),
                    end_datetime=start + timedelta(hours=end_hour),
                    duration_seconds=(end_hour - start_hour) * 3600,
                    zone_offset="+00:00",
                )
            )
            db.flush()
            db.add(
                SleepDetails(
                    record_id=record_id,
                    sleep_total_duration_minutes=(end_hour - start_hour) * 60,
                    sleep_time_in_bed_minutes=(end_hour - start_hour) * 60,
                    sleep_deep_minutes=60,
                    sleep_rem_minutes=60,
                    sleep_light_minutes=max(0, (end_hour - start_hour) * 60 - 120),
                    sleep_awake_minutes=0,
                    is_nap=False,
                )
            )
        db.commit()
    return record_ids


def _clean(session_factory: Any, user_ids: list[UUID]) -> None:
    with session_factory() as db:
        db.execute(delete(User).where(User.id.in_(user_ids)))
        db.commit()


def test_reloads_candidates_after_waiting_for_sleep_lock(engine: Any, session_factory: Any) -> None:
    user_id = uuid4()
    stale_id, survivor_id = _seed(session_factory, user_id, [(3, 7), (0, 7)])
    discovered = Event()

    def discovery_finished(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if "LEFT JOIN health_score hs" in statement and parameters.get("user_id") is None:
            discovered.set()

    event.listen(engine, "after_cursor_execute", discovery_finished)
    try:
        with session_factory() as ingest, patch.object(task_module, "SessionLocal", session_factory):
            event_record_service.lock_sleep_user(ingest, user_id)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(task_module.fill_missing_sleep_scores.run)
                try:
                    assert discovered.wait(timeout=5), "score filler did not discover the pending records"
                    ingest.execute(delete(EventRecord).where(EventRecord.id == stale_id))
                    ingest.commit()
                finally:
                    # Release the owner even if discovery fails; do not strand the worker.
                    ingest.rollback()
                result = pending.result(timeout=10)
        assert result == {"saved": 1, "skipped": 1}
        with session_factory() as db:
            assert [score.event_record_id for score in db.query(HealthScore).filter_by(user_id=user_id)] == [
                survivor_id
            ]
    finally:
        event.remove(engine, "after_cursor_execute", discovery_finished)
        _clean(session_factory, [user_id])


def test_failed_user_does_not_poison_later_score_fills(session_factory: Any) -> None:
    user_ids = sorted([uuid4(), uuid4()])
    _seed(session_factory, user_ids[0], [(0, 7)])
    healthy_id = _seed(session_factory, user_ids[1], [(0, 8)])[0]
    build = task_module.sleep_score_service.build_internal_sleep_scores

    def fail_first(db: Session, user_id: UUID, sessions: list) -> list:
        if user_id == user_ids[0]:
            # A real failed PostgreSQL statement makes this Session unusable until rollback.
            db.execute(delete(EventRecord).where(EventRecord.id == "not-a-uuid"))
        return build(db, user_id, sessions)

    try:
        with (
            patch.object(task_module, "SessionLocal", session_factory),
            patch.object(task_module.sleep_score_service, "build_internal_sleep_scores", side_effect=fail_first),
            patch.object(task_module, "log_and_capture_error") as capture,
        ):
            result = task_module.fill_missing_sleep_scores.run()
        assert result == {"saved": 1, "skipped": 1}
        assert capture.call_count == 1
        with session_factory() as db:
            assert [score.event_record_id for score in db.query(HealthScore)] == [healthy_id]
    finally:
        _clean(session_factory, user_ids)
