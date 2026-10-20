"""Scoped, auditable recovery must reuse live reconciliation without inventing sleep evidence."""

import importlib.util
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, User
from app.schemas.enums import ProviderName
from tests.factories import DataSourceFactory, EventRecordFactory, SleepDetailsFactory

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "data_migrations" / "reconcile_apple_sleep.py"
_DAY = date(2026, 9, 30)
_START = datetime(2026, 9, 30, 1, tzinfo=timezone.utc)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reconcile_apple_sleep", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_module()


def _source(**kwargs: Any) -> DataSource:
    return cast(DataSource, DataSourceFactory(**kwargs))


def _sleep(source: DataSource, start: datetime, end: datetime, *, stages: bool = True) -> EventRecord:
    record = cast(
        EventRecord,
        EventRecordFactory(
            data_source=source,
            category="sleep",
            type="sleep_session",
            external_id=str(uuid4()),
            start_datetime=start,
            end_datetime=end,
            duration_seconds=int((end - start).total_seconds()),
            zone_offset="+00:00",
        ),
    )
    SleepDetailsFactory(
        event_record=record,
        sleep_total_duration_minutes=int((end - start).total_seconds() / 60),
        sleep_time_in_bed_minutes=int((end - start).total_seconds() / 60),
        sleep_light_minutes=int((end - start).total_seconds() / 60),
        sleep_deep_minutes=0,
        sleep_rem_minutes=0,
        sleep_awake_minutes=0,
        is_nap=False,
        sleep_stages=[{"stage": "light", "start_time": start.isoformat(), "end_time": end.isoformat()}]
        if stages
        else None,
    )
    return record


@pytest.fixture(autouse=True)
def _isolate_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(script.event_record_service, "_recompute_sleep_scores", MagicMock())


def test_required_account_and_date_scope_defaults_to_dry_run() -> None:
    user_id = uuid4()
    args = script.parse_args(["--user-id", str(user_id), "--since", "2026-09-29", "--until", "2026-09-30"])

    assert args.user_id == user_id
    assert args.apply is False


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--user-id", str(uuid4())],
        ["--user-id", str(uuid4()), "--since", "2026-09-30", "--until", "2026-09-29"],
        ["--user-id", str(uuid4()), "--since", "2026-09-30", "--until", "2026-09-30", "--apply", "--dry-run"],
    ],
)
def test_rejects_unscoped_or_conflicting_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        script.parse_args(argv)


