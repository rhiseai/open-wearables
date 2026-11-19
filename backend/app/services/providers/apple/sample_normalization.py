"""Shared Apple Health sample value normalization for SDK and XML imports."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from packaging.version import InvalidVersion, Version

from app.constants.series_types.sdk.category_types import (
    AppleCategoryType,
    recode_menstrual_flow_value,
)
from app.constants.series_types.sdk.metric_types import SDKMetricType
from app.schemas.enums import SeriesType

# Health Connect's own mg/dL converter uses exactly 18.0, so values written to HC
# in mg/dL round-trip with a ~0.1% offset under this factor.
MMOL_L_TO_MG_DL = Decimal("18.0182")

# First iOS SDK release that sends every `HKUnit.percent()` value on a 0-100 scale.
# Older builds scale only SpO2 and send the series below as a 0-1 fraction,
# which the backend scales x100.
APPLE_SDK_PERCENT_SCALE_MIN_VERSION = Version("0.17.0")

_FRACTION_PERCENT_SERIES = frozenset(
    {
        SeriesType.body_fat_percentage,
        SeriesType.walking_double_support_percentage,
        SeriesType.walking_asymmetry_percentage,
        SeriesType.walking_steadiness,
    }
)

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


def sends_fraction_percentages(provider: str | None, sdk_version: str | None) -> bool:
    """Whether percentage values from this SDK arrive as a 0-1 fraction.

    Only iOS SDK builds older than `APPLE_SDK_PERCENT_SCALE_MIN_VERSION` do. A missing or
    unparseable version is treated as an old build. Pre-releases of the min version count
    as new, since they are cut after the change.
    """
    if provider != "apple":
        return False
    try:
        version = Version(sdk_version or "")
    except InvalidVersion:
        return True
    return version.release < APPLE_SDK_PERCENT_SCALE_MIN_VERSION.release


def normalize_apple_unit(
    series_type: SeriesType,
    value: Decimal,
    provider: str | None = None,
    sdk_version: str | None = None,
) -> Decimal:
    """Series-level unit conversions of mobile SDK samples (the XML export converts by its unit attribute)."""
    match series_type:
        # meters → cm
        case SeriesType.height | SeriesType.walking_step_length:
            return value * 100
        # 0-1 fraction → percent (older iOS SDKs only; Health Connect and newer iOS SDKs send percent)
        case _ if series_type in _FRACTION_PERCENT_SERIES and sends_fraction_percentages(provider, sdk_version):
            return value * 100
        case _:
            return value


def normalize_apple_category_value(
    *,
    series_type: SeriesType,
    value: Decimal,
    metric_type: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Decimal:
    """Values that are not a quantity in its unit: a mindful session's minutes come from its
    duration, and menstrual flow is recoded to the OW scale. Shared by SDK and XML imports."""
    is_mindful = metric_type in _MINDFUL_TYPES or series_type == SeriesType.mindful_minutes
    if is_mindful and start is not None and end is not None:
        duration_seconds = (end - start).total_seconds()
        value = Decimal(str(max(duration_seconds / 60.0, 0.0)))

    if series_type == SeriesType.menstrual_flow:
        value = Decimal(recode_menstrual_flow_value(int(value)))
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
    sdk_version: str | None = None,
) -> Decimal:
    """Normalize a mobile SDK sample into the OW series unit/scale.

    Handles mindful session duration, menstrual flow recode, meters→cm, fraction→percent
    (iOS SDKs before `APPLE_SDK_PERCENT_SCALE_MIN_VERSION`), blood glucose mmol/L→mg/dL,
    dietary water L→mL, and caffeine g→mg.
    """
    value = normalize_apple_category_value(
        series_type=series_type, value=value, metric_type=metric_type, start=start, end=end
    )
    value = normalize_apple_unit(series_type, value, provider, sdk_version)

    unit_lower = (unit or "").strip().lower()
    if series_type == SeriesType.blood_glucose and unit_lower.startswith("mmol"):
        value = value * MMOL_L_TO_MG_DL

    if series_type == SeriesType.hydration and unit_lower in {"l", "liter", "liters", "litre", "litres"}:
        value = value * Decimal("1000")

    if series_type == SeriesType.dietary_caffeine and unit_lower in {"g", "gram", "grams"}:
        value = value * Decimal("1000")

    return value
