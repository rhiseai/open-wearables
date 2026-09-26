import contextlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from logging import getLogger
from uuid import UUID, uuid4

import sentry_sdk

from app.config import settings
from app.constants.series_types.sdk import (
    SleepPhase,
    get_apple_sleep_phase,
)
from app.constants.sleep import SleepStageType
from app.database import DbSession
from app.integrations.redis_client import get_redis_client
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    SleepStage,
)
from app.schemas.providers.mobile_sdk import (
    SLEEP_START_STATES,
    SleepState,
    SleepStateStage,
)
from app.schemas.providers.mobile_sdk import (
    SyncRequest as SDKSyncRequest,
)
from app.services.event_record_service import event_record_service
from app.services.sdk.device_resolution import extract_device_info
from app.services.sdk.sleep_night import SleepNight, build_sleep_night, sleep_source_key
from app.utils.sleep_invariants import SleepInvariantViolationError, sleep_invariant_violations
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)


def key(user_id: str) -> str:
    """Generate a key for the sleep state."""
    return f"sleep:active:{user_id}"


def active_users_key() -> str:
    """Generate a key for the active users."""
    return "sleep:active_users"


def load_sleep_state(user_id: str) -> SleepState | None:
    """Load the sleep state from Redis."""
    sleep_state_key = key(user_id)
    state = get_redis_client().get(sleep_state_key)
    if not state:
        return None
    try:
        if isinstance(state, bytes):
            state = state.decode("utf-8")
        return SleepState.model_validate_json(state)
    except Exception as e:
        logger.error(f"Failed to parse sleep state for user {user_id}: {e}")
        try:
            raw = json.loads(state)
            return SleepState.model_validate(raw)
        except Exception as fallback_e:
            logger.error(f"Legacy state migration failed for user {user_id}: {fallback_e}; session will be dropped")
            return None


def save_sleep_state(user_id: str, state: SleepState) -> None:
    get_redis_client().set(key(user_id), state.model_dump_json())
    get_redis_client().expire(key(user_id), settings.redis_sleep_ttl_seconds)
    get_redis_client().sadd(active_users_key(), user_id)


def delete_sleep_state(user_id: str) -> None:
    get_redis_client().delete(key(user_id))
    get_redis_client().srem(active_users_key(), user_id)


def _create_new_sleep_state(
    start_time: datetime,
    end_time: datetime,
    id: str | None = None,
    provider: str | None = None,
    source_name: str | None = None,
    device_model: str | None = None,
    zone_offset: str | None = None,
) -> SleepState:
    return SleepState(
        uuid=id or str(uuid4()),
        source_name=source_name or "unknown",
        device_model=device_model,
        provider=provider,
        zone_offset=zone_offset,
        start_time=start_time,
        end_time=end_time,
        last_start_timestamp=start_time,
        last_end_timestamp=end_time,
        in_bed_seconds=0,
        awake_seconds=0,
        sleeping_seconds=0,
        light_seconds=0,
        deep_seconds=0,
        rem_seconds=0,
        stages=[],
    )


def _apply_transition(
    db_session: DbSession,
    user_id: str,
    state: SleepState,
    sleep_phase: SleepPhase,
    start_time: datetime,
    end_time: datetime,
    provider: str,
    uuid: str | None = None,
    source_name: str | None = None,
    device_model: str | None = None,
    zone_offset: str | None = None,
    source_key: str | None = None,
) -> SleepState:
    """Apply a transition to the sleep state."""

    # Compute the gap using session boundaries (start_time / end_time) rather than
    # the timestamps of the last-processed sample.  This correctly handles payloads
    # that arrive out of chronological order: a sample that chains directly onto an
    # earlier part of the night will have a near-zero distance to the session window
    # even if it was enqueued after a later-night payload was already processed.
    if start_time <= state.end_time and end_time >= state.start_time:
        # New sample overlaps with the current session window → same session.
        delta_seconds = 0.0
    elif end_time <= state.start_time:
        # New sample is entirely before the session start.
        delta_seconds = (state.start_time - end_time).total_seconds()
    else:
        # New sample is entirely after the session end.
        delta_seconds = (start_time - state.end_time).total_seconds()

    if delta_seconds > settings.sleep_end_gap_minutes * 60:
        persist_sleep(db_session, user_id, state, close=True)
        state = _create_new_sleep_state(start_time, end_time, uuid, provider, source_name, device_model, zone_offset)

    if zone_offset and not state.zone_offset:
        state.zone_offset = zone_offset

    duration_seconds = (end_time - start_time).total_seconds()

    stage_label: SleepStageType

    match sleep_phase:
        case SleepPhase.IN_BED:
            state.in_bed_seconds += duration_seconds
            stage_label = SleepStageType.IN_BED
        case SleepPhase.AWAKE:
            state.awake_seconds += duration_seconds
            stage_label = SleepStageType.AWAKE
        case SleepPhase.ASLEEP_LIGHT:
            state.light_seconds += duration_seconds
            stage_label = SleepStageType.LIGHT
        case SleepPhase.ASLEEP_DEEP:
            state.deep_seconds += duration_seconds
            stage_label = SleepStageType.DEEP
        case SleepPhase.SLEEPING:
            state.sleeping_seconds += duration_seconds
            stage_label = SleepStageType.SLEEPING
        case SleepPhase.ASLEEP_REM:
            state.rem_seconds += duration_seconds
            stage_label = SleepStageType.REM
        case _:
            stage_label = SleepStageType.UNKNOWN

    if end_time > state.end_time:
        state.end_time = end_time
    elif start_time < state.start_time:
        state.start_time = start_time

    state.last_start_timestamp = start_time
    state.last_end_timestamp = end_time

    state.stages.append(
        SleepStateStage(
            stage=stage_label,
            start_time=start_time,
            end_time=end_time,
            source_key=source_key,
            source_name=source_name,
            device_model=device_model,
        )
    )

    return state


