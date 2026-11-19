"""Historical Oura activity and heart rate commit in bounded batches."""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.models import DataPointSeries
from app.repositories.data_point_series_repository import WriteCounts
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from app.schemas.sync_status import SyncRunContext, SyncScope, SyncSource, SyncStatus
from app.services.providers.oura.data_247 import Oura247Data
from app.services.providers.oura.strategy import OuraStrategy
from app.services.timeseries_service import timeseries_service
from app.utils.context import sync_run_context
from app.utils.dates import to_rfc3339
from tests.factories import UserFactory

_ACTIVITY = "/v2/usercollection/daily_activity"
_HEART_RATE = "/v2/usercollection/heartrate"


@pytest.fixture
def data_247() -> Oura247Data:
    data = OuraStrategy().data_247
    assert isinstance(data, Oura247Data)
    return data


@pytest.fixture(autouse=True)
def _disable_raw_payload_storage() -> Iterator[None]:
    with patch("app.services.providers.oura.data_247.store_raw_payload"):
        yield


def _activity(at: datetime, *, points: int = 1) -> dict[str, Any]:
    return {
        "id": at.date().isoformat(),
        "day": at.date().isoformat(),
        "met": {"timestamp": at.isoformat(), "interval": 60, "items": [1.5] * points},
        "class_5_min": "2" * 288,
    }


def _heart_rate(at: datetime) -> dict[str, Any]:
    return {"timestamp": at.isoformat(), "bpm": 65}


def _row_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(DataPointSeries)) or 0


