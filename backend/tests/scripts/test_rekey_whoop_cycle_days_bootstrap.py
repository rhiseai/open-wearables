"""Bootstrap outcomes must not hide a rolled-back attempt or expose database details.

The PostgreSQL case exercises the same SQL used at startup. The mocked
Session cases cover transaction boundaries and diagnostics.
"""

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from psycopg.errors import QueryCanceled
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, HealthScore, SeriesTypeDefinition
from app.schemas.enums import HealthScoreCategory, ProviderName, SeriesType, get_series_type_id
from tests.factories import (
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    HealthScoreFactory,
    SleepDetailsFactory,
    UserFactory,
)

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "data_migrations" / "rekey_whoop_cycle_days.py"


@pytest.fixture
def script() -> ModuleType:
    database = ModuleType("app.database")
    database.__dict__["SessionLocal"] = MagicMock()
    spec = importlib.util.spec_from_file_location("rekey_whoop_bootstrap_test", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"app.database": database, spec.name: module}):
        spec.loader.exec_module(module)
    return module


def _daily_point(source: DataSource, metric: SeriesTypeDefinition | None, recorded_at: datetime) -> DataPointSeries:
    assert metric is not None
    return cast(
        DataPointSeries,
        DataPointSeriesFactory(data_source=source, series_type=metric, recorded_at=recorded_at, is_daily_total=True),
    )


def test_rekey_moves_only_uncontested_whoop_cycle_values(db: Session, script: ModuleType) -> None:
    user = UserFactory()
    whoop = cast(DataSource, DataSourceFactory(provider=ProviderName.WHOOP, user=user))
    apple = cast(DataSource, DataSourceFactory(provider=ProviderName.APPLE, user=user))
    steps = db.get(SeriesTypeDefinition, get_series_type_id(SeriesType.steps))
    energy = db.get(SeriesTypeDefinition, get_series_type_id(SeriesType.active_energy))
    start = datetime(2026, 1, 2, 10, 30, tzinfo=UTC)
    target = datetime(2026, 1, 3, tzinfo=UTC)

    # An early nap would anchor the cycle on Jan 2. The next full sleep is
    # exactly 30h later, which puts the midpoint on Jan 3 instead of Jan 2.
    for hours, is_nap in ((2, True), (30, False)):
        event = EventRecordFactory(
            category="sleep", type="sleep", data_source=whoop, start_datetime=start + timedelta(hours=hours)
        )
        SleepDetailsFactory(event_record=event, is_nap=is_nap)

    winner = _daily_point(whoop, steps, start)
    # Without a later onset, both evening starts map to the next day under
    # the 24h fallback. The later start overlaps more of that target day.
    earlier = _daily_point(whoop, steps, start + timedelta(days=2, hours=10))
    later = _daily_point(whoop, steps, earlier.recorded_at + timedelta(hours=1))
    conflict = _daily_point(whoop, energy, start)
    occupied = cast(
        DataPointSeries,
        DataPointSeriesFactory(data_source=whoop, series_type=energy, recorded_at=target, is_daily_total=False),
    )
    unrelated = _daily_point(apple, steps, start)
    strain = HealthScoreFactory(
        data_source=whoop,
        provider=ProviderName.WHOOP,
        category=HealthScoreCategory.STRAIN,
        recorded_at=start,
        components={},
    )
    workout_strain = HealthScoreFactory(
        data_source=whoop,
        provider=ProviderName.WHOOP,
        category=HealthScoreCategory.STRAIN,
        recorded_at=start + timedelta(hours=1),
        components={"percent_recorded": 95},
    )
    expected_series = {row.id: row.recorded_at for row in (winner, earlier, later, conflict, occupied, unrelated)}
    expected_scores = {strain.id: start, workout_strain.id: workout_strain.recorded_at}

    def timestamps(model: type[DataPointSeries] | type[HealthScore]) -> dict[UUID, datetime]:
        return dict(db.execute(select(model.id, model.recorded_at)).tuples().all())

    assert script.rekey_whoop_cycle_days(db, dry_run=True) == 0
    assert timestamps(DataPointSeries) == expected_series
    assert timestamps(HealthScore) == expected_scores

    assert script.rekey_whoop_cycle_days(db, dry_run=False) == 3
    expected_series[winner.id] = target
    expected_series[later.id] = target + timedelta(days=2)
    expected_scores[strain.id] = target
    assert timestamps(DataPointSeries) == expected_series
    assert timestamps(HealthScore) == expected_scores
    assert script.rekey_whoop_cycle_days(db, dry_run=False) == 0


