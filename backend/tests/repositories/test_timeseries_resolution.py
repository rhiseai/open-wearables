"""Server-side downsampling for ``/timeseries`` (``resolution``).

The parameter was accepted and dropped: every read returned raw samples,
so a caller sizing its page budget for "5min" fetched a fraction of the
window it asked for. These tests pin the bucketing itself — the aggregate
per series type, what stays separate inside a bucket, and paging over
buckets rather than samples.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataPointSeries
from app.repositories.data_point_series_repository import DataPointSeriesRepository
from app.schemas.enums import SeriesType, TimeseriesResolution, get_series_type_id
from app.schemas.model_crud.activities import TimeSeriesQueryParams, TimeSeriesSampleCreate
from app.utils.pagination import encode_cursor
from tests.factories import DataSourceFactory, UserFactory

_START = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def series_repo() -> DataPointSeriesRepository:
    return DataPointSeriesRepository(DataPointSeries)


def _sample(
    repo: DataPointSeriesRepository,
    db: Session,
    data_source,  # noqa: ANN001 - DataSource model instance from the factory
    user_id,  # noqa: ANN001 - UUID
    *,
    minutes: float,
    value: float,
    series_type: SeriesType = SeriesType.heart_rate,
    is_daily_total: bool | None = None,
) -> None:
    repo.create(
        db,
        TimeSeriesSampleCreate(
            id=uuid4(),
            user_id=user_id,
            source=data_source.source,
            device_model=data_source.device_model,
            data_source_id=data_source.id,
            recorded_at=_START + timedelta(minutes=minutes),
            value=Decimal(str(value)),
            series_type=series_type,
            is_daily_total=is_daily_total,
        ),
    )


def _params(resolution: TimeseriesResolution, **kwargs) -> TimeSeriesQueryParams:  # noqa: ANN003
    return TimeSeriesQueryParams(
        start_datetime=_START,
        end_datetime=_START + timedelta(hours=6),
        resolution=resolution,
        **kwargs,
    )


class TestBucketAggregate:
    """The aggregate follows the series type's own method."""

    def test_rate_series_is_averaged_per_bucket(self, db: Session, series_repo: DataPointSeriesRepository) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user)
        # Two five-minute windows: 60/70/80 then 100.
        for minutes, value in ((0, 60), (2, 70), (4, 80), (5, 100)):
            _sample(series_repo, db, source, user.id, minutes=minutes, value=value)

        results, total_count = series_repo.get_samples(
            db, _params(TimeseriesResolution.FIVE_MINUTES), [SeriesType.heart_rate], user.id
        )

        assert total_count == 2
        assert [(s.recorded_at, float(s.value)) for s, _ in results] == [
            (_START, 70.0),
            (_START + timedelta(minutes=5), 100.0),
        ]

    def test_cumulative_series_is_summed_per_bucket(self, db: Session, series_repo: DataPointSeriesRepository) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user)
        for minutes, value in ((0, 100), (7, 250), (20, 40)):
            _sample(series_repo, db, source, user.id, minutes=minutes, value=value, series_type=SeriesType.steps)

        results, _ = series_repo.get_samples(
            db, _params(TimeseriesResolution.FIFTEEN_MINUTES), [SeriesType.steps], user.id
        )

        assert [(s.recorded_at, float(s.value)) for s, _ in results] == [
            (_START, 350.0),
            (_START + timedelta(minutes=15), 40.0),
        ]

    def test_buckets_start_on_the_wall_clock_not_on_the_range(
        self, db: Session, series_repo: DataPointSeriesRepository
    ) -> None:
        """A bucket covers the same window whenever it is asked for."""
        user = UserFactory()
        source = DataSourceFactory(user=user)
        _sample(series_repo, db, source, user.id, minutes=3, value=60)
        _sample(series_repo, db, source, user.id, minutes=63, value=80)

        params = TimeSeriesQueryParams(
            start_datetime=_START + timedelta(minutes=1),
            end_datetime=_START + timedelta(hours=6),
            resolution=TimeseriesResolution.ONE_HOUR,
        )
        results, _ = series_repo.get_samples(db, params, [SeriesType.heart_rate], user.id)

        assert [s.recorded_at for s, _ in results] == [_START, _START + timedelta(hours=1)]


