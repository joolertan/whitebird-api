"""Request preparation, response decoding and retry policy, shared by the sync and async engines."""

from __future__ import annotations

import functools
import random
import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from . import _json
from ._config import Config
from ._exceptions import APIError, retry_after
from .types import RequestOptions

Envelope = Literal["retcode"]
_T = TypeVar("_T")

MFA_HEADER = "x-mfa-token"
RETRY_STATUSES = frozenset({429, 502, 503, 504})
# Methods that only read: retried after any network error, timeout, 429, 502, 503 or 504.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Errors raised before a request reached the server: any request can be sent again after them.
UNSENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
MAX_RETRY_DELAY = 60.0
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 8.0
_PATH_PARAM = re.compile(r"\{([^}]+)\}")
_TEXT_PLAIN = "text/plain"


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
    files: Mapping[str, object] | None,
    content_types: Mapping[str, str] | None,
    options: RequestOptions,
    mfa_token: str | None,
) -> dict[str, Any]:
    """Returns the keyword arguments of `httpx.Client.build_request` for an API call.

    Keys whose value is None are left out of `params`, `headers`, `data`, `files` and of a
    mapping passed as `json`. A `json` mapping or `data` is sent even when empty. With `files`,
    the body is multipart: `data` fields and `files` become parts (see `multipart_parts`).
    """
    request_headers = config.headers()
    if mfa_token:
        request_headers[MFA_HEADER] = mfa_token
    request_headers.update(_present(headers or {}))
    content = None
    parts = None
    if json is not None:
        content = _json.dumps(_present(json) if isinstance(json, Mapping) else json)
        request_headers["Content-Type"] = "application/json"
    if files is not None:
        parts = multipart_parts({**(data or {}), **files}, content_types or {}) or None
    elif data is not None:
        content = urlencode(_fields(data) or {}, doseq=True)
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request_headers.update(options.get("extra_headers", {}))
    return {
        "method": method,
        "url": config.url(service, format_path(path, path_params or {})),
        "params": _fields(params),
        "headers": request_headers,
        "content": content,
        "files": parts,
        "timeout": options.get("timeout", config.timeout),
    }


def multipart_parts(
    values: Mapping[str, object], content_types: Mapping[str, str]
) -> list[tuple[str, Any]]:
    """Turns field values into multipart parts, for `httpx` `files`.

    A file (bytes, a binary file or a `(filename, content[, type])` tuple) is a file part, a
    model or a mapping a JSON part, any other value a text part. A list or tuple is sent as one
    part per item under the same name, unless its content type is JSON. `content_types` sets
    the media type of a field's parts; a JSON one encodes values that are not strings as JSON.
    A `text/plain` one sends models and mappings as JSON text in a plain text part, without a
    content type, as a browser sends a string appended to a form.
    """
    parts: list[tuple[str, Any]] = []
    for name, value in _present(values).items():
        content_type = content_types.get(name)
        repeated = not _is_file(value) and not _is_json(content_type)
        items = value if isinstance(value, list | tuple) and repeated else [value]
        parts += [(name, _part(item, content_type)) for item in items]
    return parts


def operation_mfa_token(response: httpx.Response) -> str:
    """Reads the token issued by the operation MFA endpoint.

    The token is the `mfaToken` field of the body, a body that is a bare JSON or plain string,
    or the `x-mfa-token` response header.
    """
    try:
        body: Any = _json.loads(response.content)
    except ValueError:
        body = response.text
    token = body.get("mfaToken") if isinstance(body, dict) else body
    if isinstance(token, str) and token.strip():
        return token.strip()
    header: str = response.headers.get(MFA_HEADER, "").strip()
    if header:
        return header
    raise APIError(
        "The operation MFA endpoint answered without a token",
        status_code=response.status_code,
        body=body,
        response=response,
    )


def format_path(path: str, values: Mapping[str, object]) -> str:
    """Substitutes `{name}` placeholders with URL-quoted values.

    Raises:
        ValueError: A value is empty, `.` or `..`, which would change the path of the request.
    """

    def substitute(match: re.Match[str]) -> str:
        value = _text(values[match[1]])
        if value in ("", ".", ".."):
            raise ValueError(f"path parameter {match[1]!r} cannot be {value!r}")
        return quote(value, safe="")

    return _PATH_PARAM.sub(substitute, path)


