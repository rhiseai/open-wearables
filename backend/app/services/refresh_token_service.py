import secrets
from datetime import datetime, timedelta, timezone
from logging import Logger, getLogger
from uuid import UUID

from fastapi import HTTPException, status

from app.config import settings
from app.database import DbSession
from app.models import Application, RefreshToken
from app.repositories.refresh_token_repository import refresh_token_repository
from app.schemas.auth import SDKTokenOrigin, TokenResponse, TokenType
from app.services.sdk_token_service import create_sdk_user_token
from app.utils.auth_diagnostics import AuthBoundary, AuthOutcome, AuthReason, log_auth_decision
from app.utils.security import create_access_token
from app.utils.structured_logging import log_structured


class RefreshTokenService:
    """Service for managing refresh tokens."""

    def __init__(self, log: Logger) -> None:
        self.logger = log
        self.repo = refresh_token_repository

    @staticmethod
    def _generate_refresh_token_id() -> str:
        """Generate an opaque refresh token ID with rt- prefix."""
        return f"rt-{secrets.token_hex(16)}"

    @staticmethod
    def _sdk_app_id_claim(
        origin: SDKTokenOrigin,
        application: Application | None,
        issued_by_developer_id: UUID | None,
    ) -> str:
        """Build the app_id claim of an SDK access token from where the token came from."""
        if origin == SDKTokenOrigin.APPLICATION:
            return application.app_id  # ty:ignore[unresolved-attribute]
        # The issuing developer may have been deleted since; keep the origin prefix alone then
        return f"{origin}:{issued_by_developer_id}" if issued_by_developer_id else str(origin)

    def issue_sdk_tokens(
        self,
        db_session: DbSession,
        user_id: UUID,
        origin: SDKTokenOrigin,
        application: Application | None = None,
        issued_by_developer_id: UUID | None = None,
    ) -> tuple[str, str]:
        """Issue an SDK access token and a matching refresh token.

        Args:
            db_session: Database session
            user_id: The OpenWearables User ID
            origin: How the token is being issued
            application: The application that requested the token (APPLICATION origin)
            issued_by_developer_id: The developer who issued the token (ADMIN / INVITE origin)

        Returns:
            Tuple of (access_token, refresh_token)
        """
        access_token = create_sdk_user_token(
            app_id=self._sdk_app_id_claim(origin, application, issued_by_developer_id),
            user_id=str(user_id),
        )
        refresh_token = self.create_sdk_refresh_token(
            db_session,
            user_id=user_id,
            origin=origin,
            application_id=application.id if application else None,
            issued_by_developer_id=issued_by_developer_id,
        )
        return access_token, refresh_token

    def create_sdk_refresh_token(
        self,
        db_session: DbSession,
        user_id: UUID,
        origin: SDKTokenOrigin,
        application_id: UUID | None = None,
        issued_by_developer_id: UUID | None = None,
        replaces_id: str | None = None,
    ) -> str:
        """Create a refresh token for an SDK token.

        Args:
            db_session: Database session
            user_id: The OpenWearables User ID
            origin: How the token was issued
            application_id: The application that requested the token (APPLICATION origin)
            issued_by_developer_id: The developer who issued the token (ADMIN / INVITE origin)
            replaces_id: The refresh token this one replaces, when issued by rotation

        Returns:
            The refresh token string (rt-{hex})
        """
        token_id = self._generate_refresh_token_id()
        token = RefreshToken(
            id=token_id,
            token_type=TokenType.SDK,
            user_id=user_id,
            origin=origin,
            application_id=application_id,
            issued_by_developer_id=issued_by_developer_id,
            developer_id=None,
            replaces_id=replaces_id,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        self.repo.create(db_session, token)
        log_structured(
            self.logger,
            "debug",
            "Created SDK refresh token",
            action="sdk_refresh_token_created",
            origin=str(origin),
        )
        return token_id

    def create_developer_refresh_token(
        self,
        db_session: DbSession,
        developer_id: UUID,
        replaces_id: str | None = None,
    ) -> str:
        """Create a refresh token for a developer token.

        Args:
            db_session: Database session
            developer_id: The developer ID
            replaces_id: The refresh token this one replaces, when issued by rotation

        Returns:
            The refresh token string (rt-{hex})
        """
        token_id = self._generate_refresh_token_id()
        token = RefreshToken(
            id=token_id,
            token_type=TokenType.DEVELOPER,
            user_id=None,
            developer_id=developer_id,
            replaces_id=replaces_id,
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
            revoked_at=None,
        )
        self.repo.create(db_session, token)
        self.logger.debug("Created developer refresh token")
        return token_id

    def refresh_token(self, db_session: DbSession, refresh_token_str: str) -> TokenResponse:
        """Exchange a refresh token for a new access token.

        Implements refresh token rotation: the old refresh token is revoked and a new one is
        issued with each refresh request. A replay of a token that rotation revoked returns
        the same successor with a fresh access token, until the successor is used or revoked
        or `refresh_token_replay_window_seconds` has passed: a client whose request reached
        the server but whose response it never read still holds the old token.

        Args:
            db_session: Database session
            refresh_token_str: The refresh token string

        Returns:
            TokenResponse with new access token and new refresh token

        Raises:
            HTTPException: If the refresh token is invalid or revoked
        """
        token = self.repo.claim_valid_token(db_session, refresh_token_str)
        if not token:
            return self._replay_rotation(db_session, refresh_token_str)

        # Minting first rejects an unknown token type before anything is stored. Storing the new
        # refresh token commits the old one's revocation with it, so a failure before that leaves
        # the old token usable
        access_token = self._mint_access_token(token)
        if token.token_type == TokenType.SDK:
            new_refresh_token = self.create_sdk_refresh_token(
                db_session,
                user_id=token.user_id,  # ty:ignore[invalid-argument-type]
                origin=token.origin,  # ty:ignore[invalid-argument-type]
                application_id=token.application_id,
                issued_by_developer_id=token.issued_by_developer_id,
                replaces_id=token.id,
            )
        else:
            new_refresh_token = self.create_developer_refresh_token(
                db_session,
                developer_id=token.developer_id,  # ty:ignore[invalid-argument-type]
                replaces_id=token.id,
            )
        self.logger.debug("Refreshed refresh token (rotated)")
        log_auth_decision(AuthReason.REFRESH_ROTATED, AuthOutcome.ACCEPTED, boundary=AuthBoundary.REFRESH)
        return self._token_response(access_token, new_refresh_token)

    def _replay_rotation(self, db_session: DbSession, refresh_token_str: str) -> TokenResponse:
        """Answer a replay of a rotated token with its successor, or reject the token."""
        old = self.repo.get_by_id(db_session, refresh_token_str)
        successor = self._replayable_successor(db_session, old) if old is not None else None
        if old is None or successor is None:
            log_auth_decision(
                AuthReason.REFRESH_INVALID_OR_REVOKED, AuthOutcome.REJECTED, boundary=AuthBoundary.REFRESH
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked refresh token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        access_token = self._mint_access_token(old)
        self.logger.debug("Replayed a rotated refresh token")
        log_auth_decision(AuthReason.REFRESH_REPLAYED, AuthOutcome.RECOVERED, boundary=AuthBoundary.REFRESH)
        return self._token_response(access_token, successor.id)

    def _replayable_successor(self, db_session: DbSession, token: RefreshToken) -> RefreshToken | None:
        """The successor a replay of `token` would be given, or None when `token` cannot replay.

        Only rotation gives a token a successor, so a token revoked any other way (revoke,
        password change) never replays. A successor that was used or revoked since, or a
        rotation older than `refresh_token_replay_window_seconds`, ends the replays.
        """
        if token.revoked_at is None:
            return None
        window = timedelta(seconds=settings.refresh_token_replay_window_seconds)
        if datetime.now(timezone.utc) - token.revoked_at > window:
            return None
        successor = self.repo.get_successor(db_session, token.id)
        if successor is None or successor.revoked_at is not None:
            return None
        return successor

    def _mint_access_token(self, token: RefreshToken) -> str:
        """Mint an access token for the subject of `token`."""
        if token.token_type == TokenType.SDK:
            return create_sdk_user_token(
                app_id=self._sdk_app_id_claim(
                    token.origin,  # ty:ignore[invalid-argument-type]
                    token.application,
                    token.issued_by_developer_id,
                ),
                user_id=str(token.user_id),
            )
        if token.token_type == TokenType.DEVELOPER:
            return create_access_token(subject=str(token.developer_id))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown token type: {token.token_type}",
        )

    @staticmethod
    def _token_response(access_token: str, refresh_token: str) -> TokenResponse:
        return TokenResponse(
            access_token=access_token,
            token_type="bearer",
            refresh_token=refresh_token,
            expires_in=settings.access_token_expire_minutes * 60,
        )

    def revoke_token(self, db_session: DbSession, refresh_token_str: str) -> bool:
        """Revoke a refresh token.

        A rotated token that could still be replayed revokes its successor instead: within the
        replay window it gives access to that successor, so revoking it must end that access.

        Args:
            db_session: Database session
            refresh_token_str: The refresh token string

        Returns:
            True if the token was revoked

        Raises:
            HTTPException: If the refresh token is not found
        """
        token = self.repo.claim_valid_token(db_session, refresh_token_str)
        if not token:
            rotated = self.repo.get_by_id(db_session, refresh_token_str)
            successor = self._replayable_successor(db_session, rotated) if rotated is not None else None
            token = self.repo.claim_valid_token(db_session, successor.id) if successor is not None else None
        if not token:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Refresh token not found",
            )

        db_session.commit()
        self.logger.debug("Revoked refresh token")
        return True


refresh_token_service = RefreshTokenService(log=getLogger(__name__))
