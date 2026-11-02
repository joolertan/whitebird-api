"""Sends API calls with authentication, retries, error mapping and response decoding."""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from collections.abc import Mapping
from typing import Any, Literal, TypeVar, overload

import httpx

from . import _http
from ._auth import AsyncAuthenticator, Credentials, SyncAuthenticator, clean_code
from ._config import Config
from ._exceptions import MFARequiredError, TransportError, error_from_response
from ._generated.services import OPERATION_MFA_SERVICE, OPERATION_MFA_TOKEN_PATH
from .pagination import AsyncPager, Page, SyncPager
from .streaming import AsyncEventStream, EventStream
from .types import RequestOptions

T = TypeVar("T")

logger = logging.getLogger("whitebird")

_STREAM_HEADERS = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}


SessionEffect = Literal["replace", "end"]
"""What a call does to the session: `replace` stores the token pair it answers with (a password
change), `end` forgets the session once it is done, whatever its outcome (a logout)."""


class _Engine:
    """State and request preparation shared by the sync and async engines."""

    _auth: SyncAuthenticator | AsyncAuthenticator

    def __init__(self, config: Config) -> None:
        self.config = config

    @property
    def _credentials(self) -> Credentials:
        # Read through the authenticator: a password change replaces its credentials.
        return self._auth.credentials

    @property
    def mfa_token(self) -> str | None:
        """Operation MFA token sent as `x-mfa-token`; kept with the session."""
        return self._auth.mfa_token

    @mfa_token.setter
    def mfa_token(self, token: str | None) -> None:
        self._auth.mfa_token = token

    def _prepare(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = None,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str | None] | None = None,
        json: object = None,
        data: Mapping[str, object] | None = None,
        files: Mapping[str, object] | None = None,
        content_types: Mapping[str, str] | None = None,
        options: RequestOptions,
    ) -> dict[str, Any]:
        return _http.prepare(
            self.config,
            service,
            method,
            path,
            path_params=path_params,
            params=params,
            headers=headers,
            json=json,
            data=data,
            files=files,
            content_types=content_types,
            options=options,
            mfa_token=self.mfa_token,
        )

    def _confirmation(self, operation: str, code: str | None) -> dict[str, Any]:
        code = clean_code(code)
        if code is None:
            raise MFARequiredError(
                f"Confirming the {operation} operation needs a one-time code: pass code= or "
                "configure otp on the client"
            )
        return self._prepare(
            OPERATION_MFA_SERVICE,
            "POST",
            OPERATION_MFA_TOKEN_PATH,
            json={"operation": operation, "mfaCode": code},
            options={},
        )

    def _retries(self, options: RequestOptions) -> int:
        return max(options.get("max_retries", self.config.max_retries), 0)

    def _session_end(self, session: SessionEffect | None) -> None:
        if session == "end":
            self._auth.end_session()

    def _session_replace(
        self,
        session: SessionEffect | None,
        response: httpx.Response,
        json: object,
        generation: int,
    ) -> None:
        if session == "replace":
            new_password = json.get("newPassword") if isinstance(json, Mapping) else None
            self._auth.replace_session(
                response, generation, new_password if isinstance(new_password, str) else None
            )

    def _stream_options(self, options: RequestOptions | None) -> RequestOptions:
        timeout = self.config.timeout
        if not isinstance(timeout, httpx.Timeout):
            timeout = httpx.Timeout(timeout)
        # Events may be minutes apart: only connecting, writing and pooling time out.
        defaults: RequestOptions = {
            "timeout": httpx.Timeout(
                connect=timeout.connect, read=None, write=timeout.write, pool=timeout.pool
            )
        }
        return {**defaults, **(options or {}), "max_retries": 0}


