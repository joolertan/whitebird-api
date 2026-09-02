"""Exceptions raised by the clients."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx

from . import _json

__all__ = [
    "APIError",
    "AuthenticationError",
    "BadRequestError",
    "ConflictError",
    "MFARequiredError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "ServerError",
    "TransportError",
    "VerificationRequiredError",
    "WhitebirdError",
]

_VERIFICATIONS: dict[str, Literal["sms", "liveness"]] = {
    "SMS_VERIFICATION_REQUIRED": "sms",
    "LIVENESS_VERIFICATION_REQUIRED": "liveness",
}
_OAUTH_ERROR = re.compile(r"[a-z_]+")


class WhitebirdError(Exception):
    """Base class of every error raised by this package."""


class TransportError(WhitebirdError):
    """The request did not get an answer: connection failure, timeout or protocol error."""


class APIError(WhitebirdError):
    """Whitebird answered with an error.

    Attributes:
        message: Human-readable description sent by the server.
        status_code: HTTP status of the response, if the error came from one.
        code: Numeric error code from the body (`code`, or `retCode` of enveloped responses).
        error_code: Machine-readable error, e.g. `SMS_VERIFICATION_REQUIRED` or `invalid_grant`.
        fields: Per-field validation problems, exactly as sent by the server.
        body: Decoded response body (JSON value or text).
        response: The HTTP response, if the error came from one.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: int | None = None,
        error_code: str | None = None,
        fields: list[Any] | None = None,
        body: Any = None,
        response: httpx.Response | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.error_code = error_code
        self.fields = fields or []
        self.body = body
        self.response = response

    def __str__(self) -> str:
        prefix = " ".join(str(part) for part in (self.status_code, self.error_code) if part)
        return f"{prefix}: {self.message}" if prefix else self.message


class BadRequestError(APIError):
    """The request was rejected as invalid (HTTP 400)."""


class VerificationRequiredError(BadRequestError):
    """The operation needs an extra verification step in the Whitebird app first.

    Attributes:
        kind: `"sms"` for an SMS confirmation, `"liveness"` for a liveness check.
    """

    def __init__(self, message: str, *, kind: Literal["sms", "liveness"], **details: Any) -> None:
        super().__init__(message, **details)
        self.kind = kind


class AuthenticationError(APIError):
    """Credentials or tokens were rejected (HTTP 401, OAuth `invalid_grant`), or are missing."""


class MFARequiredError(AuthenticationError):
    """A one-time code or an operation MFA token is required.

    Raised when logging in to an account with two-factor authentication without a code, and
    when an operation answers that it requires MFA checking (see `Whitebird.mfa_token`).
    """


class PermissionDeniedError(APIError):
    """The account is not allowed to perform the operation (HTTP 403)."""


class NotFoundError(APIError):
    """The resource does not exist (HTTP 404)."""


class ConflictError(APIError):
    """The request conflicts with the current state of the resource (HTTP 409)."""


class RateLimitError(APIError):
    """Too many requests (HTTP 429).

    Attributes:
        retry_after: Seconds to wait before retrying, if the server said so.
    """

    def __init__(self, message: str, *, retry_after: float | None = None, **details: Any) -> None:
        super().__init__(message, **details)
        self.retry_after = retry_after


class ServerError(APIError):
    """Whitebird failed to process the request (HTTP 5xx)."""


_BY_STATUS: dict[int, type[APIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
}


def error_from_response(response: httpx.Response) -> APIError:
    """Builds the exception matching an error response; the body must have been read."""
    body = _decode(response)
    details: dict[str, Any] = {
        "status_code": response.status_code,
        "body": body,
        "response": response,
    }
    message = response.reason_phrase or f"HTTP {response.status_code}"
    error_code: str | None = None
    if isinstance(body, dict):
        code = body.get("code")
        details["code"] = code if isinstance(code, int) else None
        details["fields"] = body.get("fields") if isinstance(body.get("fields"), list) else None
        error_code = _error_code(body)
        message = str(body.get("message") or body.get("error_description") or message)
    elif isinstance(body, str) and body.strip():
        message = body.strip()
    details["error_code"] = error_code

    if error_code in _VERIFICATIONS:
        return VerificationRequiredError(message, kind=_VERIFICATIONS[error_code], **details)
    if error_code == "MFA_CHECKING_REQUIRED" or "requires mfa checking" in message.lower():
        return MFARequiredError(message, **details)
    if error_code == "invalid_grant":
        return AuthenticationError(message, **details)
    if response.status_code == 429:
        return RateLimitError(message, retry_after=retry_after(response), **details)
    if response.status_code >= 500:
        return ServerError(message, **details)
    return _BY_STATUS.get(response.status_code, APIError)(message, **details)


def retry_after(response: httpx.Response) -> float | None:
    """Reads the `Retry-After` header (seconds or HTTP date) as a delay in seconds."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


def _decode(response: httpx.Response) -> Any:
    try:
        return _json.loads(response.content)
    except ValueError:
        return response.text


def _error_code(body: dict[str, Any]) -> str | None:
    status = body.get("status")
    if isinstance(status, str):
        return status
    error = body.get("error")
    # Spring's default error body uses `error` for the reason phrase; OAuth uses snake_case codes.
    if isinstance(error, str) and _OAUTH_ERROR.fullmatch(error):
        return error
    return None
