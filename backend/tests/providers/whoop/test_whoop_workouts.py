"""Tests for Whoop single-workout ingestion (the ``workout.updated`` webhook path)."""

from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, WorkoutDetails
from app.schemas.enums import WorkoutType
from app.schemas.providers.whoop import WhoopWebhookNotificationType
from app.services.providers.whoop.strategy import WhoopStrategy
from app.services.providers.whoop.workouts import WhoopWorkouts
from tests.factories import UserFactory

WHOOP_WORKOUT_ID = "ecfc6a15-4661-442f-a9a4-f160dd7afae8"
OTHER_WHOOP_WORKOUT_ID = "1d0f4b3a-9c2e-4f77-8a51-6b0e2c7d9f10"


def _whoop_workout_payload(
    sport_name: str,
    start: str,
    end: str,
    workout_id: str = WHOOP_WORKOUT_ID,
) -> dict[str, Any]:
    """Whoop ``GET /v2/activity/workout/{id}`` response for a single workout."""
    return {
        "id": workout_id,
        "user_id": 10129,
        "created_at": "2026-06-30T08:12:00.000Z",
        "updated_at": "2026-06-30T09:20:00.000Z",
        "start": start,
        "end": end,
        "timezone_offset": "+01:00",
        "sport_name": sport_name,
        "score_state": "SCORED",
        "score": {
            "strain": 5.2,
            "average_heart_rate": 92,
            "max_heart_rate": 118,
            "kilojoule": 890.0,
            "percent_recorded": 100,
            "distance_meter": 2600.0,
            "altitude_gain_meter": 12.0,
        },
    }


def _user_records(db: Session, user_id: UUID) -> list[EventRecord]:
    return (
        db.query(EventRecord)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .filter(DataSource.user_id == user_id)
        .all()
    )


def _user_workout_details(db: Session, user_id: UUID) -> list[WorkoutDetails]:
    return (
        db.query(WorkoutDetails)
        .join(EventRecord, WorkoutDetails.record_id == EventRecord.id)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .filter(DataSource.user_id == user_id)
        .all()
    )


def _orphaned_workout_details(db: Session) -> int:
    return (
        db.query(WorkoutDetails)
        .outerjoin(EventRecord, WorkoutDetails.record_id == EventRecord.id)
        .filter(EventRecord.id.is_(None))
        .count()
    )


