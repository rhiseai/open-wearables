#!/usr/bin/env python3
"""Recompute Apple Health sleep nights that report impossible totals (RHISE-4202).

Problem: Apple Health nights could be stored so that the daily sleep summary
reports more sleep than time in bed, about twice the real night. The summary
adds up every main-sleep record of a (date, source, device) group, so one night
stored as two overlapping records reads as double. A user whose Oura ring writes
into Apple Health had eight such nights (12 to 16 hours of "sleep" inside 6 to 9
hours in bed). Ingestion now builds a night from interval unions of a single
HealthKit source, prefers the session's own record when re-flushing, serializes
merges per user, and refuses to write a night that breaks the invariants; this
script repairs what is already stored.

What it changes, per Apple sleep night (records of one data source whose windows
overlap) since ``--since``:

* The night is re-derived from the stored stage intervals of all its records with
  the same interval rules as ingestion (``build_sleep_night``): total sleep is the
  union of asleep time, stage minutes come from stage intervals, and time in bed is
  the best single record's time in bed, never less than sleep plus awake and never
  more than the night's window.
* The longest record is kept and updated in place; the other overlapping records
  are deleted (their sleep scores cascade), sleep scores are recomputed for the
  affected dates, and one ``sleep.updated`` webhook is emitted per changed night so
  consumers re-pull the day.
* A night whose re-derived values still break the invariants is left untouched and
  reported (stdout and a Sentry warning).
* A night whose records carry no stage intervals is only de-duplicated (the longest
  record stays as stored); a single such record that is itself impossible is left
  untouched and reported, since there is nothing to rebuild it from.

With ``--only-violations`` (the default) only nights that break an invariant are
touched: overlapping records, total sleep above time in bed, above 16 hours, above
the night's window, or stage minutes above total sleep. ``--no-only-violations``
re-derives every Apple night and writes the ones whose values change.

Idempotent: a repaired night is a single record whose values re-derive to
themselves, so a re-run finds nothing to change. Not wired into startup; run it by
hand, staging first.

Usage (inside the app container):
    uv run python scripts/data_migrations/recompute_apple_sleep.py --since 2026-08-01 --dry-run
    uv run python scripts/data_migrations/recompute_apple_sleep.py --since 2026-08-01
    uv run python scripts/data_migrations/recompute_apple_sleep.py --since 2026-08-01 --user-id <uuid>
    uv run python scripts/data_migrations/recompute_apple_sleep.py --since 2026-08-01 --no-only-violations
"""

import argparse
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import sentry_sdk
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.constants.sleep import SleepStageType
from app.database import SessionLocal
from app.integrations.sentry import init_sentry
from app.models import DataSource, EventRecord, SleepDetails
from app.schemas.enums import ProviderName
from app.schemas.model_crud.activities import EventRecordDetailCreate, SleepStage
from app.schemas.providers.mobile_sdk import SleepStateStage
from app.services.event_record_service import event_record_service
from app.services.outgoing_webhooks.events import on_sleep_updated
from app.services.sdk.sleep_night import build_sleep_night
from app.utils.sleep_invariants import sleep_invariant_violations

_STAGE_FIELDS = ("deep_minutes", "light_minutes", "rem_minutes")


@dataclass(frozen=True)
class StoredSleep:
    """One stored main-sleep record, detached from the ORM."""

    record_id: UUID
    start: datetime
    end: datetime
    zone_offset: str | None
    total_sleep_minutes: int | None
    time_in_bed_minutes: int | None
    deep_minutes: int | None
    light_minutes: int | None
    rem_minutes: int | None
    awake_minutes: int | None
    stages: list[SleepStage] = field(default_factory=list)

    @property
    def window_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class NightPlan:
    """How one stored night gets rewritten."""

    keep: StoredSleep
    drop: list[StoredSleep]
    reasons: list[str]
    start: datetime
    end: datetime
    detail: dict[str, Any]
    changed: bool


@dataclass(frozen=True)
class Unrepairable:
    """A night that breaks the invariants and cannot be rebuilt."""

    records: list[StoredSleep]
    reasons: list[str]
    values: dict[str, Any]


