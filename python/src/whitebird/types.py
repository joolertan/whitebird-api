"""Types used in method signatures."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import IO, TypeAlias, TypedDict

import httpx

__all__ = ["CodeProvider", "FileContent", "RequestOptions"]

FileContent: TypeAlias = (
    bytes
    | IO[bytes]
    | tuple[str | None, bytes | IO[bytes]]
    | tuple[str | None, bytes | IO[bytes], str]
)
"""File upload: raw bytes, a binary file, `(filename, content)` or `(filename, content, type)`."""

CodeProvider: TypeAlias = str | Callable[[], str]
"""Source of one-time codes: a static code, or a callable such as `whitebird.otp.TOTP`."""


class RequestOptions(TypedDict, total=False):
    """Per-request options, accepted as keyword arguments by every API method.

    Attributes:
        timeout: Timeout in seconds (or an `httpx.Timeout`) instead of the client default;
            None disables it.
        extra_headers: Headers to add to the request, overriding those set by the client.
        max_retries: Number of retries instead of the client default.
        idempotent: Allows retrying a request whose method is not idempotent, e.g. a POST
            that only reads data. GET, HEAD, PUT, DELETE and OPTIONS are always retried.
    """

    timeout: float | httpx.Timeout | None
    extra_headers: Mapping[str, str]
    max_retries: int
    idempotent: bool
