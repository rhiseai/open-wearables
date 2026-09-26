"""Build one night of sleep from raw HealthKit sleep samples.

HealthKit is a shared store: several apps write sleep for the same night (the
Apple Watch, the Oura app, a sleep tracking app), and a single app often writes
overlapping samples, e.g. Oura writes an umbrella ``asleepUnspecified`` interval
plus ``asleepCore/Deep/REM`` stage samples over the same minutes. Summing stage
seconds therefore over-counts. A night is built from intervals instead:

* One source per night. Samples are grouped by writer (bundle id, else source
  name); the source with stage data wins, else the one with the most sleep.
  Sources are never merged into one total.
* Every instant covered by a sample of that source lands in exactly one bucket:
  a ``light``/``deep``/``rem`` sample wins (earliest-starting sample on ties),
  then ``awake``, then ``sleeping`` (so the umbrella only counts where no stage
  sample covers it), then ``in_bed``.
* ``time_in_bed`` is the union of all the source's samples, total sleep is the
  union of asleep-type time, so total sleep <= time in bed and the stage minutes
  add up to total sleep by construction.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.constants.sleep import SleepStageType
from app.schemas.model_crud.activities import SleepStage
from app.schemas.providers.mobile_sdk import SleepStateStage, SourceInfo

UNKNOWN_SOURCE = "unknown"

# The iPhone writes as ``com.apple.health`` and each paired Watch as
# ``com.apple.health.<uuid>``. They are one sleep system (iPhone bedtime in_bed
# plus Watch stages), not competing sources.
APPLE_HEALTH_BUNDLE = "com.apple.health"

_DETAILED_STAGES = frozenset({SleepStageType.LIGHT, SleepStageType.DEEP, SleepStageType.REM})
_ASLEEP_STAGES = _DETAILED_STAGES | {SleepStageType.SLEEPING}

# Lower wins when samples of one source overlap.
_BUCKET_PRIORITY: dict[SleepStageType, int] = {
    SleepStageType.LIGHT: 0,
    SleepStageType.DEEP: 0,
    SleepStageType.REM: 0,
    SleepStageType.AWAKE: 1,
    SleepStageType.SLEEPING: 2,
    SleepStageType.IN_BED: 3,
}

_STAGE_TO_METRIC: dict[SleepStageType, str] = {
    SleepStageType.AWAKE: "awake_seconds",
    SleepStageType.SLEEPING: "sleeping_seconds",
    SleepStageType.LIGHT: "light_seconds",
    SleepStageType.DEEP: "deep_seconds",
    SleepStageType.REM: "rem_seconds",
}


def sleep_source_key(source: SourceInfo | None) -> str | None:
    """Stable identity of the HealthKit writer of a sample."""
    if source is None:
        return None
    bundle = source.bundle_identifier or source.app_id
    if bundle:
        if bundle == APPLE_HEALTH_BUNDLE or bundle.startswith(f"{APPLE_HEALTH_BUNDLE}."):
            return APPLE_HEALTH_BUNDLE
        return bundle
    return source.name or None


@dataclass(frozen=True)
class SleepNight:
    """One night built from a single source's samples."""

    metrics: dict[str, float]
    stages: list[SleepStage]
    source_key: str | None = None
    source_name: str | None = None
    device_model: str | None = None
    discarded_sources: list[str] = field(default_factory=list)
    # Raw summed sample seconds per source and stage, before any union: what the
    # night looked like on the wire, for debugging a rejected night.
    raw_buckets: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def total_sleep_seconds(self) -> float:
        m = self.metrics
        return m["sleeping_seconds"] + m["light_seconds"] + m["deep_seconds"] + m["rem_seconds"]

    @property
    def time_in_bed_seconds(self) -> float:
        return self.metrics["in_bed_seconds"]


def _empty_metrics() -> dict[str, float]:
    return {
        "in_bed_seconds": 0.0,
        "awake_seconds": 0.0,
        "sleeping_seconds": 0.0,
        "light_seconds": 0.0,
        "deep_seconds": 0.0,
        "rem_seconds": 0.0,
    }


