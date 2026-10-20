"""Polar's generated incoming UUID must never become the score FK after canonical reconciliation."""

from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, HealthScore, User
from app.schemas.providers.polar import PolarWebhookEventType
from app.services.event_record_service import event_record_service
from app.services.health_score_service import health_score_service
from app.services.providers.polar.data_247 import Polar247Data
from app.services.providers.polar.strategy import PolarStrategy
from tests.factories import UserFactory

_START = datetime(2026, 9, 30, 1, tzinfo=timezone.utc)


def _raw(start_hour: int, end_hour: int) -> dict[str, Any]:
    return {
        "polar_user": "https://www.polaraccesslink.com/v3/users/475",
        "date": "2026-09-30",
        "sleep_start_time": f"2026-09-30T{start_hour:02}:00:00+00:00",
        "sleep_end_time": f"2026-09-30T{end_hour:02}:00:00+00:00",
        "light_sleep": (end_hour - start_hour) * 3600,
        "deep_sleep": 0,
        "rem_sleep": 0,
        "total_interruption_duration": 0,
        "sleep_score": 82,
    }


@pytest.fixture(autouse=True)
def _isolate_internal_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(event_record_service, "_recompute_sleep_scores", MagicMock())


@pytest.mark.parametrize("entrypoint", ["webhook", "range"])
def test_provider_score_uses_surviving_record_in_both_persistence_paths(db: Session, entrypoint: str) -> None:
    user = cast(User, UserFactory())
    data = PolarStrategy().data_247
    assert isinstance(data, Polar247Data)

    def save(raw: dict[str, Any]) -> None:
        if entrypoint == "webhook":
            with patch.object(data, "_make_api_request", return_value=raw):
                result = data.fetch_and_save_from_webhook(
                    db, user.id, PolarWebhookEventType.SLEEP, "/v3/users/sleep/2026-09-30"
                )
            assert result == {"sleep": 1}
        else:
            with patch.object(data, "get_sleep_data", return_value=[raw]):
                count = data._save_sleep(db, user.id, _START, _START)
            assert count == 1

    save(_raw(1, 3))
    original_id = db.query(EventRecord).join(DataSource).filter(DataSource.user_id == user.id).one().id
    save(_raw(2, 4))
    save(_raw(2, 4))

    records = db.query(EventRecord).join(DataSource).filter(DataSource.user_id == user.id).all()
    scores = db.query(HealthScore).filter(HealthScore.user_id == user.id, HealthScore.provider == "polar").all()
    assert len(records) == 1
    assert records[0].id == original_id
    assert len(scores) == 1
    assert scores[0].event_record_id == original_id
    assert scores[0].data_source_id == records[0].data_source_id


def test_sleep_and_provider_score_commit_together(db: Session) -> None:
    user = cast(User, UserFactory())
    data = PolarStrategy().data_247
    assert isinstance(data, Polar247Data)
    record, detail, score, _samples = data.normalize_sleep([_raw(1, 3)], user.id)[0]
    commits: list[int] = []
    original_commit = db.commit

    def commit() -> None:
        # A sleep-only intermediate commit would see zero provider scores here.
        commits.append(db.query(HealthScore).filter(HealthScore.user_id == user.id).count())
        original_commit()

    with patch.object(db, "commit", side_effect=commit):
        data._save_sleep_record(db, user.id, record, detail, score)

    assert commits == [1]


def test_score_failure_rolls_back_sleep_and_propagates_original_error(db: Session) -> None:
    user = cast(User, UserFactory())
    user_id = user.id
    data = PolarStrategy().data_247
    assert isinstance(data, Polar247Data)
    record, detail, score, _samples = data.normalize_sleep([_raw(1, 3)], user_id)[0]

    with (
        patch.object(health_score_service, "bulk_create", side_effect=RuntimeError("provider score failed")),
        pytest.raises(RuntimeError, match="provider score failed"),
    ):
        data._save_sleep_record(db, user_id, record, detail, score)

    assert db.query(EventRecord).filter(EventRecord.id == record.id).count() == 0
    assert db.query(HealthScore).filter(HealthScore.event_record_id == record.id).count() == 0
