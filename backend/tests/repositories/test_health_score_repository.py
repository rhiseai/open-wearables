"""
Tests for HealthScoreRepository.

Tests cover:
- Basic CRUD via CrudRepository base
- get_with_filters: category, provider, date range, user scoping
- get_latest_by_category
- get_latest_per_category
- bulk_create with on_conflict_do_nothing
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import HealthScore
from app.repositories.health_score_repository import HealthScoreRepository
from app.schemas.enums import HealthScoreCategory, ProviderName
from app.schemas.model_crud.activities import HealthScoreCreate, HealthScoreQueryParams
from app.schemas.model_crud.activities.health_score import ScoreComponent
from tests.factories import DataSourceFactory, HealthScoreFactory, UserFactory


@pytest.fixture
def repo() -> HealthScoreRepository:
    return HealthScoreRepository(HealthScore)


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


class TestHealthScoreRepositoryGetWithFilters:
    def test_filter_by_category(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        data_source = DataSourceFactory(user=user)
        HealthScoreFactory(data_source=data_source, category=HealthScoreCategory.SLEEP)
        HealthScoreFactory(data_source=data_source, category=HealthScoreCategory.SLEEP)
        HealthScoreFactory(data_source=data_source, category=HealthScoreCategory.RECOVERY)

        results, total = repo.get_with_filters(db, user.id, HealthScoreQueryParams(category=HealthScoreCategory.SLEEP))

        assert total == 2
        assert all(s.category == HealthScoreCategory.SLEEP for s in results)

    def test_filter_by_provider(self, db: Session, repo: HealthScoreRepository) -> None:
        user = UserFactory()
        data_source = DataSourceFactory(user=user)
        HealthScoreFactory(data_source=data_source, provider=ProviderName.GARMIN)
        HealthScoreFactory(data_source=data_source, provider=ProviderName.OURA)

        results, total = repo.get_with_filters(db, user.id, HealthScoreQueryParams(provider=ProviderName.GARMIN))

        assert total == 1
        assert results[0].provider == ProviderName.GARMIN

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
        )

        assert total == 2

    def test_scoped_to_user(self, db: Session, repo: HealthScoreRepository) -> None:
        user_a = UserFactory()
        user_b = UserFactory()
        HealthScoreFactory(data_source=DataSourceFactory(user=user_a))
        HealthScoreFactory(data_source=DataSourceFactory(user=user_b))

        results, total = repo.get_with_filters(db, user_a.id, HealthScoreQueryParams())

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
