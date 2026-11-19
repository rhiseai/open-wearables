"""RHISE-7796: a nap the provider flagged never merges into the night beside it."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session

from app.config import settings
from app.models import DataSource, EventRecord
from app.schemas.enums import ProviderName
from app.schemas.model_crud.activities import EventRecordCreate, EventRecordDetailCreate
from app.services.event_record_service import event_record_service
from app.services.summaries_service import SummariesService
from tests.factories import DataSourceFactory


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def _write(
    db: Session, source: DataSource, start: str, end: str, *, is_nap: bool, external_id: str | None = None
) -> EventRecord:
    """One cloud sleep record, as a provider's data_247 path hands it over."""
    begin, finish = _at(start), _at(end)
    minutes = int((finish - begin).total_seconds() // 60)
    record = EventRecordCreate(
        id=uuid4(),
        external_id=external_id or f"cloud-{uuid4()}",
        user_id=source.user_id,
        data_source_id=source.id,
        provider=source.provider,
        source=source.source,
        source_name=source.source or "unknown",
        device_model=source.device_model,
        category="sleep",
        type="sleep_session",
        start_datetime=begin,
        end_datetime=finish,
        zone_offset="+00:00",
        duration_seconds=minutes * 60,
    )
    detail = EventRecordDetailCreate(
        record_id=record.id,
        sleep_total_duration_minutes=minutes - 10,
        sleep_time_in_bed_minutes=minutes,
        sleep_efficiency_score=Decimal(90),
        is_nap=is_nap,
    )
    return event_record_service.create_or_merge_sleep(
        db, source.user_id, record, detail, settings.sleep_end_gap_minutes
    )


def _stored(db: Session, source: DataSource) -> list[tuple[str, str, bool]]:
    records = (
        db.query(EventRecord).filter(EventRecord.data_source_id == source.id).order_by(EventRecord.start_datetime).all()
    )
    for record in records:
        db.refresh(record)
    return [
        (
            record.start_datetime.strftime("%H:%M"),
            record.end_datetime.strftime("%H:%M"),
            bool(record.sleep_detail and record.sleep_detail.is_nap),
        )
        for record in records
    ]


def test_a_whoop_nap_an_hour_and_a_half_after_waking_stays_a_nap(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.WHOOP, source="whoop")
    _write(db, source, "2026-11-15T23:00:00", "2026-11-16T07:00:00", is_nap=False)
    _write(db, source, "2026-11-16T08:30:00", "2026-11-16T09:30:00", is_nap=True)

    assert _stored(db, source) == [("23:00", "07:00", False), ("08:30", "09:30", True)]
    start = _at("2026-11-16T00:00:00")
    (summary,) = (
        SummariesService()
        .get_sleep_summaries(db, source.user_id, start, start + timedelta(days=1), cursor=None, limit=10)
        .data
    )
    assert summary.duration_minutes == 470
    assert summary.nap_count == 1
    assert summary.nap_duration_minutes == 60


def test_a_nap_that_syncs_first_does_not_swallow_the_night(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.OURA, source="oura")
    _write(db, source, "2026-11-16T08:30:00", "2026-11-16T09:30:00", is_nap=True)
    _write(db, source, "2026-11-15T23:00:00", "2026-11-16T07:00:00", is_nap=False)

    assert _stored(db, source) == [("23:00", "07:00", False), ("08:30", "09:30", True)]


def test_two_parts_of_one_night_still_merge(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.OURA, source="oura")
    _write(db, source, "2026-11-15T23:00:00", "2026-11-16T02:00:00", is_nap=False)
    _write(db, source, "2026-11-16T03:00:00", "2026-11-16T07:00:00", is_nap=False)

    assert _stored(db, source) == [("23:00", "07:00", False)]


def test_two_parts_of_one_nap_still_merge(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.GARMIN, source="garmin")
    _write(db, source, "2026-11-16T13:00:00", "2026-11-16T13:40:00", is_nap=True)
    _write(db, source, "2026-11-16T14:00:00", "2026-11-16T14:30:00", is_nap=True)

    assert _stored(db, source) == [("13:00", "14:30", True)]


def test_a_resent_record_that_changed_its_flag_replaces_itself(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.WHOOP, source="whoop")
    _write(db, source, "2026-11-16T08:30:00", "2026-11-16T09:30:00", is_nap=True, external_id="whoop-1")
    _write(db, source, "2026-11-16T08:30:00", "2026-11-16T09:30:00", is_nap=False, external_id="whoop-1")

    assert _stored(db, source) == [("08:30", "09:30", False)]


def test_apple_sessions_merge_as_before(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.APPLE)
    _write(db, source, "2026-11-15T23:00:00", "2026-11-16T02:00:00", is_nap=False)
    _write(db, source, "2026-11-16T03:00:00", "2026-11-16T07:00:00", is_nap=False)

    assert _stored(db, source) == [("23:00", "07:00", False)]


def test_the_same_sleep_sent_twice_with_another_flag_still_merges(db: Session) -> None:
    """Overlapping records are one sleep: two copies must never be counted as a night and a nap."""
    source = DataSourceFactory(provider=ProviderName.GARMIN, source="garmin")
    _write(db, source, "2026-11-16T13:00:00", "2026-11-16T14:00:00", is_nap=True)
    _write(db, source, "2026-11-16T13:05:00", "2026-11-16T14:10:00", is_nap=False)

    assert _stored(db, source) == [("13:00", "14:10", False)]


def test_a_nap_we_flagged_still_joins_the_night_that_follows_it(db: Session) -> None:
    """Withings sends no nap flag, so a stored one is ours: an evening sleep flagged a nap joins the night
    that starts within the merge gap, as upstream merges it, and the day is flagged again."""
    source = DataSourceFactory(provider=ProviderName.WITHINGS, source="withings")
    _write(db, source, "2026-11-15T23:00:00", "2026-11-16T07:00:00", is_nap=False)
    _write(db, source, "2026-11-16T17:00:00", "2026-11-16T18:00:00", is_nap=False)
    assert _stored(db, source)[-1] == ("17:00", "18:00", True)

    _write(db, source, "2026-11-16T19:30:00", "2026-11-17T07:00:00", is_nap=False)

    assert _stored(db, source) == [("23:00", "07:00", False), ("17:00", "07:00", False)]
