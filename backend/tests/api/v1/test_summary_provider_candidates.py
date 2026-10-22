"""Provider candidates travel through real SQL and HTTP before client-side selection."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import DataPointSeriesArchive
from app.models.archival_setting import ArchivalSetting
from app.schemas.enums import AggregationMethod, ProviderName
from app.services.priority_service import priority_service
from app.utils.pagination import decode_activity_cursor
from tests.factories import (
    ApiKeyFactory,
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    SeriesTypeDefinitionFactory,
    SleepDetailsFactory,
    UserFactory,
    WorkoutDetailsFactory,
)
from tests.utils import api_key_headers

START = datetime(2026, 10, 7, tzinfo=timezone.utc)
END = START + timedelta(days=2)


def _params(**extra: Any) -> dict[str, Any]:
    return {"start_date": START.isoformat(), "end_date": END.isoformat(), **extra}


def _sleep(source: Any, start: datetime, minutes: int, *, nap: bool = False) -> Any:
    event = EventRecordFactory(
        mapping=source,
        category="sleep",
        type="sleep",
        start_datetime=start,
        end_datetime=start + timedelta(minutes=minutes),
        duration_seconds=minutes * 60,
    )
    SleepDetailsFactory(
        event_record=event,
        sleep_total_duration_minutes=minutes,
        is_nap=nap,
        sleep_deep_minutes=minutes,
        sleep_light_minutes=0,
        sleep_rem_minutes=0,
        sleep_awake_minutes=0,
        sleep_stages=[{"stage": "deep", "start_time": start.isoformat(), "end_time": event.end_datetime.isoformat()}],
    )
    return event


@pytest.mark.parametrize("preferred", [ProviderName.APPLE, ProviderName.OURA])
def test_sleep_candidates_keep_each_provider_and_exact_session_identity(
    client: TestClient,
    db: Session,
    preferred: ProviderName,
) -> None:
    user = UserFactory()
    apple = DataSourceFactory(user=user, provider=ProviderName.APPLE, device_model="Watch", device_type="watch")
    phone = DataSourceFactory(user=user, provider=ProviderName.APPLE, device_model="Phone", device_type="phone")
    oura = DataSourceFactory(user=user, provider=ProviderName.OURA, source="oura", device_model="Ring")
    _sleep(apple, START + timedelta(hours=1), 360)
    _sleep(phone, START + timedelta(hours=1), 400)
    _sleep(oura, START + timedelta(hours=1), 420)
    _sleep(oura, START + timedelta(hours=10), 20, nap=True)
    for source, hr in [(apple, 90), (oura, 48)]:
        DataPointSeriesFactory(data_source=source, value=hr, recorded_at=START + timedelta(hours=3))
    priority_service.update_provider_priority(db, ProviderName.APPLE, 1 if preferred == ProviderName.APPLE else 2)
    priority_service.update_provider_priority(db, ProviderName.OURA, 1 if preferred == ProviderName.OURA else 2)
    headers = api_key_headers(ApiKeyFactory().plain_key)
    url = f"/api/v1/users/{user.id}/summaries/sleep"
    legacy = client.get(url, headers=headers, params=_params())
    assert legacy.status_code == 200
    assert [s["source"]["provider"] for s in legacy.json()["data"]] == [preferred.value]

    candidates = []
    cursors = []
    params = _params(source_selection="per_provider", limit=1)
    while True:
        response = client.get(url, headers=headers, params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        candidates.extend(page["data"])
        cursors.append(page["pagination"])
        if not page["pagination"]["next_cursor"]:
            break
        params["cursor"] = page["pagination"]["next_cursor"]
    assert len(candidates) == 2
    by_provider = {s["source"]["provider"]: s for s in candidates}
    assert by_provider["apple"]["duration_minutes"] == 360
    assert by_provider["oura"]["duration_minutes"] == 420
    assert by_provider["oura"]["nap_duration_minutes"] == 20
    assert by_provider["oura"]["avg_heart_rate_bpm"] == 48
    assert by_provider["apple"]["avg_heart_rate_bpm"] == 90
    assert by_provider["apple"]["source"]["data_source_id"] == str(apple.id)
    for source in (apple, oura):
        sessions = client.get(
            f"/api/v1/users/{user.id}/events/sleep",
            headers=headers,
            params=_params(data_source_id=str(source.id), include="stages"),
        )
        assert sessions.status_code == 200, sessions.text
        assert {s["source"]["data_source_id"] for s in sessions.json()["data"]} == {str(source.id)}
        assert all(s["sleep_stage_intervals"] for s in sessions.json()["data"])
    back = client.get(
        url,
        headers=headers,
        params=_params(
            source_selection="per_provider",
            limit=1,
            cursor=cursors[1]["previous_cursor"],
        ),
    )
    assert back.status_code == 200
    assert back.json()["data"] == candidates[:1]


@pytest.mark.parametrize("sort_order", ["asc", "desc"])
def test_activity_provider_candidates_paginate_same_day_without_mixing(
    client: TestClient,
    db: Session,
    sort_order: str,
) -> None:
    user = UserFactory()
    sources = {}
    steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
    for provider, steps, hr in [(ProviderName.OURA, 2128, 150), (ProviderName.APPLE, 3600, 90)]:
        source = DataSourceFactory(user=user, provider=provider, source="same-writer", device_model="same-model")
        sources[provider.value] = source
        DataPointSeriesFactory(
            data_source=source,
            series_type=steps_type,
            value=steps,
            recorded_at=START + timedelta(hours=12),
            is_daily_total=True,
        )
        DataPointSeriesFactory(
            data_source=source,
            series_type=steps_type,
            value=35 if provider == ProviderName.OURA else 5,
            recorded_at=START + timedelta(hours=10),
        )
        DataPointSeriesFactory(data_source=source, value=hr, recorded_at=START + timedelta(hours=10))
    DataPointSeriesFactory(
        data_source=sources["oura"],
        series_type=SeriesTypeDefinitionFactory.get_or_create_energy(),
        value=500,
        recorded_at=START + timedelta(hours=12),
        is_daily_total=True,
    )
    priority_service.update_provider_priority(db, ProviderName.OURA, 1)
    priority_service.update_provider_priority(db, ProviderName.APPLE, 2)
    headers = api_key_headers(ApiKeyFactory().plain_key)
    url = f"/api/v1/users/{user.id}/summaries/activity"
    legacy = client.get(url, headers=headers, params=_params())
    assert legacy.status_code == 200
    assert len(legacy.json()["data"]) == 1
    assert legacy.json()["data"][0]["steps"] == 2128
    candidates = []
    pages = []
    params = _params(source_selection="per_provider", limit=1, sort_order=sort_order)
    while True:
        response = client.get(url, headers=headers, params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        candidates.extend(page["data"])
        pages.append(page)
        if not page["pagination"]["next_cursor"]:
            break
        params["cursor"] = page["pagination"]["next_cursor"]
    assert len(candidates) == 2
    by_provider = {s["source"]["provider"]: s for s in candidates}
    assert by_provider["apple"]["steps"] == 3600
    assert by_provider["oura"]["steps"] == 2128
    assert by_provider["apple"]["active_calories_kcal"] is None
    assert by_provider["oura"]["active_calories_kcal"] == 500
    assert by_provider["apple"]["active_minutes"] == 0
    assert by_provider["oura"]["active_minutes"] == 1
    assert by_provider["apple"]["heart_rate"]["avg_bpm"] == 90
    assert by_provider["oura"]["heart_rate"]["avg_bpm"] == 150
    back = client.get(
        url,
        headers=headers,
        params=_params(
            source_selection="per_provider",
            limit=1,
            sort_order=sort_order,
            cursor=pages[1]["pagination"]["previous_cursor"],
        ),
    )
    assert back.status_code == 200
    assert back.json()["data"] == candidates[:1]


def test_workout_only_provider_is_not_hidden_by_another_providers_steps(client: TestClient, db: Session) -> None:
    user = UserFactory()
    apple = DataSourceFactory(user=user, provider=ProviderName.APPLE)
    whoop = DataSourceFactory(user=user, provider=ProviderName.WHOOP, source="whoop")
    DataPointSeriesFactory(
        data_source=apple,
        series_type=SeriesTypeDefinitionFactory.get_or_create_steps(),
        value=3600,
        recorded_at=START + timedelta(hours=12),
        is_daily_total=True,
    )
    record = EventRecordFactory(
        mapping=whoop,
        category="workout",
        type="running",
        start_datetime=START + timedelta(hours=10),
        end_datetime=START + timedelta(hours=11),
        duration_seconds=3600,
    )
    WorkoutDetailsFactory(event_record=record, total_elevation_gain=100)
    response = client.get(
        f"/api/v1/users/{user.id}/summaries/activity",
        headers=api_key_headers(ApiKeyFactory().plain_key),
        params=_params(source_selection="per_provider"),
    )
    assert response.status_code == 200, response.text
    by_provider = {s["source"]["provider"]: s for s in response.json()["data"]}
    assert set(by_provider) == {"apple", "whoop"}
    assert by_provider["whoop"]["elevation_meters"] == 100
    assert by_provider["whoop"]["steps"] is None


@pytest.mark.parametrize("kind", ["sleep", "activity"])
def test_unknown_source_selection_is_rejected(client: TestClient, db: Session, kind: str) -> None:
    user = UserFactory()
    response = client.get(
        f"/api/v1/users/{user.id}/summaries/{kind}",
        headers=api_key_headers(ApiKeyFactory().plain_key),
        params=_params(source_selection="unrecognised"),
    )
    assert response.status_code == 400


def test_candidates_preserve_sibling_device_merge_and_archived_provider(client: TestClient, db: Session) -> None:
    user = UserFactory()
    watch = DataSourceFactory(user=user, provider=ProviderName.APPLE, device_model="Watch", device_type="watch")
    phone = DataSourceFactory(user=user, provider=ProviderName.APPLE, device_model="Phone", device_type="phone")
    oura = DataSourceFactory(user=user, provider=ProviderName.OURA, source="apple_health_sdk", device_model="Watch")
    steps_type = SeriesTypeDefinitionFactory.get_or_create_steps()
    # The shared hour contributes the larger device count, then the phone's later hour is added.
    for source, hour, steps in [(watch, 10, 100), (phone, 10, 150), (phone, 11, 200)]:
        DataPointSeriesFactory(
            data_source=source, series_type=steps_type, value=steps, recorded_at=START + timedelta(hours=hour)
        )
    db.add(ArchivalSetting(id=1, archive_after_days=30, delete_after_days=None))
    db.add(
        DataPointSeriesArchive(
            id=uuid4(),
            data_source_id=oura.id,
            series_type_definition_id=steps_type.id,
            bucket_start_at=START,
            aggregation_type=AggregationMethod.SUM,
            value=Decimal("2128"),
            sample_count=1,
        )
    )
    db.flush()
    headers = api_key_headers(ApiKeyFactory().plain_key)
    url = f"/api/v1/users/{user.id}/summaries/activity"
    response = client.get(url, headers=headers, params=_params(source_selection="per_provider"))
    assert response.status_code == 200, response.text
    by_provider = {s["source"]["provider"]: s for s in response.json()["data"]}
    assert by_provider["apple"]["steps"] == 350
    assert by_provider["oura"]["steps"] == 2128
    assert by_provider["apple"]["source"]["device"] is None
    # The existing total keeps the same global-priority semantics as the default daily response.
    legacy = client.get(url, headers=headers, params=_params())
    totals = client.get(url + "/totals", headers=headers, params=_params())
    assert totals.status_code == 200
    assert totals.json()["steps"] == sum(s["steps"] or 0 for s in legacy.json()["data"])


def test_default_null_source_cursor_keeps_legacy_identity(client: TestClient, db: Session) -> None:
    user = UserFactory()
    source = DataSourceFactory(user=user, provider=ProviderName.APPLE, source=None, device_model="Watch")
    for offset in (0, 1):
        DataPointSeriesFactory(
            data_source=source,
            series_type=SeriesTypeDefinitionFactory.get_or_create_steps(),
            value=100,
            recorded_at=START + timedelta(days=offset, hours=12),
            is_daily_total=True,
        )
    headers = api_key_headers(ApiKeyFactory().plain_key)
    url = f"/api/v1/users/{user.id}/summaries/activity"
    first = client.get(url, headers=headers, params=_params(limit=1))
    assert first.status_code == 200
    cursor = first.json()["pagination"]["next_cursor"]
    assert decode_activity_cursor(cursor) == (START.date(), "unknown", "Watch", "next")
    second = client.get(url, headers=headers, params=_params(limit=1, cursor=cursor))
    assert second.status_code == 200
    assert [s["date"] for s in second.json()["data"]] == [(START + timedelta(days=1)).date().isoformat()]


def test_sleep_candidate_keeps_one_same_model_device_and_its_own_sessions(client: TestClient, db: Session) -> None:
    user = UserFactory()
    first = DataSourceFactory(
        user=user,
        provider=ProviderName.APPLE,
        source="same-writer",
        device_model="Watch",
        device_type="watch",
        device_id="watch-one",
    )
    second = DataSourceFactory(
        user=user,
        provider=ProviderName.APPLE,
        source="same-writer",
        device_model="Watch",
        device_type="watch",
        device_id="watch-two",
    )
    _sleep(first, START + timedelta(hours=1), 360)
    _sleep(second, START + timedelta(hours=2), 400)
    headers = api_key_headers(ApiKeyFactory().plain_key)
    url = f"/api/v1/users/{user.id}/summaries/sleep"
    legacy = client.get(url, headers=headers, params=_params())
    assert legacy.status_code == 200
    assert legacy.json()["data"][0]["duration_minutes"] == 760
    assert legacy.json()["data"][0]["source"]["data_source_id"] is None
    candidate_response = client.get(url, headers=headers, params=_params(source_selection="per_provider"))
    assert candidate_response.status_code == 200, candidate_response.text
    assert len(candidate_response.json()["data"]) == 1
    candidate = candidate_response.json()["data"][0]
    chosen = candidate["source"]["data_source_id"]
    expected = {
        str(first.id): (360, (START + timedelta(hours=1)).isoformat()),
        str(second.id): (400, (START + timedelta(hours=2)).isoformat()),
    }[chosen]
    assert candidate["duration_minutes"] == expected[0]
    assert len(candidate["sessions"]) == 1
    assert datetime.fromisoformat(candidate["start_time"]) == datetime.fromisoformat(expected[1])
    assert candidate["sessions"][0]["duration_minutes"] == expected[0]
