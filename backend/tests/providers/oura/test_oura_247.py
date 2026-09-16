"""Tests for Oura247Data normalization."""

from collections.abc import Generator
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.constants.sleep import SleepStageType
from app.schemas.enums import SeriesType
from app.schemas.providers.oura.imports import OuraMetJSON
from app.services.providers.oura.coverage import ACTIVITY_SERIES
from app.services.providers.oura.data_247 import Oura247Data
from app.services.providers.oura.strategy import OuraStrategy


class TestOura247SleepNormalization:
    """Test sleep data normalization."""

    @pytest.fixture
    def data_247(self) -> Oura247Data:
        strategy = OuraStrategy()
        return strategy.data_247

    @pytest.fixture
    def sample_oura_sleep(self) -> dict:
        return {
            "id": "sleep-abc123",
            "average_breath": 15.5,
            "average_heart_rate": 55.0,
            "average_hrv": 45,
            "awake_time": 1800,
            "bedtime_end": "2024-01-15T07:00:00+00:00",
            "bedtime_start": "2024-01-15T23:00:00+00:00",
            "day": "2024-01-15",
            "deep_sleep_duration": 5400,
            "efficiency": 88,
            "latency": 300,
            "light_sleep_duration": 14400,
            "low_battery_alert": False,
            "lowest_heart_rate": 48,
            "period": 0,
            "rem_sleep_duration": 7200,
            "restless_periods": 5,
            "time_in_bed": 28800,
            "total_sleep_duration": 27000,
            "type": "long_sleep",
        }

    def test_normalize_sleep_basic_fields(self, data_247: Oura247Data, sample_oura_sleep: dict) -> None:
        user_id = uuid4()
        result = data_247.normalize_sleeps([sample_oura_sleep], user_id)[0]

        assert result["user_id"] == user_id
        assert result["provider"] == "oura"
        assert result["oura_sleep_id"] == "sleep-abc123"
        assert result["duration_seconds"] == 28800
        assert result["efficiency_percent"] == 88.0

    def test_normalize_sleep_stages(self, data_247: Oura247Data, sample_oura_sleep: dict) -> None:
        user_id = uuid4()
        result = data_247.normalize_sleeps([sample_oura_sleep], user_id)[0]
        stages = result["stages"]

        assert stages["deep_seconds"] == 5400
        assert stages["light_seconds"] == 14400
        assert stages["rem_seconds"] == 7200
        assert stages["awake_seconds"] == 1800

    def test_stage_timestamps_use_30_sec_hypnogram(self, data_247: Oura247Data) -> None:
        raw = {
            "id": "sleep-30s",
            "bedtime_start": "2024-01-15T23:00:00+00:00",
            "bedtime_end": "2024-01-16T07:00:00+00:00",
            "type": "long_sleep",
            "sleep_phase_30_sec": "1" * 4 + "3" * 2,  # 120s deep, 60s rem
        }
        stages = data_247.normalize_sleeps([raw], uuid4())[0]["stage_timestamps"]

        assert [s.stage for s in stages] == [SleepStageType.DEEP, SleepStageType.REM]
        assert stages[0].start_time == datetime(2024, 1, 15, 23, 0, tzinfo=timezone.utc)
        assert stages[0].end_time == datetime(2024, 1, 15, 23, 2, tzinfo=timezone.utc)
        assert stages[1].end_time == datetime(2024, 1, 15, 23, 3, tzinfo=timezone.utc)

    def test_stage_timestamps_prefer_30_sec_over_5_min(self, data_247: Oura247Data) -> None:
        raw = {
            "id": "sleep-both",
            "bedtime_start": "2024-01-15T23:00:00+00:00",
            "bedtime_end": "2024-01-16T07:00:00+00:00",
            "type": "long_sleep",
            "sleep_phase_30_sec": "111",  # 90s deep — not expressible at 5-min resolution
            "sleep_phase_5_min": "2",
        }
        stages = data_247.normalize_sleeps([raw], uuid4())[0]["stage_timestamps"]

        assert len(stages) == 1
        assert stages[0].stage == SleepStageType.DEEP
        assert stages[0].end_time == datetime(2024, 1, 15, 23, 1, 30, tzinfo=timezone.utc)

    def test_stage_timestamps_fall_back_to_5_min(self, data_247: Oura247Data) -> None:
        raw = {
            "id": "sleep-5m",
            "bedtime_start": "2024-01-15T23:00:00+00:00",
            "bedtime_end": "2024-01-16T07:00:00+00:00",
            "type": "long_sleep",
            "sleep_phase_5_min": "14",  # 5min deep, 5min awake
        }
        stages = data_247.normalize_sleeps([raw], uuid4())[0]["stage_timestamps"]

        assert [s.stage for s in stages] == [SleepStageType.DEEP, SleepStageType.AWAKE]
        assert stages[0].end_time == datetime(2024, 1, 15, 23, 5, tzinfo=timezone.utc)
        assert stages[1].end_time == datetime(2024, 1, 15, 23, 10, tzinfo=timezone.utc)

    def test_normalize_sleep_timestamps(self, data_247: Oura247Data, sample_oura_sleep: dict) -> None:
        user_id = uuid4()
        result = data_247.normalize_sleeps([sample_oura_sleep], user_id)[0]

        assert result["start_time"] == "2024-01-15T23:00:00+00:00"
        assert result["end_time"] == "2024-01-15T07:00:00+00:00"
        assert result["zone_offset"] == "+00:00"

    def test_normalize_sleep_zone_offset_from_local_bedtime(self, data_247: Oura247Data) -> None:
        user_id = uuid4()
        raw = {
            "id": "sleep-early-wake",
            "bedtime_start": "2026-05-05T01:13:00+03:00",
            "bedtime_end": "2026-05-05T02:56:00+03:00",
            "time_in_bed": 5400,
            "type": "long_sleep",
        }
        result = data_247.normalize_sleeps([raw], user_id)[0]
        assert result["zone_offset"] == "+03:00"

    def test_normalize_sleep_not_nap(self, data_247: Oura247Data, sample_oura_sleep: dict) -> None:
        user_id = uuid4()
        result = data_247.normalize_sleeps([sample_oura_sleep], user_id)[0]
        assert result["is_nap"] is False

    def test_normalize_sleep_nap_detection_sleep_type(self, data_247: Oura247Data) -> None:
        user_id = uuid4()
        raw = {
            "id": "sleep-nap",
            "bedtime_start": "2024-01-15T14:00:00+00:00",
            "bedtime_end": "2024-01-15T14:30:00+00:00",
            "type": "sleep",
            "time_in_bed": 1800,
        }
        result = data_247.normalize_sleeps([raw], user_id)[0]
        assert result["is_nap"] is True

    def test_normalize_sleep_nap_detection_late_nap_type(self, data_247: Oura247Data) -> None:
        user_id = uuid4()
        raw = {
            "id": "sleep-late-nap",
            "bedtime_start": "2024-01-15T23:30:00+00:00",
            "bedtime_end": "2024-01-16T00:30:00+00:00",
            "type": "late_nap",
            "time_in_bed": 3600,
        }
        result = data_247.normalize_sleeps([raw], user_id)[0]
        assert result["is_nap"] is True

    def test_normalize_sleep_skips_rest_type(self, data_247: Oura247Data) -> None:
        user_id = uuid4()
        raw = {
            "id": "sleep-rest",
            "bedtime_start": "2024-01-15T14:00:00+00:00",
            "bedtime_end": "2024-01-15T14:30:00+00:00",
            "type": "rest",
            "time_in_bed": 1800,
        }
        result = data_247.normalize_sleeps([raw], user_id)
        assert result == []

    def test_normalize_sleep_skips_deleted_type(self, data_247: Oura247Data) -> None:
        user_id = uuid4()
        raw = {
            "id": "sleep-deleted",
            "bedtime_start": "2024-01-15T14:00:00+00:00",
            "bedtime_end": "2024-01-15T14:30:00+00:00",
            "type": "deleted",
            "time_in_bed": 1800,
        }
        result = data_247.normalize_sleeps([raw], user_id)
        assert result == []

    def test_normalize_sleep_mixed_types_filters_correctly(self, data_247: Oura247Data) -> None:
        user_id = uuid4()
        items = [
            {
                "id": "s1",
                "bedtime_start": "2024-01-15T00:00:00+00:00",
                "bedtime_end": "2024-01-15T08:00:00+00:00",
                "type": "long_sleep",
                "time_in_bed": 28800,
            },
            {
                "id": "s2",
                "bedtime_start": "2024-01-15T14:00:00+00:00",
                "bedtime_end": "2024-01-15T14:30:00+00:00",
                "type": "sleep",
                "time_in_bed": 1800,
            },
            {
                "id": "s3",
                "bedtime_start": "2024-01-15T15:00:00+00:00",
                "bedtime_end": "2024-01-15T15:20:00+00:00",
                "type": "rest",
                "time_in_bed": 1200,
            },
            {
                "id": "s4",
                "bedtime_start": "2024-01-15T16:00:00+00:00",
                "bedtime_end": "2024-01-15T16:30:00+00:00",
                "type": "deleted",
                "time_in_bed": 1800,
            },
        ]
        result = data_247.normalize_sleeps(items, user_id)
        assert len(result) == 2
        assert result[0]["is_nap"] is False  # long_sleep
        assert result[1]["is_nap"] is True  # sleep

    def test_normalize_sleep_heart_rate(self, data_247: Oura247Data, sample_oura_sleep: dict) -> None:
        user_id = uuid4()
        result = data_247.normalize_sleeps([sample_oura_sleep], user_id)[0]
        assert result["average_heart_rate"] == 55.0
        assert result["average_hrv"] == 45
        assert result["lowest_heart_rate"] == 48


