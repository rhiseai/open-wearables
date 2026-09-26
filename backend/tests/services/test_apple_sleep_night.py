"""Night building from raw HealthKit sleep samples (RHISE-4202).

Fixtures model what different writers put into Apple Health for one night:

- Oura: an umbrella ``sleeping`` (asleepUnspecified) interval plus ``light``/
  ``deep``/``rem``/``awake`` samples over the same minutes. Summing every sample
  reads as about twice the night.
- Apple Watch: stage samples only, plus the iPhone's bedtime ``in_bed``.
- iPhone only: ``in_bed`` plus ``sleeping``.
- Two writers on one night.
- A split night: two sleep blocks inside one session, and a nap.
"""

from datetime import datetime, timedelta

import pytest

from app.constants.sleep import SleepStageType
from app.schemas.providers.mobile_sdk import SleepStateStage, SourceInfo
from app.services.sdk.sleep_night import build_sleep_night, sleep_source_key
from app.utils.sleep_invariants import sleep_invariant_violations

OURA = "com.ouraring.oura"
AUTOSLEEP = "com.tantsissa.AutoSleep"
WATCH = "com.apple.health.3F2A6C1E-0000-4000-8000-000000000001"
IPHONE = "com.apple.health"


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _s(stage: str, start: str, end: str, source: str | None = None) -> SleepStateStage:
    return SleepStateStage(
        stage=SleepStageType(stage),
        start_time=_dt(start),
        end_time=_dt(end),
        source_key=sleep_source_key(SourceInfo(bundle_identifier=source)) if source else None,
        source_name=source,
    )


