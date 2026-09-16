"""Tests for OuraWorkouts."""

from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.constants.workout_types.oura import get_unified_workout_type
from app.models import DataSource, EventRecord
from app.schemas.enums import EntrySource, WorkoutIntensity, WorkoutType
from app.schemas.providers.oura import OuraWorkoutJSON
from app.services.providers.oura.strategy import OuraStrategy
from app.services.providers.oura.workouts import OuraWorkouts
from tests.factories import UserFactory


class TestOuraWorkoutTypeMapping:
    """Test workout type mapping from Oura activity strings."""

    def test_running(self) -> None:
        assert get_unified_workout_type("running") == WorkoutType.RUNNING

    def test_cycling(self) -> None:
        assert get_unified_workout_type("cycling") == WorkoutType.CYCLING

    def test_swimming(self) -> None:
        assert get_unified_workout_type("swimming") == WorkoutType.SWIMMING

    def test_yoga(self) -> None:
        assert get_unified_workout_type("yoga") == WorkoutType.YOGA

    def test_strength_training(self) -> None:
        assert get_unified_workout_type("strength_training") == WorkoutType.STRENGTH_TRAINING

    def test_hiking(self) -> None:
        assert get_unified_workout_type("hiking") == WorkoutType.HIKING

    def test_unknown_returns_other(self) -> None:
        assert get_unified_workout_type("some_unknown_activity") == WorkoutType.OTHER

    def test_none_returns_other(self) -> None:
        assert get_unified_workout_type(None) == WorkoutType.OTHER

    def test_case_insensitive(self) -> None:
        assert get_unified_workout_type("RUNNING") == WorkoutType.RUNNING
        assert get_unified_workout_type("Running") == WorkoutType.RUNNING


class TestOuraWorkoutsNormalization:
    """Test workout normalization."""

    @pytest.fixture
    def workouts(self) -> OuraWorkouts:
        strategy = OuraStrategy()
        return strategy.workouts

    @pytest.fixture
    def sample_oura_workout(self) -> OuraWorkoutJSON:
        return OuraWorkoutJSON(
            id="oura-workout-abc123",
            activity="running",
            calories=350.5,
            day="2024-01-15",
            distance=5000.0,
            end_datetime="2024-01-15T09:00:00+00:00",
            intensity="moderate",
            label="Morning Run",
            source="autodetected",
            start_datetime="2024-01-15T08:00:00+00:00",
        )

    def test_normalize_workout_creates_records(
        self, workouts: OuraWorkouts, sample_oura_workout: OuraWorkoutJSON
    ) -> None:
        user_id = uuid4()
        record, detail = workouts._normalize_workout(sample_oura_workout, user_id)

        assert record.category == "workout"
        assert record.type == WorkoutType.RUNNING.value
        assert record.source_name == "Oura"
        assert record.source == "oura"
        assert record.user_id == user_id
        assert record.external_id == "oura-workout-abc123"
        assert record.duration_seconds == 3600

    def test_normalize_workout_metrics(self, workouts: OuraWorkouts, sample_oura_workout: OuraWorkoutJSON) -> None:
        user_id = uuid4()
        record, detail = workouts._normalize_workout(sample_oura_workout, user_id)

        assert detail.energy_burned is not None
        assert float(detail.energy_burned) == pytest.approx(350.5)
        assert detail.distance is not None
        assert float(detail.distance) == pytest.approx(5000.0)

    def test_normalize_workout_provenance(self, workouts: OuraWorkouts, sample_oura_workout: OuraWorkoutJSON) -> None:
        """Oura's per-workout `source` must not be confused with EventRecordCreate.source,
        which stays the provider identifier."""
        user_id = uuid4()
        record, detail = workouts._normalize_workout(sample_oura_workout, user_id)

        assert record.source == "oura"
        assert detail.entry_source == EntrySource.AUTOMATIC
        assert detail.intensity == WorkoutIntensity.MODERATE
        assert detail.label == "Morning Run"

    def test_normalize_workout_no_activity(self, workouts: OuraWorkouts) -> None:
        workout = OuraWorkoutJSON(
            id="oura-workout-no-activity",
            start_datetime="2024-01-15T08:00:00+00:00",
            end_datetime="2024-01-15T08:30:00+00:00",
        )
        user_id = uuid4()
        record, detail = workouts._normalize_workout(workout, user_id)

        assert record.type == WorkoutType.OTHER.value
        assert record.duration_seconds == 1800
        assert detail.entry_source is None
        assert detail.intensity is None
        assert detail.label is None

    def test_build_bundles(self, workouts: OuraWorkouts, sample_oura_workout: OuraWorkoutJSON) -> None:
        user_id = uuid4()
        bundles = list(workouts._build_bundles([sample_oura_workout], user_id))
        assert len(bundles) == 1

        record, detail = bundles[0]
        assert record.category == "workout"
        assert detail.record_id == record.id


