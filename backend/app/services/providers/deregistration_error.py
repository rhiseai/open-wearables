"""Safe diagnostics for best-effort provider deregistration failures."""

from contextlib import suppress

import httpx
from fastapi import HTTPException


class ProviderDeregistrationError(RuntimeError):
    """A reportable failure containing only explicitly selected HTTP metadata."""

    def __init__(
        self,
        *,
        error_type: str,
        status_code: int | None,
        method: str | None,
        endpoint: str | None,
    ) -> None:
        self.error_type = error_type
        self.status_code = status_code
        self.method = method
        self.endpoint = endpoint
        message = f"Provider deregistration failed ({error_type})"
        if status_code is not None:
            message += f": HTTP {status_code}"
        if endpoint is not None:
            message += f" for {method} {endpoint}"
        super().__init__(message)

    @property
    def context(self) -> dict[str, str | int | None]:
        return {
            "error_type": self.error_type,
            "status_code": self.status_code,
            "method": self.method,
            "endpoint": self.endpoint,
        }


def safe_deregistration_error(error: Exception) -> ProviderDeregistrationError | HTTPException:
    """Keep the failure's traceback, without its message, response body or chain."""
    safe_error: ProviderDeregistrationError | HTTPException
    if isinstance(error, HTTPException):
        # Keep the existing handled-client-error policy in log_and_capture_error.
        safe_error = HTTPException(status_code=error.status_code, detail="Provider deregistration failed")
    else:
        request: httpx.Request | None = None
        if isinstance(error, (httpx.HTTPStatusError, httpx.RequestError)):
            # HTTPX raises from this property when a RequestError has no attached request.
            with suppress(RuntimeError):
                request = error.request
        safe_error = ProviderDeregistrationError(
            error_type=type(error).__name__,
            status_code=error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None,
            method=request.method if request is not None else None,
            endpoint=str(request.url.copy_with(query=None, fragment=None, userinfo=b""))
            if request is not None
            else None,
        )
    safe_error.__suppress_context__ = True
    return safe_error.with_traceback(error.__traceback__)
