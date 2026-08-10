from enum import StrEnum


class AppleCategoryType(StrEnum):
    """
    Apple HealthKit category type identifiers (HKCategoryTypeIdentifier...).

    Sleep is handled via the dedicated sleep pipeline. Other category types
    arrive in ``data.records`` as integer-valued samples and map to SeriesType.
    """

    SLEEP_ANALYSIS = "HKCategoryTypeIdentifierSleepAnalysis"
    MENSTRUAL_FLOW = "HKCategoryTypeIdentifierMenstrualFlow"
    CERVICAL_MUCUS_QUALITY = "HKCategoryTypeIdentifierCervicalMucusQuality"
    OVULATION_TEST_RESULT = "HKCategoryTypeIdentifierOvulationTestResult"
    MINDFUL_SESSION = "HKCategoryTypeIdentifierMindfulSession"


# Category types set (for backwards compatibility and validation)
CATEGORY_TYPE_IDENTIFIERS: set[AppleCategoryType] = {
    AppleCategoryType.SLEEP_ANALYSIS,
    AppleCategoryType.MENSTRUAL_FLOW,
    AppleCategoryType.CERVICAL_MUCUS_QUALITY,
    AppleCategoryType.OVULATION_TEST_RESULT,
    AppleCategoryType.MINDFUL_SESSION,
}

# HKCategoryValueMenstrualFlow → OW canonical scale.
# Apple: 1=unspecified, 2=light, 3=medium, 4=heavy, 5=none
# OW:    0=none, 1=unspecified, 2=light, 3=medium, 4=heavy
MENSTRUAL_FLOW_VALUE_RECODE: dict[int, int] = {
    1: 1,  # unspecified
    2: 2,  # light
    3: 3,  # medium
    4: 4,  # heavy
    5: 0,  # none
}

# Metadata key on menstrualFlow samples (stringified by SDK as "1"/"0" or "true"/"false").
HK_MENSTRUAL_CYCLE_START_KEY = "HKMenstrualCycleStart"


def recode_menstrual_flow_value(raw: int | float) -> int:
    """Map HealthKit menstrualFlow category value onto the OW 0–4 scale."""
    return MENSTRUAL_FLOW_VALUE_RECODE.get(int(raw), int(raw))


def is_menstrual_cycle_start(metadata: list[dict] | dict | None) -> bool:
    """Parse HKMenstrualCycleStart from SDK-stringified metadata."""
    if metadata is None:
        return False
    if isinstance(metadata, list):
        # Some payloads send metadata as a list of {key: value} dicts.
        merged: dict = {}
        for item in metadata:
            if isinstance(item, dict):
                merged.update(item)
        metadata = merged
    if not isinstance(metadata, dict):
        return False
    raw = metadata.get(HK_MENSTRUAL_CYCLE_START_KEY)
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    return text in {"1", "true", "yes"}