OURA_WORKOUT_ID = "oura-workout-abc123"


def _oura_workout_payload(activity: str, start: str, end: str) -> dict[str, Any]:
    """Oura ``GET /v2/usercollection/workout/{id}`` response for a single workout."""
    return {
        "id": OURA_WORKOUT_ID,
        "activity": activity,
        "calories": 350.5,
        "day": "2024-01-15",
        "distance": 5000.0,
        "intensity": "moderate",
        "start_datetime": start,
        "end_datetime": end,
    }


def _user_records(db: Session, user_id: UUID) -> list[EventRecord]:
    return (
        db.query(EventRecord)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .filter(DataSource.user_id == user_id)
        .all()
    )


class TestOuraSaveById:
    """``save_by_id`` serves the Oura ``workout`` webhook, including update events."""

    @pytest.fixture
    def workouts(self) -> OuraWorkouts:
        return OuraStrategy().workouts

    def test_update_replaces_existing_record(self, db: Session, workouts: OuraWorkouts) -> None:
        user = UserFactory()
        original = _oura_workout_payload("running", "2024-01-15T08:00:00+00:00", "2024-01-15T09:00:00+00:00")
        edited = _oura_workout_payload("walking", "2024-01-15T08:00:00+00:00", "2024-01-15T08:30:00+00:00")

        with patch.object(workouts, "get_workout_detail_from_api", return_value=original):
            assert workouts.save_by_id(db, user.id, OURA_WORKOUT_ID) == 1
        with patch.object(workouts, "get_workout_detail_from_api", return_value=edited):
            assert workouts.save_by_id(db, user.id, OURA_WORKOUT_ID) == 1

        records = _user_records(db, user.id)
        assert len(records) == 1
        assert records[0].type == WorkoutType.WALKING.value
        assert records[0].duration_seconds == 1800

    def test_insert_failure_keeps_original_record(self, db: Session, workouts: OuraWorkouts) -> None:
        user = UserFactory()
        original = _oura_workout_payload("running", "2024-01-15T08:00:00+00:00", "2024-01-15T09:00:00+00:00")
        edited = _oura_workout_payload("walking", "2024-01-15T08:00:00+00:00", "2024-01-15T08:30:00+00:00")

        with patch.object(workouts, "get_workout_detail_from_api", return_value=original):
            assert workouts.save_by_id(db, user.id, OURA_WORKOUT_ID) == 1
        original_record_id = _user_records(db, user.id)[0].id

        with (
            patch.object(workouts, "get_workout_detail_from_api", return_value=edited),
            patch.object(workouts.workout_repo, "create_and_flush", side_effect=RuntimeError("insert exploded")),
            pytest.raises(RuntimeError),
        ):
            workouts.save_by_id(db, user.id, OURA_WORKOUT_ID)

        records = _user_records(db, user.id)
        assert len(records) == 1
        assert records[0].id == original_record_id
        assert records[0].type == WorkoutType.RUNNING.value
        assert records[0].duration_seconds == 3600
