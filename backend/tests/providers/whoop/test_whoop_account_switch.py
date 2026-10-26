"""Reconnecting WHOOP to a different account drops the previous account's data (RHISE-6353)."""

from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, HealthScore, UserConnection
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import OAuthState, OAuthTokenResponse
from app.services.providers.whoop.strategy import WhoopStrategy
from tests.factories import (
    DataSourceFactory,
    EventRecordFactory,
    HealthScoreFactory,
    UserConnectionFactory,
    UserFactory,
)


def _reconnect(db: Session, user_id, whoop_user_id: int) -> None:
    oauth = WhoopStrategy().oauth
    assert oauth
    token = OAuthTokenResponse(
        access_token="new-access", token_type="Bearer", refresh_token="new-refresh", expires_in=3600
    )
    # WHOOP's profile API returns an integer id; the column holds text.
    oauth._save_connection(
        db,
        user_id,
        token,
        {"user_id": whoop_user_id, "username": None},  # ty: ignore[invalid-argument-type]
        OAuthState(user_id=user_id, provider="whoop"),
    )


def _seed_whoop_data(db: Session, user) -> DataSource:
    data_source = DataSourceFactory(user=user, provider=ProviderName.WHOOP, source="whoop", device_model=None)
    EventRecordFactory(data_source=data_source, category="sleep", type_="sleep_session", source_name="whoop")
    HealthScoreFactory(data_source=data_source, provider=ProviderName.WHOOP)
    return data_source


def test_reconnect_to_another_account_deletes_previous_account_data(db: Session) -> None:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="whoop", provider_user_id="37355326", provider_username="other")
    _seed_whoop_data(db, user)

    _reconnect(db, user.id, 37348761)

    connection = db.query(UserConnection).filter_by(user_id=user.id, provider="whoop").one()
    assert connection.provider_user_id == "37348761"
    assert connection.provider_username is None
    assert connection.access_token == "new-access"
    assert db.query(DataSource).filter_by(user_id=user.id, provider=ProviderName.WHOOP).count() == 0
    assert db.query(EventRecord).count() == 0
    assert db.query(HealthScore).filter_by(user_id=user.id).count() == 0


def test_reconnect_to_the_same_account_keeps_its_data(db: Session) -> None:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="whoop", provider_user_id="37348761")
    data_source = _seed_whoop_data(db, user)

    _reconnect(db, user.id, 37348761)

    assert db.query(EventRecord).filter_by(data_source_id=data_source.id).count() == 1
    assert db.query(HealthScore).filter_by(user_id=user.id).count() == 1
