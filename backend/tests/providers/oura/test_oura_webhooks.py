"""Tests for Oura webhook schemas and service."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.schemas.providers.oura import OuraWebhookNotification
from app.services.providers.factory import ProviderFactory
from app.services.providers.oura.webhook_handler import OuraWebhookHandler
from app.utils.exceptions import ProviderAuthRejectedError, ProviderError
from tests.factories import UserConnectionFactory

OURA_USER_ID = "oura-user-1825"


class TestOuraWebhookNotification:
    """Test webhook notification payload parsing."""

    def test_parse_valid_notification(self) -> None:
        payload = {
            "event_type": "create",
            "data_type": "daily_sleep",
            "user_id": "oura-user-123",
            "object_id": "abc-123",
            "event_time": "2024-01-15T08:00:00+00:00",
        }
        notification = OuraWebhookNotification(**payload)

        assert notification.event_type == "create"
        assert notification.data_type == "daily_sleep"
        assert notification.user_id == "oura-user-123"
        assert notification.object_id == "abc-123"
        assert notification.event_time == "2024-01-15T08:00:00+00:00"

    def test_parse_minimal_notification(self) -> None:
        payload = {
            "event_type": "update",
            "data_type": "workout",
            "user_id": "oura-user-456",
        }
        notification = OuraWebhookNotification(**payload)

        assert notification.event_type == "update"
        assert notification.data_type == "workout"
        assert notification.user_id == "oura-user-456"
        assert notification.object_id is None
        assert notification.event_time is None

    def test_parse_delete_event(self) -> None:
        payload = {
            "event_type": "delete",
            "data_type": "daily_activity",
            "user_id": "oura-user-789",
        }
        notification = OuraWebhookNotification(**payload)
        assert notification.event_type == "delete"

    def test_missing_required_field_raises_error(self) -> None:
        payload = {
            "event_type": "create",
            "data_type": "daily_sleep",
            # missing user_id
        }
        with pytest.raises(ValidationError):
            OuraWebhookNotification(**payload)

    def test_all_data_types(self) -> None:
        data_types = [
            "daily_activity",
            "daily_readiness",
            "daily_sleep",
            "daily_spo2",
            "workout",
            "tag",
        ]
        for dt in data_types:
            notification = OuraWebhookNotification(
                event_type="create",
                data_type=dt,
                user_id="test-user",
            )
            assert notification.data_type == dt


class TestOuraWebhookDispatch:
    """Regression: each data_type must route to its correct save path.

    ``daily_sleep`` is Oura's daily sleep *score* — it must go through
    ``normalize_daily_sleep_scores``/``save_daily_sleep_scores``, NOT the
    sleep-*session* path (``normalize_sleeps``), which silently drops the
    score and leaves the user with no ``oura`` sleep health_score.
    """

    def _handler(self) -> tuple[OuraWebhookHandler, MagicMock]:
        data_247 = MagicMock()
        data_247._make_api_request.return_value = {"id": "obj-1", "day": "2026-06-30", "score": 81}
        return OuraWebhookHandler(data_247, MagicMock()), data_247

    def _notif(self, data_type: str) -> OuraWebhookNotification:
        return OuraWebhookNotification(
            event_type="create", data_type=data_type, user_id="oura-user-1", object_id="obj-1"
        )

    def test_daily_sleep_routes_to_score_path(self) -> None:
        handler, data_247 = self._handler()
        handler._dispatch_data_type(MagicMock(), self._notif("daily_sleep"), uuid4(), "trace-1")
        data_247.normalize_daily_sleep_scores.assert_called_once()
        data_247.save_daily_sleep_scores.assert_called_once()
        # must NOT be treated as a sleep session
        data_247.normalize_sleeps.assert_not_called()
        data_247.save_sleep_data.assert_not_called()

    def test_sleep_session_routes_to_session_path(self) -> None:
        handler, data_247 = self._handler()
        handler._dispatch_data_type(MagicMock(), self._notif("sleep"), uuid4(), "trace-1")
        data_247.save_sleep_data.assert_called_once()
        data_247.save_daily_sleep_scores.assert_not_called()


class TestOuraWebhookFanOut:
    """One ring, several OW profiles: every profile must receive the delivery.

    Oura names only the ring in its payload, and has no polling path to catch a
    profile up later. Resolving a single connection therefore left every other
    profile permanently silent while still reporting itself as connected.
    """

    def _handler(self, connections: list[SimpleNamespace]) -> tuple[OuraWebhookHandler, MagicMock, MagicMock]:
        data_247 = MagicMock()
        data_247._make_api_request.return_value = {"id": "obj-1", "day": "2026-06-30"}
        # Save paths return record counts, not mocks: the completion log reads
        # ``int(count)`` and the inserted/updated split off them.
        data_247.save_sleep_data.return_value = 1
        data_247.save_daily_sleep_scores.return_value = 1
        workouts = MagicMock()
        workouts.get_workout_detail_from_api.return_value = {"id": "obj-1"}
        workouts.save_from_raw.return_value = 1
        handler = OuraWebhookHandler(data_247, workouts)
        handler.connection_repo = MagicMock()
        handler.connection_repo.get_all_by_provider_user_id.return_value = connections
        return handler, data_247, workouts

    def _payload(self, data_type: str = "sleep") -> dict:
        return {
            "event_type": "create",
            "data_type": data_type,
            "user_id": "oura-user-1",
            "object_id": "obj-1",
        }

    @staticmethod
    def _saved_user_ids(save_mock: MagicMock) -> list[UUID]:
        return [call.args[1] for call in save_mock.call_args_list]

    def test_every_linked_profile_receives_the_delivery(self) -> None:
        primary, secondary = SimpleNamespace(user_id=uuid4()), SimpleNamespace(user_id=uuid4())
        handler, data_247, _ = self._handler([primary, secondary])

        result = handler.process_payload(MagicMock(), self._payload(), "trace-1")

        assert self._saved_user_ids(data_247.save_sleep_data) == [primary.user_id, secondary.user_id]
        assert result["user_id"] == str(primary.user_id)
        assert result["linked_user_ids"] == [str(secondary.user_id)]

    def test_object_is_fetched_once_for_all_profiles(self) -> None:
        """A second fetch would spend an API call re-reading the same object."""
        handler, data_247, _ = self._handler([SimpleNamespace(user_id=uuid4()) for _ in range(3)])

        handler.process_payload(MagicMock(), self._payload(), "trace-1")

        data_247._make_api_request.assert_called_once()

    def test_workout_fans_out_without_refetching(self) -> None:
        primary, secondary = SimpleNamespace(user_id=uuid4()), SimpleNamespace(user_id=uuid4())
        handler, _, workouts = self._handler([primary, secondary])

        handler.process_payload(MagicMock(), self._payload("workout"), "trace-1")

        workouts.get_workout_detail_from_api.assert_called_once()
        assert self._saved_user_ids(workouts.save_from_raw) == [primary.user_id, secondary.user_id]
        workouts.save_by_id.assert_not_called()

    def test_last_synced_at_moves_for_every_profile(self) -> None:
        """The starved profile's staleness is the only signal this bug leaves."""
        connections = [SimpleNamespace(user_id=uuid4()), SimpleNamespace(user_id=uuid4())]
        handler, _, _ = self._handler(connections)

        handler.process_payload(MagicMock(), self._payload(), "trace-1")

        synced = [call.args[1] for call in handler.connection_repo.update_last_synced_at.call_args_list]
        assert synced == connections

    def test_single_profile_reports_no_linked_ids(self) -> None:
        only = SimpleNamespace(user_id=uuid4())
        handler, data_247, _ = self._handler([only])

        result = handler.process_payload(MagicMock(), self._payload(), "trace-1")

        assert result["linked_user_ids"] == []
        assert self._saved_user_ids(data_247.save_sleep_data) == [only.user_id]

    def test_unknown_ring_is_still_reported(self) -> None:
        handler, data_247, _ = self._handler([])

        result = handler.process_payload(MagicMock(), self._payload(), "trace-1")

        assert result["status"] == "user_not_found"
        data_247._make_api_request.assert_not_called()

    def test_dead_token_falls_through_to_the_next_profile(self) -> None:
        """One expired token must not starve the profiles sharing the ring."""
        primary, secondary = SimpleNamespace(user_id=uuid4()), SimpleNamespace(user_id=uuid4())
        handler, data_247, _ = self._handler([primary, secondary])
        data_247._make_api_request.side_effect = [
            ProviderAuthRejectedError("oura"),
            {"id": "obj-1", "day": "2026-06-30"},
        ]

        result = handler.process_payload(MagicMock(), self._payload(), "trace-1")

        fetched_as = [call.args[1] for call in data_247._make_api_request.call_args_list]
        assert fetched_as == [primary.user_id, secondary.user_id]
        # The fetch borrowed the second token, but both profiles still get saved.
        assert self._saved_user_ids(data_247.save_sleep_data) == [primary.user_id, secondary.user_id]
        assert result["status"] == "processed"

    def test_all_tokens_dead_raises_for_retry(self) -> None:
        handler, data_247, _ = self._handler([SimpleNamespace(user_id=uuid4()) for _ in range(2)])
        data_247._make_api_request.side_effect = ProviderAuthRejectedError("oura")

        with pytest.raises(ProviderAuthRejectedError):
            handler.process_payload(MagicMock(), self._payload(), "trace-1")

        assert data_247._make_api_request.call_count == 2

    def test_missing_object_is_not_retried_against_other_tokens(self) -> None:
        """A 404 is the object's verdict, not the token's — another token repeats it."""
        handler, data_247, _ = self._handler([SimpleNamespace(user_id=uuid4()) for _ in range(2)])
        data_247._make_api_request.side_effect = ProviderError("oura", upstream_status=404)

        with pytest.raises(ProviderError):
            handler.process_payload(MagicMock(), self._payload(), "trace-1")

        data_247._make_api_request.assert_called_once()

    def test_empty_fetch_saves_nothing_anywhere(self) -> None:
        handler, data_247, _ = self._handler([SimpleNamespace(user_id=uuid4()) for _ in range(2)])
        data_247._make_api_request.return_value = None

        result = handler.process_payload(MagicMock(), self._payload(), "trace-1")

        data_247.save_sleep_data.assert_not_called()
        assert result["records_saved"] == 0


