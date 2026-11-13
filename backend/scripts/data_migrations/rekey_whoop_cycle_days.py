#!/usr/bin/env python3
"""Re-key existing Whoop whole-day strain, energy and steps onto the day the cycle covers.

All were written with recorded_at = the cycle's start. A Whoop cycle runs from one
sleep onset to the next, so for anyone who falls asleep before local midnight the start
sits on the previous day: the value lands a day early, and when bedtime drifts across
midnight two cycles collapse onto one date -- that day double-counts and the next is
left empty (#1554). normalize_cycle now keys on the midpoint's local date, stored as
local midnight; this brings the existing rows onto the same key.

The cycle's end is recovered without refetching: cycle[n].end == cycle[n+1].start ==
the next non-nap sleep's onset, verified against raw payloads. Where the next onset is
more than 30h out it belongs to a later cycle (device off), so the span falls back to
start + 24h, which is what Whoop itself synthesizes for a day it has no sleep for.

Two rows can want the same new key: either two cycles resolve to one date, or the date
is already held by a row this migration does not touch. Both are left alone and
reported rather than merged or overwritten -- neither should occur in practice, and a
score is worth more than a tidy count.

Idempotent: eligibility is "recorded_at is not local midnight", which is exactly what a
migrated row is not. Cycles Whoop synthesized on local-midnight boundaries are already
on the right key and are skipped for the same reason.

Bootstrap runs before application logging is configured. Diagnostics therefore use
explicit JSON with aggregate counts and exception class names only, independently of
LOG_FORMAT. Row timestamp previews and CLI tracebacks from migration execution are
omitted: database exceptions can contain SQL, parameters and health data. Import or
configuration failures before the CLI is entered retain their existing Python behavior.
Programmatic main() attempts rollback without replacing the migration exception if
that explicit rollback fails. Session context-manager cleanup retains its normal
exception behavior. The CLI exits nonzero, and app.sh retains its existing non-fatal
retry-on-next-startup policy. No completion marker is written. Table updates are
reported as staged until the shared transaction commits.
"committed" does not mean all rows were resolved: contested targets stay untouched and
are reported separately. Dry runs and attempts moving no rows have distinct outcomes.

Usage (inside Docker):
    docker compose exec app uv run python scripts/data_migrations/rekey_whoop_cycle_days.py --dry-run
    docker compose exec app uv run python scripts/data_migrations/rekey_whoop_cycle_days.py
"""

import argparse
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import SessionLocal

# Whoop's longest real cycle is ~29h; past that the next onset opens a later cycle.
_MAX_CYCLE_SPAN = "30 hours"
_SCRIPT_ID = "rekey_whoop_cycle_days"


def _emit(level: str, action: str, message: str, **attributes: str | int | bool | None) -> None:
    """Emit bootstrap JSON without runtime formatters, exception text or trace context."""
    print(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": level,
                "logger": "bootstrap.data_migrations",
                "message": message,
                "provider": "whoop",
                "action": action,
                "script": _SCRIPT_ID,
                **attributes,
            }
        ),
        flush=True,
    )


@dataclass(frozen=True)
class _Table:
    """One table's worth of cycle-derived rows, and how to find their owner."""

    name: str
    candidates: str
    key: tuple[str, ...]  # the columns the unique constraint scopes recorded_at by


_HEALTH_SCORE = _Table(
    name="health_score",
    candidates="""
        SELECT id, user_id, provider, category, recorded_at, zone_offset
        FROM health_score
        WHERE provider = 'whoop' AND category = 'strain' AND event_record_id IS NULL
          -- percent_recorded is on WorkoutScore, never CycleScore.
          AND NOT (components ? 'percent_recorded')
    """,
    key=("user_id", "provider", "category"),
)

_DATA_POINT_SERIES = _Table(
    name="data_point_series",
    # Keep the source/metric lookup correlated so it uses the leading columns
    # of the unique index instead of scanning rows from unrelated providers.
    candidates="""
        SELECT sample.*, source.user_id
        FROM data_source source
        CROSS JOIN series_type_definition metric
        CROSS JOIN LATERAL (
            SELECT id, data_source_id, series_type_definition_id, recorded_at, zone_offset
            FROM data_point_series
            WHERE data_source_id = source.id AND series_type_definition_id = metric.id
              AND is_daily_total IS TRUE
            OFFSET 0
        ) sample
        WHERE source.provider = 'whoop' AND metric.code IN ('active_energy', 'total_energy', 'steps')
    """,
    key=("data_source_id", "series_type_definition_id"),
)


