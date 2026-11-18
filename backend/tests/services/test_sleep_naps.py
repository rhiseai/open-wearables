"""RHISE-7780: which of one source's sessions on a day are naps."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.services.sleep_naps import MAX_SPLIT_GAP, SleepBlock, nap_ids


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
