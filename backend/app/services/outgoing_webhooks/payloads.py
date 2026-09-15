"""Validation and byte-aware splitting for outgoing webhook payloads."""

from __future__ import annotations

import json
from typing import Any

# Svix accepts messages up to 1 MiB. Keep headroom for its envelope and future
# schema fields instead of estimating bytes from a sample count.
MAX_WEBHOOK_PAYLOAD_BYTES = 900 * 1024


def encoded_payload_size(payload: dict[str, Any]) -> int:
    """Return the exact compact UTF-8 JSON size that is sent to Celery/Svix."""
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))


def validate_webhook_payload(event_type: str, payload: Any) -> dict[str, Any]:
    """Require the canonical ``{"type": str, "data": dict}`` envelope and size."""
    if not isinstance(payload, dict) or set(payload) != {"type", "data"}:
        raise ValueError("Webhook payload must be a dict containing only type and data")
    if payload.get("type") != event_type or not isinstance(payload.get("type"), str):
        raise ValueError("Webhook payload type must match event_type")
    if not isinstance(payload.get("data"), dict):
        raise ValueError("Webhook payload data must be a dict")
    try:
        size = encoded_payload_size(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("Webhook payload must be JSON serializable") from exc
    if size > MAX_WEBHOOK_PAYLOAD_BYTES:
        raise ValueError(f"Webhook payload is {size} bytes; limit is {MAX_WEBHOOK_PAYLOAD_BYTES}")
    return payload


def split_samples_by_payload_bytes(
    event_type: str,
    base_data: dict[str, Any],
    samples: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split samples so every final webhook envelope stays below the byte limit."""
    if not samples:
        return [[]]

    # Reserve the chunk metadata using deliberately wide integer placeholders.
    sizing_data = {**base_data, "samples": [], "chunk_index": 999_999, "total_chunks": 999_999}
    envelope_size = encoded_payload_size({"type": event_type, "data": sizing_data})

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = envelope_size

    for sample in samples:
        try:
            sample_size = len(
                json.dumps(sample, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Webhook sample must be JSON serializable") from exc
        added_size = sample_size + (1 if current else 0)
        if current and current_size + added_size > MAX_WEBHOOK_PAYLOAD_BYTES:
            chunks.append(current)
            current = []
            current_size = envelope_size
            added_size = sample_size
        if current_size + added_size > MAX_WEBHOOK_PAYLOAD_BYTES:
            raise ValueError("A single webhook sample exceeds the payload byte limit")
        current.append(sample)
        current_size += added_size

    if current:
        chunks.append(current)
    return chunks
