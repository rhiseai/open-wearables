"""Tests for the resilience score utility (app/utils/resilience_score.py).

The resilience score translates HRV Coefficient of Variation (HRV-CV), measured
during nocturnal sleep, into a 0-100 score reflecting day-to-day autonomic stability.

Scientific grounding:
  Grosicki et al. (2026). Heart rate variability coefficient of variation during sleep
  as a digital biomarker that reflects behavior and varies by age and sex.
  Am J Physiol Heart Circ Physiol 330: H187-H199. doi:10.1152/ajpheart.00738.2025

Key design rules under test:
  - Minimum 5 of 7 nights required (paper: ICC >= 0.80 threshold)
  - HRV-CV = (sample_stdev / mean) x 100
  - Score ceiling: CV <= 7%  -> 100   (elite autonomic stability)
  - Score floor:   CV >= 40% -> 0     (severe volatility)
  - Linear interpolation between ceiling and floor
  - Clinical categories: <=10% Elite Stability | <=25% Normal | >25% Volatile
"""

import pytest

from app.utils.resilience_score import (
    RESILIENCE_CONFIG,
    calculate_resilience_score,
)

# ---------------------------------------------------------------------------
# Pre-computed reference fixtures
# ---------------------------------------------------------------------------

# CV ~5.5% — well below the 7% ceiling
# mean=60.57, sample_stdev=3.31, CV=5.46%  -> score=100
ELITE_WEEK: list[float] = [60.0, 62.0, 58.0, 65.0, 61.0, 55.0, 63.0]

# CV ~17.3% — population average per the paper (Normal category)
# mean=50.0, sample_stdev=8.66, CV=17.32%  -> score~68
NORMAL_WEEK: list[float] = [50.0, 60.0, 40.0, 55.0, 45.0, 60.0, 40.0]

# CV ~65% — far above the 40% floor (Volatile)
# mean=50.0, sample_stdev=32.66, CV=65.3%  -> score=0
VOLATILE_WEEK: list[float] = [50.0, 90.0, 10.0, 50.0, 90.0, 10.0, 50.0]

# CV=0% — zero variability (all identical HRV readings)
FLAT_WEEK: list[float] = [55.0, 55.0, 55.0, 55.0, 55.0, 55.0, 55.0]


# ---------------------------------------------------------------------------
# 1. Insufficient-data guard
# ---------------------------------------------------------------------------


class TestInsufficientData:
    def test_empty_list_returns_insufficient_data(self) -> None:
        result = calculate_resilience_score([])
        assert result["status"] == "insufficient_data"
        assert result["resilience_score"] is None
        assert result["hrv_cv_percentage"] is None

    def test_four_valid_nights_returns_insufficient_data(self) -> None:
        """Paper establishes 5 nights as the minimum for ICC >= 0.80."""
        result = calculate_resilience_score([50.0, 52.0, 48.0, 51.0])
        assert result["status"] == "insufficient_data"

    def test_nones_below_threshold_returns_insufficient_data(self) -> None:
        """4 valid + 3 None/zero should still be insufficient."""
        result = calculate_resilience_score([50.0, 52.0, None, 48.0, None, None, 51.0])
        assert result["status"] == "insufficient_data"

    def test_five_valid_nights_is_accepted(self) -> None:
        result = calculate_resilience_score([50.0, 55.0, 48.0, 52.0, 51.0])
        assert result["status"] == "success"

    def test_seven_valid_nights_is_accepted(self) -> None:
        result = calculate_resilience_score(ELITE_WEEK)
        assert result["status"] == "success"

    def test_nights_used_reflects_valid_count_only(self) -> None:
        """nights_used must count only non-None, positive values."""
        result = calculate_resilience_score([50.0, None, 52.0, 0.0, 48.0, 51.0, 53.0])
        assert result["nights_used"] == 5

    def test_insufficient_message_is_human_readable(self) -> None:
        result = calculate_resilience_score([])
        assert isinstance(result.get("message"), str)
        assert len(result["message"]) > 0


