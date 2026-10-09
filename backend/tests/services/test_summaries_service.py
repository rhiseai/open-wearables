"""Tests for SummariesService."""

from datetime import date, datetime, timedelta, timezone
from logging import getLogger
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.schemas.enums import ProviderName
from app.services.priority_service import priority_service
from app.services.summaries_service import SummariesService
from tests.factories import (
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    PersonalRecordFactory,
    SeriesTypeDefinitionFactory,
    SleepDetailsFactory,
    UserFactory,
)


@pytest.fixture
def service() -> SummariesService:
    return SummariesService(log=getLogger(__name__))


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# ---------------------------------------------------------------------------
# _filter_by_priority
# ---------------------------------------------------------------------------


class TestFilterByPriority:
    def test_returns_empty_for_empty_input(self, db: Session, service: SummariesService) -> None:
        result = service._filter_by_priority(db, uuid4(), [])
        assert result == []

    def test_single_entry_passes_through(self, db: Session, service: SummariesService) -> None:
        entry = {"activity_date": date(2026, 1, 1), "source": "garmin", "device_model": None}
        result = service._filter_by_priority(db, uuid4(), [entry])
        assert result == [entry]

    def test_picks_one_entry_per_date(self, db: Session, service: SummariesService) -> None:
        entries = [
            {"activity_date": date(2026, 1, 1), "source": "garmin", "device_model": None},
            {"activity_date": date(2026, 1, 1), "source": "apple_health_sdk", "device_model": None},
            {"activity_date": date(2026, 1, 2), "source": "garmin", "device_model": None},
        ]
        result = service._filter_by_priority(db, uuid4(), entries)
        assert len(result) == 2
        dates = {r["activity_date"] for r in result}
        assert dates == {date(2026, 1, 1), date(2026, 1, 2)}

    def test_uses_sleep_date_key(self, db: Session, service: SummariesService) -> None:
        entries = [
            {"sleep_date": date(2026, 1, 1), "source": "garmin", "device_model": None},
            {"sleep_date": date(2026, 1, 1), "source": "oura", "device_model": None},
        ]
        result = service._filter_by_priority(db, uuid4(), entries, date_key="sleep_date")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _get_user_max_hr
# ---------------------------------------------------------------------------