class TestOuraWebhookLastSyncedAt:
    """last_synced_at moves only after a completed fetch."""

    @pytest.fixture
    def handler(self) -> Any:
        return ProviderFactory().get_provider("oura").webhooks

    @staticmethod
    def _connection() -> Any:
        return UserConnectionFactory(
            provider="oura",
            provider_user_id=OURA_USER_ID,
            last_synced_at=datetime.now(timezone.utc) - timedelta(days=3),
        )

    @staticmethod
    def _payload(**overrides: Any) -> dict[str, Any]:
        return {"event_type": "create", "data_type": "daily_spo2", "user_id": OURA_USER_ID, **overrides}

    def test_missing_object_id_is_ignored_without_fetch(self, db: Session, handler: Any) -> None:
        connection = self._connection()
        before = connection.last_synced_at

        with patch.object(handler.data_247, "_make_api_request") as api:
            result = handler.process_payload(db, self._payload(), "trace-1")
        db.refresh(connection)

        api.assert_not_called()
        assert result["status"] == "ignored"
        assert result["reason"] == "missing_object_id"
        assert connection.last_synced_at == before

    def test_unhandled_data_type_keeps_last_synced_at(self, db: Session, handler: Any) -> None:
        connection = self._connection()
        before = connection.last_synced_at

        with (
            patch.object(handler.data_247, "_make_api_request", return_value={"id": "tag-1"}),
            patch("app.services.providers.oura.webhook_handler.store_raw_payload"),
        ):
            result = handler.process_payload(db, self._payload(data_type="tag", object_id="tag-1"), "trace-2")
        db.refresh(connection)

        assert result["status"] == "ignored"
        assert result["reason"] == "unhandled_data_type: tag"
        assert connection.last_synced_at == before

    def test_completed_fetch_updates_last_synced_at(self, db: Session, handler: Any) -> None:
        connection = self._connection()
        before = connection.last_synced_at

        with patch.object(handler.data_247, "_make_api_request", return_value={}) as api:
            result = handler.process_payload(db, self._payload(object_id="spo2-1"), "trace-3")
        db.refresh(connection)

        api.assert_called_once()
        assert result["status"] == "processed"
        assert result["records_saved"] == 0
        assert connection.last_synced_at > before
