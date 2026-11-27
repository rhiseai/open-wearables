"""Historical imports keep their Redis backlog while a separate worker serves live SDK jobs."""

import asyncio
from datetime import datetime, timedelta, timezone
from importlib import import_module
from multiprocessing import get_context
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import EndpointConnectionError
from celery import Celery, signals
from celery._state import get_current_app
from celery.contrib.testing.worker import start_worker
from httpx import ASGITransport, AsyncClient, Client, Request, Response
from redis import Redis
from sqlalchemy import create_engine, delete, event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app import database
from app.config import settings
from app.constants.celery_tasks import CeleryTask
from app.integrations.celery.tasks import process_sdk_upload_task
from app.main import api, celery_app
from app.models import ApiKey, DataSource, ProviderPriority, SeriesSample, User, UserConnection
from app.repositories.provider_priority_repository import ProviderPriorityRepository
from app.schemas.enums import ProviderName
from app.services.api_key_service import api_key_service
from tests.integrations.test_auth_during_wearable_imports import _apple_records

_WAIT = 60
_HISTORY_QUEUES = ("historical_sdk", "historical_vendor")
_BATCH_SIZE = 32
_WAITING_BATCHES = 6
_OURA_PAGE_SIZE = 8
_HISTORICAL_SESSION = "historical-import-worker-isolation"


def _run_worker(
    worker_name: str,
    queues: tuple[str, ...],
    postgres_url: str,
    redis_url: str,
    payloads: dict[str, str],
    hold_ref: str,
    retry_ref: str,
    ready: Any,
    release: Any,
    stop: Any,
    apple_held: Any,
    oura_held: Any,
    retry_deliveries: Any,
) -> None:
    """Distinct OS processes keep Redis prefetch state local, as on ECS."""
    redis_address = urlparse(redis_url)
    settings.redis_host = redis_address.hostname or "localhost"
    settings.redis_port = redis_address.port or 6379
    # The xdist worker's own Redis database: database 0 is gw0's, whose rollup lock these workers would take.
    settings.redis_db = int(redis_address.path.lstrip("/") or 0)
    settings.outgoing_webhooks_enabled = False
    settings.telemetry_enabled = False
    settings.persist_live_sync_runs = False

    engine = create_engine(postgres_url, connect_args={"application_name": worker_name})
    sessions = sessionmaker(engine, expire_on_commit=False)
    process_sdk_upload_task.SessionLocal = sessions
    import_module("app.integrations.celery.tasks.sync_vendor_data_task").SessionLocal = sessions
    import_module("app.services.sync_status_service").SessionLocal = sessions

    def load_payload(ref: str) -> str:
        task = process_sdk_upload_task.process_sdk_upload
        if ref == retry_ref:
            retry_deliveries.put(
                (task.request.id, task.request.retries, task.request.delivery_info["routing_key"], ref)
            )
            if task.request.retries == 0:
                raise EndpointConnectionError(endpoint_url="https://s3.example.test")
        return payloads[ref]

    def discard_payload(ref: str) -> None:
        # The references stand in for objects in a shared bucket. Each process has its own copy.
        payloads.pop(ref, None)

    def track_sdk_task(sender: Any = None, kwargs: dict[str, Any] | None = None, **_: Any) -> None:
        if sender.name == CeleryTask.PROCESS_SDK_UPLOAD and kwargs and kwargs.get("payload_ref") == hold_ref:

            @event.listens_for(engine, "after_cursor_execute", once=False)
            def hold_after_coverage(
                connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
            ) -> None:
                if statement.lower().lstrip().startswith("insert into data_type_coverage") and not apple_held.is_set():
                    apple_held.set()
                    assert release.wait(_WAIT), "Apple transaction was not released"

    def request(client: Client, method: str, url: str, **kwargs: Any) -> Response:
        req = Request(method, url, params=kwargs.get("params"))
        assert req.url.host == "api.ouraring.com"
        if not oura_held.is_set():
            oura_held.set()
            assert release.wait(_WAIT), "Oura HTTP request was not released"
        if req.url.path.endswith("/heartrate"):
            lower = datetime.fromisoformat(req.url.params["start_datetime"])
            page = int(req.url.params.get("next_token", "0"))
            body = {
                "data": [
                    {"bpm": 65, "timestamp": (lower + timedelta(minutes=page * _OURA_PAGE_SIZE + i)).isoformat()}
                    for i in range(_OURA_PAGE_SIZE)
                ],
                "next_token": "1" if page == 0 else None,
            }
        else:
            body = {} if req.url.path.endswith("/personal_info") else {"data": []}
        return Response(200, json=body, request=req)

    signals.task_prerun.connect(track_sdk_task, weak=False)
    celery_app.conf.update(
        broker_url=redis_url,
        result_backend=redis_url,
        worker_disable_prefetch=queues == _HISTORY_QUEUES,
        # The deployed worker's TCP keepalive options use Linux socket constants.
        broker_transport_options={},
    )
    try:
        with (
            patch.object(process_sdk_upload_task, "get_payload_from_s3", load_payload),
            patch.object(process_sdk_upload_task, "delete_payload_from_s3", discard_payload),
            patch.object(process_sdk_upload_task, "_S3_RETRY_BASE_SECONDS", 0),
            patch.object(Client, "request", request),
            start_worker(
                get_current_app(),
                concurrency=1,
                pool="threads",
                hostname=worker_name,
                queues=queues,
                perform_ping_check=False,
                loglevel="WARNING",
            ),
        ):
            ready.set()
            stop.wait(_WAIT * 3)
    finally:
        engine.dispose()


