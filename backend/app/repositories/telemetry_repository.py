from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, cast, distinct, func, literal, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import FromClause, LateralFromClause

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
from app.schemas.enums import EventCategory

_ACTIVE = UserConnection.status == ConnectionStatus.ACTIVE
# Users made by the seed data generator. A small set served by the partial index
# ix_user_is_seed, so the NOT IN filters below become a hashed lookup.
_SEED_USER_IDS = select(User.id).where(User.is_seed)
_REAL_CONNECTION = UserConnection.user_id.not_in(_SEED_USER_IDS)

# Values at or above this threshold all map to the existing "1M+" usage bucket.
TELEMETRY_COUNT_SATURATION = 1_000_001


class TelemetryRepository:
    """Data access for anonymous usage telemetry: the singleton state row and aggregate counts.

    Every count excludes seed users and their data, which are reported only through count_seed_users.
    """

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
        return db.scalar(select(func.count()).select_from(User).where(~User.is_seed)) or 0

    def count_seed_users(self, db: DbSession) -> int:
        return db.scalar(select(func.count()).select_from(User).where(User.is_seed)) or 0

    def count_users_with_active_connection(self, db: DbSession) -> int:
        return db.scalar(select(func.count(distinct(UserConnection.user_id))).where(_ACTIVE, _REAL_CONNECTION)) or 0

    def count_inactive_connections(self, db: DbSession) -> int:
        return db.scalar(select(func.count()).select_from(UserConnection).where(~_ACTIVE, _REAL_CONNECTION)) or 0

    def count_active_connections_by_provider(self, db: DbSession) -> dict[str, int]:
        rows = db.execute(
            select(UserConnection.provider, func.count())
            .where(_ACTIVE, _REAL_CONNECTION)
            .group_by(UserConnection.provider)
        )
        return {provider: count for provider, count in rows}

    def count_data_points_by_provider(self, db: DbSession) -> dict[str, int]:
        return self._count_by_provider(db, DataPointSeries)

    def count_events_by_provider(self, db: DbSession, category: EventCategory) -> dict[str, int]:
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

        Walk each provider's sources in increasing UUID order. Aggregate one
        source at a time with only the remaining row budget, stopping when the
        provider saturates or has no next source. Unlike joining limited rows,
        this avoids a nested-loop iteration for every fact. Source traversal and
        MVCC visibility checks still have costs; this is not a wall-clock bound.
        """
        first_sources = (
            select(DataSource.provider, DataSource.id.label("source_id"))
            .where(DataSource.user_id.not_in(_SEED_USER_IDS))
            .distinct(DataSource.provider)
            .order_by(DataSource.provider, DataSource.id)
            .subquery("telemetry_first_sources")
        )

        def count_source(
            source_id: ColumnElement[UUID],
            budget: int | ColumnElement[int],
            correlated: tuple[FromClause, ...],
            name: str,
        ) -> LateralFromClause:
            rows = select(literal(1)).select_from(model).where(model.data_source_id == source_id)
            if category is not None:
                rows = rows.where(EventRecord.category == category)
            limited = rows.limit(budget).correlate(*correlated).subquery(name + "_rows")
            return select(cast(func.count(), BigInteger).label("row_count")).select_from(limited).lateral(name)

        first_count = count_source(
            first_sources.c.source_id, TELEMETRY_COUNT_SATURATION, (first_sources,), "telemetry_first_count"
        )
        counts = (
            select(
                first_sources.c.provider,
                first_sources.c.source_id.label("last_id"),
                first_count.c.row_count.label("total"),
            )
            .select_from(first_sources)
            .join(first_count, true())
            .cte("telemetry_counts", recursive=True)
        )
        next_source = (
            select(DataSource.id.label("source_id"))
            .where(
                DataSource.provider == counts.c.provider,
                DataSource.id > counts.c.last_id,
                DataSource.user_id.not_in(_SEED_USER_IDS),
            )
            .order_by(DataSource.id)
            .limit(1)
            .correlate(counts)
            .lateral("telemetry_next_source")
        )
        next_count = count_source(
            next_source.c.source_id,
            literal(TELEMETRY_COUNT_SATURATION, type_=BigInteger) - counts.c.total,
            (counts, next_source),
            "telemetry_next_count",
        )
        counts = counts.union_all(
            select(
                counts.c.provider,
                next_source.c.source_id.label("last_id"),
                (counts.c.total + next_count.c.row_count).label("total"),
            )
            .select_from(counts)
            .join(next_source, true())
            .join(next_count, true())
            .where(counts.c.total < TELEMETRY_COUNT_SATURATION)
        )
        maximum = func.max(counts.c.total)
        query = select(counts.c.provider, maximum.label("row_count")).group_by(counts.c.provider).having(maximum > 0)
        return {
            provider.value if hasattr(provider, "value") else provider: count
            for provider, count in db.execute(query).all()
        }