def _with_page(
    page_in: Literal["query", "body"],
    params: Mapping[str, object] | None,
    json: object,
    number: int,
) -> tuple[Mapping[str, object] | None, object]:
    if page_in == "query":
        return {**(params or {}), "page": number}, json
    if not isinstance(json, Mapping):
        raise TypeError("a body-paginated request needs a mapping body")
    return params, {**json, "page": number}


def _start_page(source: object) -> int:
    page = source.get("page") if isinstance(source, Mapping) else None
    return page if isinstance(page, int) else 0


def _transport_error(error: httpx.TransportError) -> TransportError:
    return TransportError(str(error) or type(error).__name__)


class SyncEngine(_Engine):
    """Performs API calls for `Whitebird`."""

    _auth: SyncAuthenticator

    def __init__(self, http: httpx.Client, config: Config, auth: SyncAuthenticator) -> None:
        super().__init__(config)
        self._http = http
        self._auth = auth
        self._confirming = threading.Lock()

    @overload
    def request(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = ...,
        params: Mapping[str, object] | None = ...,
        headers: Mapping[str, str | None] | None = ...,
        json: object = ...,
        data: Mapping[str, object] | None = ...,
        files: Mapping[str, object] | None = ...,
        content_types: Mapping[str, str] | None = ...,
        authenticated: bool = ...,
        envelope: _http.Envelope | None = ...,
        mfa_operation: str | None = ...,
        session: SessionEffect | None = ...,
        cast_to: type[T],
        options: RequestOptions | None = ...,
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
        headers: Mapping[str, str | None] | None = ...,
        json: object = ...,
        data: Mapping[str, object] | None = ...,
        files: Mapping[str, object] | None = ...,
        content_types: Mapping[str, str] | None = ...,
        authenticated: bool = ...,
        envelope: _http.Envelope | None = ...,
        mfa_operation: str | None = ...,
        session: SessionEffect | None = ...,
        cast_to: object = ...,
        options: RequestOptions | None = ...,
    ) -> Any: ...

    def request(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = None,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str | None] | None = None,
        json: object = None,
        data: Mapping[str, object] | None = None,
        files: Mapping[str, object] | None = None,
        content_types: Mapping[str, str] | None = None,
        authenticated: bool = True,
        envelope: _http.Envelope | None = None,
        mfa_operation: str | None = None,
        session: SessionEffect | None = None,
        cast_to: Any = None,
        options: RequestOptions | None = None,
    ) -> Any:
        """Sends a call and decodes its response (see `_http.prepare` and `_http.decode`).

        A call with an `mfa_operation` that Whitebird refuses for MFA is confirmed (see
        `confirm_operation`) and sent once more, if `otp` is configured. `session` applies the
        effect of the call on the session.
        """
        options = options or {}
        request = self._http.build_request(
            **self._prepare(
                service,
                method,
                path,
                path_params=path_params,
                params=params,
                headers=headers,
                json=json,
                data=data,
                files=files,
                content_types=content_types,
                options=options,
            )
        )
        generation = self._auth.generation
        try:
            if mfa_operation is None:
                response = self.send(request, authenticated=authenticated, options=options)
            else:
                response = self._send_confirmed(request, mfa_operation, options)
        finally:
            self._session_end(session)
        self._session_replace(session, response, json, generation)
        return _http.decode(response, cast_to, envelope)

    def paginate(
        self,
        service: str,
        method: str,
        path: str,
        *,
        cast_to: type[Page[T]],
        page_in: Literal["query", "body"] = "query",
        path_params: Mapping[str, object] | None = None,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str | None] | None = None,
        json: object = None,
        authenticated: bool = True,
        options: RequestOptions | None = None,
    ) -> SyncPager[T]:
        """Returns a pager that sends the call once per page, setting the `page` field."""

        def fetch(number: int) -> Page[T]:
            query, body = _with_page(page_in, params, json, number)
            page = self.request(
                service,
                method,
                path,
                path_params=path_params,
                params=query,
                headers=headers,
                json=body,
                authenticated=authenticated,
                cast_to=cast_to,
                options=options,
            )
            if page.number is None:
                page.number = number
            return page

        return SyncPager(fetch, _start_page(params if page_in == "query" else json))

    @overload
    def stream(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = ...,
        params: Mapping[str, object] | None = ...,
        headers: Mapping[str, str | None] | None = ...,
        authenticated: bool = ...,
        cast_to: type[T],
        options: RequestOptions | None = ...,
    ) -> EventStream[T]: ...

    @overload
    def stream(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = ...,
        params: Mapping[str, object] | None = ...,
        headers: Mapping[str, str | None] | None = ...,
        authenticated: bool = ...,
        cast_to: object = ...,
        options: RequestOptions | None = ...,
    ) -> EventStream[Any]: ...

    def stream(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = None,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str | None] | None = None,
        authenticated: bool = True,
        cast_to: Any = None,
        options: RequestOptions | None = None,
    ) -> EventStream[Any]:
        """Returns a Server-Sent Events stream that connects on first iteration."""
        stream_options = self._stream_options(options)

        def connect(last_event_id: str | None) -> httpx.Response:
            request = self._http.build_request(
                **self._prepare(
                    service,
                    method,
                    path,
                    path_params=path_params,
                    params=params,
                    headers={**_STREAM_HEADERS, "Last-Event-ID": last_event_id, **(headers or {})},
                    options=stream_options,
                )
            )
            return self.send(
                request, authenticated=authenticated, options=stream_options, stream=True
            )

        return EventStream(connect, functools.partial(_http.event_data, cast_to))

    def confirm_operation(self, operation: str, code: str | None = None) -> str:
        """Obtains an operation MFA token, sets it as `mfa_token` and returns it."""
        code = clean_code(code) or self._credentials.code()
        request = self._http.build_request(**self._confirmation(operation, code))
        response = self.send(request, authenticated=True, options={})
        token = _http.operation_mfa_token(response)
        self.mfa_token = token
        return token

    def _send_confirmed(
        self, request: httpx.Request, operation: str, options: RequestOptions
    ) -> httpx.Response:
        try:
            return self.send(request, authenticated=True, options=options)
        except MFARequiredError:
            if self._credentials.otp is None:
                raise
        rejected = request.headers.get(_http.MFA_HEADER)
        with self._confirming:
            # Calls refused together share one confirmation: the first one obtains the token.
            token = self.mfa_token
            if token is None or token == rejected:
                token = self.confirm_operation(operation)
        request.headers[_http.MFA_HEADER] = token
        return self.send(request, authenticated=True, options=options)

    def send(
        self,
        request: httpx.Request,
        *,
        authenticated: bool,
        options: RequestOptions,
        stream: bool = False,
    ) -> httpx.Response:
        """Sends a prepared request.

        Adds the bearer token, renews the session once on 401, and raises the matching
        `APIError` for an error answer. Requests that only read (GET, HEAD, OPTIONS, or marked
        `idempotent`) are retried after network errors, timeouts and HTTP 429, 502, 503 and 504;
        other requests only after HTTP 429 or when the connection could not be established.
        """
        retries = self._retries(options)
        idempotent = _http.is_idempotent(request.method, options)
        attempt = 0
        renewed = False
        while True:
            token = self._auth.token() if authenticated else None
            if token is not None:
                request.headers["Authorization"] = f"Bearer {token}"
            try:
                response = self._http.send(request, stream=stream)
            except httpx.TransportError as error:
                if attempt >= retries or not _http.is_retryable_error(error, idempotent):
                    raise _transport_error(error) from error
                delay = _http.retry_delay(attempt)
            else:
                if response.status_code == 401 and token is not None and not renewed:
                    response.close()
                    self._auth.refresh(token)
                    renewed = True
                    continue
                retry = _http.is_retryable_status(response.status_code, idempotent)
                if not retry or attempt >= retries:
                    if response.is_error:
                        response.read()
                        raise error_from_response(response)
                    return response
                delay = _http.retry_delay(attempt, response)
                response.close()
            logger.info("Retrying %s %s in %.1f s", request.method, request.url, delay)
            time.sleep(delay)
            attempt += 1


