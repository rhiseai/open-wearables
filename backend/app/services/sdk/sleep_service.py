import contextlib
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from logging import getLogger
from typing import Any
from uuid import UUID, uuid4

from celery import current_app as celery_app
from redis.exceptions import LockNotOwnedError

from app.config import settings
from app.constants.celery_tasks import CeleryTask
from app.constants.series_types.sdk import (
    SleepPhase,
    get_apple_sleep_phase,
)
from app.constants.sleep import SleepStageType
from app.database import DbSession
from app.integrations.redis_client import get_redis_client
from app.models import EventRecord
from app.schemas.enums import DeviceType, EventCategory, HealthScoreCategory, ProviderName
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    HealthScoreCreate,
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
from app.services.health_score_service import health_score_service
from app.services.sdk.device_resolution import extract_device_ids, extract_device_info, extract_device_type
from app.services.sleep_reconciliation import merge_sleep_stages
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

_STAGE_TO_METRIC: dict[str, str] = {
    "awake": "awake_seconds",
    "sleeping": "sleeping_seconds",
    "light": "light_seconds",
    "deep": "deep_seconds",
    "rem": "rem_seconds",
}


SLEEP_SCORE_VALUE_TYPE = "sleepScore"


def _extract_sleep_score(values: list[dict[str, Any]] | None) -> float | None:
    """Return the provider sleep score carried in an SDK sleep entry's `values`, if any."""
    for value in values or []:
        if value.get("type") == SLEEP_SCORE_VALUE_TYPE and value.get("value") is not None:
            with contextlib.suppress(TypeError, ValueError):
                return float(value["value"])
    return None


def _state_identity(provider: str | None, source_name: str | None, device_model: str | None) -> tuple[str, str, str]:
    """Use the same nullable identity fields as DataSourceRepository."""
    source = source_name if source_name != "unknown" else None
    return provider or "", source or "", device_model or ""


def key(user_id: str, state: SleepState | None = None) -> str:
    """The legacy user key, or a state scoped to its existing data-source identity."""
    if state is None:
        return f"sleep:active:{user_id}"
    identity = _state_identity(state.provider, state.source_name, state.device_model)
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return f"sleep:active:{user_id}:{digest}"


def source_states_key(user_id: str) -> str:
    return f"sleep:active_sources:{user_id}"


def active_users_key() -> str:
    """Generate a key for the active users."""
    return "sleep:active_users"


def load_sleep_state(user_id: str, *, storage_key: str | None = None) -> SleepState | None:
    """Load the sleep state from Redis."""
    sleep_state_key = storage_key or key(user_id)
    state = get_redis_client().get(sleep_state_key)
    if not state:
        return None
    if isinstance(state, bytes):
        state = state.decode("utf-8")
    # Never overwrite an unreadable state with a new partial session.
    return SleepState.model_validate_json(state)


def load_source_sleep_states(user_id: str) -> dict[str, SleepState]:
    states = {}
    for raw_key in get_redis_client().smembers(source_states_key(user_id)):
        storage_key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
        state = load_sleep_state(user_id, storage_key=storage_key)
        if state is not None:
            states[storage_key] = state
    return states


def save_sleep_state(user_id: str, state: SleepState) -> None:
    storage_key = key(user_id, state)
    get_redis_client().set(storage_key, state.model_dump_json())
    get_redis_client().expire(storage_key, settings.redis_sleep_ttl_seconds)
    get_redis_client().sadd(source_states_key(user_id), storage_key)
    get_redis_client().expire(source_states_key(user_id), settings.redis_sleep_ttl_seconds)
    get_redis_client().sadd(active_users_key(), user_id)


def delete_sleep_state(user_id: str, *, storage_key: str | None = None) -> None:
    redis_client = get_redis_client()
    storage_key = storage_key or key(user_id)
    redis_client.delete(storage_key)
    redis_client.srem(source_states_key(user_id), storage_key)
    if not redis_client.exists(key(user_id)) and not redis_client.scard(source_states_key(user_id)):
        redis_client.srem(active_users_key(), user_id)


def prune_sleep_state_registry(user_id: str) -> None:
    """Retire expired cache keys while the caller owns sleep processing."""
    redis_client = get_redis_client()
    for storage_key in redis_client.smembers(source_states_key(user_id)):
        if not redis_client.exists(storage_key):
            redis_client.srem(source_states_key(user_id), storage_key)
    if not redis_client.exists(key(user_id)) and not redis_client.scard(source_states_key(user_id)):
        redis_client.srem(active_users_key(), user_id)


