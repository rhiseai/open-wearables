"""Remove credentials from Sentry payloads without changing provider requests."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sentry_sdk.scrubber import DEFAULT_DENYLIST

if TYPE_CHECKING:
    from sentry_sdk._types import Breadcrumb, Event, Hint

FILTERED = "[Filtered]"


def _normalize_key(key: str) -> str:
    return re.sub(r"[-_.\s]", "", key).casefold()


_PRIVATE_KEYS = {
    _normalize_key(key)
    for key in (
        *DEFAULT_DENYLIST,
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "client_assertion",
        "code",
        "code_verifier",
        "query",
        "query_string",
        "query_params",
        "fragment",
        "http.query",
        "http.fragment",
        "url.query",
        "url.fragment",
    )
}
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_URL_USERINFO = re.compile(r"(?<=//)[^/@]+@")
_BEARER = re.compile(r"\b(Bearer)[ \t]+[^\s,;\"'<>]+", re.IGNORECASE)
# Log formatting can turn credential dictionaries into JSON or Python repr strings.
# Match those values too, including a value cut off by Sentry's string limit.
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(?P<prefix>(?:access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"client[_-]?secret|client[_-]?assertion|code[_-]?verifier|api[_-]?key|"
    r"proxy[_-]?authorization|authorization|password|passwd|secret|token|code)"
    r"[\"']?\s*[:=]\s*)"
    r"(?:\[Filtered\]|\"(?:\\.|[^\"\\])*(?:\"|\\?$)|'(?:\\.|[^'\\])*(?:'|\\?$)|"
    r"(?:Bearer|Basic)[ \t]+[^\s,;\"'<>]+|[^\s,;&}\]\)\"']+)",
    re.IGNORECASE,
)


def _redact_url(match: re.Match[str]) -> str:
    # Splitting also handles incomplete URLs in truncated exception messages.
    endpoint = re.split(r"[?#]", match.group(), maxsplit=1)[0]
    return _URL_USERINFO.sub(f"{FILTERED}@", endpoint, count=1)


def _redact_text(value: str) -> str:
    # Preserve escaped quote boundaries before URL or bearer matching can consume them.
    value = _CREDENTIAL_ASSIGNMENT.sub(lambda match: match["prefix"] + FILTERED, value)
    value = _URL.sub(_redact_url, value)
    return _BEARER.sub(rf"\1 {FILTERED}", value)


def redact_telemetry(value: Any) -> Any:
    """Copy a telemetry tree, filtering secrets in fields and embedded strings."""
    if isinstance(value, dict):
        return {
            key: FILTERED if isinstance(key, str) and _normalize_key(key) in _PRIVATE_KEYS else redact_telemetry(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_telemetry(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_telemetry(item) for item in value)
    if isinstance(value, str):
        return _redact_text(value)
    return value


def before_send(event: Event, hint: Hint) -> Event:
    """Filter fully assembled error events and transactions before transmission."""
    return redact_telemetry(event)


def before_breadcrumb(breadcrumb: Breadcrumb, hint: Hint) -> Breadcrumb:
    """Filter HTTP and logging breadcrumbs before they enter the scope."""
    return redact_telemetry(breadcrumb)
