from fastapi import APIRouter, status

from app.database import DbSession
from app.schemas.auth import RefreshTokenRequest, TokenResponse
from app.services import refresh_token_service

router = APIRouter()


@router.post("/token/refresh")
def refresh_token(
    payload: RefreshTokenRequest,
    db: DbSession,
) -> TokenResponse:
    """Exchange refresh token for new access token.

    This endpoint accepts both SDK and Developer refresh tokens and returns
    a new access token of the same type. Implements refresh token rotation:
    the old refresh token is revoked and a new one is issued.

    A token that rotation revoked can be presented again, for a client that
    never read the response: it returns the same new refresh token with a fresh
    access token, until that refresh token is used or revoked or the replay
    window (`REFRESH_TOKEN_REPLAY_WINDOW_SECONDS`, 7 days by default) ends.

    Args:
        payload: Request containing the refresh token
        db: Database session

    Returns:
        TokenResponse with new access token and new refresh token

    Raises:
        401: If the refresh token is invalid, revoked, or rotated outside its replay window
    """
    return refresh_token_service.refresh_token(db, payload.refresh_token)


@router.post("/token/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke_refresh_token(
    payload: RefreshTokenRequest,
    db: DbSession,
) -> None:
    """Revoke a refresh token.

    Use this endpoint to invalidate a refresh token, for example when
    logging out or when a token is compromised. This follows the OAuth 2.0
    Token Revocation specification (RFC 7009) which uses POST.

    Args:
        payload: Request containing the refresh token to revoke
        db: Database session

    Raises:
        404: If the refresh token is not found
    """
    refresh_token_service.revoke_token(db, payload.refresh_token)