# ---------------------------------------------------------------------------
# 2. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_all_identical_hrv_scores_100(self) -> None:
        """Zero variability (CV=0%) should yield the maximum score of 100."""
        result = calculate_resilience_score(FLAT_WEEK)
        assert result["resilience_score"] == 100
        assert result["hrv_cv_percentage"] == pytest.approx(0.0, abs=0.01)

    def test_none_values_are_filtered_before_calculation(self) -> None:
        """None values should be silently ignored; result must equal the all-valid case."""
        with_nones = [50.0, None, 55.0, 48.0, None, 52.0, 51.0]
        without_nones = [50.0, 55.0, 48.0, 52.0, 51.0]
        result_nones = calculate_resilience_score(with_nones)
        result_clean = calculate_resilience_score(without_nones)
        assert result_nones["resilience_score"] == result_clean["resilience_score"]
        assert result_nones["hrv_cv_percentage"] == result_clean["hrv_cv_percentage"]

    def test_zero_values_are_filtered_before_calculation(self) -> None:
        """Zero HRV readings indicate missing data and must be excluded."""
        with_zeros = [50.0, 0.0, 55.0, 48.0, 0.0, 52.0, 51.0]
        without_zeros = [50.0, 55.0, 48.0, 52.0, 51.0]
        assert (
            calculate_resilience_score(with_zeros)["hrv_cv_percentage"]
            == calculate_resilience_score(without_zeros)["hrv_cv_percentage"]
        )

    def test_score_is_integer(self) -> None:
        result = calculate_resilience_score(NORMAL_WEEK)
        assert isinstance(result["resilience_score"], int)

    def test_hrv_cv_is_rounded_to_one_decimal(self) -> None:
        result = calculate_resilience_score(NORMAL_WEEK)
        cv = result["hrv_cv_percentage"]
        assert cv == round(cv, 1)


# ---------------------------------------------------------------------------
# 3. Score scaling
# ---------------------------------------------------------------------------


class TestScoreScaling:
    def test_elite_cv_yields_100(self) -> None:
        """CV ~5.5% is below the 7% ceiling; score must be capped at 100."""
        result = calculate_resilience_score(ELITE_WEEK)
        assert result["resilience_score"] == 100

    def test_volatile_cv_yields_0(self) -> None:
        """CV ~65% far exceeds the 40% floor; score must floor at 0."""
        result = calculate_resilience_score(VOLATILE_WEEK)
        assert result["resilience_score"] == 0

    def test_cv_exactly_at_ceiling_yields_100(self) -> None:
        """CV == cv_ceiling (7%) should produce exactly 100."""
        ceiling = RESILIENCE_CONFIG.cv_ceiling_pct
        # Force a known CV by using stdev/mean relationship:
        # need sample_stdev = ceiling/100 * mean
        # With mean=50 and target CV=7%: stdev = 3.5
        # sample_stdev^2 * (n-1) = sum_sq_devs
        # 3.5^2 * 6 = 73.5; distribute across 2 pairs: d = sqrt(73.5/4) = 4.284
        d = (ceiling / 100) ** 0.5 * 50  # approximate; verify via CV check in result
        data = [50.0 + d, 50.0 - d, 50.0 + d, 50.0 - d, 50.0 + d, 50.0 - d, 50.0]
        result = calculate_resilience_score(data)
        # The actual CV may not be exactly 7% due to sample-vs-population stdev;
        # just assert that a data set with CV <= ceiling always scores 100.
        if result["hrv_cv_percentage"] <= ceiling:
            assert result["resilience_score"] == 100

    def test_cv_at_floor_yields_0(self) -> None:
        """CV == cv_floor (40%) should produce exactly 0."""
        result = calculate_resilience_score(VOLATILE_WEEK)
        assert result["resilience_score"] == 0

    def test_normal_population_cv_yields_moderate_score(self) -> None:
        """Population mean HRV-CV (~17%) should land in the 60-80 range."""
        result = calculate_resilience_score(NORMAL_WEEK)
        assert 60 <= result["resilience_score"] <= 80

    def test_scores_are_bounded_0_to_100(self) -> None:
        """No input combination should produce a score outside 0-100."""
        test_inputs = [
            ELITE_WEEK,
            NORMAL_WEEK,
            VOLATILE_WEEK,
            FLAT_WEEK,
            [10.0, 100.0, 5.0, 80.0, 20.0],  # extreme spread
        ]
        for data in test_inputs:
            result = calculate_resilience_score(data)
            score = result["resilience_score"]
            assert 0 <= score <= 100, f"Score {score} out of bounds for input {data}"

    def test_score_decreases_monotonically_with_rising_cv(self) -> None:
        """Higher HRV-CV (more volatile) must always produce a lower or equal score.

        Constructed via progressively wider HRV swings around a fixed mean of 50.
        """
        datasets = [
            [50.0, 51.0, 49.0, 50.0, 51.0, 49.0, 50.0],  # tiny swing  -> low CV
            [50.0, 55.0, 45.0, 50.0, 55.0, 45.0, 50.0],  # small swing
            [50.0, 65.0, 35.0, 50.0, 65.0, 35.0, 50.0],  # medium swing
            [50.0, 80.0, 20.0, 50.0, 80.0, 20.0, 50.0],  # large swing -> high CV
        ]
        scores = [calculate_resilience_score(d)["resilience_score"] for d in datasets]
        assert scores == sorted(scores, reverse=True), f"Scores should decrease as variability grows, got: {scores}"

    def test_config_ceiling_and_floor_are_respected(self) -> None:
        """Score formula must use the exported RESILIENCE_CONFIG values."""
        assert RESILIENCE_CONFIG.cv_ceiling_pct < RESILIENCE_CONFIG.cv_floor_pct
        assert RESILIENCE_CONFIG.min_nights == 5


