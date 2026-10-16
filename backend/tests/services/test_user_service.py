"""
Tests for UserService.

Tests cover:
- Creating users with auto-generated ID and timestamp
- Updating users with auto-set updated_at
- Getting users by ID
- Deleting users
- Counting users in date range
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.schemas.model_crud.user_management import UserCreate, UserUpdate
from app.services.providers.deregistration_error import ProviderDeregistrationError
from app.services.providers.oura.oauth import OuraOAuth
from app.services.user_connection_service import user_connection_service
from app.services.user_service import user_service
from tests.factories import UserConnectionFactory, UserFactory


class TestUserServiceCreate:
    """Test user creation with auto-generated fields."""

    def test_create_user_generates_id_and_timestamp(self, db: Session) -> None:
        """Should create user with auto-generated ID and created_at."""
        # Arrange
        payload = UserCreate(
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        # Act
        user = user_service.create(db, payload)

        # Assert
        assert user.id is not None
        assert user.created_at is not None
        assert user.email == "test@example.com"
        assert user.first_name == "Test"
        assert user.last_name == "User"

        # Verify timestamp is recent (within last minute)
        assert (datetime.now(timezone.utc) - user.created_at).total_seconds() < 60

    def test_create_user_with_minimal_data(self, db: Session) -> None:
        """Should create user with only email."""
        # Arrange
        payload = UserCreate(email="minimal@example.com")

        # Act
        user = user_service.create(db, payload)

        # Assert
        assert user.id is not None
        assert user.created_at is not None
        assert user.email == "minimal@example.com"
        assert user.first_name is None
        assert user.last_name is None

    def test_create_user_with_external_user_id(self, db: Session) -> None:
        """Should create user with external_user_id."""
        # Arrange
        payload = UserCreate(
            email="external@example.com",
            external_user_id="ext_12345",
        )

        # Act
        user = user_service.create(db, payload)

        # Assert
        assert user.external_user_id == "ext_12345"

    def test_create_user_persists_to_database(self, db: Session) -> None:
        """Should persist user to database."""
        # Arrange
        payload = UserCreate(email="persist@example.com")

        # Act
        user = user_service.create(db, payload)

        # Assert - verify in database
        db_user = user_service.get(db, user.id)
        assert db_user is not None
        assert db_user.email == "persist@example.com"

    def test_create_user_duplicate_email_returns_409(self, db: Session) -> None:
        """Should raise 409 when registering with an already-used email."""
        UserFactory(email="taken@example.com")

        with pytest.raises(HTTPException) as exc_info:
            user_service.create(db, UserCreate(email="taken@example.com"))

        assert exc_info.value.status_code == 409


class TestUserServiceUpdate:
    """Test user update with auto-set updated_at."""

    def test_update_user_sets_updated_at(self, db: Session) -> None:
        """Should set updated_at when updating user."""
        # Arrange
        user = UserFactory(email="original@example.com")
        update_payload = UserUpdate(email="updated@example.com")

        # Act
        updated_user = user_service.update(db, user.id, update_payload)

        # Assert
        assert updated_user is not None
        assert updated_user.email == "updated@example.com"

    def test_update_user_partial_fields(self, db: Session) -> None:
        """Should update only specified fields."""
        # Arrange
        user = UserFactory(
            email="original@example.com",
            first_name="Original",
            last_name="Name",
        )
        update_payload = UserUpdate(first_name="Updated")

        # Act
        updated_user = user_service.update(db, user.id, update_payload)

        # Assert
        assert updated_user is not None
        assert updated_user.first_name == "Updated"
        assert updated_user.last_name == "Name"  # Unchanged
        assert updated_user.email == "original@example.com"  # Unchanged

    def test_update_nonexistent_user_returns_none(self, db: Session) -> None:
        """Should return None when updating non-existent user."""
        # Arrange
        fake_id = uuid4()
        update_payload = UserUpdate(email="new@example.com")

        # Act
        result = user_service.update(db, fake_id, update_payload, raise_404=False)

        # Assert
        assert result is None

    def test_update_user_with_raise_404(self, db: Session) -> None:
        """Should raise HTTPException(404) when updating non-existent user with raise_404=True."""
        # Arrange
        from fastapi import HTTPException

        fake_id = uuid4()
        update_payload = UserUpdate(email="new@example.com")

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            user_service.update(db, fake_id, update_payload, raise_404=True)

        assert exc_info.value.status_code == 404


class TestUserServiceGet:
    """Test getting users by ID."""

    def test_get_existing_user(self, db: Session) -> None:
        """Should retrieve existing user by ID."""
        # Arrange
        user = UserFactory(email="get@example.com")

        # Act
        retrieved = user_service.get(db, user.id)

        # Assert
        assert retrieved is not None
        assert retrieved.id == user.id
        assert retrieved.email == user.email

    def test_get_nonexistent_user_returns_none(self, db: Session) -> None:
        """Should return None for non-existent user."""
        # Arrange
        fake_id = uuid4()

        # Act
        result = user_service.get(db, fake_id)

        # Assert
        assert result is None

    def test_get_user_by_string_id(self, db: Session) -> None:
        """Should retrieve user using string ID."""
        # Arrange
        user = UserFactory(email="string@example.com")

        # Act
        retrieved = user_service.get(db, str(user.id))

        # Assert
        assert retrieved is not None
        assert retrieved.id == user.id


class TestUserServiceDelete:
    """Test user deletion."""

    def test_delete_existing_user(self, db: Session) -> None:
        """Should delete existing user."""
        # Arrange
        user = UserFactory(email="delete@example.com")
        user_id = user.id

        # Act
        user_service.delete(db, user_id)

        # Assert - user should no longer exist
        result = user_service.get(db, user_id)
        assert result is None

    def test_delete_nonexistent_user(self, db: Session) -> None:
        """Should handle deleting non-existent user gracefully."""
        # Arrange
        fake_id = uuid4()

        # Act & Assert - should not raise error
        user_service.delete(db, fake_id, raise_404=False)

    @pytest.mark.parametrize(
        "revoke_outcome",
        [200, 401, 500, httpx.ReadTimeout, httpx.ConnectError, RuntimeError, HTTPException],
        ids=[
            "success",
            "unauthorized",
            "server-error",
            "timeout",
            "connection-error",
            "unexpected-error",
            "refresh-unauthorized",
        ],
    )
    def test_delete_continues_after_provider_revoke(self, db: Session, revoke_outcome: int | type[Exception]) -> None:
        """A failed Oura revoke must still attempt later providers and remove the user."""
        user = UserFactory()
        token = "synthetic-oura-deletion-token"
        following = UserConnectionFactory(
            user=user, provider="polar", created_at=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        oura = UserConnectionFactory(
            user=user,
            provider="oura",
            access_token=token,
            token_expires_at=datetime.now(timezone.utc)
            + (timedelta(hours=-1) if revoke_outcome is HTTPException else timedelta(days=1)),
        )
        user_id, connection_ids = user.id, (oura.id, following.id)
        following_token, following_provider_id = following.access_token, following.provider_user_id
        oura_oauth = OuraOAuth(user_service.crud, user_connection_service.crud, "oura", "https://api.ouraring.com")
        following_oauth = MagicMock()
        request = httpx.Request("POST", "https://api.ouraring.com/oauth/revoke", params={"access_token": token})
        response = httpx.Response(revoke_outcome, request=request) if isinstance(revoke_outcome, int) else None
        error = None
        if revoke_outcome is HTTPException:
            error = HTTPException(401, detail=f"Refresh rejected {token}", headers={"Authorization": f"Bearer {token}"})
        elif not isinstance(revoke_outcome, int):
            error = (
                revoke_outcome(f"Revoke failed for {request.url}", request=request)
                if issubclass(revoke_outcome, httpx.RequestError)
                else revoke_outcome(f"Revoke failed for {request.url}")
            )

        with (
            patch("app.services.user_service.ProviderFactory") as factory,
            patch(
                "app.services.providers.oura.oauth.httpx.post",
                return_value=response,
                side_effect=None if revoke_outcome is HTTPException else error,
            ) as post,
            patch.object(oura_oauth, "refresh_access_token", side_effect=error) as refresh,
            patch("app.services.user_connection_service.log_and_capture_error") as report,
            patch("app.services.user_service.release_stale_primary"),
            patch("app.services.user_service.force_release_backfill_lock"),
        ):
            factory.return_value.get_provider.side_effect = [
                SimpleNamespace(oauth=oura_oauth),
                SimpleNamespace(oauth=following_oauth),
            ]
            deleted = user_service.delete(db, user_id)

        assert deleted is not None
        assert user_service.get(db, user_id) is None
        assert all(user_connection_service.get(db, connection_id) is None for connection_id in connection_ids)
        assert factory.return_value.get_provider.call_args_list == [call("oura"), call("polar")]
        following_oauth.deregister_user.assert_called_once_with(following_token, provider_user_id=following_provider_id)
        if revoke_outcome is HTTPException:
            refresh.assert_called_once()
            post.assert_not_called()
        else:
            refresh.assert_not_called()
            post.assert_called_once_with(
                "https://api.ouraring.com/oauth/revoke", params={"access_token": token}, timeout=30.0
            )
        if revoke_outcome == 200:
            report.assert_not_called()
        else:
            report.assert_called_once()
            captured = report.call_args.args[0]
            if revoke_outcome is HTTPException:
                assert isinstance(captured, HTTPException)
                assert captured.status_code == 401
                assert captured.headers is None
                expected_context = {"error_type": "HTTPException", "status_code": 401}
            else:
                assert isinstance(captured, ProviderDeregistrationError)
                assert captured.error_type == (
                    "HTTPStatusError" if isinstance(revoke_outcome, int) else revoke_outcome.__name__
                )
                assert captured.status_code == (revoke_outcome if isinstance(revoke_outcome, int) else None)
                assert captured.method == (None if revoke_outcome is RuntimeError else "POST")
                assert captured.endpoint == (
                    None if revoke_outcome is RuntimeError else "https://api.ouraring.com/oauth/revoke"
                )
                expected_context = captured.context
            assert report.call_args.kwargs["extra"] == {"user_id": str(user_id), "provider": "oura", **expected_context}
            assert captured is not error
            assert captured.__cause__ is None
            assert captured.__context__ is None
            assert not hasattr(captured, "request")
            assert not hasattr(captured, "response")
            assert token not in str(captured)
            assert token not in repr(vars(captured))
            assert token not in repr(report.call_args)


class TestUserServiceGetCountInRange:
    """Test counting users in date range."""

    def test_get_count_in_range_with_users(self, db: Session) -> None:
        """Should count users created within date range."""
        # Arrange
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        two_weeks_ago = now - timedelta(days=14)

        # Create users at different times
        UserFactory(created_at=two_weeks_ago)  # Outside range
        UserFactory(created_at=week_ago + timedelta(days=1))  # Inside range
        UserFactory(created_at=now - timedelta(days=1))  # Inside range

        # Act
        count = user_service.get_count_in_range(db, week_ago, now)

        # Assert
        assert count == 2

    def test_get_count_in_range_empty_result(self, db: Session) -> None:
        """Should return 0 when no users in range."""
        # Arrange
        now = datetime.now(timezone.utc)
        future = now + timedelta(days=7)
        far_future = future + timedelta(days=7)

        UserFactory()  # Created now, outside future range

        # Act
        count = user_service.get_count_in_range(db, future, far_future)

        # Assert
        assert count == 0

    def test_get_count_in_range_boundary_inclusive(self, db: Session) -> None:
        """Should include users created at start boundary but exclude end boundary (half-open interval)."""
        # Arrange
        start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 2, 1, 0, 0, 0, tzinfo=timezone.utc)  # End is exclusive

        # Create users at boundaries
        UserFactory(created_at=start)  # At start boundary (included)
        UserFactory(created_at=datetime(2024, 1, 31, 23, 59, 59, tzinfo=timezone.utc))  # Just before end (included)
        UserFactory(
            created_at=datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        )  # Middle

        # Act
        count = user_service.get_count_in_range(db, start, end)

        # Assert
        assert count == 3

    def test_get_count_in_range_with_no_users(self, db: Session) -> None:
        """Should return 0 when no users exist at all."""
        # Arrange
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)

        # Act
        count = user_service.get_count_in_range(db, week_ago, now)

        # Assert
        assert count == 0
