"""Unit tests for Apple menstrual / dietary / mindful series mapping helpers."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.constants.series_types.sdk.category_types import (
    is_menstrual_cycle_start,
    recode_menstrual_flow_value,
)
from app.constants.series_types.sdk.metric_types import (
    SDKMetricType,
    get_series_type_from_metric_type,
)
from app.schemas.enums import AggregationMethod, SeriesType
from app.schemas.enums.aggregation_method import get_aggregation_method
from app.services.providers.apple.sample_normalization import (
    normalize_apple_sample_value,
    parse_apple_raw_value,
)
from app.services.sdk.menstrual_service import _assemble_periods, _Period


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1, 1),
        (2, 2),
        (3, 3),
        (4, 4),
        (5, 0),
    ],
)
def test_recode_menstrual_flow_value(raw: int, expected: int) -> None:
    assert recode_menstrual_flow_value(raw) == expected


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (None, False),
        ({}, False),
        ({"HKMenstrualCycleStart": "1"}, True),
        ({"HKMenstrualCycleStart": "0"}, False),
        ({"HKMenstrualCycleStart": "true"}, True),
        ({"HKMenstrualCycleStart": True}, True),
        ([{"HKMenstrualCycleStart": "1"}], True),
    ],
)
def test_is_menstrual_cycle_start(metadata: object, expected: bool) -> None:
    assert is_menstrual_cycle_start(metadata) is expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("metric", "series"),
    [
        (SDKMetricType.APPLE_DIETARY_ENERGY_CONSUMED, SeriesType.dietary_energy_consumed),
        (SDKMetricType.APPLE_DIETARY_WATER, SeriesType.hydration),
        (SDKMetricType.APPLE_DIETARY_FIBER, SeriesType.dietary_fiber),
        (SDKMetricType.APPLE_DIETARY_CAFFEINE, SeriesType.dietary_caffeine),
        (SDKMetricType.APPLE_MENSTRUAL_FLOW, SeriesType.menstrual_flow),
        (SDKMetricType.APPLE_MINDFUL_SESSION, SeriesType.mindful_minutes),
        (SDKMetricType.BASAL_BODY_TEMPERATURE, SeriesType.basal_body_temperature),
        (SDKMetricType.APPLE_CERVICAL_MUCUS_QUALITY, SeriesType.cervical_mucus_quality),
        (SDKMetricType.APPLE_OVULATION_TEST_RESULT, SeriesType.ovulation_test_result),
    ],
)
def test_metric_type_mapping(metric: SDKMetricType, series: SeriesType) -> None:
    assert get_series_type_from_metric_type(metric) is series
    assert get_series_type_from_metric_type(metric.value) is series


@pytest.mark.parametrize(
    "series",
    [
        SeriesType.dietary_energy_consumed,
        SeriesType.dietary_protein,
        SeriesType.dietary_carbohydrates,
        SeriesType.dietary_fat_total,
        SeriesType.dietary_fiber,
        SeriesType.dietary_sugar,
        SeriesType.dietary_caffeine,
        SeriesType.hydration,
        SeriesType.mindful_minutes,
    ],
)
def test_sum_aggregation_for_consumables(series: SeriesType) -> None:
    assert get_aggregation_method(series) is AggregationMethod.SUM


def test_assemble_periods_gap_and_cycle_start() -> None:
    days = [
        date(2025, 3, 1),
        date(2025, 3, 2),
        date(2025, 3, 3),
        # gap > 1
        date(2025, 3, 10),
        date(2025, 3, 11),
    ]
    periods = _assemble_periods(days, cycle_starts=set())
    assert periods == [
        _Period(start=date(2025, 3, 1), end=date(2025, 3, 3)),
        _Period(start=date(2025, 3, 10), end=date(2025, 3, 11)),
    ]

    # Explicit cycle start splits consecutive days
    consecutive = [date(2025, 4, 1), date(2025, 4, 2), date(2025, 4, 3)]
    split = _assemble_periods(consecutive, cycle_starts={date(2025, 4, 3)})
    assert split == [
        _Period(start=date(2025, 4, 1), end=date(2025, 4, 2)),
        _Period(start=date(2025, 4, 3), end=date(2025, 4, 3)),
    ]


def test_period_external_id() -> None:
    assert _Period(start=date(2025, 3, 1), end=date(2025, 3, 4)).external_id == "apple-menstrual-2025-03-01"
    assert _Period(start=date(2025, 3, 1), end=date(2025, 3, 4)).period_length == 4


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HKCategoryValueMenstrualFlowLight", Decimal("2")),
        ("HKCategoryValueMenstrualFlowHeavy", Decimal("4")),
        ("3", Decimal("3")),
        ("HKCategoryValueMindfulSession", None),
    ],
)
def test_parse_apple_raw_value_xml_enums(raw: str, expected: Decimal | None) -> None:
    assert parse_apple_raw_value(raw, "HKCategoryTypeIdentifierMenstrualFlow") == expected


def test_normalize_xml_style_values() -> None:
    start = datetime(2025, 4, 10, 8, 0, tzinfo=timezone.utc)
    end = datetime(2025, 4, 10, 8, 12, tzinfo=timezone.utc)

    mindful = normalize_apple_sample_value(
        series_type=SeriesType.mindful_minutes,
        value=Decimal("0"),
        metric_type="HKCategoryTypeIdentifierMindfulSession",
        start=start,
        end=end,
        provider="apple",
    )
    assert mindful == Decimal("12")

    water = normalize_apple_sample_value(
        series_type=SeriesType.hydration,
        value=Decimal("0.5"),
        metric_type="HKQuantityTypeIdentifierDietaryWater",
        unit="L",
        provider="apple",
    )
    assert water == Decimal("500")

    caffeine = normalize_apple_sample_value(
        series_type=SeriesType.dietary_caffeine,
        value=Decimal("0.08"),
        metric_type="HKQuantityTypeIdentifierDietaryCaffeine",
        unit="g",
        provider="apple",
    )
    assert caffeine == Decimal("80")

    flow = normalize_apple_sample_value(
        series_type=SeriesType.menstrual_flow,
        value=Decimal("5"),
        metric_type="HKCategoryTypeIdentifierMenstrualFlow",
        provider="apple",
    )
    assert flow == Decimal("0")
