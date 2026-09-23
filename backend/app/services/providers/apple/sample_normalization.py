"""Shared Apple Health sample value normalization for SDK and XML imports."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from app.constants.series_types.sdk.category_types import (
    AppleCategoryType,
    recode_menstrual_flow_value,
)
from app.constants.series_types.sdk.metric_types import SDKMetricType
from app.schemas.enums import SeriesType

# Health Connect's own mg/dL converter uses exactly 18.0, so values written to HC
# in mg/dL round-trip with a ~0.1% offset under this factor.
MMOL_L_TO_MG_DL = Decimal("18.0182")

_MINDFUL_TYPES = {
    AppleCategoryType.MINDFUL_SESSION.value,
    SDKMetricType.APPLE_MINDFUL_SESSION.value,
}

# Apple Health XML exports category values as enum names (or sometimes integers).
_MENSTRUAL_FLOW_XML_VALUES: dict[str, int] = {
    "HKCategoryValueMenstrualFlowUnspecified": 1,
    "HKCategoryValueMenstrualFlowLight": 2,
    "HKCategoryValueMenstrualFlowMedium": 3,
    "HKCategoryValueMenstrualFlowHeavy": 4,
    "HKCategoryValueMenstrualFlowNone": 5,
}


def parse_apple_raw_value(raw_value: str | int | float | Decimal | None, metric_type: str) -> Decimal | None:
    """Parse a quantity/category raw value from SDK or XML into a Decimal.

    Returns None when the value is missing/invalid. Mindful sessions may return
    None for non-numeric XML enum strings; callers should fall back to duration.
    """
    if raw_value is None:
        return None
    if isinstance(raw_value, Decimal):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return Decimal(str(raw_value))

    text = str(raw_value).strip()
    if not text:
        return None

    if text in _MENSTRUAL_FLOW_XML_VALUES:
        return Decimal(_MENSTRUAL_FLOW_XML_VALUES[text])

    try:
        return Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def normalize_apple_unit(series_type: SeriesType, value: Decimal, provider: str | None = None) -> Decimal:
    """Apply series-level unit conversions shared by Apple SDK/XML paths."""
    match series_type:
        case SeriesType.height | SeriesType.walking_step_length:
            return value * 100
        case SeriesType.body_fat_percentage if provider == "apple":
            return value * 100
        case (
            SeriesType.walking_double_support_percentage
            | SeriesType.walking_asymmetry_percentage
            | SeriesType.walking_steadiness
        ):
            return value * 100
        case _:
            return value


def normalize_apple_sample_value(
    *,
    series_type: SeriesType,
    value: Decimal,
    metric_type: str,
    unit: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    provider: str | None = None,
) -> Decimal:
    """Normalize a mapped Apple sample into the OW series unit/scale.

    Handles mindful session duration, menstrual flow recode, fraction→percent,
    blood glucose mmol/L→mg/dL, dietary water L→mL, and caffeine g→mg.
    """
    is_mindful = metric_type in _MINDFUL_TYPES or series_type == SeriesType.mindful_minutes
    if is_mindful and start is not None and end is not None:
        duration_seconds = (end - start).total_seconds()
        value = Decimal(str(max(duration_seconds / 60.0, 0.0)))

    if series_type == SeriesType.menstrual_flow:
        value = Decimal(recode_menstrual_flow_value(int(value)))

    value = normalize_apple_unit(series_type, value, provider)

    unit_lower = (unit or "").lower()
    if series_type == SeriesType.blood_glucose and unit_lower.startswith("mmol"):
        value = value * MMOL_L_TO_MG_DL

    if series_type == SeriesType.hydration and unit_lower in {"l", "liter", "liters"}:
        value = value * Decimal("1000")

    if series_type == SeriesType.dietary_caffeine and unit_lower in {"g", "gram", "grams"}:
        value = value * Decimal("1000")

    return value
