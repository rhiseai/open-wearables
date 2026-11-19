"""RHISE-7780: sessions outside the day's main sleep are stored as naps, for every source whose sleep goes
through create_or_merge_sleep.

Apple Health has no nap flag, so every session used to land as main sleep and
the daily summary summed naps into the night. The first tests check the rule in
app.services.sleep_nap_marking; the rest go through the write path the SDK uses
(``create_or_merge_sleep``) and read the daily summary back.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord
from app.schemas.enums import ProviderName
from app.schemas.model_crud.activities import EventRecordCreate, EventRecordDetailCreate
from app.schemas.responses.activity import SleepSummary
from app.services.event_record_service import event_record_service
from app.services.sleep_nap_marking import MAX_SPLIT_GAP, SleepBlock, nap_ids, sleep_gap_minutes
from app.services.summaries_service import SummariesService
from tests.factories import DataSourceFactory, UserFactory


def _block(start: str, end: str, minutes: float) -> SleepBlock:
    return SleepBlock(
        id=uuid4(),
        start=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        end=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
        sleep_minutes=minutes,
    )


def _ids(*blocks: SleepBlock) -> set[UUID]:
    return {block.id for block in blocks}


def test_one_session_is_never_a_nap() -> None:
    assert nap_ids([_block("2026-11-15T23:00", "2026-11-16T07:00", 450)]) == set()


def test_no_sessions_no_naps() -> None:
    assert nap_ids([]) == set()


def test_a_night_shift_keeps_the_longer_sleep() -> None:
    # The prod case behind RHISE-7667: 2h10 at dawn, 5h41 from mid-morning.
    dawn = _block("2026-11-14T03:25", "2026-11-14T05:38", 130)
    day = _block("2026-11-14T08:36", "2026-11-14T14:37", 341)

    assert nap_ids([day, dawn]) == _ids(dawn)


def test_a_long_wake_and_an_afternoon_sleep_are_naps() -> None:
    # 2h17 awake after the first block, 4h27 before the last one.
    first = _block("2026-11-16T00:20", "2026-11-16T02:48", 135)
    night = _block("2026-11-16T05:05", "2026-11-16T08:59", 220)
    afternoon = _block("2026-11-16T13:26", "2026-11-16T15:53", 140)

    assert nap_ids([first, night, afternoon]) == _ids(first, afternoon)


def test_blocks_less_than_ninety_minutes_apart_are_one_sleep() -> None:
    # 85 minutes awake after a short first sleep: the two count together and
    # beat the single longer block later in the day.
    short = _block("2026-11-15T22:00", "2026-11-15T22:40", 30)
    rest = _block("2026-11-16T00:05", "2026-11-16T04:00", 200)
    later = _block("2026-11-16T09:00", "2026-11-16T12:45", 220)

    assert nap_ids([later, short, rest]) == _ids(later)


def test_exactly_ninety_minutes_apart_is_a_separate_sleep() -> None:
    night = _block("2026-11-15T23:00", "2026-11-16T05:00", 340)
    end = datetime.fromisoformat("2026-11-16T05:00").replace(tzinfo=timezone.utc)
    start = end + MAX_SPLIT_GAP
    after = SleepBlock(id=uuid4(), start=start, end=start + timedelta(hours=1), sleep_minutes=55)

    assert nap_ids([night, after]) == _ids(after)


def test_a_tie_keeps_the_earlier_sleep() -> None:
    early = _block("2026-11-16T00:00", "2026-11-16T03:00", 170)
    late = _block("2026-11-16T06:00", "2026-11-16T09:00", 170)

    assert nap_ids([late, early]) == _ids(late)


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def _write(
    db: Session,
    source: DataSource,
    start: str,
    end: str,
    sleep_minutes: int,
    *,
    is_nap: bool = False,
    external_id: str | None = None,
) -> EventRecord:
    """One session, as a provider path hands it to ``create_or_merge_sleep``."""
    begin, finish = _at(start), _at(end)
    window = int((finish - begin).total_seconds() // 60)
    record = EventRecordCreate(
        id=uuid4(),
        external_id=external_id or f"{source.provider}-{uuid4()}",
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
        is_nap=is_nap,
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
    result = SummariesService().get_sleep_summaries(
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


@pytest.mark.parametrize(
    ("provider", "source_name"), [(ProviderName.APPLE, "apple_health_sdk"), (ProviderName.WITHINGS, "withings")]
)
def test_a_nap_sent_again_unchanged_is_not_announced(db: Session, provider: ProviderName, source_name: str) -> None:
    """Every SDK batch writes the open Apple sessions again, and Withings re-sends its sessions: a source
    without a nap flag sends the nap the rule made as main sleep, and that unchanged nap is no sleep.updated."""
    source = DataSourceFactory(provider=provider, source=source_name)
    _write(db, source, "2026-11-15T23:00:00", "2026-11-16T07:00:00", 450)
    _write(db, source, "2026-11-16T14:00:00", "2026-11-16T15:00:00", 55, external_id="resent-nap")

    with (
        patch("app.services.event_record_service.on_sleep_created") as created,
        patch("app.services.event_record_service.on_sleep_updated") as updated,
    ):
        _write(db, source, "2026-11-16T14:00:00", "2026-11-16T15:00:00", 55, external_id="resent-nap")

    assert _naps(db, source) == {"23:00": False, "14:00": True}
    created.assert_not_called()
    updated.assert_not_called()


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


def test_an_oura_afternoon_sleep_sent_as_sleep_becomes_a_nap(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.OURA, source="oura")
    _write(db, source, "2026-11-15T23:00:00", "2026-11-16T07:00:00", 450)
    _write(db, source, "2026-11-16T14:00:00", "2026-11-16T15:00:00", 55)

    assert _naps(db, source) == {"23:00": False, "14:00": True}


def test_whoop_sleep_before_a_long_wake_is_a_nap_as_whoop_shows_it(db: Session) -> None:
    """A staging WHOOP night (Nov 3): the WHOOP app lists 23:24-00:54 as a nap, its API sends it as a sleep."""
    source = DataSourceFactory(provider=ProviderName.WHOOP, source="whoop")
    _write(db, source, "2026-11-02T23:24:00", "2026-11-03T00:54:00", 75)
    _write(db, source, "2026-11-03T05:05:00", "2026-11-03T09:26:00", 248)

    assert _naps(db, source) == {"23:24": True, "05:05": False}
    summary = _summary(db, source, "2026-11-03")
    assert summary.duration_minutes == 248
    assert summary.nap_count == 1
    assert summary.total_duration_minutes == 248 + 90


def test_whoop_night_that_syncs_after_its_nap(db: Session) -> None:
    """A staging WHOOP night (Oct 24): the short sleep syncs first and was stored as the night."""
    source = DataSourceFactory(provider=ProviderName.WHOOP, source="whoop")
    _write(db, source, "2026-10-23T23:27:00", "2026-10-24T00:34:00", 64)
    assert _naps(db, source) == {"23:27": False}

    _write(db, source, "2026-10-24T02:58:00", "2026-10-24T09:02:00", 341)

    assert _naps(db, source) == {"23:27": True, "02:58": False}


def test_a_nap_the_source_flagged_stays_a_nap_and_never_wins_the_night(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.OURA, source="oura")
    _write(db, source, "2026-11-15T23:00:00", "2026-11-16T02:00:00", 170)
    _write(db, source, "2026-11-16T12:00:00", "2026-11-16T16:00:00", 230, is_nap=True)

    assert _naps(db, source) == {"23:00": False, "12:00": True}


def test_a_resent_marked_nap_is_judged_again(db: Session) -> None:
    source = DataSourceFactory(provider=ProviderName.WHOOP, source="whoop")
    _write(db, source, "2026-11-02T23:24:00", "2026-11-03T00:54:00", 75, external_id="whoop-early")
    _write(db, source, "2026-11-03T05:05:00", "2026-11-03T09:26:00", 248)
    _write(db, source, "2026-11-02T23:24:00", "2026-11-03T00:54:00", 75, external_id="whoop-early")

    assert _naps(db, source) == {"23:24": True, "05:05": False}