@pytest.mark.parametrize(("minutes_until_sleep", "expected_utc_day"), [(1800, 2), (1801, 1)])
def test_rekey_uses_local_day_and_falls_back_beyond_thirty_hours(
    db: Session, script: ModuleType, minutes_until_sleep: int, expected_utc_day: int
) -> None:
    whoop = DataSourceFactory(provider=ProviderName.WHOOP)
    start = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
    event = EventRecordFactory(
        category="sleep", type="sleep", data_source=whoop, start_datetime=start + timedelta(minutes=minutes_until_sleep)
    )
    SleepDetailsFactory(event_record=event, is_nap=None)
    strain = HealthScoreFactory(
        data_source=whoop,
        provider=ProviderName.WHOOP,
        category=HealthScoreCategory.STRAIN,
        recorded_at=start,
        zone_offset="+01:00",
        components={},
    )

    assert script.rekey_whoop_cycle_days(db, dry_run=False) == 1
    db.refresh(strain)
    assert strain.recorded_at == datetime(2026, 1, expected_utc_day, 23, tzinfo=UTC)


def _session(*results: Any) -> MagicMock:
    db = MagicMock(spec=Session)
    db.__enter__.return_value = db
    db.execute.side_effect = results
    return db


def _count(eligible: int, contested: int = 0) -> SimpleNamespace:
    return SimpleNamespace(one=lambda: (eligible, contested))


def _timeout() -> OperationalError:
    return OperationalError(
        "SELECT synthetic_private_sql",
        {"synthetic_parameter": "private_fixture_value"},
        QueryCanceled("private_fixture_value"),
    )


