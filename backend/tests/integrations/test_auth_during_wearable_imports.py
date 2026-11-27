"""Check API liveness during a pool wait with two real wearable writers in flight."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from importlib import import_module
from threading import Barrier, Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import PoolProxiedConnection

from app import database
from app.config import settings
from app.integrations.celery.tasks import process_sdk_upload_task
from app.main import api
from app.models import ApiKey, DataSource, ProviderPriority, SeriesSample, User
from app.repositories.data_point_series_repository import DataPointSeriesRepository, WriteCounts
from app.schemas.enums import ProviderName
from app.services.api_key_service import api_key_service
from app.services.providers.oura.data_247 import Oura247Data
from app.services.providers.oura.strategy import OuraStrategy

_BATCH_SIZE = 128
_WAIT_SECONDS = 15


def _apple_records(timestamps: list[str]) -> str:
    return json.dumps(
        {
            "provider": "apple",
            "sdkVersion": "0.15.0",
            "syncTimestamp": timestamps[-1],
            "data": {
                "records": [
                    {
                        "id": f"test-apple-hr-{index}",
                        "type": "HKQuantityTypeIdentifierHeartRate",
                        "value": 60,
                        "unit": "count/min",
                        "startDate": timestamp,
                        "endDate": timestamp,
                        "zoneOffset": "+00:00",
                        "source": {"name": "Apple Watch", "productType": "Watch7,1", "deviceType": "watch"},
                    }
                    for index, timestamp in enumerate(timestamps)
                ]
            },
        }
    )


def _import_oura(session_factory: sessionmaker[Session], user_id: UUID, timestamps: list[str]) -> WriteCounts:
    # Synthetic Oura response, real provider normalization, COPY/merge and commit.
    data = OuraStrategy().data_247
    assert isinstance(data, Oura247Data)
    with session_factory() as db:
        return data.save_heart_rate_data(db, user_id, [{"bpm": 68, "timestamp": timestamp} for timestamp in timestamps])


async def test_healthz_serves_while_api_auth_waits_during_wearable_imports(
    engine: Engine, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reserve the API slot independently of the two worker-side transactions."""
    api_engine = create_engine(engine.url, pool_size=1, max_overflow=0, pool_timeout=3)
    api_sessions = sessionmaker(api_engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", api_sessions)
    monkeypatch.setattr(import_module("app.services.api_key_service"), "SessionLocal", api_sessions)
    monkeypatch.setattr(process_sdk_upload_task, "SessionLocal", session_factory)
    monkeypatch.setattr(settings, "persist_live_sync_runs", False)
    monkeypatch.setattr(settings, "outgoing_webhooks_enabled", False)

    both_writers_merged = Barrier(3)
    release_writers = Event()
    merge = DataPointSeriesRepository._insert_data_points

    def pause_after_merge(repository: DataPointSeriesRepository, *args: Any, **kwargs: Any) -> WriteCounts:
        counts = merge(repository, *args, **kwargs)
        both_writers_merged.wait(_WAIT_SECONDS)
        assert release_writers.wait(_WAIT_SECONDS), "Timed out waiting to commit the provider imports"
        return counts

    monkeypatch.setattr(DataPointSeriesRepository, "_insert_data_points", pause_after_merge)

    user_id = uuid4()
    providers = (ProviderName.APPLE, ProviderName.OURA)
    with session_factory() as db:
        existing_priorities = db.scalars(
            select(ProviderPriority.id).where(ProviderPriority.provider.in_(providers))
        ).all()
        db.add(User(id=user_id))
        api_key, raw_key = api_key_service.create_api_key(db, None)
        key_id = api_key.id

    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
    timestamps = [(start + timedelta(seconds=index)).isoformat() for index in range(_BATCH_SIZE)]
    checkout_started = Event()
    checkout_finished = Event()
    pool_connect = api_engine.pool.connect

    def observe_checkout() -> PoolProxiedConnection:
        checkout_started.set()
        try:
            return pool_connect()
        finally:
            checkout_finished.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            apple = workers.submit(
                process_sdk_upload_task.process_sdk_upload.run,
                _apple_records(timestamps),
                "application/json",
                str(user_id),
                "apple",
                str(uuid4()),
            )
            oura = workers.submit(_import_oura, session_factory, user_id, timestamps)
            try:
                await asyncio.to_thread(both_writers_merged.wait, _WAIT_SECONDS)
                with api_engine.connect() as reserved:
                    monkeypatch.setattr(api_engine.pool, "connect", observe_checkout)
                    async with AsyncClient(
                        transport=ASGITransport(app=api, raise_app_exceptions=False), base_url="http://testserver"
                    ) as client:
                        protected_request = asyncio.create_task(
                            client.get(
                                f"{settings.api_v1}/users/{user_id}/timeseries",
                                headers={"X-Open-Wearables-API-Key": raw_key},
                                params={
                                    "start_time": start.isoformat(),
                                    "end_time": (start + timedelta(minutes=5)).isoformat(),
                                },
                            )
                        )
                        try:
                            assert await asyncio.to_thread(checkout_started.wait, _WAIT_SECONDS)
                            health = await client.get("/healthz")
                            assert health.status_code == 200
                            assert not checkout_finished.is_set(), "/healthz stalled until API-key checkout returned"
                        finally:
                            reserved.close()
                            protected = await asyncio.wait_for(protected_request, _WAIT_SECONDS)
                        assert protected.status_code == 200
            finally:
                release_writers.set()

            assert apple.result(timeout=_WAIT_SECONDS)["records_saved"] == _BATCH_SIZE
            assert oura.result(timeout=_WAIT_SECONDS).inserted == _BATCH_SIZE

        with session_factory() as db:
            counts = db.execute(
                select(DataSource.provider, func.count(SeriesSample.id))
                .join(SeriesSample, SeriesSample.data_source_id == DataSource.id)
                .where(DataSource.user_id == user_id)
                .group_by(DataSource.provider)
            ).all()
            assert {provider: count for provider, count in counts} == {"apple": _BATCH_SIZE, "oura": _BATCH_SIZE}
    finally:
        api_engine.dispose()
        with session_factory() as db:
            db.execute(delete(ApiKey).where(ApiKey.id == key_id))
            db.execute(delete(User).where(User.id == user_id))
            # The importers can initialize global priorities when creating data sources.
            db.execute(
                delete(ProviderPriority).where(
                    ProviderPriority.provider.in_(providers), ProviderPriority.id.not_in(existing_priorities)
                )
            )
            db.commit()
