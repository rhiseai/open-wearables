"""Publish-time routing must protect live work across every historical caller."""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from app.constants.celery_tasks import CeleryQueue, CeleryTask
from app.integrations.celery.tasks.process_sdk_upload_task import process_sdk_upload
from app.integrations.celery.tasks.sync_vendor_data_task import sync_vendor_data
from app.main import celery_app
from app.schemas.providers.mobile_sdk import SyncRequest
from app.services.outgoing_webhooks.batching import collect_sdk_webhooks
from app.services.sdk.import_service import ImportService


def _queue(name: str, args: tuple[Any, ...] = (), **kwargs: Any) -> str:
    return celery_app.amqp.router.route({}, name, args, kwargs)["queue"].name


@pytest.mark.parametrize("task_name", [CeleryTask.SYNC_VENDOR_DATA, CeleryTask.PROCESS_SDK_UPLOAD])
def test_live_calls_stay_in_their_original_queue(task_name: str) -> None:
    expected = CeleryQueue.DEFAULT if task_name == CeleryTask.SYNC_VENDOR_DATA else CeleryQueue.SDK_SYNC
    assert _queue(task_name) == expected


@pytest.mark.parametrize("linked", [False, True])
def test_oauth_manual_and_linked_backfills_share_the_vendor_history_queue(linked: bool) -> None:
    assert (
        _queue(
            CeleryTask.SYNC_VENDOR_DATA,
            user_id="user",
            providers=["oura"],
            is_historical=True,
            _skip_linked_fan_out=linked,
        )
        == CeleryQueue.HISTORICAL_VENDOR
    )
    assert _queue(CeleryTask.SYNC_VENDOR_DATA, ("user", None, None, ["oura"], True)) == CeleryQueue.HISTORICAL_VENDOR


@pytest.mark.parametrize(
    ("sync_type", "legacy_history", "expected"),
    [
        ("historical", False, CeleryQueue.HISTORICAL_SDK),
        ("historical", True, CeleryQueue.HISTORICAL_SDK),
        ("live", False, CeleryQueue.SDK_SYNC),
        ("live", True, CeleryQueue.SDK_SYNC),
        (None, False, CeleryQueue.SDK_SYNC),
        (None, True, CeleryQueue.HISTORICAL_SDK),
    ],
)
def test_sdk_classification_preserves_explicit_live_during_an_export(
    sync_type: str | None, legacy_history: bool, expected: str
) -> None:
    assert (
        _queue(
            CeleryTask.PROCESS_SDK_UPLOAD,
            content=None,
            payload_ref="s3://test-bucket/history.json",
            historical_export=legacy_history,
            sync_type=sync_type,
        )
        == expected
    )
    assert (
        _queue(
            CeleryTask.PROCESS_SDK_UPLOAD,
            (None, "application/json", "user", "apple", "batch", "s3://test/history", legacy_history, None, sync_type),
        )
        == expected
    )


@pytest.mark.parametrize("historical", [False, True])
def test_deferred_sleep_keeps_its_import_lane(historical: bool) -> None:
    expected = CeleryQueue.HISTORICAL_SDK if historical else CeleryQueue.SDK_SYNC
    assert _queue(CeleryTask.PROCESS_SDK_SLEEP, ("{}", "user", "batch", historical)) == expected
    assert _queue(CeleryTask.PROCESS_SDK_SLEEP, historical=historical) == expected


@pytest.mark.parametrize(
    ("scope", "suppressed", "expected"),
    [
        ("live", True, CeleryQueue.SDK_SYNC),
        ("historical", False, CeleryQueue.HISTORICAL_SDK),
        (None, True, CeleryQueue.HISTORICAL_SDK),
        (None, False, CeleryQueue.SDK_SYNC),
    ],
)
def test_deferred_sleep_publisher_separates_scheduling_from_webhook_suppression(
    scope: str | None, suppressed: bool, expected: str
) -> None:
    request = SyncRequest.model_validate(
        {
            "provider": "apple",
            "sdkVersion": "0.15.0",
            "syncTimestamp": datetime.now(timezone.utc).isoformat(),
            "syncType": scope,
            "data": {},
        }
    )
    with collect_sdk_webhooks("sleep-batch", historical=suppressed), patch.object(celery_app, "send_task") as publish:
        ImportService()._defer_sleep(request, "user", "sleep-batch")
    options = publish.call_args.kwargs
    assert options["args"][3] is suppressed
    route = celery_app.amqp.router.route(options, CeleryTask.PROCESS_SDK_SLEEP, options["args"], {})
    assert route["queue"].name == expected


@pytest.mark.parametrize("task", [process_sdk_upload, sync_vendor_data])
def test_delay_does_not_override_dynamic_routing(task: Any) -> None:
    kwargs = (
        {"user_id": "user", "is_historical": True}
        if task == sync_vendor_data
        else {
            "content": "{}",
            "content_type": "application/json",
            "user_id": "user",
            "provider": "apple",
            "sync_type": "historical",
        }
    )
    with patch.object(task.app, "send_task") as publish:
        task.apply_async(kwargs=kwargs)
    options = publish.call_args.kwargs
    assert options.get("queue") is None


def test_explicit_retry_queue_is_preserved() -> None:
    # Celery carries delivery_info into retry options; no new admission retries or budget.
    route = celery_app.amqp.router.route(
        {"queue": CeleryQueue.HISTORICAL_SDK},
        CeleryTask.PROCESS_SDK_UPLOAD,
        (),
        {"content": None, "payload_ref": "s3://test-bucket/history.json", "historical_export": True},
    )
    assert route["queue"].name == CeleryQueue.HISTORICAL_SDK
