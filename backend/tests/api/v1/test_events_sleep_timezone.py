"""The sleep list and totals rank sources per night on the user's day (RHISE-8109).

`filter_by_priority` keeps the top source's sessions per sleep date, the local date
of the wake. Apple Health sends most nights without an offset of their own, and
those fell on their UTC date: a Tokyo night that one source ended before 09:00 and
another after it counted as two nights, so both sources' copies came back.
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.schemas.enums import ProviderName
from app.services.priority_service import priority_service
from tests.factories import ApiKeyFactory, DataSourceFactory, EventRecordFactory, UserFactory
from tests.utils import api_key_headers


def _night(user: object, db: Session) -> tuple[object, object]:
    """One Tokyo night (wake on Apr 11, local) recorded by Garmin and Oura, neither with an offset."""
    garmin = DataSourceFactory(user=user, provider="garmin", source="garmin")
    oura = DataSourceFactory(user=user, provider="oura", source="oura")
    # Wakes 08:30 in Tokyo: still Apr 10 in UTC.
    garmin_night = EventRecordFactory(
        mapping=garmin,
        category="sleep",
        type_="sleep",
        start_datetime=datetime(2026, 4, 10, 15, 0, tzinfo=timezone.utc),
        end_datetime=datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc),
        duration_seconds=30600,
        zone_offset=None,
    )
    # Wakes 10:00 in Tokyo: Apr 11 in UTC too.
    oura_night = EventRecordFactory(
        mapping=oura,
        category="sleep",
        type_="sleep",
        start_datetime=datetime(2026, 4, 10, 15, 30, tzinfo=timezone.utc),
        end_datetime=datetime(2026, 4, 11, 1, 0, tzinfo=timezone.utc),
        duration_seconds=34200,
        zone_offset=None,
    )
    priority_service.update_provider_priority(db, ProviderName.GARMIN, 1)
    priority_service.update_provider_priority(db, ProviderName.OURA, 2)
    return garmin_night, oura_night


def _get(client: TestClient, path: str, **extra: str) -> dict:
    response = client.get(
        path,
        headers=api_key_headers(ApiKeyFactory().plain_key),
        params={"start_date": "2026-04-09", "end_date": "2026-04-12", "filter_by_priority": "true", **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestSleepPriorityOnTheUsersDay:
    def test_one_local_night_keeps_only_the_top_source(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        garmin_night, _ = _night(user, db)

        listed = _get(client, f"/api/v1/users/{user.id}/events/sleep", timezone="Asia/Tokyo")["data"]

        assert [s["id"] for s in listed] == [str(garmin_night.id)]

    def test_without_a_zone_the_night_still_falls_on_its_utc_dates(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        garmin_night, oura_night = _night(user, db)

        listed = _get(client, f"/api/v1/users/{user.id}/events/sleep")["data"]

        assert {s["id"] for s in listed} == {str(garmin_night.id), str(oura_night.id)}

    def test_the_totals_add_up_the_same_night(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        _night(user, db)

        totals = _get(client, f"/api/v1/users/{user.id}/events/sleep/totals", timezone="Asia/Tokyo")

        assert totals["count"] == 1

    def test_an_unknown_zone_is_refused(self, client: TestClient, db: Session) -> None:
        user = UserFactory()

        response = client.get(
            f"/api/v1/users/{user.id}/events/sleep",
            headers=api_key_headers(ApiKeyFactory().plain_key),
            params={"start_date": "2026-04-09", "end_date": "2026-04-12", "timezone": "Mars/Olympus"},
        )

        assert response.status_code in (400, 422)
