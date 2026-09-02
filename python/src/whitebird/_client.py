"""The synchronous and asynchronous clients."""

from __future__ import annotations

from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self, TypeVar, overload

import httpx
from typing_extensions import Unpack

from ._auth import AsyncAuthenticator, Credentials, SyncAuthenticator
from ._config import Config
from ._engine import AsyncEngine, SyncEngine
from ._generated.resources import AsyncResources, Resources
from ._generated.services import Environment
from ._http import Envelope
from .tokens import MemoryTokenStore, TokenStore
from .types import CodeProvider, FileContent, RequestOptions

T = TypeVar("T")

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2


class _Client:
    _engine: SyncEngine | AsyncEngine

    @property
    def mfa_token(self) -> str | None:
        """Operation MFA token, sent as `x-mfa-token` with every request while set.

        Sensitive operations such as withdrawals answer with `MFARequiredError` until a valid
        token is set. Assign None to stop sending it.
        """
        return self._engine.mfa_token

    @mfa_token.setter
    def mfa_token(self, token: str | None) -> None:
        self._engine.mfa_token = token


class Whitebird(_Client, Resources):
    """Synchronous client of the Whitebird API.

    Operations are grouped by resource, e.g. `client.brokerage.otc_orders.create(...)`.
    The client logs in on the first call that needs a token and refreshes the token before it
    expires; public endpoints work without credentials.

    Args:
        username: E-mail of the account, needed to log in with a password.
        password: Password of the account.
        otp: One-time codes for accounts with two-factor authentication: a static code, a
            callable returning a fresh code, or `whitebird.otp.TOTP`.
        environment: `"production"`, or `"demo"` for the sandbox trading contour.
        base_urls: Base URL overrides by service key, e.g. `{"trading": "https://..."}`.
        token_store: Where the session is kept, in memory by default. A `FileTokenStore` keeps
            it across runs.
        timeout: Default timeout of a request in seconds, or an `httpx.Timeout`.
        max_retries: How many times idempotent requests are retried after network errors and
            HTTP 429, 502, 503 or 504.
        http_client: An `httpx.Client` to send requests with (proxies, certificates, ...). It
            is left open by `close()`.

    Examples:
        ```python
        from whitebird import Whitebird
        from whitebird.otp import TOTP

        with Whitebird("me@example.com", "password", otp=TOTP("BASE32SECRET")) as client:
            for order in client.brokerage.otc_orders.list(size=100):
                print(order.uuid, order.status)
        ```
    """

    _engine: SyncEngine

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        otp: CodeProvider | None = None,
        environment: Environment = "production",
        base_urls: Mapping[str, str] | None = None,
        token_store: TokenStore | None = None,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx.Client | None = None,
    ) -> None:
        config = Config.create(environment, base_urls, timeout, max_retries)
        self._http = http_client or httpx.Client()
        self._owns_http = http_client is None
        self._auth = SyncAuthenticator(
            self._http,
            config,
            Credentials(username, password, otp),
            token_store or MemoryTokenStore(),
        )
        self._engine = SyncEngine(self._http, config, self._auth)

    def login(self, code: str | None = None) -> None:
        """Logs in with the password now rather than on the first authenticated call.

        Args:
            code: One-time code; by default one is taken from `otp`.

        Raises:
            MFARequiredError: Two-factor authentication is enabled and no code is available.
            AuthenticationError: The credentials or the code were rejected.
        """
        self._auth.login(code)

    @overload
    def request(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = ...,
        params: Mapping[str, object] | None = ...,
        json: object = ...,
        data: Mapping[str, object] | None = ...,
        files: Mapping[str, FileContent | None] | None = ...,
        headers: Mapping[str, str | None] | None = ...,
        authenticated: bool = ...,
        envelope: Envelope | None = ...,
        cast_to: type[T],
        **options: Unpack[RequestOptions],
    ) -> T: ...

    @overload
    def request(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = ...,
        params: Mapping[str, object] | None = ...,
        json: object = ...,
        data: Mapping[str, object] | None = ...,
        files: Mapping[str, FileContent | None] | None = ...,
        headers: Mapping[str, str | None] | None = ...,
        authenticated: bool = ...,
        envelope: Envelope | None = ...,
        cast_to: None = ...,
        **options: Unpack[RequestOptions],
    ) -> Any: ...

    def request(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = None,
        params: Mapping[str, object] | None = None,
        json: object = None,
        data: Mapping[str, object] | None = None,
        files: Mapping[str, FileContent | None] | None = None,
        headers: Mapping[str, str | None] | None = None,
        authenticated: bool = True,
        envelope: Envelope | None = None,
        cast_to: Any = None,
        **options: Unpack[RequestOptions],
    ) -> Any:
        """Calls any endpoint with the client's authentication, retries and error handling.

        Meant for endpoints this version of the SDK does not cover yet. Keys whose value is
        None are left out of `params`, `headers`, `data`, `files` and of a mapping `json`.

        Args:
            service: Service key, e.g. `"trading"` or `"market_data"`.
            method: HTTP method.
            path: Path from the service root, with `{name}` placeholders for `path_params`.
            path_params: Values of the path placeholders; they are URL-quoted.
            params: Query parameters; list values are repeated.
            json: JSON body; models are written with their wire names.
            data: Form fields, sent as multipart together with `files` if there are any.
            files: Files to upload as multipart/form-data.
            headers: Additional headers.
            authenticated: Whether to send the bearer token, logging in first if needed.
            envelope: `"retcode"` to unwrap `{"retCode", "retMsg", "result"}` responses.
            cast_to: Type to validate the response against, e.g. a model or `list[Model]`.
            **options: Per-request options, see `RequestOptions`.

        Returns:
            The response validated against `cast_to`; without it the decoded JSON (non-integer
            numbers as `Decimal`), the text of a non-JSON response, or None for an empty body.

        Examples:
            ```python
            instruments = client.request(
                "market_data",
                "GET",
                "/api/v1/market/instruments",
                authenticated=False,
                envelope="retcode",
            )
            ```
        """
        return self._engine.request(
            service,
            method,
            path,
            path_params=path_params,
            params=params,
            headers=headers,
            json=json,
            data=data,
            files=files,
            authenticated=authenticated,
            envelope=envelope,
            cast_to=cast_to,
            options=options,
        )

    def close(self) -> None:
        """Closes the HTTP connections, unless the HTTP client was passed in."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class AsyncWhitebird(_Client, AsyncResources):
    """Asynchronous client of the Whitebird API.

    Mirrors `Whitebird`: methods are coroutines, paginated methods return an `AsyncPager` and
    streams an `AsyncEventStream`. Concurrent tasks share one login and one token refresh.

    Args:
        username: E-mail of the account, needed to log in with a password.
        password: Password of the account.
        otp: One-time codes for accounts with two-factor authentication: a static code, a
            callable returning a fresh code, or `whitebird.otp.TOTP`.
        environment: `"production"`, or `"demo"` for the sandbox trading contour.
        base_urls: Base URL overrides by service key, e.g. `{"trading": "https://..."}`.
        token_store: Where the session is kept, in memory by default. A `FileTokenStore` keeps
            it across runs.
        timeout: Default timeout of a request in seconds, or an `httpx.Timeout`.
        max_retries: How many times idempotent requests are retried after network errors and
            HTTP 429, 502, 503 or 504.
        http_client: An `httpx.AsyncClient` to send requests with. It is left open by
            `aclose()`.

    Examples:
        ```python
        from whitebird import AsyncWhitebird

        async with AsyncWhitebird("me@example.com", "password", otp="123456") as client:
            async for order in client.brokerage.otc_orders.list(size=100):
                print(order.uuid, order.status)
        ```
    """

    _engine: AsyncEngine

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        otp: CodeProvider | None = None,
        environment: Environment = "production",
        base_urls: Mapping[str, str] | None = None,
        token_store: TokenStore | None = None,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        config = Config.create(environment, base_urls, timeout, max_retries)
        self._http = http_client or httpx.AsyncClient()
        self._owns_http = http_client is None
        self._auth = AsyncAuthenticator(
            self._http,
            config,
            Credentials(username, password, otp),
            token_store or MemoryTokenStore(),
        )
        self._engine = AsyncEngine(self._http, config, self._auth)

    async def login(self, code: str | None = None) -> None:
        """Logs in with the password now rather than on the first authenticated call.

        Args:
            code: One-time code; by default one is taken from `otp`.

        Raises:
            MFARequiredError: Two-factor authentication is enabled and no code is available.
            AuthenticationError: The credentials or the code were rejected.
        """
        await self._auth.login(code)

    @overload
    async def request(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = ...,
        params: Mapping[str, object] | None = ...,
        json: object = ...,
        data: Mapping[str, object] | None = ...,
        files: Mapping[str, FileContent | None] | None = ...,
        headers: Mapping[str, str | None] | None = ...,
        authenticated: bool = ...,
        envelope: Envelope | None = ...,
        cast_to: type[T],
        **options: Unpack[RequestOptions],
    ) -> T: ...

    @overload
    async def request(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = ...,
        params: Mapping[str, object] | None = ...,
        json: object = ...,
        data: Mapping[str, object] | None = ...,
        files: Mapping[str, FileContent | None] | None = ...,
        headers: Mapping[str, str | None] | None = ...,
        authenticated: bool = ...,
        envelope: Envelope | None = ...,
        cast_to: None = ...,
        **options: Unpack[RequestOptions],
    ) -> Any: ...

    async def request(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = None,
        params: Mapping[str, object] | None = None,
        json: object = None,
        data: Mapping[str, object] | None = None,
        files: Mapping[str, FileContent | None] | None = None,
        headers: Mapping[str, str | None] | None = None,
        authenticated: bool = True,
        envelope: Envelope | None = None,
        cast_to: Any = None,
        **options: Unpack[RequestOptions],
    ) -> Any:
        """Calls any endpoint with the client's authentication, retries and error handling.

        See `Whitebird.request`.
        """
        return await self._engine.request(
            service,
            method,
            path,
            path_params=path_params,
            params=params,
            headers=headers,
            json=json,
            data=data,
            files=files,
            authenticated=authenticated,
            envelope=envelope,
            cast_to=cast_to,
            options=options,
        )

    async def aclose(self) -> None:
        """Closes the HTTP connections, unless the HTTP client was passed in."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
