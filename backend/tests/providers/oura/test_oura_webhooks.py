"""Tests for Oura webhook schemas and service."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.providers.oura import OuraWebhookNotification
from app.services.providers.oura.webhook_handler import OuraWebhookHandler


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
