"""Assemble Apple Health menstrual_flow samples into menstrual_cycle events.

HealthKit sends daily category samples (light/medium/heavy/…) plus optional
``HKMenstrualCycleStart`` metadata. That flag is stringified by the SDK and is
**not** stored on ``DataPointSeries`` (no metadata column). Cycle-start
boundaries therefore come from the in-flight sync payload; historical
re-grouping across syncs falls back to a gap > 1 day heuristic.

Limitation (v1): back-to-back periods with gap ≤ 1 day and no cycle-start flag
in the current payload will merge into one event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from logging import getLogger
from uuid import UUID, uuid4

from app.constants.series_types.sdk.category_types import (
    AppleCategoryType,
    is_menstrual_cycle_start,
)
from app.constants.series_types.sdk.metric_types import SDKMetricType
from app.database import DbSession
from app.models import DataPointSeries, DataSource, EventRecord
from app.schemas.enums import SeriesType, get_series_type_id
from app.schemas.model_crud.activities import EventRecordCreate, MenstrualCycleDetailCreate
from app.schemas.providers.mobile_sdk import SyncRequest as SDKSyncRequest
from app.services.apple.healthkit.device_resolution import extract_device_info
from app.services.event_record_service import event_record_service
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

_LOOKBACK_DAYS = 45
_EXTERNAL_ID_PREFIX = "apple-menstrual-"
_MENSTRUAL_RECORD_TYPES = {
    AppleCategoryType.MENSTRUAL_FLOW.value,
    SDKMetricType.APPLE_MENSTRUAL_FLOW.value,
}


@dataclass(frozen=True)
class _Period:
    start: date
    end: date  # inclusive last bleeding day

    @property
    def period_length(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def external_id(self) -> str:
        return f"{_EXTERNAL_ID_PREFIX}{self.start.isoformat()}"


def handle_menstrual_data(db_session: DbSession, request: SDKSyncRequest, user_id: str) -> int:
    """Rebuild menstrual_cycle events for the window touched by this sync.

    Upserts assembled periods by ``external_id``, then deletes orphan
    ``apple-menstrual-*`` events in the same window whose grouping no longer
    matches (merge/split/lookback clipping).

    Returns the number of periods upserted.
    """
    menstrual_records = [r for r in request.data.records if (r.type or "") in _MENSTRUAL_RECORD_TYPES]
    if not menstrual_records:
        return 0

    cycle_starts = {
        r.startDate.astimezone(timezone.utc).date() for r in menstrual_records if is_menstrual_cycle_start(r.metadata)
    }

    payload_dates = [r.startDate.astimezone(timezone.utc).date() for r in menstrual_records]
    window_start = min(payload_dates) - timedelta(days=_LOOKBACK_DAYS)
    window_end = max(payload_dates) + timedelta(days=_LOOKBACK_DAYS)

    flow_days = _load_flow_days(db_session, UUID(user_id), window_start, window_end)
    if not flow_days:
        return 0

    periods = _assemble_periods(sorted(flow_days), cycle_starts)
    if not periods:
        return 0

    # Device metadata from the first payload sample (best-effort).
    device_model, software_version, original_source_name = extract_device_info(menstrual_records[0].source)
    source_name = original_source_name or "Apple Health"
    provider = request.provider or "apple"
    user_uuid = UUID(user_id)
    now = datetime.now(timezone.utc)

    upserted = 0
    for idx, period in enumerate(periods):
        next_start = periods[idx + 1].start if idx + 1 < len(periods) else None
        cycle_length = (next_start - period.start).days if next_start is not None else None

        start_dt = datetime(period.start.year, period.start.month, period.start.day, tzinfo=timezone.utc)
        # Exclusive end of the last bleeding day → start of the following day.
        end_dt = datetime(period.end.year, period.end.month, period.end.day, tzinfo=timezone.utc) + timedelta(days=1)
        duration_seconds = int((end_dt - start_dt).total_seconds())

        existing = event_record_service.crud.get_by_external_id(
            db_session,
            user_uuid,
            period.external_id,
            provider=provider,
        )

        if existing is not None:
            existing.start_datetime = start_dt
            existing.end_datetime = end_dt
            existing.duration_seconds = duration_seconds
            existing.type = "menstruation"
            record_id = existing.id
            db_session.flush()
        else:
            record = EventRecordCreate(
                id=uuid4(),
                category="menstrual_cycle",
                type="menstruation",
                source_name=source_name,
                device_model=device_model,
                software_version=software_version,
                duration_seconds=duration_seconds,
                start_datetime=start_dt,
                end_datetime=end_dt,
                external_id=period.external_id,
                source=original_source_name,
                provider=provider,
                user_id=user_uuid,
            )
            created = event_record_service.crud.create_and_flush(db_session, record)
            record_id = created.id

        detail = MenstrualCycleDetailCreate(
            record_id=record_id,
            period_length=period.period_length,
            cycle_length=cycle_length,
            has_specified_period_length=True,
            has_specified_cycle_length=cycle_length is not None,
            last_updated_at=now,
        )
        event_record_service.bulk_create_details(db_session, [detail], detail_type="menstrual_cycle")
        upserted += 1

    orphans_deleted = _delete_orphan_periods(
        db_session,
        user_uuid,
        window_start=window_start,
        window_end=window_end,
        kept_external_ids={p.external_id for p in periods},
    )

    db_session.commit()

    log_structured(
        logger,
        "info",
        "Apple menstrual cycle assembly completed",
        provider=provider,
        action="apple_menstrual_assemble",
        user_id=user_id,
        periods_upserted=upserted,
        orphans_deleted=orphans_deleted,
        cycle_starts_from_payload=len(cycle_starts),
        flow_days=len(flow_days),
    )
    return upserted


def _delete_orphan_periods(
    db_session: DbSession,
    user_id: UUID,
    *,
    window_start: date,
    window_end: date,
    kept_external_ids: set[str],
) -> int:
    """Remove stale apple-menstrual-* events in the assembly window not in ``kept``."""
    start_dt = datetime(window_start.year, window_start.month, window_start.day, tzinfo=timezone.utc)
    end_dt = datetime(window_end.year, window_end.month, window_end.day, tzinfo=timezone.utc) + timedelta(days=1)

    existing = (
        db_session.query(EventRecord)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .filter(
            DataSource.user_id == user_id,
            EventRecord.category == "menstrual_cycle",
            EventRecord.external_id.like(f"{_EXTERNAL_ID_PREFIX}%"),
            EventRecord.start_datetime >= start_dt,
            EventRecord.start_datetime < end_dt,
        )
        .all()
    )

    deleted = 0
    for record in existing:
        if record.external_id in kept_external_ids:
            continue
        event_record_service.crud.delete_flush(db_session, record)
        deleted += 1
    return deleted


def _load_flow_days(
    db_session: DbSession,
    user_id: UUID,
    window_start: date,
    window_end: date,
) -> set[date]:
    """Return calendar days with active menstrual flow (OW scale 1–4) in the window."""
    series_id = get_series_type_id(SeriesType.menstrual_flow)
    start_dt = datetime(window_start.year, window_start.month, window_start.day, tzinfo=timezone.utc)
    end_dt = datetime(window_end.year, window_end.month, window_end.day, tzinfo=timezone.utc) + timedelta(days=1)

    rows = (
        db_session.query(DataPointSeries.recorded_at, DataPointSeries.value)
        .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
        .filter(
            DataSource.user_id == user_id,
            DataPointSeries.series_type_definition_id == series_id,
            DataPointSeries.recorded_at >= start_dt,
            DataPointSeries.recorded_at < end_dt,
            DataPointSeries.value >= 1,
            DataPointSeries.value <= 4,
        )
        .all()
    )

    return {recorded_at.astimezone(timezone.utc).date() for recorded_at, _ in rows}


def _assemble_periods(sorted_days: list[date], cycle_starts: set[date]) -> list[_Period]:
    """Group consecutive bleeding days into periods.

    A new period starts when the gap from the previous day is > 1, or when the
    day is marked as a cycle start in the current payload (and is not already
    the first day of the open period).
    """
    if not sorted_days:
        return []

    periods: list[_Period] = []
    period_start = sorted_days[0]
    prev = sorted_days[0]

    for day in sorted_days[1:]:
        gap = (day - prev).days
        starts_new = gap > 1 or (day in cycle_starts and day != period_start)
        if starts_new:
            periods.append(_Period(start=period_start, end=prev))
            period_start = day
        prev = day

    periods.append(_Period(start=period_start, end=prev))
    return periods
