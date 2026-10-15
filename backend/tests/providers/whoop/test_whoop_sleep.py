"""Tests for Whoop sleep ingestion: the night's local offset and the date it is summarised under."""

from datetime import date, datetime
from logging import getLogger
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord
from app.services.providers.whoop.data_247 import Whoop247Data
from app.services.providers.whoop.strategy import WhoopStrategy
from app.services.summaries_service import SummariesService
from tests.factories import UserFactory

FIRST_NIGHT_ID = "4f0c8a52-1d3e-4b6a-9c7f-2e8d5a1b3c90"
SECOND_NIGHT_ID = "7a2e9b14-6c5d-4f83-a1b0-9d3c8e7f6a25"
MINUTE_MS = 60_000


def _whoop_sleep_payload(
    sleep_id: str,
    start: str,
    end: str,
    *,
    in_bed_minutes: int,
    light_minutes: int,
    deep_minutes: int,
    rem_minutes: int,
    timezone_offset: str = "+07:00",
) -> dict[str, Any]:
    """Whoop ``GET /v2/activity/sleep/{id}`` response for one scored night."""
    asleep_minutes = light_minutes + deep_minutes + rem_minutes
    return {
        "id": sleep_id,
        "cycle_id": 93845,
        "v1_id": 93845,
        "user_id": 10129,
        "created_at": end,
        "updated_at": end,
        "start": start,
        "end": end,
        "timezone_offset": timezone_offset,
        "nap": False,
        "score_state": "SCORED",
        "score": {
            "stage_summary": {
                "total_in_bed_time_milli": in_bed_minutes * MINUTE_MS,
                "total_awake_time_milli": (in_bed_minutes - asleep_minutes) * MINUTE_MS,
                "total_no_data_time_milli": 0,
                "total_light_sleep_time_milli": light_minutes * MINUTE_MS,
                "total_slow_wave_sleep_time_milli": deep_minutes * MINUTE_MS,
                "total_rem_sleep_time_milli": rem_minutes * MINUTE_MS,
                "sleep_cycle_count": 4,
                "disturbance_count": 9,
            },
            "respiratory_rate": 15.2,
            "sleep_performance_percentage": 81,
            "sleep_consistency_percentage": 70,
            "sleep_efficiency_percentage": 94.0,
        },
    }


# A user at UTC+7 who woke at 09:49 local on 14 Oct and at 06:02 local on 15 Oct:
# both wake-ups fall on 14 Oct in UTC.
FIRST_NIGHT = _whoop_sleep_payload(
    FIRST_NIGHT_ID,
    "2026-10-13T18:46:50.000Z",
    "2026-10-14T02:49:41.000Z",
    in_bed_minutes=483,
    light_minutes=250,
    deep_minutes=100,
    rem_minutes=104,
)
SECOND_NIGHT = _whoop_sleep_payload(
    SECOND_NIGHT_ID,
    "2026-10-14T18:20:00.000Z",
    "2026-10-14T23:02:00.000Z",
    in_bed_minutes=282,
    light_minutes=180,
    deep_minutes=50,
    rem_minutes=48,
)


def _load(data_247: Whoop247Data, db: Session, user_id: UUID, payload: dict[str, Any]) -> None:
    with patch.object(data_247, "get_sleep_record", return_value=payload):
        saved, _ = data_247.load_single_sleep(db, user_id, payload["id"])
    assert saved == 1


def _sleep_records(db: Session, user_id: UUID) -> list[EventRecord]:
    return (
        db.query(EventRecord)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .filter(DataSource.user_id == user_id, EventRecord.category == "sleep")
        .order_by(EventRecord.start_datetime)
        .all()
    )


@pytest.fixture
def data_247() -> Whoop247Data:
    return WhoopStrategy().data_247


class TestWhoopSleepZoneOffset:
    """Whoop names a sleep's UTC offset ``timezone_offset``; the stored night must keep it."""

    def test_normalize_sleep_reads_timezone_offset(self, data_247: Whoop247Data) -> None:
        normalized, _ = data_247.normalize_sleep(FIRST_NIGHT, UserFactory.build().id)

        assert normalized["zone_offset"] == "+07:00"

    def test_saved_night_keeps_timezone_offset(self, db: Session, data_247: Whoop247Data) -> None:
        user = UserFactory()

        _load(data_247, db, user.id, FIRST_NIGHT)

        [record] = _sleep_records(db, user.id)
        assert record.zone_offset == "+07:00"

    def test_pulled_night_keeps_timezone_offset(self, db: Session, data_247: Whoop247Data) -> None:
        """The periodic pull (``/v2/activity/sleep`` pages) stores the offset like the webhook path."""
        user = UserFactory()

        with patch.object(data_247, "_fetch_paginated", return_value=([FIRST_NIGHT], False)):
            saved, _ = data_247.load_and_save_sleep(
                db,
                user.id,
                datetime.fromisoformat("2026-10-13T00:00:00+00:00"),
                datetime.fromisoformat("2026-10-15T00:00:00+00:00"),
            )

        assert saved == 1
        [record] = _sleep_records(db, user.id)
        assert record.zone_offset == "+07:00"

    def test_resync_fills_offset_of_night_stored_without_it(self, db: Session, data_247: Whoop247Data) -> None:
        """A night stored before the fix has no offset; the next fetch of the same sleep adds it."""
        user = UserFactory()
        stored_without_offset = {key: value for key, value in FIRST_NIGHT.items() if key != "timezone_offset"}
        _load(data_247, db, user.id, stored_without_offset)
        assert _sleep_records(db, user.id)[0].zone_offset is None

        _load(data_247, db, user.id, FIRST_NIGHT)

        [record] = _sleep_records(db, user.id)
        assert record.external_id == FIRST_NIGHT_ID
        assert record.zone_offset == "+07:00"


class TestWhoopNightsSummarisedByLocalWakeUpDate:
    """Two nights whose wake-ups share a UTC date are still two days for a user east of UTC."""

    def test_consecutive_nights_get_their_own_dates(self, db: Session, data_247: Whoop247Data) -> None:
        user = UserFactory()
        _load(data_247, db, user.id, FIRST_NIGHT)
        _load(data_247, db, user.id, SECOND_NIGHT)

        result = SummariesService(log=getLogger(__name__)).get_sleep_summaries(
            db,
            user.id,
            datetime.fromisoformat("2026-10-13T00:00:00+00:00"),
            datetime.fromisoformat("2026-10-16T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        nights = {summary.date: summary for summary in result.data}
        assert sorted(nights) == [date(2026, 10, 14), date(2026, 10, 15)]

        first, second = nights[date(2026, 10, 14)], nights[date(2026, 10, 15)]
        assert (first.duration_minutes, first.time_in_bed_minutes) == (454, 483)
        assert first.start_time == datetime.fromisoformat("2026-10-13T18:46:50+00:00")
        assert first.end_time == datetime.fromisoformat("2026-10-14T02:49:41+00:00")
        assert (second.duration_minutes, second.time_in_bed_minutes) == (278, 282)
        assert second.start_time == datetime.fromisoformat("2026-10-14T18:20:00+00:00")
        assert second.end_time == datetime.fromisoformat("2026-10-14T23:02:00+00:00")
        assert second.zone_offset == "+07:00"
