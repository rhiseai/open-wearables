"""Oura rescores reach persisted daily scores and committed sleep notifications."""

from collections.abc import Generator
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import EventRecord, HealthScore, User
from app.schemas.enums import HealthScoreCategory, ProviderName
from app.services.event_record_service import event_record_service
from app.services.outgoing_webhooks import svix as svix_service
from app.services.outgoing_webhooks.events import on_sleep_updated
from app.services.providers.oura.strategy import OuraStrategy
from tests.factories import (
    DataSourceFactory,
    EventRecordFactory,
    SleepDetailsFactory,
    UserConnectionFactory,
    UserFactory,
)


@pytest.fixture
def strategy() -> OuraStrategy:
    return OuraStrategy()


@pytest.fixture(autouse=True)
def enable_webhooks() -> Generator[None, None, None]:
    with patch.object(svix_service, "is_enabled", return_value=True):
        yield


def _daily(score: int | None, **overrides: Any) -> dict[str, Any]:
    return {
        "id": "daily-oura-1",
        "day": "2026-10-22",
        "score": score,
        "contributors": {"deep_sleep": score},
        **overrides,
    }


def _sleep(
    user: User,
    *,
    provider: ProviderName = ProviderName.OURA,
    end: datetime = datetime(2026, 10, 22, 7, tzinfo=timezone.utc),
    zone_offset: str = "+00:00",
    is_nap: bool = False,
    minutes: int = 420,
) -> EventRecord:
    source = DataSourceFactory(user=user, provider=provider, source="oura", device_type="ring")
    record = EventRecordFactory(
        data_source=source,
        category="sleep",
        type="sleep_session",
        start_datetime=end - timedelta(minutes=minutes),
        end_datetime=end,
        duration_seconds=minutes * 60,
        zone_offset=zone_offset,
    )
    SleepDetailsFactory(event_record=record, is_nap=is_nap, sleep_total_duration_minutes=minutes)
    return record


def _sleep_calls(dispatch: MagicMock) -> list[Any]:
    return [call for call in dispatch.send_task.call_args_list if call.kwargs["args"][0] == "sleep.updated"]


@pytest.mark.parametrize(
    ("data_type", "category"),
    [
        ("daily_sleep", HealthScoreCategory.SLEEP),
        ("daily_readiness", HealthScoreCategory.READINESS),
        ("daily_activity", HealthScoreCategory.ACTIVITY),
    ],
)
def test_webhook_rescores_same_row_and_preserves_counts(
    db: Session, strategy: OuraStrategy, data_type: str, category: HealthScoreCategory
) -> None:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="oura", provider_user_id="ring-1")
    payload = {"event_type": "create", "data_type": data_type, "user_id": "ring-1", "object_id": "daily-oura-1"}
    with patch.object(
        strategy.data_247, "_make_api_request", side_effect=[_daily(61), _daily(75), _daily(75)]
    ) as fetch:
        created = strategy.webhooks.process_payload(db, payload, "create")
        row_id = db.query(HealthScore).filter_by(user_id=user.id, category=category).one().id
        payload["event_type"] = "update"
        updated = strategy.webhooks.process_payload(db, payload, "update")
        replayed = strategy.webhooks.process_payload(db, payload, "replay")

    rows = db.query(HealthScore).filter_by(user_id=user.id, category=category).all()
    assert len(rows) == 1
    assert rows[0].id == row_id
    assert rows[0].value == 75
    assert rows[0].components == {"deep_sleep": {"value": 75, "qualifier": None}}
    assert (created["records_saved"].inserted, created["records_saved"].updated) == (1, 0)
    assert (updated["records_saved"].inserted, updated["records_saved"].updated) == (0, 1)
    assert replayed["records_saved"] == 0
    assert fetch.call_count == 3  # Even a delayed/replayed notification reads the current object.


def test_sleep_score_changes_notify_even_when_score_returns_to_original(
    db: Session, strategy: OuraStrategy, mock_webhook_dispatch: MagicMock
) -> None:
    user = UserFactory()
    record = _sleep(user)
    for value in (61, 75, 61, 61):
        normalized = strategy.data_247.normalize_daily_sleep_scores([_daily(value)], user.id)
        strategy.data_247.save_daily_sleep_scores(db, user.id, normalized)

    calls = _sleep_calls(mock_webhook_dispatch)
    assert len(calls) == 3  # Initial score arrival, two actual changes, no identical replay.
    assert len({call.kwargs["kwargs"]["idempotency_key"] for call in calls}) == 3
    payloads = [call.kwargs["args"][1] for call in calls]
    assert payloads[0] == payloads[1] == payloads[2]  # Public sleep payload remains unchanged.
    assert payloads[0]["data"]["id"] == str(record.id)
    assert payloads[0]["data"]["source"]["provider"] == "oura"
    assert "revision_token" not in payloads[0]["data"]


def test_sleep_score_matches_provider_main_session_and_local_wake_day(
    db: Session, strategy: OuraStrategy, mock_webhook_dispatch: MagicMock
) -> None:
    user = UserFactory()
    expected = _sleep(user, end=datetime(2026, 10, 21, 23, tzinfo=timezone.utc), zone_offset="+03:00")
    _sleep(user, minutes=60)  # A shorter split part should not produce another day refresh.
    _sleep(user, provider=ProviderName.APPLE, minutes=600)
    _sleep(user, is_nap=True, minutes=700)
    _sleep(user, end=datetime(2026, 10, 23, 7, tzinfo=timezone.utc), minutes=800)
    _sleep(UserFactory(), minutes=900)
    normalized = strategy.data_247.normalize_daily_sleep_scores(
        [_daily(75, timestamp="2026-10-22T00:00:00+03:00")], user.id
    )
    db.execute(text("SET LOCAL TIME ZONE 'America/Los_Angeles'"))

    strategy.data_247.save_daily_sleep_scores(db, user.id, normalized)

    calls = _sleep_calls(mock_webhook_dispatch)
    assert len(calls) == 1
    assert calls[0].kwargs["args"][1]["data"]["id"] == str(expected.id)


