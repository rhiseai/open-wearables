"""Short-lived state used to recognize a mobile SDK historical export."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from uuid import UUID

from app.integrations.redis_client import get_redis_client

logger = logging.getLogger(__name__)
HISTORICAL_SYNC_TTL_SECONDS = 6 * 60 * 60
_EMPTY_SYNC_SENTINEL = "__sync__"

# A large upload is treated as historical even if the diagnostic marker raced
# or Redis was temporarily unavailable.
SDK_REALTIME_ITEM_LIMIT = 250
_SDK_BATCH_DATA_TYPES = ("records", "workouts", "sleep")


def sdk_payload_exceeds_realtime_limit(data: object) -> bool:
    """Check the realtime threshold independently for each SDK payload type."""
    if not isinstance(data, dict):
        return False
    return any(
        isinstance(items, list) and len(items) > SDK_REALTIME_ITEM_LIMIT
        for key in _SDK_BATCH_DATA_TYPES
        if (items := data.get(key)) is not None
    )


def _key(user_id: str | UUID, provider: str) -> str:
    return f"sdk:historical-sync:{user_id}:{provider.lower()}"


def mark_historical_sync_started(user_id: str | UUID, provider: str, data_types: Iterable[str]) -> None:
    try:
        values = {value for value in data_types if value} or {_EMPTY_SYNC_SENTINEL}
        key = _key(user_id, provider)
        pipeline = get_redis_client().pipeline(transaction=True)
        pipeline.delete(key)
        pipeline.sadd(key, *values)
        pipeline.expire(key, HISTORICAL_SYNC_TTL_SECONDS)
        pipeline.execute()
    except Exception:
        logger.warning("Could not mark SDK historical sync", exc_info=True)


def mark_historical_data_types_completed(user_id: str | UUID, provider: str, data_types: Iterable[str]) -> None:
    """Clear the marker after the SDK reports every declared data type complete."""
    try:
        key = _key(user_id, provider)
        client = get_redis_client()
        values = {value for value in data_types if value}
        if values:
            client.srem(key, *values)
        if client.scard(key) == 0:
            client.delete(key)
    except Exception:
        logger.warning("Could not complete SDK historical sync state", exc_info=True)


def is_historical_sync_active(user_id: str | UUID, provider: str) -> bool:
    try:
        # ``exists`` also recognizes the legacy string marker during rollout;
        # current markers are Redis sets of the remaining data types.
        return bool(get_redis_client().exists(_key(user_id, provider)))
    except Exception:
        logger.warning("Could not read SDK historical sync state", exc_info=True)
        return False