def _minutes(seconds: float) -> int:
    return int(seconds // 60)


def _raw_sum_minutes(samples: list[SleepStateStage], stages: set[str]) -> int:
    return _minutes(sum((s.end_time - s.start_time).total_seconds() for s in samples if s.stage in stages))


def _assert_invariants(night_metrics: dict, total: float, in_bed: float) -> None:
    stages = [night_metrics[k] for k in ("light_seconds", "deep_seconds", "rem_seconds", "sleeping_seconds")]
    assert (
        sleep_invariant_violations(
            total_sleep_minutes=_minutes(total),
            time_in_bed_minutes=_minutes(in_bed),
            stage_minutes=[_minutes(s) for s in stages],
        )
        == []
    )


# Oura night modelled on RHISE-4198: in bed 23:03 to 05:17 (6h14).
OURA_NIGHT = [
    _s("in_bed", "2026-09-23T23:03:00Z", "2026-09-24T05:17:00Z", OURA),
    _s("sleeping", "2026-09-23T23:15:00Z", "2026-09-24T05:10:00Z", OURA),
    _s("light", "2026-09-23T23:15:00Z", "2026-09-24T00:05:00Z", OURA),
    _s("deep", "2026-09-24T00:05:00Z", "2026-09-24T01:20:00Z", OURA),
    _s("light", "2026-09-24T01:20:00Z", "2026-09-24T02:10:00Z", OURA),
    _s("awake", "2026-09-24T02:10:00Z", "2026-09-24T02:25:00Z", OURA),
    _s("rem", "2026-09-24T02:25:00Z", "2026-09-24T03:20:00Z", OURA),
    _s("light", "2026-09-24T03:20:00Z", "2026-09-24T04:30:00Z", OURA),
    _s("rem", "2026-09-24T04:30:00Z", "2026-09-24T05:10:00Z", OURA),
]


class TestOuraShapedNight:
    def test_naive_sum_reproduces_the_double_count(self) -> None:
        """Adding every asleep sample, umbrella included, reads as about 2x the night."""
        stage_sum = _raw_sum_minutes(OURA_NIGHT, {"light", "deep", "rem"})
        naive = _raw_sum_minutes(OURA_NIGHT, {"sleeping", "light", "deep", "rem"})
        in_bed = 6 * 60 + 14

        assert naive > in_bed
        assert naive / stage_sum == pytest.approx(2.0, abs=0.1)

    def test_total_is_the_stage_sum_not_double(self) -> None:
        night = build_sleep_night(OURA_NIGHT)
        m = night.metrics

        stage_sum = m["light_seconds"] + m["deep_seconds"] + m["rem_seconds"]
        assert _minutes(night.total_sleep_seconds) == _minutes(stage_sum) == 340
        # The umbrella adds nothing where stages cover it; the awake gap is awake.
        assert m["sleeping_seconds"] == 0
        assert _minutes(m["awake_seconds"]) == 15
        assert _minutes(night.time_in_bed_seconds) == 6 * 60 + 14
        _assert_invariants(m, night.total_sleep_seconds, night.time_in_bed_seconds)

    def test_umbrella_counts_only_where_no_stage_covers_it(self) -> None:
        """Umbrella minutes outside every stage sample are real unstaged sleep."""
        samples = [
            _s("sleeping", "2026-09-23T23:00:00Z", "2026-09-24T07:00:00Z", OURA),
            _s("light", "2026-09-23T23:30:00Z", "2026-09-24T03:00:00Z", OURA),
            _s("deep", "2026-09-24T03:00:00Z", "2026-09-24T06:00:00Z", OURA),
        ]
        night = build_sleep_night(samples)
        m = night.metrics

        assert _minutes(m["sleeping_seconds"]) == 30 + 60
        assert _minutes(m["light_seconds"]) == 210
        assert _minutes(m["deep_seconds"]) == 180
        assert _minutes(night.total_sleep_seconds) == 8 * 60
        assert {s.stage for s in night.stages} == {SleepStageType.SLEEPING, SleepStageType.LIGHT, SleepStageType.DEEP}

    def test_duplicate_samples_do_not_add_up(self) -> None:
        """The same night re-sent in a later batch is counted once."""
        night = build_sleep_night(OURA_NIGHT + OURA_NIGHT)
        assert _minutes(night.total_sleep_seconds) == 340

    def test_raw_buckets_keep_the_wire_shape(self) -> None:
        night = build_sleep_night(OURA_NIGHT)
        assert _minutes(night.raw_buckets[OURA]["sleeping"]) == 355
        assert night.source_key == OURA


class TestWatchShapedNight:
    NIGHT = [
        _s("in_bed", "2026-03-10T22:00:00Z", "2026-03-11T06:00:00Z", IPHONE),
        _s("light", "2026-03-10T22:15:00Z", "2026-03-10T23:00:00Z", WATCH),
        _s("deep", "2026-03-10T23:00:00Z", "2026-03-11T00:30:00Z", WATCH),
        _s("rem", "2026-03-11T00:30:00Z", "2026-03-11T01:15:00Z", WATCH),
        _s("awake", "2026-03-11T01:15:00Z", "2026-03-11T01:25:00Z", WATCH),
        _s("deep", "2026-03-11T01:25:00Z", "2026-03-11T02:30:00Z", WATCH),
        _s("light", "2026-03-11T02:30:00Z", "2026-03-11T05:45:00Z", WATCH),
    ]

    def test_stages_only_with_iphone_bedtime(self) -> None:
        """The iPhone bedtime and Watch stages are one Apple sleep system."""
        night = build_sleep_night(self.NIGHT)
        m = night.metrics

        assert night.source_key == IPHONE
        assert night.discarded_sources == []
        assert _minutes(m["light_seconds"]) == 45 + 195
        assert _minutes(m["deep_seconds"]) == 90 + 65
        assert _minutes(m["rem_seconds"]) == 45
        assert _minutes(m["awake_seconds"]) == 10
        assert m["sleeping_seconds"] == 0
        assert _minutes(night.total_sleep_seconds) == 440
        assert _minutes(night.time_in_bed_seconds) == 8 * 60
        _assert_invariants(m, night.total_sleep_seconds, night.time_in_bed_seconds)

    def test_hypnogram_excludes_in_bed(self) -> None:
        night = build_sleep_night(self.NIGHT)
        assert SleepStageType.IN_BED not in {s.stage for s in night.stages}
        assert night.stages[0].start_time == _dt("2026-03-10T22:15:00Z")
        assert night.stages[-1].end_time == _dt("2026-03-11T05:45:00Z")


class TestPhoneOnlyNight:
    def test_in_bed_plus_sleeping(self) -> None:
        samples = [
            _s("in_bed", "2026-04-10T22:30:00Z", "2026-04-11T06:30:00Z", IPHONE),
            _s("sleeping", "2026-04-10T23:00:00Z", "2026-04-11T03:00:00Z", IPHONE),
            _s("sleeping", "2026-04-11T03:20:00Z", "2026-04-11T06:00:00Z", IPHONE),
        ]
        night = build_sleep_night(samples)

        assert _minutes(night.total_sleep_seconds) == 240 + 160
        assert _minutes(night.metrics["sleeping_seconds"]) == 400
        assert _minutes(night.time_in_bed_seconds) == 8 * 60
        _assert_invariants(night.metrics, night.total_sleep_seconds, night.time_in_bed_seconds)

    def test_in_bed_only_is_treated_as_sleep(self) -> None:
        night = build_sleep_night([_s("in_bed", "2026-04-10T22:30:00Z", "2026-04-11T06:00:00Z", IPHONE)])

        assert night.metrics["sleeping_seconds"] == 7.5 * 3600
        assert night.time_in_bed_seconds == 7.5 * 3600
        assert [s.stage for s in night.stages] == [SleepStageType.SLEEPING]


class TestTwoSourcesOneNight:
    def test_source_with_stages_wins_and_sources_are_not_merged(self) -> None:
        autosleep = [
            _s("in_bed", "2026-09-23T22:30:00Z", "2026-09-24T06:30:00Z", AUTOSLEEP),
            _s("sleeping", "2026-09-23T22:45:00Z", "2026-09-24T06:15:00Z", AUTOSLEEP),
        ]
        night = build_sleep_night(OURA_NIGHT + autosleep)

        assert night.source_key == OURA
        assert night.discarded_sources == [AUTOSLEEP]
        assert _minutes(night.total_sleep_seconds) == 340
        assert _minutes(night.time_in_bed_seconds) == 6 * 60 + 14
        assert set(night.raw_buckets) == {OURA, AUTOSLEEP}

    def test_without_stages_the_longest_source_wins(self) -> None:
        short = [_s("sleeping", "2026-09-24T00:00:00Z", "2026-09-24T05:00:00Z", AUTOSLEEP)]
        long = [_s("sleeping", "2026-09-23T23:00:00Z", "2026-09-24T06:30:00Z", IPHONE)]
        night = build_sleep_night(short + long)

        assert night.source_key == IPHONE
        assert night.discarded_sources == [AUTOSLEEP]
        assert _minutes(night.total_sleep_seconds) == 450

    def test_source_name_and_device_come_from_the_chosen_source(self) -> None:
        samples = [
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-09-23T23:00:00Z"),
                end_time=_dt("2026-09-24T06:00:00Z"),
                source_key=AUTOSLEEP,
                source_name="AutoSleep",
            ),
            SleepStateStage(
                stage=SleepStageType.DEEP,
                start_time=_dt("2026-09-24T00:00:00Z"),
                end_time=_dt("2026-09-24T01:00:00Z"),
                source_key=OURA,
                source_name="Oura",
                device_model="Oura Ring Gen3",
            ),
        ]
        night = build_sleep_night(samples)

        assert night.source_name == "Oura"
        assert night.device_model == "Oura Ring Gen3"


