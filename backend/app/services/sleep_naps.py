"""Which of one source's sleep sessions on one day are naps -- pure, no I/O.

Apple Health has no nap flag: every session the SDK writes is stored as main
sleep, so the daily summary sums an afternoon nap, or the second half of a
night broken by a long wake, into the night's duration, stages, efficiency
and heart rate window (RHISE-7780).

The rule: sessions less than :data:`MAX_SPLIT_GAP` apart are one sleep. The
chain with the most sleep is the day's main sleep; every other chain is a nap.
The summary then keeps naps out of the night's numbers and reports them as
``nap_count`` / ``nap_duration_minutes``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

# An hour and a half awake still belongs to the same sleep: people can lie
# awake that long trying to fall asleep again. A longer gap starts a separate
# sleep (product decision on RHISE-7667, as WHOOP splits them).
MAX_SPLIT_GAP = timedelta(minutes=90)


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