class TestOura247RestingHeartRatePersistence:
    """Test resting_heart_rate emission from sleep sessions."""

    @pytest.fixture
    def data_247(self) -> Oura247Data:
        strategy = OuraStrategy()
        return strategy.data_247

    @pytest.fixture
    def base_sleep(self) -> dict:
        return {
            "id": uuid4(),
            "user_id": uuid4(),
            "provider": "oura",
            "is_nap": False,
            "lowest_heart_rate": 48,
            "average_heart_rate": 55.0,
            "oura_sleep_id": "sleep-abc123",
        }

    @pytest.fixture
    def timeseries_service_mock(self) -> Generator[MagicMock, None, None]:
        with patch("app.services.providers.oura.data_247.timeseries_service") as mock:
            yield mock

    def test_emits_rhr_from_lowest_heart_rate(
        self,
        data_247: Oura247Data,
        base_sleep: dict,
        timeseries_service_mock: MagicMock,
    ) -> None:
        db = MagicMock()
        recorded_at = datetime(2024, 1, 15, 7, 0, tzinfo=timezone.utc)

        data_247._persist_resting_heart_rate(db, base_sleep["user_id"], base_sleep, recorded_at, "+00:00")

        timeseries_service_mock.bulk_create_samples.assert_called_once()
        samples = timeseries_service_mock.bulk_create_samples.call_args[0][1]
        assert len(samples) == 1
        sample = samples[0]
        assert sample.series_type == SeriesType.resting_heart_rate
        assert sample.value == Decimal("48")
        assert sample.recorded_at == recorded_at
        assert sample.zone_offset == "+00:00"
        assert sample.user_id == base_sleep["user_id"]
        assert sample.source == "oura"
        assert sample.external_id == "sleep-abc123"
        db.commit.assert_called_once()

    def test_falls_back_to_average_heart_rate(
        self,
        data_247: Oura247Data,
        base_sleep: dict,
        timeseries_service_mock: MagicMock,
    ) -> None:
        base_sleep["lowest_heart_rate"] = None

        data_247._persist_resting_heart_rate(
            MagicMock(), base_sleep["user_id"], base_sleep, datetime.now(timezone.utc), None
        )

        samples = timeseries_service_mock.bulk_create_samples.call_args[0][1]
        assert samples[0].value == Decimal("55.0")

    def test_skips_nap_sessions(
        self,
        data_247: Oura247Data,
        base_sleep: dict,
        timeseries_service_mock: MagicMock,
    ) -> None:
        base_sleep["is_nap"] = True

        data_247._persist_resting_heart_rate(
            MagicMock(), base_sleep["user_id"], base_sleep, datetime.now(timezone.utc), None
        )

        timeseries_service_mock.bulk_create_samples.assert_not_called()

    def test_skips_when_no_heart_rate_available(
        self,
        data_247: Oura247Data,
        base_sleep: dict,
        timeseries_service_mock: MagicMock,
    ) -> None:
        base_sleep["lowest_heart_rate"] = None
        base_sleep["average_heart_rate"] = None

        data_247._persist_resting_heart_rate(
            MagicMock(), base_sleep["user_id"], base_sleep, datetime.now(timezone.utc), None
        )

        timeseries_service_mock.bulk_create_samples.assert_not_called()

    def test_swallows_service_errors_and_rolls_back(
        self,
        data_247: Oura247Data,
        base_sleep: dict,
        timeseries_service_mock: MagicMock,
    ) -> None:
        timeseries_service_mock.bulk_create_samples.side_effect = RuntimeError("db down")
        db = MagicMock()

        data_247._persist_resting_heart_rate(db, base_sleep["user_id"], base_sleep, datetime.now(timezone.utc), None)

        timeseries_service_mock.bulk_create_samples.assert_called_once()
        db.rollback.assert_called_once()
        db.commit.assert_not_called()


