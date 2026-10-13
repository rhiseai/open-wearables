#!/usr/bin/env python3
"""Reconcile stored overlapping Apple sleep sessions for an explicitly scoped account (RHISE-4915).

Older ingestion could leave two sessions from one data_source with overlapping windows.
Extending either into the other's exact window then violated ix_event_record_source_time.
This tool reuses live ingestion's canonical reconciler; it does not invent missing HealthKit
samples, combine sources, change source priorities, or repair a lone truncated night.

Both --user-id and an inclusive local wake-date window (--since/--until) are required.
Dry-run is the default; --apply takes the same PostgreSQL user lock as ingestion, re-reads
the rows, then reconciles only components containing actual overlapping duplicate rows.
The full transitive gap-adjacent component must remain inside the requested date scope,
its external IDs must not also name a disconnected session, and every contributing row
must have stored stages. Otherwise the component is reported as
skipped: ambiguous scalar totals cannot reconstruct the missing intervals or provenance.

JSON Lines on stdout contain before snapshots and the surviving record after applying.
Save and review the dry-run output, take the database's normal backup, and compare the
applied audit with OpenWearables and Lucie's historical sleep rows. A second run has no
overlapping duplicate to change. Nothing is registered for startup or scheduled execution.

Usage (inside the backend container):
    uv run python scripts/data_migrations/reconcile_apple_sleep.py \\
        --user-id <UUID> --since 2026-09-29 --until 2026-09-30 --dry-run
    uv run python scripts/data_migrations/reconcile_apple_sleep.py \\
        --user-id <UUID> --since 2026-09-29 --until 2026-09-30 --apply
"""

import argparse
import json
from collections import defaultdict
from collections.abc import Sequence
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import SessionLocal
from app.models import DataSource, EventRecord
from app.schemas.enums import ProviderName
from app.schemas.model_crud.activities.event_record import EventRecordCreate
from app.schemas.model_crud.activities.event_record_detail import EventRecordDetailCreate
from app.services.event_record_service import event_record_service

_DETAIL_FIELDS = (
    "sleep_total_duration_minutes",
    "sleep_time_in_bed_minutes",
    "sleep_efficiency_score",
    "sleep_deep_minutes",
    "sleep_rem_minutes",
    "sleep_light_minutes",
    "sleep_awake_minutes",
    "is_nap",
    "sleep_stages",
)


def _wake_date(record: EventRecord) -> date:
    offset = datetime.fromisoformat(f"2000-01-01T00:00:00{record.zone_offset or '+00:00'}").utcoffset()
    stamp = record.end_datetime
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (stamp.astimezone(timezone.utc) + (offset or timedelta())).date()


def _components(
    rows: Sequence[tuple[EventRecord, DataSource]], gap: timedelta
) -> list[list[tuple[EventRecord, DataSource]]]:
    """Connected interval components, isolated by the existing exact data_source_id."""
    components: list[list[tuple[EventRecord, DataSource]]] = []
    current: list[tuple[EventRecord, DataSource]] = []
    component_end: datetime | None = None
    for record, source in sorted(
        rows, key=lambda pair: (str(pair[0].data_source_id), pair[0].start_datetime, str(pair[0].id))
    ):
        if current:
            assert component_end is not None
            if record.data_source_id != current[0][0].data_source_id or record.start_datetime > component_end + gap:
                components.append(current)
                current = []
                component_end = None
        current.append((record, source))
        component_end = max(component_end, record.end_datetime) if component_end is not None else record.end_datetime
    if current:
        components.append(current)
    return components


def _has_overlap(rows: Sequence[tuple[EventRecord, DataSource]]) -> bool:
    end: datetime | None = None
    for record, _source in rows:
        if end is not None and record.start_datetime < end:
            return True
        end = max(end, record.end_datetime) if end is not None else record.end_datetime
    return False


def _snapshot(record: EventRecord) -> dict[str, Any]:
    detail = record.sleep_detail
    return {
        "id": str(record.id),
        "external_id": record.external_id,
        "data_source_id": str(record.data_source_id),
        "start_datetime": record.start_datetime.isoformat(),
        "end_datetime": record.end_datetime.isoformat(),
        "zone_offset": record.zone_offset,
        "duration_seconds": record.duration_seconds,
        "details": deepcopy({field: getattr(detail, field) for field in _DETAIL_FIELDS})
        if detail is not None
        else None,
    }


