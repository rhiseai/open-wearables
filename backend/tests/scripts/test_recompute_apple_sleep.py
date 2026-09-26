"""Tests for the RHISE-4202 backfill that recomputes impossible Apple sleep nights.

See scripts/data_migrations/recompute_apple_sleep.py.
"""

import importlib.util
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.constants.sleep import SleepStageType
from app.models import EventRecord
from app.schemas.enums import ProviderName
from app.schemas.model_crud.activities import SleepStage
from tests.factories import DataSourceFactory, EventRecordFactory, SleepDetailsFactory, UserFactory

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "data_migrations" / "recompute_apple_sleep.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("recompute_apple_sleep", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_module()
StoredSleep = script.StoredSleep
NightPlan = script.NightPlan
Unrepairable = script.Unrepairable


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


# Oura night of 2026-09-24 as stored after ingestion: stages only, 340 minutes asleep.
OURA_STAGES = [
    SleepStage(stage=SleepStageType(stage), start_time=_dt(a), end_time=_dt(b))
    for stage, a, b in (
        ("light", "2026-09-23T23:15:00Z", "2026-09-24T00:05:00Z"),
        ("deep", "2026-09-24T00:05:00Z", "2026-09-24T01:20:00Z"),
        ("light", "2026-09-24T01:20:00Z", "2026-09-24T02:10:00Z"),
        ("awake", "2026-09-24T02:10:00Z", "2026-09-24T02:25:00Z"),
        ("rem", "2026-09-24T02:25:00Z", "2026-09-24T03:20:00Z"),
        ("light", "2026-09-24T03:20:00Z", "2026-09-24T04:30:00Z"),
        ("rem", "2026-09-24T04:30:00Z", "2026-09-24T05:10:00Z"),
    )
]


def _stored(
    start: str,
    end: str,
    *,
    total: int | None = 340,
    in_bed: int | None = 374,
    stages: list[SleepStage] | None = None,
    record_id: UUID | None = None,
) -> "StoredSleep":
    return StoredSleep(
        record_id=record_id or uuid4(),
        start=_dt(start),
        end=_dt(end),
        zone_offset=None,
        total_sleep_minutes=total,
        time_in_bed_minutes=in_bed,
        deep_minutes=75 if total else None,
        light_minutes=170 if total else None,
        rem_minutes=95 if total else None,
        awake_minutes=15 if total else None,
        stages=list(OURA_STAGES if stages is None else stages),
    )


# Two records of the same night: the summary adds them up to 680 minutes in 6h14.
LEON_NIGHT = [
    _stored("2026-09-23T23:03:00Z", "2026-09-24T05:17:00Z"),
    _stored("2026-09-23T23:15:00Z", "2026-09-24T05:10:00Z", in_bed=355),
]


class TestClusterAndViolations:
    def test_overlapping_records_form_one_night(self) -> None:
        other_night = _stored("2026-09-24T23:00:00Z", "2026-09-25T06:30:00Z")
        clusters = script.cluster_overlapping([other_night, *LEON_NIGHT])

        assert [len(c) for c in clusters] == [2, 1]

    def test_double_counted_night_is_a_violation(self) -> None:
        reasons = script.night_violations(LEON_NIGHT)

        assert "overlapping_records" in reasons
        assert "total_sleep_exceeds_window" in reasons

    def test_plausible_night_is_not(self) -> None:
        assert script.night_violations([LEON_NIGHT[0]]) == []

    def test_single_record_with_total_above_in_bed(self) -> None:
        record = _stored("2026-09-23T23:03:00Z", "2026-09-24T05:17:00Z", total=767)

        assert script.night_violations([record]) == ["total_sleep_exceeds_time_in_bed", "total_sleep_exceeds_window"]