def is_idempotent(method: str, options: RequestOptions) -> bool:
    """Tells whether a request may be sent again after any transient failure."""
    return options.get("idempotent", method.upper() in IDEMPOTENT_METHODS)


def is_retryable_error(error: httpx.TransportError, idempotent: bool) -> bool:
    """Tells whether a request that failed with `error` may be sent again.

    A request that does not only read is retried only when it surely did not reach the server.
    """
    return idempotent or isinstance(error, UNSENT_ERRORS)


def is_retryable_status(status_code: int, idempotent: bool) -> bool:
    """Tells whether a request answered with `status_code` may be sent again.

    HTTP 429 means the request was not processed; 502, 503 and 504 are retried only for
    requests that only read, since the server may have processed them.
    """
    return status_code == 429 or (idempotent and status_code in RETRY_STATUSES)


def retry_delay(attempt: int, response: httpx.Response | None = None) -> float:
    """Seconds to wait before retry number `attempt` (from 0), honouring `Retry-After`."""
    requested = retry_after(response) if response is not None else None
    if requested is not None:
        return min(requested, MAX_RETRY_DELAY)
    return backoff(attempt, _BACKOFF_BASE, _BACKOFF_CAP)


def backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with jitter: `base * 2**attempt`, at most `cap`, never overflowing."""
    delay = cap if attempt >= 64 else min(base * 2.0 ** max(attempt, 0), cap)
    return delay * random.uniform(0.75, 1.0)


def decode(response: httpx.Response, cast_to: Any = None, envelope: Envelope | None = None) -> Any:
    """Decodes a successful response.

    `bytes` returns the raw body. Otherwise the JSON body is unwrapped from its envelope and,
    if `cast_to` is given, validated against it. A body that is not JSON is returned as text
    when `cast_to` is None or accepts a string, and so is any body when `cast_to` is `str`.
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
        return _decode_text(response, cast_to)
    if envelope == "retcode":
        data = _unwrap_retcode(data, response)
    if cast_to is None:
        return data
    if cast_to is str and envelope is None and not isinstance(data, str):
        # A string returned raw by Spring, e.g. digits that happen to be valid JSON.
        return response.text
    return validate(cast_to, data)


def _decode_text(response: httpx.Response, cast_to: Any) -> Any:
    if cast_to is None:
        return response.text
    try:
        return validate(cast_to, response.text)
    except ValidationError:
        raise APIError(
            "Whitebird answered with a body that is not valid JSON",
            status_code=response.status_code,
            body=response.text,
            response=response,
        ) from None


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
    """Unwraps `{"retCode", "retMsg", "result"}`; a code other than 0 or 200 is an error."""
    if not isinstance(data, dict):
        return data
    code = data.get("retCode", data.get("code"))
    if code is not None and code not in _RETCODE_SUCCESS:
        raise APIError(
            str(data.get("retMsg") or data.get("message") or f"retCode {code}"),
            status_code=response.status_code,
            code=code if isinstance(code, int) else None,
            body=data,
            response=response,
        )
    return data.get("result", data)


_RETCODE_SUCCESS = (0, 200, "0", "200")


def _part(value: object, content_type: str | None) -> tuple[str | None, Any, str | None]:
    if _is_file(value):
        if isinstance(value, tuple):
            return value if len(value) == 3 or content_type is None else (*value, content_type)
        return (Path(str(getattr(value, "name", "upload"))).name, value, content_type)
    if _is_text(content_type):
        return (None, _json.dumps(value) if _is_structured(value) else _text(value), None)
    if isinstance(value, str):
        return (None, value, content_type)
    if _is_json(content_type) or _is_structured(value):
        return (None, _json.dumps(value), content_type or "application/json")
    return (None, _text(value), content_type)


def _is_file(value: object) -> bool:
    if isinstance(value, tuple):
        return (
            len(value) in (2, 3)
            and (value[0] is None or isinstance(value[0], str))
            and _is_content(value[1])
        )
    return _is_content(value)


def _is_content(value: object) -> bool:
    return isinstance(value, bytes) or hasattr(value, "read")


def _is_json(content_type: str | None) -> bool:
    return content_type is not None and "json" in content_type


def _is_text(content_type: str | None) -> bool:
    return content_type is not None and content_type.split(";")[0].strip() == _TEXT_PLAIN


def _is_structured(value: object) -> bool:
    return isinstance(value, BaseModel | Mapping | list | tuple)


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
    if isinstance(value, date):
        return _json.format_time(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)