class TestWhoopLoadSingleWorkout:
    """``load_single_workout`` must replace, not append, an already-stored workout.

    Whoop sends ``workout.updated`` when the user edits an activity in the app.
    The edited workout keeps its ``external_id`` but its sport and time window
    change, so it no longer collides with the ``(data_source_id, start, end)``
    unique index — before the fix each edit left a new version of the same
    workout in ``event_record`` and API consumers saw the workout twice.
    """

    @pytest.fixture
    def workouts(self) -> WhoopWorkouts:
        return WhoopStrategy().workouts

    def test_update_replaces_existing_record(self, db: Session, workouts: WhoopWorkouts) -> None:
        user = UserFactory()

        original = _whoop_workout_payload(
            "Activity",
            "2026-06-30T08:12:00.000Z",
            "2026-06-30T09:04:00.000Z",  # 52 minutes
        )
        edited = _whoop_workout_payload(
            "Dog Walking",
            "2026-06-30T08:12:00.000Z",
            "2026-06-30T08:46:00.000Z",  # trimmed to 34 minutes
        )

        with patch.object(workouts, "get_workout_detail_from_api", return_value=original):
            assert workouts.load_single_workout(db, user.id, WHOOP_WORKOUT_ID) == 1

        records = _user_records(db, user.id)
        assert len(records) == 1
        assert records[0].type == WorkoutType.GENERIC.value

        with patch.object(workouts, "get_workout_detail_from_api", return_value=edited):
            assert workouts.load_single_workout(db, user.id, WHOOP_WORKOUT_ID) == 1

        records = _user_records(db, user.id)
        assert len(records) == 1
        assert records[0].external_id == WHOOP_WORKOUT_ID
        assert records[0].type == WorkoutType.WALKING.value
        assert records[0].duration_seconds == 34 * 60

    def test_update_leaves_no_orphaned_detail_rows(self, db: Session, workouts: WhoopWorkouts) -> None:
        user = UserFactory()

        original = _whoop_workout_payload(
            "Activity",
            "2026-06-30T08:12:00.000Z",
            "2026-06-30T09:04:00.000Z",
        )
        edited = _whoop_workout_payload(
            "Dog Walking",
            "2026-06-30T08:12:00.000Z",
            "2026-06-30T08:46:00.000Z",
        )

        with patch.object(workouts, "get_workout_detail_from_api", return_value=original):
            workouts.load_single_workout(db, user.id, WHOOP_WORKOUT_ID)
        with patch.object(workouts, "get_workout_detail_from_api", return_value=edited):
            workouts.load_single_workout(db, user.id, WHOOP_WORKOUT_ID)

        records = _user_records(db, user.id)
        details = _user_workout_details(db, user.id)
        assert len(records) == 1
        assert len(details) == 1
        # The surviving detail row belongs to the surviving record, and deleting
        # the stale record took its detail row with it (FK ON DELETE CASCADE).
        assert details[0].record_id == records[0].id
        assert details[0].moving_time_seconds == 34 * 60
        assert _orphaned_workout_details(db) == 0

    def test_redelivered_identical_workout_stays_single(self, db: Session, workouts: WhoopWorkouts) -> None:
        user = UserFactory()
        payload = _whoop_workout_payload(
            "Dog Walking",
            "2026-06-30T08:12:00.000Z",
            "2026-06-30T08:46:00.000Z",
        )

        with patch.object(workouts, "get_workout_detail_from_api", return_value=payload):
            assert workouts.load_single_workout(db, user.id, WHOOP_WORKOUT_ID) == 1
            assert workouts.load_single_workout(db, user.id, WHOOP_WORKOUT_ID) == 1

        assert len(_user_records(db, user.id)) == 1
        assert len(_user_workout_details(db, user.id)) == 1
        assert _orphaned_workout_details(db) == 0

    def test_insert_failure_keeps_original_record(self, db: Session, workouts: WhoopWorkouts) -> None:
        """The stale record must only disappear once the replacement is persisted."""
        user = UserFactory()

        original = _whoop_workout_payload(
            "Activity",
            "2026-06-30T08:12:00.000Z",
            "2026-06-30T09:04:00.000Z",
        )
        edited = _whoop_workout_payload(
            "Dog Walking",
            "2026-06-30T08:12:00.000Z",
            "2026-06-30T08:46:00.000Z",
        )

        with patch.object(workouts, "get_workout_detail_from_api", return_value=original):
            assert workouts.load_single_workout(db, user.id, WHOOP_WORKOUT_ID) == 1
        original_record_id = _user_records(db, user.id)[0].id

        with (
            patch.object(workouts, "get_workout_detail_from_api", return_value=edited),
            patch.object(workouts.workout_repo, "create_and_flush", side_effect=RuntimeError("insert exploded")),
            pytest.raises(RuntimeError),
        ):
            workouts.load_single_workout(db, user.id, WHOOP_WORKOUT_ID)

        # The delete shared the failed insert's transaction, so it rolled back too.
        records = _user_records(db, user.id)
        assert len(records) == 1
        assert records[0].id == original_record_id
        assert records[0].type == WorkoutType.GENERIC.value
        assert records[0].duration_seconds == 52 * 60

        details = _user_workout_details(db, user.id)
        assert len(details) == 1
        assert details[0].record_id == original_record_id

    def test_time_window_taken_by_another_workout_keeps_both(self, db: Session, workouts: WhoopWorkouts) -> None:
        """An insert absorbed by an unrelated workout must not consume the edited one.

        Two distinct workouts can end up sharing a time window, which the
        ``(data_source_id, start, end)`` unique index rejects. Rather than grafting
        this workout's details onto the unrelated record, the replace is abandoned and
        both stored workouts are left as they were.
        """
        user = UserFactory()

        original = _whoop_workout_payload(
            "Activity",
            "2026-06-30T08:12:00.000Z",
            "2026-06-30T09:04:00.000Z",
        )
        unrelated = _whoop_workout_payload(
            "Cycling",
            "2026-06-30T08:12:00.000Z",
            "2026-06-30T08:46:00.000Z",
            workout_id=OTHER_WHOOP_WORKOUT_ID,
        )
        # The edit moves the workout onto the slot the unrelated workout already holds.
        edited = _whoop_workout_payload(
            "Dog Walking",
            "2026-06-30T08:12:00.000Z",
            "2026-06-30T08:46:00.000Z",
        )

        with patch.object(workouts, "get_workout_detail_from_api", return_value=original):
            assert workouts.load_single_workout(db, user.id, WHOOP_WORKOUT_ID) == 1
        with patch.object(workouts, "get_workout_detail_from_api", return_value=unrelated):
            assert workouts.load_single_workout(db, user.id, OTHER_WHOOP_WORKOUT_ID) == 1

        with patch.object(workouts, "get_workout_detail_from_api", return_value=edited):
            assert workouts.load_single_workout(db, user.id, WHOOP_WORKOUT_ID) == 0

        records = {r.external_id: r for r in _user_records(db, user.id)}
        assert set(records) == {WHOOP_WORKOUT_ID, OTHER_WHOOP_WORKOUT_ID}
        assert records[WHOOP_WORKOUT_ID].duration_seconds == 52 * 60
        assert records[OTHER_WHOOP_WORKOUT_ID].type == WorkoutType.CYCLING.value
        assert len(_user_workout_details(db, user.id)) == 2
        assert _orphaned_workout_details(db) == 0

    def test_workout_updated_webhook_replaces_existing_record(self, db: Session) -> None:
        """End-to-end through the webhook handler's update branch."""
        strategy = WhoopStrategy()
        user = UserFactory()

        original = _whoop_workout_payload(
            "Activity",
            "2026-06-30T08:12:00.000Z",
            "2026-06-30T09:04:00.000Z",
        )
        edited = _whoop_workout_payload(
            "Dog Walking",
            "2026-06-30T08:12:00.000Z",
            "2026-06-30T08:46:00.000Z",
        )

        with patch.object(strategy.workouts, "get_workout_detail_from_api", return_value=original):
            strategy.webhooks._handle_updated(
                db, WhoopWebhookNotificationType.WORKOUT_UPDATED, user.id, WHOOP_WORKOUT_ID
            )
        with patch.object(strategy.workouts, "get_workout_detail_from_api", return_value=edited):
            result = strategy.webhooks._handle_updated(
                db, WhoopWebhookNotificationType.WORKOUT_UPDATED, user.id, WHOOP_WORKOUT_ID
            )

        assert result["records_saved"] == 1
        records = _user_records(db, user.id)
        assert len(records) == 1
        assert records[0].type == WorkoutType.WALKING.value
        assert _orphaned_workout_details(db) == 0
