"""Exceptions raised by the clients."""

from __future__ import annotations

import math
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
    "CaptchaRequiredError",
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
# Machine codes, unlike the reason phrases ("Bad Request") Spring puts in `error`.
_MACHINE_CODE = re.compile(r"[a-z_]+|[A-Z_]+")
# How Whitebird refuses an operation that needs an operation MFA token, in `message` or `status`.
_MFA_REFUSAL = "requires mfa checking"
# Messages of the token endpoint that are not about the credentials (sign-in form of the web app).
_USER_BLOCKED = "Username is blocked"
_TOO_MANY_REQUESTS = "Too many requests"
# Longest text body kept as the message of an error.
_MAX_TEXT = 500


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
        sub_status: Refinement of `error_code` sent by some services as `subStatus`.
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
        sub_status: str | None = None,
        fields: list[Any] | None = None,
        body: Any = None,
        response: httpx.Response | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.error_code = error_code
        self.sub_status = sub_status
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
    when an operation answers that it requires MFA checking (see `Whitebird.confirm_operation`).
    """


class CaptchaRequiredError(AuthenticationError):
    """Logging in needs a solved captcha.

    Raised when an account without two-factor authentication logs in and the client has no
    `captcha` solver, or when the answers of the solver were rejected three times.
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


def error_from_response(response: httpx.Response, *, login: bool = False) -> APIError:
    """Builds the exception matching an error response; the body must have been read.

    Args:
        response: The error response.
        login: The response answers a password or refresh grant of the token endpoint: a
            rejection that mentions a captcha means `CaptchaRequiredError`, and other HTTP 400
            and 401 answers mean the credentials or the code were wrong.
    """
    body = _decode(response)
    details: dict[str, Any] = {
        "status_code": response.status_code,
        "body": body,
        "response": response,
    }
    message = response.reason_phrase or f"HTTP {response.status_code}"
    error_code: str | None = None
    if isinstance(body, dict):
        code = body.get("code", body.get("retCode"))
        details["code"] = code if isinstance(code, int) and not isinstance(code, bool) else None
        details["fields"] = body.get("fields") if isinstance(body.get("fields"), list) else None
        sub_status = body.get("subStatus")
        details["sub_status"] = sub_status if isinstance(sub_status, str) else None
        error_code = _error_code(body)
        text = body.get("message") or body.get("retMsg") or body.get("error_description")
        status = body.get("status")
        if not text and isinstance(status, str) and " " in status:
            text = status  # a sentence rather than a code, e.g. the MFA refusal
        message = str(text or message)
    elif isinstance(body, str) and body.strip():
        message = _truncate(body.strip())
    details["error_code"] = error_code

    if login and captcha_status(body):
        return CaptchaRequiredError(message, **details)
    if error_code in _VERIFICATIONS:
        return VerificationRequiredError(message, kind=_VERIFICATIONS[error_code], **details)
    if _MFA_REFUSAL in f"{message}\n{error_code or ''}".lower():
        return MFARequiredError(message, **details)
    if response.status_code == 429 or (login and message == _TOO_MANY_REQUESTS):
        return RateLimitError(message, retry_after=retry_after(response), **details)
    if error_code == "invalid_grant":
        return AuthenticationError(message, **details)
    if login and response.status_code in (400, 401) and message != _USER_BLOCKED:
        return AuthenticationError(message, **details)
    if response.status_code >= 500:
        return ServerError(message, **details)
    return _BY_STATUS.get(response.status_code, APIError)(message, **details)


def retry_after(response: httpx.Response) -> float | None:
    """Reads the `Retry-After` header (seconds or HTTP date) as a delay in seconds."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return max(seconds, 0.0) if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


def captcha_status(body: object) -> str | None:
    """Returns the `status` or `error` of an error body when it mentions a captcha."""
    if not isinstance(body, dict):
        return None
    for key in ("status", "error"):
        value = body.get(key)
        if isinstance(value, str) and "CAPTCHA" in value.upper():
            return value
    return None


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "…"


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
    if isinstance(error, str) and _MACHINE_CODE.fullmatch(error):
        return error
    return None
