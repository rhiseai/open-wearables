from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import event as sa_event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Query, Session, SessionTransaction

from app.config import settings
from app.database import DbSession
from app.integrations.rollup_queue import mark_dirty_days
from app.models import (
    DataSource,
    DetailType,
    EventRecord,
    EventRecordAggregate,
    EventRecordDetail,
    HealthScore,
    MealDetails,
    MenstrualCycleDetails,
    SeriesSample,
    SleepDetails,
    WorkoutDetails,
)
from app.repositories import (
    AggregateRepository,
    DataPointSeriesRepository,
    DataSourceRepository,
    EventRecordDetailRepository,
    EventRecordRepository,
    HealthScoreRepository,
)
from app.repositories.data_point_series_repository import WorkoutWindow
from app.repositories.data_type_coverage_repository import data_type_coverage_repository
from app.repositories.event_record_repository import local_days
from app.schemas.data_type_coverage import CoverageSpan
from app.schemas.enums import (
    EventCategory,
    HealthScoreCategory,
    ProviderName,
    SeriesType,
    SourceSelection,
    get_series_type_unit,
)
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    EventRecordQueryParams,
    EventRecordResponse,
    EventRecordUpdate,
    MealDetailCreate,
    MenstrualCycleDetailCreate,
    SleepInclude,
    WorkoutInclude,
)
from app.schemas.model_crud.activities.zones import HRZones, PowerZones
from app.schemas.responses.activity import (
    Macros,
    Meal,
    MenstrualCycleRecord,
    NutrientValue,
    SleepSession,
    SleepStagesSummary,
    SleepTotals,
    Workout,
    WorkoutTotals,
)
from app.schemas.sync_status import DataTypeKind
from app.schemas.utils import (
    PaginatedResponse,
    Pagination,
    TimeseriesMetadata,
)
from app.schemas.utils import (
    SourceMetadata as DataSourceSchema,
)
from app.services.outgoing_webhooks import svix as svix_service
from app.services.outgoing_webhooks.events import (
    on_meal_created,
    on_menstrual_cycle_created,
    on_sleep_created,
    on_sleep_updated,
    on_workout_created,
)
from app.services.priority_service import priority_service
from app.services.scores.sleep_service import sleep_score_service
from app.services.services import AppService
from app.services.sleep_finality import final_record_ids
from app.services.sleep_nap_marking import SleepBlock, flags_own_naps, nap_ids
from app.services.sleep_reconciliation import merge_sleep_parts
from app.utils.context import current_sync_payload
from app.utils.conversion import as_dict_list, as_float, as_int, as_model, minutes_to_seconds
from app.utils.dates import local_date
from app.utils.pagination import encode_cursor
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class _RecordSnapshot:
    """Plain-Python snapshot of EventRecord fields used by the outgoing webhook.

    Captured before commit so that a later session.rollback() (which always
    expires every attached instance) cannot turn the after_commit listener
    into a lazy-load on a closed session.
    """

    id: UUID
    category: str | None
    type: str | None
    start_datetime: datetime
    end_datetime: datetime
    zone_offset: str | None
    duration_seconds: int | None


@dataclass(frozen=True)
class _DataSourceSnapshot:
    """Plain-Python snapshot of DataSource fields used by the outgoing webhook."""

    user_id: UUID
    provider: Any
    device_model: str | None
    source: str | None
    device_type: Any


def _snapshot_record(record: EventRecord) -> _RecordSnapshot:
    return _RecordSnapshot(
        id=record.id,
        category=record.category,
        type=record.type,
        start_datetime=record.start_datetime,
        end_datetime=record.end_datetime,
        zone_offset=record.zone_offset,
        duration_seconds=record.duration_seconds,
    )


def _snapshot_data_source(data_source: DataSource) -> _DataSourceSnapshot:
    return _DataSourceSnapshot(
        user_id=data_source.user_id,
        provider=data_source.provider,
        device_model=data_source.device_model,
        source=data_source.source,
        device_type=data_source.device_type,
    )


def _meal_summary(nutrients: dict[SeriesType, float]) -> tuple[float | None, Macros | None, float | None]:
    """Calories, macros (None when none recorded) and water for a meal, as /events/meals and meal.created show them."""
    macros = Macros(
        protein_g=nutrients.get(SeriesType.dietary_protein),
        carbohydrates_g=nutrients.get(SeriesType.dietary_carbohydrates),
        fat_g=nutrients.get(SeriesType.dietary_fat_total),
        fiber_g=nutrients.get(SeriesType.dietary_fiber),
    )
    has_macros = any(v is not None for v in macros.model_dump().values())
    return (
        nutrients.get(SeriesType.dietary_energy_consumed),
        macros if has_macros else None,
        nutrients.get(SeriesType.hydration),
    )


def _nutrient_values(nutrients: dict[SeriesType, float]) -> dict[str, NutrientValue]:
    return {t.value: NutrientValue(value=v, unit=get_series_type_unit(t)) for t, v in nutrients.items()}


def pace_sec_per_km(distance_meters: float | None, seconds: int | None) -> float | None:
    """Average pace in seconds per kilometre.

    Derived from distance and time rather than ``average_speed``: every provider stores m/s now,
    but a pace from the workout's own distance and time needs no speed at all.
    """
    if distance_meters is None or seconds is None or distance_meters <= 0 or seconds <= 0:
        return None
    return round(seconds / (distance_meters / 1000), 1)


def _first_number(*values: Decimal | None) -> float | None:
    """The first value that is set, so a provider's 0 kcal is kept rather than skipped."""
    return next((float(value) for value in values if value is not None), None)


