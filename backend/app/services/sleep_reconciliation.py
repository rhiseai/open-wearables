"""Deterministic interval reconciliation for fragments of one sleep writer."""

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from app.schemas.model_crud.activities import EventRecordDetailCreate, SleepStage

SleepPart = tuple[datetime, datetime, EventRecordDetailCreate]

_STAGE_FIELDS = {
    "deep": "sleep_deep_minutes",
    "light": "sleep_light_minutes",
    "rem": "sleep_rem_minutes",
    "awake": "sleep_awake_minutes",
}
_MINUTE_FIELDS = (*_STAGE_FIELDS.values(), "sleep_total_duration_minutes")
_ASLEEP_STAGES = {"deep", "light", "rem", "sleeping"}


def merge_sleep_stages(stages: Sequence[SleepStage]) -> list[SleepStage]:
    """Deduplicate and clip overlaps using stable chronological precedence.

    This keeps the existing merge rule that an earlier segment owns an overlap;
    equal starts use end time and stage as deterministic tie breakers.
    """
    unique = {(s.start_time, s.end_time, str(s.stage)): s for s in stages if s.end_time > s.start_time}
    cleaned: list[SleepStage] = []
    for _, stage in sorted(unique.items()):
        start = max(stage.start_time, cleaned[-1].end_time) if cleaned else stage.start_time
        if start >= stage.end_time:
            continue
        cleaned.append(stage.model_copy(update={"start_time": start}))
    return cleaned


def merge_sleep_parts(parts: Sequence[SleepPart]) -> SleepPart:
    """Merge cumulative snapshots without adding a contained night twice.

    Detailed overlapping records derive their sleep minutes from the union of
    stage intervals. Stage-less providers keep their established aggregate
    semantics; a contained/equal snapshot contributes the maximum, not a sum.
    Separate fragments still sum time in bed and exclude their inter-session gap.
    """
    start, end, detail = parts[0]
    stages = merge_sleep_stages(detail.sleep_stages or [])
    detail = detail.model_copy(update={"sleep_stages": stages or None})
    for next_start, next_end, incoming in parts[1:]:
        # Some legacy rows have aggregate sleep that their stored hypnogram
        # cannot explain. A partial batch cannot disprove that existing sleep.
        # Carry it as a conservative floor, without inventing missing intervals.
        opaque_floor = max(
            (
                part.sleep_total_duration_minutes or 0
                if (part.sleep_total_duration_minutes or 0)
                > int(
                    sum(
                        (stage.end_time - stage.start_time).total_seconds()
                        for stage in merge_sleep_stages(part.sleep_stages or [])
                        if str(stage.stage) in _ASLEEP_STAGES
                    )
                    // 60
                )
                else 0
            )
            for part in (detail, incoming)
        )
        overlap = min(end, next_end) > max(start, next_start)
        contained = (start <= next_start and end >= next_end) or (next_start <= start and next_end >= end)
        merged_start, merged_end = min(start, next_start), max(end, next_end)
        fields: dict = {}
        for field in _MINUTE_FIELDS:
            old, new = getattr(detail, field), getattr(incoming, field)
            values = [value for value in (old, new) if value is not None]
            fields[field] = (max(values) if contained else sum(values)) if values else None

        old_in_bed, new_in_bed = detail.sleep_time_in_bed_minutes or 0, incoming.sleep_time_in_bed_minutes or 0
        in_bed = max(old_in_bed, new_in_bed) if contained else old_in_bed + new_in_bed
        if overlap:
            in_bed = min(in_bed, int((merged_end - merged_start).total_seconds() // 60))
        fields["sleep_time_in_bed_minutes"] = in_bed

        stages = merge_sleep_stages([*stages, *(incoming.sleep_stages or [])])
        if overlap and stages:
            seconds: dict[str, float] = defaultdict(float)
            for stage in stages:
                seconds[str(stage.stage)] += (stage.end_time - stage.start_time).total_seconds()
            for stage_name, field in _STAGE_FIELDS.items():
                fields[field] = int(seconds[stage_name] // 60)
            fields["sleep_total_duration_minutes"] = int(
                sum(seconds[stage_name] for stage_name in _ASLEEP_STAGES) // 60
            )
            fields["sleep_total_duration_minutes"] = max(fields["sleep_total_duration_minutes"], opaque_floor)
            # Being inside the outer window does not mean a batch is a replay:
            # an out-of-order fragment may fill a previously empty split-night
            # gap. The known asleep+awake coverage must fit in time in bed.
            in_bed = max(in_bed, fields["sleep_total_duration_minutes"] + fields["sleep_awake_minutes"])
            fields["sleep_time_in_bed_minutes"] = in_bed

        cumulative_replay = (
            start <= next_start
            and end >= next_end
            and in_bed == old_in_bed
            and all(fields[field] == getattr(detail, field) for field in _MINUTE_FIELDS)
        )
        if cumulative_replay:
            efficiency = detail.sleep_efficiency_score
        elif overlap and stages and in_bed:
            efficiency = Decimal(str(round(fields["sleep_total_duration_minutes"] / in_bed * 100, 2)))
        elif contained:
            # An unchanged subset must not drift the cumulative night's score.
            efficiency = (
                detail.sleep_efficiency_score
                if start <= next_start and end >= next_end
                else incoming.sleep_efficiency_score
            )
        else:
            weighted = [
                (part.sleep_efficiency_score, minutes)
                for part, minutes in ((detail, old_in_bed), (incoming, new_in_bed))
                if part.sleep_efficiency_score is not None and minutes > 0
            ]
            efficiency = (
                Decimal(
                    str(
                        round(
                            sum(float(score) * minutes for score, minutes in weighted)
                            / sum(minutes for _, minutes in weighted),
                            2,
                        )
                    )
                )
                if weighted
                else None
            )
        fields.update(
            sleep_efficiency_score=efficiency,
            sleep_stages=stages or None,
            is_nap=bool(detail.is_nap) and bool(incoming.is_nap),
        )
        start, end = merged_start, merged_end
        detail = detail.model_copy(update=fields)
    return start, end, detail