def cluster_overlapping(records: Sequence[StoredSleep]) -> list[list[StoredSleep]]:
    """Group one data source's records into nights: records whose windows overlap."""
    clusters: list[list[StoredSleep]] = []
    cluster_end: datetime | None = None
    for record in sorted(records, key=lambda r: (r.start, r.end)):
        if clusters and cluster_end is not None and record.start < cluster_end:
            clusters[-1].append(record)
            cluster_end = max(cluster_end, record.end)
        else:
            clusters.append([record])
            cluster_end = record.end
    return clusters


def _sum(values: Sequence[int | None]) -> int | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def night_violations(cluster: Sequence[StoredSleep]) -> list[str]:
    """Invariants the night breaks as the daily summary reports it (summed records)."""
    reasons: list[str] = []
    if len(cluster) > 1:
        reasons.append("overlapping_records")

    total = _sum([r.total_sleep_minutes for r in cluster])
    reasons += sleep_invariant_violations(
        total_sleep_minutes=total,
        time_in_bed_minutes=_sum([r.time_in_bed_minutes for r in cluster]),
        stage_minutes=[_sum([getattr(r, f) for r in cluster]) for f in _STAGE_FIELDS],
    )
    window = int((max(r.end for r in cluster) - min(r.start for r in cluster)).total_seconds() // 60)
    if total is not None and total > window:
        reasons.append("total_sleep_exceeds_window")
    return reasons


def _current_values(record: StoredSleep) -> dict[str, Any]:
    return {
        "sleep_total_duration_minutes": record.total_sleep_minutes,
        "sleep_time_in_bed_minutes": record.time_in_bed_minutes,
        "sleep_deep_minutes": record.deep_minutes,
        "sleep_light_minutes": record.light_minutes,
        "sleep_rem_minutes": record.rem_minutes,
        "sleep_awake_minutes": record.awake_minutes,
    }


def plan_night(cluster: Sequence[StoredSleep], reasons: list[str]) -> NightPlan | Unrepairable:
    """Re-derive one night from its stored stage intervals."""
    keep = max(cluster, key=lambda r: (r.window_minutes, -r.start.timestamp()))
    drop = [r for r in cluster if r.record_id != keep.record_id]
    start = min(r.start for r in cluster)
    end = max(r.end for r in cluster)
    window = int((end - start).total_seconds() // 60)

    samples = [
        SleepStateStage(stage=s.stage, start_time=s.start_time, end_time=s.end_time)
        for r in cluster
        for s in r.stages
        if s.stage != SleepStageType.IN_BED
    ]
    if samples:
        night = build_sleep_night(samples)
        total = int(night.total_sleep_seconds // 60)
        in_bed_candidates = [
            min(r.time_in_bed_minutes, r.window_minutes) for r in cluster if r.time_in_bed_minutes is not None
        ]
        in_bed = min(max([*in_bed_candidates, int(night.time_in_bed_seconds // 60)]), window)
        detail: dict[str, Any] = {
            "sleep_total_duration_minutes": total,
            "sleep_time_in_bed_minutes": in_bed,
            "sleep_deep_minutes": int(night.metrics["deep_seconds"] // 60),
            "sleep_light_minutes": int(night.metrics["light_seconds"] // 60),
            "sleep_rem_minutes": int(night.metrics["rem_seconds"] // 60),
            "sleep_awake_minutes": int(night.metrics["awake_seconds"] // 60),
        }
        stages: list[SleepStage] | None = night.stages or None
        sleeping_minutes = int(night.metrics["sleeping_seconds"] // 60)
    else:
        # Nothing to rebuild from: the longest record is the night.
        start, end = keep.start, keep.end
        detail = _current_values(keep)
        stages = keep.stages or None
        sleeping_minutes = 0

    violations = sleep_invariant_violations(
        total_sleep_minutes=detail["sleep_total_duration_minutes"],
        time_in_bed_minutes=detail["sleep_time_in_bed_minutes"],
        stage_minutes=[
            detail["sleep_deep_minutes"],
            detail["sleep_light_minutes"],
            detail["sleep_rem_minutes"],
            sleeping_minutes,
        ],
    )
    if violations:
        return Unrepairable(records=list(cluster), reasons=reasons + violations, values=detail)

    total = detail["sleep_total_duration_minutes"]
    in_bed = detail["sleep_time_in_bed_minutes"]
    detail["sleep_efficiency_score"] = (
        Decimal(str(round(total / in_bed * 100, 2))) if total is not None and in_bed else None
    )
    detail["sleep_stages"] = stages

    changed = (
        bool(drop)
        or start != keep.start
        or end != keep.end
        or any(detail[k] != v for k, v in _current_values(keep).items())
    )
    return NightPlan(keep=keep, drop=drop, reasons=reasons, start=start, end=end, detail=detail, changed=changed)


def _load_nights(db: Session, since: date, user_id: UUID | None) -> dict[UUID, list[StoredSleep]]:
    """Stored Apple main-sleep records since ``since``, keyed by data source."""
    since_dt = datetime.combine(since, time.min, tzinfo=timezone.utc)
    query = (
        db.query(EventRecord, SleepDetails)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .join(SleepDetails, SleepDetails.record_id == EventRecord.id)
        .filter(
            DataSource.provider == ProviderName.APPLE,
            EventRecord.category == "sleep",
            EventRecord.type == "sleep_session",
            EventRecord.end_datetime >= since_dt,
            func.coalesce(SleepDetails.is_nap, False).is_(False),
        )
    )
    if user_id is not None:
        query = query.filter(DataSource.user_id == user_id)

    by_source: dict[UUID, list[StoredSleep]] = defaultdict(list)
    for record, detail in query.order_by(EventRecord.data_source_id, EventRecord.start_datetime).all():
        by_source[record.data_source_id].append(
            StoredSleep(
                record_id=record.id,
                start=record.start_datetime,
                end=record.end_datetime,
                zone_offset=record.zone_offset,
                total_sleep_minutes=detail.sleep_total_duration_minutes,
                time_in_bed_minutes=detail.sleep_time_in_bed_minutes,
                deep_minutes=detail.sleep_deep_minutes,
                light_minutes=detail.sleep_light_minutes,
                rem_minutes=detail.sleep_rem_minutes,
                awake_minutes=detail.sleep_awake_minutes,
                stages=[SleepStage.model_validate(s) for s in (detail.sleep_stages or [])],
            )
        )
    return by_source


def _apply(db: Session, data_source: DataSource, plan: NightPlan) -> None:
    """Write one planned night and emit ``sleep.updated`` for it."""
    user_id = data_source.user_id
    dates = {event_record_service._local_sleep_date(r.start, r.zone_offset) for r in [plan.keep, *plan.drop]}

    for dropped in plan.drop:
        record = db.get(EventRecord, dropped.record_id)
        if record is not None:
            db.delete(record)
    db.flush()

    kept = db.get(EventRecord, plan.keep.record_id)
    if kept is None:
        raise RuntimeError(f"sleep record {plan.keep.record_id} disappeared")
    kept.start_datetime = plan.start
    kept.end_datetime = plan.end
    kept.duration_seconds = int((plan.end - plan.start).total_seconds())
    db.flush()

    detail = EventRecordDetailCreate(record_id=kept.id, is_nap=False, **plan.detail)
    event_record_service.event_record_detail_repo.delete_by_record_id(db, kept.id, "sleep")
    event_record_service.event_record_detail_repo.create_and_flush(db, detail, detail_type="sleep")
    dates.add(event_record_service._local_sleep_date(kept.start_datetime, kept.zone_offset))
    event_record_service._recompute_sleep_scores(db, user_id, dates)
    db.commit()

    eff = detail.sleep_efficiency_score
    has_stages = any(
        [detail.sleep_awake_minutes, detail.sleep_light_minutes, detail.sleep_deep_minutes, detail.sleep_rem_minutes]
    )
    on_sleep_updated(
        record_id=kept.id,
        user_id=user_id,
        provider=ProviderName(data_source.provider).value,
        device=data_source.device_model,
        start_time=kept.start_datetime.isoformat(),
        end_time=kept.end_datetime.isoformat(),
        zone_offset=kept.zone_offset,
        duration_seconds=kept.duration_seconds,
        efficiency_percent=float(eff) if eff is not None else None,
        stages={
            "awake_minutes": detail.sleep_awake_minutes,
            "light_minutes": detail.sleep_light_minutes,
            "deep_minutes": detail.sleep_deep_minutes,
            "rem_minutes": detail.sleep_rem_minutes,
        }
        if has_stages
        else None,
        is_nap=False,
        source_app=data_source.source,
        device_type=data_source.device_type,
        sleep_duration_seconds=(
            detail.sleep_total_duration_minutes * 60 if detail.sleep_total_duration_minutes is not None else None
        ),
        sleep_stage_intervals=[s.model_dump(mode="json") for s in detail.sleep_stages] if detail.sleep_stages else None,
    )


def _report_unrepairable(data_source: DataSource, night: Unrepairable) -> None:
    records = [str(r.record_id) for r in night.records]
    print(
        f"SKIP user={data_source.user_id} source={data_source.id} records={records} "
        f"reasons={night.reasons} values={night.values}"
    )
    with sentry_sdk.push_scope() as scope:
        scope.set_level("warning")
        scope.set_context(
            "sleep_night",
            {
                "user_id": str(data_source.user_id),
                "data_source_id": str(data_source.id),
                "records": [
                    {
                        "record_id": str(r.record_id),
                        "start": r.start.isoformat(),
                        "end": r.end.isoformat(),
                        **_current_values(r),
                    }
                    for r in night.records
                ],
                "reasons": night.reasons,
                "rebuilt": night.values,
            },
        )
        sentry_sdk.capture_message("Apple sleep backfill: night still impossible after recompute, left as is")


def recompute_apple_sleep(
    db: Session,
    since: date,
    *,
    only_violations: bool = True,
    user_id: UUID | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Re-derive Apple sleep nights since ``since``; returns counters."""
    counts = {"nights": 0, "changed": 0, "unchanged": 0, "skipped_ok": 0, "unrepairable": 0, "failed": 0}

    for data_source_id, records in _load_nights(db, since, user_id).items():
        data_source = db.get(DataSource, data_source_id)
        if data_source is None:
            continue
        for cluster in cluster_overlapping(records):
            counts["nights"] += 1
            reasons = night_violations(cluster)
            if only_violations and not reasons:
                counts["skipped_ok"] += 1
                continue

            plan = plan_night(cluster, reasons)
            if isinstance(plan, Unrepairable):
                counts["unrepairable"] += 1
                _report_unrepairable(data_source, plan)
                continue
            if not plan.changed:
                counts["unchanged"] += 1
                continue

            total_before = _sum([r.total_sleep_minutes for r in cluster])
            in_bed_before = _sum([r.time_in_bed_minutes for r in cluster])
            print(
                f"{'WOULD FIX' if dry_run else 'FIX'} user={data_source.user_id} record={plan.keep.record_id} "
                f"night={plan.start.isoformat()}..{plan.end.isoformat()} reasons={reasons} "
                f"drop={[str(r.record_id) for r in plan.drop]} "
                f"total={total_before}->{plan.detail['sleep_total_duration_minutes']} "
                f"in_bed={in_bed_before}->{plan.detail['sleep_time_in_bed_minutes']}"
            )
            counts["changed"] += 1
            if dry_run:
                continue
            try:
                _apply(db, data_source, plan)
            except Exception as e:
                db.rollback()
                counts["failed"] += 1
                counts["changed"] -= 1
                print(f"ERROR: failed to rewrite night {plan.keep.record_id}: {e}", file=sys.stderr)
                sentry_sdk.capture_exception(e)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute Apple Health sleep nights from stored stage intervals.")
    parser.add_argument("--since", type=date.fromisoformat, required=True, help="First night to scan (YYYY-MM-DD).")
    parser.add_argument(
        "--only-violations",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Only touch nights that break a sleep invariant (default). --no-only-violations re-derives every night.",
    )
    parser.add_argument("--user-id", type=UUID, default=None, help="Limit to one user.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without writing.")
    args = parser.parse_args()

    init_sentry()
    with SessionLocal() as db:
        counts = recompute_apple_sleep(
            db,
            args.since,
            only_violations=args.only_violations,
            user_id=args.user_id,
            dry_run=args.dry_run,
        )
    print(f"\nDone{' (dry run, nothing written)' if args.dry_run else ''}: {counts}")


if __name__ == "__main__":
    main()
