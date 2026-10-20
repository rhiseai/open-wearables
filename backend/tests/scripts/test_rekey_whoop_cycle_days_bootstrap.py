"""Bootstrap outcomes must not hide a rolled-back attempt or expose database details.

These synthetic Session fixtures exercise the script without loading application
configuration or connecting to a database. They do not validate the re-key SQL itself.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from psycopg.errors import QueryCanceled
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "data_migrations" / "rekey_whoop_cycle_days.py"


@pytest.fixture
def script() -> ModuleType:
    database = ModuleType("app.database")
    database.SessionLocal = MagicMock()
    spec = importlib.util.spec_from_file_location("rekey_whoop_bootstrap_test", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"app.database": database, spec.name: module}):
        spec.loader.exec_module(module)
    return module


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
