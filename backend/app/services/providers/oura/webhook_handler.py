"""Oura webhook handler.

Oura sends notify-only webhooks: a lightweight payload containing the user ID,
data type, event type, and object_id of the changed resource. The actual data
is fetched via GET /v2/usercollection/{data_type}/{object_id}.

Signature scheme
----------------
  Message  : timestamp_string + raw_request_body
  Algorithm: HMAC-SHA256(client_secret, message)
  Encoding : hex (upper-case)
  Headers  : x-oura-signature, x-oura-timestamp

Challenge verification
-----------------------
  When a subscription is created, Oura sends a GET request with
  ``verification_token`` and ``challenge`` query params. The handler must
  verify the token and echo the challenge back.

The endpoint must respond quickly; ``dispatch()`` stores the raw payload and
enqueues a Celery task, returning 200 immediately. ``process_payload()`` does
the actual API fetch and DB write, called by the task.

Fan-out
-------
  Oura names only the ring in its payload, and one ring can be connected to
  several OW profiles. The object is fetched once — any linked profile's token
  reads the same account — and then saved for every profile sharing it. The
  oldest connection is the primary; resolving a single one would leave every
  other profile silent forever, since Oura has no polling path to catch up.

Supported data types
---------------------
  workout / daily_sleep / sleep / daily_readiness / daily_activity / daily_spo2

See: https://cloud.ouraring.com/v2/docs#tag/Webhook-Subscription-Routes
"""

import json
import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from celery import current_app as celery_app
from fastapi import HTTPException, Request
from pydantic import ValidationError

from app.config import settings
from app.database import DbSession
from app.models.user_connection import UserConnection
from app.repositories import UserConnectionRepository
from app.schemas.providers.oura import OuraWebhookNotification
from app.services.providers.oura.data_247 import Oura247Data
from app.services.providers.oura.workouts import OuraWorkouts
from app.services.providers.templates.base_webhook_handler import BaseWebhookHandler
from app.services.raw_payload_storage import store_raw_payload
from app.utils.structured_logging import LogContext, log_structured

logger = logging.getLogger(__name__)

_PROCESS_PUSH_TASK = "app.integrations.celery.tasks.webhook_push_task.process_webhook_push"

# Upstream statuses that condemn one profile's token rather than the object
# itself, so the fetch is worth retrying with another linked profile's token.
_TOKEN_FAILURE_STATUSES = frozenset({401, 403})

SUPPORTED_DATA_TYPES = [
    "workout",
    "sleep",
    "daily_sleep",
    "daily_readiness",
    "daily_activity",
    "daily_spo2",
    "daily_cardiovascular_age",
    "vo2_max",
]

# Oura webhook data_type → REST collection name (only entries that differ)
_COLLECTION_NAME: dict[str, str] = {
    "vo2_max": "vO2_max",
}