class TestBucketGrouping:
    """What a bucket must not merge."""

    def test_sources_stay_separate(self, db: Session, series_repo: DataPointSeriesRepository) -> None:
        user = UserFactory()
        watch = DataSourceFactory(user=user, device_model="Watch7,1", source="watch_app")
        phone = DataSourceFactory(user=user, device_model="iPhone15,2", source="phone_app")
        _sample(series_repo, db, watch, user.id, minutes=0, value=60)
        _sample(series_repo, db, phone, user.id, minutes=1, value=90)

        results, total_count = series_repo.get_samples(
            db, _params(TimeseriesResolution.FIVE_MINUTES), [SeriesType.heart_rate], user.id
        )

        assert total_count == 2
        by_device = {ds.device_model: float(s.value) for s, ds in results}
        assert by_device == {"Watch7,1": 60.0, "iPhone15,2": 90.0}

    def test_series_types_stay_separate(self, db: Session, series_repo: DataPointSeriesRepository) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user)
        _sample(series_repo, db, source, user.id, minutes=0, value=60)
        _sample(series_repo, db, source, user.id, minutes=1, value=500, series_type=SeriesType.steps)

        results, _ = series_repo.get_samples(
            db,
            _params(TimeseriesResolution.ONE_HOUR),
            [SeriesType.heart_rate, SeriesType.steps],
            user.id,
        )

        by_type = {s.series_type_definition_id: float(s.value) for s, _ in results}
        assert by_type == {
            get_series_type_id(SeriesType.heart_rate): 60.0,
            get_series_type_id(SeriesType.steps): 500.0,
        }

    def test_a_daily_total_is_never_summed_into_its_own_intraday_samples(
        self, db: Session, series_repo: DataPointSeriesRepository
    ) -> None:
        """Garmin and Suunto send both; adding them would double the day."""
        user = UserFactory()
        source = DataSourceFactory(user=user)
        steps = {"series_type": SeriesType.steps}
        _sample(series_repo, db, source, user.id, minutes=0, value=100, is_daily_total=False, **steps)
        _sample(series_repo, db, source, user.id, minutes=1, value=150, is_daily_total=False, **steps)
        _sample(series_repo, db, source, user.id, minutes=2, value=8000, is_daily_total=True, **steps)

        results, _ = series_repo.get_samples(db, _params(TimeseriesResolution.ONE_HOUR), [SeriesType.steps], user.id)

        assert sorted(float(s.value) for s, _ in results) == [250.0, 8000.0]
        assert {s.is_daily_total for s, _ in results} == {False, True}


class TestBucketPagination:
    """Cursors page over buckets, and the count is a bucket count."""

    def test_total_count_is_buckets_not_samples(self, db: Session, series_repo: DataPointSeriesRepository) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user)
        for minutes in range(12):
            _sample(series_repo, db, source, user.id, minutes=minutes, value=60 + minutes)

        _, total_count = series_repo.get_samples(
            db, _params(TimeseriesResolution.FIVE_MINUTES), [SeriesType.heart_rate], user.id
        )

        assert total_count == 3

    def test_a_cursor_resumes_after_the_bucket_it_names(
        self, db: Session, series_repo: DataPointSeriesRepository
    ) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user)
        for minutes in range(0, 20, 5):
            _sample(series_repo, db, source, user.id, minutes=minutes, value=60 + minutes)

        first_page, _ = series_repo.get_samples(
            db, _params(TimeseriesResolution.FIVE_MINUTES, limit=2), [SeriesType.heart_rate], user.id
        )
        # The repository returns limit + 1 so the service can see a next page.
        assert len(first_page) == 3
        last_of_page = first_page[1][0]
        cursor = encode_cursor(last_of_page.recorded_at, last_of_page.id, "next")

        second_page, total_count = series_repo.get_samples(
            db,
            _params(TimeseriesResolution.FIVE_MINUTES, limit=2, cursor=cursor),
            [SeriesType.heart_rate],
            user.id,
        )

        assert total_count == 4
        assert [s.recorded_at for s, _ in second_page] == [
            _START + timedelta(minutes=10),
            _START + timedelta(minutes=15),
        ]


class TestRawIsUnchanged:
    def test_raw_still_returns_every_sample(self, db: Session, series_repo: DataPointSeriesRepository) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user)
        for minutes in range(4):
            _sample(series_repo, db, source, user.id, minutes=minutes, value=60 + minutes)

        results, total_count = series_repo.get_samples(
            db, _params(TimeseriesResolution.RAW), [SeriesType.heart_rate], user.id
        )

        assert total_count == 4
        assert [float(s.value) for s, _ in results] == [60.0, 61.0, 62.0, 63.0]