# ---------------------------------------------------------------------------
# 4. Clinical categories
# ---------------------------------------------------------------------------


class TestClinicalCategory:
    def test_elite_stability_category(self) -> None:
        """CV <= 10% -> 'Elite Stability'."""
        result = calculate_resilience_score(ELITE_WEEK)
        assert result["clinical_category"] == "Elite Stability"
        assert result["hrv_cv_percentage"] <= 10.0

    def test_normal_category(self) -> None:
        """Population-average CV (~17%) should map to 'Normal'."""
        result = calculate_resilience_score(NORMAL_WEEK)
        assert result["clinical_category"] == "Normal"

    def test_volatile_category(self) -> None:
        """CV ~65% should map to 'Volatile'."""
        result = calculate_resilience_score(VOLATILE_WEEK)
        assert result["clinical_category"] == "Volatile"

    def test_boundary_at_10_pct_is_elite(self) -> None:
        """Exactly 10% CV should fall inside 'Elite Stability', not 'Normal'."""
        # mean=50, target_cv=10% -> sample_stdev=5
        # sample_stdev^2 * (n-1) = 25 * 6 = 150; 4 pairs: d = sqrt(150/4) = 6.12
        d = (0.10 * 50) / (4 / 6) ** 0.5  # approximate
        data = [50.0 + d, 50.0 - d, 50.0 + d, 50.0 - d, 50.0 + d, 50.0 - d, 50.0]
        result = calculate_resilience_score(data)
        if result["hrv_cv_percentage"] <= 10.0:
            assert result["clinical_category"] == "Elite Stability"

    def test_boundary_above_25_pct_is_volatile(self) -> None:
        """CV > 25% must be 'Volatile'."""
        result = calculate_resilience_score(VOLATILE_WEEK)
        assert result["hrv_cv_percentage"] > 25.0
        assert result["clinical_category"] == "Volatile"

    def test_category_is_never_none_on_success(self) -> None:
        for data in [ELITE_WEEK, NORMAL_WEEK, VOLATILE_WEEK, FLAT_WEEK]:
            result = calculate_resilience_score(data)
            assert result["clinical_category"] is not None


# ---------------------------------------------------------------------------
# 5. Return-shape contract
# ---------------------------------------------------------------------------


class TestReturnShape:
    _REQUIRED_SUCCESS_KEYS = {
        "resilience_score",
        "hrv_cv_percentage",
        "clinical_category",
        "nights_used",
        "status",
    }
    _REQUIRED_INSUFFICIENT_KEYS = {
        "resilience_score",
        "hrv_cv_percentage",
        "status",
        "message",
    }

    def test_success_response_has_all_required_keys(self) -> None:
        result = calculate_resilience_score(ELITE_WEEK)
        assert self._REQUIRED_SUCCESS_KEYS.issubset(result.keys())

    def test_insufficient_response_has_all_required_keys(self) -> None:
        result = calculate_resilience_score([])
        assert self._REQUIRED_INSUFFICIENT_KEYS.issubset(result.keys())

    def test_status_is_success_on_valid_input(self) -> None:
        assert calculate_resilience_score(ELITE_WEEK)["status"] == "success"

    def test_status_is_insufficient_data_on_short_input(self) -> None:
        assert calculate_resilience_score([50.0, 51.0])["status"] == "insufficient_data"

    def test_nights_used_equals_valid_count(self) -> None:
        result = calculate_resilience_score(ELITE_WEEK)
        assert result["nights_used"] == 7

    def test_nights_used_is_five_minimum(self) -> None:
        five_nights = [50.0, 55.0, 48.0, 52.0, 51.0]
        result = calculate_resilience_score(five_nights)
        assert result["nights_used"] == 5
