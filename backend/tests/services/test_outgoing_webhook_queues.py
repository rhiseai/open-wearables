"""Outgoing webhook queues: per-event routing, timeseries opt-in gating, throughput settings."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.config import Settings, settings
from app.integrations.celery.tasks import webhook_queue_monitor_task
from app.integrations.celery.tasks.emit_webhook_event_task import emit_webhook_event
from app.main import celery_app
from app.schemas.webhooks.event_types import WebhookEventType
from app.services.outgoing_webhooks import svix as svix_service
from app.services.outgoing_webhooks.batching import collect_sdk_webhooks
from app.services.outgoing_webhooks.events import on_sleep_created, on_timeseries_batch_saved
from app.services.outgoing_webhooks.routing import (
    EMIT_WEBHOOK_TASK_NAME,
    WEBHOOK_BULK_QUEUE,
    WEBHOOK_PRIORITY_QUEUE,
    webhook_queue_for,
)
from app.services.outgoing_webhooks.subscriptions import (
    GATED_TIMESERIES_EVENT_TYPES,
    clear_subscription_cache,
    timeseries_event_enabled,
)

PRIORITY_EVENTS = {
    "connection.created",
    "connection.revoked",
    "sleep.created",
    "sleep.updated",
    "workout.created",
    "sync.completed",
    "body_composition.created",
}

_EMIT_TASK = "app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event"
_SUBSCRIBED = "app.services.outgoing_webhooks.svix.subscribed_event_types"


@pytest.fixture
def fresh_subscriptions() -> Generator[None, None, None]:
    clear_subscription_cache()
    yield
    clear_subscription_cache()


def _routed_queue(args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None) -> str:
    route = celery_app.amqp.router.route({}, EMIT_WEBHOOK_TASK_NAME, args, kwargs or {})
    return route["queue"].name


class TestRouting:
    @pytest.mark.parametrize("event_type", [e.value for e in WebhookEventType])
    def test_every_event_type_has_the_expected_queue(self, event_type: str) -> None:
        expected = WEBHOOK_PRIORITY_QUEUE if event_type in PRIORITY_EVENTS else WEBHOOK_BULK_QUEUE
        assert webhook_queue_for(event_type) == expected

    @pytest.mark.parametrize(
        ("event_type", "queue"),
        [
            ("workout.updated", WEBHOOK_PRIORITY_QUEUE),
            ("workout.deleted", WEBHOOK_PRIORITY_QUEUE),
            ("workout_metrics.created", WEBHOOK_BULK_QUEUE),
            ("sync.started", WEBHOOK_BULK_QUEUE),
            ("sync.failed", WEBHOOK_BULK_QUEUE),
            ("heart_rate.created", WEBHOOK_BULK_QUEUE),
            ("series.weight.created", WEBHOOK_BULK_QUEUE),
        ],
    )
    def test_routing_is_by_resource(self, event_type: str, queue: str) -> None:
        assert webhook_queue_for(event_type) == queue

    def test_celery_router_routes_delay_calls_by_event_type(self) -> None:
        assert _routed_queue(args=("connection.created", {})) == WEBHOOK_PRIORITY_QUEUE
        assert _routed_queue(args=("sync.completed", {})) == WEBHOOK_PRIORITY_QUEUE
        assert _routed_queue(args=("heart_rate.created", {})) == WEBHOOK_BULK_QUEUE
        assert _routed_queue(kwargs={"event_type": "sleep.created", "payload": {}}) == WEBHOOK_PRIORITY_QUEUE

    def test_delay_reaches_the_router_without_a_fixed_queue(self) -> None:
        """A task-level queue would arrive as an explicit option and override the router."""
        payload = {"type": "connection.created", "data": {}}
        with patch.object(celery_app, "send_task") as send_task:
            emit_webhook_event.delay("connection.created", payload, idempotency_key="k")
        options = send_task.call_args.kwargs
        assert options.get("queue") is None
        routed = celery_app.amqp.router.route(options, EMIT_WEBHOOK_TASK_NAME, ("connection.created", payload), {})
        assert routed["queue"].name == WEBHOOK_PRIORITY_QUEUE

    def test_both_queues_are_declared(self) -> None:
        queues = set(celery_app.conf.task_queues)
        assert {WEBHOOK_PRIORITY_QUEUE, WEBHOOK_BULK_QUEUE} <= queues

    def test_retry_stays_on_the_event_queue(self) -> None:
        payload = {
            "type": "connection.created",
            "data": {"user_id": str(uuid4()), "provider": "apple", "connection_id": str(uuid4())},
        }
        developer = MagicMock(id=uuid4(), email="dev@example.com")
        with (
            patch("app.integrations.celery.tasks.emit_webhook_event_task.developer_service") as dev_service,
            patch("app.integrations.celery.tasks.emit_webhook_event_task.svix_service") as svix_mock,
            patch.object(emit_webhook_event, "retry", side_effect=RuntimeError("retry")) as retry,
        ):
            dev_service.crud.get_all.return_value = [developer]
            svix_mock.send.return_value = None
            with pytest.raises(RuntimeError, match="retry"):
                emit_webhook_event("connection.created", payload, idempotency_key="connection.created.x")

        assert retry.call_args.kwargs["queue"] == WEBHOOK_PRIORITY_QUEUE
        svix_mock.forget_application.assert_called_once_with(str(developer.id))

    def test_idempotency_key_and_channels_reach_the_task_unchanged(self) -> None:
        user_id, record_id = uuid4(), uuid4()
        with patch(_EMIT_TASK) as task, patch.object(svix_service, "is_enabled", return_value=True):
            on_sleep_created(
                record_id=record_id,
                user_id=user_id,
                provider="apple",
                device=None,
                start_time="2026-10-10T22:00:00+00:00",
                end_time="2026-10-11T06:00:00+00:00",
                zone_offset=None,
                duration_seconds=28800,
            )
        kwargs = task.delay.call_args.kwargs
        assert kwargs["idempotency_key"].startswith(f"sleep.created.{record_id}.")
        assert kwargs["channels"] == [f"user.{user_id}"]


@pytest.mark.usefixtures("fresh_subscriptions")
class TestTimeseriesGating:
    def test_gated_set_is_timeseries_without_body_composition(self) -> None:
        assert "heart_rate.created" in GATED_TIMESERIES_EVENT_TYPES
        assert "steps.created" in GATED_TIMESERIES_EVENT_TYPES
        assert "body_composition.created" not in GATED_TIMESERIES_EVENT_TYPES
        assert not GATED_TIMESERIES_EVENT_TYPES & PRIORITY_EVENTS

    @pytest.mark.parametrize("mode", ["subscribed", "all", "none"])
    def test_product_events_are_never_gated(self, mode: str) -> None:
        with patch.object(settings, "outgoing_webhooks_timeseries_events", mode), patch(_SUBSCRIBED) as subscribed:
            subscribed.return_value = frozenset()
            for event_type in PRIORITY_EVENTS | {"sync.started", "meal.created"}:
                assert timeseries_event_enabled(event_type)

    def test_mode_all_and_none_do_not_call_svix(self) -> None:
        with patch(_SUBSCRIBED) as subscribed:
            with patch.object(settings, "outgoing_webhooks_timeseries_events", "all"):
                assert timeseries_event_enabled("heart_rate.created")
            with patch.object(settings, "outgoing_webhooks_timeseries_events", "none"):
                assert not timeseries_event_enabled("heart_rate.created")
        subscribed.assert_not_called()

    def test_subscribed_mode_follows_endpoint_filters(self) -> None:
        with (
            patch.object(settings, "outgoing_webhooks_timeseries_events", "subscribed"),
            patch(_SUBSCRIBED, return_value=frozenset({"heart_rate.created", "sleep.created"})),
        ):
            assert timeseries_event_enabled("heart_rate.created")
            assert not timeseries_event_enabled("steps.created")
            assert not timeseries_event_enabled("calories.created")

    def test_unfiltered_endpoint_receives_every_type(self) -> None:
        with (
            patch.object(settings, "outgoing_webhooks_timeseries_events", "subscribed"),
            patch(_SUBSCRIBED, return_value=None),
        ):
            assert timeseries_event_enabled("steps.created")

    def test_subscriptions_are_cached_per_process(self) -> None:
        with (
            patch.object(settings, "outgoing_webhooks_timeseries_events", "subscribed"),
            patch.object(settings, "outgoing_webhooks_subscription_cache_seconds", 300),
            patch(_SUBSCRIBED, return_value=frozenset()) as subscribed,
        ):
            for _ in range(50):
                timeseries_event_enabled("heart_rate.created")
            assert subscribed.call_count == 1
            clear_subscription_cache()
            timeseries_event_enabled("heart_rate.created")
            assert subscribed.call_count == 2

    def test_first_read_failure_keeps_emitting(self) -> None:
        with (
            patch.object(settings, "outgoing_webhooks_timeseries_events", "subscribed"),
            patch(_SUBSCRIBED, side_effect=ConnectionError("svix down")),
        ):
            assert timeseries_event_enabled("heart_rate.created")

    def test_later_failure_keeps_the_last_known_view(self) -> None:
        with (
            patch.object(settings, "outgoing_webhooks_timeseries_events", "subscribed"),
            patch.object(settings, "outgoing_webhooks_subscription_cache_seconds", 0),
            patch(_SUBSCRIBED, side_effect=[frozenset({"sleep.created"}), ConnectionError("svix down")]),
        ):
            assert not timeseries_event_enabled("heart_rate.created")
            assert not timeseries_event_enabled("heart_rate.created")

    def test_unsubscribed_timeseries_is_not_enqueued(self) -> None:
        with (
            patch.object(settings, "outgoing_webhooks_timeseries_events", "subscribed"),
            patch.object(svix_service, "is_enabled", return_value=True),
            patch(_SUBSCRIBED, return_value=frozenset({"sleep.created", "connection.created"})),
            patch(_EMIT_TASK) as task,
        ):
            on_timeseries_batch_saved(
                user_id=uuid4(), provider="apple", series_type="heart_rate", sample_count=1, samples=[]
            )
        task.delay.assert_not_called()

    def test_body_composition_is_enqueued_even_when_timeseries_are_off(self) -> None:
        with (
            patch.object(settings, "outgoing_webhooks_timeseries_events", "none"),
            patch.object(svix_service, "is_enabled", return_value=True),
            patch(_EMIT_TASK) as task,
        ):
            on_timeseries_batch_saved(user_id=uuid4(), provider="apple", series_type="weight", sample_count=1)
        assert task.delay.call_args.args[0] == "body_composition.created"

    def test_unsubscribed_timeseries_takes_no_sdk_batch_slot(self) -> None:
        with (
            patch.object(settings, "outgoing_webhooks_timeseries_events", "none"),
            patch.object(svix_service, "is_enabled", return_value=True),
            patch(_EMIT_TASK) as task,
        ):
            with collect_sdk_webhooks("batch-gated", historical=False) as batch:
                for _ in range(100):
                    on_timeseries_batch_saved(
                        user_id=uuid4(), provider="apple", series_type="heart_rate", sample_count=1
                    )
                on_sleep_created(
                    record_id=uuid4(),
                    user_id=uuid4(),
                    provider="apple",
                    device=None,
                    start_time="2026-10-10T22:00:00+00:00",
                    end_time="2026-10-11T06:00:00+00:00",
                    zone_offset=None,
                    duration_seconds=28800,
                )
            summary = batch.flush()
        assert [c.args[0] for c in task.delay.call_args_list] == ["sleep.created"]
        assert summary["suppressed"] == 0


class TestSvixSubscriptions:
    @staticmethod
    def _page(items: list[Any], *, done: bool = True, iterator: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(data=items, done=done, iterator=iterator)

    def test_collects_filters_of_enabled_endpoints_across_pages(self) -> None:
        client = MagicMock()
        client.application.list.return_value = self._page([SimpleNamespace(id="app_1")])
        client.endpoint.list.side_effect = [
            self._page(
                [
                    SimpleNamespace(disabled=False, event_types=["sleep.created", "connection.created"]),
                    SimpleNamespace(disabled=True, event_types=None),
                ],
                done=False,
                iterator="it_2",
            ),
            self._page([SimpleNamespace(disabled=False, event_types=["sync.completed"])]),
        ]
        with patch.object(svix_service, "_client", client):
            assert svix_service.subscribed_event_types() == {"sleep.created", "connection.created", "sync.completed"}
        assert client.endpoint.list.call_args_list[1].args[1].iterator == "it_2"

    def test_enabled_unfiltered_endpoint_means_every_type(self) -> None:
        client = MagicMock()
        client.application.list.return_value = self._page([SimpleNamespace(id="app_1")])
        client.endpoint.list.return_value = self._page([SimpleNamespace(disabled=False, event_types=None)])
        with patch.object(svix_service, "_client", client):
            assert svix_service.subscribed_event_types() is None

    def test_application_is_ensured_once_per_process(self) -> None:
        client = MagicMock()
        developer_id = str(uuid4())
        with patch.object(svix_service, "_client", client):
            svix_service.ensure_application(developer_id, "dev@example.com")
            svix_service.ensure_application(developer_id, "dev@example.com")
            assert client.application.get_or_create.call_count == 1
            svix_service.forget_application(developer_id)
            svix_service.ensure_application(developer_id, "dev@example.com")
            assert client.application.get_or_create.call_count == 2

    def test_failed_ensure_is_retried_next_time(self) -> None:
        client = MagicMock()
        client.application.get_or_create.side_effect = [RuntimeError("boom"), None]
        developer_id = str(uuid4())
        with patch.object(svix_service, "_client", client):
            svix_service.ensure_application(developer_id, "dev@example.com")
            svix_service.ensure_application(developer_id, "dev@example.com")
        assert client.application.get_or_create.call_count == 2


class TestQueueDepthMonitor:
    def test_reports_both_queues_and_warns_over_threshold(self) -> None:
        redis = MagicMock()
        redis.llen.side_effect = lambda name: {WEBHOOK_PRIORITY_QUEUE: 3, WEBHOOK_BULK_QUEUE: 44306}[name]
        with (
            patch.object(webhook_queue_monitor_task, "get_redis_client", return_value=redis),
            patch.object(webhook_queue_monitor_task, "log_structured") as log,
            patch.object(settings, "webhook_queue_depth_warn_threshold", 1000),
        ):
            result = webhook_queue_monitor_task.report_webhook_queue_depth()

        assert result == {WEBHOOK_PRIORITY_QUEUE: 3, WEBHOOK_BULK_QUEUE: 44306}
        levels = {c.kwargs["queue"]: c.args[1] for c in log.call_args_list}
        assert levels == {WEBHOOK_PRIORITY_QUEUE: "info", WEBHOOK_BULK_QUEUE: "warning"}
        assert all(c.kwargs["action"] == "webhook_queue_depth" for c in log.call_args_list)

    def test_is_scheduled_by_beat_on_the_priority_queue(self) -> None:
        task_name = "app.integrations.celery.tasks.webhook_queue_monitor_task.report_webhook_queue_depth"
        schedule = celery_app.conf.beat_schedule["report-webhook-queue-depth"]
        assert schedule["task"] == task_name
        assert schedule["schedule"] == float(settings.webhook_queue_monitor_interval_seconds)
        assert celery_app.amqp.router.route({}, task_name)["queue"].name == WEBHOOK_PRIORITY_QUEUE


class TestConfigDefaults:
    def test_settings_defaults(self) -> None:
        fields = Settings.model_fields
        assert fields["outgoing_webhooks_timeseries_events"].default == "subscribed"
        assert fields["outgoing_webhooks_subscription_cache_seconds"].default == 300
        assert fields["webhook_emit_rate_limit"].default is None
        assert fields["webhook_queue_monitor_interval_seconds"].default == 60
        assert fields["webhook_queue_depth_warn_threshold"].default == 1000

    def test_emit_task_rate_limit_comes_from_settings(self) -> None:
        assert emit_webhook_event.rate_limit == settings.webhook_emit_rate_limit


class TestWorkerScript:
    @staticmethod
    def _run_worker_script(tmp_path: Path, extra_env: dict[str, str] | None = None) -> list[str]:
        """Run worker.sh against a fake ``uv`` that records each worker command line."""
        log = tmp_path / "commands.log"
        mock_uv = tmp_path / "uv"
        mock_uv.write_text(
            """#!/bin/bash