def handle_sleep_data(
    db_session: DbSession,
    request: SDKSyncRequest,
    user_id: str,
) -> None:
    """
    Process SDK sleep data and track sleep sessions using Redis state.

    Stages accumulate in Redis across SDK batches. After each batch the current
    session is flushed to Postgres via ``create_or_merge_sleep`` (same path as
    cloud providers) so summaries are available immediately — including while
    the night is still in progress. Redis state is only deleted when a session
    boundary is detected (inter-sample gap or wall-clock quiet gap >
    ``sleep_end_gap_minutes``).

    A per-user Redis lock serializes concurrent calls so that parallel Celery tasks
    (e.g. from a bulk historical upload) accumulate stages into the same session instead
    of overwriting each other's state.

    Repeated flushes use a stable ``external_id`` (``state.uuid``) so the DB row is
    updated in place rather than double-counting. When a new Redis session starts
    after the previous one closed, ``create_or_merge_sleep`` still merges with an
    adjacent DB record when payloads for the same night arrive as separate batches.

    Args:
        db_session: Database session for persisting sleep records
        request: Parsed SDKSyncRequest containing sleep records
        user_id: User identifier for associating sleep data

    Flow:
        - Acquire a per-user Redis lock to prevent concurrent state corruption
        - Deduplicate incoming data based on start/end/stage/source
        - If no active session exists: Create new session in Redis (only for valid start states)
        - If active session exists: Check gap between new sample and the session window
          * Gap > 2 hours: Persist+close existing session, start new one
          * Otherwise: Accumulate sleep stage durations in existing session
        - Save Redis state once; flush to Postgres; close Redis if quiet gap elapsed
        - Dispatch the stale-sleep housekeeping task
    """
    redis_client = get_redis_client()
    lock = redis_client.lock(f"sleep:lock:{user_id}", timeout=30, blocking_timeout=15)

    try:
        acquired = lock.acquire()
        if not acquired:
            logger.warning("Could not acquire sleep processing lock for user %s; skipping batch", user_id)
            return

        current_state = load_sleep_state(user_id)
        provider = request.provider

        # Deduplicate and sort
        seen = set()
        unique_data = []

        # Sort first by startDate to ensure chronological processing
        sorted_raw = sorted(request.data.sleep, key=lambda x: x.startDate)

        for item in sorted_raw:
            # Create a unique key for deduplication
            # SourceInfo is not hashable, use JSON dump
            source_key = item.source.model_dump_json() if item.source else None
            key_tuple = (item.startDate, item.endDate, item.stage, source_key)

            if key_tuple not in seen:
                seen.add(key_tuple)
                unique_data.append(item)

        for sjson in unique_data:
            # Extract device info
            device_model, software_version, original_source_name = extract_device_info(sjson.source)

            sleep_phase = get_apple_sleep_phase(sjson.stage)

            if sleep_phase is None:
                continue

            if not current_state:
                if sleep_phase not in SLEEP_START_STATES:
                    continue

                current_state = _create_new_sleep_state(
                    sjson.startDate,
                    sjson.endDate,
                    sjson.id,
                    provider,
                    original_source_name,
                    device_model,
                    sjson.zoneOffset,
                )

            current_state = _apply_transition(
                db_session,
                user_id,
                current_state,
                sleep_phase,
                sjson.startDate,
                sjson.endDate,
                provider,
                sjson.id,
                original_source_name,
                device_model,
                sjson.zoneOffset,
                sleep_source_key(sjson.source),
            )

        # Persist the accumulated state to Redis only once after processing the entire batch,
        # then flush to Postgres immediately so summaries are visible without waiting for
        # the quiet-gap finalize. Close Redis only when the session is already stale
        # (historical uploads / post-wake quiet gap).
        if current_state:
            save_sleep_state(user_id, current_state)

            session_end = current_state.end_time
            if session_end.tzinfo is None:
                session_end = session_end.replace(tzinfo=timezone.utc)
            close = datetime.now(timezone.utc) - session_end >= timedelta(minutes=settings.sleep_end_gap_minutes)
            persist_sleep(db_session, user_id, current_state, close=close)

    finally:
        with contextlib.suppress(Exception):
            lock.release()

    # import not at module level in order to avoid circular import
    from app.integrations.celery.tasks.finalize_stale_sleep_task import finalize_stale_sleeps

    # Dispatch the stale-sleep task so sessions that have gone quiet (including
    # other users' sessions) are finalised promptly without waiting for the next beat.
    finalize_stale_sleeps.delay()


