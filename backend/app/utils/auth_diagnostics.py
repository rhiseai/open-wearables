"""Bounded auth decisions without credentials, identifiers, or request context."""

from contextlib import suppress
from enum import StrEnum
from logging import getLogger

from app.utils.structured_logging import log_structured

logger = getLogger(__name__)


class AuthReason(StrEnum):
    CREDENTIALS_MISSING = "credentials_missing"
    SDK_TOKEN_EXPIRED = "sdk_token_expired"
    SDK_TOKEN_INVALID = "sdk_token_invalid"
    SDK_TOKEN_WRONG_SCOPE = "sdk_token_wrong_scope"
    API_KEY_INVALID = "api_key_invalid"
    SUBJECT_MISMATCH = "subject_mismatch"
    REFRESH_INVALID_OR_REVOKED = "refresh_invalid_or_revoked"
    REFRESH_ROTATED = "refresh_rotated"


class AuthOutcome(StrEnum):
    REJECTED = "rejected"
    RECOVERED = "recovered"
    ACCEPTED = "accepted"


class AuthBoundary(StrEnum):
    SDK_CREDENTIALS = "sdk_credentials"
    SDK_SUBJECT = "sdk_subject"
    REFRESH = "refresh"


def log_auth_decision(
    reason: AuthReason,
    outcome: AuthOutcome,
    *,
    boundary: AuthBoundary = AuthBoundary.SDK_CREDENTIALS,
) -> None:
    """Record the final decision at an auth boundary using fixed values only."""
    # A closed/unavailable log stream must not change authentication behavior.
    with suppress(OSError, ValueError):
        log_structured(
            logger,
            "warning" if outcome is AuthOutcome.REJECTED else "info",
            "auth_decision",
            inherit_trace_id=False,
            action="auth_decision",
            auth_boundary=boundary,
            auth_reason=reason,
            auth_outcome=outcome,
        )
