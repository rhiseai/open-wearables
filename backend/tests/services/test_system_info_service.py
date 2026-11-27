"""
Tests for SystemInfoService.

Tests cover:
- Getting system dashboard information
- Aggregating metrics from multiple services

Note: the total data-points count is served approximately (planner statistics / cache) rather than
an exact ``COUNT(*)``, so it is only asserted to be a valid non-negative integer.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, EventRecordAggregate, SeriesTypeDefinition
from app.schemas.enums import ComputedBy, ProviderName, SeriesType, get_series_type_id
from app.services.providers.factory import ProviderFactory
from app.services.providers.withings.data_247 import Withings247Data
from app.services.system_info_service import system_info_service
from tests.factories import (
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    SeriesTypeDefinitionFactory,
    UserConnectionFactory,
    UserFactory,
    WorkoutDetailsFactory,
    provider_day,
    roll_up_everything,
)


class TestSystemInfoServiceGetSystemInfo:
    """Test getting system information."""

    def test_get_system_info_structure(self, db: Session) -> None:
        """Should return properly structured system info."""
        # Act
        info = system_info_service.get_system_info(db)

        # Assert
        assert info.total_users is not None
        assert info.active_conn is not None
        assert info.data_points is not None

        assert hasattr(info.total_users, "count")
        assert hasattr(info.active_conn, "count")
        assert hasattr(info.data_points, "count")
        assert isinstance(info.data_points.count, int)

    def test_get_system_info_total_users(self, db: Session) -> None:
        """Should count total users correctly."""
        # Arrange
        initial_info = system_info_service.get_system_info(db)
        initial_count = initial_info.total_users.count

        # Create new users
        UserFactory(email="user1@example.com")
        UserFactory(email="user2@example.com")

        # Act
        info = system_info_service.get_system_info(db)

        # Assert
        assert info.total_users.count == initial_count + 2

    def test_get_system_info_active_connections(self, db: Session) -> None:
        """Should count active connections correctly."""
        # Arrange
        from app.schemas.auth import ConnectionStatus

        initial_info = system_info_service.get_system_info(db)
        initial_count = initial_info.active_conn.count

        user = UserFactory()

        # Create active and inactive connections with different providers
        UserConnectionFactory(user=user, provider="garmin", status=ConnectionStatus.ACTIVE)
        UserConnectionFactory(user=user, provider="polar", status=ConnectionStatus.ACTIVE)
        UserConnectionFactory(user=user, provider="suunto", status=ConnectionStatus.REVOKED)

        # Act
        info = system_info_service.get_system_info(db)

        # Assert
        assert info.active_conn.count >= initial_count + 2

    def test_get_system_info_data_points_count_is_valid_int(self, db: Session) -> None:
        """The data-points count is approximate/cached; assert only that it is a non-negative int."""
        # Arrange
        mapping = DataSourceFactory()
        series_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        for _ in range(5):
            DataPointSeriesFactory(mapping=mapping, series_type=series_type)

        # Act
        info = system_info_service.get_system_info(db)

        # Assert
        assert isinstance(info.data_points.count, int)
        assert info.data_points.count >= 0

    def test_get_system_info_event_records(self, db: Session) -> None:
        """Should report event-record totals with a per-category breakdown."""
        # Arrange
        initial = system_info_service.get_system_info(db).event_records
        mapping = DataSourceFactory()
        EventRecordFactory(mapping=mapping, category="workout", type_="running")
        EventRecordFactory(mapping=mapping, category="workout", type_="cycling")
        EventRecordFactory(mapping=mapping, category="sleep", type_=None)

        # Act
        event_records = system_info_service.get_system_info(db).event_records

        # Assert
        assert event_records.workouts == initial.workouts + 2
        assert event_records.sleep == initial.sleep + 1
        assert event_records.count == initial.count + 3
        assert isinstance(event_records.menstrual_cycles, int)

    def test_get_system_info_connections_coverage(self, db: Session) -> None:
        """Should include connections coverage in the response."""
        # Act
        info = system_info_service.get_system_info(db)

        # Assert
        assert hasattr(info.connections_coverage, "users_with_active")
        assert hasattr(info.connections_coverage, "users_with_multi_active")
        assert isinstance(info.connections_coverage.top_providers, list)

    def test_get_system_info_empty_database(self, db: Session) -> None:
        """Should handle empty database gracefully."""
        # Act
        info = system_info_service.get_system_info(db)

        # Assert
        assert info.total_users.count >= 0
        assert info.active_conn.count >= 0
        assert info.data_points.count >= 0


class TestUserDataSummaryCountsDayValues:
    def test_a_providers_day_value_counts_as_a_data_point(self, db: Session) -> None:
        """Garmin's daily steps were one sample a day; as a day row they still count for the user's summary."""
        source = DataSourceFactory(provider=ProviderName.GARMIN)
        provider_day(db, source, SeriesType.steps, date(2026, 3, 1), sum=Decimal("8000"))

        summary = system_info_service.get_user_data_summary(db, source.user_id)

        by_provider = {entry.provider: entry for entry in summary.by_provider}
        assert (by_provider["garmin"].data_points, by_provider["garmin"].series_counts) == (1, {"steps": 1})


# A Withings night on a free plan, which sends no HRV: its lowest heart rate is its one heart value.
_WITHINGS_NIGHT = {
    "startdate": 1594159200,
    "enddate": 1594188000,
    "id": 12345,
    "timezone": "Europe/Warsaw",
    "data": {"total_timeinbed": 28800, "total_sleep_time": 25200, "hr_min": 48},
}
_NIGHT_START = datetime(2020, 7, 7, 22, tzinfo=timezone.utc)


def _withings_night(db: Session) -> UUID:
    """A user with one Withings night, saved the way the sync saves it: hr_min is the sleep's resting heart rate."""
    user = UserFactory()
    data_247 = ProviderFactory().get_provider("withings").data_247
    assert isinstance(data_247, Withings247Data)
    with (
        patch("app.services.providers.withings.data_247.paginate", return_value=MagicMock(rows=[_WITHINGS_NIGHT])),
        patch("app.services.providers.withings.data_247.withings_request", return_value={"series": []}),
    ):
        saved = data_247.save_sleep(db, user.id, _NIGHT_START - timedelta(days=1), _NIGHT_START + timedelta(days=1))
    assert saved == 1
    return user.id


