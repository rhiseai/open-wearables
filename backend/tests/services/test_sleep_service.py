"""
Tests for Apple HealthKit sleep service processing.

Tests the sleep pipeline (handle_sleep_data, _apply_transition, _calculate_final_metrics,
persist_sleep) using synthetic payloads modeled after real Apple HealthKit SDK data.

Apple Watch sleep data patterns:
- Older Apple Watch (pre-watchOS 9): only "in_bed" and "sleeping" stages
- Newer Apple Watch (watchOS 9+): "in_bed", "awake", "light", "deep", "rem" stages
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.constants.sleep import SleepStageType
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
)
from app.schemas.providers.mobile_sdk import (
    SleepState,
    SleepStateStage,
    SyncRequest,
)
from app.services.apple.healthkit.sleep_service import (
    _calculate_final_metrics,
    handle_sleep_data,
    persist_sleep,
)


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Synthetic payload: older Apple Watch (pre-watchOS 9) with sleeping + in_bed
# Mimics pattern: Watch sends "sleeping" segments, iPhone sends "in_bed"
# ---------------------------------------------------------------------------
OLD_WATCH_PAYLOAD = {
    "provider": "apple",
    "sdkVersion": "0.5.0",
    "syncTimestamp": "2026-03-11T13:28:04Z",
    "data": {
        "records": [],
        "workouts": [],
        "sleep": [
            {
                "id": "aaaa1111-0000-0000-0000-000000000001",
                "parentId": None,
                "stage": "sleeping",
                "startDate": "2026-03-10T23:00:00Z",
                "endDate": "2026-03-10T23:50:00Z",
                "source": {
                    "device_type": "watch",
                    "device_model": "Watch3,3",
                },
            },
            {
                "id": "aaaa1111-0000-0000-0000-000000000002",
                "parentId": None,
                "stage": "sleeping",
                "startDate": "2026-03-10T23:52:00Z",
                "endDate": "2026-03-11T00:45:00Z",
                "source": {
                    "device_type": "watch",
                    "device_model": "Watch3,3",
                },
            },
            {
                "id": "aaaa1111-0000-0000-0000-000000000003",
                "parentId": None,
                "stage": "sleeping",
                "startDate": "2026-03-11T00:47:00Z",
                "endDate": "2026-03-11T01:30:00Z",
                "source": {
                    "device_type": "watch",
                    "device_model": "Watch3,3",
                },
            },
            {
                "id": "aaaa1111-0000-0000-0000-000000000004",
                "parentId": None,
                "stage": "in_bed",
                "startDate": "2026-03-10T22:55:00Z",
                "endDate": "2026-03-11T01:35:00Z",
                "source": {
                    "device_type": "phone",
                    "device_model": "iPhone15,2",
                },
            },
        ],
    },
}

# ---------------------------------------------------------------------------
# Synthetic payload: detailed stages (watchOS 9+ style)
# ---------------------------------------------------------------------------
DETAILED_STAGES_PAYLOAD = {
    "provider": "apple",
    "sdkVersion": "1.0.0",
    "syncTimestamp": "2026-03-11T13:28:04Z",
    "data": {
        "records": [],
        "workouts": [],
        "sleep": [
            {
                "id": "A001",
                "stage": "in_bed",
                "startDate": "2026-03-10T22:00:00Z",
                "endDate": "2026-03-11T06:00:00Z",
                "source": {"device_type": "phone", "device_model": "iPhone15,2"},
            },
            {
                "id": "A002",
                "stage": "light",
                "startDate": "2026-03-10T22:15:00Z",
                "endDate": "2026-03-10T23:00:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A003",
                "stage": "deep",
                "startDate": "2026-03-10T23:00:00Z",
                "endDate": "2026-03-11T00:30:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A004",
                "stage": "rem",
                "startDate": "2026-03-11T00:30:00Z",
                "endDate": "2026-03-11T01:15:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A005",
                "stage": "awake",
                "startDate": "2026-03-11T01:15:00Z",
                "endDate": "2026-03-11T01:25:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A006",
                "stage": "deep",
                "startDate": "2026-03-11T01:25:00Z",
                "endDate": "2026-03-11T02:30:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A007",
                "stage": "light",
                "startDate": "2026-03-11T02:30:00Z",
                "endDate": "2026-03-11T04:00:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A008",
                "stage": "rem",
                "startDate": "2026-03-11T04:00:00Z",
                "endDate": "2026-03-11T05:00:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
            {
                "id": "A009",
                "stage": "light",
                "startDate": "2026-03-11T05:00:00Z",
                "endDate": "2026-03-11T05:45:00Z",
                "source": {"device_type": "watch", "device_model": "Watch7,1"},
            },
        ],
    },
}


class TestCalculateFinalMetrics:
    """Tests for _calculate_final_metrics with different stage combinations."""

    def test_sleeping_stages_only(self) -> None:
        """Older Apple Watch data: only 'sleeping' stages should NOT map to deep."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-03-10T23:00:00Z"),
                end_time=_dt("2026-03-10T23:50:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-03-10T23:52:00Z"),
                end_time=_dt("2026-03-11T00:45:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-03-11T00:47:00Z"),
                end_time=_dt("2026-03-11T01:30:00Z"),
            ),
        ]

        metrics, cleaned = _calculate_final_metrics(stages)

        # "sleeping" should go to sleeping_seconds, NOT deep_seconds
        assert metrics["deep_seconds"] == 0
        assert metrics["light_seconds"] == 0
        assert metrics["rem_seconds"] == 0
        assert metrics["sleeping_seconds"] > 0

        # All cleaned stages should be SLEEPING type
        for s in cleaned:
            assert s.stage == SleepStageType.SLEEPING

        # Total sleeping time: 50min + 53min + 43min = 146min = 8760s
        total_sleeping = metrics["sleeping_seconds"]
        assert total_sleeping == pytest.approx(8760, abs=60)

    def test_sleeping_plus_in_bed(self) -> None:
        """Mixed old-style data: sleeping (watch) + in_bed (phone)."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-03-10T23:00:00Z"),
                end_time=_dt("2026-03-11T01:30:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.IN_BED,
                start_time=_dt("2026-03-10T22:55:00Z"),
                end_time=_dt("2026-03-11T01:35:00Z"),
            ),
        ]

        metrics, cleaned = _calculate_final_metrics(stages)

        # deep should be 0 — sleeping is not deep
        assert metrics["deep_seconds"] == 0
        assert metrics["sleeping_seconds"] > 0
        # in_bed calculated from in_bed intervals
        assert metrics["in_bed_seconds"] > 0
        # Cleaned stages should only include sleeping (not in_bed)
        assert all(s.stage == SleepStageType.SLEEPING for s in cleaned)

    def test_detailed_stages(self) -> None:
        """Modern Apple Watch data with deep/light/rem/awake breakdown."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.LIGHT, start_time=_dt("2026-03-10T22:15:00Z"), end_time=_dt("2026-03-10T23:00:00Z")
            ),
            SleepStateStage(
                stage=SleepStageType.DEEP, start_time=_dt("2026-03-10T23:00:00Z"), end_time=_dt("2026-03-11T00:30:00Z")
            ),
            SleepStateStage(
                stage=SleepStageType.REM, start_time=_dt("2026-03-11T00:30:00Z"), end_time=_dt("2026-03-11T01:15:00Z")
            ),
            SleepStateStage(
                stage=SleepStageType.AWAKE, start_time=_dt("2026-03-11T01:15:00Z"), end_time=_dt("2026-03-11T01:25:00Z")
            ),
            SleepStateStage(
                stage=SleepStageType.DEEP, start_time=_dt("2026-03-11T01:25:00Z"), end_time=_dt("2026-03-11T02:30:00Z")
            ),
        ]

        metrics, cleaned = _calculate_final_metrics(stages)

        assert metrics["sleeping_seconds"] == 0
        assert metrics["light_seconds"] == 45 * 60  # 45 min
        assert metrics["deep_seconds"] == (90 + 65) * 60  # 155 min
        assert metrics["rem_seconds"] == 45 * 60  # 45 min
        assert metrics["awake_seconds"] == 10 * 60  # 10 min

    def test_in_bed_fallback_includes_sleeping(self) -> None:
        """When no in_bed stages exist, in_bed_seconds should sum all sleep types."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-03-11T00:00:00Z"),
                end_time=_dt("2026-03-11T06:00:00Z"),
            ),
        ]

        metrics, _ = _calculate_final_metrics(stages)

        # No in_bed stages → fallback includes sleeping_seconds
        assert metrics["in_bed_seconds"] == metrics["sleeping_seconds"] + metrics["awake_seconds"]

    def test_empty_stages(self) -> None:
        """Empty stages list should return zero metrics."""
        metrics, cleaned = _calculate_final_metrics([])

        assert metrics["sleeping_seconds"] == 0
        assert metrics["deep_seconds"] == 0
        assert metrics["in_bed_seconds"] == 0
        assert cleaned == []

    def test_only_in_bed_treated_as_sleeping(self) -> None:
        """When only in_bed stages exist (no sleep phases), treat in_bed as sleeping."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.IN_BED,
                start_time=_dt("2026-04-10T22:30:00Z"),
                end_time=_dt("2026-04-11T06:00:00Z"),
            ),
        ]

        metrics, cleaned = _calculate_final_metrics(stages)

        # in_bed should be converted to sleeping
        assert metrics["sleeping_seconds"] == 7.5 * 3600
        assert metrics["deep_seconds"] == 0
        assert metrics["light_seconds"] == 0
        assert metrics["rem_seconds"] == 0
        # in_bed_seconds still calculated from original in_bed intervals
        assert metrics["in_bed_seconds"] == 7.5 * 3600
        # Hypnogram should show sleeping, not in_bed
        assert len(cleaned) == 1
        assert cleaned[0].stage == SleepStageType.SLEEPING

    def test_detailed_plus_sleeping_wrapper_excludes_sleeping(self) -> None:
        """When detailed phases + sleeping wrapper coexist, sleeping is dropped."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-04-10T22:00:00Z"),
                end_time=_dt("2026-04-11T06:00:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.LIGHT,
                start_time=_dt("2026-04-10T22:10:00Z"),
                end_time=_dt("2026-04-10T23:00:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.DEEP,
                start_time=_dt("2026-04-10T23:00:00Z"),
                end_time=_dt("2026-04-11T01:00:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.REM,
                start_time=_dt("2026-04-11T01:00:00Z"),
                end_time=_dt("2026-04-11T02:00:00Z"),
            ),
        ]

        metrics, cleaned = _calculate_final_metrics(stages)

        # sleeping wrapper must NOT be counted
        assert metrics["sleeping_seconds"] == 0
        assert metrics["light_seconds"] == 50 * 60
        assert metrics["deep_seconds"] == 2 * 3600
        assert metrics["rem_seconds"] == 1 * 3600
        # Hypnogram should not contain sleeping
        stage_types = {s.stage for s in cleaned}
        assert SleepStageType.SLEEPING not in stage_types
        assert SleepStageType.IN_BED not in stage_types

    def test_detailed_plus_sleeping_plus_in_bed(self) -> None:
        """Full modern scenario: in_bed + sleeping wrapper + detailed phases."""
        stages = [
            SleepStateStage(
                stage=SleepStageType.IN_BED,
                start_time=_dt("2026-04-10T22:00:00Z"),
                end_time=_dt("2026-04-11T06:00:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.SLEEPING,
                start_time=_dt("2026-04-10T22:00:00Z"),
                end_time=_dt("2026-04-11T06:00:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.DEEP,
                start_time=_dt("2026-04-10T22:30:00Z"),
                end_time=_dt("2026-04-11T00:00:00Z"),
            ),
            SleepStateStage(
                stage=SleepStageType.LIGHT,
                start_time=_dt("2026-04-11T00:00:00Z"),
                end_time=_dt("2026-04-11T02:00:00Z"),
            ),
        ]

        metrics, cleaned = _calculate_final_metrics(stages)

        # Only detailed phases should be counted
        assert metrics["sleeping_seconds"] == 0
        assert metrics["deep_seconds"] == 1.5 * 3600
        assert metrics["light_seconds"] == 2 * 3600
        # in_bed still calculated from original intervals
        assert metrics["in_bed_seconds"] == 8 * 3600
        # Hypnogram: only deep + light
        stage_types = {s.stage for s in cleaned}
        assert stage_types == {SleepStageType.DEEP, SleepStageType.LIGHT}


class TestPersistSleep:
    """Tests for persist_sleep with different stage compositions."""

    @patch("app.services.apple.healthkit.sleep_service.event_record_service")
    @patch("app.services.apple.healthkit.sleep_service.delete_sleep_state")
    def test_persist_sleep_with_sleeping_stages(
        self,
        mock_delete_state: MagicMock,
        mock_event_service: MagicMock,
        db: Session,
    ) -> None:
        """Persist sleep with old-style 'sleeping' data should set correct totals."""
        user_id = str(uuid4())
        state_uuid = str(uuid4())

        state = SleepState(
            uuid=state_uuid,
            source_name="Apple Watch",
            device_model="Watch3,3",
            provider="apple",
            start_time=_dt("2026-03-15T23:00:00Z"),
            end_time=_dt("2026-03-16T01:30:00Z"),
            last_start_timestamp=_dt("2026-03-16T00:47:00Z"),
            last_end_timestamp=_dt("2026-03-16T01:30:00Z"),
            sleeping_seconds=8760.0,
            stages=[
                SleepStateStage(
                    stage=SleepStageType.SLEEPING,
                    start_time=_dt("2026-03-15T23:00:00Z"),
                    end_time=_dt("2026-03-15T23:50:00Z"),
                ),
                SleepStateStage(
                    stage=SleepStageType.SLEEPING,
                    start_time=_dt("2026-03-15T23:52:00Z"),
                    end_time=_dt("2026-03-16T00:45:00Z"),
                ),
                SleepStateStage(
                    stage=SleepStageType.SLEEPING,
                    start_time=_dt("2026-03-16T00:47:00Z"),
                    end_time=_dt("2026-03-16T01:30:00Z"),
                ),
            ],
        )

        persist_sleep(db, user_id, state, close=True)

        mock_event_service.create_or_merge_sleep.assert_called_once()
        call_args = mock_event_service.create_or_merge_sleep.call_args
        record: EventRecordCreate = call_args[0][2]
        detail: EventRecordDetailCreate = call_args[0][3]

        assert record.external_id == state_uuid
        assert detail.sleep_total_duration_minutes > 0
        assert detail.sleep_deep_minutes == 0
        assert detail.sleep_rem_minutes == 0
        assert detail.sleep_light_minutes == 0
        assert detail.sleep_stages is not None
        assert len(detail.sleep_stages) == 3
        assert all(s.stage == SleepStageType.SLEEPING for s in detail.sleep_stages)
        mock_delete_state.assert_called_once_with(user_id)

    @patch("app.services.apple.healthkit.sleep_service.event_record_service")
    @patch("app.services.apple.healthkit.sleep_service.delete_sleep_state")
    def test_persist_sleep_keeps_redis_when_not_closing(
        self,
        mock_delete_state: MagicMock,
        mock_event_service: MagicMock,
        db: Session,
    ) -> None:
        """close=False flushes to DB but leaves Redis state for further accumulation."""
        user_id = str(uuid4())
        state = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model="Watch7,1",
            provider="apple",
            start_time=_dt("2026-03-10T22:15:00Z"),
            end_time=_dt("2026-03-11T02:30:00Z"),
            last_start_timestamp=_dt("2026-03-11T01:25:00Z"),
            last_end_timestamp=_dt("2026-03-11T02:30:00Z"),
            light_seconds=2700.0,
            deep_seconds=9300.0,
            rem_seconds=2700.0,
            awake_seconds=600.0,
            stages=[
                SleepStateStage(
                    stage=SleepStageType.LIGHT,
                    start_time=_dt("2026-03-10T22:15:00Z"),
                    end_time=_dt("2026-03-10T23:00:00Z"),
                ),
                SleepStateStage(
                    stage=SleepStageType.DEEP,
                    start_time=_dt("2026-03-10T23:00:00Z"),
                    end_time=_dt("2026-03-11T00:30:00Z"),
                ),
            ],
        )

        persist_sleep(db, user_id, state, close=False)

        mock_event_service.create_or_merge_sleep.assert_called_once()
        mock_delete_state.assert_not_called()

    @patch("app.services.apple.healthkit.sleep_service.event_record_service")
    @patch("app.services.apple.healthkit.sleep_service.delete_sleep_state")
    def test_persist_sleep_with_detailed_stages(
        self,
        mock_delete_state: MagicMock,
        mock_event_service: MagicMock,
        db: Session,
    ) -> None:
        """Persist sleep with detailed stages should set deep/rem/light correctly."""
        user_id = str(uuid4())

        state = SleepState(
            uuid=str(uuid4()),
            source_name="Apple Watch",
            device_model="Watch7,1",
            provider="apple",
            start_time=_dt("2026-03-10T22:15:00Z"),
            end_time=_dt("2026-03-11T02:30:00Z"),
            last_start_timestamp=_dt("2026-03-11T01:25:00Z"),
            last_end_timestamp=_dt("2026-03-11T02:30:00Z"),
            light_seconds=2700.0,  # 45 min
            deep_seconds=9300.0,  # 155 min
            rem_seconds=2700.0,  # 45 min
            awake_seconds=600.0,  # 10 min
            stages=[
                SleepStateStage(
                    stage=SleepStageType.LIGHT,
                    start_time=_dt("2026-03-10T22:15:00Z"),
                    end_time=_dt("2026-03-10T23:00:00Z"),
                ),
                SleepStateStage(
                    stage=SleepStageType.DEEP,
                    start_time=_dt("2026-03-10T23:00:00Z"),
                    end_time=_dt("2026-03-11T00:30:00Z"),
                ),
                SleepStateStage(
                    stage=SleepStageType.REM,
                    start_time=_dt("2026-03-11T00:30:00Z"),
                    end_time=_dt("2026-03-11T01:15:00Z"),
                ),
                SleepStateStage(
                    stage=SleepStageType.AWAKE,
                    start_time=_dt("2026-03-11T01:15:00Z"),
                    end_time=_dt("2026-03-11T01:25:00Z"),
                ),
                SleepStateStage(
                    stage=SleepStageType.DEEP,
                    start_time=_dt("2026-03-11T01:25:00Z"),
                    end_time=_dt("2026-03-11T02:30:00Z"),
                ),
            ],
        )

        persist_sleep(db, user_id, state, close=True)

        detail: EventRecordDetailCreate = mock_event_service.create_or_merge_sleep.call_args[0][3]

        assert detail.sleep_deep_minutes == 155
        assert detail.sleep_light_minutes == 45
        assert detail.sleep_rem_minutes == 45
        assert detail.sleep_awake_minutes == 10
        assert detail.sleep_total_duration_minutes == 245  # light+deep+rem (no sleeping)


class TestHandleSleepDataIntegration:
    """Integration tests for handle_sleep_data with real payload structures."""

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
    @patch("app.services.apple.healthkit.sleep_service.event_record_service")
    @patch("app.services.apple.healthkit.sleep_service.get_redis_client")
    def test_handle_real_payload_sleeping_stages(
        self,
        mock_redis_func: MagicMock,
        mock_event_service: MagicMock,
        mock_finalize: MagicMock,
        db: Session,
    ) -> None:
        """Process a synthetic payload with in_bed + sleeping stages.

        Modeled after older Apple Watch pattern:
        - 3 sleeping segments (Watch3,3)
        - 1 in_bed segment (iPhone15,2)
        All within gap threshold → single session.
        Historical end_time → flush with close=True.
        """
        user_id = str(uuid4())

        mock_redis = MagicMock()
        mock_redis.get.return_value = None  # No existing state
        mock_redis_func.return_value = mock_redis

        request = SyncRequest.model_validate(OLD_WATCH_PAYLOAD)

        handle_sleep_data(db, request, user_id)

        # Sleep state should be saved to Redis before flush
        assert mock_redis.set.called

        # Eager flush to Postgres via the shared create_or_merge_sleep path
        mock_event_service.create_or_merge_sleep.assert_called_once()

        # The finalize task should be dispatched for housekeeping
        mock_finalize.delay.assert_called_once()

        # Verify saved state: grab the last set() call's value
        last_set_call = mock_redis.set.call_args_list[-1]
        state_json = last_set_call[0][1]  # second positional arg
        state = SleepState.model_validate_json(state_json)

        # sleeping_seconds should be populated, NOT deep_seconds
        assert state.sleeping_seconds > 0
        assert state.deep_seconds == 0
        assert state.light_seconds == 0
        assert state.rem_seconds == 0

        # All entries are within the gap threshold so they merge into one session.
        sleeping_stages = [s for s in state.stages if s.stage == SleepStageType.SLEEPING]
        in_bed_stages = [s for s in state.stages if s.stage == SleepStageType.IN_BED]
        assert len(sleeping_stages) >= 1
        assert len(in_bed_stages) == 1

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
    @patch("app.services.apple.healthkit.sleep_service.event_record_service")
    @patch("app.services.apple.healthkit.sleep_service.get_redis_client")
    def test_handle_detailed_stages_payload(
        self,
        mock_redis_func: MagicMock,
        mock_event_service: MagicMock,
        mock_finalize: MagicMock,
        db: Session,
    ) -> None:
        """Process a modern payload with detailed sleep stages."""
        user_id = str(uuid4())

        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis_func.return_value = mock_redis

        request = SyncRequest.model_validate(DETAILED_STAGES_PAYLOAD)

        handle_sleep_data(db, request, user_id)

        # Eager flush on every batch
        mock_event_service.create_or_merge_sleep.assert_called_once()

        # Verify saved state
        last_set_call = mock_redis.set.call_args_list[-1]
        state = SleepState.model_validate_json(last_set_call[0][1])

        assert state.sleeping_seconds == 0
        assert state.deep_seconds > 0
        assert state.light_seconds > 0
        assert state.rem_seconds > 0
        assert state.awake_seconds > 0

        # Verify stage types
        stage_types = {s.stage for s in state.stages}
        assert SleepStageType.DEEP in stage_types
        assert SleepStageType.LIGHT in stage_types
        assert SleepStageType.REM in stage_types
        assert SleepStageType.AWAKE in stage_types

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
    @patch("app.services.apple.healthkit.sleep_service.persist_sleep")
    @patch("app.services.apple.healthkit.sleep_service.get_redis_client")
    def test_fresh_night_batch_flushes_without_closing_redis(
        self,
        mock_redis_func: MagicMock,
        mock_persist: MagicMock,
        mock_finalize: MagicMock,
        db: Session,
    ) -> None:
        """A just-ended night (within quiet gap) flushes with close=False."""
        user_id = str(uuid4())
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=1)
        end = now - timedelta(minutes=10)

        fresh_payload = {
            "provider": "apple",
            "sdkVersion": "1.0.0",
            "syncTimestamp": now.isoformat().replace("+00:00", "Z"),
            "data": {
                "records": [],
                "workouts": [],
                "sleep": [
                    {
                        "id": "F001",
                        "stage": "light",
                        "startDate": start.isoformat().replace("+00:00", "Z"),
                        "endDate": end.isoformat().replace("+00:00", "Z"),
                        "source": {"device_type": "watch", "device_model": "Watch7,1"},
                    },
                ],
            },
        }

        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis_func.return_value = mock_redis

        handle_sleep_data(db, SyncRequest.model_validate(fresh_payload), user_id)

        mock_persist.assert_called_once()
        assert mock_persist.call_args.kwargs["close"] is False
        # Redis state must remain (set was called; delete not part of persist mock)
        assert mock_redis.set.called
        mock_finalize.delay.assert_called_once()


class TestSDKSyncEndpointSleep:
    """Test the /sdk/users/{user_id}/sync endpoint with sleep payloads."""

    def test_sync_endpoint_accepts_sleeping_stage(
        self,
        client: MagicMock,
        db: Session,
    ) -> None:
        """Endpoint should validate payload with 'sleeping' stage (older Apple Watch)."""
        from app.services.sdk_token_service import create_sdk_user_token

        user_id = str(uuid4())
        token = create_sdk_user_token("test_app", user_id)

        with patch("app.api.routes.v1.sdk_sync.process_sdk_upload") as mock_task:
            mock_task.delay.return_value = None

            response = client.post(
                "/api/v1/sdk/users/" + user_id + "/sync/",
                headers={"Authorization": f"Bearer {token}"},
                json=OLD_WATCH_PAYLOAD,
            )

        assert response.status_code == 202
        data = response.json()
        assert data["status_code"] == 202
        mock_task.delay.assert_called_once()

    def test_sync_endpoint_accepts_detailed_stages(
        self,
        client: MagicMock,
        db: Session,
    ) -> None:
        """Endpoint should validate payload with detailed sleep stages."""
        from app.services.sdk_token_service import create_sdk_user_token

        user_id = str(uuid4())
        token = create_sdk_user_token("test_app", user_id)

        with patch("app.api.routes.v1.sdk_sync.process_sdk_upload") as mock_task:
            mock_task.delay.return_value = None

            response = client.post(
                "/api/v1/sdk/users/" + user_id + "/sync/",
                headers={"Authorization": f"Bearer {token}"},
                json=DETAILED_STAGES_PAYLOAD,
            )

        assert response.status_code == 202
        data = response.json()
        assert data["status_code"] == 202


class TestNoIntermediateRedisSaves:
    """Regression test: Redis state must only be saved once per batch, not per stage.

    Previously, save_sleep_state was called inside the per-stage loop, exposing
    partially-accumulated intermediate states to the concurrent finalize_stale_sleeps
    task.  That task could read a partial state, decide it was stale, and finalize it
    — producing a duplicate (subset) sleep record.  Moving the save outside the loop
    prevents this race condition.
    """

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
    @patch("app.services.apple.healthkit.sleep_service.event_record_service")
    @patch("app.services.apple.healthkit.sleep_service.get_redis_client")
    def test_redis_set_called_once_per_batch(
        self,
        mock_redis_func: MagicMock,
        mock_event_service: MagicMock,
        mock_finalize: MagicMock,
        db: Session,
    ) -> None:
        """Redis .set() should be called exactly once after processing all stages."""
        user_id = str(uuid4())

        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis_func.return_value = mock_redis

        request = SyncRequest.model_validate(DETAILED_STAGES_PAYLOAD)

        handle_sleep_data(db, request, user_id)

        # Count how many times set() was called (each call = one Redis state save).
        # With the fix, this should be exactly 1 — after the loop finishes.
        # Before the fix, it was called once per stage (9 times for this payload).
        set_calls = mock_redis.set.call_args_list
        assert len(set_calls) == 1, (
            f"Expected exactly 1 Redis save per batch, got {len(set_calls)}. "
            "Intermediate saves expose partial state to finalize_stale_sleeps."
        )

        # Verify the single saved state contains ALL stages from the payload
        state = SleepState.model_validate_json(set_calls[0][0][1])
        # The payload has 9 stages; in_bed is included but counted under in_bed_seconds
        assert len(state.stages) >= 8  # at least the 8 watch stages + 1 in_bed


class TestHistoricalBulkUploadMerging:
    """Regression tests: consecutive payloads for the same night must produce
    a single merged DB record rather than one record per payload.

    Root cause: Apple sends one night's sleep as many small consecutive payloads
    (each ending where the next begins).  When uploaded hours after recording the
    quiet-gap check fires on every payload (now - end_time >> 2 h),
    immediately closing each Redis session.

    The fix: persist_sleep() uses create_or_merge_sleep which merges adjacent
    sessions (or updates in place for the same external_id). Combined with the
    per-user Redis lock that serializes concurrent tasks, this guarantees one
    DB record per night regardless of how many payloads Apple sends.
    """

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
    @patch("app.services.apple.healthkit.sleep_service.event_record_service")
    @patch("app.services.apple.healthkit.sleep_service.get_redis_client")
    def test_second_payload_merges_via_create_or_merge_sleep(
        self,
        mock_redis_func: MagicMock,
        mock_event_service: MagicMock,
        mock_finalize: MagicMock,
        db: Session,
    ) -> None:
        """When payload B arrives after payload A has already been written to the
        DB, persist_sleep should call create_or_merge_sleep with B's stages so the
        shared merge path can extend the adjacent record.

        Payload B: 01:00–06:00 (rem + light), chains directly onto A (23:00–01:00).
        """

        user_id = str(uuid4())

        payload_b = {
            "provider": "apple",
            "sdkVersion": "1.0.0",
            "syncTimestamp": "2026-03-23T08:00:01Z",
            "data": {
                "records": [],
                "workouts": [],
                "sleep": [
                    {
                        "id": "B003",
                        "stage": "rem",
                        "startDate": "2026-03-23T01:00:00Z",
                        "endDate": "2026-03-23T02:30:00Z",
                        "source": {"device_type": "watch", "device_model": "Watch7,9"},
                    },
                    {
                        "id": "B004",
                        "stage": "light",
                        "startDate": "2026-03-23T02:30:00Z",
                        "endDate": "2026-03-23T06:00:00Z",
                        "source": {"device_type": "watch", "device_model": "Watch7,9"},
                    },
                ],
            },
        }

        mock_redis = MagicMock()
        mock_redis.get.return_value = None  # No active Redis state for this user
        mock_redis_func.return_value = mock_redis

        handle_sleep_data(db, SyncRequest.model_validate(payload_b), user_id)

        mock_event_service.create_or_merge_sleep.assert_called_once()
        call_args = mock_event_service.create_or_merge_sleep.call_args
        record: EventRecordCreate = call_args[0][2]
        detail: EventRecordDetailCreate = call_args[0][3]

        # Payload B window
        assert record.start_datetime == _dt("2026-03-23T01:00:00Z")
        assert record.end_datetime == _dt("2026-03-23T06:00:00Z")
        assert record.external_id == "B003"  # first sample id becomes state.uuid

        assert detail.sleep_stages is not None
        stage_types = {s.stage for s in detail.sleep_stages}
        assert SleepStageType.LIGHT in stage_types
        assert SleepStageType.REM in stage_types

    @patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps")
    @patch("app.services.apple.healthkit.sleep_service.event_record_service")
    @patch("app.services.apple.healthkit.sleep_service.get_redis_client")
    def test_follow_up_batch_reuses_same_external_id(
        self,
        mock_redis_func: MagicMock,
        mock_event_service: MagicMock,
        mock_finalize: MagicMock,
        db: Session,
    ) -> None:
        """A second batch for the same Redis session re-flushes with the same
        external_id so create_or_merge_sleep replaces detail in place (no double-count).
        """
        user_id = str(uuid4())
        session_uuid = str(uuid4())
        now = datetime.now(timezone.utc)

        # Existing Redis state from an earlier mid-night sync
        existing_state = SleepState(
            uuid=session_uuid,
            source_name="Apple Watch",
            device_model="Watch7,1",
            provider="apple",
            start_time=now - timedelta(hours=3),
            end_time=now - timedelta(hours=1),
            last_start_timestamp=now - timedelta(hours=3),
            last_end_timestamp=now - timedelta(hours=1),
            light_seconds=3600.0,
            stages=[
                SleepStateStage(
                    stage=SleepStageType.LIGHT,
                    start_time=now - timedelta(hours=3),
                    end_time=now - timedelta(hours=1),
                ),
            ],
        )

        mock_redis = MagicMock()
        mock_redis.get.return_value = existing_state.model_dump_json()
        mock_redis_func.return_value = mock_redis

        follow_up = {
            "provider": "apple",
            "sdkVersion": "1.0.0",
            "syncTimestamp": now.isoformat().replace("+00:00", "Z"),
            "data": {
                "records": [],
                "workouts": [],
                "sleep": [
                    {
                        "id": "NEW1",
                        "stage": "deep",
                        "startDate": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                        "endDate": (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
                        "source": {"device_type": "watch", "device_model": "Watch7,1"},
                    },
                ],
            },
        }

        handle_sleep_data(db, SyncRequest.model_validate(follow_up), user_id)

        mock_event_service.create_or_merge_sleep.assert_called_once()
        record: EventRecordCreate = mock_event_service.create_or_merge_sleep.call_args[0][2]
        detail: EventRecordDetailCreate = mock_event_service.create_or_merge_sleep.call_args[0][3]

        # Same Redis session uuid → same external_id for in-place update
        assert record.external_id == session_uuid
        # Full accumulated stages (old light + new deep), not just the new batch
        assert detail.sleep_stages is not None
        stage_types = {s.stage for s in detail.sleep_stages}
        assert SleepStageType.LIGHT in stage_types
        assert SleepStageType.DEEP in stage_types
        # Totals reflect union, not a double-counted sum of two separate flushes
        assert detail.sleep_light_minutes == 120
        assert detail.sleep_deep_minutes == 50
