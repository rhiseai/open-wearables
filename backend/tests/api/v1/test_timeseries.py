"""Tests for the /timeseries endpoint's ``resolution`` parameter.

The endpoint accepted ``resolution`` and threw it away, so a day of dense
heart rate came back raw whatever was asked for — and a client sizing its
page budget for "5min" silently read a fraction of the window.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.factories import (
    ApiKeyFactory,
    DataPointSeriesFactory,
    DataSourceFactory,
    SeriesTypeDefinitionFactory,
    UserFactory,
)
from tests.utils import api_key_headers

_DAY_START = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)


def _get(client: TestClient, user_id: str, **params: object) -> dict:
    api_key = ApiKeyFactory()
    response = client.get(
        f"/api/v1/users/{user_id}/timeseries",
        headers=api_key_headers(api_key.plain_key),
        params={
            "start_time": "2026-06-01T00:00:00Z",
            "end_time": "2026-06-01T23:59:59Z",
            "types": ["heart_rate"],
            "limit": 100,
            **params,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestTimeseriesResolution:
    def test_an_hourly_read_returns_one_sample_per_hour(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user)
        heart_rate = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        # Four samples an hour for three hours: 12 raw, 3 hourly.
        for quarter in range(12):
            DataPointSeriesFactory(
                data_source=source,
                series_type=heart_rate,
                recorded_at=_DAY_START + timedelta(minutes=15 * quarter),
                value=Decimal("60") + quarter,
            )

        raw = _get(client, str(user.id), resolution="raw")
        hourly = _get(client, str(user.id), resolution="1hour")

        assert len(raw["data"]) == 12
        assert raw["pagination"]["total_count"] == 12
        assert raw["metadata"]["resolution"] == "raw"

        assert len(hourly["data"]) == 3
        # Aggregated reads avoid a full count over the largest table; cursors and
        # has_more provide bounded pagination without scanning the whole range.
        assert hourly["pagination"]["total_count"] is None
        assert hourly["metadata"]["resolution"] == "1hour"
        assert [s["timestamp"] for s in hourly["data"]] == [
            "2026-06-01T00:00:00Z",
            "2026-06-01T01:00:00Z",
            "2026-06-01T02:00:00Z",
        ]
        # Heart rate is a rate: each hour is the mean of its four samples.
        assert [s["value"] for s in hourly["data"]] == [61.5, 65.5, 69.5]
        assert {s["unit"] for s in hourly["data"]} == {"bpm"}
        assert hourly["data"][0]["source"]["provider"] == source.provider

    def test_a_downsampled_page_carries_a_working_cursor(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user)
        heart_rate = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        for minute in range(0, 60, 5):
            DataPointSeriesFactory(
                data_source=source,
                series_type=heart_rate,
                recorded_at=_DAY_START + timedelta(minutes=minute),
                value=Decimal("60") + minute,
            )

        first = _get(client, str(user.id), resolution="15min", limit=2)

        assert [s["timestamp"] for s in first["data"]] == ["2026-06-01T00:00:00Z", "2026-06-01T00:15:00Z"]
        assert first["pagination"]["has_more"] is True

        second = _get(client, str(user.id), resolution="15min", limit=2, cursor=first["pagination"]["next_cursor"])

        assert [s["timestamp"] for s in second["data"]] == ["2026-06-01T00:30:00Z", "2026-06-01T00:45:00Z"]
        assert second["pagination"]["has_more"] is False

    def test_an_unknown_resolution_is_rejected(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        api_key = ApiKeyFactory()

        response = client.get(
            f"/api/v1/users/{user.id}/timeseries",
            headers=api_key_headers(api_key.plain_key),
            params={
                "start_time": "2026-06-01T00:00:00Z",
                "end_time": "2026-06-01T23:59:59Z",
                "resolution": "30min",
            },
        )

        # The app maps request validation errors to 400.
        assert response.status_code == 400