def _events(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    captured = capsys.readouterr()
    assert captured.err == ""
    return [json.loads(line) for line in captured.out.splitlines()]


def test_timeout_in_second_table_rolls_back_and_a_later_attempt_can_commit(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    failure = _timeout()
    failed = _session(_count(1), SimpleNamespace(rowcount=1), failure)
    retried = _session(_count(1), SimpleNamespace(rowcount=1), _count(1), SimpleNamespace(rowcount=1))
    script.SessionLocal.side_effect = [failed, retried]

    with pytest.raises(OperationalError) as caught:
        script.main(dry_run=False)

    assert caught.value is failure
    failed.rollback.assert_called_once()
    failed.commit.assert_not_called()
    failed.__exit__.assert_called_once()
    assert "UPDATE health_score" in str(failed.execute.call_args_list[1].args[0])
    assert "FROM data_point_series" in str(failed.execute.call_args_list[2].args[0])
    assert all(event.get("outcome") != "committed" for event in _events(capsys))

    script.main(dry_run=False)

    assert script.SessionLocal.call_count == 2
    retried.commit.assert_called_once()
    retried.rollback.assert_not_called()
    assert _events(capsys)[-1]["outcome"] == "committed"


def test_cli_failure_reports_only_exception_classes_and_returns_nonzero(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _session(_timeout())
    script.SessionLocal.return_value = db

    assert script.cli([]) == 1

    events = _events(capsys)
    assert len(events) == 1
    event = events[0]
    assert set(event) == {
        "timestamp",
        "level",
        "logger",
        "message",
        "provider",
        "action",
        "script",
        "exception_class",
        "cause_class",
    }
    assert event["level"] == "error"
    assert event["action"] == "bootstrap_data_migration_failed"
    assert event["script"] == "rekey_whoop_cycle_days"
    assert event["exception_class"] == "OperationalError"
    assert event["cause_class"] == "QueryCanceled"
    assert "private_fixture_value" not in json.dumps(events)
    assert "synthetic_private_sql" not in json.dumps(events)
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_rollback_failure_does_not_replace_original_failure_or_expose_cleanup_details(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    failure = _timeout()
    db = _session(failure)
    db.rollback.side_effect = RuntimeError("private_cleanup_details")
    script.SessionLocal.return_value = db

    with pytest.raises(OperationalError) as caught:
        script.main(dry_run=False)

    assert caught.value is failure
    events = _events(capsys)
    assert events[-1]["action"] == "bootstrap_data_migration_rollback_failed"
    assert events[-1]["exception_class"] == "RuntimeError"
    assert "private_cleanup_details" not in json.dumps(events)
    db.commit.assert_not_called()

    cli_db = _session(_timeout())
    cli_db.rollback.side_effect = RuntimeError("private_cleanup_details")
    script.SessionLocal.return_value = cli_db

    assert script.cli([]) == 1

    assert _events(capsys)[-1]["exception_class"] == "OperationalError"


def test_cleanup_diagnostics_failure_also_preserves_the_original_exception(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = _timeout()
    db = _session(failure)
    db.rollback.side_effect = RuntimeError("private_cleanup_details")
    script.SessionLocal.return_value = db
    monkeypatch.setattr(script, "_emit", MagicMock(side_effect=BrokenPipeError("unavailable output")))

    with pytest.raises(OperationalError) as caught:
        script.main(dry_run=False)

    assert caught.value is failure


@pytest.mark.parametrize("output_error", [BrokenPipeError("closed pipe"), ValueError("closed stdout")])
def test_cli_output_failure_returns_nonzero_without_exposing_original_exception(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_error: OSError | ValueError,
) -> None:
    db = _session(_timeout())
    script.SessionLocal.return_value = db
    monkeypatch.setattr(script, "_emit", MagicMock(side_effect=output_error))

    assert script.cli([]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_committed_outcome_is_emitted_after_commit_and_contested_rows_stay_visible(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _session(_count(2, 1), SimpleNamespace(rowcount=1), _count(0))
    script.SessionLocal.return_value = db
    order = []
    db.commit.side_effect = lambda: order.append("commit")
    emit = script._emit

    def observed_emit(level: str, action: str, message: str, **attributes: Any) -> None:
        if attributes.get("outcome") == "committed":
            order.append("committed")
        emit(level, action, message, **attributes)

    monkeypatch.setattr(script, "_emit", observed_emit)

    script.main(dry_run=False)

    assert order == ["commit", "committed"]
    events = _events(capsys)
    assert events[0]["contested"] == 1
    assert events[-1]["outcome"] == "committed"
    assert events[-1]["rows_moved"] == 1
    assert "all_resolved" not in events[-1]
    assert all("recorded_at" not in event for event in events)


@pytest.mark.parametrize(
    ("dry_run", "results", "outcome"),
    [
        (True, [_count(2, 1), _count(1)], "dry_run"),
        (False, [_count(0), _count(0)], "no_rows_moved"),
        (False, [_count(2, 2), SimpleNamespace(rowcount=0), _count(0)], "no_rows_moved"),
    ],
)
def test_noncommitting_outcomes_do_not_claim_migration_completion(
    script: ModuleType,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    results: list[Any],
    outcome: str,
) -> None:
    db = _session(*results)
    script.SessionLocal.return_value = db

    assert script.cli(["--dry-run"] if dry_run else []) == 0

    db.commit.assert_not_called()
    events = _events(capsys)
    assert events[-1]["outcome"] == outcome
    assert events[-1]["rows_moved"] == 0
    if dry_run:
        assert db.execute.call_count == 2


def test_commit_failure_is_rolled_back_without_a_success_outcome(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _session(_count(1), SimpleNamespace(rowcount=1), _count(0))
    db.commit.side_effect = _timeout()
    script.SessionLocal.return_value = db

    assert script.cli([]) == 1

    db.rollback.assert_called_once()
    events = _events(capsys)
    assert events[-1]["action"] == "bootstrap_data_migration_failed"
    assert all(event.get("outcome") != "committed" for event in events)


def test_programmatic_rekey_leaves_transaction_ownership_with_its_caller(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _session(_count(1), SimpleNamespace(rowcount=1), _count(1), SimpleNamespace(rowcount=1))

    assert script.rekey_whoop_cycle_days(db, dry_run=False) == 2

    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    assert all(event.get("outcome") != "committed" for event in _events(capsys))


@pytest.mark.parametrize("closed_stdout", [False, True])
def test_python_entrypoint_exits_nonzero_without_raw_traceback(closed_stdout: bool) -> None:
    code = """
import runpy
import sys
from types import ModuleType
from psycopg.errors import QueryCanceled
from sqlalchemy.exc import OperationalError

close_stdout_on_failure = sys.argv[2] == 'closed'

def unavailable_session():
    if close_stdout_on_failure:
        sys.stdout.close()
    raise OperationalError('SELECT synthetic_private_sql', {'key': 'private_fixture_value'},
                           QueryCanceled('private_fixture_value'))

database = ModuleType('app.database')
database.SessionLocal = unavailable_session
sys.modules['app.database'] = database
script_path = sys.argv[1]
sys.argv = [script_path]
runpy.run_path(script_path, run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(_SCRIPT_PATH), "closed" if closed_stdout else "open"],
        capture_output=True,
        text=True,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr == ""
    if closed_stdout:
        assert result.stdout == ""
    else:
        event = json.loads(result.stdout)
        assert event["exception_class"] == "OperationalError"
        assert event["cause_class"] == "QueryCanceled"
    assert "private_fixture_value" not in result.stdout
    assert "synthetic_private_sql" not in result.stdout