def test_dry_run_preserves_rows_and_reports_before_snapshots(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    first = _sleep(source, _START, _START + timedelta(hours=1))
    second = _sleep(source, _START, _START + timedelta(hours=2))
    lock = MagicMock()
    merge = MagicMock()
    monkeypatch.setattr(script.event_record_service, "lock_sleep_user", lock)
    monkeypatch.setattr(script.event_record_service, "create_or_merge_sleep", merge)

    audit = script.reconcile_apple_sleep(db, user_id=source.user_id, since=_DAY, until=_DAY)

    assert audit[0]["status"] == "would_reconcile"
    assert {row["id"] for row in audit[0]["before"]} == {str(first.id), str(second.id)}
    assert all(row["details"]["sleep_stages"] for row in audit[0]["before"])
    assert db.query(EventRecord).filter(EventRecord.data_source_id == source.id).count() == 2
    lock.assert_not_called()
    merge.assert_not_called()


def test_apply_reconciles_same_source_duplicates_and_second_run_is_unchanged(db: Session) -> None:
    source = _source()
    _sleep(source, _START, _START + timedelta(hours=1))
    _sleep(source, _START, _START + timedelta(hours=2))

    audit = script.reconcile_apple_sleep(db, user_id=source.user_id, since=_DAY, until=_DAY, apply=True)
    second = script.reconcile_apple_sleep(db, user_id=source.user_id, since=_DAY, until=_DAY, apply=True)

    assert audit[0]["status"] == "reconciled"
    assert audit[0]["after"]["details"]["sleep_total_duration_minutes"] == 120
    assert len(audit[0]["before"]) == 2
    assert second[0]["status"] == "unchanged"
    assert second[0]["reason"] == "no_overlapping_duplicates"
    assert db.query(EventRecord).filter(EventRecord.data_source_id == source.id).count() == 1


def test_apply_reconciles_full_gap_connected_component(db: Session) -> None:
    source = _source()
    _sleep(source, _START, _START + timedelta(hours=1))
    _sleep(source, _START + timedelta(minutes=30), _START + timedelta(hours=2))
    _sleep(source, _START + timedelta(hours=3), _START + timedelta(hours=4))

    audit = script.reconcile_apple_sleep(db, user_id=source.user_id, since=_DAY, until=_DAY, apply=True)

    assert len(audit[0]["before"]) == 3
    assert audit[0]["status"] == "reconciled"
    assert db.query(EventRecord).filter(EventRecord.data_source_id == source.id).count() == 1


def test_does_not_combine_sources_or_touch_other_users_and_providers(db: Session) -> None:
    source = _source()
    user = db.get(User, source.user_id)
    assert user is not None
    sibling = _source(user=user, source=source.source, device_model="Another Watch")
    cloud = _source(user=user, provider=ProviderName.OURA)
    other_user = _source()
    _sleep(source, _START, _START + timedelta(hours=1))
    _sleep(source, _START, _START + timedelta(hours=2))
    sibling_record = _sleep(sibling, _START, _START + timedelta(hours=2))
    cloud_record = _sleep(cloud, _START, _START + timedelta(hours=2))
    foreign_record = _sleep(other_user, _START, _START + timedelta(hours=2))

    audit = script.reconcile_apple_sleep(db, user_id=source.user_id, since=_DAY, until=_DAY, apply=True)

    assert sorted(item["status"] for item in audit) == ["reconciled", "unchanged"]
    assert db.get(EventRecord, sibling_record.id) is not None
    assert db.get(EventRecord, cloud_record.id) is not None
    assert db.get(EventRecord, foreign_record.id) is not None


def test_skips_stage_less_duplicates_and_lone_tail(db: Session) -> None:
    source = _source()
    _sleep(source, _START, _START + timedelta(hours=1), stages=False)
    _sleep(source, _START, _START + timedelta(hours=2))

    audit = script.reconcile_apple_sleep(db, user_id=source.user_id, since=_DAY, until=_DAY, apply=True)

    assert audit[0]["status"] == "skipped"
    assert audit[0]["reason"] == "missing_stage_evidence"
    assert db.query(EventRecord).filter(EventRecord.data_source_id == source.id).count() == 2


def test_apply_clamps_a_lone_night_whose_replays_were_summed_into_time_in_bed(db: Session) -> None:
    source = _source()
    night = _sleep(source, _START, _START + timedelta(hours=8))
    detail = night.sleep_detail
    assert detail is not None
    detail.sleep_time_in_bed_minutes = 1320
    detail.sleep_efficiency_score = Decimal("36.36")
    detail.sleep_stages = [*detail.sleep_stages, *detail.sleep_stages]
    db.flush()

    dry = script.reconcile_apple_sleep(db, user_id=source.user_id, since=_DAY, until=_DAY)
    audit = script.reconcile_apple_sleep(db, user_id=source.user_id, since=_DAY, until=_DAY, apply=True)
    second = script.reconcile_apple_sleep(db, user_id=source.user_id, since=_DAY, until=_DAY, apply=True)

    assert dry[0]["status"] == "would_reconcile"
    assert audit[0]["status"] == "reconciled"
    after = audit[0]["after"]
    assert after["id"] == str(night.id)
    assert datetime.fromisoformat(after["start_datetime"]) == _START
    assert after["details"]["sleep_time_in_bed_minutes"] == 480
    assert after["details"]["sleep_total_duration_minutes"] == 480
    assert after["details"]["sleep_efficiency_score"] == Decimal("100.00")
    assert len(after["details"]["sleep_stages"]) == 1
    assert second[0]["status"] == "unchanged"


def test_gap_adjacent_sessions_without_overlap_are_not_repaired(db: Session) -> None:
    source = _source()
    _sleep(source, _START, _START + timedelta(hours=1))
    _sleep(source, _START + timedelta(hours=2), _START + timedelta(hours=3))

    audit = script.reconcile_apple_sleep(db, user_id=source.user_id, since=_DAY, until=_DAY, apply=True)

    assert audit[0]["status"] == "unchanged"
    assert db.query(EventRecord).filter(EventRecord.data_source_id == source.id).count() == 2


def test_skip_component_crossing_requested_local_wake_dates(db: Session) -> None:
    source = _source()
    start = datetime(2026, 9, 29, 22, tzinfo=timezone.utc)
    _sleep(source, start, start + timedelta(hours=1, minutes=30))
    _sleep(source, start + timedelta(hours=1), start + timedelta(hours=2, minutes=30))

    audit = script.reconcile_apple_sleep(db, user_id=source.user_id, since=_DAY, until=_DAY, apply=True)

    assert audit[0]["status"] == "skipped"
    assert audit[0]["reason"] == "component_outside_date_scope"
    assert db.query(EventRecord).filter(EventRecord.data_source_id == source.id).count() == 2


def test_scope_uses_local_wake_date(db: Session) -> None:
    source = _source()
    start = datetime(2026, 9, 29, 22, tzinfo=timezone.utc)
    first = _sleep(source, start, start + timedelta(minutes=30))
    second = _sleep(source, start, start + timedelta(hours=1))
    first.zone_offset = second.zone_offset = "+03:00"
    db.flush()

    audit = script.reconcile_apple_sleep(db, user_id=source.user_id, since=_DAY, until=_DAY)

    assert audit[0]["status"] == "would_reconcile"


def test_skips_external_id_also_owned_by_disconnected_out_of_scope_session(db: Session) -> None:
    source = _source()
    first = _sleep(source, _START, _START + timedelta(hours=1))
    second = _sleep(source, _START, _START + timedelta(hours=2))
    outside = _sleep(source, _START - timedelta(days=1), _START - timedelta(days=1) + timedelta(hours=1))
    second.external_id = outside.external_id = first.external_id
    db.flush()

    audit = script.reconcile_apple_sleep(db, user_id=source.user_id, since=_DAY, until=_DAY, apply=True)

    assert audit[0]["status"] == "skipped"
    assert audit[0]["reason"] == "disconnected_external_id"
    assert audit[0]["related_record_ids"] == [str(outside.id)]
    assert db.query(EventRecord).filter(EventRecord.data_source_id == source.id).count() == 3


def test_cli_rolls_back_and_does_not_print_success_on_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = MagicMock()
    db.__enter__.return_value = db
    monkeypatch.setattr(script, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(script, "reconcile_apple_sleep", MagicMock(side_effect=RuntimeError("original failure")))

    with pytest.raises(RuntimeError, match="original failure"):
        script.main(["--user-id", str(uuid4()), "--since", "2026-09-30", "--until", "2026-09-30", "--apply"])

    db.rollback.assert_called_once()
    db.commit.assert_not_called()
    assert capsys.readouterr().out == ""