async def _probe_api(user_id: UUID, raw_key: str, start: datetime) -> None:
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://testserver") as client:
        health, timeseries = await asyncio.gather(
            client.get("/healthz"),
            client.get(
                f"{settings.api_v1}/users/{user_id}/timeseries",
                headers={"X-Open-Wearables-API-Key": raw_key},
                params={"start_time": start.isoformat(), "end_time": (start + timedelta(days=1)).isoformat()},
            ),
        )
    assert health.status_code == 200
    assert timeseries.status_code == 200


# Two spawned Celery workers take about 15 s to start, run and stop: past the suite's 10 s default.
@pytest.mark.timeout(60)
def test_historical_backlog_leaves_live_sdk_worker_available(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    redis_url = settings.redis_url
    producer = Celery("import-isolation-producer", broker=redis_url, backend=redis_url, set_as_current=False)
    producer.conf.update(
        task_routes=celery_app.conf.task_routes,
        task_queues=celery_app.conf.task_queues,
        task_default_queue=celery_app.conf.task_default_queue,
        task_default_exchange=celery_app.conf.task_default_exchange,
    )
    broker = Redis.from_url(redis_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(import_module("app.services.api_key_service"), "SessionLocal", sessions)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("SENTRY_ENABLED", "false")

    user_id, live_user = uuid4(), uuid4()
    providers = (ProviderName.APPLE, ProviderName.OURA)
    with sessions() as db:
        existing_priorities = db.scalars(
            select(ProviderPriority.id).where(ProviderPriority.provider.in_(providers))
        ).all()
        for provider in providers:
            ProviderPriorityRepository().ensure_provider_exists(db, provider)
        db.add_all([User(id=user_id), User(id=live_user)])
        db.flush()
        db.add(
            UserConnection(
                id=uuid4(),
                user_id=user_id,
                provider="oura",
                status="active",
                provider_user_id="test-oura-account",
                access_token="test-oura-token",
                updated_at=datetime.now(timezone.utc),
            )
        )
        key, raw_key = api_key_service.create_api_key(db, None)
        key_id = key.id

    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=90)
    refs = [f"s3://fixture/history/{i}.json" for i in range(_WAITING_BATCHES + 1)]
    payloads = {
        ref: _apple_records(
            [(start + timedelta(seconds=index * _BATCH_SIZE + j)).isoformat() for j in range(_BATCH_SIZE)]
        )
        for index, ref in enumerate(refs)
    }
    ctx = get_context("spawn")
    release, stop, apple_held, oura_held = ctx.Event(), ctx.Event(), ctx.Event(), ctx.Event()
    retry_deliveries = ctx.Queue()
    workers = []

    def start_worker_process(name: str, queues: tuple[str, ...]) -> None:
        ready = ctx.Event()
        worker = ctx.Process(
            target=_run_worker,
            args=(
                name,
                queues,
                engine.url.render_as_string(hide_password=False),
                redis_url,
                payloads,
                refs[0],
                refs[1],
                ready,
                release,
                stop,
                apple_held,
                oura_held,
                retry_deliveries,
            ),
        )
        worker.start()
        workers.append(worker)
        assert ready.wait(_WAIT), f"{name} never became ready"

    def send_apple(ref: str) -> tuple[str, Any]:
        batch_id = str(uuid4())
        result = producer.send_task(
            CeleryTask.PROCESS_SDK_UPLOAD,
            kwargs={
                "content": None,
                "content_type": "application/json",
                "user_id": str(user_id),
                "provider": "apple",
                "payload_ref": ref,
                "batch_id": batch_id,
                "sync_type": "historical",
                "sync_session_id": _HISTORICAL_SESSION,
            },
        )
        return batch_id, result

    try:
        start_worker_process("test-history-1", _HISTORY_QUEUES)
        start_worker_process("test-history-2", _HISTORY_QUEUES)
        start_worker_process("test-live", ("default", "sdk_sync"))
        oura = producer.send_task(
            CeleryTask.SYNC_VENDOR_DATA,
            kwargs={
                "user_id": str(user_id),
                "start_date": start.isoformat(),
                "end_date": (start + timedelta(days=90)).isoformat(),
                "providers": ["oura"],
                "is_historical": True,
            },
        )
        assert oura_held.wait(_WAIT), "Oura historical worker did not reach provider HTTP"
        apple = [send_apple(refs[0])]
        assert apple_held.wait(_WAIT), "Apple historical worker did not reach the coverage upsert"
        apple.extend(send_apple(ref) for ref in refs[1:])

        live = producer.send_task(
            CeleryTask.PROCESS_SDK_UPLOAD,
            kwargs={
                "content": _apple_records([start.isoformat()]),
                "content_type": "application/json",
                "user_id": str(live_user),
                "provider": "apple",
                "batch_id": str(uuid4()),
                "sync_type": "live",
                "historical_export": True,
            },
        )
        assert live.get(timeout=_WAIT)["records_inserted"] == 1
        assert not release.is_set()
        assert broker.llen(_HISTORY_QUEUES[0]) == _WAITING_BATCHES
        asyncio.run(_probe_api(user_id, raw_key, start))
        with engine.connect() as monitor:
            assert (
                monitor.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE application_name LIKE 'test-history-%' AND wait_event_type = 'Lock'"
                    )
                )
                == 0
            )

        release.set()
        assert oura.get(timeout=_WAIT)["providers_synced"]["oura"]["success"]
        for batch_id, result in apple:
            saved = result.get(timeout=_WAIT)
            assert saved["batch_id"] == batch_id
            assert saved["records_inserted"] == _BATCH_SIZE
        retry_id = apple[1][1].id
        assert retry_deliveries.get(timeout=_WAIT) == (retry_id, 0, _HISTORY_QUEUES[0], refs[1])
        assert retry_deliveries.get(timeout=_WAIT) == (retry_id, 1, _HISTORY_QUEUES[0], refs[1])

        with sessions() as db:
            counts = db.execute(
                select(DataSource.provider, func.count(SeriesSample.id))
                .join(SeriesSample, SeriesSample.data_source_id == DataSource.id)
                .where(DataSource.user_id == user_id)
                .group_by(DataSource.provider)
            ).all()
            assert {provider: count for provider, count in counts} == {
                "apple": (_WAITING_BATCHES + 1) * _BATCH_SIZE,
                "oura": _OURA_PAGE_SIZE * 6,
            }
    finally:
        release.set()
        stop.set()
        for worker in workers:
            worker.join(15)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        producer.close()
        broker.close()
        retry_deliveries.close()
        with sessions() as db:
            db.execute(delete(ApiKey).where(ApiKey.id == key_id))
            db.execute(delete(User).where(User.id.in_((user_id, live_user))))
            db.execute(
                delete(ProviderPriority).where(
                    ProviderPriority.provider.in_(providers),
                    ProviderPriority.id.not_in(existing_priorities),
                )
            )
            db.commit()