def _timeline(samples: Sequence[SleepStateStage]) -> tuple[dict[str, float], list[SleepStage]]:
    """Sweep one source's samples into disjoint buckets.

    Returns the per-bucket seconds and the hypnogram (every bucket except in_bed,
    adjacent segments of the same stage joined).
    """
    metrics = _empty_metrics()
    valid = [s for s in samples if s.stage in _BUCKET_PRIORITY and s.end_time > s.start_time]
    if not valid:
        return metrics, []

    # Phone-only or legacy nights with nothing but in_bed: the bed time is the
    # only sleep signal there is.
    if not any(s.stage in _ASLEEP_STAGES for s in valid):
        valid = [
            s.model_copy(update={"stage": SleepStageType.SLEEPING}) if s.stage == SleepStageType.IN_BED else s
            for s in valid
        ]

    ordered = sorted(valid, key=lambda s: (s.start_time, s.end_time))
    boundaries = sorted({s.start_time for s in ordered} | {s.end_time for s in ordered})

    segments: list[SleepStage] = []
    active: list[SleepStateStage] = []
    next_idx = 0
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        while next_idx < len(ordered) and ordered[next_idx].start_time <= left:
            active.append(ordered[next_idx])
            next_idx += 1
        active = [s for s in active if s.end_time > left]
        if not active:
            continue

        winner = min(active, key=lambda s: (_BUCKET_PRIORITY[s.stage], s.start_time))
        seconds = (right - left).total_seconds()
        metrics["in_bed_seconds"] += seconds
        metric_key = _STAGE_TO_METRIC.get(winner.stage)
        if metric_key is None:
            continue
        metrics[metric_key] += seconds

        if segments and segments[-1].stage == winner.stage and segments[-1].end_time == left:
            segments[-1] = segments[-1].model_copy(update={"end_time": right})
        else:
            segments.append(SleepStage(stage=winner.stage, start_time=left, end_time=right))

    return metrics, segments


def _raw_buckets(samples: Sequence[SleepStateStage]) -> dict[str, float]:
    buckets: dict[str, float] = defaultdict(float)
    for s in samples:
        buckets[str(s.stage)] += max((s.end_time - s.start_time).total_seconds(), 0.0)
    return dict(buckets)


def _first_attr(samples: Sequence[SleepStateStage], attr: str) -> str | None:
    # Prefer the device that recorded the stages (the Watch) over the one that
    # only wrote in_bed (the iPhone).
    for s in sorted(samples, key=lambda s: (s.stage not in _DETAILED_STAGES, s.start_time)):
        value = getattr(s, attr)
        if value:
            return value
    return None


def build_sleep_night(samples: Sequence[SleepStateStage]) -> SleepNight:
    """Build a night from raw samples, choosing one HealthKit source."""
    groups: dict[str, list[SleepStateStage]] = defaultdict(list)
    for s in samples:
        if s.stage == SleepStageType.UNKNOWN:
            continue
        groups[s.source_key or UNKNOWN_SOURCE].append(s)

    if not groups:
        return SleepNight(metrics=_empty_metrics(), stages=[])

    built = {key: _timeline(group) for key, group in groups.items()}

    def rank(key: str) -> tuple[bool, float, float, str]:
        metrics = built[key][0]
        has_stages = any(s.stage in _DETAILED_STAGES for s in groups[key])
        asleep = sum(metrics[k] for k in ("sleeping_seconds", "light_seconds", "deep_seconds", "rem_seconds"))
        return has_stages, asleep, metrics["in_bed_seconds"], key

    chosen = max(groups, key=rank)
    metrics, stages = built[chosen]
    chosen_samples = groups[chosen]
    return SleepNight(
        metrics=metrics,
        stages=stages,
        source_key=None if chosen == UNKNOWN_SOURCE else chosen,
        source_name=_first_attr(chosen_samples, "source_name"),
        device_model=_first_attr(chosen_samples, "device_model"),
        discarded_sources=sorted(key for key in groups if key != chosen),
        raw_buckets={key: _raw_buckets(group) for key, group in groups.items()},
    )