class OuraWebhookHandler(BaseWebhookHandler):
    """Webhook handler for Oura notify-only events."""

    user_id_field = "user_id"

    def __init__(self, data_247: Oura247Data, workouts: OuraWorkouts) -> None:
        super().__init__("oura")
        self.data_247 = data_247
        self.workouts = workouts
        self.connection_repo = UserConnectionRepository()

    # ------------------------------------------------------------------
    # BaseWebhookHandler interface
    # ------------------------------------------------------------------

    def verify_signature(self, request: Request, body: bytes) -> bool:
        """Verify x-oura-signature using HMAC-SHA256 + hex (upper-case)."""
        secret_setting = settings.oura_client_secret
        if not secret_setting:
            log_structured(
                logger,
                "error",
                "OURA_CLIENT_SECRET not configured; rejecting webhook",
                provider="oura",
                action="webhook_signature_missing_secret",
            )
            return False

        signature = request.headers.get("x-oura-signature")
        timestamp = request.headers.get("x-oura-timestamp")

        if not signature or not timestamp:
            log_structured(
                logger,
                "warning",
                "Missing Oura webhook signature headers",
                provider="oura",
                action="webhook_signature_missing",
                has_signature=bool(signature),
                has_timestamp=bool(timestamp),
            )
            return False

        secret = secret_setting.get_secret_value()
        return self._verify_hmac_sha256(
            secret,
            body,
            signature,
            prefix=timestamp.encode(),
            case_insensitive=True,
        )

    def parse_payload(self, body: bytes) -> dict[str, Any]:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    def dispatch(self, db: DbSession, payload: dict[str, Any]) -> dict[str, Any]:
        """Store the raw payload and enqueue async processing. Returns immediately."""
        request_trace_id = str(uuid4())[:8]
        event_type = payload.get("event_type", "unknown")
        data_type = payload.get("data_type", "unknown")
        provider_user_id = payload.get("user_id", "unknown")

        log_structured(
            logger,
            "info",
            "Received Oura webhook",
            provider="oura",
            trace_id=request_trace_id,
            event_type=event_type,
            data_type=data_type,
            provider_user_id=provider_user_id,
        )

        store_raw_payload(source="webhook", provider="oura", payload=payload, trace_id=request_trace_id)

        task = celery_app.send_task(_PROCESS_PUSH_TASK, args=["oura", payload, request_trace_id], queue="webhook_sync")
        log_structured(
            logger,
            "info",
            "Enqueued Oura webhook processing task",
            provider="oura",
            trace_id=request_trace_id,
            provider_user_id=provider_user_id,
            task_id=getattr(task, "id", None),
        )

        return {"status": "accepted"}

    def handle_challenge(self, request: Request) -> dict[str, Any]:
        """Handle Oura GET subscription verification challenge.

        Oura sends ``?verification_token=...&challenge=...`` when a subscription
        is created. We verify the token and echo the challenge back.
        """
        expected = settings.oura_webhook_verification_token.get_secret_value()  # ty:ignore[unresolved-attribute]

        verification_token = request.query_params.get("verification_token")
        challenge = request.query_params.get("challenge", "")

        if not verification_token or not self._verify_token(expected, verification_token):
            raise HTTPException(status_code=401, detail="Invalid verification token")

        return {"challenge": challenge}

    def supported_event_types(self) -> list[str]:
        return SUPPORTED_DATA_TYPES

    # ------------------------------------------------------------------
    # Async processing (called by Celery task)
    # ------------------------------------------------------------------

    def process_payload(self, db: DbSession, payload: dict[str, Any], trace_id: str) -> dict[str, Any]:
        """Process an Oura notify-only payload synchronously.

        Called by the ``process_webhook_push`` Celery task with its own DB session.
        """
        try:
            notification = OuraWebhookNotification(**payload)
        except (ValidationError, TypeError) as exc:
            log_structured(
                logger,
                "warning",
                "Invalid Oura webhook payload",
                provider="oura",
                trace_id=trace_id,
                provider_user_id=payload.get("user_id", "unknown"),
                data_type=payload.get("data_type", "unknown"),
                error=str(exc),
            )
            return {"status": "error", "error": f"Invalid payload: {exc}"}

        if notification.event_type == "delete":
            log_structured(
                logger,
                "info",
                "Ignoring Oura delete event",
                provider="oura",
                trace_id=trace_id,
                provider_user_id=notification.user_id,
                data_type=notification.data_type,
            )
            return {"status": "ignored", "reason": "delete_event"}

        connections = self.connection_repo.get_all_by_provider_user_id(db, "oura", notification.user_id)
        if not connections:
            log_structured(
                logger,
                "warning",
                "No connection found for Oura user",
                provider="oura",
                trace_id=trace_id,
                provider_user_id=notification.user_id,
                data_type=notification.data_type,
            )
            return {
                "status": "user_not_found",
                "oura_user_id": notification.user_id,
                "data_type": notification.data_type,
            }

        # One ring can be connected to several OW profiles, and Oura names only
        # the ring in its payload. Resolving a single connection therefore sends
        # every delivery to one profile and starves the rest indefinitely —
        # Oura has no polling fallback to catch them up. ``connections`` is
        # ordered oldest-first, so the primary stays stable across deliveries.
        user_id: UUID = connections[0].user_id
        linked_user_ids: list[UUID] = [connection.user_id for connection in connections[1:]]

        log_structured(
            logger,
            "info",
            "Processing Oura webhook notification",
            provider="oura",
            trace_id=trace_id,
            user_id=str(user_id),
            linked_user_ids=[str(linked) for linked in linked_user_ids],
            provider_user_id=notification.user_id,
            data_type=notification.data_type,
            event_type=notification.event_type,
            object_id=notification.object_id,
        )

        for connection in connections:
            self.connection_repo.update_last_synced_at(db, connection)

        raw = self._fetch_object_for_connections(db, notification, connections, trace_id)

        saved: dict[UUID, Any] = {}
        if raw is not None:
            for connection in connections:
                count = self._save_object(db, notification, raw, connection.user_id, trace_id)
                if count is None:
                    # Unhandled data types are a property of the payload, not of
                    # the profile, so the first verdict settles it for all.
                    log_structured(
                        logger,
                        "info",
                        "Unhandled Oura data type",
                        provider="oura",
                        trace_id=trace_id,
                        data_type=notification.data_type,
                        user_id=str(user_id),
                        provider_user_id=notification.user_id,
                    )
                    return {
                        "status": "ignored",
                        "reason": f"unhandled_data_type: {notification.data_type}",
                        "user_id": str(user_id),
                    }
                saved[connection.user_id] = count

        # The primary's count is what ``records_saved`` has always meant: the
        # records one profile received. Summing the fan-out here would report a
        # single night of sleep twice in the sync log.
        primary_count: Any = saved.get(user_id, 0)

        log_structured(
            logger,
            "info",
            "Oura webhook notification processed",
            provider="oura",
            action="oura_webhook_complete",
            trace_id=trace_id,
            user_id=str(user_id),
            linked_user_ids=[str(linked) for linked in linked_user_ids],
            provider_user_id=notification.user_id,
            data_type=notification.data_type,
            event_type=notification.event_type,
            records_saved=int(primary_count),
            records_saved_total=sum(int(count) for count in saved.values()),
            records_inserted=getattr(primary_count, "inserted", None),
            records_updated=getattr(primary_count, "updated", None),
        )
        return {
            "status": "processed",
            "data_type": notification.data_type,
            "event_type": notification.event_type,
            "records_saved": primary_count,
            "user_id": str(user_id),
            "linked_user_ids": [str(linked) for linked in linked_user_ids],
        }

    # ------------------------------------------------------------------
    # Per-data-type handlers
    # ------------------------------------------------------------------

    def _dispatch_data_type(
        self,
        db: DbSession,
        notification: OuraWebhookNotification,
        user_id: UUID,
        trace_id: str,
    ) -> int | None:
        """Fetch the changed object with ``user_id``'s token and save it for them.

        ``process_payload`` drives the two halves separately so one fetch can
        feed every profile sharing the ring; this keeps the single-profile path
        readable as one call.
        """
        raw = self._fetch_object(db, notification, user_id, trace_id)
        if raw is None:
            return 0
        return self._save_object(db, notification, raw, user_id, trace_id)

    def _fetch_object_for_connections(
        self,
        db: DbSession,
        notification: OuraWebhookNotification,
        connections: Sequence[UserConnection],
        trace_id: str,
    ) -> dict[str, Any] | None:
        """Fetch the changed object once, trying each profile's token in turn.

        Every connection here points at the same Oura account, so any of their
        tokens can read the object. Falling through on an auth failure keeps a
        dead token on the oldest profile from starving all the others — the bug
        this fan-out exists to fix, one step removed.
        """
        auth_error: HTTPException | None = None
        for connection in connections:
            try:
                return self._fetch_object(db, notification, connection.user_id, trace_id)
            except HTTPException as exc:
                if exc.status_code not in _TOKEN_FAILURE_STATUSES:
                    raise
                auth_error = exc
                log_structured(
                    logger,
                    "warning",
                    "Oura rejected this profile's token; trying the next linked profile",
                    provider="oura",
                    trace_id=trace_id,
                    user_id=str(connection.user_id),
                    provider_user_id=notification.user_id,
                    data_type=notification.data_type,
                    status_code=exc.status_code,
                )
        if auth_error is not None:
            raise auth_error
        return None

    def _fetch_object(
        self,
        db: DbSession,
        notification: OuraWebhookNotification,
        user_id: UUID,
        trace_id: str,
    ) -> dict[str, Any] | None:
        """Read the changed object from Oura with ``user_id``'s token.

        Returns None when there is nothing to save: a payload carrying no
        object_id, or a fetch that came back empty.
        """
        data_type = notification.data_type
        object_id = notification.object_id

        if not object_id:
            log_structured(
                logger,
                "warning",
                "Oura webhook missing object_id; skipping fetch",
                provider="oura",
                trace_id=trace_id,
                user_id=str(user_id),
                provider_user_id=notification.user_id,
                data_type=data_type,
                event_type=notification.event_type,
            )
            return None

        if data_type == "workout":
            raw = self.workouts.get_workout_detail_from_api(db, user_id, object_id)
        else:
            collection = _COLLECTION_NAME.get(data_type, data_type)
            raw = self.data_247._make_api_request(db, user_id, f"/v2/usercollection/{collection}/{object_id}")

        if not raw or not isinstance(raw, dict):
            log_structured(
                logger,
                "warning",
                "Oura object fetch returned no data",
                provider="oura",
                trace_id=trace_id,
                user_id=str(user_id),
                provider_user_id=notification.user_id,
                data_type=data_type,
                object_id=object_id,
            )
            return None

        store_raw_payload(
            source="api_response",
            provider="oura",
            payload=raw,
            user_id=str(user_id),
            trace_id=trace_id,
        )
        return raw

    def _save_object(
        self,
        db: DbSession,
        notification: OuraWebhookNotification,
        raw: dict[str, Any],
        user_id: UUID,
        trace_id: str,
    ) -> int | None:
        """Persist one already-fetched object for one profile.

        Returns None for a data type this handler does not store.
        """
        data_type = notification.data_type

        if data_type == "workout":
            return self.workouts.save_from_raw(db, user_id, raw, str(notification.object_id), trace_id=trace_id)

        docs = [raw]

        log_ctx = LogContext(provider_user_id=notification.user_id, trace_id=trace_id)

        match data_type:
            case "sleep":
                return self.data_247.save_sleep_data(
                    db, user_id, self.data_247.normalize_sleeps(docs, user_id), log_ctx
                )
            case "daily_sleep":
                # daily_sleep is Oura's daily sleep *score* (not a sleep session).
                # It must go through the score path, not normalize_sleeps — otherwise
                # the score is silently dropped and never lands in health_score.
                return self.data_247.save_daily_sleep_scores(
                    db, user_id, self.data_247.normalize_daily_sleep_scores(docs, user_id)
                )
            case "daily_readiness":
                return self.data_247.save_readiness_data(
                    db, user_id, self.data_247.normalize_readiness(docs, user_id), log_ctx
                )
            case "daily_activity":
                return self.data_247.save_activity_data(
                    db, user_id, self.data_247.normalize_activity_samples(docs, user_id), log_ctx
                )
            case "daily_spo2":
                return self.data_247.save_spo2_data(db, user_id, docs, log_ctx)
            case "daily_cardiovascular_age":
                return self.data_247.save_cardiovascular_age_data(
                    db, user_id, self.data_247.normalize_cardiovascular_age_samples(docs, user_id), log_ctx
                )
            case "vo2_max":
                return self.data_247.save_vo2_data(db, user_id, docs, log_ctx)
            case _:
                log_structured(
                    logger,
                    "warning",
                    "Unhandled Oura data type",
                    provider="oura",
                    trace_id=trace_id,
                    user_id=str(user_id),
                    provider_user_id=notification.user_id,
                    data_type=data_type,
                    event_type=notification.event_type,
                )
                return None
