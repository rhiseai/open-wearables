"""
Tests for HealthScoreRepository.

Tests cover:
- Basic CRUD via CrudRepository base
- get_with_filters: category, provider, date range, user scoping
- get_latest_by_category
- get_latest_per_category
- bulk_create with on_conflict_do_nothing
- bulk_create_keeping_highest
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import HealthScore, ProviderPriority
from app.repositories import ProviderPriorityRepository
from app.repositories.health_score_repository import HealthScoreRepository
from app.repositories.repositories import SourceRanking
from app.schemas.enums import HealthScoreCategory, ProviderName
from app.schemas.model_crud.activities import HealthScoreCreate, HealthScoreQueryParams
from app.schemas.model_crud.activities.health_score import ScoreComponent
from tests.factories import (
    DataSourceFactory,
    EventRecordFactory,
    HealthScoreFactory,
    SleepDetailsFactory,
    UserFactory,
)


@pytest.fixture
def repo() -> HealthScoreRepository:
    return HealthScoreRepository(HealthScore)


NIGHT = datetime(2026, 9, 24, 7, tzinfo=timezone.utc)


def _ranking(db: Session) -> SourceRanking:
    return SourceRanking(ProviderPriorityRepository(ProviderPriority).get_priority_order(db), {})


def _prioritise(db: Session, *providers: ProviderName) -> None:
    priorities = ProviderPriorityRepository(ProviderPriority)
    for rank, provider in enumerate(providers, start=1):
        priorities.upsert(db, provider, rank)


class TestHealthScoreRepositoryCreate:
    def test_create(self, db: Session, repo: HealthScoreRepository) -> None:
        data_source = DataSourceFactory()
        score = HealthScoreCreate(
            id=uuid4(),
            user_id=data_source.user_id,
            data_source_id=data_source.id,
            provider=ProviderName.GARMIN,
            category=HealthScoreCategory.SLEEP,
            value=Decimal("82.00"),
            qualifier="GOOD",
            recorded_at=datetime.now(timezone.utc),
        )

        result = repo.create(db, score)

        assert result.id == score.id
        assert result.category == HealthScoreCategory.SLEEP
        assert result.value == Decimal("82.00")
        assert result.qualifier == "GOOD"

    def test_create_duplicate_returns_existing(self, db: Session, repo: HealthScoreRepository) -> None:
        recorded_at = datetime.now(timezone.utc)
        data_source = DataSourceFactory()
        score = HealthScoreCreate(
            id=uuid4(),
            user_id=data_source.user_id,
            data_source_id=data_source.id,
            provider=ProviderName.GARMIN,
            category=HealthScoreCategory.SLEEP,
            value=Decimal("82.00"),
            recorded_at=recorded_at,
        )
        repo.create(db, score)

        duplicate = HealthScoreCreate(
            id=uuid4(),
            user_id=data_source.user_id,
            data_source_id=data_source.id,
            provider=ProviderName.GARMIN,
            category=HealthScoreCategory.SLEEP,
            value=Decimal("90.00"),
            recorded_at=recorded_at,
        )
        result = repo.create(db, duplicate)

        assert result.id == score.id
        assert result.value == Decimal("82.00")

    def test_create_duplicate_without_a_source_returns_existing(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        recorded_at = datetime(2026, 9, 24, 7, tzinfo=timezone.utc)
        scores = [
            HealthScoreCreate(
                id=uuid4(),
                user_id=user.id,
                provider=ProviderName.WHOOP,
                category=HealthScoreCategory.RECOVERY,
                value=Decimal(value),
                recorded_at=recorded_at,
            )
            for value in ("70.00", "75.00")
        ]

        first = repo.create(db, scores[0])
        result = repo.create(db, scores[1])

        assert result.id == first.id
        assert result.value == Decimal("70.00")


class TestHealthScoreRepositoryGetWithFilters:
    def test_filter_by_category(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        data_source = DataSourceFactory(user=user)
        HealthScoreFactory(data_source=data_source, category=HealthScoreCategory.SLEEP)
        HealthScoreFactory(data_source=data_source, category=HealthScoreCategory.SLEEP)
        HealthScoreFactory(data_source=data_source, category=HealthScoreCategory.RECOVERY)

        results, total = repo.get_with_filters(
            db, user.id, HealthScoreQueryParams(category=HealthScoreCategory.SLEEP), SourceRanking({}, {})
        )

        assert total == 2
        assert all(s.category == HealthScoreCategory.SLEEP for s, _ in results)

    def test_filter_by_provider(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        data_source = DataSourceFactory(user=user)
        HealthScoreFactory(data_source=data_source, provider=ProviderName.GARMIN)
        HealthScoreFactory(data_source=data_source, provider=ProviderName.OURA)

        results, total = repo.get_with_filters(
            db, user.id, HealthScoreQueryParams(provider=ProviderName.GARMIN), SourceRanking({}, {})
        )

        assert total == 1
        assert results[0][0].provider == ProviderName.GARMIN

    def test_filter_by_date_range(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        data_source = DataSourceFactory(user=user)
        now = datetime.now(timezone.utc)
        HealthScoreFactory(data_source=data_source, recorded_at=now - timedelta(days=1))
        HealthScoreFactory(data_source=data_source, recorded_at=now - timedelta(days=5))
        HealthScoreFactory(data_source=data_source, recorded_at=now - timedelta(days=10))

        results, total = repo.get_with_filters(
            db,
            user.id,
            HealthScoreQueryParams(
                start_datetime=now - timedelta(days=6),
                end_datetime=now,
            ),
            SourceRanking({}, {}),
        )

        assert total == 2

    def test_scoped_to_user(self, db: Session, repo: HealthScoreRepository) -> None:
        user_a = UserFactory()
        user_b = UserFactory()
        HealthScoreFactory(data_source=DataSourceFactory(user=user_a))
        HealthScoreFactory(data_source=DataSourceFactory(user=user_b))

        results, total = repo.get_with_filters(db, user_a.id, HealthScoreQueryParams(), SourceRanking({}, {}))

        assert total == 1


class TestHealthScoreRepositoryLatest:
    def test_get_latest_by_category(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        data_source = DataSourceFactory(user=user)
        now = datetime.now(timezone.utc)
        HealthScoreFactory(
            data_source=data_source,
            category=HealthScoreCategory.SLEEP,
            recorded_at=now - timedelta(days=2),
            value=Decimal("70.00"),
        )
        latest = HealthScoreFactory(
            data_source=data_source, category=HealthScoreCategory.SLEEP, recorded_at=now, value=Decimal("85.00")
        )

        result = repo.get_latest_by_category(db, user.id, HealthScoreCategory.SLEEP)

        assert result is not None
        assert result.id == latest.id
        assert result.value == Decimal("85.00")

    def test_get_latest_by_category_returns_none_when_missing(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()

        result = repo.get_latest_by_category(db, user.id, HealthScoreCategory.RECOVERY)

        assert result is None

    def test_get_latest_per_category(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        data_source = DataSourceFactory(user=user)
        now = datetime.now(timezone.utc)
        HealthScoreFactory(
            data_source=data_source, category=HealthScoreCategory.SLEEP, recorded_at=now - timedelta(days=1)
        )
        HealthScoreFactory(data_source=data_source, category=HealthScoreCategory.SLEEP, recorded_at=now)
        HealthScoreFactory(data_source=data_source, category=HealthScoreCategory.RECOVERY, recorded_at=now)

        results = repo.get_latest_per_category(db, user.id)

        categories = {s.category for s in results}
        assert categories == {HealthScoreCategory.SLEEP, HealthScoreCategory.RECOVERY}
        assert len(results) == 2


class TestHealthScoreRepositoryBulkCreate:
    def test_bulk_create(self, db: Session, repo: HealthScoreRepository) -> None:
        data_source = DataSourceFactory()
        now = datetime.now(timezone.utc)
        scores = [
            HealthScoreCreate(
                id=uuid4(),
                user_id=data_source.user_id,
                data_source_id=data_source.id,
                provider=ProviderName.GARMIN,
                category=HealthScoreCategory.SLEEP,
                value=Decimal("80.00"),
                recorded_at=now - timedelta(days=i),
            )
            for i in range(3)
        ]

        repo.bulk_create(db, scores)
        db.commit()

        results = db.query(HealthScore).filter(HealthScore.data_source_id == data_source.id).all()
        assert len(results) == 3

    def test_bulk_create_ignores_duplicates(self, db: Session, repo: HealthScoreRepository) -> None:
        data_source = DataSourceFactory()
        recorded_at = datetime.now(timezone.utc)
        original = HealthScoreCreate(
            id=uuid4(),
            user_id=data_source.user_id,
            data_source_id=data_source.id,
            provider=ProviderName.GARMIN,
            category=HealthScoreCategory.SLEEP,
            value=Decimal("80.00"),
            recorded_at=recorded_at,
        )
        repo.bulk_create(db, [original])
        db.commit()

        duplicate = HealthScoreCreate(
            id=uuid4(),
            user_id=data_source.user_id,
            data_source_id=data_source.id,
            provider=ProviderName.GARMIN,
            category=HealthScoreCategory.SLEEP,
            value=Decimal("99.00"),
            recorded_at=recorded_at,
        )
        repo.bulk_create(db, [duplicate])
        db.commit()

        results = db.query(HealthScore).filter(HealthScore.data_source_id == data_source.id).all()
        assert len(results) == 1
        assert results[0].value == Decimal("80.00")

    def test_bulk_create_keeps_scores_of_two_sources_at_the_same_time(
        self, db: Session, repo: HealthScoreRepository
    ) -> None:
        user = UserFactory()
        garmin = DataSourceFactory(user=user, provider=ProviderName.GARMIN)
        oura = DataSourceFactory(user=user, provider=ProviderName.OURA)
        recorded_at = datetime(2026, 9, 24, 7, tzinfo=timezone.utc)

        repo.bulk_create(
            db,
            [
                HealthScoreCreate(
                    id=uuid4(),
                    user_id=user.id,
                    data_source_id=source.id,
                    provider=ProviderName.INTERNAL,
                    category=HealthScoreCategory.SLEEP,
                    value=Decimal("80.00"),
                    recorded_at=recorded_at,
                )
                for source in (garmin, oura)
            ],
        )
        db.commit()

        assert db.query(HealthScore).filter(HealthScore.user_id == user.id).count() == 2

    def test_bulk_create_ignores_duplicate_scores_without_a_source(
        self, db: Session, repo: HealthScoreRepository
    ) -> None:
        user = UserFactory()
        recorded_at = datetime(2026, 9, 24, 7, tzinfo=timezone.utc)
        for value in ("80.00", "99.00"):
            repo.bulk_create(
                db,
                [
                    HealthScoreCreate(
                        id=uuid4(),
                        user_id=user.id,
                        provider=ProviderName.OURA,
                        category=HealthScoreCategory.SLEEP,
                        value=Decimal(value),
                        recorded_at=recorded_at,
                    )
                ],
            )
            db.commit()

        scores = db.query(HealthScore).filter(HealthScore.user_id == user.id).all()
        assert [score.value for score in scores] == [Decimal("80.00")]


class TestHealthScoreRepositorySourcePriority:
    def _ow(self, data_source: Any, recorded_at: datetime = NIGHT, **kwargs: Any) -> HealthScore:
        return HealthScoreFactory(
            data_source=data_source, provider=ProviderName.INTERNAL, recorded_at=recorded_at, **kwargs
        )

    def test_keeps_the_ow_score_of_the_highest_priority_source(self, db: Session, repo: HealthScoreRepository) -> None:
        _prioritise(db, ProviderName.GARMIN, ProviderName.OURA)
        user = UserFactory()
        garmin = self._ow(DataSourceFactory(user=user, provider=ProviderName.GARMIN))
        self._ow(DataSourceFactory(user=user, provider=ProviderName.OURA), NIGHT + timedelta(hours=1))

        results, total = repo.get_with_filters(db, user.id, HealthScoreQueryParams(), _ranking(db))

        assert total == 1
        assert [score.id for score, _ in results] == [garmin.id]

    def test_priority_decides_not_the_order_of_writes(self, db: Session, repo: HealthScoreRepository) -> None:
        _prioritise(db, ProviderName.OURA, ProviderName.GARMIN)
        user = UserFactory()
        self._ow(DataSourceFactory(user=user, provider=ProviderName.GARMIN))
        oura = self._ow(DataSourceFactory(user=user, provider=ProviderName.OURA))

        results, _ = repo.get_with_filters(db, user.id, HealthScoreQueryParams(), _ranking(db))

        assert [score.id for score, _ in results] == [oura.id]

    def test_one_ow_score_per_category(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user, provider=ProviderName.GARMIN)
        self._ow(source, category=HealthScoreCategory.SLEEP)
        self._ow(source, category=HealthScoreCategory.RESILIENCE)

        _, total = repo.get_with_filters(db, user.id, HealthScoreQueryParams(), _ranking(db))

        assert total == 2

    def test_the_night_wins_over_an_unflagged_nap_of_the_same_source(
        self, db: Session, repo: HealthScoreRepository
    ) -> None:
        """SDK naps can come without the nap flag (#938) and get a sleep score of their own."""
        user = UserFactory()
        source = DataSourceFactory(user=user, provider=ProviderName.APPLE)
        scores = []
        for wake_up, hours in ((NIGHT, 8), (NIGHT + timedelta(hours=8), 1)):
            session = EventRecordFactory(
                data_source=source,
                category="sleep",
                type="sleep",
                start_datetime=wake_up - timedelta(hours=hours),
                end_datetime=wake_up,
                duration_seconds=hours * 3600,
            )
            SleepDetailsFactory(event_record=session, is_nap=False, sleep_total_duration_minutes=hours * 60 - 20)
            scores.append(self._ow(source, wake_up, event_record_id=session.id))

        results, _ = repo.get_with_filters(db, user.id, HealthScoreQueryParams(), _ranking(db))

        assert [score.id for score, _ in results] == [scores[0].id]

    def test_days_are_utc_dates_whatever_the_database_time_zone(self, db: Session, repo: HealthScoreRepository) -> None:
        """01:00 UTC is still the previous evening in New York; it must not split the day."""
        db.execute(text("SET LOCAL TIME ZONE 'America/New_York'"))
        _prioritise(db, ProviderName.GARMIN, ProviderName.OURA)
        user = UserFactory()
        garmin = self._ow(DataSourceFactory(user=user, provider=ProviderName.GARMIN), NIGHT - timedelta(hours=6))
        self._ow(DataSourceFactory(user=user, provider=ProviderName.OURA), NIGHT)

        results, _ = repo.get_with_filters(db, user.id, HealthScoreQueryParams(), _ranking(db))

        assert [score.id for score, _ in results] == [garmin.id]

    @pytest.mark.parametrize(
        ("garmin_at", "oura_at", "params"),
        [
            (NIGHT, NIGHT + timedelta(hours=6), HealthScoreQueryParams(start_datetime=NIGHT + timedelta(hours=5))),
            (NIGHT + timedelta(hours=6), NIGHT, HealthScoreQueryParams(end_datetime=NIGHT + timedelta(hours=1))),
        ],
    )
    def test_a_bound_inside_a_day_does_not_change_its_pick(
        self,
        db: Session,
        repo: HealthScoreRepository,
        garmin_at: datetime,
        oura_at: datetime,
        params: HealthScoreQueryParams,
    ) -> None:
        """The day's score is Garmin's; cutting it off returns nothing for the day, not Oura's."""
        _prioritise(db, ProviderName.GARMIN, ProviderName.OURA)
        user = UserFactory()
        self._ow(DataSourceFactory(user=user, provider=ProviderName.GARMIN), garmin_at)
        self._ow(DataSourceFactory(user=user, provider=ProviderName.OURA), oura_at)

        results, total = repo.get_with_filters(db, user.id, params, _ranking(db))

        assert (results, total) == ([], 0)

    def test_a_bound_at_midnight_keeps_the_day_whole(self, db: Session, repo: HealthScoreRepository) -> None:
        _prioritise(db, ProviderName.GARMIN, ProviderName.OURA)
        user = UserFactory()
        garmin = self._ow(DataSourceFactory(user=user, provider=ProviderName.GARMIN))
        self._ow(DataSourceFactory(user=user, provider=ProviderName.OURA), NIGHT + timedelta(hours=1))
        day = NIGHT.replace(hour=0)

        results, _ = repo.get_with_filters(
            db,
            user.id,
            HealthScoreQueryParams(start_datetime=day, end_datetime=day + timedelta(days=1)),
            _ranking(db),
        )

        assert [score.id for score, _ in results] == [garmin.id]

    def test_provider_scores_are_returned_as_they_are(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        garmin = DataSourceFactory(user=user, provider=ProviderName.GARMIN)
        oura = DataSourceFactory(user=user, provider=ProviderName.OURA)
        HealthScoreFactory(data_source=garmin, provider=ProviderName.GARMIN, recorded_at=NIGHT)
        HealthScoreFactory(data_source=oura, provider=ProviderName.OURA, recorded_at=NIGHT)
        self._ow(garmin)
        self._ow(oura)

        results, total = repo.get_with_filters(db, user.id, HealthScoreQueryParams(), _ranking(db))

        assert total == 3
        assert sorted(score.provider for score, _ in results) == ["garmin", "internal", "oura"]

    def test_an_ow_score_with_a_source_beats_one_without(self, db: Session, repo: HealthScoreRepository) -> None:
        """Even when the source's provider is missing from the priorities."""
        user = UserFactory()
        sourced = self._ow(
            DataSourceFactory(user=user, provider=ProviderName.GARMIN), category=HealthScoreCategory.RESILIENCE
        )
        HealthScoreFactory(
            data_source_id=None,
            user_id=user.id,
            provider=ProviderName.INTERNAL,
            category=HealthScoreCategory.RESILIENCE,
            recorded_at=NIGHT,
        )

        results, _ = repo.get_with_filters(db, user.id, HealthScoreQueryParams(), SourceRanking({}, {}))

        assert [score.id for score, _ in results] == [sourced.id]

    def test_paging_counts_the_picked_scores(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        garmin = DataSourceFactory(user=user, provider=ProviderName.GARMIN)
        oura = DataSourceFactory(user=user, provider=ProviderName.OURA)
        for day in range(3):
            self._ow(garmin, NIGHT + timedelta(days=day))
            self._ow(oura, NIGHT + timedelta(days=day))

        results, total = repo.get_with_filters(db, user.id, HealthScoreQueryParams(offset=1, limit=1), _ranking(db))

        assert total == 3
        assert [score.recorded_at.date() for score, _ in results] == [(NIGHT + timedelta(days=1)).date()]

    def test_ow_score_without_a_source_is_kept(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        resilience = HealthScoreFactory(
            data_source_id=None,
            user_id=user.id,
            provider=ProviderName.INTERNAL,
            category=HealthScoreCategory.RESILIENCE,
            recorded_at=NIGHT,
        )

        results, _ = repo.get_with_filters(db, user.id, HealthScoreQueryParams(), _ranking(db))

        assert results == [(resilience, None)]

    def test_returns_each_score_with_its_source(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user, provider=ProviderName.GARMIN)
        score = self._ow(source)

        results, _ = repo.get_with_filters(db, user.id, HealthScoreQueryParams(), _ranking(db))

        assert results == [(score, source)]


def _body_battery(user_id: UUID, recorded_at: datetime, value: str | None) -> HealthScoreCreate:
    return HealthScoreCreate(
        id=uuid4(),
        user_id=user_id,
        provider=ProviderName.GARMIN,
        category=HealthScoreCategory.BODY_BATTERY,
        value=Decimal(value) if value is not None else None,
        recorded_at=recorded_at,
    )


class TestHealthScoreRepositoryBulkCreateKeepingHighest:
    def test_a_higher_value_replaces_the_stored_one(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        day = datetime(2026, 10, 28, tzinfo=timezone.utc)

        first = repo.bulk_create_keeping_highest(db, [_body_battery(user.id, day, "46")])
        higher = repo.bulk_create_keeping_highest(db, [_body_battery(user.id, day, "78")])
        lower = repo.bulk_create_keeping_highest(db, [_body_battery(user.id, day, "60")])
        db.commit()

        [score] = db.query(HealthScore).filter(HealthScore.user_id == user.id).all()
        assert score.value == Decimal("78")
        assert (first.inserted, first.updated) == (1, 0)
        assert (higher.inserted, higher.updated) == (0, 1)
        assert (lower.inserted, lower.updated) == (0, 0)

    def test_one_batch_keeps_the_highest_of_a_day(self, db: Session, repo: HealthScoreRepository) -> None:
        # ON CONFLICT DO UPDATE can't touch a row twice in one statement
        user = UserFactory()
        day = datetime(2026, 10, 28, tzinfo=timezone.utc)
        scores = [_body_battery(user.id, day, value) for value in ("46", "78", "60")]

        written = repo.bulk_create_keeping_highest(db, scores)
        db.commit()

        [score] = db.query(HealthScore).filter(HealthScore.user_id == user.id).all()
        assert score.value == Decimal("78")
        assert (written.inserted, written.updated) == (1, 0)

    def test_a_value_replaces_a_stored_empty_one(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        day = datetime(2026, 10, 28, tzinfo=timezone.utc)
        repo.bulk_create(db, [_body_battery(user.id, day, None)])

        written = repo.bulk_create_keeping_highest(db, [_body_battery(user.id, day, "78")])
        db.commit()

        [score] = db.query(HealthScore).filter(HealthScore.user_id == user.id).all()
        assert score.value == Decimal("78")
        assert (written.inserted, written.updated) == (0, 1)

    def test_a_score_without_a_value_is_skipped(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()

        written = repo.bulk_create_keeping_highest(db, [_body_battery(user.id, datetime.now(timezone.utc), None)])

        assert (written.inserted, written.updated) == (0, 0)
        assert db.query(HealthScore).filter(HealthScore.user_id == user.id).count() == 0

    def test_empty(self, db: Session, repo: HealthScoreRepository) -> None:
        written = repo.bulk_create_keeping_highest(db, [])

        assert (written.inserted, written.updated) == (0, 0)


class TestProviderSleepScoreOfAReSentNight:
    """A provider sleep score sits at its night's end, which a re-sent night moves: one score per night stays."""

    START = datetime(2026, 3, 1, 22, tzinfo=timezone.utc)

    def _night(self, end_hour: int) -> tuple[Any, Any]:
        user = UserFactory()
        source = DataSourceFactory(user=user, provider=ProviderName.WHOOP.value, source="whoop")
        record = EventRecordFactory(
            data_source=source,
            category="sleep",
            type_="sleep_session",
            start_datetime=self.START,
            end_datetime=self.START + timedelta(hours=end_hour),
        )
        return user, record

    def _score(self, user: Any, at: datetime, value: int) -> HealthScoreCreate:
        return HealthScoreCreate(
            id=uuid4(),
            user_id=user.id,
            provider=ProviderName.WHOOP,
            category=HealthScoreCategory.SLEEP,
            value=value,
            recorded_at=at,
        )

    def _write(self, db: Session, user: Any, at: datetime, value: int) -> None:
        HealthScoreRepository(HealthScore).bulk_create(db, [self._score(user, at, value)])

    def _scores(self, db: Session, user: Any) -> list[tuple[datetime, int]]:
        rows = db.query(HealthScore).filter(HealthScore.user_id == user.id).order_by(HealthScore.recorded_at)
        return [(row.recorded_at, int(row.value)) for row in rows]

    def test_an_extended_night_replaces_its_earlier_score(self, db: Session) -> None:
        user, record = self._night(8)
        self._write(db, user, self.START + timedelta(hours=8), 80)
        record.end_datetime = self.START + timedelta(hours=9)
        db.flush()

        self._write(db, user, self.START + timedelta(hours=9), 85)

        assert self._scores(db, user) == [(self.START + timedelta(hours=9), 85)]

    def test_a_night_re_sent_with_the_same_end_refreshes_its_score(self, db: Session) -> None:
        user, _ = self._night(8)
        self._write(db, user, self.START + timedelta(hours=8), 80)

        self._write(db, user, self.START + timedelta(hours=8), 82)

        assert self._scores(db, user) == [(self.START + timedelta(hours=8), 82)]

    def test_a_score_left_at_the_nights_start_gives_way(self, db: Session) -> None:
        user, _ = self._night(8)
        self._write(db, user, self.START, 70)

        self._write(db, user, self.START + timedelta(hours=8), 80)

        assert self._scores(db, user) == [(self.START + timedelta(hours=8), 80)]

    def test_an_earlier_version_in_the_same_batch_gives_way(self, db: Session) -> None:
        user, _ = self._night(9)

        HealthScoreRepository(HealthScore).bulk_create(
            db,
            [
                self._score(user, self.START + timedelta(hours=8), 80),
                self._score(user, self.START + timedelta(hours=9), 85),
            ],
        )

        assert self._scores(db, user) == [(self.START + timedelta(hours=9), 85)]

    def test_other_nights_and_internal_scores_stay(self, db: Session) -> None:
        user, _ = self._night(8)
        HealthScoreFactory(
            user_id=user.id, provider=ProviderName.INTERNAL, category=HealthScoreCategory.SLEEP, recorded_at=self.START
        )
        self._write(db, user, self.START - timedelta(days=1), 60)

        self._write(db, user, self.START + timedelta(hours=8), 80)

        assert len(self._scores(db, user)) == 3


class TestHealthScoreRepositoryProviderDailyUpsert:
    DAY = datetime(2026, 10, 21, tzinfo=timezone.utc)

    def _score(self, user_id: UUID, value: int = 61) -> HealthScoreCreate:
        return HealthScoreCreate(
            id=uuid4(),
            user_id=user_id,
            provider=ProviderName.OURA,
            category=HealthScoreCategory.SLEEP,
            value=value,
            recorded_at=self.DAY,
            components={"deep_sleep": ScoreComponent(value=40), "restfulness": ScoreComponent(value=30)},
        )

    def test_rescore_retains_identity_and_replaces_components(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        original = self._score(user.id)
        counts, created = repo.bulk_upsert_provider_daily_scores(db, [original])
        db.commit()
        stored = created[0]
        created_at = stored.created_at

        assert (counts.inserted, counts.updated) == (1, 0)
        revised = self._score(user.id, 75).model_copy(update={"components": {"deep_sleep": ScoreComponent(value=80)}})
        counts, changed = repo.bulk_upsert_provider_daily_scores(db, [revised])

        assert (counts.inserted, counts.updated) == (0, 1)
        assert changed == [stored]
        assert stored.id == original.id
        assert stored.created_at == created_at
        assert stored.value == Decimal("75.000")
        assert stored.components == {"deep_sleep": {"value": 80, "qualifier": None}}
        assert db.query(HealthScore).filter(HealthScore.user_id == user.id).count() == 1

    def test_components_only_change_and_clear_are_updates(self, db: Session, repo: HealthScoreRepository) -> None:
        score = self._score(UserFactory().id)
        repo.bulk_upsert_provider_daily_scores(db, [score])
        replacement = score.model_copy(update={"components": {"deep_sleep": ScoreComponent(value=90)}})

        counts, changed = repo.bulk_upsert_provider_daily_scores(db, [replacement])

        assert (counts.inserted, counts.updated) == (0, 1)
        assert changed[0].value == 61
        assert changed[0].components == {"deep_sleep": {"value": 90, "qualifier": None}}

        counts, changed = repo.bulk_upsert_provider_daily_scores(db, [score.model_copy(update={"components": None})])

        assert (counts.inserted, counts.updated) == (0, 1)
        assert changed[0].components is None

    def test_identical_replay_writes_nothing(self, db: Session, repo: HealthScoreRepository) -> None:
        score = self._score(UserFactory().id)
        repo.bulk_upsert_provider_daily_scores(db, [score])
        db.commit()

        counts, changed = repo.bulk_upsert_provider_daily_scores(db, [score.model_copy(update={"id": uuid4()})])

        assert (counts.inserted, counts.updated) == (0, 0)
        assert changed == []

    def test_empty_batch_writes_nothing(self, db: Session, repo: HealthScoreRepository) -> None:
        counts, changed = repo.bulk_upsert_provider_daily_scores(db, [])

        assert (counts.inserted, counts.updated) == (0, 0)
        assert changed == []

    def test_duplicate_batch_key_uses_last_value(self, db: Session, repo: HealthScoreRepository) -> None:
        user_id = UserFactory().id
        first, last = self._score(user_id), self._score(user_id, 75)

        counts, changed = repo.bulk_upsert_provider_daily_scores(db, [first, last])

        assert (counts.inserted, counts.updated) == (1, 0)
        assert len(changed) == 1
        assert changed[0].id == last.id
        assert changed[0].value == 75

        first, last = self._score(user_id, 85), self._score(user_id, 61)
        counts, changed = repo.bulk_upsert_provider_daily_scores(db, [first, last])

        assert (counts.inserted, counts.updated) == (0, 1)
        assert len(changed) == 1
        assert changed[0].value == 61

    def test_unique_key_scopes_updates_and_counts(self, db: Session, repo: HealthScoreRepository) -> None:
        original = self._score(UserFactory().id)
        other_user = self._score(UserFactory().id)
        other_provider = original.model_copy(update={"id": uuid4(), "provider": ProviderName.WHOOP})
        other_category = original.model_copy(update={"id": uuid4(), "category": HealthScoreCategory.RECOVERY})
        other_day = original.model_copy(update={"id": uuid4(), "recorded_at": self.DAY + timedelta(days=1)})
        originals = [original, other_user, other_provider, other_category, other_day]
        counts, _ = repo.bulk_upsert_provider_daily_scores(db, originals)

        assert (counts.inserted, counts.updated) == (5, 0)
        new_day = original.model_copy(update={"id": uuid4(), "recorded_at": self.DAY + timedelta(days=2)})
        counts, changed = repo.bulk_upsert_provider_daily_scores(
            db, [original.model_copy(update={"id": uuid4(), "value": 75}), other_day, new_day]
        )

        assert (counts.inserted, counts.updated) == (1, 1)
        assert {row.id for row in changed} == {original.id, new_day.id}
        for score in originals[1:]:
            assert db.get(HealthScore, score.id).value == 61

    def test_caller_can_roll_back_rescore_and_insert(self, db: Session, repo: HealthScoreRepository) -> None:
        score = self._score(UserFactory().id)
        repo.bulk_upsert_provider_daily_scores(db, [score])
        db.commit()
        new_day = score.model_copy(update={"id": uuid4(), "recorded_at": self.DAY + timedelta(days=1)})

        repo.bulk_upsert_provider_daily_scores(db, [score.model_copy(update={"value": 75}), new_day])
        db.rollback()

        assert db.get(HealthScore, score.id).value == 61
        assert db.get(HealthScore, new_day.id) is None

    @pytest.mark.parametrize("invalid_kind", ["internal", "event_linked"])
    def test_rejects_other_score_kinds_before_writing(
        self, db: Session, repo: HealthScoreRepository, invalid_kind: str
    ) -> None:
        valid = self._score(UserFactory().id)
        invalid = valid.model_copy(
            update={"provider": ProviderName.INTERNAL} if invalid_kind == "internal" else {"event_record_id": uuid4()}
        )

        with pytest.raises(ValueError, match="Daily score upserts require provider scores without an event record"):
            repo.bulk_upsert_provider_daily_scores(db, [valid, invalid])

        assert db.get(HealthScore, valid.id) is None

    def test_scores_of_two_sources_at_the_same_time_are_upserted_apart(
        self, db: Session, repo: HealthScoreRepository
    ) -> None:
        """The key holds the data source: a rescore of one source's score leaves another source's alone."""
        user = UserFactory()
        ring, other_ring = (DataSourceFactory(user=user, provider=ProviderName.OURA) for _ in range(2))
        first = self._score(user.id).model_copy(update={"data_source_id": ring.id})
        second = self._score(user.id).model_copy(update={"id": uuid4(), "data_source_id": other_ring.id})
        without_source = self._score(user.id).model_copy(update={"id": uuid4()})

        counts, _ = repo.bulk_upsert_provider_daily_scores(db, [first, second, without_source])
        assert (counts.inserted, counts.updated) == (3, 0)

        counts, changed = repo.bulk_upsert_provider_daily_scores(
            db, [first.model_copy(update={"id": uuid4(), "value": 75})]
        )

        assert (counts.inserted, counts.updated) == (0, 1)
        assert [score.id for score in changed] == [first.id]
        assert db.get(HealthScore, second.id).value == 61
        assert db.get(HealthScore, without_source.id).value == 61