class TestSplitNight:
    def test_gap_between_blocks_is_not_in_bed(self) -> None:
        samples = [
            _s("in_bed", "2026-05-01T22:30:00Z", "2026-05-02T02:00:00Z", IPHONE),
            _s("light", "2026-05-01T22:45:00Z", "2026-05-02T01:45:00Z", WATCH),
            _s("in_bed", "2026-05-02T03:00:00Z", "2026-05-02T07:00:00Z", IPHONE),
            _s("deep", "2026-05-02T03:15:00Z", "2026-05-02T04:15:00Z", WATCH),
            _s("rem", "2026-05-02T04:15:00Z", "2026-05-02T06:45:00Z", WATCH),
        ]
        night = build_sleep_night(samples)

        assert _minutes(night.time_in_bed_seconds) == 210 + 240
        assert _minutes(night.total_sleep_seconds) == 180 + 60 + 150
        _assert_invariants(night.metrics, night.total_sleep_seconds, night.time_in_bed_seconds)

    def test_nap_and_main_sleep_are_built_separately(self) -> None:
        """Sessions split on the gap threshold, so each is its own night."""
        nap = [_s("sleeping", "2026-05-01T14:00:00Z", "2026-05-01T14:40:00Z", WATCH)]
        main = [
            _s("light", "2026-05-01T23:00:00Z", "2026-05-02T03:00:00Z", WATCH),
            _s("deep", "2026-05-02T03:00:00Z", "2026-05-02T06:30:00Z", WATCH),
        ]

        assert _minutes(build_sleep_night(nap).total_sleep_seconds) == 40
        assert _minutes(build_sleep_night(main).total_sleep_seconds) == 450


