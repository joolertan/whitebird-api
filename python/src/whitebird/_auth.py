"""OAuth password login and token refresh, renewed by a single caller at a time."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ._config import USER_AGENT, Config
from ._exceptions import (
    APIError,
    AuthenticationError,
    MFARequiredError,
    TransportError,
    error_from_response,
)
from ._generated.services import OAUTH_BASIC, OAUTH_SERVICE, OAUTH_TOKEN_PATH
from .tokens import Tokens, TokenStore
from .types import CodeProvider

MFA_CHECK_PATH = "/api/v1/client/mfa"
REFRESH_MARGIN = 30.0

_MFA_REQUIRED = (
    "Two-factor authentication is enabled for this account: pass otp= to the client "
    "(a code, a callable or whitebird.otp.TOTP) or call login(code=...)"
)

logger = logging.getLogger("whitebird")


@dataclass(frozen=True)
class Credentials:
    """What the client may use to log in on its own."""

    username: str | None = None
    password: str | None = None
    otp: CodeProvider | None = None

    @property
    def can_login(self) -> bool:
        return bool(self.username and self.password)

    def code(self) -> str | None:
        """Returns a one-time code from the configured provider, if any."""
        if self.otp is None or isinstance(self.otp, str):
            return self.otp
        return self.otp()


class _Authenticator:
    """Session state and OAuth requests shared by the sync and async authenticators."""

    def __init__(
        self,
        http: httpx.Client | httpx.AsyncClient,
        config: Config,
        credentials: Credentials,
        store: TokenStore,
    ) -> None:
        self._build_request = http.build_request
        self._config = config
        self._credentials = credentials
        self._store = store
        self._tokens = store.load()

    def _valid_token(self) -> str | None:
        tokens = self._tokens
        if tokens is None or tokens.expires_within(REFRESH_MARGIN):
            return None
        return tokens.access_token

    def _needs_renewal(self, rejected: str) -> bool:
        # False when another caller renewed the session since `rejected` was handed out.
        return self._tokens is None or self._tokens.access_token == rejected

    def _refresh_request(self) -> httpx.Request | None:
        refresh_token = self._tokens.refresh_token if self._tokens else None
        if not refresh_token:
            return None
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def _password_request(self, code: str | None) -> httpx.Request:
        username, password = self._username(), self._credentials.password
        form = {"grant_type": "password", "username": username, "password": password or ""}
        if code:
            form["code"] = code
        return self._token_request(form)

    def _mfa_check_request(self) -> httpx.Request:
        return self._build_request(
            "POST",
            self._config.url(OAUTH_SERVICE, MFA_CHECK_PATH),
            json={"username": self._username()},
            headers={"User-Agent": USER_AGENT},
            timeout=self._config.timeout,
        )

    def _token_request(self, form: dict[str, str]) -> httpx.Request:
        return self._build_request(
            "POST",
            self._config.url(OAUTH_SERVICE, OAUTH_TOKEN_PATH),
            data=form,
            headers={"Authorization": f"Basic {OAUTH_BASIC}", "User-Agent": USER_AGENT},
            timeout=self._config.timeout,
        )

    def _username(self) -> str:
        if not self._credentials.can_login:
            raise AuthenticationError(
                "Not logged in: pass username and password to the client, "
                "or a token store that holds a session"
            )
        return (self._credentials.username or "").lower()

    def _accept(self, response: httpx.Response, *, code_sent: bool) -> Tokens:
        tokens = _parse_tokens(response, code_sent=code_sent)
        self._tokens = tokens
        self._store.save(tokens)
        return tokens


class SyncAuthenticator(_Authenticator):
    """Provides access tokens to the sync engine; thread-safe."""

    def __init__(
        self, http: httpx.Client, config: Config, credentials: Credentials, store: TokenStore
    ) -> None:
        super().__init__(http, config, credentials, store)
        self._http = http
        self._lock = threading.Lock()

    def token(self) -> str:
        """Returns a valid access token, logging in or refreshing first when needed."""
        token = self._valid_token()
        if token is None:
            with self._lock:
                token = self._valid_token() or self._renew().access_token
        return token

    def refresh(self, rejected: str) -> None:
        """Renews the session after `rejected` got a 401, unless another call already did."""
        with self._lock:
            if self._needs_renewal(rejected):
                self._renew()

    def login(self, code: str | None = None) -> None:
        """Logs in with the password, even if a session exists."""
        with self._lock:
            self._login(code)

    def _renew(self) -> Tokens:
        request = self._refresh_request()
        if request is not None:
            try:
                return self._accept(self._send(request), code_sent=False)
            except AuthenticationError:
                if not self._credentials.can_login:
                    raise
                logger.info("Refresh token rejected, logging in again")
        return self._login(None)

    def _login(self, code: str | None) -> Tokens:
        code = code or self._credentials.code()
        request = self._password_request(code)
        if code is None and self._mfa_enabled():
            raise MFARequiredError(_MFA_REQUIRED)
        return self._accept(self._send(request), code_sent=code is not None)

    def _mfa_enabled(self) -> bool:
        try:
            response = self._send(self._mfa_check_request())
        except APIError:
            return False
        return _mfa_enabled(response)

    def _send(self, request: httpx.Request) -> httpx.Response:
        try:
            response = self._http.send(request)
        except httpx.TransportError as error:
            raise TransportError(str(error) or type(error).__name__) from error
        if response.is_error:
            raise error_from_response(response)
        return response


class AsyncAuthenticator(_Authenticator):
    """Provides access tokens to the async engine; safe for concurrent tasks."""

    def __init__(
        self, http: httpx.AsyncClient, config: Config, credentials: Credentials, store: TokenStore
    ) -> None:
        super().__init__(http, config, credentials, store)
        self._http = http
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        """Returns a valid access token, logging in or refreshing first when needed."""
        token = self._valid_token()
        if token is None:
            async with self._lock:
                token = self._valid_token() or (await self._renew()).access_token
        return token

    async def refresh(self, rejected: str) -> None:
        """Renews the session after `rejected` got a 401, unless another task already did."""
        async with self._lock:
            if self._needs_renewal(rejected):
                await self._renew()

    async def login(self, code: str | None = None) -> None:
        """Logs in with the password, even if a session exists."""
        async with self._lock:
            await self._login(code)

    async def _renew(self) -> Tokens:
        request = self._refresh_request()
        if request is not None:
            try:
                return self._accept(await self._send(request), code_sent=False)
            except AuthenticationError:
                if not self._credentials.can_login:
                    raise
                logger.info("Refresh token rejected, logging in again")
        return await self._login(None)

    async def _login(self, code: str | None) -> Tokens:
        code = code or self._credentials.code()
        request = self._password_request(code)
        if code is None and await self._mfa_enabled():
            raise MFARequiredError(_MFA_REQUIRED)
        return self._accept(await self._send(request), code_sent=code is not None)

    async def _mfa_enabled(self) -> bool:
        try:
            response = await self._send(self._mfa_check_request())
        except APIError:
            return False
        return _mfa_enabled(response)

    async def _send(self, request: httpx.Request) -> httpx.Response:
        try:
            response = await self._http.send(request)
        except httpx.TransportError as error:
            raise TransportError(str(error) or type(error).__name__) from error
        if response.is_error:
            raise error_from_response(response)
        return response


def _parse_tokens(response: httpx.Response, *, code_sent: bool) -> Tokens:
    body = _json_object(response)
    access_token = body.get("access_token")
    if isinstance(access_token, str) and access_token:
        refresh_token = body.get("refresh_token")
        return Tokens(
            access_token=access_token,
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
            expires_at=_expiry(access_token, body.get("expires_in")),
        )
    details: dict[str, Any] = {
        "status_code": response.status_code,
        "body": body,
        "response": response,
    }
    if body.get("mfaEnabled"):
        if code_sent:
            raise AuthenticationError("The one-time code was rejected", **details)
        raise MFARequiredError(_MFA_REQUIRED, **details)
    raise AuthenticationError("The token endpoint answered without an access token", **details)


def _mfa_enabled(response: httpx.Response) -> bool:
    return bool(_json_object(response).get("mfaEnabled"))


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _expiry(access_token: str, expires_in: object) -> float | None:
    try:
        payload = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (IndexError, KeyError, TypeError, ValueError):
        pass
    if isinstance(expires_in, int | float) and not isinstance(expires_in, bool):
        return time.time() + expires_in
    return None