def _calculate_final_metrics(stages: list[SleepStateStage]) -> tuple[dict, list[SleepStage]]:
    """Metrics and hypnogram of a session, built from one source's intervals.

    Returns (metrics_dict, cleaned_stages_list). See ``build_sleep_night``.
    """
    night = build_sleep_night(stages)
    return night.metrics, night.stages


def _in_bed_bounds(stages: list[SleepStateStage]) -> tuple[datetime, datetime] | None:
    """Bounds of the in_bed union: earliest in_bed start and latest in_bed end.

    Returns None when the session has no in_bed samples.
    """
    in_bed = [s for s in stages if s.stage == SleepStageType.IN_BED]
    if not in_bed:
        return None

    return min(s.start_time for s in in_bed), max(s.end_time for s in in_bed)


def _report_rejected_night(
    user_id: str,
    state: SleepState,
    night: SleepNight,
    violations: list[str],
    values: dict,
) -> None:
    """Log and send to Sentry a night that breaks the sleep invariants."""
    log_structured(
        logger,
        "warning",
        "Apple sleep night rejected: impossible totals",
        provider=state.provider or "unknown",
        action="sleep_night_rejected",
        user_id=user_id,
        session_id=state.uuid,
        violations=violations,
        chosen_source=night.source_key or "unknown",
        discarded_sources=night.discarded_sources,
        **values,
    )
    with sentry_sdk.push_scope() as scope:
        scope.set_level("warning")
        scope.set_context(
            "sleep_night",
            {
                "user_id": user_id,
                "session_id": state.uuid,
                "window": [state.start_time.isoformat(), state.end_time.isoformat()],
                "violations": violations,
                "values": values,
                "chosen_source": night.source_key,
                "discarded_sources": night.discarded_sources,
                "raw_buckets": night.raw_buckets,
            },
        )
        sentry_sdk.capture_message("Apple sleep night rejected: impossible totals")


