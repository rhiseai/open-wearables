"""
Tests for concurrent OAuth token refresh handling.

Regression cover for the Whoop false-revocation race: two inbound webhooks each
triggered a refresh, the single-use refresh token was consumed by the winner and
the loser's 400 revoked a connection that had just been given valid tokens.

Tests cover:
- Refresh failure with a rotated stored token does not revoke the connection
- Refresh failure with a genuinely dead token still revokes and notifies
- The refresh lock lets a late caller reuse the token another worker stored
- Redis being unavailable does not block the refresh
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.models import User, UserConnection
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import ConnectionStatus
from app.schemas.model_crud.credentials import OAuthTokenResponse
from app.services.providers.api_client import _get_valid_token
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.providers.whoop.oauth import WhoopOAuth
from tests.factories import UserConnectionFactory, UserFactory


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    """Build the HTTPStatusError httpx raises for a rejected refresh."""
    request = httpx.Request("POST", "https://api.prod.whoop.com/oauth/oauth2/token")
    response = httpx.Response(status_code, text="invalid_grant", request=request)
    return httpx.HTTPStatusError("rejected", request=request, response=response)


class TestRefreshTokenRotationGuard:
    """Test suite for the revocation guard in BaseOAuthTemplate.refresh_access_token."""

    @pytest.fixture
    def whoop_oauth(self) -> WhoopOAuth:
        """Create WhoopOAuth instance backed by real repositories."""
        return WhoopOAuth(
            user_repo=UserRepository(User),
            connection_repo=UserConnectionRepository(),
            provider_name="whoop",
            api_base_url="https://api.prod.whoop.com",
        )

    @patch("app.services.providers.templates.base_oauth.on_connection_revoked")
    @patch("httpx.post")
    def test_rejected_refresh_token_that_was_rotated_does_not_revoke(
        self,
        mock_post: MagicMock,
        mock_on_revoked: MagicMock,
        whoop_oauth: WhoopOAuth,
        db: Session,
    ) -> None:
        """Should return the stored token instead of revoking when the token was rotated."""
        # Arrange - a concurrent refresh already stored a rotated token pair
        user = UserFactory()
        connection = UserConnectionFactory(
            user=user,
            provider="whoop",
            access_token="winner_access_token",
            refresh_token="rotated_refresh_token",
            token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        mock_post.return_value.raise_for_status.side_effect = _http_error(400)

        # Act - this worker refreshes with the token the winner already consumed
        token_response = whoop_oauth.refresh_access_token(db, user.id, "consumed_refresh_token")

        # Assert
        assert token_response.access_token == "winner_access_token"
        assert token_response.refresh_token == "rotated_refresh_token"
        assert connection.status == ConnectionStatus.ACTIVE
        mock_on_revoked.assert_not_called()

    @patch("app.services.providers.templates.base_oauth.on_connection_revoked")
    @patch("httpx.post")
    def test_genuinely_dead_refresh_token_revokes_connection(
        self,
        mock_post: MagicMock,
        mock_on_revoked: MagicMock,
        whoop_oauth: WhoopOAuth,
        db: Session,
    ) -> None:
        """Should revoke, notify, and raise 401 when the stored token is the rejected one."""
        # Arrange
        user = UserFactory()
        connection = UserConnectionFactory(
            user=user,
            provider="whoop",
            access_token="stale_access_token",
            refresh_token="dead_refresh_token",
            token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        mock_post.return_value.raise_for_status.side_effect = _http_error(400)

        # Act
        with pytest.raises(HTTPException) as exc_info:
            whoop_oauth.refresh_access_token(db, user.id, "dead_refresh_token")

        # Assert
        assert exc_info.value.status_code == 401
        assert connection.status == ConnectionStatus.REVOKED
        mock_on_revoked.assert_called_once()
        assert mock_on_revoked.call_args.kwargs["reason"] == "refresh_failed"

    @patch("app.services.providers.templates.base_oauth.on_connection_revoked")
    @patch("httpx.post")
    def test_rotation_between_check_and_revoke_does_not_revoke(
        self,
        mock_post: MagicMock,
        mock_on_revoked: MagicMock,
        whoop_oauth: WhoopOAuth,
        db: Session,
    ) -> None:
        """Should not revoke when the rotation commits after the check but before the revoke."""
        # Arrange
        user = UserFactory()
        connection = UserConnectionFactory(
            user=user,
            provider="whoop",
            access_token="stale_access_token",
            refresh_token="racing_refresh_token",
            token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        mock_post.return_value.raise_for_status.side_effect = _http_error(400)

        real_get_rotated_token = whoop_oauth._get_rotated_token
        checks: list[int] = []

        def rotate_after_first_check(*args: object, **kwargs: object) -> OAuthTokenResponse | None:
            """First check sees the rejected token, then the winner commits its rotation."""
            if checks:
                return real_get_rotated_token(db, user.id, "racing_refresh_token")
            checks.append(1)
            connection.access_token = "winner_access_token"
            connection.refresh_token = "rotated_refresh_token"
            connection.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            db.add(connection)
            db.commit()
            return None

        # Act
        with patch.object(whoop_oauth, "_get_rotated_token", side_effect=rotate_after_first_check):
            token_response = whoop_oauth.refresh_access_token(db, user.id, "racing_refresh_token")

        # Assert - the guarded revoke matched no rows, so the connection survived
        assert token_response.access_token == "winner_access_token"
        assert connection.status == ConnectionStatus.ACTIVE
        mock_on_revoked.assert_not_called()

    @patch("app.services.providers.templates.base_oauth.on_connection_revoked")
    @patch("httpx.post")
    def test_already_revoked_connection_is_not_revoked_twice(
        self,
        mock_post: MagicMock,
        mock_on_revoked: MagicMock,
        whoop_oauth: WhoopOAuth,
        db: Session,
    ) -> None:
        """Should not emit a second webhook when the connection is already revoked."""
        # Arrange - revoked connections have their tokens cleared
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="whoop",
            access_token=None,
            refresh_token=None,
            token_expires_at=None,
            status=ConnectionStatus.REVOKED,
        )
        mock_post.return_value.raise_for_status.side_effect = _http_error(401)

        # Act
        with pytest.raises(HTTPException) as exc_info:
            whoop_oauth.refresh_access_token(db, user.id, "dead_refresh_token")

        # Assert
        assert exc_info.value.status_code == 401
        mock_on_revoked.assert_not_called()


class TestGetValidTokenLocking:
    """Test suite for the refresh lock in api_client._get_valid_token."""

    @pytest.fixture
    def connection_repo(self) -> UserConnectionRepository:
        """Real repository so the fresh re-read hits the database."""
        return UserConnectionRepository()

    @pytest.fixture
    def expiring_connection(self) -> UserConnection:
        """Connection whose access token is inside the 5 minute refresh buffer."""
        return UserConnectionFactory(
            user=UserFactory(),
            provider="whoop",
            access_token="expiring_access_token",
            refresh_token="stored_refresh_token",
            token_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )

    def test_token_refreshed_while_waiting_for_lock_is_reused(
        self,
        connection_repo: UserConnectionRepository,
        expiring_connection: UserConnection,
        db: Session,
    ) -> None:
        """Should skip the HTTP refresh when the lock holder already stored a fresh token."""
        # Arrange - acquiring the lock stands in for waiting on the worker that refreshes
        oauth = MagicMock(spec=BaseOAuthTemplate)

        def refresh_while_blocked() -> bool:
            expiring_connection.access_token = "winner_access_token"
            expiring_connection.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            db.add(expiring_connection)
            db.commit()
            return True

        lock = MagicMock()
        lock.acquire.side_effect = refresh_while_blocked
        redis_client = MagicMock()
        redis_client.lock.return_value = lock

        # Act
        with patch("app.services.providers.api_client.get_redis_client", return_value=redis_client):
            access_token = _get_valid_token(db, expiring_connection.user_id, "whoop", connection_repo, oauth)

        # Assert
        assert access_token == "winner_access_token"
        oauth.refresh_access_token.assert_not_called()
        redis_client.lock.assert_called_once()
        assert redis_client.lock.call_args.args[0] == f"oauth:token_refresh:{expiring_connection.user_id}:whoop"
        lock.release.assert_called_once()

    def test_expiring_token_is_refreshed_under_the_lock(
        self,
        connection_repo: UserConnectionRepository,
        expiring_connection: UserConnection,
        db: Session,
    ) -> None:
        """Should refresh with the stored refresh token and release the lock afterwards."""
        # Arrange
        oauth = MagicMock(spec=BaseOAuthTemplate)
        oauth.refresh_access_token.return_value = OAuthTokenResponse(
            access_token="new_access_token",
            token_type="Bearer",
            refresh_token="new_refresh_token",
            expires_in=3600,
        )
        lock = MagicMock()
        lock.acquire.return_value = True
        redis_client = MagicMock()
        redis_client.lock.return_value = lock

        # Act
        with patch("app.services.providers.api_client.get_redis_client", return_value=redis_client):
            access_token = _get_valid_token(db, expiring_connection.user_id, "whoop", connection_repo, oauth)

        # Assert
        assert access_token == "new_access_token"
        oauth.refresh_access_token.assert_called_once_with(
            db,
            expiring_connection.user_id,
            "stored_refresh_token",
        )
        lock.release.assert_called_once()

    def test_refresh_proceeds_when_redis_is_unavailable(
        self,
        connection_repo: UserConnectionRepository,
        expiring_connection: UserConnection,
        db: Session,
    ) -> None:
        """Should degrade gracefully and still refresh when the lock cannot be taken."""
        # Arrange
        oauth = MagicMock(spec=BaseOAuthTemplate)
        oauth.refresh_access_token.return_value = OAuthTokenResponse(
            access_token="new_access_token",
            token_type="Bearer",
            refresh_token="new_refresh_token",
            expires_in=3600,
        )

        # Act
        with patch(
            "app.services.providers.api_client.get_redis_client",
            side_effect=RedisError("connection refused"),
        ):
            access_token = _get_valid_token(db, expiring_connection.user_id, "whoop", connection_repo, oauth)

        # Assert
        assert access_token == "new_access_token"
        oauth.refresh_access_token.assert_called_once()

    def test_valid_token_skips_the_lock_entirely(
        self,
        connection_repo: UserConnectionRepository,
        db: Session,
    ) -> None:
        """Should not touch Redis when the stored token is still valid."""
        # Arrange
        connection = UserConnectionFactory(
            user=UserFactory(),
            provider="whoop",
            access_token="valid_access_token",
            token_expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        oauth = MagicMock(spec=BaseOAuthTemplate)
        redis_client = MagicMock()

        # Act
        with patch("app.services.providers.api_client.get_redis_client", return_value=redis_client):
            access_token = _get_valid_token(db, connection.user_id, "whoop", connection_repo, oauth)

        # Assert
        assert access_token == "valid_access_token"
        redis_client.lock.assert_not_called()
        oauth.refresh_access_token.assert_not_called()
