"""Exercise the real SDK and score-filler writers on independent PostgreSQL sessions.

The SQL observers only schedule competing transactions or inject a real PostgreSQL
error. Reconciliation, score calculation, DELETE/INSERT, commits and Redis outcome
bookkeeping all run normally. External broker/webhook leaves remain isolated.
"""

import json
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Event, local
from time import monotonic, sleep
from typing import Any, Literal
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from psycopg import Error as PostgresError
from sqlalchemy import delete, event, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.integrations.celery.tasks import fill_missing_sleep_scores_task as filler_module
from app.integrations.celery.tasks import process_sdk_upload_task as sdk_module
from app.models import DataPointSeries, DataSource, EventRecord, HealthScore, SleepDetails, User, UserConnection
from app.schemas.enums import HealthScoreCategory, ProviderName
from app.schemas.sync_status import SyncStage, SyncStatus
from app.services.sdk.sleep_service import load_source_sleep_states
from app.services.sync_status_service import get_recent_events

_WAIT_SECONDS = 10
_FUTURE_SECONDS = 15
_SOURCE = {"name": "Apple Watch", "productType": "Watch7,1", "deviceType": "watch"}
_SLEEP_EXTERNAL_ID = "concurrency-sleep-a"
_SAMPLE_EXTERNAL_ID = "concurrency-heart-rate"
_ROLE = local()
type SessionFactory = sessionmaker[Session]


@dataclass(frozen=True)
class SleepScenario:
    user_id: UUID
    canonical_id: UUID
    other_id: UUID
    stale_score_id: UUID
    start: datetime
    old_end: datetime
    new_end: datetime
    other_end: datetime

    def payload(self) -> str:
        return json.dumps(
            {
                "provider": "apple",
                "sdkVersion": "0.15.0",
                "syncTimestamp": (self.start + timedelta(hours=16)).isoformat(),
                "data": {
                    "records": [
                        {
                            "id": _SAMPLE_EXTERNAL_ID,
                            "type": "HKQuantityTypeIdentifierHeartRate",
                            "value": 60,
                            "unit": "count/min",
                            "startDate": (self.start + timedelta(hours=15)).isoformat(),
                            "endDate": (self.start + timedelta(hours=15)).isoformat(),
                            "zoneOffset": "+00:00",
                            "source": _SOURCE,
                        }
                    ],
                    "sleep": [
                        {
                            "id": _SLEEP_EXTERNAL_ID,
                            "stage": "light",
                            "startDate": self.start.isoformat(),
                            "endDate": self.new_end.isoformat(),
                            "zoneOffset": "+00:00",
                            "source": _SOURCE,
                        }
                    ],
                },
            }
        )