class TestOura247ReadinessNormalization:
    """Test readiness (recovery) data normalization."""

    @pytest.fixture
    def data_247(self) -> Oura247Data:
        strategy = OuraStrategy()
        return strategy.data_247

    @pytest.fixture
    def sample_oura_readiness(self) -> dict:
        return {
            "id": "readiness-abc123",
            "day": "2024-01-15",
            "score": 82,
            "temperature_deviation": 0.15,
            "temperature_trend_deviation": 0.05,
            "timestamp": "2024-01-15T07:00:00+00:00",
        }

    def test_normalize_readiness_score(self, data_247: Oura247Data, sample_oura_readiness: dict) -> None:
        user_id = uuid4()
        recovery_metrics, health_scores = data_247.normalize_readiness([sample_oura_readiness], user_id)
        result = recovery_metrics[0]

        assert result["recovery_score"] == 82
        assert result["provider"] == "oura"
        assert result["user_id"] == user_id

    def test_normalize_readiness_temperature(self, data_247: Oura247Data, sample_oura_readiness: dict) -> None:
        user_id = uuid4()
        recovery_metrics, _ = data_247.normalize_readiness([sample_oura_readiness], user_id)
        assert recovery_metrics[0]["temperature_deviation"] == 0.15

    def test_normalize_readiness_timestamp(self, data_247: Oura247Data, sample_oura_readiness: dict) -> None:
        user_id = uuid4()
        recovery_metrics, _ = data_247.normalize_readiness([sample_oura_readiness], user_id)
        assert recovery_metrics[0]["timestamp"] is not None


