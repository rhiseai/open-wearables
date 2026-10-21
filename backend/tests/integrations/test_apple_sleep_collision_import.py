"""The reported sleep collision must not abort the rest of an Apple SDK import."""

import json
import logging
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import EventRecord
from app.services.event_record_service import event_record_service
from app.services.sdk.import_service import ImportService
from tests.factories import (
    DataSourceFactory,
    EventRecordFactory,
    SleepDetailsFactory,
    UserConnectionFactory,
    UserFactory,
)

_START = datetime.fromisoformat("2026-09-29T21:31:14+00:00")
_END = datetime.fromisoformat("2026-09-30T05:38:00+00:00")


def _seed_collision() -> tuple:
    user = UserFactory()
    connection = UserConnectionFactory(user=user, provider="apple", access_token=None, refresh_token=None)
    source = DataSourceFactory(user=user, source="Apple Watch", device_model="Watch7,1")
    own = None
    for external_id, end in (
        ("own-sample", datetime.fromisoformat("2026-09-30T02:00:00+00:00")),
        ("other-sample", _END),
    ):
        minutes = int((end - _START).total_seconds() // 60)
        record = EventRecordFactory(
            data_source=source,
            external_id=external_id,
            category="sleep",
            type_="sleep_session",
            start_datetime=_START,
            end_datetime=end,
            duration_seconds=int((end - _START).total_seconds()),
            zone_offset="+00:00",
        )
        SleepDetailsFactory(
            event_record=record,
            sleep_total_duration_minutes=minutes,
            sleep_time_in_bed_minutes=minutes,
            sleep_deep_minutes=0,
            sleep_light_minutes=0,
            sleep_rem_minutes=0,
            sleep_awake_minutes=0,
            sleep_efficiency_score=100,
            is_nap=False,
            sleep_stages=[{"stage": "sleeping", "start_time": _START.isoformat(), "end_time": end.isoformat()}],
        )
        if external_id == "own-sample":
            own = record
    return user, connection, own


def _payload() -> str:
    source = {"name": "Apple Watch", "productType": "Watch7,1", "deviceType": "watch"}
    return json.dumps(
        {
            "provider": "apple",
            "sdkVersion": "0.15.0",
            "syncTimestamp": "2026-09-30T10:00:00Z",
            "data": {
                "sleep": [
                    {
                        "id": "own-sample",
                        "stage": "sleeping",
                        "startDate": _START.isoformat(),
                        "endDate": _END.isoformat(),
                        "zoneOffset": "+00:00",
                        "source": source,
                    }
                ],
                "records": [
                    {
                        "id": "flow-sample",
                        "type": "HKCategoryTypeIdentifierMenstrualFlow",
                        "value": 3,
                        "unit": "count",
                        "startDate": "2026-09-30T12:00:00Z",
                        "endDate": "2026-09-30T12:00:00Z",
                        "source": source,
                    }
                ],
            },
        }
    )


def test_collision_import_saves_sleep_menstrual_events_and_sync_marker(db: Session) -> None:
    user, connection, own = _seed_collision()
    own_id = own.id
    importer = ImportService(log=logging.getLogger("test.sleep-collision"))
    with patch("app.services.sdk.sleep_service.celery_app"):
        for _ in range(2):
            result = importer.import_data_from_request(db, _payload(), "application/json", str(user.id))
            assert result.status_code == 200, result.response
            assert result.sleep_saved == 1
    rows = db.query(EventRecord).filter_by(category="sleep").all()
    assert len(rows) == 1
    assert rows[0].id == own_id
    assert rows[0].start_datetime == _START
    assert rows[0].end_datetime == _END
    assert rows[0].duration_seconds == 29206
    assert rows[0].sleep_detail.sleep_total_duration_minutes == 486
    stages = rows[0].sleep_detail.sleep_stages
    assert len({(stage["stage"], stage["start_time"], stage["end_time"]) for stage in stages}) == len(stages)
    assert (
        sum(
            (datetime.fromisoformat(stage["end_time"]) - datetime.fromisoformat(stage["start_time"])).total_seconds()
            for stage in stages
        )
        == 29206
    )
    assert db.query(EventRecord).filter_by(category="menstrual_cycle").count() == 1
    db.refresh(connection)
    assert connection.last_synced_at is not None


def test_database_failure_rolls_back_sleep_and_reports_original_cause(db: Session) -> None:
    user, connection, _own = _seed_collision()
    importer = ImportService(log=logging.getLogger("test.sleep-collision"))

    def fail_detail(*args: object, **kwargs: object) -> None:
        db.execute(text("SELECT 1 / 0"))

    with (
        patch.object(event_record_service.event_record_detail_repo, "create_and_flush", side_effect=fail_detail),
        patch("app.services.sdk.import_service.log_and_capture_error") as capture,
    ):
        result = importer.import_data_from_request(db, _payload(), "application/json", str(user.id))
    assert result.status_code == 400
    assert "division by zero" in result.response
    assert "PendingRollback" not in result.response
    assert "DivisionByZero" in str(capture.call_args.args[0])
    assert db.query(EventRecord).filter_by(category="sleep").count() == 2
    assert db.query(EventRecord).filter_by(category="menstrual_cycle").count() == 0
    db.refresh(connection)
    assert connection.last_synced_at is None