class TestUserDataSummaryCountsNightValues:
    def test_a_withings_night_without_hrv_counts_its_resting_heart_rate(self, db: Session) -> None:
        """hr_min was a sample before the aggregate tables and is now the sleep's provider value: it still counts,
        so a reader that offers Withings for heart data from these counts still offers it."""
        user_id = _withings_night(db)

        summary = system_info_service.get_user_data_summary(db, user_id)

        withings = {entry.provider: entry for entry in summary.by_provider}["withings"]
        assert (withings.data_points, withings.series_counts, withings.sleep_count) == (
            1,
            {"resting_heart_rate": 1},
            1,
        )

    @pytest.mark.parametrize(
        ("start", "end", "counted"),
        [
            (_NIGHT_START, _NIGHT_START + timedelta(hours=1), 1),
            (_NIGHT_START + timedelta(seconds=1), None, 0),
            (None, _NIGHT_START, 0),
        ],
    )
    def test_a_window_counts_the_nights_that_started_in_it(
        self, db: Session, start: datetime | None, end: datetime | None, counted: int
    ) -> None:
        user_id = _withings_night(db)

        summary = system_info_service.get_user_data_summary(db, user_id, start, end)

        assert summary.series_type_counts.get("resting_heart_rate", 0) == counted

    def test_our_rollup_of_a_nights_readings_is_not_counted_again(self, db: Session) -> None:
        """The rollup keeps its own statistics of the night's readings on the sleep; the readings already count."""
        user_id = _withings_night(db)
        sleep = db.query(EventRecord).join(DataSource).filter(DataSource.user_id == user_id).one()
        rmssd = db.get(SeriesTypeDefinition, get_series_type_id(SeriesType.heart_rate_variability_rmssd))
        DataPointSeriesFactory(
            data_source=db.get(DataSource, sleep.data_source_id),
            series_type=rmssd,
            recorded_at=_NIGHT_START + timedelta(hours=2),
            value=Decimal("40"),
        )
        roll_up_everything(db)
        ours = db.query(EventRecordAggregate).filter(
            EventRecordAggregate.record_id == sleep.id, EventRecordAggregate.computed_by == ComputedBy.OPEN_WEARABLES
        )
        assert ours.count() > 0

        summary = system_info_service.get_user_data_summary(db, user_id)

        assert summary.series_type_counts == {"resting_heart_rate": 1, "heart_rate_variability_rmssd": 1}

    def test_a_workouts_own_statistics_are_not_counted(self, db: Session) -> None:
        """A workout's heart rate and steps are its workout_details columns, kept as provider rows on the workout:
        they never were samples, so they add no data points."""
        source = DataSourceFactory(provider=ProviderName.GARMIN)
        workout = EventRecordFactory(category="workout", data_source=source)
        WorkoutDetailsFactory(event_record=workout)
        assert db.query(EventRecordAggregate).filter(EventRecordAggregate.record_id == workout.id).count() > 0

        summary = system_info_service.get_user_data_summary(db, source.user_id)

        assert (summary.series_type_counts, summary.total_workouts) == ({}, 1)