class TestPlanNight:
    def test_duplicates_collapse_into_the_longest_record(self) -> None:
        plan = script.plan_night(LEON_NIGHT, ["overlapping_records"])

        assert isinstance(plan, NightPlan)
        assert plan.keep is LEON_NIGHT[0]
        assert plan.drop == [LEON_NIGHT[1]]
        assert plan.detail["sleep_total_duration_minutes"] == 340
        assert plan.detail["sleep_time_in_bed_minutes"] == 374
        assert plan.detail["sleep_deep_minutes"] == 75
        assert plan.detail["sleep_light_minutes"] == 170
        assert plan.detail["sleep_rem_minutes"] == 95
        assert plan.detail["sleep_awake_minutes"] == 15
        assert plan.start == _dt("2026-09-23T23:03:00Z")
        assert plan.end == _dt("2026-09-24T05:17:00Z")
        assert plan.changed is True

    def test_inflated_total_is_rebuilt_from_stored_stages(self) -> None:
        inflated = _stored("2026-09-23T23:03:00Z", "2026-09-24T05:17:00Z", total=695)
        plan = script.plan_night([inflated], ["total_sleep_exceeds_time_in_bed"])

        assert isinstance(plan, NightPlan)
        assert plan.detail["sleep_total_duration_minutes"] == 340
        assert plan.changed is True

    def test_repaired_night_re_derives_to_itself(self) -> None:
        first = script.plan_night(LEON_NIGHT, ["overlapping_records"])
        assert isinstance(first, NightPlan)
        repaired = StoredSleep(
            record_id=first.keep.record_id,
            start=first.start,
            end=first.end,
            zone_offset=None,
            total_sleep_minutes=first.detail["sleep_total_duration_minutes"],
            time_in_bed_minutes=first.detail["sleep_time_in_bed_minutes"],
            deep_minutes=first.detail["sleep_deep_minutes"],
            light_minutes=first.detail["sleep_light_minutes"],
            rem_minutes=first.detail["sleep_rem_minutes"],
            awake_minutes=first.detail["sleep_awake_minutes"],
            stages=first.detail["sleep_stages"],
        )

        assert script.night_violations([repaired]) == []
        again = script.plan_night([repaired], [])
        assert isinstance(again, NightPlan)
        assert again.changed is False

    def test_stageless_duplicates_keep_the_longest_as_stored(self) -> None:
        night = [
            _stored("2026-09-23T23:00:00Z", "2026-09-24T07:00:00Z", total=450, in_bed=480, stages=[]),
            _stored("2026-09-23T23:30:00Z", "2026-09-24T06:30:00Z", total=400, in_bed=420, stages=[]),
        ]
        plan = script.plan_night(night, ["overlapping_records"])

        assert isinstance(plan, NightPlan)
        assert plan.keep is night[0]
        assert plan.detail["sleep_total_duration_minutes"] == 450
        assert plan.detail["sleep_time_in_bed_minutes"] == 480

    def test_impossible_stageless_record_is_unrepairable(self) -> None:
        record = _stored("2026-09-23T23:03:00Z", "2026-09-24T05:17:00Z", total=767, stages=[])
        plan = script.plan_night([record], ["total_sleep_exceeds_time_in_bed"])

        assert isinstance(plan, Unrepairable)
        assert "total_sleep_exceeds_time_in_bed" in plan.reasons


class TestRecomputeAppleSleep:
    """End to end against the database."""

    def _night(self, mapping: object, start: str, end: str, total: int, in_bed: int) -> EventRecord:
        record = EventRecordFactory(
            mapping=mapping,
            category="sleep",
            type_="sleep_session",
            start_datetime=_dt(start),
            end_datetime=_dt(end),
            duration_seconds=int((_dt(end) - _dt(start)).total_seconds()),
        )
        SleepDetailsFactory(
            event_record=record,
            sleep_total_duration_minutes=total,
            sleep_time_in_bed_minutes=in_bed,
            sleep_deep_minutes=75,
            sleep_light_minutes=170,
            sleep_rem_minutes=95,
            sleep_awake_minutes=15,
            sleep_stages=[s.model_dump(mode="json") for s in OURA_STAGES],
            is_nap=False,
        )
        return record

    def test_collapses_duplicates_emits_once_and_is_idempotent(self, db: Session) -> None:
        user = UserFactory()
        apple = DataSourceFactory(user=user, provider=ProviderName.APPLE, source="Oura")
        kept = self._night(apple, "2026-09-23T23:03:00Z", "2026-09-24T05:17:00Z", 340, 374)
        duplicate = self._night(apple, "2026-09-23T23:15:00Z", "2026-09-24T05:10:00Z", 340, 355)
        oura_cloud = DataSourceFactory(user=user, provider=ProviderName.OURA)
        untouched = self._night(oura_cloud, "2026-09-23T23:15:00Z", "2026-09-24T05:10:00Z", 340, 355)
        self._night(oura_cloud, "2026-09-23T23:20:00Z", "2026-09-24T05:00:00Z", 330, 340)

        with patch.object(script, "on_sleep_updated") as emit:
            dry = script.recompute_apple_sleep(db, date(2026, 9, 20), dry_run=True)
            assert dry["changed"] == 1
            assert db.get(EventRecord, duplicate.id) is not None
            emit.assert_not_called()

            counts = script.recompute_apple_sleep(db, date(2026, 9, 20))

        assert counts["changed"] == 1
        assert db.get(EventRecord, duplicate.id) is None
        assert db.get(EventRecord, untouched.id) is not None
        record = db.get(EventRecord, kept.id)
        assert record is not None
        db.refresh(record)
        assert record.sleep_detail.sleep_total_duration_minutes == 340
        assert record.sleep_detail.sleep_time_in_bed_minutes == 374
        emit.assert_called_once()
        assert emit.call_args.kwargs["record_id"] == kept.id
        assert emit.call_args.kwargs["provider"] == "apple"
        assert emit.call_args.kwargs["sleep_duration_seconds"] == 340 * 60

        with patch.object(script, "on_sleep_updated") as emit_again:
            again = script.recompute_apple_sleep(db, date(2026, 9, 20), only_violations=False)
        assert again["changed"] == 0
        emit_again.assert_not_called()

    def test_since_and_user_filters(self, db: Session) -> None:
        user = UserFactory()
        apple = DataSourceFactory(user=user, provider=ProviderName.APPLE)
        self._night(apple, "2026-09-23T23:03:00Z", "2026-09-24T05:17:00Z", 340, 374)
        self._night(apple, "2026-09-23T23:15:00Z", "2026-09-24T05:10:00Z", 340, 355)

        with patch.object(script, "on_sleep_updated", MagicMock()):
            later = script.recompute_apple_sleep(db, date(2026, 9, 25), dry_run=True)
            other_user = script.recompute_apple_sleep(db, date(2026, 9, 20), user_id=uuid4(), dry_run=True)

        assert later["nights"] == 0
        assert other_user["nights"] == 0