echo "$*" >> "$COMMAND_LOG"
if [[ "$*" == *"cpu@%h"* ]]; then
  sleep 0.5
  exit 3
fi
trap 'exit 0' TERM INT
while true; do sleep 0.1; done
"""
        )
        mock_uv.chmod(0o755)
        environment = os.environ.copy()
        environment.pop("CELERY_IO_CONCURRENCY", None)
        environment.pop("CELERY_WEBHOOK_PRIORITY_CONCURRENCY", None)
        environment.update({"PATH": f"{tmp_path}:{environment['PATH']}", "COMMAND_LOG": str(log)})
        environment.update(extra_env or {})
        subprocess.run(
            ["bash", "scripts/start/worker.sh"],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            timeout=10,
            check=False,
        )
        return [line for line in log.read_text().splitlines() if " worker " in f" {line} "]

    @staticmethod
    def _worker(commands: list[str], node: str) -> list[str]:
        return next(line for line in commands if f"-n {node}" in line).split()

    @staticmethod
    def _option(argv: list[str], name: str) -> str:
        for index, arg in enumerate(argv):
            if arg.startswith(f"{name}="):
                return arg.split("=", 1)[1]
            if arg == name:
                return argv[index + 1]
        raise AssertionError(f"{name} not in {argv}")

    def test_layout_and_default_concurrency(self, tmp_path: Path) -> None:
        commands = self._run_worker_script(tmp_path)
        io = self._worker(commands, "io@%h")
        webhook = self._worker(commands, "webhook@%h")

        assert self._option(webhook, "-Q") == WEBHOOK_PRIORITY_QUEUE
        assert self._option(webhook, "--pool") == "threads"
        assert self._option(webhook, "--concurrency") == "4"

        io_queues = self._option(io, "-Q").split(",")
        assert io_queues[0] == WEBHOOK_PRIORITY_QUEUE
        assert WEBHOOK_BULK_QUEUE in io_queues
        assert self._option(io, "--concurrency") == "16"

    def test_concurrency_is_configurable(self, tmp_path: Path) -> None:
        commands = self._run_worker_script(
            tmp_path, {"CELERY_IO_CONCURRENCY": "32", "CELERY_WEBHOOK_PRIORITY_CONCURRENCY": "8"}
        )
        assert self._option(self._worker(commands, "io@%h"), "--concurrency") == "32"
        assert self._option(self._worker(commands, "webhook@%h"), "--concurrency") == "8"
