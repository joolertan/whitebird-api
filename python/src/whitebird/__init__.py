"""Unofficial client of the Whitebird crypto exchange API.

Examples:
    ```python
    from whitebird import Whitebird

    with Whitebird("me@example.com", "password") as client:
        print(client.brokerage.subaccounts.me())
    ```
"""

from ._client import AsyncWhitebird, Whitebird
from ._exceptions import (
    APIError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    MFARequiredError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
    TransportError,
    VerificationRequiredError,
    WhitebirdError,
)
from ._generated.services import Environment
from ._version import __version__
from .pagination import AsyncPager, Page, SyncPager
from .streaming import AsyncEventStream, EventStream, ServerSentEvent
from .tokens import FileTokenStore, MemoryTokenStore, Tokens, TokenStore
from .types import CodeProvider, FileContent, RequestOptions

__all__ = [
    "APIError",
    "AsyncEventStream",
    "AsyncPager",
    "AsyncWhitebird",
    "AuthenticationError",
    "BadRequestError",
    "CodeProvider",
    "ConflictError",
    "Environment",
    "EventStream",
    "FileContent",
    "FileTokenStore",
    "MFARequiredError",
    "MemoryTokenStore",
    "NotFoundError",
    "Page",
    "PermissionDeniedError",
    "RateLimitError",
    "RequestOptions",
    "ServerError",
    "ServerSentEvent",
    "SyncPager",
    "TokenStore",
    "Tokens",
    "TransportError",
    "VerificationRequiredError",
    "Whitebird",
    "WhitebirdError",
    "__version__",
]