def _mapping(table: _Table) -> str:
    """Every eligible row with the local midnight it belongs on, ranked within its day."""
    partition = ", ".join(f"t.{column}" for column in table.key)
    return f"""
        SELECT t.*,
               d.new_recorded_at,
               row_number() OVER (
                   PARTITION BY {partition}, d.new_recorded_at
                   ORDER BY least(c.cycle_end, d.new_recorded_at + interval '1 day')
                          - greatest(t.recorded_at, d.new_recorded_at) DESC,
                            t.recorded_at DESC
               ) AS rank
        FROM ({table.candidates}) t
        CROSS JOIN LATERAL (
            SELECT cast(coalesce(t.zone_offset, '+00:00') AS interval) AS off
        ) o
        CROSS JOIN LATERAL (
            SELECT coalesce(min(er.start_datetime), t.recorded_at + interval '24 hours') AS cycle_end
            FROM data_source ds
            JOIN event_record er ON er.data_source_id = ds.id
            JOIN sleep_details sd ON sd.record_id = er.id
            WHERE ds.user_id = t.user_id
              AND ds.provider = 'whoop'
              AND er.category = 'sleep'
              AND sd.is_nap IS NOT TRUE
              AND er.start_datetime > t.recorded_at
              AND er.start_datetime <= t.recorded_at + interval '{_MAX_CYCLE_SPAN}'
        ) c
        CROSS JOIN LATERAL (
            SELECT ((
                (((t.recorded_at + (c.cycle_end - t.recorded_at) / 2 + o.off) AT TIME ZONE 'UTC')::date)::timestamp
                - o.off
            ) AT TIME ZONE 'UTC') AS new_recorded_at
        ) d
        WHERE ((t.recorded_at + o.off) AT TIME ZONE 'UTC')::time <> '00:00:00'
    """


def _candidate_queries(table: _Table) -> str:
    """Share one mapping between the diagnostics and their conflict check."""
    # All key columns are NOT NULL, so equality can use the unique index.
    same_key = " AND ".join(f"x.{column} = m.{column}" for column in table.key)
    return f"""
        WITH mapping AS MATERIALIZED ({_mapping(table)}),
        movable AS (
            SELECT m.id, m.new_recorded_at
            FROM mapping m
            WHERE m.rank = 1
              AND NOT EXISTS (
                  SELECT 1 FROM {table.name} x
                  WHERE {same_key}
                    AND x.recorded_at = m.new_recorded_at
                    AND x.id <> m.id
              )
        )
    """


def _rekey(db: Session, table: _Table, *, dry_run: bool) -> int:
    candidates = _candidate_queries(table)
    eligible, contested = db.execute(
        text(f"""
            {candidates}
            SELECT count(*), count(*) - (SELECT count(*) FROM movable)
            FROM mapping
        """)
    ).one()
    _emit(
        "info",
        "bootstrap_data_migration_inspected",
        "Inspected Whoop cycle rows",
        table=table.name,
        eligible=eligible,
        contested=contested,
        dry_run=dry_run,
    )
    if not eligible:
        return 0

    if dry_run:
        return 0

    moved = db.execute(
        text(f"""
            {candidates}
            UPDATE {table.name} tgt
            SET recorded_at = v.new_recorded_at
            FROM movable v
            WHERE tgt.id = v.id
        """)
    ).rowcount  # ty: ignore[unresolved-attribute]
    _emit(
        "info",
        "bootstrap_data_migration_staged",
        "Staged Whoop cycle row updates",
        table=table.name,
        rows_staged=moved,
    )
    return moved


def rekey_whoop_cycle_days(db: Session, *, dry_run: bool) -> int:
    """Re-key Whoop cycle rows onto the day they cover. Does not commit -- caller owns the transaction."""
    return sum(_rekey(db, table, dry_run=dry_run) for table in (_HEALTH_SCORE, _DATA_POINT_SERIES))


def main(dry_run: bool) -> None:
    with SessionLocal() as db:
        try:
            moved = rekey_whoop_cycle_days(db, dry_run=dry_run)
            if not dry_run and moved:
                db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception as rollback_error:
                # Cleanup or its diagnostics must not replace the migration failure.
                with suppress(Exception):
                    _emit(
                        "error",
                        "bootstrap_data_migration_rollback_failed",
                        "Whoop cycle re-key rollback failed",
                        exception_class=type(rollback_error).__name__,
                    )
            raise
        outcome = "dry_run" if dry_run else "committed" if moved else "no_rows_moved"
        _emit(
            "info",
            "bootstrap_data_migration_finished",
            "Whoop cycle re-key attempt finished",
            outcome=outcome,
            rows_moved=moved,
        )


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview affected rows without modifying data")
    args = parser.parse_args(argv)
    try:
        main(dry_run=args.dry_run)
    except Exception as exc:
        cause = getattr(exc, "orig", None)
        if cause is None:
            cause = exc.__cause__
        # A closed output stream must not expose the original SQL exception through
        # an unhandled logging error's implicit traceback context.
        with suppress(OSError, ValueError):
            _emit(
                "error",
                "bootstrap_data_migration_failed",
                "Whoop cycle re-key attempt failed",
                exception_class=type(exc).__name__,
                cause_class=type(cause).__name__ if cause is not None else None,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
