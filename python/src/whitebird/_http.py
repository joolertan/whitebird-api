"""Request preparation, response decoding and retry policy, shared by the sync and async engines."""

from __future__ import annotations

import functools
import random
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, TypeVar
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter

from . import _json
from ._config import USER_AGENT, Config
from ._exceptions import APIError, retry_after
from .types import FileContent, RequestOptions

Envelope = Literal["retcode"]
_T = TypeVar("_T")

MFA_HEADER = "x-mfa-token"
RETRY_STATUSES = frozenset({429, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})
MAX_RETRY_DELAY = 60.0
_PATH_PARAM = re.compile(r"\{([^}]+)\}")


def prepare(
    config: Config,
    service: str,
    method: str,
    path: str,
    *,
    path_params: Mapping[str, object] | None,
    params: Mapping[str, object] | None,
    headers: Mapping[str, str | None] | None,
    json: object,
    data: Mapping[str, object] | None,
    files: Mapping[str, FileContent | None] | None,
    options: RequestOptions,
    mfa_token: str | None,
) -> dict[str, Any]:
    """Returns the keyword arguments of `httpx.Client.build_request` for an API call.

    Keys whose value is None are left out of `params`, `headers`, `data`, `files` and of a
    mapping passed as `json`.
    """
    request_headers = {"User-Agent": USER_AGENT}
    if mfa_token:
        request_headers[MFA_HEADER] = mfa_token
    request_headers.update(_present(headers or {}))
    content = None
    if json is not None:
        content = _json.dumps(_present(json) if isinstance(json, Mapping) else json)
        request_headers["Content-Type"] = "application/json"
    request_headers.update(options.get("extra_headers", {}))
    return {
        "method": method,
        "url": config.url(service, format_path(path, path_params or {})),
        "params": _fields(params),
        "headers": request_headers,
        "content": content,
        "data": _fields(data),
        "files": _present(files or {}) or None,
        "timeout": options.get("timeout", config.timeout),
    }


def format_path(path: str, values: Mapping[str, object]) -> str:
    """Substitutes `{name}` placeholders with URL-quoted values."""
    return _PATH_PARAM.sub(lambda match: quote(_text(values[match[1]]), safe=""), path)


def is_retryable(method: str, options: RequestOptions) -> bool:
    """Tells whether a failed request may be sent again."""
    return options.get("idempotent", method.upper() in IDEMPOTENT_METHODS)


def retry_delay(attempt: int, response: httpx.Response | None = None) -> float:
    """Seconds to wait before retry number `attempt` (from 0), honouring `Retry-After`."""
    requested = retry_after(response) if response is not None else None
    if requested is not None:
        return min(requested, MAX_RETRY_DELAY)
    return min(0.5 * 2.0**attempt, 8.0) * random.uniform(0.75, 1.0)


def decode(response: httpx.Response, cast_to: Any = None, envelope: Envelope | None = None) -> Any:
    """Decodes a successful response.

    `bytes` returns the raw body and `str` the text of non-JSON responses. Otherwise the JSON
    body is unwrapped from its envelope and, if `cast_to` is given, validated against it.
    """
    if cast_to is bytes:
        return response.content
    if not response.content:
        return None
    is_json = "json" in response.headers.get("content-type", "")
    if cast_to is str and not is_json:
        return response.text
    try:
        data = _json.loads(response.content)
    except ValueError:
        if cast_to is None:
            return response.text
        raise APIError(
            "Whitebird answered with a body that is not valid JSON",
            status_code=response.status_code,
            body=response.text,
            response=response,
        ) from None
    if envelope == "retcode":
        data = _unwrap_retcode(data, response)
    return data if cast_to is None else validate(cast_to, data)


def event_data(cast_to: Any, data: str) -> Any:
    """Decodes the data of a Server-Sent Event: JSON if possible, validated against `cast_to`."""
    if cast_to is str:
        return data
    try:
        value = _json.loads(data)
    except ValueError:
        value = data
    return value if cast_to is None else validate(cast_to, value)


def validate(cast_to: Any, data: Any) -> Any:
    """Validates decoded JSON against a type such as a model or `list[Model]`."""
    return _adapter(cast_to).validate_python(data)


@functools.cache
def _adapter(cast_to: Any) -> TypeAdapter[Any]:
    return TypeAdapter(cast_to)


def _unwrap_retcode(data: Any, response: httpx.Response) -> Any:
    if not isinstance(data, dict) or "retCode" not in data:
        return data
    if data["retCode"] != 0:
        code = data["retCode"]
        raise APIError(
            str(data.get("retMsg") or f"retCode {code}"),
            status_code=response.status_code,
            code=code if isinstance(code, int) else None,
            body=data,
            response=response,
        )
    return data.get("result")


def _present(values: Mapping[str, _T | None]) -> dict[str, _T]:
    return {key: value for key, value in values.items() if value is not None}


def _fields(values: Mapping[str, object] | None) -> dict[str, str | list[str]] | None:
    if not values:
        return None
    return {
        key: [_text(item) for item in value] if isinstance(value, list | tuple) else _text(value)
        for key, value in _present(values).items()
    }


def _text(value: object) -> str:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)