def reconcile_apple_sleep(
    db: Session, *, user_id: UUID, since: date, until: date, apply: bool = False
) -> list[dict[str, Any]]:
    """Return auditable scoped results; the caller commits or rolls back the whole pass."""
    if since > until:
        raise ValueError("--since must be on or before --until")
    if apply:
        event_record_service.lock_sleep_user(db, user_id)
    query = (
        db.query(EventRecord, DataSource)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .options(selectinload(EventRecord.sleep_detail))
        .filter(
            DataSource.user_id == user_id,
            DataSource.provider == ProviderName.APPLE,
            EventRecord.category == "sleep",
            EventRecord.type == "sleep_session",
        )
        .populate_existing()
    )
    if apply:
        query = query.with_for_update(of=EventRecord)
    # Read all of this account's Apple sessions so a transitive component cannot
    # silently extend beyond the operator's date scope. Only scoped components write.
    rows = [(record, source) for record, source in query.all()]
    components = _components(rows, timedelta(minutes=settings.sleep_end_gap_minutes))
    external_owners: dict[tuple[UUID, str], set[UUID]] = defaultdict(set)
    for record, _source in rows:
        if record.external_id is not None:
            external_owners[(record.data_source_id, record.external_id)].add(record.id)
    audit: list[dict[str, Any]] = []
    for component in components:
        wake_dates = [_wake_date(record) for record, _source in component]
        if not any(since <= wake_date <= until for wake_date in wake_dates):
            continue
        item: dict[str, Any] = {"user_id": str(user_id), "before": [_snapshot(record) for record, _ in component]}
        component_ids = {record.id for record, _source in component}
        disconnected_owners: set[UUID] = set()
        for record, _source in component:
            if record.external_id is not None:
                disconnected_owners.update(external_owners[(record.data_source_id, record.external_id)] - component_ids)
        if not all(since <= wake_date <= until for wake_date in wake_dates):
            item.update(status="skipped", reason="component_outside_date_scope")
        elif disconnected_owners:
            item.update(
                status="skipped",
                reason="disconnected_external_id",
                related_record_ids=sorted(str(record_id) for record_id in disconnected_owners),
            )
        elif any(record.sleep_detail is None or not record.sleep_detail.sleep_stages for record, _ in component):
            item.update(status="skipped", reason="missing_stage_evidence")
        elif not _has_overlap(component):
            item.update(status="unchanged", reason="no_overlapping_duplicates")
        elif not apply:
            item.update(status="would_reconcile")
        else:
            own, source = component[0]
            own_detail = own.sleep_detail
            assert own_detail is not None
            record = EventRecordCreate(
                id=own.id,
                external_id=own.external_id,
                user_id=user_id,
                data_source_id=own.data_source_id,
                provider=ProviderName.APPLE,
                source=source.source,
                source_name=own.source_name,
                device_model=source.device_model,
                category="sleep",
                type="sleep_session",
                start_datetime=own.start_datetime,
                end_datetime=own.end_datetime,
                duration_seconds=own.duration_seconds,
                zone_offset=own.zone_offset,
            )
            detail = EventRecordDetailCreate(
                record_id=own.id, **{field: getattr(own_detail, field) for field in _DETAIL_FIELDS}
            )
            saved = event_record_service.create_or_merge_sleep(
                db, user_id, record, detail, settings.sleep_end_gap_minutes, commit=False
            )
            db.flush()
            db.expire(saved, ["sleep_detail"])
            item.update(status="reconciled", after=_snapshot(saved))
        audit.append(item)
    return audit


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, type=UUID)
    parser.add_argument("--since", required=True, type=date.fromisoformat, help="First local wake date, inclusive")
    parser.add_argument("--until", required=True, type=date.fromisoformat, help="Last local wake date, inclusive")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report only (the default)")
    mode.add_argument("--apply", action="store_true", help="Commit scoped, stage-backed duplicate reconciliation")
    args = parser.parse_args(argv)
    if args.since > args.until:
        parser.error("--since must be on or before --until")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    with SessionLocal() as db:
        try:
            audit = reconcile_apple_sleep(
                db, user_id=args.user_id, since=args.since, until=args.until, apply=args.apply
            )
            if args.apply:
                db.commit()
            else:
                db.rollback()
        except Exception:
            db.rollback()
            raise
    for item in audit:
        print(json.dumps(item, default=str, sort_keys=True))
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "components": len(audit)}))


if __name__ == "__main__":
    main()