class TestGetUserMaxHr:
    def test_falls_back_to_default_when_no_user(self, db: Session, service: SummariesService) -> None:
        result = service._get_user_max_hr(db, uuid4(), datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert result == 190

    def test_falls_back_to_default_when_no_birth_date(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        PersonalRecordFactory(user=user, birth_date=None)
        result = service._get_user_max_hr(db, user.id, datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert result == 190

    def test_calculates_from_birth_date(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        PersonalRecordFactory(user=user, birth_date=date(1990, 6, 1))
        ref = datetime(2026, 6, 26, tzinfo=timezone.utc)  # age = 36
        result = service._get_user_max_hr(db, user.id, ref)
        assert result == 220 - 36

    def test_adjusts_when_birthday_not_yet_this_year(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        PersonalRecordFactory(user=user, birth_date=date(1990, 12, 31))
        ref = datetime(2026, 6, 26, tzinfo=timezone.utc)  # birthday hasn't happened yet -> age 35
        result = service._get_user_max_hr(db, user.id, ref)
        assert result == 220 - 35


# ---------------------------------------------------------------------------
# get_sleep_summaries
# ---------------------------------------------------------------------------


class TestGetSleepSummaries:
    def _make_sleep_record(self, user: Any, start: str, end: str) -> Any:
        ds = DataSourceFactory(user=user, provider=ProviderName.GARMIN, source="garmin")
        return EventRecordFactory(
            data_source=ds,
            category="sleep",
            type="sleep",
            start_datetime=_dt(start),
            end_datetime=_dt(end),
            duration_seconds=int((_dt(end) - _dt(start)).total_seconds()),
            zone_offset="+00:00",
        )

    def test_returns_empty_when_no_data(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-07T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert result.data == []
        assert result.pagination.has_more is False

    def test_returns_sleep_record(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        record = self._make_sleep_record(user, "2026-01-01T23:00:00+00:00", "2026-01-02T07:00:00+00:00")
        SleepDetailsFactory(event_record=record)

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert len(result.data) == 1
        summary = result.data[0]
        assert summary.duration_minutes == 8 * 60
        assert summary.source.provider == "garmin"

    def test_does_not_return_other_users_data(self, db: Session, service: SummariesService) -> None:
        user_a = UserFactory()
        user_b = UserFactory()
        self._make_sleep_record(user_b, "2026-01-01T23:00:00+00:00", "2026-01-02T07:00:00+00:00")

        result = service.get_sleep_summaries(
            db,
            user_a.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert result.data == []

    def test_physio_averages_within_sleep_window(self, db: Session, service: SummariesService) -> None:
        """avg_heart_rate_bpm/avg_hrv_sdnn_ms are computed from data_point_series
        samples within [min_start_time, max_end_time), independently per series
        type, and samples outside the window are excluded."""
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.GARMIN, source="garmin")
        record = EventRecordFactory(
            data_source=ds,
            category="sleep",
            type="sleep",
            start_datetime=_dt("2026-01-01T23:00:00+00:00"),
            end_datetime=_dt("2026-01-02T07:00:00+00:00"),
            duration_seconds=8 * 3600,
            zone_offset="+00:00",
        )
        SleepDetailsFactory(event_record=record)

        hr_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        hrv_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate_variability_sdnn()

        for i, val in enumerate([50, 60, 70]):
            DataPointSeriesFactory(
                data_source=ds,
                series_type=hr_type,
                value=val,
                recorded_at=_dt(f"2026-01-02T0{i}:00:00+00:00"),
            )
        DataPointSeriesFactory(
            data_source=ds,
            series_type=hrv_type,
            value=45,
            recorded_at=_dt("2026-01-02T02:00:00+00:00"),
        )
        # Outside the sleep window - must not affect the average
        DataPointSeriesFactory(
            data_source=ds,
            series_type=hr_type,
            value=200,
            recorded_at=_dt("2026-01-02T12:00:00+00:00"),
        )

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert len(result.data) == 1
        summary = result.data[0]
        assert summary.avg_heart_rate_bpm == 60
        assert summary.avg_hrv_sdnn_ms == 45

    def test_physio_averages_none_without_physio_data(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        record = self._make_sleep_record(user, "2026-01-01T23:00:00+00:00", "2026-01-02T07:00:00+00:00")
        SleepDetailsFactory(event_record=record)

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert len(result.data) == 1
        summary = result.data[0]
        assert summary.avg_heart_rate_bpm is None
        assert summary.avg_hrv_sdnn_ms is None
        assert summary.avg_hrv_rmssd_ms is None
        assert summary.avg_respiratory_rate is None
        assert summary.avg_spo2_percent is None

    def test_has_more_flag_and_pagination(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.GARMIN, source="garmin")
        for day in range(1, 6):
            EventRecordFactory(
                data_source=ds,
                category="sleep",
                type="sleep",
                start_datetime=_dt(f"2026-01-{day:02d}T23:00:00+00:00"),
                end_datetime=_dt(f"2026-01-{day + 1:02d}T07:00:00+00:00"),
                duration_seconds=8 * 3600,
                zone_offset="+00:00",
            )

        result = service.get_sleep_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-10T00:00:00+00:00"),
            cursor=None,
            limit=3,
        )
        assert len(result.data) == 3
        assert result.pagination.has_more is True
        assert result.pagination.next_cursor is not None


# ---------------------------------------------------------------------------
# get_recovery_summaries
# ---------------------------------------------------------------------------


class TestGetRecoverySummaries:
    def test_rmssd_input_is_exposed_as_rmssd_not_sdnn(
        self, service: SummariesService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = {
            "recovery_date": date(2026, 1, 2),
            "provider": "whoop",
            "source": "whoop",
            "device_model": "WHOOP",
            "device_type": "band",
            "record_id": uuid4(),
            "recorded_at": _dt("2026-01-02T00:00:00+00:00"),
            "recovery_score": 74,
            "resting_heart_rate": 51,
            "hrv_rmssd_milli": 63.2,
            "spo2_percentage": 98.4,
        }
        monkeypatch.setattr(service.health_score_repo, "get_recovery_summaries", lambda *_: [row])
        monkeypatch.setattr(service, "_filter_by_priority", lambda *_args, **_kwargs: [row])

        result = service.get_recovery_summaries(
            db_session=None,
            user_id=uuid4(),
            start_date=_dt("2026-01-01T00:00:00+00:00"),
            end_date=_dt("2026-01-03T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        summary = result.data[0]
        assert summary.avg_hrv_sdnn_ms is None
        assert summary.avg_hrv_rmssd_ms == 63.2


# ---------------------------------------------------------------------------
# get_activity_summaries
# ---------------------------------------------------------------------------


class TestGetActivitySummaries:
    def test_returns_empty_when_no_data(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        result = service.get_activity_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-07T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert result.data == []

    def test_aggregates_steps_for_user(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.APPLE, source="apple_health_sdk")
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()

        for i in range(3):
            DataPointSeriesFactory(
                data_source=ds,
                series_type=steps_type,
                value=1000,
                recorded_at=_dt(f"2026-01-01T10:0{i}:00+00:00"),
            )

        result = service.get_activity_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert len(result.data) == 1
        assert result.data[0].steps == 3000

    def test_fills_watch_gaps_from_phone_hours(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        watch = DataSourceFactory(
            user=user,
            provider=ProviderName.APPLE,
            source="watch",
            device_model="Watch7,1",
            device_type="watch",
        )
        phone = DataSourceFactory(
            user=user,
            provider=ProviderName.APPLE,
            source="phone",
            device_model="iPhone15,2",
            device_type="phone",
        )
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
        DataPointSeriesFactory(
            data_source=watch,
            series_type=steps_type,
            value=500,
            recorded_at=_dt("2026-01-01T08:00:00+00:00"),
        )
        DataPointSeriesFactory(
            data_source=phone,
            series_type=steps_type,
            value=6000,
            recorded_at=_dt("2026-01-01T15:00:00+00:00"),
        )

        result = service.get_activity_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        assert result.data[0].source.device == "Watch7,1"
        assert result.data[0].steps == 6500

    def test_does_not_double_count_devices_in_the_same_hour(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        watch = DataSourceFactory(
            user=user,
            provider=ProviderName.APPLE,
            source="watch",
            device_model="Watch7,1",
            device_type="watch",
        )
        phone = DataSourceFactory(
            user=user,
            provider=ProviderName.APPLE,
            source="phone",
            device_model="iPhone15,2",
            device_type="phone",
        )
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
        DataPointSeriesFactory(
            data_source=watch,
            series_type=steps_type,
            value=500,
            recorded_at=_dt("2026-01-01T08:00:00+00:00"),
        )
        DataPointSeriesFactory(
            data_source=phone,
            series_type=steps_type,
            value=6000,
            recorded_at=_dt("2026-01-01T08:30:00+00:00"),
        )

        result = service.get_activity_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        assert result.data[0].steps == 6000

    def test_keeps_a_larger_provider_daily_total(self, db: Session, service: SummariesService) -> None:
        user = UserFactory()
        watch = DataSourceFactory(
            user=user,
            provider=ProviderName.GARMIN,
            source="garmin_daily",
            device_model="Fenix 8",
            device_type="watch",
        )
        phone = DataSourceFactory(
            user=user,
            provider=ProviderName.GARMIN,
            source="garmin_samples",
            device_model="Android phone",
            device_type="phone",
        )
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
        DataPointSeriesFactory(
            data_source=watch,
            series_type=steps_type,
            value=10000,
            is_daily_total=True,
            recorded_at=_dt("2026-01-01T00:00:00+00:00"),
        )
        DataPointSeriesFactory(
            data_source=phone,
            series_type=steps_type,
            value=6000,
            recorded_at=_dt("2026-01-01T15:00:00+00:00"),
        )

        result = service.get_activity_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        assert result.data[0].steps == 10000

    def _ring_and_watch(self, db: Session, user: Any, top: ProviderName = ProviderName.OURA) -> tuple[Any, Any]:
        """A top-ranked ring or band without a device model, and an Apple Watch ranked below it."""
        ring = DataSourceFactory(user=user, provider=top, source=top.value, device_model=None, device_type="ring")
        watch = DataSourceFactory(
            user=user,
            provider=ProviderName.APPLE,
            source="watch",
            device_model="Watch7,1",
            device_type="watch",
        )
        priority_service.update_provider_priority(db, top, 1)
        priority_service.update_provider_priority(db, ProviderName.APPLE, 2)
        return ring, watch

    def test_steps_come_from_the_next_provider_when_the_top_one_has_only_heart_rate(
        self, db: Session, service: SummariesService
    ) -> None:
        """A ring that synced heart rate but no daily activity record is not a day of zero steps."""
        user = UserFactory()
        ring, watch = self._ring_and_watch(db, user)
        hr_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
        for minute in range(2):
            DataPointSeriesFactory(
                data_source=ring,
                series_type=hr_type,
                value=130,
                recorded_at=_dt("2026-01-01T09:00:00+00:00") + timedelta(minutes=minute),
            )
        for minute in range(3):
            DataPointSeriesFactory(
                data_source=watch,
                series_type=steps_type,
                value=1200,
                recorded_at=_dt("2026-01-01T10:00:00+00:00") + timedelta(minutes=minute),
            )

        result = service.get_activity_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        day = result.data[0]
        assert day.steps == 3600
        assert (day.source.provider, day.source.device) == ("apple", "Watch7,1")
        assert day.heart_rate is not None
        assert day.heart_rate.avg_bpm == 130
        # The derived minutes follow the samples they are derived from: the step
        # threshold runs over the watch, the heart-rate zones over the ring.
        assert day.active_minutes == 3
        assert day.intensity_minutes is not None
        assert day.intensity_minutes.moderate == 2

    def test_an_energy_only_provider_does_not_hide_steps(self, db: Session, service: SummariesService) -> None:
        """Whoop stores a daily active energy and no steps; the steps still come from the next provider."""
        user = UserFactory()
        band, watch = self._ring_and_watch(db, user, top=ProviderName.WHOOP)
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
        energy_type = SeriesTypeDefinitionFactory.get_or_create_energy()
        basal_type = SeriesTypeDefinitionFactory.get_or_create_basal_energy()
        DataPointSeriesFactory(
            data_source=band,
            series_type=energy_type,
            value=2500,
            is_daily_total=True,
            recorded_at=_dt("2026-01-01T00:00:00+00:00"),
        )
        at = _dt("2026-01-01T10:00:00+00:00")
        DataPointSeriesFactory(data_source=watch, series_type=steps_type, value=3600, recorded_at=at)
        DataPointSeriesFactory(data_source=watch, series_type=energy_type, value=400, recorded_at=at)
        DataPointSeriesFactory(data_source=watch, series_type=basal_type, value=1700, recorded_at=at)

        result = service.get_activity_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        day = result.data[0]
        assert day.steps == 3600
        assert day.source.provider == "apple"
        # Energy comes whole from the band: its active energy is not topped up with the
        # watch's basal energy.
        assert day.active_calories_kcal == 2500
        assert day.total_calories_kcal == 2500

    def test_each_metric_comes_from_the_first_provider_that_holds_it(
        self, db: Session, service: SummariesService
    ) -> None:
        """The top provider keeps the metrics it has; only the ones it lacks fall through."""
        user = UserFactory()
        ring, watch = self._ring_and_watch(db, user)
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
        flights_type = SeriesTypeDefinitionFactory.get_or_create_flights_climbed()
        DataPointSeriesFactory(
            data_source=ring,
            series_type=steps_type,
            value=2128,
            is_daily_total=True,
            recorded_at=_dt("2026-01-01T00:00:00+00:00"),
        )
        at = _dt("2026-01-01T10:00:00+00:00")
        DataPointSeriesFactory(data_source=watch, series_type=steps_type, value=3600, recorded_at=at)
        DataPointSeriesFactory(data_source=watch, series_type=flights_type, value=12, recorded_at=at)

        result = service.get_activity_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        day = result.data[0]
        assert (day.steps, day.source.provider) == (2128, "oura")
        assert day.floors_climbed == 12

    def test_a_day_without_steps_from_any_source_has_null_steps(self, db: Session, service: SummariesService) -> None:
        """Heart rate alone is a day with no step count, not a day of zero steps and zero calories."""
        user = UserFactory()
        ring, _watch = self._ring_and_watch(db, user)
        hr_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        DataPointSeriesFactory(
            data_source=ring, series_type=hr_type, value=62, recorded_at=_dt("2026-01-01T09:00:00+00:00")
        )

        result = service.get_activity_summaries(
            db,
            user.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )

        day = result.data[0]
        assert (day.steps, day.active_calories_kcal, day.total_calories_kcal) == (None, None, None)
        assert day.heart_rate is not None
        assert day.heart_rate.avg_bpm == 62

    def test_does_not_return_other_users_data(self, db: Session, service: SummariesService) -> None:
        user_a = UserFactory()
        user_b = UserFactory()
        ds = DataSourceFactory(user=user_b)
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
        DataPointSeriesFactory(
            data_source=ds, series_type=steps_type, value=5000, recorded_at=_dt("2026-01-01T10:00:00+00:00")
        )

        result = service.get_activity_summaries(
            db,
            user_a.id,
            _dt("2026-01-01T00:00:00+00:00"),
            _dt("2026-01-02T00:00:00+00:00"),
            cursor=None,
            limit=10,
        )
        assert result.data == []


class TestActivityPagingWindow:
    """Each page aggregates only a window of days around it, widened as needed."""

    def test_pages_through_sparse_days_without_skipping_or_repeating(
        self, db: Session, service: SummariesService
    ) -> None:
        # A day a month for two years: far sparser than any first window, so every
        # page has to widen it, and the cursor must still hand over cleanly.
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.APPLE, source="apple_health_sdk")
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
        start = _dt("2024-01-01T00:00:00+00:00")
        days = [start + timedelta(days=30 * i, hours=10) for i in range(25)]
        for at in days:
            DataPointSeriesFactory(data_source=ds, series_type=steps_type, value=1000, recorded_at=at)
        expected = [at.date() for at in days]
        end = _dt("2026-06-01T00:00:00+00:00")

        for order, want in (("desc", list(reversed(expected))), ("asc", expected)):
            seen: list[date] = []
            cursor = None
            pages = []
            while True:
                page = service.get_activity_summaries(db, user.id, start, end, cursor=cursor, limit=3, sort_order=order)
                pages.append(page)
                seen += [summary.date for summary in page.data]
                if not page.pagination.next_cursor:
                    break
                cursor = page.pagination.next_cursor
            assert seen == want

            # Back from the third page lands on the second.
            back = service.get_activity_summaries(
                db, user.id, start, end, cursor=pages[2].pagination.previous_cursor, limit=3, sort_order=order
            )
            assert sorted(summary.date for summary in back.data) == sorted(summary.date for summary in pages[1].data)


class TestActivityTotals:
    def test_adds_up_the_days_the_summaries_return(self, db: Session, service: SummariesService) -> None:
        # The total is defined by the daily summaries, so it is checked against them.
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.APPLE, source="apple_health_sdk")
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
        start = _dt("2026-01-01T00:00:00+00:00")
        for day, value in ((0, 1000), (1, 3000), (5, 2000)):
            DataPointSeriesFactory(
                data_source=ds, series_type=steps_type, value=value, recorded_at=start + timedelta(days=day, hours=10)
            )
        end = _dt("2026-02-01T00:00:00+00:00")

        totals = service.get_activity_totals(db, user.id, start, end)
        days = service.get_activity_summaries(db, user.id, start, end, cursor=None, limit=100).data

        assert totals.days == len(days) == 3
        assert totals.steps == sum(day.steps or 0 for day in days) == 6000
        assert totals.avg_steps == 2000

    def test_a_heart_rate_only_day_is_a_day_without_steps(self, db: Session, service: SummariesService) -> None:
        """A day the ring only synced heart rate for counts as a day, but not as zero steps in the average."""
        user = UserFactory()
        ring = DataSourceFactory(user=user, provider=ProviderName.OURA, source="oura", device_model=None)
        steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
        hr_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        start = _dt("2026-01-01T00:00:00+00:00")
        DataPointSeriesFactory(
            data_source=ring, series_type=steps_type, value=4000, is_daily_total=True, recorded_at=start
        )
        DataPointSeriesFactory(
            data_source=ring, series_type=hr_type, value=60, recorded_at=start + timedelta(days=1, hours=9)
        )

        totals = service.get_activity_totals(db, user.id, start, start + timedelta(days=7))

        assert (totals.days, totals.steps, totals.avg_steps) == (2, 4000, 4000)

    def test_is_empty_without_data(self, db: Session, service: SummariesService) -> None:
        totals = service.get_activity_totals(
            db, UserFactory().id, _dt("2026-01-01T00:00:00+00:00"), _dt("2026-02-01T00:00:00+00:00")
        )
        assert (totals.days, totals.steps, totals.avg_steps) == (0, 0, None)