def test_historical_commits_bounded_batches_before_fetching_the_next_page(db: Session, data_247: Oura247Data) -> None:
    user_id = UserFactory().id
    start = datetime(2026, 6, 1, 8, tzinfo=timezone(timedelta(hours=2)))
    window_end = start + timedelta(days=30)
    end = start + timedelta(days=31, hours=3)
    pending: list[int] = []
    committed: list[int] = []
    activity_requests: list[dict[str, Any]] = []
    heart_rate_requests: list[dict[str, Any]] = []
    write = timeseries_service.bulk_create_samples

    def track_write(session: Session, samples: list[TimeSeriesSampleCreate]) -> WriteCounts:
        counts = write(session, samples)
        pending.append(len(samples))
        return counts

    def track_commit(_session: Session) -> None:
        committed.extend(pending)
        pending.clear()

    def fetch(_db: Session, _user_id: UUID, endpoint: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if endpoint == _ACTIVITY:
            assert params is not None
            activity_requests.append(params)
            if "next_token" not in params:
                return {
                    "data": [_activity(start + timedelta(days=day), points=1440) for day in range(8)],
                    "next_token": "activity-page-2",
                }
            assert committed == [10_080, 1440]
            return {"data": [_activity(start + timedelta(days=8))]}
        if endpoint == _HEART_RATE:
            assert params is not None
            heart_rate_requests.append(params)
            if "next_token" in params:
                assert committed == [10_080, 1440, 1, 10_000, 1]
                return {"data": [_heart_rate(start + timedelta(days=8))]}
            if len(heart_rate_requests) == 1:
                return {
                    "data": [_heart_rate(start + timedelta(minutes=minute)) for minute in range(10_001)],
                    "next_token": "heart-rate-page-2",
                }
            assert committed == [10_080, 1440, 1, 10_000, 1, 1]
            return {"data": [_heart_rate(end - timedelta(minutes=1))]}
        return {"data": []}

    event.listen(db, "after_commit", track_commit)
    try:
        with (
            patch.object(data_247, "_make_api_request", side_effect=fetch),
            patch.object(timeseries_service, "bulk_create_samples", side_effect=track_write),
            sync_run_context(SyncRunContext(run_id="testing", source=SyncSource.BACKFILL, scope=SyncScope.HISTORICAL)),
        ):
            result = data_247.load_and_save_all(db, user_id, start, end)
    finally:
        event.remove(db, "after_commit", track_commit)

    activity_range = {"start_date": "2026-06-01", "end_date": "2026-07-02T23:59:59"}
    first_heart_rate_range = {"start_datetime": to_rfc3339(start), "end_datetime": to_rfc3339(window_end)}
    assert activity_requests == [activity_range, {**activity_range, "next_token": "activity-page-2"}]
    assert heart_rate_requests == [
        first_heart_rate_range,
        {**first_heart_rate_range, "next_token": "heart-rate-page-2"},
        {"start_datetime": to_rfc3339(window_end), "end_datetime": to_rfc3339(end)},
    ]
    assert result.failures == {}
    assert result.outcomes["activity"].rows_written == 11_521
    assert result.outcomes["heart_rate"].rows_written == 10_003
    assert committed == [10_080, 1440, 1, 10_000, 1, 1, 1]
    assert _row_count(db) == result.inserted == 21_524


@pytest.mark.parametrize(
    ("data_type", "failure"),
    [("activity", "page"), ("heart_rate", "window"), ("activity", "write"), ("heart_rate", "write")],
)
def test_historical_failure_preserves_committed_progress_and_allows_replay(
    db: Session, data_247: Oura247Data, data_type: str, failure: str
) -> None:
    user_id = UserFactory().id
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=31)
    target = _ACTIVITY if data_type == "activity" else _HEART_RATE
    record = _activity if data_type == "activity" else _heart_rate
    fail = True
    writes = 0
    requests = 0
    write = timeseries_service.bulk_create_samples

    def fetch(_db: Session, _user_id: UUID, endpoint: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal requests
        if endpoint != target:
            return {"data": []}
        requests += 1
        if requests == 1:
            page = {"data": [record(start)]}
            if failure != "window":
                page["next_token"] = "next-page"
            return page
        if requests == 2:
            if fail and failure != "write":
                raise HTTPException(503, "Provider temporarily unavailable")
            point_time = start + timedelta(days=30) if failure == "window" else start + timedelta(days=1)
            return {"data": [record(point_time)]}
        return {"data": []}

    def fail_next_write(session: Session, samples: list[TimeSeriesSampleCreate]) -> WriteCounts:
        nonlocal writes
        writes += 1
        counts = write(session, samples)
        if fail and failure == "write" and writes == 2:
            raise RuntimeError("Rollback the second batch before its commit")
        return counts

    with (
        patch.object(data_247, "_make_api_request", side_effect=fetch),
        patch.object(timeseries_service, "bulk_create_samples", side_effect=fail_next_write),
        sync_run_context(SyncRunContext(run_id="testing", source=SyncSource.BACKFILL, scope=SyncScope.HISTORICAL)),
    ):
        failed = data_247.load_and_save_all(db, user_id, start, end)
        assert failed.summary().status == SyncStatus.PARTIAL
        assert failed.failures == {data_type: "RuntimeError" if failure == "write" else "http_503"}
        assert failed.outcomes[data_type].rows_written == _row_count(db) == 1
        assert requests == 2

        fail = False
        requests = 0
        replayed = data_247.load_and_save_all(db, user_id, start, end)

    assert replayed.failures == {}
    assert replayed.outcomes[data_type].rows_written == _row_count(db) == 2
    assert replayed.inserted == 1


def test_live_keeps_legacy_page_collection_and_partial_fetch_behavior(db: Session, data_247: Oura247Data) -> None:
    user_id = UserFactory().id
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)

    def fetch(_db: Session, _user_id: UUID, endpoint: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if endpoint in (_ACTIVITY, _HEART_RATE):
            assert params is not None
            if "next_token" not in params:
                first = _activity(start) if endpoint == _ACTIVITY else _heart_rate(start)
                return {"data": [first], "next_token": "page-2"}
            if endpoint == _ACTIVITY:
                assert _row_count(db) == 0
                raise HTTPException(503)
            assert _row_count(db) == 1  # Activity committed, heart rate is still fetching.
            return {"data": [_heart_rate(end)]}
        return {"data": []}

    with (
        patch.object(data_247, "_make_api_request", side_effect=fetch),
        sync_run_context(SyncRunContext(run_id="testing", source=SyncSource.PULL, scope=SyncScope.LIVE)),
    ):
        result = data_247.load_and_save_all(db, user_id, start, end)

    assert result.failures == {}
    assert result.outcomes["activity"].rows_written == 1
    assert result.outcomes["heart_rate"].rows_written == 2
    assert _row_count(db) == 3
