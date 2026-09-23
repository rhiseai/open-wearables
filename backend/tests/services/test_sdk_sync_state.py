from app.services.sdk_sync_state import sdk_payload_exceeds_realtime_limit


def test_realtime_limit_is_counted_per_payload_type() -> None:
    data = {
        "records": [{}] * 200,
        "workouts": [{}] * 40,
        "sleep": [{}] * 20,
    }

    assert sdk_payload_exceeds_realtime_limit(data) is False


def test_one_oversized_payload_type_is_historical() -> None:
    data = {
        "records": [{}] * 251,
        "workouts": [],
        "sleep": [],
    }

    assert sdk_payload_exceeds_realtime_limit(data) is True