def release_sleep_lock(lock: Any, user_id: str) -> None:
    try:
        lock.release()
    except LockNotOwnedError:
        # PostgreSQL is authoritative if the Redis lease expired while writing.
        # Never release a lease another worker acquired in the meantime.
        log_structured(
            logger, "warning", "Sleep processing lease expired", action="sleep_lock_expired", user_id=user_id
        )


def _create_new_sleep_state(
    start_time: datetime,
    end_time: datetime,
    id: str | None = None,
    provider: str | None = None,
    source_name: str | None = None,
    device_model: str | None = None,
    zone_offset: str | None = None,
    device_type: DeviceType | None = None,
    device_id: str | None = None,
    source_app_id: str | None = None,
    device_manufacturer: str | None = None,
) -> SleepState:
    return SleepState(
        uuid=id or str(uuid4()),
        source_name=source_name or "unknown",
        device_model=device_model,
        device_type=device_type,
        device_id=device_id,
        source_app_id=source_app_id,
        device_manufacturer=device_manufacturer,
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
    device_type: DeviceType | None = None,
    device_id: str | None = None,
    source_app_id: str | None = None,
    device_manufacturer: str | None = None,
    *,
    commit: bool = True,
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
        persist_sleep(db_session, user_id, state, close=True, commit=commit)
        state = _create_new_sleep_state(
            start_time,
            end_time,
            uuid,
            provider,
            source_name,
            device_model,
            zone_offset,
            device_type,
            device_id,
            source_app_id,
            device_manufacturer,
        )

    if zone_offset and not state.zone_offset:
        state.zone_offset = zone_offset
    if device_type and not state.device_type and (source_name or "unknown") == state.source_name:
        state.device_type = device_type

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
    if start_time < state.start_time:
        state.start_time = start_time

    state.last_start_timestamp = start_time
    state.last_end_timestamp = end_time

    state.stages.append(
        SleepStateStage(
            stage=stage_label,
            start_time=start_time,
            end_time=end_time,
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

    Stages accumulate separately for each existing data-source identity. After each batch the current
    session is flushed to Postgres via ``create_or_merge_sleep`` (same path as
    cloud providers) so summaries are available immediately — including while
    the night is still in progress. Redis state is only deleted when a session
    boundary is detected (inter-sample gap or wall-clock quiet gap >
    ``sleep_end_gap_minutes``).

    A PostgreSQL user lock owns database reconciliation; the Redis lease also spans
    publishing the committed state. Redis remains a cache if that lease expires.

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
        - Commit all sleep and provider-score writes before publishing Redis state
        - Dispatch the stale-sleep housekeeping task
    """
    redis_client = get_redis_client()
    lock = redis_client.lock(f"sleep:lock:{user_id}", timeout=30, blocking_timeout=15)
    acquired = lock.acquire()
    if not acquired:
        raise RuntimeError(f"Could not acquire sleep processing lock for user {user_id}")

    try:
        event_record_service.lock_sleep_user(db_session, UUID(user_id))
        stored_states = load_source_sleep_states(user_id)
        legacy = load_sleep_state(user_id)
        if legacy is not None:
            stored_states[key(user_id)] = legacy

        current_states: dict[tuple[str, str, str], SleepState] = {}
        old_keys: set[str] = set(stored_states)
        for state in stored_states.values():
            identity = _state_identity(state.provider, state.source_name, state.device_model)
            previous = current_states.get(identity)
            if previous is not None:
                # A rolling upgrade can leave a legacy and scoped state together.
                # Flush both under their original identity before retiring the old key.
                persist_sleep(db_session, user_id, previous, commit=False)
            current_states[identity] = state

        seen = set()
        for sample in sorted(request.data.sleep, key=lambda item: item.startDate):
            device_model, _, source_name = extract_device_info(sample.source)
            device_type = extract_device_type(sample.source)
            device_id, source_app_id, device_manufacturer = extract_device_ids(sample.source)
            identity = _state_identity(request.provider, source_name, device_model)
            sample_key = (identity, sample.startDate, sample.endDate, sample.stage)
            if sample_key in seen:
                continue
            seen.add(sample_key)
            sleep_phase = get_apple_sleep_phase(sample.stage)
            if sleep_phase is None:
                continue

            state = current_states.get(identity)
            if state is None:
                if sleep_phase not in SLEEP_START_STATES:
                    continue
                state = _create_new_sleep_state(
                    sample.startDate,
                    sample.endDate,
                    sample.id,
                    request.provider,
                    source_name,
                    device_model,
                    sample.zoneOffset,
                    device_type,
                    device_id,
                    source_app_id,
                    device_manufacturer,
                )
            state = _apply_transition(
                db_session,
                user_id,
                state,
                sleep_phase,
                sample.startDate,
                sample.endDate,
                request.provider,
                sample.id,
                source_name,
                device_model,
                sample.zoneOffset,
                device_type,
                device_id,
                source_app_id,
                device_manufacturer,
                commit=False,
            )
            sleep_score = _extract_sleep_score(sample.values)
            if sleep_score is not None:
                state.sleep_score = sleep_score
            current_states[identity] = state

        to_publish: list[tuple[SleepState, bool]] = []
        for state in current_states.values():
            session_end = state.end_time
            if session_end.tzinfo is None:
                session_end = session_end.replace(tzinfo=timezone.utc)
            close = datetime.now(timezone.utc) - session_end >= timedelta(minutes=settings.sleep_end_gap_minutes)
            persist_sleep(db_session, user_id, state, close=close, commit=False)
            to_publish.append((state, close))

        # Sleep rows, details and provider scores are one transaction. Redis is a
        # resumable cache and must never advance past a failed database write.
        db_session.commit()
        for state, close in to_publish:
            save_sleep_state(user_id, state)
            storage_key = key(user_id, state)
            old_keys.discard(storage_key)
            if close:
                delete_sleep_state(user_id, storage_key=storage_key)
        for storage_key in old_keys:
            delete_sleep_state(user_id, storage_key=storage_key)
        prune_sleep_state_registry(user_id)
    except Exception:
        db_session.rollback()
        raise
    finally:
        release_sleep_lock(lock, user_id)

    # Dispatch the stale-sleep task so sessions that have gone quiet (including
    # other users' sessions) are finalised promptly without waiting for the next beat.
    celery_app.send_task(CeleryTask.FINALIZE_STALE_SLEEPS)


def _calculate_final_metrics(stages: list[SleepStateStage]) -> tuple[dict, list[SleepStage]]:
    """
    Recalculate metrics from stages, handling overlaps by prioritizing earlier segments.
    Returns (metrics_dict, cleaned_stages_list).

    Input stages are now list[SleepStateStage] Pydantic models with normalized stage values.
    """
    metrics = {
        "in_bed_seconds": 0,
        "awake_seconds": 0,
        "sleeping_seconds": 0,
        "light_seconds": 0,
        "deep_seconds": 0,
        "rem_seconds": 0,
    }

    # Determine processing strategy based on what stage types are present:
    # - Only in_bed (no sleeping/light/deep/rem): treat in_bed as sleeping (legacy devices)
    # - Detailed phases present (light/deep/rem): use only detailed + awake, drop sleeping wrapper
    # - Only sleeping (no detailed): use sleeping + awake as-is
    has_detailed = any(s.stage in ("light", "deep", "rem") for s in stages)
    has_sleep_data = any(s.stage in ("sleeping", "light", "deep", "rem") for s in stages)

    if not has_sleep_data:
        processable = [
            SleepStateStage(stage=SleepStageType.SLEEPING, start_time=s.start_time, end_time=s.end_time)
            if s.stage == "in_bed"
            else s
            for s in stages
            if s.stage != "unknown"
        ]
    elif has_detailed:
        processable = [s for s in stages if s.stage not in ("in_bed", "sleeping", "unknown")]
    else:
        processable = [s for s in stages if s.stage not in ("in_bed", "unknown")]

    cleaned_stages = merge_sleep_stages(
        [SleepStage(stage=stage.stage, start_time=stage.start_time, end_time=stage.end_time) for stage in processable]
    )
    for stage in cleaned_stages:
        metric_key = _STAGE_TO_METRIC.get(str(stage.stage))
        if metric_key:
            metrics[metric_key] += (stage.end_time - stage.start_time).total_seconds()

    # 2. Process IN_BED duration separately (union of intervals)
    in_bed_raw = [s for s in stages if s.stage == "in_bed"]
    if in_bed_raw:
        sorted_in_bed = sorted(in_bed_raw, key=lambda x: x.start_time)
        current_start = None
        current_end = None

        for stage in sorted_in_bed:
            start = stage.start_time
            end = stage.end_time

            if current_start is None:
                current_start = start
                current_end = end
                continue

            if start < current_end:  # ty:ignore[unsupported-operator]
                current_end = max(current_end, end)  # ty:ignore[invalid-argument-type]
            else:
                metrics["in_bed_seconds"] += (current_end - current_start).total_seconds()  # ty:ignore[unsupported-operator]
                current_start = start
                current_end = end

        if current_start and current_end:
            metrics["in_bed_seconds"] += (current_end - current_start).total_seconds()
    else:
        metrics["in_bed_seconds"] = (
            metrics["awake_seconds"]
            + metrics["sleeping_seconds"]
            + metrics["light_seconds"]
            + metrics["deep_seconds"]
            + metrics["rem_seconds"]
        )

    return metrics, cleaned_stages


def _in_bed_bounds(stages: list[SleepStateStage]) -> tuple[datetime, datetime] | None:
    """Bounds of the in_bed union: earliest in_bed start and latest in_bed end.

    Returns None when the session has no in_bed samples.
    """
    in_bed = [s for s in stages if s.stage == SleepStageType.IN_BED]
    if not in_bed:
        return None

    return min(s.start_time for s in in_bed), max(s.end_time for s in in_bed)


def persist_sleep(
    db_session: DbSession,
    user_id: str,
    state: SleepState,
    *,
    close: bool = False,
    commit: bool = True,
) -> None:
    """Flush the current Redis sleep state to Postgres.

    Uses the shared ``create_or_merge_sleep`` path so Apple sessions behave like
    other providers: the first flush creates the row, subsequent flushes with the
    same ``state.uuid`` as ``external_id`` update it in place (no double-counting).
    Adjacent sessions from a different Redis uuid (historical multi-payload nights)
    are still merged by ``create_or_merge_sleep``.

    Args:
        db_session: Database session
        user_id: User identifier
        state: In-progress sleep state from Redis
        close: When True, delete the Redis state after a successful write (session
            boundary / quiet-gap finalization). When False, Redis keeps accumulating
            stages for the next batch.
        commit: When False, the caller owns the transaction and Redis publication.
    """
    metrics, cleaned_stages = _calculate_final_metrics(state.stages)

    if cleaned_stages:
        start_time = cleaned_stages[0].start_time
        end_time = cleaned_stages[-1].end_time
        # The hypnogram can cover only part of the night while in_bed samples cover
        # all of it (e.g. a Whoop night relayed through Apple Health with asleep
        # stages for a sub-window only).  Widen the event window to the in-bed union
        # so the record is never shorter than the time-in-bed it reports.
        in_bed_bounds = _in_bed_bounds(state.stages)
        if in_bed_bounds:
            start_time = min(start_time, in_bed_bounds[0])
            end_time = max(end_time, in_bed_bounds[1])
    else:
        end_time = state.end_time
        start_time = state.start_time

    source_for_lookup = state.source_name if state.source_name != "unknown" else None

    total_duration = (end_time - start_time).total_seconds()
    total_sleep_seconds = (
        metrics["sleeping_seconds"] + metrics["light_seconds"] + metrics["deep_seconds"] + metrics["rem_seconds"]
    )
    time_in_bed_seconds = max(metrics["in_bed_seconds"], total_sleep_seconds + metrics["awake_seconds"])
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
        category=EventCategory.SLEEP,
        type="sleep_session",
        source_name=state.source_name or "unknown",
        source=source_for_lookup,
        provider=state.provider,
        device_model=state.device_model,
        device_type=state.device_type,
        device_id=state.device_id,
        source_app_id=state.source_app_id,
        device_manufacturer=state.device_manufacturer,
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

    try:
        event_record_service.lock_sleep_user(db_session, UUID(user_id))
        saved_record = event_record_service.create_or_merge_sleep(
            db_session,
            UUID(user_id),
            sleep_record,
            detail,
            settings.sleep_end_gap_minutes,
            commit=False,
        )
        _save_provider_sleep_score(db_session, user_id, state, saved_record, start_time)
        # Only drop Redis after a successful DB write so a transient error keeps the
        # session available for the next flush / periodic finalization attempt.
        if commit:
            db_session.commit()
            if close:
                delete_sleep_state(user_id, storage_key=key(user_id, state))
    except Exception:
        db_session.rollback()
        raise


def finish_sleep(db_session: DbSession, user_id: str, state: SleepState, *, commit: bool = True) -> None:
    """Finalize a sleep session: persist to Postgres and close the Redis state."""
    persist_sleep(db_session, user_id, state, close=True, commit=commit)


def _save_provider_sleep_score(
    db_session: DbSession,
    user_id: str,
    state: SleepState,
    record: EventRecord,
    start_time: datetime,
) -> None:
    """Persist the provider-computed sleep score (e.g. Samsung SLEEP_SCORE) linked to the session."""
    if state.sleep_score is None or state.provider is None:
        return
    try:
        provider = ProviderName(state.provider)
    except ValueError:
        return

    health_score_service.bulk_create(
        db_session,
        [
            HealthScoreCreate(
                id=uuid4(),
                user_id=UUID(user_id),
                data_source_id=record.data_source_id,
                provider=provider,
                category=HealthScoreCategory.SLEEP,
                value=state.sleep_score,
                recorded_at=start_time,
                zone_offset=state.zone_offset,
                event_record_id=record.id,
            )
        ],
    )