@pytest.fixture
def scenario(session_factory: SessionFactory) -> Generator[SleepScenario, None, None]:
    """Commit setup outside conftest's rollback-only factory Session."""
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    seed = SleepScenario(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        start,
        start + timedelta(hours=5),
        start + timedelta(hours=8),
        start + timedelta(hours=14),
    )
    with session_factory() as db:
        db.add(User(id=seed.user_id))
        db.flush()
        connection_id = uuid4()
        db.add(
            UserConnection(
                id=connection_id,
                user_id=seed.user_id,
                provider="apple",
                status="active",
                updated_at=datetime.now(timezone.utc),
            )
        )
        db.flush()
        for record_id, source_name, model, external_id, begin, end in (
            (seed.canonical_id, "Apple Watch", "Watch7,1", _SLEEP_EXTERNAL_ID, start, seed.old_end),
            (
                seed.other_id,
                "Other Watch",
                "Watch8,1",
                "concurrency-sleep-b",
                start + timedelta(hours=12),
                seed.other_end,
            ),
        ):
            source_id = uuid4()
            db.add(
                DataSource(
                    id=source_id,
                    user_id=seed.user_id,
                    provider=ProviderName.APPLE,
                    source=source_name,
                    device_model=model,
                    user_connection_id=connection_id,
                )
            )
            db.flush()
            minutes = int((end - begin).total_seconds() // 60)
            db.add(
                EventRecord(
                    id=record_id,
                    data_source_id=source_id,
                    external_id=external_id,
                    category="sleep",
                    type="sleep_session",
                    source_name=source_name,
                    start_datetime=begin,
                    end_datetime=end,
                    duration_seconds=minutes * 60,
                    zone_offset="+00:00",
                )
            )
            db.flush()
            db.add(
                SleepDetails(
                    record_id=record_id,
                    sleep_total_duration_minutes=minutes,
                    sleep_time_in_bed_minutes=minutes,
                    sleep_deep_minutes=0,
                    sleep_rem_minutes=0,
                    sleep_light_minutes=minutes,
                    sleep_awake_minutes=0,
                    is_nap=False,
                    sleep_stages=[{"stage": "light", "start_time": begin.isoformat(), "end_time": end.isoformat()}],
                )
            )
            if record_id == seed.canonical_id:
                db.add(
                    HealthScore(
                        id=seed.stale_score_id,
                        user_id=seed.user_id,
                        data_source_id=source_id,
                        provider=ProviderName.INTERNAL,
                        category=HealthScoreCategory.SLEEP,
                        value=1,
                        event_record_id=record_id,
                        recorded_at=end,
                    )
                )
        db.commit()
    try:
        yield seed
    finally:
        with session_factory() as db:
            db.execute(delete(User).where(User.id == seed.user_id))
            db.commit()


@pytest.fixture
def actual_tasks(
    engine: Engine,
    session_factory: SessionFactory,
) -> Generator[None, None, None]:
    """Redirect only Session construction and external transport leaves."""

    def bound_transaction(connection: Connection) -> None:
        connection.exec_driver_sql("SET LOCAL lock_timeout = '8s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '10s'")

    event.listen(engine, "begin", bound_transaction)
    try:
        with (
            patch.object(sdk_module, "SessionLocal", session_factory),
            patch.object(filler_module, "SessionLocal", session_factory),
            patch.object(settings, "persist_live_sync_runs", False),
            patch.object(settings, "outgoing_webhooks_timeseries_events", "all"),
            patch("app.services.outgoing_webhooks.svix.is_enabled", return_value=True),
            # Real Redis records sync outcomes; only the external delivery leaf is
            # replaced, so daemon webhook dispatch cannot outlive the test patches.
            patch("app.services.sync_status_service._maybe_dispatch_outgoing_webhook"),
            patch("app.services.sdk.sleep_service.celery_app"),
        ):
            yield
    finally:
        event.remove(engine, "begin", bound_transaction)


def _sdk(seed: SleepScenario, batch_id: str) -> dict[str, Any]:
    _ROLE.name = "sdk"
    try:
        return sdk_module.process_sdk_upload.run(
            seed.payload(), "application/json", str(seed.user_id), "apple", batch_id
        )
    finally:
        _ROLE.name = ""


def _filler() -> dict[str, int]:
    _ROLE.name = "filler"
    try:
        return filler_module.fill_missing_sleep_scores.run()
    finally:
        _ROLE.name = ""


def _sample_count(db: Session, seed: SleepScenario) -> int:
    return (
        db.query(DataPointSeries)
        .join(DataSource)
        .filter(DataSource.user_id == seed.user_id, DataPointSeries.recorded_at == seed.start + timedelta(hours=15))
        .count()
    )


def _assert_complete(session_factory: SessionFactory, seed: SleepScenario) -> None:
    with session_factory() as db:
        sleeps = db.scalars(
            select(EventRecord)
            .join(DataSource)
            .where(DataSource.user_id == seed.user_id, EventRecord.category == "sleep")
        ).all()
        assert {record.id for record in sleeps} == {seed.canonical_id, seed.other_id}
        canonical = db.get(EventRecord, seed.canonical_id)
        assert canonical is not None
        assert canonical.start_datetime == seed.start
        assert canonical.external_id == _SLEEP_EXTERNAL_ID
        assert canonical.end_datetime == seed.new_end
        assert canonical.duration_seconds == 8 * 3600
        assert canonical.sleep_detail.sleep_total_duration_minutes == 480
        other = db.get(EventRecord, seed.other_id)
        assert other is not None
        assert other.start_datetime == seed.start + timedelta(hours=12)
        assert other.end_datetime == seed.other_end
        assert other.sleep_detail.sleep_total_duration_minutes == 120
        scores = db.scalars(
            select(HealthScore).where(
                HealthScore.user_id == seed.user_id,
                HealthScore.provider == ProviderName.INTERNAL,
                HealthScore.category == HealthScoreCategory.SLEEP,
            )
        ).all()
        assert len(scores) == 2
        assert {score.event_record_id: score.recorded_at for score in scores} == {
            seed.canonical_id: seed.new_end,
            seed.other_id: seed.other_end,
        }
        assert seed.stale_score_id not in {score.id for score in scores}
        assert all(score.value is not None and score.value > 1 and score.components for score in scores)
        assert _sample_count(db, seed) == 1
        sample = db.scalar(
            select(DataPointSeries)
            .join(DataSource)
            .where(
                DataSource.user_id == seed.user_id,
                DataPointSeries.recorded_at == seed.start + timedelta(hours=15),
            )
        )
        assert sample is not None
        assert sample.external_id == _SAMPLE_EXTERNAL_ID
        assert sample.value == 60
        assert sample.data_source_id == canonical.data_source_id
        connection = db.scalar(select(UserConnection).where(UserConnection.user_id == seed.user_id))
        assert connection is not None
        assert connection.last_synced_at is not None


def _assert_outcomes(seed: SleepScenario, batch_id: str, statuses: list[SyncStatus]) -> None:
    terminal = [
        event
        for event in reversed(get_recent_events(seed.user_id))
        if event.run_id == batch_id and event.stage in (SyncStage.COMPLETED, SyncStage.FAILED)
    ]
    assert [event.status for event in terminal] == statuses
    assert all(
        event.stage == (SyncStage.COMPLETED if status == SyncStatus.SUCCESS else SyncStage.FAILED)
        for event, status in zip(terminal, statuses, strict=True)
    )


@dataclass
class CompetingWrites:
    owner: Literal["sdk", "filler"]
    held: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    waiter_entered: Event = field(default_factory=Event)
    owner_pid: int = 0
    waiter_pid: int = 0
    writes: list[tuple[str, str, int]] = field(default_factory=list)

    def before(
        self,
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        role = getattr(_ROLE, "name", "")
        if (
            self.held.is_set()
            and role in {"sdk", "filler"}
            and role != self.owner
            and "pg_advisory_xact_lock" in statement
        ):
            self.waiter_pid = connection.connection.driver_connection.info.backend_pid
            self.waiter_entered.set()

    def after(
        self,
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        role = getattr(_ROLE, "name", "")
        sql = statement.lstrip().lower()
        if role not in {"sdk", "filler"}:
            return
        kind = (
            "delete"
            if sql.startswith("delete from health_score")
            else "insert"
            if sql.startswith("insert into health_score")
            else ""
        )
        if not kind:
            return
        self.writes.append((role, kind, cursor.rowcount))
        boundary = kind == ("delete" if self.owner == "sdk" else "insert")
        if role == self.owner and boundary and cursor.rowcount > 0 and not self.held.is_set():
            self.owner_pid = connection.connection.driver_connection.info.backend_pid
            self.held.set()
            assert self.release.wait(_WAIT_SECONDS), "competing writer barrier was not released"

    def assert_blocked(self, session_factory: SessionFactory) -> None:
        assert self.waiter_entered.wait(_WAIT_SECONDS), "competing writer did not reach the sleep advisory lock"
        assert self.owner_pid != self.waiter_pid
        deadline = monotonic() + 3
        with session_factory() as db:
            assert db.scalar(text("SELECT pg_backend_pid()")) not in {self.owner_pid, self.waiter_pid}
            while monotonic() < deadline:
                blocked = db.scalar(
                    text("SELECT :owner = ANY(pg_blocking_pids(:waiter))"),
                    {"owner": self.owner_pid, "waiter": self.waiter_pid},
                )
                if blocked:
                    return
                sleep(0.01)
        pytest.fail("PostgreSQL did not confirm the expected competing writer as the blocker")


@pytest.mark.parametrize("owner", ["sdk", "filler"])
def test_actual_sdk_and_score_filler_serialize_score_rebuild(
    engine: Engine,
    session_factory: SessionFactory,
    scenario: SleepScenario,
    actual_tasks: None,
    owner: Literal["sdk", "filler"],
) -> None:
    contention = CompetingWrites(owner)
    batch_id = str(uuid4())
    event.listen(engine, "before_cursor_execute", contention.before)
    event.listen(engine, "after_cursor_execute", contention.after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                first = pool.submit(_sdk, scenario, batch_id) if owner == "sdk" else pool.submit(_filler)
                assert contention.held.wait(_WAIT_SECONDS), "first writer did not execute its real score write"
                second = pool.submit(_filler) if owner == "sdk" else pool.submit(_sdk, scenario, batch_id)
                contention.assert_blocked(session_factory)
                # The SDK commits samples before attempting the contested sleep transaction.
                with session_factory() as db:
                    assert _sample_count(db, scenario) == 1
            finally:
                contention.release.set()
            first_result = first.result(timeout=_FUTURE_SECONDS)
            second_result = second.result(timeout=_FUTURE_SECONDS)
        sdk_result, filler_result = (first_result, second_result) if owner == "sdk" else (second_result, first_result)
        assert sdk_result["status_code"] == 200, sdk_result
        assert sdk_result["sleep_saved"] == 1
        assert filler_result == ({"saved": 0, "skipped": 1} if owner == "sdk" else {"saved": 1, "skipped": 0})
        assert any(
            role == "sdk" and kind == "delete" and count == (1 if owner == "sdk" else 2)
            for role, kind, count in contention.writes
        )
        assert any(role == "sdk" and kind == "insert" and count > 0 for role, kind, count in contention.writes)
        if owner == "filler":
            assert any(role == "filler" and kind == "insert" and count == 1 for role, kind, count in contention.writes)
        _assert_complete(session_factory, scenario)
        _assert_outcomes(scenario, batch_id, [SyncStatus.SUCCESS])
    finally:
        contention.release.set()
        event.remove(engine, "before_cursor_execute", contention.before)
        event.remove(engine, "after_cursor_execute", contention.after)


@pytest.mark.parametrize("sqlstate", ["40P01", "23514"])
def test_sleep_sql_failure_preserves_earlier_commit_and_truthful_replay(
    engine: Engine,
    session_factory: SessionFactory,
    scenario: SleepScenario,
    actual_tasks: None,
    sqlstate: str,
    mock_webhook_dispatch: MagicMock,
) -> None:
    """40P01 is an injected SQLSTATE, not a reproduction of a cyclic deadlock.

    The permanent constraint fault must fail again while it is present. Recovery
    is an explicit replay after removal, never an asserted automatic DB retry.
    """
    batch_id = str(uuid4())
    faults: list[str] = []

    def fail_score_write(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if getattr(_ROLE, "name", "") == "sdk" and statement.lstrip().lower().startswith("delete from health_score"):
            assert cursor.rowcount > 0
            # The failing SQL executes in the genuine score transaction. PostgreSQL
            # aborts it, so the application's real rollback path must recover it.
            try:
                cursor.execute(
                    f"DO $$ BEGIN RAISE EXCEPTION 'synthetic score boundary fault' USING ERRCODE = '{sqlstate}'; END $$"
                )
            except PostgresError as exc:
                faults.append(exc.sqlstate or "missing_sqlstate")
                raise

    event.listen(engine, "after_cursor_execute", fail_score_write)
    expected: list[SyncStatus] = []
    try:
        for attempt in range(2 if sqlstate == "23514" else 1):
            result = _sdk(scenario, batch_id)
            assert result["status_code"] == 400
            assert "synthetic score boundary fault" in result["response"]
            assert faults == [sqlstate] * (attempt + 1), "a failed import must not silently retry its DB transaction"
            expected.append(SyncStatus.FAILED)
            _assert_outcomes(scenario, batch_id, expected)
            with session_factory() as db:
                assert _sample_count(db, scenario) == 1
                canonical = db.get(EventRecord, scenario.canonical_id)
                assert canonical is not None
                assert canonical.end_datetime == scenario.old_end
                assert canonical.duration_seconds == 5 * 3600
                assert canonical.sleep_detail.sleep_total_duration_minutes == 300
                scores = db.scalars(select(HealthScore).where(HealthScore.user_id == scenario.user_id)).all()
                assert [score.id for score in scores] == [scenario.stale_score_id]
                assert scores[0].value == 1
                connection = db.scalar(select(UserConnection).where(UserConnection.user_id == scenario.user_id))
                assert connection is not None
                assert connection.last_synced_at is None
            assert load_source_sleep_states(str(scenario.user_id)) == {}
            assert not any(
                call.kwargs["args"][0] in {"sleep.created", "sleep.updated", "heart_rate.created"}
                for call in mock_webhook_dispatch.send_task.call_args_list
            )
    finally:
        event.remove(engine, "after_cursor_execute", fail_score_write)
    replay = _sdk(scenario, batch_id)
    assert replay["status_code"] == 200, replay
    assert len(faults) == len(expected)
    _assert_complete(session_factory, scenario)
    _assert_outcomes(scenario, batch_id, [*expected, SyncStatus.SUCCESS])
    assert any(call.kwargs["args"][0] == "sleep.updated" for call in mock_webhook_dispatch.send_task.call_args_list)
