"""RHISE-7780: Apple sessions outside the day's main sleep are stored as naps.

Apple Health has no nap flag, so every session used to land as main sleep and
the daily summary summed naps into the night. These go through the write path
the SDK uses (``create_or_merge_sleep``) and read the daily summary back.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from logging import getLogger
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord
from app.schemas.enums import ProviderName
from app.schemas.model_crud.activities import EventRecordCreate, EventRecordDetailCreate
from app.schemas.responses.activity import SleepSummary
from app.services.event_record_service import event_record_service
from app.services.sleep_naps import sleep_gap_minutes
from app.services.summaries_service import SummariesService
from tests.factories import DataSourceFactory, UserFactory


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def _write(db: Session, source: DataSource, start: str, end: str, sleep_minutes: int) -> EventRecord:
    """One SDK session, as the Apple path hands it to ``create_or_merge_sleep``."""
    begin, finish = _at(start), _at(end)
    window = int((finish - begin).total_seconds() // 60)
    record = EventRecordCreate(
        id=uuid4(),
        external_id=f"apple-{uuid4()}",
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
        duration_seconds=window * 60,
    )
    detail = EventRecordDetailCreate(
        record_id=record.id,
        sleep_total_duration_minutes=sleep_minutes,
        sleep_time_in_bed_minutes=window,
        sleep_light_minutes=sleep_minutes - 60,
        sleep_deep_minutes=30,
        sleep_rem_minutes=30,
        sleep_awake_minutes=window - sleep_minutes,
        sleep_efficiency_score=Decimal(round(sleep_minutes / window * 100)),
        is_nap=False,
    )
    return event_record_service.create_or_merge_sleep(
        db,
        source.user_id,
        record,
        detail,
        sleep_gap_minutes(source.provider),
    )


def _naps(db: Session, source: DataSource) -> dict[str, bool]:
    records = db.query(EventRecord).filter(EventRecord.data_source_id == source.id).all()
    for record in records:
        db.refresh(record)
    return {record.start_datetime.strftime("%H:%M"): bool(record.sleep_detail.is_nap) for record in records}


def _summary(db: Session, source: DataSource, day: str) -> SleepSummary:
    start = _at(f"{day}T00:00:00")
    result = SummariesService(log=getLogger(__name__)).get_sleep_summaries(
        db, source.user_id, start, start + timedelta(days=1), cursor=None, limit=10
    )
    (summary,) = result.data
    return summary


def test_a_night_split_by_a_long_wake_with_an_afternoon_nap(db: Session) -> None:
    """An Apple multi-session day: 2h17 awake at night, then a nap after lunch."""
    source = DataSourceFactory(provider=ProviderName.APPLE)
    _write(db, source, "2026-11-16T00:20:00", "2026-11-16T02:48:00", 135)
    _write(db, source, "2026-11-16T05:05:00", "2026-11-16T08:59:00", 220)
    _write(db, source, "2026-11-16T13:26:00", "2026-11-16T15:53:00", 140)

    assert _naps(db, source) == {"00:20": True, "05:05": False, "13:26": True}
    summary = _summary(db, source, "2026-11-16")
    assert summary.duration_minutes == 220
    assert summary.nap_count == 2
    assert summary.nap_duration_minutes == 148 + 147
    assert summary.total_duration_minutes == 220 + 148 + 147
    assert summary.stages is not None
    assert summary.stages.deep_minutes == 30
    assert (summary.start_time, summary.end_time) == (_at("2026-11-16T05:05:00"), _at("2026-11-16T08:59:00"))


def test_an_afternoon_nap_after_a_full_night(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.APPLE)
    _write(db, source, "2026-11-15T23:00:00", "2026-11-16T07:00:00", 450)
    _write(db, source, "2026-11-16T14:00:00", "2026-11-16T15:00:00", 55)

    assert _naps(db, source) == {"23:00": False, "14:00": True}
    summary = _summary(db, source, "2026-11-16")
    assert summary.duration_minutes == 450
    assert summary.nap_count == 1
    assert summary.nap_duration_minutes == 60


def test_the_main_sleep_moves_to_a_longer_session_that_arrives_later(db: Session) -> None:
    """The night shift behind RHISE-7667: the dawn block syncs first, the day block after."""
    source = DataSourceFactory(provider=ProviderName.APPLE)
    first = _write(db, source, "2026-11-14T03:25:00", "2026-11-14T05:38:00", 130)
    db.refresh(first)
    assert first.sleep_detail.is_nap is False

    _write(db, source, "2026-11-14T08:36:00", "2026-11-14T14:37:00", 341)

    assert _naps(db, source) == {"03:25": True, "08:36": False}
    summary = _summary(db, source, "2026-11-14")
    assert summary.duration_minutes == 341
    assert summary.nap_count == 1


def test_an_hour_awake_is_still_one_night(db: Session) -> None:
    """Apple sessions less than 90 minutes apart merge into one record, as before."""
    source = DataSourceFactory(provider=ProviderName.APPLE)
    _write(db, source, "2026-11-15T23:00:00", "2026-11-16T02:00:00", 170)
    _write(db, source, "2026-11-16T03:00:00", "2026-11-16T07:00:00", 220)

    assert _naps(db, source) == {"23:00": False}
    assert _summary(db, source, "2026-11-16").nap_count == 0


def test_an_hour_and_three_quarters_awake_splits_off_a_nap(db: Session) -> None:
    """A 1h45 wake used to stay inside the night (OW merged Apple records up to 2 hours apart)."""
    source = DataSourceFactory(provider=ProviderName.APPLE)
    _write(db, source, "2026-11-15T23:00:00", "2026-11-16T02:00:00", 170)
    _write(db, source, "2026-11-16T03:45:00", "2026-11-16T07:00:00", 190)

    assert _naps(db, source) == {"23:00": True, "03:45": False}
    summary = _summary(db, source, "2026-11-16")
    assert summary.duration_minutes == 190
    assert summary.nap_count == 1


def test_other_providers_still_merge_up_to_two_hours(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.OURA, source="oura")
    _write(db, source, "2026-11-15T23:00:00", "2026-11-16T02:00:00", 170)
    _write(db, source, "2026-11-16T03:45:00", "2026-11-16T07:00:00", 190)

    assert _naps(db, source) == {"23:00": False}


def test_another_apple_device_is_marked_on_its_own(db: Session) -> None:
    user = UserFactory()
    watch = DataSourceFactory(user=user, provider=ProviderName.APPLE, device_type="watch")
    phone = DataSourceFactory(user=user, provider=ProviderName.APPLE, device_type="phone")
    _write(db, watch, "2026-11-15T23:00:00", "2026-11-16T07:00:00", 450)
    _write(db, phone, "2026-11-16T14:00:00", "2026-11-16T15:00:00", 55)

    assert _naps(db, watch) == {"23:00": False}
    assert _naps(db, phone) == {"14:00": False}


def test_other_providers_keep_their_own_nap_flag(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.OURA, source="oura")
    _write(db, source, "2026-11-15T23:00:00", "2026-11-16T07:00:00", 450)
    _write(db, source, "2026-11-16T14:00:00", "2026-11-16T15:00:00", 55)

    assert _naps(db, source) == {"23:00": False, "14:00": False}
