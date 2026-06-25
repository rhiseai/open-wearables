from enum import Enum


class ProviderName(str, Enum):
    """Supported data providers."""

    APPLE = "apple"
    SAMSUNG = "samsung"
    GOOGLE = "google"
    GARMIN = "garmin"
    POLAR = "polar"
    SUUNTO = "suunto"
    WHOOP = "whoop"
    STRAVA = "strava"
    OURA = "oura"
    FITBIT = "fitbit"
    ULTRAHUMAN = "ultrahuman"
    UNKNOWN = "unknown"
    INTERNAL = "internal"

    @classmethod
    def from_source_string(cls, source: str | None) -> "ProviderName":
        """Infer provider from a source string by checking if provider name appears in it.

        Args:
            source: Source string (e.g., "apple_health_sdk", "Garmin Connect")

        Returns:
            Matching ProviderName or UNKNOWN if no match found
        """
        if not source:
            return cls.UNKNOWN

        source_lower = source.lower()
        # Check each provider (except UNKNOWN) to see if it appears in the source string
        for provider in cls:
            if provider in (cls.UNKNOWN, cls.INTERNAL):
                continue
            if provider.value in source_lower:
                return provider

        return cls.UNKNOWN


DEFAULT_PROVIDER_PRIORITY: dict[ProviderName, int] = {
    ProviderName.OURA: 1,
    ProviderName.WHOOP: 2,
    ProviderName.GARMIN: 3,
    ProviderName.POLAR: 4,
    ProviderName.SUUNTO: 5,
    ProviderName.FITBIT: 6,
    ProviderName.ULTRAHUMAN: 7,
    ProviderName.APPLE: 8,
}
