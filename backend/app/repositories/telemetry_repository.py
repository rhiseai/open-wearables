from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import distinct, func, literal, or_, select, true, update
from sqlalchemy.exc import IntegrityError

from app.database import DbSession
from app.models import (
    DataPointSeries,
    DataSource,
    EventRecord,
    ProviderSetting,
    TelemetryState,
    User,
    UserConnection,
)
from app.schemas.auth import ConnectionStatus

_ACTIVE = UserConnection.status == ConnectionStatus.ACTIVE

# Values at or above this threshold all map to the existing "1M+" usage bucket.
TELEMETRY_COUNT_SATURATION = 1_000_001


class TelemetryRepository:
    """Data access for anonymous usage telemetry: the singleton state row and aggregate counts."""

    def get_or_create_state(self, db: DbSession) -> TelemetryState:
        state = db.get(TelemetryState, 1)
        if state is not None:
            return state

        state = TelemetryState(
            id=1,
            instance_id=uuid4(),
            created_at=datetime.now(timezone.utc),
            last_sent_at=None,
        )
        db.add(state)
        try:
            db.commit()
        except IntegrityError:
            # Another worker created the row concurrently - use theirs.
            db.rollback()
            state = db.get(TelemetryState, 1)
            if state is None:  # pragma: no cover - only on DB failure
                raise
        return state

    def claim_send(self, db: DbSession, interval: timedelta) -> datetime | None:
        """Reserve the next delivery, or return None when the last one is within `interval`.

        A conditional UPDATE, committed before the request goes out, so that
        concurrent callers (several workers enqueueing the startup ping, or a
        startup ping racing the hourly beat) cannot all pass the check: the
        row lock makes the second UPDATE re-check against the first one's
        timestamp and match nothing.
        """
        now = datetime.now(timezone.utc)
        claimed = db.execute(
            update(TelemetryState)
            .where(
                TelemetryState.id == 1,
                or_(TelemetryState.last_sent_at.is_(None), TelemetryState.last_sent_at <= now - interval),
            )
            .values(last_sent_at=now)
            .returning(TelemetryState.id)
        ).scalar_one_or_none()
        db.commit()
        return now if claimed is not None else None

    def release_claim(self, db: DbSession, claimed_at: datetime, previous_sent_at: datetime | None) -> None:
        """Undo a claim, restoring the previous value only if no other caller has claimed since."""
        db.rollback()
        db.execute(
            update(TelemetryState)
            .where(TelemetryState.id == 1, TelemetryState.last_sent_at == claimed_at)
            .values(last_sent_at=previous_sent_at)
        )
        db.commit()

    def count_users(self, db: DbSession) -> int:
        return db.scalar(select(func.count()).select_from(User)) or 0

    def count_users_with_active_connection(self, db: DbSession) -> int:
        return db.scalar(select(func.count(distinct(UserConnection.user_id))).where(_ACTIVE)) or 0

    def count_inactive_connections(self, db: DbSession) -> int:
        return db.scalar(select(func.count()).select_from(UserConnection).where(~_ACTIVE)) or 0

    def count_active_connections_by_provider(self, db: DbSession) -> dict[str, int]:
        rows = db.execute(
            select(UserConnection.provider, func.count()).where(_ACTIVE).group_by(UserConnection.provider)
        )
        return {provider: count for provider, count in rows}

    def count_data_points_by_provider(self, db: DbSession) -> dict[str, int]:
        return self._count_by_provider(db, DataPointSeries)

    def count_events_by_provider(self, db: DbSession, category: str) -> dict[str, int]:
        return self._count_by_provider(db, EventRecord, category=category)

    def get_provider_settings(self, db: DbSession) -> list[ProviderSetting]:
        return list(db.scalars(select(ProviderSetting)).all())

    @staticmethod
    def _count_by_provider(
        db: DbSession,
        model: type[DataPointSeries] | type[EventRecord],
        category: str | None = None,
    ) -> dict[str, int]:
        """Count live facts up to the largest telemetry bucket, per provider.

        A limit inside the correlated source lookup retains an index-driven
        LATERAL lookup instead of joining and aggregating the entire fact table.
        The provider-level limit stops consuming rows once its bucket is known,
        including when those rows span several sources. Source enumeration and
        MVCC visibility checks still have costs; this is not a wall-clock bound.
        """
        providers = select(DataSource.provider).distinct().subquery("telemetry_providers")
        matching_rows = (
            select(literal(1)).select_from(model).where(model.data_source_id == DataSource.id).correlate(DataSource)
        )
        if category is not None:
            matching_rows = matching_rows.where(EventRecord.category == category)
        source_rows = matching_rows.limit(TELEMETRY_COUNT_SATURATION).lateral("telemetry_source_rows")
        limited_rows = (
            select(literal(1))
            .select_from(DataSource)
            .join(source_rows, true())
            .where(DataSource.provider == providers.c.provider)
            .limit(TELEMETRY_COUNT_SATURATION)
            .correlate(providers)
            .subquery("telemetry_limited_rows")
        )
        provider_count = (
            select(func.count().label("row_count")).select_from(limited_rows).lateral("telemetry_provider_count")
        )
        query = (
            select(providers.c.provider, provider_count.c.row_count)
            .select_from(providers)
            .join(provider_count, true())
            .where(provider_count.c.row_count > 0)
        )
        return {
            provider.value if hasattr(provider, "value") else provider: count
            for provider, count in db.execute(query).all()
        }
