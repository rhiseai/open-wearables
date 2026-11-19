"""Which of one source's sleep sessions on one day are naps -- pure, no I/O.

Apple Health has no nap flag: every session the SDK writes is stored as main
sleep, so the daily summary sums an afternoon nap, or the second half of a
night broken by a long wake, into the night's duration, stages, efficiency
and heart rate window (RHISE-7780). Cloud sources have the same gap: WHOOP
sends a short sleep before the night as an ordinary sleep, though its own app
shows it as a nap.

The rule: sessions less than :data:`MAX_SPLIT_GAP` apart are one sleep. The
chain with the most sleep is the day's main sleep; every other chain is a nap.
The summary then keeps naps out of the night's numbers and reports them as
``nap_count`` / ``nap_duration_minutes``.

It replaces upstream's own nap rule (the 06:00-20:00 window and its startup
backfill), so the fork has one nap rule only. It runs on every sleep write that
goes through ``EventRecordService.create_or_merge_sleep``, on Sensor Bio's
writes and, for a source with no nap flag, on a sleep deleted through
``EventRecordService.delete_event_record``; WHOOP's ``sleep.deleted`` webhook
deletes through ``EventRecordRepository.delete_by_external_id`` without it.
Garmin's sleep is stored without it, and its flags stay as Garmin sends them.

A source in :data:`NO_NAP_FLAG_PROVIDERS` sends no nap flag, so the rule marks
its sessions both ways: a nap it made stops being one when the day's main sleep
changes. Any other source keeps the naps it flags; they take no part in choosing
the main sleep, and the rule only adds naps to them (see :func:`flags_own_naps`).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from app.config import settings
from app.constants.sdk_providers import SDK_PROVIDERS
from app.schemas.enums import ProviderName

# An hour and a half awake still belongs to the same sleep: people can lie
# awake that long trying to fall asleep again. A longer gap starts a separate
# sleep (product decision on RHISE-7667, as WHOOP splits them).
MAX_SPLIT_GAP = timedelta(minutes=90)

# Their sleep comes without a nap flag: the mobile SDK (Apple, Samsung, Health
# Connect) has none, and Sensor Bio, Ultrahuman and Withings send none. Polar,
# which sends none either, is left out (see flags_own_naps).
NO_NAP_FLAG_PROVIDERS: frozenset[str] = frozenset(
    {
        *SDK_PROVIDERS,
        ProviderName.SENSORBIO.value,
        ProviderName.ULTRAHUMAN.value,
        ProviderName.WITHINGS.value,
    }
)


def flags_own_naps(provider: str | None) -> bool:
    """Whether a source's sleep arrives with a nap flag of its own.

    A source in :data:`NO_NAP_FLAG_PROVIDERS` sends none, so every stored nap
    flag of its is the rule's and is decided again both ways on each write. Any
    other source keeps the naps it flags, and the rule only adds naps to them.
    Polar sends no nap flag either but keeps one sleep per date, so the rule
    never finds a nap among its sessions and it is left out of the set.
    """
    return provider not in NO_NAP_FLAG_PROVIDERS


def sleep_gap_minutes(provider: str | None) -> int:
    """Minutes without sleep data after which a source's raw samples start a new sleep.

    Apple's samples end a sleep after :data:`MAX_SPLIT_GAP`, the same gap that
    splits a day into its main sleep and naps; with the general
    ``sleep_end_gap_minutes`` (120) a 90-120 minute wake was stored inside the
    night and could never become a nap. Other sources keep the general gap.
    """
    if provider == ProviderName.APPLE:
        return int(MAX_SPLIT_GAP.total_seconds() // 60)
    return settings.sleep_end_gap_minutes


@dataclass(frozen=True)
class SleepBlock:
    """One stored sleep session: its window and minutes of sleep."""

    id: UUID
    start: datetime
    end: datetime
    sleep_minutes: float


def nap_ids(blocks: Sequence[SleepBlock]) -> set[UUID]:
    """The sessions outside the day's main sleep.

    Sessions are chained in start order while each starts less than
    :data:`MAX_SPLIT_GAP` after the chain's latest end. The chain with the
    most sleep is the main sleep (the earlier one on a tie); the sessions of
    every other chain are naps.
    """
    if len(blocks) < 2:
        return set()
    ordered = sorted(blocks, key=lambda block: (block.start, block.end, str(block.id)))
    chains: list[list[SleepBlock]] = [[ordered[0]]]
    chain_end = ordered[0].end
    for block in ordered[1:]:
        if block.start - chain_end >= MAX_SPLIT_GAP:
            chains.append([block])
            chain_end = block.end
            continue
        chains[-1].append(block)
        chain_end = max(chain_end, block.end)
    main = max(chains, key=lambda chain: sum(block.sleep_minutes for block in chain))
    return {block.id for chain in chains if chain is not main for block in chain}
