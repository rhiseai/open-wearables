from enum import StrEnum


class SummarySourceSelection(StrEnum):
    """Whether daily summaries choose globally or expose one candidate per provider."""

    PRIORITY = "priority"
    PER_PROVIDER = "per_provider"
