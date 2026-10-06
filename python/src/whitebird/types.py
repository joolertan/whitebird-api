"""Types used in method signatures."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import IO, TypeAlias, TypedDict

import httpx

__all__ = [
    "AsyncCaptchaSolver",
    "Captcha",
    "CaptchaSolver",
    "CodeProvider",
    "FileContent",
    "RequestOptions",
]

FileContent: TypeAlias = (
    bytes
    | IO[bytes]
    | tuple[str | None, bytes | IO[bytes]]
    | tuple[str | None, bytes | IO[bytes], str]
)
"""File upload: raw bytes, a binary file, `(filename, content)` or `(filename, content, type)`."""

CodeProvider: TypeAlias = str | Callable[[], str]
"""Source of one-time codes: a static code, or a callable such as `whitebird.otp.TOTP`."""


@dataclass(frozen=True)
class Captcha:
    """Slider captcha of the sign-in form, handed to a captcha solver.

    The answer is the horizontal position, in pixels of the background image, at which the
    puzzle piece fills the gap.

    Attributes:
        request_id: Identifier of this captcha, sent back with the answer.
        background_image: PNG image with the gap.
        puzzle_piece: PNG image of the piece that fills the gap.
        position_y: Vertical position of the piece on the background, in pixels.
    """

    request_id: str
    background_image: bytes
    puzzle_piece: bytes
    position_y: int


CaptchaSolver: TypeAlias = Callable[[Captcha], int]
"""Returns the slider position that solves a captcha."""

AsyncCaptchaSolver: TypeAlias = Callable[[Captcha], int | Awaitable[int]]
"""Captcha solver of `AsyncWhitebird`: a plain function or a coroutine function."""


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