class TestOura247ActivityNormalization:
    """Test activity data normalization."""

    @pytest.fixture
    def data_247(self) -> Oura247Data:
        strategy = OuraStrategy()
        return strategy.data_247

    def test_normalize_activity_samples(self, data_247: Oura247Data) -> None:
        user_id = uuid4()
        raw = [
            {
                "id": "activity-1",
                "day": "2024-01-15",
                "steps": 8500,
                "active_calories": 350,
                "equivalent_walking_distance": 6500,
                "timestamp": "2024-01-15T23:59:59+00:00",
            },
        ]
        samples, _ = data_247.normalize_activity_samples(raw, user_id)

        assert len(samples["steps"]) == 1
        assert samples["steps"][0]["value"] == 8500
        assert len(samples["energy"]) == 1
        assert samples["energy"][0]["value"] == 350
        assert len(samples["distance"]) == 1
        assert samples["distance"][0]["value"] == 6500

    def test_normalize_activity_empty(self, data_247: Oura247Data) -> None:
        user_id = uuid4()
        samples, _ = data_247.normalize_activity_samples([], user_id)

        assert samples["steps"] == []
        assert samples["energy"] == []
        assert samples["distance"] == []

    def test_normalize_activity_samples_includes_met(self, data_247: Oura247Data) -> None:
        user_id = uuid4()
        raw = [
            {
                "id": "activity-met",
                "day": "2024-01-15",
                "timestamp": "2024-01-15T00:00:00+00:00",
                "met": {
                    "interval": 60,
                    "items": [1.0, 1.2, None, 1.5],
                    "timestamp": "2024-01-15T00:00:00+00:00",
                },
                "class_5_min": "2",
            },
        ]
        samples, _ = data_247.normalize_activity_samples(raw, user_id)

        assert len(samples["met"]) == 3
        assert [s["value"] for s in samples["met"]] == [1.0, 1.2, 1.5]

    def test_normalize_activity_samples_met_absent(self, data_247: Oura247Data) -> None:
        user_id = uuid4()
        raw = [{"id": "activity-no-met", "day": "2024-01-15", "timestamp": "2024-01-15T00:00:00+00:00"}]
        samples, _ = data_247.normalize_activity_samples(raw, user_id)

        assert samples["met"] == []