def persist_sleep(
    db_session: DbSession,
    user_id: str,
    state: SleepState,
    *,
    close: bool = False,
) -> None:
    """Flush the current Redis sleep state to Postgres.

    Uses the shared ``create_or_merge_sleep`` path so Apple sessions behave like
    other providers: the first flush creates the row, subsequent flushes with the
    same ``state.uuid`` as ``external_id`` update it in place (no double-counting).
    Adjacent sessions from a different Redis uuid (historical multi-payload nights)
    are still merged by ``create_or_merge_sleep``.

    The night is built from a single HealthKit source (see ``build_sleep_night``)
    and must pass the sleep invariants; a night that fails them, alone or merged
    with an adjacent record, is not written and is reported to Sentry.

    Args:
        db_session: Database session
        user_id: User identifier
        state: In-progress sleep state from Redis
        close: When True, delete the Redis state after a successful write (session
            boundary / quiet-gap finalization). When False, Redis keeps accumulating
            stages for the next batch.
    """
    night = build_sleep_night(state.stages)
    metrics, cleaned_stages = night.metrics, night.stages

    if night.discarded_sources:
        log_structured(
            logger,
            "info",
            "Apple sleep night built from one HealthKit source",
            provider=state.provider or "unknown",
            action="sleep_source_chosen",
            user_id=user_id,
            session_id=state.uuid,
            chosen_source=night.source_key or "unknown",
            discarded_sources=night.discarded_sources,
        )

    if cleaned_stages:
        start_time = cleaned_stages[0].start_time
        end_time = cleaned_stages[-1].end_time
        # The hypnogram can cover only part of the night while in_bed samples cover
        # all of it (e.g. a Whoop night relayed through Apple Health with asleep
        # stages for a sub-window only).  Widen the event window to the in-bed union
        # so the record is never shorter than the time-in-bed it reports.
        in_bed_bounds = _in_bed_bounds([s for s in state.stages if s.source_key == night.source_key])
        if in_bed_bounds:
            start_time = min(start_time, in_bed_bounds[0])
            end_time = max(end_time, in_bed_bounds[1])
    else:
        end_time = state.end_time
        start_time = state.start_time

    source_for_lookup = state.source_name if state.source_name != "unknown" else None

    total_duration = (end_time - start_time).total_seconds()
    total_sleep_seconds = night.total_sleep_seconds
    time_in_bed_seconds = night.time_in_bed_seconds
    sleep_efficiency = (
        Decimal(str(total_sleep_seconds / time_in_bed_seconds * 100)) if time_in_bed_seconds > 0 else None
    )

    sleep_record = EventRecordCreate(
        id=uuid4(),
        external_id=state.uuid,
        user_id=UUID(user_id),
        start_datetime=start_time,
        end_datetime=end_time,
        zone_offset=state.zone_offset,
        duration_seconds=int(total_duration),
        category="sleep",
        type="sleep_session",
        source_name=state.source_name or "unknown",
        source=source_for_lookup,
        provider=state.provider,
        device_model=state.device_model,
    )

    detail = EventRecordDetailCreate(
        record_id=sleep_record.id,
        sleep_total_duration_minutes=int(total_sleep_seconds // 60),
        sleep_time_in_bed_minutes=int(time_in_bed_seconds // 60),
        sleep_deep_minutes=int(metrics["deep_seconds"] // 60),
        sleep_rem_minutes=int(metrics["rem_seconds"] // 60),
        sleep_light_minutes=int(metrics["light_seconds"] // 60),
        sleep_awake_minutes=int(metrics["awake_seconds"] // 60),
        sleep_efficiency_score=sleep_efficiency,
        is_nap=False,
        sleep_stages=cleaned_stages or None,
    )

    values = {
        "total_sleep_minutes": detail.sleep_total_duration_minutes,
        "time_in_bed_minutes": detail.sleep_time_in_bed_minutes,
        "deep_minutes": detail.sleep_deep_minutes,
        "rem_minutes": detail.sleep_rem_minutes,
        "light_minutes": detail.sleep_light_minutes,
        "sleeping_minutes": int(metrics["sleeping_seconds"] // 60),
        "awake_minutes": detail.sleep_awake_minutes,
    }
    violations = sleep_invariant_violations(
        total_sleep_minutes=values["total_sleep_minutes"],
        time_in_bed_minutes=values["time_in_bed_minutes"],
        stage_minutes=(values[k] for k in ("deep_minutes", "rem_minutes", "light_minutes", "sleeping_minutes")),
    )
    if violations:
        _report_rejected_night(user_id, state, night, violations, values)
        if close:
            delete_sleep_state(user_id)
        return

    try:
        event_record_service.create_or_merge_sleep(
            db_session,
            UUID(user_id),
            sleep_record,
            detail,
            settings.sleep_end_gap_minutes,
            enforce_invariants=True,
        )
        # Only drop Redis after a successful DB write so a transient error keeps the
        # session available for the next flush / periodic finalization attempt.
        if close:
            delete_sleep_state(user_id)
    except SleepInvariantViolationError as e:
        db_session.rollback()
        _report_rejected_night(user_id, state, night, e.violations, {**values, "merged": e.values})
        if close:
            delete_sleep_state(user_id)
    except Exception as e:
        log_structured(
            logger,
            "error",
            f"Error saving sleep record {sleep_record.id} for user {user_id}: {e}",
            provider=state.provider or "unknown",
            action="sleep_record_save_error",
            user_id=user_id,
            event_record_id=sleep_record.id,
            error=str(e),
        )


def finish_sleep(db_session: DbSession, user_id: str, state: SleepState) -> None:
    """Finalize a sleep session: persist to Postgres and close the Redis state."""
    persist_sleep(db_session, user_id, state, close=True)
