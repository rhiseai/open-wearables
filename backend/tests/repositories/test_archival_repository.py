"""Tests for DataPointSeriesArchiveRepository."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import DataPointSeriesArchive
from app.repositories.archival_repository import DataPointSeriesArchiveRepository
from app.schemas.enums import AggregationMethod, ProviderName, SeriesType, get_series_type_id
from tests.factories import DataSourceFactory, SeriesTypeDefinitionFactory, UserFactory


class TestDailyActivityAggregatesFromArchive:
    def test_a_heart_rate_only_source_has_no_step_or_energy_sum(self, db: Session) -> None:
        """Archived heart rate without archived steps is a missing sum, not a sum of zero."""
        user = UserFactory()
        ring = DataSourceFactory(user=user, provider=ProviderName.OURA, source="oura", device_model=None)
        hr_type = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        day = datetime(2025, 12, 26, tzinfo=timezone.utc)
        db.add(
            DataPointSeriesArchive(
                id=uuid4(),
                data_source_id=ring.id,
                series_type_definition_id=hr_type.id,
                bucket_start_at=day,
                aggregation_type=AggregationMethod.AVG,
                value=Decimal("62"),
                sample_count=120,
            )
        )
        db.commit()

        rows = DataPointSeriesArchiveRepository().get_daily_activity_aggregates_from_archive(
            db,
            user.id,
            day,
            day + timedelta(days=1),
            [
                get_series_type_id(series_type)
                for series_type in (
                    SeriesType.steps,
                    SeriesType.active_energy,
                    SeriesType.basal_energy,
                    SeriesType.heart_rate,
                )
            ],
        )

        assert len(rows) == 1
        assert rows[0]["hr_avg"] == 62
        assert (rows[0]["steps_sum"], rows[0]["active_energy_sum"], rows[0]["basal_energy_sum"]) == (None, None, None)