class TestOura247MetSeriesExpansion:
    """Test expansion of the intraday MET series embedded in daily activity records."""

    @pytest.fixture
    def data_247(self) -> Oura247Data:
        strategy = OuraStrategy()
        return strategy.data_247

    def test_expand_met_series_basic(self, data_247: Oura247Data) -> None:
        met = OuraMetJSON(interval=60, items=[1.0, 1.1, 1.2], timestamp="2024-01-15T00:00:00+00:00")
        samples = data_247._expand_met_series(met, "2")

        assert [s["value"] for s in samples] == [1.0, 1.1, 1.2]
        assert samples[0]["recorded_at"] == datetime(2024, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        assert samples[1]["recorded_at"] == datetime(2024, 1, 15, 0, 1, 0, tzinfo=timezone.utc)
        assert samples[2]["recorded_at"] == datetime(2024, 1, 15, 0, 2, 0, tzinfo=timezone.utc)

    def test_expand_met_series_skips_null_items(self, data_247: Oura247Data) -> None:
        met = OuraMetJSON(interval=60, items=[1.0, None, 1.2], timestamp="2024-01-15T00:00:00+00:00")
        samples = data_247._expand_met_series(met, "2")

        # A null (unworn/unmeasured) sample must be dropped, never coerced to 0.0.
        assert [s["value"] for s in samples] == [1.0, 1.2]

    def test_expand_met_series_skips_not_worn_sentinel(self, data_247: Oura247Data) -> None:
        met = OuraMetJSON(interval=60, items=[1.0, 0.1, 1.2], timestamp="2024-01-15T00:00:00+00:00")
        samples = data_247._expand_met_series(met, "2")

        assert [s["value"] for s in samples] == [1.0, 1.2]

    def test_expand_met_series_all_null(self, data_247: Oura247Data) -> None:
        met = OuraMetJSON(interval=60, items=[None, None], timestamp="2024-01-15T00:00:00+00:00")
        assert data_247._expand_met_series(met, "2") == []

    def test_expand_met_series_missing_met(self, data_247: Oura247Data) -> None:
        assert data_247._expand_met_series(None, "2") == []

    def test_expand_met_series_empty_items(self, data_247: Oura247Data) -> None:
        met = OuraMetJSON(interval=60, items=[], timestamp="2024-01-15T00:00:00+00:00")
        assert data_247._expand_met_series(met, "2") == []

    def test_expand_met_series_missing_interval_is_dropped(self, data_247: Oura247Data) -> None:
        # Oura's schema documents `interval` as a plain float, not a fixed 60s cadence —
        # a missing value must not be guessed.
        met = OuraMetJSON(interval=None, items=[1.0, 1.1], timestamp="2024-01-15T00:00:00+00:00")
        assert data_247._expand_met_series(met, "2") == []

    def test_expand_met_series_zero_interval_is_dropped(self, data_247: Oura247Data) -> None:
        met = OuraMetJSON(interval=0, items=[1.0, 1.1], timestamp="2024-01-15T00:00:00+00:00")
        assert data_247._expand_met_series(met, "2") == []

    def test_expand_met_series_negative_interval_is_dropped(self, data_247: Oura247Data) -> None:
        met = OuraMetJSON(interval=-60, items=[1.0, 1.1], timestamp="2024-01-15T00:00:00+00:00")
        assert data_247._expand_met_series(met, "2") == []

    def test_expand_met_series_unrepresentable_interval_returns_empty(self, data_247: Oura247Data) -> None:
        # A finite but huge interval overflows `timedelta` once multiplied by the item
        # index, instead of raising and failing the whole activity import.
        met = OuraMetJSON(interval=1e20, items=[1.0, 1.1], timestamp="2024-01-15T00:00:00+00:00")
        assert data_247._expand_met_series(met, "2") == []

    def test_expand_met_series_unparseable_timestamp_is_dropped(self, data_247: Oura247Data) -> None:
        # No fallback to the daily activity's own timestamp — it can anchor the whole
        # series to the wrong point in the day (mirrors the sleep HR/HRV interval handling).
        met = OuraMetJSON(interval=60, items=[1.0], timestamp="not-a-timestamp")
        assert data_247._expand_met_series(met, "2") == []

    def test_expand_met_series_missing_timestamp_is_dropped(self, data_247: Oura247Data) -> None:
        met = OuraMetJSON(interval=60, items=[1.0], timestamp=None)
        assert data_247._expand_met_series(met, "2") == []

    def test_expand_met_series_offset_less_timestamp_is_dropped(self, data_247: Oura247Data) -> None:
        met = OuraMetJSON(interval=60, items=[1.0, 1.1], timestamp="2024-01-15T00:00:00")
        assert data_247._expand_met_series(met, "2") == []

    def test_expand_met_series_full_day(self, data_247: Oura247Data) -> None:
        met = OuraMetJSON(interval=60, items=[1.0] * 1440, timestamp="2024-01-15T00:00:00+00:00")
        samples = data_247._expand_met_series(met, "2" * 288)

        assert len(samples) == 1440
        assert samples[-1]["recorded_at"] == datetime(2024, 1, 15, 23, 59, 0, tzinfo=timezone.utc)

    def test_expand_met_series_carries_zone_offset(self, data_247: Oura247Data) -> None:
        met = OuraMetJSON(interval=60, items=[1.0], timestamp="2024-01-15T00:00:00+02:00")
        samples = data_247._expand_met_series(met, "2")

        assert samples[0]["zone_offset"] == "+02:00"

    def test_met_maps_to_physical_effort(self) -> None:
        # average_met means one scalar per workout everywhere else (Apple/Google/Samsung);
        # Oura's series is continuous per-minute data, so it must not share that series type.
        assert ACTIVITY_SERIES["met"] == SeriesType.physical_effort

    def test_expand_met_series_drops_unmeasured_tail_using_class_5_min_length(self, data_247: Oura247Data) -> None:
        # Reproduces real Oura behavior: `met.items` is pre-allocated for the full
        items = [1.2] * 10 + [0.9] * 1430
        class_5_min = "22"  # only the first 10 minutes (2 * 5 min) have been measured
        met = OuraMetJSON(interval=60, items=items, timestamp="2024-01-15T04:00:00+00:00")

        samples = data_247._expand_met_series(met, class_5_min)

        assert len(samples) == 10
        assert all(s["value"] == 1.2 for s in samples)

    def test_expand_met_series_keeps_genuine_low_value_within_measured_window(self, data_247: Oura247Data) -> None:
        items = [0.9] * 5
        met = OuraMetJSON(interval=60, items=items, timestamp="2024-01-15T04:00:00+00:00")

        samples = data_247._expand_met_series(met, "1")

        assert [s["value"] for s in samples] == [0.9] * 5

    def test_expand_met_series_drops_not_worn_values_regardless_of_class_5_min_content(
        self, data_247: Oura247Data
    ) -> None:
        items = [1.2, 1.2, 1.2, 1.2, 1.2, 0.1, 0.1, 0.1, 0.1, 0.1]
        class_5_min = "22"
        met = OuraMetJSON(interval=60, items=items, timestamp="2024-01-15T04:00:00+00:00")

        samples = data_247._expand_met_series(met, class_5_min)

        assert len(samples) == 5
        assert all(s["value"] == 1.2 for s in samples)

    def test_expand_met_series_without_class_5_min_is_dropped(self, data_247: Oura247Data) -> None:
        met = OuraMetJSON(interval=60, items=[1.0, 1.1], timestamp="2024-01-15T00:00:00+00:00")

        assert data_247._expand_met_series(met, None) == []