class TestLegacyStates:
    def test_samples_without_a_source_form_one_night(self) -> None:
        """Redis states written before source keys existed still build."""
        samples = [
            SleepStateStage(stage=SleepStageType.LIGHT, start_time=_dt(a), end_time=_dt(b))
            for a, b in (
                ("2026-03-10T22:00:00Z", "2026-03-10T23:00:00Z"),
                ("2026-03-10T22:30:00Z", "2026-03-10T23:30:00Z"),
            )
        ]
        night = build_sleep_night(samples)

        assert night.source_key is None
        assert _minutes(night.total_sleep_seconds) == 90

    def test_empty(self) -> None:
        night = build_sleep_night([])
        assert night.total_sleep_seconds == 0
        assert night.stages == []

    def test_zero_length_samples_are_ignored(self) -> None:
        t = _dt("2026-03-10T22:00:00Z")
        samples = [
            SleepStateStage(stage=SleepStageType.DEEP, start_time=t, end_time=t),
            SleepStateStage(stage=SleepStageType.LIGHT, start_time=t, end_time=t + timedelta(minutes=30)),
        ]
        assert _minutes(build_sleep_night(samples).metrics["deep_seconds"]) == 0


class TestSleepSourceKey:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (None, None),
            (SourceInfo(bundle_identifier="com.apple.health"), IPHONE),
            (SourceInfo(bundle_identifier=WATCH), IPHONE),
            (SourceInfo(bundle_identifier=OURA, name="Oura"), OURA),
            (SourceInfo(name="Danil's Apple Watch"), "Danil's Apple Watch"),
            (SourceInfo(device_model="Watch7,1"), None),
        ],
    )
    def test_key(self, source: SourceInfo | None, expected: str | None) -> None:
        assert sleep_source_key(source) == expected

    def test_bundle_lookalike_is_not_apple(self) -> None:
        assert sleep_source_key(SourceInfo(bundle_identifier="com.apple.healthy.app")) == "com.apple.healthy.app"


class TestSleepInvariants:
    def test_plausible_night(self) -> None:
        assert sleep_invariant_violations(total_sleep_minutes=420, time_in_bed_minutes=480, stage_minutes=[400]) == []

    def test_leons_night(self) -> None:
        """2026-09-24: 767 minutes of sleep inside 6h14 in bed."""
        assert sleep_invariant_violations(total_sleep_minutes=767, time_in_bed_minutes=374) == [
            "total_sleep_exceeds_time_in_bed"
        ]

    def test_over_16h_and_stage_overflow(self) -> None:
        assert sleep_invariant_violations(
            total_sleep_minutes=977, time_in_bed_minutes=1000, stage_minutes=[600, 400]
        ) == ["total_sleep_exceeds_16h", "stages_exceed_total_sleep"]

    def test_unknown_values_are_not_violations(self) -> None:
        assert sleep_invariant_violations(total_sleep_minutes=None, time_in_bed_minutes=10) == []
        assert sleep_invariant_violations(total_sleep_minutes=300, time_in_bed_minutes=None, stage_minutes=[None]) == []
