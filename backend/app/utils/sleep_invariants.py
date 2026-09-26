"""Physical invariants every stored night of sleep must satisfy."""

from collections.abc import Iterable

MAX_NIGHT_SLEEP_MINUTES = 16 * 60


class SleepInvariantViolationError(Exception):
    """A night of sleep that cannot be real; the caller must not store it."""

    def __init__(self, violations: list[str], values: dict) -> None:
        super().__init__(", ".join(violations))
        self.violations = violations
        self.values = values


def sleep_invariant_violations(
    *,
    total_sleep_minutes: int | None,
    time_in_bed_minutes: int | None,
    stage_minutes: Iterable[int | None] = (),
) -> list[str]:
    """Names of the invariants a night breaks; empty when it is plausible.

    ``stage_minutes`` are the asleep stages (light, deep, rem and unstaged
    sleep), never awake.
    """
    if total_sleep_minutes is None:
        return []

    violations: list[str] = []
    if time_in_bed_minutes is not None and total_sleep_minutes > time_in_bed_minutes:
        violations.append("total_sleep_exceeds_time_in_bed")
    if total_sleep_minutes > MAX_NIGHT_SLEEP_MINUTES:
        violations.append("total_sleep_exceeds_16h")
    if sum(m or 0 for m in stage_minutes) > total_sleep_minutes:
        violations.append("stages_exceed_total_sleep")
    return violations