class AsyncEngine(_Engine):
    """Performs API calls for `AsyncWhitebird`."""

    _auth: AsyncAuthenticator

    def __init__(self, http: httpx.AsyncClient, config: Config, auth: AsyncAuthenticator) -> None:
        super().__init__(config)
        self._http = http
        self._auth = auth
        self._confirming = asyncio.Lock()

    @overload
    async def request(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = ...,
        params: Mapping[str, object] | None = ...,
        headers: Mapping[str, str | None] | None = ...,
        json: object = ...,
        data: Mapping[str, object] | None = ...,
        files: Mapping[str, object] | None = ...,
        content_types: Mapping[str, str] | None = ...,
        authenticated: bool = ...,
        envelope: _http.Envelope | None = ...,
        mfa_operation: str | None = ...,
        session: SessionEffect | None = ...,
        cast_to: type[T],
        options: RequestOptions | None = ...,
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
        headers: Mapping[str, str | None] | None = ...,
        json: object = ...,
        data: Mapping[str, object] | None = ...,
        files: Mapping[str, object] | None = ...,
        content_types: Mapping[str, str] | None = ...,
        authenticated: bool = ...,
        envelope: _http.Envelope | None = ...,
        mfa_operation: str | None = ...,
        session: SessionEffect | None = ...,
        cast_to: object = ...,
        options: RequestOptions | None = ...,
    ) -> Any: ...

    async def request(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = None,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str | None] | None = None,
        json: object = None,
        data: Mapping[str, object] | None = None,
        files: Mapping[str, object] | None = None,
        content_types: Mapping[str, str] | None = None,
        authenticated: bool = True,
        envelope: _http.Envelope | None = None,
        mfa_operation: str | None = None,
        session: SessionEffect | None = None,
        cast_to: Any = None,
        options: RequestOptions | None = None,
    ) -> Any:
        """Sends a call and decodes its response (see `_http.prepare` and `_http.decode`).

        A call with an `mfa_operation` that Whitebird refuses for MFA is confirmed (see
        `confirm_operation`) and sent once more, if `otp` is configured. `session` applies the
        effect of the call on the session.
        """
        options = options or {}
        request = self._http.build_request(
            **self._prepare(
                service,
                method,
                path,
                path_params=path_params,
                params=params,
                headers=headers,
                json=json,
                data=data,
                files=files,
                content_types=content_types,
                options=options,
            )
        )
        generation = self._auth.generation
        try:
            if mfa_operation is None:
                response = await self.send(request, authenticated=authenticated, options=options)
            else:
                response = await self._send_confirmed(request, mfa_operation, options)
        finally:
            self._session_end(session)
        self._session_replace(session, response, json, generation)
        return _http.decode(response, cast_to, envelope)

    def paginate(
        self,
        service: str,
        method: str,
        path: str,
        *,
        cast_to: type[Page[T]],
        page_in: Literal["query", "body"] = "query",
        path_params: Mapping[str, object] | None = None,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str | None] | None = None,
        json: object = None,
        authenticated: bool = True,
        options: RequestOptions | None = None,
    ) -> AsyncPager[T]:
        """Returns a pager that sends the call once per page, setting the `page` field."""

        async def fetch(number: int) -> Page[T]:
            query, body = _with_page(page_in, params, json, number)
            page = await self.request(
                service,
                method,
                path,
                path_params=path_params,
                params=query,
                headers=headers,
                json=body,
                authenticated=authenticated,
                cast_to=cast_to,
                options=options,
            )
            if page.number is None:
                page.number = number
            return page

        return AsyncPager(fetch, _start_page(params if page_in == "query" else json))

    @overload
    def stream(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = ...,
        params: Mapping[str, object] | None = ...,
        headers: Mapping[str, str | None] | None = ...,
        authenticated: bool = ...,
        cast_to: type[T],
        options: RequestOptions | None = ...,
    ) -> AsyncEventStream[T]: ...

    @overload
    def stream(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = ...,
        params: Mapping[str, object] | None = ...,
        headers: Mapping[str, str | None] | None = ...,
        authenticated: bool = ...,
        cast_to: object = ...,
        options: RequestOptions | None = ...,
    ) -> AsyncEventStream[Any]: ...

    def stream(
        self,
        service: str,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, object] | None = None,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str | None] | None = None,
        authenticated: bool = True,
        cast_to: Any = None,
        options: RequestOptions | None = None,
    ) -> AsyncEventStream[Any]:
        """Returns a Server-Sent Events stream that connects on first iteration."""
        stream_options = self._stream_options(options)

        async def connect(last_event_id: str | None) -> httpx.Response:
            request = self._http.build_request(
                **self._prepare(
                    service,
                    method,
                    path,
                    path_params=path_params,
                    params=params,
                    headers={**_STREAM_HEADERS, "Last-Event-ID": last_event_id, **(headers or {})},
                    options=stream_options,
                )
            )
            return await self.send(
                request, authenticated=authenticated, options=stream_options, stream=True
            )

        return AsyncEventStream(connect, functools.partial(_http.event_data, cast_to))

    async def confirm_operation(self, operation: str, code: str | None = None) -> str:
        """Obtains an operation MFA token, sets it as `mfa_token` and returns it."""
        code = clean_code(code) or await self._credentials.acode()
        request = self._http.build_request(**self._confirmation(operation, code))
        response = await self.send(request, authenticated=True, options={})
        token = _http.operation_mfa_token(response)
        self.mfa_token = token
        return token

    async def _send_confirmed(
        self, request: httpx.Request, operation: str, options: RequestOptions
    ) -> httpx.Response:
        try:
            return await self.send(request, authenticated=True, options=options)
        except MFARequiredError:
            if self._credentials.otp is None:
                raise
        rejected = request.headers.get(_http.MFA_HEADER)
        async with self._confirming:
            # Calls refused together share one confirmation: the first one obtains the token.
            token = self.mfa_token
            if token is None or token == rejected:
                token = await self.confirm_operation(operation)
        request.headers[_http.MFA_HEADER] = token
        return await self.send(request, authenticated=True, options=options)

    async def send(
        self,
        request: httpx.Request,
        *,
        authenticated: bool,
        options: RequestOptions,
        stream: bool = False,
    ) -> httpx.Response:
        """Sends a prepared request; see `SyncEngine.send`."""
        retries = self._retries(options)
        idempotent = _http.is_idempotent(request.method, options)
        attempt = 0
        renewed = False
        while True:
            token = await self._auth.token() if authenticated else None
            if token is not None:
                request.headers["Authorization"] = f"Bearer {token}"
            try:
                response = await self._http.send(request, stream=stream)
            except httpx.TransportError as error:
                if attempt >= retries or not _http.is_retryable_error(error, idempotent):
                    raise _transport_error(error) from error
                delay = _http.retry_delay(attempt)
            else:
                if response.status_code == 401 and token is not None and not renewed:
                    await response.aclose()
                    await self._auth.refresh(token)
                    renewed = True
                    continue
                retry = _http.is_retryable_status(response.status_code, idempotent)
                if not retry or attempt >= retries:
                    if response.is_error:
                        await response.aread()
                        raise error_from_response(response)
                    return response
                delay = _http.retry_delay(attempt, response)
                await response.aclose()
            logger.info("Retrying %s %s in %.1f s", request.method, request.url, delay)
            await asyncio.sleep(delay)
            attempt += 1