class EventRecordService(
    AppService[EventRecordRepository, EventRecord, EventRecordCreate, EventRecordUpdate],
):
    """Service coordinating CRUD access for unified health records."""

    def __init__(self, **kwargs):
        super().__init__(crud_model=EventRecordRepository, model=EventRecord, **kwargs)
        self.event_record_detail_repo = EventRecordDetailRepository(EventRecordDetail)
        self.aggregate_repo = AggregateRepository(EventRecordAggregate)
        self.data_source_repo = DataSourceRepository()
        self.data_point_series_repo = DataPointSeriesRepository(SeriesSample)
        self.health_score_repo = HealthScoreRepository(HealthScore)

    def _resolve_avg_hr(
        self,
        db_session: DbSession,
        records: list[EventRecord],
        statistics: dict[UUID, dict[str, Decimal | None]],
    ) -> dict[UUID, int | None]:
        """Return avg HR for each workout record.

        Uses the workout's heart rate aggregate (the provider's, else ours from its own samples) where
        available, falls back to a single batch query against series_sample of the other sources of the
        same writer.
        """
        result: dict[UUID, int | None] = {}
        missing = []

        for r in records:
            average = statistics.get(r.id, {}).get("heart_rate_avg")
            if average is not None:
                # Half up, as the SQL fallback below rounds: 72.5 is 73 either way.
                result[r.id] = int(Decimal(average).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            else:
                result[r.id] = None
                missing.append(r)

        if missing:
            siblings = self.data_source_repo.get_writer_siblings(db_session, {r.data_source_id for r in missing})
            windows = [
                WorkoutWindow(
                    r.id, r.data_source_id, r.start_datetime, r.end_datetime, siblings.get(r.data_source_id, ())
                )
                for r in missing
            ]
            result.update(self.data_point_series_repo.get_avg_hr_for_workout_batch(db_session, windows))

        return result

    def _build_response(
        self,
        record: EventRecord,
        data_source: DataSource,
    ) -> EventRecordResponse:
        return EventRecordResponse(
            id=record.id,
            external_id=record.external_id,
            category=record.category,
            type=record.type,
            source_name=record.source_name,
            duration_seconds=record.duration_seconds,
            start_datetime=record.start_datetime,
            end_datetime=record.end_datetime,
            zone_offset=record.zone_offset,
            data_source_id=record.data_source_id,
            user_id=data_source.user_id,
            source=data_source.source,
        )

    def create_detail(
        self,
        db_session: DbSession,
        detail: EventRecordDetailCreate,
        detail_type: DetailType = EventCategory.WORKOUT,
    ) -> EventRecordDetail:
        result = self.event_record_detail_repo.create(db_session, detail, detail_type=detail_type)
        # event_record_detail_repo.create commits internally, so data is already persisted.
        record = db_session.get(EventRecord, detail.record_id)
        if record is not None and record.data_source_id is not None:
            data_source = self.data_source_repo.get(db_session, record.data_source_id)
            if data_source is not None:
                # Snapshot ORM fields into plain dataclasses BEFORE registering the
                # listener.  Any rollback that happens later in the same session
                # (e.g. an IntegrityError on a duplicate workout during a historical
                # sync) expires every attached instance, regardless of
                # expire_on_commit, and the after_commit listener would then trip
                # a lazy-load on a session that is already in 'committed' state.
                _record_snap = _snapshot_record(record)
                _data_source_snap = _snapshot_data_source(data_source)
                _detail = detail

                # The repository has already committed, so dispatch now. Registering an
                # after_commit listener at this point would wait for an unrelated future
                # transaction and can silently lose the event when no second commit occurs.
                self._emit_event_record_webhook(_record_snap, _data_source_snap, _detail, current_sync_payload())

        return result

    @staticmethod
    def _to_local(dt: datetime, zone_offset: str | None) -> datetime:
        """Shift a datetime into the session's local zone (mirrors SQL logic in fill task)."""
        dt = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        if zone_offset is None:
            # PostgreSQL renders timestamptz values in the connection's timezone.
            # A missing wearable offset means UTC, not the database/session timezone.
            return dt.astimezone(timezone.utc)
        sign = 1 if zone_offset[0] == "+" else -1
        hours, minutes = int(zone_offset[1:3]), int(zone_offset[4:6])
        return dt.astimezone(timezone(timedelta(hours=sign * hours, minutes=sign * minutes)))

    @classmethod
    def _local_sleep_date(cls, start_datetime: datetime, zone_offset: str | None) -> date:
        """Return the local calendar date of a sleep session start."""
        return cls._to_local(start_datetime, zone_offset).date()

    def _mark_naps(
        self, db_session: DbSession, data_source_id: UUID, wake_dates: set[date], *, keep_source_naps: bool
    ) -> set[UUID]:
        """Re-mark which of one source's sessions on each wake date are naps (RHISE-7780).

        The day is decided again on every write, so the main sleep can move to a
        later, longer session. Days are keyed by the local date a session ends,
        as the daily sleep summary is. Returns the ids marked as naps.

        A source with no nap flag of its own (``flags_own_naps`` is False) is
        marked both ways. With ``keep_source_naps`` a nap the source flagged
        stays a nap and takes no part in choosing the main sleep, and the rule
        only adds naps: WHOOP, for one, sends a short sleep before the night as
        an ordinary sleep and shows it as a nap in its own app.
        """
        if not wake_dates:
            return set()
        records = self.crud.get_sleep_sessions_ending_between(
            db_session,
            data_source_id,
            datetime.combine(min(wake_dates) - timedelta(days=1), time.min, tzinfo=timezone.utc),
            datetime.combine(max(wake_dates) + timedelta(days=2), time.min, tzinfo=timezone.utc),
        )
        naps: set[UUID] = set()
        for wake_date in wake_dates:
            day = [
                (record, detail)
                for record in records
                if (detail := record.sleep_detail) is not None
                and self._to_local(record.end_datetime, record.zone_offset).date() == wake_date
                and not (keep_source_naps and detail.is_nap)
            ]
            day_naps = nap_ids(
                [
                    SleepBlock(
                        id=record.id,
                        start=record.start_datetime,
                        end=record.end_datetime,
                        sleep_minutes=float(
                            detail.sleep_total_duration_minutes
                            if detail.sleep_total_duration_minutes is not None
                            else (record.duration_seconds or 0) / 60
                        ),
                    )
                    for record, detail in day
                ]
            )
            for record, detail in day:
                if bool(detail.is_nap) != (record.id in day_naps):
                    detail.is_nap = record.id in day_naps
            naps |= day_naps
        db_session.flush()
        return naps

    def _widen_sleep_coverage(self, db_session: DbSession, session: EventRecord) -> None:
        """Widen the provider's sleep coverage to a session re-sent over a longer window.

        A shorter one is not narrowed here: that recount takes the global coverage lock, too much for every webhook.
        """
        data_source = self.data_source_repo.get(db_session, session.data_source_id)
        if data_source is None:
            return
        span = CoverageSpan(
            data_source.user_id,
            data_source.provider,
            EventCategory.SLEEP.value,
            DataTypeKind.EVENT,
            session.start_datetime,
            session.end_datetime,
        )
        data_type_coverage_repository.record(db_session, [span])

    @staticmethod
    def _source_flags_own_naps(db_session: DbSession, data_source_id: UUID) -> bool:
        """Whether the data source's provider sends a nap flag of its own (``flags_own_naps``).

        The source a record is filed under decides, not the provider a payload names: Whoop's and Sensor Bio's
        records name none.
        """
        return flags_own_naps(db_session.get_one(DataSource, data_source_id).provider)

    def _with_marked_naps(
        self,
        db_session: DbSession,
        survivor: EventRecord,
        detail: EventRecordDetailCreate,
        wake_dates: set[date],
        *,
        keep_source_naps: bool,
    ) -> EventRecordDetailCreate:
        """Mark the source's naps on the written dates; the returned detail carries the survivor's flag."""
        naps = self._mark_naps(db_session, survivor.data_source_id, wake_dates, keep_source_naps=keep_source_naps)
        is_nap = survivor.id in naps or (keep_source_naps and bool(detail.is_nap))
        return detail.model_copy(update={"is_nap": is_nap})

    def _recompute_sleep_scores(
        self,
        db_session: DbSession,
        user_id: UUID,
        sleep_dates: set[date],
    ) -> None:
        """Delete existing internal sleep scores for each date and recompute them immediately.

        Accepts multiple dates so callers can cover both the old and new local
        date when a session shifts across midnight.  The session data has already
        been flushed, so sleep_score_service sees up-to-date rows within the
        same transaction.

        Scores per session rather than per date, matching fill_missing_sleep_scores.
        The two writers have to agree: recorded_at is what the unique constraint keys
        on, so a second convention here produces a duplicate score instead of
        replacing the existing one.
        """
        if not settings.ow_scores_enabled:
            return

        # Widened by a day either side so a session whose local start lands on a target
        # date is still inside the window whatever its zone offset.
        window_start = datetime.combine(min(sleep_dates), time.min, tzinfo=timezone.utc) - timedelta(days=1)
        window_end = datetime.combine(max(sleep_dates), time.min, tzinfo=timezone.utc) + timedelta(days=2)

        scorable = [
            (record, self._to_local(record.end_datetime, record.zone_offset))
            for record, details in self.crud.get_sleep_records_with_details(
                db_session, user_id, window_start, window_end
            )
            if details is not None and not details.is_nap and details.sleep_total_duration_minutes
        ]

        # A session running past midnight is scored on the day it ends, so clear the wake
        # dates of the sessions the caller named as well as the start dates themselves.
        stale_dates = sleep_dates | {
            local_end.date()
            for record, local_end in scorable
            if self._local_sleep_date(record.start_datetime, record.zone_offset) in sleep_dates
        }

        # Rebuild every session scored on a cleared date, not just the caller's — an
        # earlier session ending that morning is scored there too and would otherwise
        # be deleted without being rewritten.
        sessions = [
            (record.id, record.data_source_id, local_end)
            for record, local_end in scorable
            if local_end.date() in stale_dates
        ]

        for d in stale_dates:
            self.health_score_repo.delete_for_user_date(db_session, user_id, d, HealthScoreCategory.SLEEP)

        creators = sleep_score_service.build_internal_sleep_scores(db_session, user_id, sessions)
        if creators:
            self.health_score_repo.bulk_create(db_session, creators)

    def mark_sleep_naps_after_write(self, db_session: DbSession, user_id: UUID, record: EventRecord) -> None:
        """Mark the naps of a stored session's wake date and rescore it, then commit.

        For Sensor Bio, whose sleep is stored by ``create`` and ``create_detail`` rather than by
        ``create_or_merge_sleep`` (which marks naps inside its own write). The sleep is already committed: on a
        database error only the marking and the rescore are undone, and the database session stays usable for
        the caller's next write. The day is marked and rescored again by the next write of a session of that
        source that ends on it.
        """
        record_id, data_source_id = record.id, record.data_source_id
        wake_date = local_date(record.end_datetime, record.zone_offset)
        try:
            with db_session.begin_nested():
                self.lock_sleep_user(db_session, user_id)
                keep_source_naps = self._source_flags_own_naps(db_session, data_source_id)
                self._mark_naps(db_session, data_source_id, {wake_date}, keep_source_naps=keep_source_naps)
                self._recompute_sleep_scores(db_session, user_id, {wake_date})
        except SQLAlchemyError as e:
            logger.error(
                "Nap marking failed after sleep write", exc_info=e, record_id=str(record_id), user_id=str(user_id)
            )
        db_session.commit()

    def claim_sleep_stored_without_id(self, db_session: DbSession, user_id: UUID, record: EventRecordCreate) -> None:
        """Give the record's external_id to the overlapping night of its provider stored without one."""
        self.crud.claim_sleep_stored_without_id(db_session, user_id, record)

    def find_adjacent_sleep_record(
        self,
        db_session: DbSession,
        user_id: UUID,
        start_time: datetime,
        end_time: datetime,
        threshold_minutes: int,
        source: str | None = None,
        provider: str | None = None,
        source_app_id: str | None = None,
    ) -> EventRecord | None:
        """Find an existing sleep session adjacent to [start_time, end_time]."""
        return self.crud.find_adjacent_sleep_record(
            db_session,
            user_id,
            start_time,
            end_time,
            threshold_minutes,
            source=source,
            provider=provider,
            source_app_id=source_app_id,
        )

    def upsert_meals(
        self,
        db_session: DbSession,
        meals: list[tuple[EventRecordCreate, MealDetailCreate]],
    ) -> int:
        """Insert or refresh meals with their details; webhooks only for new ones. Returns the number inserted."""
        stored = self.crud.bulk_upsert_meals(db_session, [record for record, _ in meals])
        inserted: list[EventRecordDetailCreate] = []
        refreshed: list[EventRecordDetailCreate] = []
        for record, detail in meals:
            if record.id not in stored:
                continue
            record_id, is_inserted = stored[record.id]
            (inserted if is_inserted else refreshed).append(detail.model_copy(update={"record_id": record_id}))

        if inserted:
            self.bulk_create_details(db_session, inserted, detail_type=EventCategory.MEAL)
        if refreshed:
            self.event_record_detail_repo.bulk_create(db_session, refreshed, detail_type=EventCategory.MEAL)
        return len(inserted)

    def lock_sleep_user(self, db_session: DbSession, user_id: UUID) -> None:
        """Hold the shared sleep/score writer lock until the caller finishes its transaction."""
        self.crud.lock_sleep_user(db_session, user_id)

    @staticmethod
    def _sleep_detail_snapshot(record: EventRecord) -> EventRecordDetailCreate:
        detail = record.sleep_detail
        values = (
            {
                name: getattr(detail, name)
                for name in (
                    "sleep_total_duration_minutes",
                    "sleep_time_in_bed_minutes",
                    "sleep_efficiency_score",
                    "sleep_deep_minutes",
                    "sleep_rem_minutes",
                    "sleep_light_minutes",
                    "sleep_awake_minutes",
                    "is_nap",
                    "sleep_stages",
                    "provider_state",
                )
            }
            if detail
            else {}
        )
        return EventRecordDetailCreate(record_id=record.id, **values)

    def create_or_merge_sleep(
        self,
        db_session: DbSession,
        user_id: UUID,
        record: EventRecordCreate,
        detail: EventRecordDetailCreate,
        threshold_minutes: int,
        *,
        commit: bool = True,
        merge_adjacent: bool = True,
    ) -> EventRecord:
        """Reconcile one writer's complete adjacent sleep component atomically.

        A same-ID cloud session is an authoritative replacement; Apple IDs name
        samples, so Apple batches accumulate without shrinking a stored night.
        Existing row identity survives merges. Absorbed rows are deleted and
        flushed before expanding that row onto an occupied unique time window.
        ``commit=False`` leaves transaction ownership to the SDK batch caller.
        Webhooks contain snapshots and are dispatched only on the outer commit.
        Without ``merge_adjacent`` only the session stored under the record's
        external_id and sessions overlapping it join; a neighbour within the
        threshold stays apart (Polar and Ultrahuman: another id is another night).
        """
        try:
            self.lock_sleep_user(db_session, user_id)
            result, action, final_detail = self._create_or_merge_sleep_inner(
                db_session, user_id, record, detail, threshold_minutes, merge_adjacent
            )
            if action is not None:
                self._queue_sleep_webhook(db_session, user_id, record, result, action, final_detail)
            if commit:
                db_session.commit()
            else:
                db_session.flush()
            return result
        except Exception:
            if commit:
                db_session.rollback()
            raise

    def _queue_sleep_webhook(
        self,
        db_session: DbSession,
        user_id: UUID,
        incoming: EventRecordCreate,
        result: EventRecord,
        action: str,
        detail: EventRecordDetailCreate,
        *,
        revision_token: str | None = None,
    ) -> None:
        """Queue only plain values; savepoint commits and rollbacks never publish them."""
        device_type = None
        if svix_service.is_enabled():
            data_source = self.data_source_repo.get(db_session, result.data_source_id)
            if data_source is not None:
                device_type = data_source.device_type
        # Null only when no stage is reported: a measured 0 is a stage (#1953)
        has_stages = any(
            minutes is not None
            for minutes in (
                detail.sleep_awake_minutes,
                detail.sleep_light_minutes,
                detail.sleep_deep_minutes,
                detail.sleep_rem_minutes,
            )
        )
        payload = {
            "record_id": result.id,
            "user_id": user_id,
            "provider": incoming.provider or incoming.source_name,
            "device": incoming.device_model,
            "start_time": result.start_datetime.isoformat(),
            "end_time": result.end_datetime.isoformat(),
            "zone_offset": result.zone_offset,
            "duration_seconds": result.duration_seconds,
            "efficiency_percent": float(detail.sleep_efficiency_score)
            if detail.sleep_efficiency_score is not None
            else None,
            "stages": {
                "awake_minutes": detail.sleep_awake_minutes,
                "light_minutes": detail.sleep_light_minutes,
                "deep_minutes": detail.sleep_deep_minutes,
                "rem_minutes": detail.sleep_rem_minutes,
            }
            if has_stages
            else None,
            "is_nap": detail.is_nap,
            "source_app": incoming.source,
            "device_type": device_type,
            "sleep_duration_seconds": (
                detail.sleep_total_duration_minutes * 60 if detail.sleep_total_duration_minutes is not None else None
            ),
            "sleep_stage_intervals": (
                [stage.model_dump(mode="json") for stage in detail.sleep_stages] if detail.sleep_stages else None
            ),
            # Read now: the after_commit listener may run outside the sync run's context.
            "sync": current_sync_payload(),
        }
        pending = db_session.info.setdefault("sleep_webhooks", {})
        previous = pending.get(result.id)
        if revision_token is None and previous:
            revision_token = previous[1].get("revision_token")
        if revision_token is not None:
            payload["revision_token"] = revision_token
        pending[result.id] = (previous[0] if previous and previous[0] == "created" else action, payload)
        if db_session.info.get("sleep_webhook_listeners"):
            return
        db_session.info["sleep_webhook_listeners"] = True

        @sa_event.listens_for(db_session, "after_commit")
        def _dispatch_sleep_webhooks(session: Session) -> None:
            if session.in_nested_transaction():
                return
            for event_action, values in session.info.pop("sleep_webhooks", {}).values():
                emit = on_sleep_updated if event_action == "updated" else on_sleep_created
                emit(**values)

        @sa_event.listens_for(db_session, "after_soft_rollback")
        def _discard_sleep_webhooks(session: Session, transaction: SessionTransaction) -> None:
            if not transaction.nested:
                session.info.pop("sleep_webhooks", None)

    def queue_sleep_score_updates(
        self, db_session: DbSession, user_id: UUID, provider: ProviderName, wake_dates: set[date]
    ) -> None:
        """Notify consumers of changed daily scores once their transaction commits.

        The caller holds the shared sleep writer lock while updating the scores.
        A score can arrive before its session; that session's later create event
        will make the already-stored score visible without a synthetic session.
        """
        for record, source in self.crud.get_main_sleep_by_wake_dates(db_session, user_id, provider, wake_dates):
            incoming = EventRecordCreate(
                id=record.id,
                user_id=user_id,
                data_source_id=source.id,
                provider=provider,
                source_name=source.source or provider,
                source=source.source,
                device_model=source.device_model,
                category=EventCategory.SLEEP,
                start_datetime=record.start_datetime,
                end_datetime=record.end_datetime,
            )
            self._queue_sleep_webhook(
                db_session,
                user_id,
                incoming,
                record,
                "updated",
                self._sleep_detail_snapshot(record),
                revision_token=uuid4().hex,
            )

    def _create_or_merge_sleep_inner(
        self,
        db_session: DbSession,
        user_id: UUID,
        record: EventRecordCreate,
        detail: EventRecordDetailCreate,
        threshold_minutes: int,
        merge_adjacent: bool = True,
    ) -> tuple[EventRecord, str | None, EventRecordDetailCreate]:
        data_source_id = self.crud.resolve_sleep_data_source(db_session, record)
        record = record.model_copy(update={"data_source_id": data_source_id})
        authoritative = record.provider != ProviderName.APPLE and record.external_id is not None
        own_nap_flag = self._source_flags_own_naps(db_session, data_source_id)
        candidates = self.crud.find_sleep_merge_candidates(
            db_session,
            data_source_id,
            record.start_datetime,
            record.end_datetime,
            threshold_minutes,
            record.external_id,
            replace_own_window=authoritative,
            # A source with no nap flag of its own sends every session as main sleep, and a stored nap flag of
            # its is the rule's, decided again after this write: it keeps no session from joining another.
            is_nap=bool(detail.is_nap) if own_nap_flag else None,
            merge_adjacent=merge_adjacent,
        )
        own = [
            candidate
            for candidate in candidates
            if record.external_id is not None and candidate.external_id == record.external_id
        ]
        survivor = min(own or candidates, key=lambda candidate: (candidate.start_datetime, candidate.id), default=None)
        old_dates = {
            self._to_local(boundary, candidate.zone_offset).date()
            for candidate in candidates
            for boundary in (candidate.start_datetime, candidate.end_datetime)
        }
        parts = [
            (candidate.start_datetime, candidate.end_datetime, self._sleep_detail_snapshot(candidate))
            for candidate in candidates
            if not (authoritative and candidate in own)
        ]
        parts.append((record.start_datetime, record.end_datetime, detail))
        start, end, final_detail = merge_sleep_parts(parts)
        # The state the provider sent last stands (#2036), as it would in a replaced detail.
        final_detail = final_detail.model_copy(update={"provider_state": detail.provider_state})
        if survivor is None:
            creation = record.model_copy(
                update={
                    "start_datetime": start,
                    "end_datetime": end,
                    "duration_seconds": int((end - start).total_seconds()),
                }
            )
            survivor = self.crud.create_and_flush(db_session, creation)
            if survivor.category != "sleep" or survivor.type != "sleep_session":
                raise ValueError("Sleep window is occupied by another event category")
            final_detail = final_detail.model_copy(update={"record_id": survivor.id})
            self.event_record_detail_repo.create_and_flush(db_session, final_detail, detail_type=EventCategory.SLEEP)
            dates = {self._to_local(boundary, record.zone_offset).date() for boundary in (start, end)}
            db_session.expire(survivor, ["sleep_detail"])
            final_detail = self._with_marked_naps(
                db_session, survivor, final_detail, dates, keep_source_naps=own_nap_flag
            )
            self._recompute_sleep_scores(db_session, user_id, dates)
            return survivor, "created", final_detail

        final_detail = final_detail.model_copy(update={"record_id": survivor.id})
        if not own_nap_flag and survivor.sleep_detail is not None:
            # The incoming session says nothing about naps: keep the stored flag until the day is
            # re-flagged, so a nap sent again unchanged is not rewritten and announced as updated.
            final_detail = final_detail.model_copy(update={"is_nap": survivor.sleep_detail.is_nap})
        if authoritative and survivor not in own:
            # Matched by its window, not its id: the stored session takes the delivery's id, so the next delivery
            # finds it wherever it moves (Google re-issues a session under a new name, #1909).
            survivor.external_id = record.external_id
        # Google Health sleep stored before it carried a type (#1853).
        survivor.type = "sleep_session"
        duplicates = [candidate for candidate in candidates if candidate.id != survivor.id]
        zone_offset = record.zone_offset if record.zone_offset is not None else survivor.zone_offset
        old_start, old_end, old_zone = survivor.start_datetime, survivor.end_datetime, survivor.zone_offset
        stored = (old_start, old_end, old_zone, self._sleep_detail_snapshot(survivor))
        if not duplicates and (start, end, zone_offset, final_detail) == stored:
            return survivor, None, final_detail

        # Free any target slot before the surviving row is expanded. Everything
        # stays in the same transaction so a later error restores every old row.
        self.crud.preserve_sleep_provider_scores(db_session, survivor, duplicates)
        for duplicate in duplicates:
            # An absorbed row's session values (the provider's lowest heart rate, our rollup) would go with
            # it; they move to the surviving night, where they combine with its own.
            self.aggregate_repo.move_record(db_session, duplicate.id, survivor.id)
        self.crud.delete_sleep_candidates(db_session, duplicates)
        survivor.start_datetime = start
        survivor.end_datetime = end
        survivor.zone_offset = zone_offset
        survivor.duration_seconds = int((end - start).total_seconds())
        if authoritative and survivor in own and end != old_end:
            # Its provider score sits at the old end, which the re-sent night moved (#2034).
            self.health_score_repo.drop_provider_sleep_score(db_session, survivor.data_source_id, old_end)
        db_session.flush()
        if (start, end, zone_offset) != (old_start, old_end, old_zone):
            self._widen_sleep_coverage(db_session, survivor)
            # Its session rows are rolled up over the window it has now, on the days it left and joined.
            mark_dirty_days(
                db_session,
                [
                    *local_days(survivor.data_source_id, old_start, old_end, old_zone),
                    *local_days(survivor.data_source_id, start, end, zone_offset),
                ],
            )
        self.event_record_detail_repo.delete_by_record_id(db_session, survivor.id, EventCategory.SLEEP)
        self.event_record_detail_repo.create_and_flush(db_session, final_detail, detail_type=EventCategory.SLEEP)
        db_session.expire(survivor, ["sleep_detail"])
        old_dates.update(self._to_local(boundary, zone_offset).date() for boundary in (start, end))
        final_detail = self._with_marked_naps(
            db_session, survivor, final_detail, old_dates, keep_source_naps=own_nap_flag
        )
        self._recompute_sleep_scores(db_session, user_id, old_dates)
        if not duplicates and (start, end, zone_offset, final_detail) == stored:
            # A source with a nap flag of its own sends a nap the rule made as main sleep, and the rule has
            # made it a nap again: the session's values are stored as they were, so nothing is announced.
            return survivor, None, final_detail
        logger.info(
            "Reconciled adjacent sleep records",
            provider=record.provider,
            action="sleep_records_reconciled",
            user_id=str(user_id),
            record_id=str(survivor.id),
            absorbed_record_ids=[str(candidate.id) for candidate in duplicates],
        )
        # Drop snapshots of rows absorbed by another merge in this transaction.
        for duplicate in duplicates:
            db_session.info.get("sleep_webhooks", {}).pop(duplicate.id, None)
        return survivor, "updated", final_detail

    @staticmethod
    def _emit_event_record_webhook(
        record: _RecordSnapshot,
        data_source: _DataSourceSnapshot,
        detail: EventRecordDetailCreate,
        sync: dict[str, str] | None = None,
    ) -> None:
        """Fire the appropriate outgoing webhook for a newly created event record.

        Operates on plain-Python snapshots — never on live ORM instances —
        because this runs from an after_commit listener where the session can
        already be in a 'committed' state and a stray lazy-load would crash.
        """
        if not svix_service.is_enabled():
            return
        category = (record.category or "").lower()
        provider = str(data_source.provider)
        device = data_source.device_model
        zone_offset = record.zone_offset
        match category:
            case EventCategory.SLEEP:
                eff = detail.sleep_efficiency_score
                has_stages = any(
                    minutes is not None
                    for minutes in (
                        detail.sleep_awake_minutes,
                        detail.sleep_light_minutes,
                        detail.sleep_deep_minutes,
                        detail.sleep_rem_minutes,
                    )
                )
                on_sleep_created(
                    record_id=record.id,
                    user_id=data_source.user_id,
                    provider=provider,
                    device=device,
                    start_time=record.start_datetime.isoformat(),
                    end_time=record.end_datetime.isoformat(),
                    zone_offset=zone_offset,
                    duration_seconds=record.duration_seconds,
                    efficiency_percent=float(eff) if eff is not None else None,
                    stages={
                        "awake_minutes": detail.sleep_awake_minutes,
                        "light_minutes": detail.sleep_light_minutes,
                        "deep_minutes": detail.sleep_deep_minutes,
                        "rem_minutes": detail.sleep_rem_minutes,
                    }
                    if has_stages
                    else None,
                    is_nap=detail.is_nap,
                    source_app=data_source.source,
                    device_type=data_source.device_type,
                    sleep_duration_seconds=(
                        detail.sleep_total_duration_minutes * 60
                        if detail.sleep_total_duration_minutes is not None
                        else None
                    ),
                    sleep_stage_intervals=(
                        [stage.model_dump(mode="json") for stage in detail.sleep_stages]
                        if detail.sleep_stages
                        else None
                    ),
                    sync=sync,
                )
            case EventCategory.MENSTRUAL_CYCLE:
                mcd = detail if isinstance(detail, MenstrualCycleDetailCreate) else None
                on_menstrual_cycle_created(
                    record_id=record.id,
                    user_id=data_source.user_id,
                    provider=provider,
                    device=device,
                    start_time=record.start_datetime.isoformat(),
                    end_time=record.end_datetime.isoformat(),
                    zone_offset=zone_offset,
                    current_phase_type=mcd.current_phase_type if mcd else None,
                    day_in_cycle=mcd.day_in_cycle if mcd else None,
                    cycle_length=mcd.cycle_length if mcd else None,
                    is_predicted_cycle=mcd.is_predicted_cycle if mcd else None,
                    pregnancy_snapshot=mcd.pregnancy_snapshot if mcd else None,
                    sync=sync,
                )
            case EventCategory.WORKOUT:
                avg_pace = pace_sec_per_km(
                    as_float(detail.distance),
                    detail.moving_time_seconds if detail.moving_time_seconds is not None else record.duration_seconds,
                )
                on_workout_created(
                    record_id=record.id,
                    user_id=data_source.user_id,
                    provider=provider,
                    device=device,
                    workout_type=record.type,
                    start_time=record.start_datetime.isoformat(),
                    end_time=record.end_datetime.isoformat(),
                    zone_offset=zone_offset,
                    duration_seconds=record.duration_seconds,
                    calories_kcal=float(detail.energy_burned) if detail.energy_burned is not None else None,
                    distance_meters=float(detail.distance) if detail.distance is not None else None,
                    avg_heart_rate_bpm=int(detail.heart_rate_avg) if detail.heart_rate_avg is not None else None,
                    max_heart_rate_bpm=int(detail.heart_rate_max) if detail.heart_rate_max is not None else None,
                    elevation_gain_meters=float(detail.total_elevation_gain)
                    if detail.total_elevation_gain is not None
                    else None,
                    avg_pace_sec_per_km=round(avg_pace) if avg_pace is not None else None,
                    sync=sync,
                )
            case EventCategory.MEAL if isinstance(detail, MealDetailCreate):
                calories_kcal, macros, water_ml = _meal_summary(detail.nutrients)
                on_meal_created(
                    record_id=record.id,
                    user_id=data_source.user_id,
                    provider=provider,
                    device=device,
                    start_time=record.start_datetime.isoformat(),
                    end_time=record.end_datetime.isoformat(),
                    zone_offset=zone_offset,
                    title=detail.title,
                    meal_type=detail.meal_type,
                    calories_kcal=calories_kcal,
                    macros=macros.model_dump() if macros else None,
                    water_ml=water_ml,
                    nutrients={code: value.model_dump() for code, value in _nutrient_values(detail.nutrients).items()},
                    sync=sync,
                )

    def bulk_create(
        self,
        db_session: DbSession,
        records: list[EventRecordCreate],
    ) -> dict[UUID, UUID]:
        """Bulk create event records with batch data source resolution."""
        # Webhooks are not emitted for bulk-created records; they fire from
        # bulk_create_details() when the matching details are inserted.
        return self.crud.bulk_create(db_session, records)

    def bulk_create_with_details(
        self,
        db_session: DbSession,
        items: list[tuple[EventRecordCreate, EventRecordDetailCreate]],
        detail_type: DetailType = EventCategory.WORKOUT,
    ) -> int:
        """Insert records and their details in batches. Returns how many records were stored, new or refreshed.

        Records matched on external_id refresh their details; only new ones fire webhooks. Caller commits.
        """
        stored = self.bulk_create(db_session, [record for record, _ in items])
        inserted = {record_id for record_id, stored_id in stored.items() if record_id == stored_id}
        details = [
            detail.model_copy(update={"record_id": stored[detail.record_id]})
            for _, detail in items
            if detail.record_id in stored
        ]
        self.bulk_create_details(db_session, details, detail_type=detail_type, created=inserted)
        return len(set(stored.values()))

    def bulk_create_details(
        self,
        db_session: DbSession,
        details: list[EventRecordDetailCreate],
        detail_type: DetailType = EventCategory.WORKOUT,
        created: set[UUID] | None = None,
    ) -> None:
        """Bulk create event record details and fire one webhook per detail on commit.

        With created given, only those records fire it; the rest are refreshes of stored records.
        """
        # Several deliveries of one workout in a batch resolve to one record; the last one's detail stands
        details = list({d.record_id: d for d in details}.values())
        self.event_record_detail_repo.bulk_create(db_session, details, detail_type=detail_type)

        if created is not None:
            details = [d for d in details if d.record_id in created]
        if not details or not svix_service.is_enabled():
            return

        record_ids = [d.record_id for d in details if d.record_id is not None]
        if not record_ids:
            return

        records = self.crud.get_by_ids(db_session, record_ids)
        records_by_id = {r.id: r for r in records}

        data_source_ids = {r.data_source_id for r in records if r.data_source_id is not None}
        data_sources = self.data_source_repo.get_by_ids(db_session, data_source_ids)
        data_sources_by_id = {ds.id: ds for ds in data_sources}

        # Snapshot ORM fields up front; see create_detail() for why this is
        # required (rollback expires every attached instance and would break
        # an after_commit listener that closed over ORM references).
        dispatches: list[tuple[_RecordSnapshot, _DataSourceSnapshot, EventRecordDetailCreate]] = []
        for detail in details:
            record = records_by_id.get(detail.record_id)
            if record is None or record.data_source_id is None:
                continue
            data_source = data_sources_by_id.get(record.data_source_id)
            if data_source is None:
                continue
            dispatches.append((_snapshot_record(record), _snapshot_data_source(data_source), detail))

        if not dispatches:
            return

        # Detach so expire_on_commit doesn't expire these; reading expired attributes
        # inside after_commit (committed session) would raise. Only loaded scalars are
        # read, and these objects are local (callers get only IDs), so detaching is safe.
        for record in records:
            db_session.expunge(record)
        for data_source in data_sources:
            db_session.expunge(data_source)

        sync = current_sync_payload()

        @sa_event.listens_for(db_session, "after_commit", once=True)
        def _dispatch_bulk_webhooks(session: DbSession) -> None:  # noqa: ARG001
            for record_snap, data_source_snap, detail in dispatches:
                self._emit_event_record_webhook(record_snap, data_source_snap, detail, sync)

    def _get_records_with_filters(
        self,
        db_session: DbSession,
        query_params: EventRecordQueryParams,
        user_id: str,
        restrict_to_record_ids: Query | None = None,
    ) -> tuple[list[tuple[EventRecord, DataSource]], int]:
        logger.debug("Fetching event records", user_id=user_id, filters=query_params.model_dump(mode="json"))

        records, total_count = self.crud.get_records_with_filters(
            db_session, query_params, user_id, restrict_to_record_ids=restrict_to_record_ids
        )

        logger.debug("Retrieved event records", user_id=user_id, count=len(records), total_count=total_count)

        return records, total_count

    def get_records_response(
        self,
        db_session: DbSession,
        query_params: EventRecordQueryParams,
        user_id: str,
    ) -> list[EventRecordResponse]:
        records, _ = self._get_records_with_filters(db_session, query_params, user_id)

        return [self._build_response(record, data_source) for record, data_source in records]

    def get_workout_types(self, db_session: DbSession, user_id: UUID) -> list[str]:
        """Workout types this user actually has, so a client can filter on real options only."""
        return self.crud.get_distinct_workout_types(db_session, user_id)

    def get_category_counts(self, db_session: DbSession) -> list[tuple[str, int]]:
        """Count event records grouped by category (cheap aggregate on a small table)."""
        return self.crud.get_category_counts(db_session)

    def _map_source(self, data_source: DataSource) -> DataSourceSchema:
        return DataSourceSchema(
            provider=data_source.provider or "unknown",
            source=data_source.source,
            device=data_source.device_model,
            device_type=data_source.device_type,
            device_id=data_source.device_id,
        )

    def get_workout_totals(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
        source_selection: SourceSelection = SourceSelection.ALL,
    ) -> WorkoutTotals:
        restrict = self._winning_session_ids(db_session, user_id, params, EventCategory.WORKOUT, source_selection)
        count, seconds, energy, distance = self.crud.get_workout_totals(
            db_session, params, str(user_id), restrict_to_record_ids=restrict
        )
        return WorkoutTotals(
            count=count,
            duration_seconds=seconds,
            calories_kcal=float(energy) if energy is not None else None,
            distance_meters=float(distance) if distance is not None else None,
        )

    def get_workouts(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
        include: Sequence[WorkoutInclude] = (),
        source_selection: SourceSelection = SourceSelection.ALL,
    ) -> PaginatedResponse[Workout]:
        params.category = EventCategory.WORKOUT
        with_zones = WorkoutInclude.ZONES in include
        with_segments = WorkoutInclude.SEGMENTS in include
        restrict = self._winning_session_ids(db_session, user_id, params, EventCategory.WORKOUT, source_selection)
        records, total_count = self._get_records_with_filters(
            db_session, params, str(user_id), restrict_to_record_ids=restrict
        )
        # Ensure total_count is always an int (not None)
        total_count = total_count if total_count is not None else 0

        limit = params.limit or 20
        has_more = len(records) > limit

        # Check if this is backward pagination
        is_backward = params.cursor and params.cursor.startswith("prev_")

        # Trim to limit
        if has_more:
            records = records[-limit:] if is_backward else records[:limit]

        # Generate cursors
        next_cursor = None
        previous_cursor = None

        if records:
            # Always generate next_cursor if has_more
            if has_more:
                last_record, _ = records[-1]
                next_cursor = encode_cursor(last_record.start_datetime, last_record.id, "next")

            # Generate previous_cursor only if:
            # 1. We used a cursor to get here (not the first page)
            # 2. There are more items before (for backward) OR we're doing forward navigation
            if params.cursor:
                # For backward navigation: only set previous_cursor if has_more
                # For forward navigation: always set previous_cursor
                if is_backward:
                    if has_more:
                        first_record, _ = records[0]
                        previous_cursor = encode_cursor(first_record.start_datetime, first_record.id, "prev")
                else:
                    first_record, _ = records[0]
                    previous_cursor = encode_cursor(first_record.start_datetime, first_record.id, "prev")

        statistics = self.crud.get_workout_statistics(db_session, [r.id for r, _ in records])
        computed_hr = self._resolve_avg_hr(db_session, [r for r, _ in records], statistics)

        data = []
        for record, data_source in records:
            details: WorkoutDetails | None = record.workout_detail
            stats = statistics.get(record.id, {})
            calories = _first_number(
                stats.get("provider_energy"), details.energy_burned if details else None, stats.get("own_energy")
            )
            moving_seconds = (
                details.moving_time_seconds
                if details and details.moving_time_seconds is not None
                else record.duration_seconds
            )
            workout = Workout(
                id=record.id,
                type=record.type or "unknown",
                name=details.label if details else None,
                start_time=record.start_datetime,
                end_time=record.end_datetime,
                zone_offset=record.zone_offset,
                duration_seconds=record.duration_seconds,
                source=self._map_source(data_source),
                entry_source=details.entry_source if details else None,
                intensity=details.intensity if details else None,
                calories_kcal=calories,
                distance_meters=as_float(stats.get("distance")),
                avg_heart_rate_bpm=computed_hr.get(record.id),
                max_heart_rate_bpm=as_int(stats.get("heart_rate_max")),
                avg_pace_sec_per_km=pace_sec_per_km(as_float(stats.get("distance")), moving_seconds),
                elevation_gain_meters=as_float(stats.get("total_elevation_gain")),
                heart_rate_min=as_int(stats.get("heart_rate_min")),
                steps_count=as_int(stats.get("steps_count")),
                average_speed=as_float(stats.get("average_speed")),
                max_speed=as_float(stats.get("max_speed")),
                average_cadence=as_float(stats.get("average_cadence")),
                average_watts=as_float(stats.get("average_watts")),
                max_watts=as_float(stats.get("max_watts")),
                average_met=as_float(stats.get("average_met")),
                moving_time_seconds=details.moving_time_seconds if details else None,
                elev_high=as_float(stats.get("elev_high")),
                elev_low=as_float(stats.get("elev_low")),
                hr_zones=as_model(HRZones, details.hr_zones) if details and with_zones else None,
                power_zones=as_model(PowerZones, details.power_zones) if details and with_zones else None,
                segments=as_dict_list(details.segments) if details and with_segments else None,
            )
            data.append(workout)

        return PaginatedResponse(
            data=data,
            pagination=Pagination(
                has_more=has_more,
                next_cursor=next_cursor,
                previous_cursor=previous_cursor,
                total_count=total_count,
            ),
            metadata=TimeseriesMetadata(
                sample_count=len(data),
                start_time=params.start_datetime,
                end_time=params.end_datetime,
            ),
        )

    def _winning_session_ids(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
        category: EventCategory,
        source_selection: SourceSelection,
    ) -> Query | None:
        """An inline subquery of the sessions no better-ranked source also recorded, None when every source shows."""
        # By value: a caller passing the plain string "per_session" takes this path too.
        if source_selection != SourceSelection.PER_SESSION:
            return None
        params = params.model_copy(update={"category": category})
        return self.crud.winning_session_record_ids(
            db_session, str(user_id), params, priority_service.get_source_ranking(db_session)
        )

    def get_sleep_totals(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
        source_selection: SourceSelection = SourceSelection.ALL,
    ) -> SleepTotals:
        restrict = self._winning_session_ids(db_session, user_id, params, EventCategory.SLEEP, source_selection)
        count, naps, asleep, in_bed, efficiency = self.crud.get_sleep_totals(
            db_session, params, str(user_id), restrict_to_record_ids=restrict
        )
        return SleepTotals(
            count=count,
            naps=naps,
            sleep_duration_seconds=asleep,
            time_in_bed_seconds=in_bed,
            avg_efficiency_percent=float(efficiency) if efficiency is not None else None,
        )

    def get_sleep_sessions(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
        source_selection: SourceSelection = SourceSelection.ALL,
        include: Sequence[SleepInclude] = (),
    ) -> PaginatedResponse[SleepSession]:
        params.category = EventCategory.SLEEP
        with_stages = SleepInclude.STAGES in include
        restrict_to_record_ids = self._winning_session_ids(
            db_session, user_id, params, EventCategory.SLEEP, source_selection
        )

        records, total_count = self._get_records_with_filters(
            db_session, params, str(user_id), restrict_to_record_ids=restrict_to_record_ids
        )
        # Ensure total_count is always an int (not None)
        total_count = total_count if total_count is not None else 0

        limit = params.limit or 20
        has_more = len(records) > limit

        # Check if this is backward pagination
        is_backward = params.cursor and params.cursor.startswith("prev_")

        # Trim to limit
        if has_more:
            records = records[-limit:] if is_backward else records[:limit]

        # Generate cursors
        next_cursor = None
        previous_cursor = None

        if records:
            # Always generate next_cursor if has_more
            if has_more:
                last_record, _ = records[-1]
                next_cursor = encode_cursor(last_record.start_datetime, last_record.id, "next")

            # Generate previous_cursor only if:
            # 1. We used a cursor to get here (not the first page)
            # 2. There are more items before (for backward) OR we're doing forward navigation
            if params.cursor:
                # For backward navigation: only set previous_cursor if has_more
                # For forward navigation: always set previous_cursor
                if is_backward:
                    if has_more:
                        first_record, _ = records[0]
                        previous_cursor = encode_cursor(first_record.start_datetime, first_record.id, "prev")
                else:
                    first_record, _ = records[0]
                    previous_cursor = encode_cursor(first_record.start_datetime, first_record.id, "prev")

        final_ids = final_record_ids(db_session, user_id, records, datetime.now(timezone.utc))
        data = []
        for record, data_source in records:
            details: SleepDetails | None = record.sleep_detail

            sleep_duration_seconds = minutes_to_seconds(details.sleep_total_duration_minutes) if details else None
            time_in_bed_seconds = minutes_to_seconds(details.sleep_time_in_bed_minutes) if details else None
            session = SleepSession(
                id=record.id,
                start_time=record.start_datetime,
                end_time=record.end_datetime,
                zone_offset=record.zone_offset,
                source=self._map_source(data_source).model_copy(update={"data_source_id": data_source.id}),
                duration_seconds=record.duration_seconds or 0,
                sleep_duration_seconds=sleep_duration_seconds,
                time_in_bed_seconds=time_in_bed_seconds,
                efficiency_percent=as_float(details.sleep_efficiency_score) if details else None,
                is_nap=details.is_nap if (details and details.is_nap is not None) else False,
                is_final=record.id in final_ids,
                sleep_stage_intervals=details.sleep_stages if details and with_stages else None,
                stages=SleepStagesSummary(
                    deep_minutes=details.sleep_deep_minutes,
                    light_minutes=details.sleep_light_minutes,
                    rem_minutes=details.sleep_rem_minutes,
                    awake_minutes=details.sleep_awake_minutes,
                )
                if details
                else None,
            )
            data.append(session)

        return PaginatedResponse(
            data=data,
            pagination=Pagination(
                has_more=has_more,
                next_cursor=next_cursor,
                previous_cursor=previous_cursor,
                total_count=total_count,
            ),
            metadata=TimeseriesMetadata(
                sample_count=len(data),
                start_time=params.start_datetime,
                end_time=params.end_datetime,
            ),
        )

    def get_menstrual_cycles(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
    ) -> PaginatedResponse[MenstrualCycleRecord]:
        params.category = EventCategory.MENSTRUAL_CYCLE
        # Cycles can end in the future (predicted end of cycle), so filtering by
        # end_datetime would exclude current/upcoming cycles. Filter by start_datetime only.
        params.end_datetime = None
        records, total_count = self._get_records_with_filters(db_session, params, str(user_id))
        total_count = total_count if total_count is not None else 0

        limit = params.limit or 20
        has_more = len(records) > limit
        is_backward = params.cursor and params.cursor.startswith("prev_")

        if has_more:
            records = records[-limit:] if is_backward else records[:limit]

        next_cursor = None
        previous_cursor = None

        if records:
            if has_more:
                last_record, _ = records[-1]
                next_cursor = encode_cursor(last_record.start_datetime, last_record.id, "next")
            if params.cursor:
                if is_backward:
                    if has_more:
                        first_record, _ = records[0]
                        previous_cursor = encode_cursor(first_record.start_datetime, first_record.id, "prev")
                else:
                    first_record, _ = records[0]
                    previous_cursor = encode_cursor(first_record.start_datetime, first_record.id, "prev")

        data = []
        for record, data_source in records:
            details: MenstrualCycleDetails | None = record.menstrual_cycle_detail
            data.append(
                MenstrualCycleRecord(
                    id=record.id,
                    start_time=record.start_datetime,
                    end_time=record.end_datetime,
                    zone_offset=record.zone_offset,
                    source=self._map_source(data_source),
                    current_phase=details.current_phase if details else None,
                    current_phase_type=details.current_phase_type if details else None,
                    day_in_cycle=details.day_in_cycle if details else None,
                    cycle_length=details.cycle_length if details else None,
                    predicted_cycle_length=details.predicted_cycle_length if details else None,
                    is_predicted_cycle=details.is_predicted_cycle if details else None,
                    period_length=details.period_length if details else None,
                    length_of_current_phase=details.length_of_current_phase if details else None,
                    days_until_next_phase=details.days_until_next_phase if details else None,
                    fertile_window_start=details.fertile_window_start if details else None,
                    length_of_fertile_window=details.length_of_fertile_window if details else None,
                    last_updated_at=details.last_updated_at if details else None,
                    has_specified_cycle_length=details.has_specified_cycle_length if details else None,
                    has_specified_period_length=details.has_specified_period_length if details else None,
                    pregnancy_snapshot=details.pregnancy_snapshot if details else None,
                )
            )

        return PaginatedResponse(
            data=data,
            pagination=Pagination(
                has_more=has_more,
                next_cursor=next_cursor,
                previous_cursor=previous_cursor,
                total_count=total_count,
            ),
            metadata=TimeseriesMetadata(
                sample_count=len(data),
                start_time=params.start_datetime,
                end_time=params.end_datetime,
            ),
        )

    def get_meals(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
    ) -> PaginatedResponse[Meal]:
        params.category = EventCategory.MEAL
        records, total_count = self._get_records_with_filters(db_session, params, str(user_id))

        limit = params.limit or 20
        has_more = len(records) > limit
        is_backward = params.cursor and params.cursor.startswith("prev_")

        if has_more:
            records = records[-limit:] if is_backward else records[:limit]

        next_cursor = None
        previous_cursor = None

        if records:
            if has_more:
                last_record, _ = records[-1]
                next_cursor = encode_cursor(last_record.start_datetime, last_record.id, "next")
            if params.cursor:
                if is_backward:
                    if has_more:
                        first_record, _ = records[0]
                        previous_cursor = encode_cursor(first_record.start_datetime, first_record.id, "prev")
                else:
                    first_record, _ = records[0]
                    previous_cursor = encode_cursor(first_record.start_datetime, first_record.id, "prev")

        data = []
        for record, data_source in records:
            details: MealDetails | None = record.meal_detail
            nutrients = {
                SeriesType(code): value
                for code, value in (details.nutrients if details else {}).items()
                if code in SeriesType.__members__
            }
            calories_kcal, macros, water_ml = _meal_summary(nutrients)
            data.append(
                Meal(
                    id=record.id,
                    timestamp=record.start_datetime,
                    meal_type=details.meal_type if details else None,
                    name=details.title if details else None,
                    source=self._map_source(data_source),
                    calories_kcal=calories_kcal,
                    macros=macros,
                    water_ml=water_ml,
                    nutrients=_nutrient_values(nutrients),
                )
            )

        return PaginatedResponse(
            data=data,
            pagination=Pagination(
                has_more=has_more,
                next_cursor=next_cursor,
                previous_cursor=previous_cursor,
                total_count=total_count,
            ),
            metadata=TimeseriesMetadata(
                sample_count=len(data),
                start_time=params.start_datetime,
                end_time=params.end_datetime,
            ),
        )

    def delete_event_record(
        self,
        db_session: DbSession,
        user_id: UUID,
        record_id: UUID,
        category: EventCategory,
    ) -> bool:
        """Delete an event record by id and category. Returns False if not found or not owned by user."""
        if category == EventCategory.SLEEP:
            # The shared sleep writer lock, taken before the user's sessions are read, as create_or_merge_sleep
            # and the writers of Open Wearables' own sleep scores do: the delete changes the day's scores, and
            # maybe its naps, below
            self.lock_sleep_user(db_session, user_id)
        record = self.crud.get_record_with_details(db_session, record_id, category)
        if not record:
            return False
        data_source = self.data_source_repo.get(db_session, record.data_source_id)
        if not data_source or data_source.user_id != user_id:
            return False
        # Read before the delete: the day is rescored, and without the night a nap of that day may be its main sleep
        source_id, wake_date = record.data_source_id, local_date(record.end_datetime, record.zone_offset)
        self.crud.delete_flush(db_session, record)
        data_type_coverage_repository.refresh_events(
            db_session, [(data_source.user_id, data_source.provider, record.category)]
        )
        if category == EventCategory.SLEEP:
            if not flags_own_naps(data_source.provider):
                # Its nap flags are all the rule's (sleep_nap_marking): the day is marked again both ways. A source
                # with a flag of its own keeps its flags as they are, as upstream does.
                self._mark_naps(db_session, source_id, {wake_date}, keep_source_naps=False)
            self._recompute_sleep_scores(db_session, user_id, {wake_date})
        db_session.commit()
        return True


event_record_service = EventRecordService()