def test_score_before_sleep_is_saved_without_synthetic_sleep_event(
    db: Session, strategy: OuraStrategy, mock_webhook_dispatch: MagicMock
) -> None:
    user = UserFactory()
    normalized = strategy.data_247.normalize_daily_sleep_scores([_daily(75)], user.id)

    counts = strategy.data_247.save_daily_sleep_scores(db, user.id, normalized)

    assert counts.inserted == 1
    assert db.query(HealthScore).filter_by(user_id=user.id).one().value == 75
    assert db.query(EventRecord).count() == 0
    assert _sleep_calls(mock_webhook_dispatch) == []


def test_null_score_does_not_erase_stored_score_or_notify(
    db: Session, strategy: OuraStrategy, mock_webhook_dispatch: MagicMock
) -> None:
    user = UserFactory()
    _sleep(user)
    strategy.data_247.save_daily_sleep_scores(
        db, user.id, strategy.data_247.normalize_daily_sleep_scores([_daily(75)], user.id)
    )
    mock_webhook_dispatch.reset_mock()

    counts = strategy.data_247.save_daily_sleep_scores(
        db, user.id, strategy.data_247.normalize_daily_sleep_scores([_daily(None)], user.id)
    )

    assert counts == 0
    assert db.query(HealthScore).filter_by(user_id=user.id).one().value == 75
    assert _sleep_calls(mock_webhook_dispatch) == []


def test_notification_waits_for_outer_commit_and_rollback_discards_it(
    db: Session, mock_webhook_dispatch: MagicMock
) -> None:
    user = UserFactory()
    _sleep(user)
    user_id = user.id
    db.commit()

    event_record_service.queue_sleep_score_updates(db, user_id, ProviderName.OURA, {date(2026, 10, 22)})
    assert _sleep_calls(mock_webhook_dispatch) == []
    db.rollback()
    db.commit()
    assert _sleep_calls(mock_webhook_dispatch) == []

    event_record_service.queue_sleep_score_updates(db, user_id, ProviderName.OURA, {date(2026, 10, 22)})
    assert _sleep_calls(mock_webhook_dispatch) == []
    db.commit()
    assert len(_sleep_calls(mock_webhook_dispatch)) == 1
    db.commit()
    assert len(_sleep_calls(mock_webhook_dispatch)) == 1


def test_failed_score_commit_does_not_emit_later(
    db: Session, strategy: OuraStrategy, mock_webhook_dispatch: MagicMock
) -> None:
    user = UserFactory()
    _sleep(user)
    user_id = user.id
    db.commit()
    normalized = strategy.data_247.normalize_daily_sleep_scores([_daily(75)], user_id)
    with patch.object(db, "commit", side_effect=RuntimeError("commit failed")), pytest.raises(RuntimeError):
        strategy.data_247.save_daily_sleep_scores(db, user_id, normalized)
    db.rollback()
    db.commit()

    assert db.query(HealthScore).filter_by(user_id=user_id).count() == 0
    assert _sleep_calls(mock_webhook_dispatch) == []


def test_same_queued_revision_keeps_transport_idempotency(mock_webhook_dispatch: MagicMock) -> None:
    user = UserFactory()
    record = _sleep(user)
    values = {
        "record_id": record.id,
        "user_id": user.id,
        "provider": "oura",
        "device": None,
        "start_time": record.start_datetime.isoformat(),
        "end_time": record.end_datetime.isoformat(),
        "zone_offset": "+00:00",
        "duration_seconds": record.duration_seconds,
        "revision_token": "same-committed-write",
    }
    on_sleep_updated(**values)
    on_sleep_updated(**values)

    first, second = _sleep_calls(mock_webhook_dispatch)
    assert first.kwargs == second.kwargs


def test_historical_loader_replaces_scores_and_notifies_existing_sleep(
    db: Session, strategy: OuraStrategy, mock_webhook_dispatch: MagicMock
) -> None:
    user = UserFactory()
    _sleep(user)
    strategy.data_247.save_daily_sleep_scores(
        db, user.id, strategy.data_247.normalize_daily_sleep_scores([_daily(61)], user.id)
    )
    mock_webhook_dispatch.reset_mock()

    def response(_db: Session, _user_id: Any, endpoint: str, **_kwargs: Any) -> dict[str, Any]:
        if endpoint.endswith("daily_sleep"):
            return {"data": [_daily(75)]}
        if endpoint.endswith(("daily_readiness", "daily_activity")):
            return {"data": [_daily(80)]}
        return {"data": []}

    with patch.object(strategy.data_247, "_make_api_request", side_effect=response):
        result = strategy.data_247.load_and_save_all(
            db,
            user.id,
            datetime(2026, 10, 22, tzinfo=timezone.utc),
            datetime(2026, 10, 23, tzinfo=timezone.utc),
        )

    assert result.failures == {}
    assert result.outcomes["sleep_score"].counts.updated == 1
    assert db.query(HealthScore).filter_by(user_id=user.id, category=HealthScoreCategory.SLEEP).one().value == 75
    assert len(_sleep_calls(mock_webhook_dispatch)) == 1
