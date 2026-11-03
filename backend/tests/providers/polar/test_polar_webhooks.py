"""Polar webhook URLs must reach the API with their query parameters intact."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy.orm import Session

from app.models import HealthScore
from app.services.providers.polar.strategy import PolarStrategy
from tests.factories import UserFactory


@pytest.mark.parametrize(
    ("event_type", "resource", "score_fields", "saved_key"),
    [
        (
            "SLEEP_WISE_ALERTNESS",
            "alertness",
            {"grade": 8.5, "grade_type": "GRADE_TYPE_PRIMARY"},
            "alertness",
        ),
        (
            "SLEEP_WISE_CIRCADIAN_BEDTIME",
            "circadian-bedtime",
            {"quality": "CIRCADIAN_BEDTIME_QUALITY_CLEARLY_RECOGNIZABLE"},
            "circadian_bedtime",
        ),
    ],
)
def test_sleepwise_webhook_fetches_range_and_saves_each_score(
    db: Session, event_type: str, resource: str, score_fields: dict[str, Any], saved_key: str
) -> None:
    user = UserFactory()
    handler = PolarStrategy().webhooks
    handler.connection_repo = MagicMock()
    handler.connection_repo.get_by_provider_user_id.return_value = SimpleNamespace(user_id=user.id)
    path = f"/v3/users/sleepwise/{resource}/date"
    requests: list[httpx.Request] = []
    response = [{**score_fields, "period_start_time": f"2026-11-{day:02}T07:00:00+00:00"} for day in (1, 2)]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "from" not in request.url.params:
            return httpx.Response(400, text="Query parameter from must be specified")
        return httpx.Response(200, json=response)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    with (
        patch("app.services.providers.api_client.get_valid_token", return_value="test-token"),
        patch("app.services.providers.api_client.httpx.Client", return_value=client),
    ):
        result = handler.process_payload(
            db,
            {
                "event": event_type,
                "user_id": 123,
                "url": f"https://www.polaraccesslink.com{path}?from=2026-11-01&to=2026-11-02",
            },
            "polar-sleepwise-regression",
        )

    assert len(requests) == 1
    assert requests[0].url.host == "www.polaraccesslink.com"
    assert requests[0].url.path == path
    assert dict(requests[0].url.params) == {"from": "2026-11-01", "to": "2026-11-02"}
    assert result == {"status": "accepted", "user_id": str(user.id), "saved": {saved_key: 2}}
    scores = db.query(HealthScore).filter_by(user_id=user.id).all()
    assert len(scores) == 2
    assert {score.recorded_at.day for score in scores} == {1, 2}
    assert all(score.provider == "polar" for score in scores)


@pytest.mark.parametrize(
    ("event_type", "path", "query", "expected_params"),
    [
        ("EXERCISE", "/v3/exercises/exercise-123", "", {}),
        ("SLEEP", "/v3/users/sleep/2026-11-01", "", {}),
        ("ACTIVITY_SUMMARY", "/v3/users/activities/2026-11-01", "", {"activity_zones": "true"}),
        (
            "ACTIVITY_SUMMARY",
            "/v3/users/activities/2026-11-01",
            "?from=2026-11-01&to=2026-11-02",
            {"from": "2026-11-01", "to": "2026-11-02", "activity_zones": "true"},
        ),
    ],
)
def test_webhook_request_preserves_existing_entity_paths_and_activity_options(
    db: Session, event_type: str, path: str, query: str, expected_params: dict[str, str]
) -> None:
    user = UserFactory()
    handler = PolarStrategy().webhooks
    handler.connection_repo = MagicMock()
    handler.connection_repo.get_by_provider_user_id.return_value = SimpleNamespace(user_id=user.id)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    with (
        patch("app.services.providers.api_client.get_valid_token", return_value="test-token"),
        patch("app.services.providers.api_client.httpx.Client", return_value=client),
    ):
        result = handler.process_payload(
            db,
            {"event": event_type, "user_id": 123, "url": f"https://www.polaraccesslink.com{path}{query}"},
            "polar-entity-regression",
        )

    assert result["status"] == "accepted"
    assert len(requests) == 1
    assert requests[0].url.path == path
    assert dict(requests[0].url.params) == expected_params
