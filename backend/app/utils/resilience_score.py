"""Resilience score: HRV Coefficient of Variation (HRV-CV) model.

Translates nocturnal HRV variability into a 0-100 resilience score that
reflects day-to-day autonomic stability. Higher scores indicate more stable
HRV (lower day-to-day fluctuation), which is associated with better behavioral
profiles (less alcohol, more physical activity, longer and more consistent sleep).

Scientific grounding:
  Grosicki et al. (2026). Heart rate variability coefficient of variation during
  sleep as a digital biomarker that reflects behavior and varies by age and sex.
  Am J Physiol Heart Circ Physiol 330: H187-H199. doi:10.1152/ajpheart.00738.2025

Key design decisions:
  - Minimum 5 of 7 nights (ICC >= 0.80 reliability threshold per the paper)
  - HRV-CV = (sample_stdev / mean) x 100 over the valid readings window
  - Score ceiling: CV <= 7%  -> 100   (elite autonomic stability)
  - Score floor:   CV >= 40% -> 0     (severe volatility, rare in healthy adults)
  - Linear interpolation between ceiling and floor
  - Compatible with both RMSSD (most providers) and SDNN (Apple Health)
"""

import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class ResilienceConfig:
    min_nights: int = 5
    cv_ceiling_pct: float = 7.0  # CV <= this -> score 100 (elite)
    cv_floor_pct: float = 40.0  # CV >= this -> score 0   (severe volatility)
    elite_threshold_pct: float = 10.0  # CV <= this -> "Elite Stability"
    volatile_threshold_pct: float = 25.0  # CV >  this -> "Volatile"


RESILIENCE_CONFIG = ResilienceConfig()


def calculate_resilience_score(weekly_hrv: list[float | None]) -> dict:
    """Convert a week of nocturnal HRV readings into a 0-100 resilience score.

    Args:
        weekly_hrv: Up to 7 nightly HRV readings (ms). None and zero values
            indicate missing nights and are silently excluded before calculation.

    Returns:
        On success::

            {
                "resilience_score":    int,    # 0-100
                "hrv_cv_percentage":   float,  # rounded to 1 decimal
                "clinical_category":   str,    # "Elite Stability" | "Normal" | "Volatile"
                "nights_used":         int,    # count of valid nights used
                "status":              str,    # "success"
            }

        On insufficient data::

            {
                "resilience_score":  None,
                "hrv_cv_percentage": None,
                "status":            "insufficient_data",
                "message":           str,
            }
    """
    valid_hrv = [v for v in weekly_hrv if v is not None and v > 0]

    if len(valid_hrv) < RESILIENCE_CONFIG.min_nights:
        return {
            "resilience_score": None,
            "hrv_cv_percentage": None,
            "status": "insufficient_data",
            "message": (
                f"Need at least {RESILIENCE_CONFIG.min_nights} nights of HRV data to calculate "
                "resilience. Reliability requires a minimum 5-of-7 night window "
                "(Grosicki et al., 2026)."
            ),
        }

    mean_hrv = statistics.mean(valid_hrv)
    sd_hrv = statistics.stdev(valid_hrv) if len(valid_hrv) > 1 else 0.0
    cv_pct = (sd_hrv / mean_hrv) * 100.0

    if cv_pct <= RESILIENCE_CONFIG.cv_ceiling_pct:
        score = 100.0
    elif cv_pct >= RESILIENCE_CONFIG.cv_floor_pct:
        score = 0.0
    else:
        penalty_ratio = (cv_pct - RESILIENCE_CONFIG.cv_ceiling_pct) / (
            RESILIENCE_CONFIG.cv_floor_pct - RESILIENCE_CONFIG.cv_ceiling_pct
        )
        score = 100.0 - penalty_ratio * 100.0

    if cv_pct <= RESILIENCE_CONFIG.elite_threshold_pct:
        category = "Elite Stability"
    elif cv_pct <= RESILIENCE_CONFIG.volatile_threshold_pct:
        category = "Normal"
    else:
        category = "Volatile"

    return {
        "resilience_score": int(score),
        "hrv_cv_percentage": round(cv_pct, 1),
        "clinical_category": category,
        "nights_used": len(valid_hrv),
        "status": "success",
    }
