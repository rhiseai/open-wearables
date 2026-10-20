"""Tests for TelemetryRepository."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.repositories import telemetry_repository
from app.repositories.telemetry_repository import TelemetryRepository
from app.schemas.auth import ConnectionStatus
from app.services.telemetry_service import count_bucket
from tests.factories import (
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    UserConnectionFactory,
    UserFactory,
)

DAY = timedelta(days=1)


class TestClaimSend:
    def test_first_claim_succeeds_and_a_second_one_is_refused(self, db: Session) -> None:
        repo = TelemetryRepository()
        repo.get_or_create_state(db)

        first = repo.claim_send(db, DAY)
        second = repo.claim_send(db, DAY)

        assert first is not None
        assert second is None
        db.expire_all()
        assert repo.get_or_create_state(db).last_sent_at == first

    def test_claim_succeeds_once_the_interval_has_passed(self, db: Session) -> None:
        repo = TelemetryRepository()
        state = repo.get_or_create_state(db)
        state.last_sent_at = datetime.now(timezone.utc) - timedelta(days=2)
        db.commit()

        assert repo.claim_send(db, DAY) is not None

    def test_release_restores_the_previous_value(self, db: Session) -> None:
        repo = TelemetryRepository()
        previous = datetime.now(timezone.utc) - timedelta(days=2)
        state = repo.get_or_create_state(db)
        state.last_sent_at = previous
        db.commit()

        claimed_at = repo.claim_send(db, DAY)
        assert claimed_at is not None
        repo.release_claim(db, claimed_at, previous)

        db.expire_all()
        assert repo.get_or_create_state(db).last_sent_at == previous

    def test_release_leaves_a_newer_claim_alone(self, db: Session) -> None:
        repo = TelemetryRepository()
        repo.get_or_create_state(db)
        claimed_at = repo.claim_send(db, DAY)
        assert claimed_at is not None
        # Someone else claimed after us - our release must not undo their timestamp.
        newer = claimed_at + timedelta(seconds=5)
        state = repo.get_or_create_state(db)
        state.last_sent_at = newer
        db.commit()

        repo.release_claim(db, claimed_at, None)

        db.expire_all()
        assert repo.get_or_create_state(db).last_sent_at == newer


class TestCounts:
    def test_saturation_preserves_the_largest_published_bucket(self) -> None:
        assert count_bucket(telemetry_repository.TELEMETRY_COUNT_SATURATION - 1) == "100k-1M"
        assert count_bucket(telemetry_repository.TELEMETRY_COUNT_SATURATION) == "1M+"

    @pytest.mark.parametrize("total", [0, 10, 11, 17])
    def test_data_point_counts_saturate_per_provider_across_sources(self, db: Session, total: int) -> None:
        sources = [DataSourceFactory(provider="garmin"), DataSourceFactory(provider="garmin")]
        sparse = DataSourceFactory(provider="oura")
        DataSourceFactory(provider="apple")
        for index in range(total):
            DataPointSeriesFactory(data_source=sources[index % len(sources)])
        DataPointSeriesFactory(data_source=sparse)
        db.flush()

        with patch.object(telemetry_repository, "TELEMETRY_COUNT_SATURATION", 11):
            counts = TelemetryRepository().count_data_points_by_provider(db)

        assert counts == ({"garmin": min(total, 11), "oura": 1} if total else {"oura": 1})

    @pytest.mark.parametrize("category", ["workout", "sleep"])
    def test_event_counts_filter_category_before_saturation(self, db: Session, category: str) -> None:
        sources = [DataSourceFactory(provider="garmin"), DataSourceFactory(provider="garmin")]
        sparse = DataSourceFactory(provider="oura")
        DataSourceFactory(provider="apple")
        for source in sources:
            for _ in range(13):
                EventRecordFactory(data_source=source, category="menstrual_cycle")
            for _ in range(7):
                EventRecordFactory(data_source=source, category=category)
        EventRecordFactory(data_source=sparse, category=category)
        db.flush()

        with patch.object(telemetry_repository, "TELEMETRY_COUNT_SATURATION", 11):
            counts = TelemetryRepository().count_events_by_provider(db, category)

        assert counts == {"garmin": 11, "oura": 1}

    def test_counts_on_an_empty_instance_are_zero(self, db: Session) -> None:
        repo = TelemetryRepository()

        assert repo.count_users(db) == 0
        assert repo.count_users_with_active_connection(db) == 0
        assert repo.count_inactive_connections(db) == 0
        assert repo.count_active_connections_by_provider(db) == {}
        assert repo.count_events_by_provider(db, "workout") == {}

    def test_connection_and_event_counts(self, db: Session) -> None:
        repo = TelemetryRepository()
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", status=ConnectionStatus.ACTIVE)
        UserConnectionFactory(user=UserFactory(), provider="oura", status=ConnectionStatus.REVOKED)
        garmin = DataSourceFactory(user=user, provider="garmin")
        EventRecordFactory(data_source=garmin, category="workout")
        EventRecordFactory(data_source=garmin, category="workout")
        EventRecordFactory(data_source=garmin, category="sleep")
        db.flush()

        assert repo.count_users(db) == 2
        assert repo.count_users_with_active_connection(db) == 1
        assert repo.count_inactive_connections(db) == 1
        assert repo.count_active_connections_by_provider(db) == {"garmin": 1}
        assert repo.count_events_by_provider(db, "workout") == {"garmin": 2}
        assert repo.count_events_by_provider(db, "sleep") == {"garmin": 1}
