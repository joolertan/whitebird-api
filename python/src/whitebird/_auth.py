"""OAuth password login and token refresh, renewed by a single caller at a time."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import threading
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

import httpx

from ._config import USER_AGENT, Config
from ._exceptions import (
    APIError,
    AuthenticationError,
    CaptchaRequiredError,
    MFARequiredError,
    TransportError,
    captcha_status,
    error_from_response,
)
from ._generated.services import (
    OAUTH_BASIC,
    OAUTH_CAPTCHA_PATH,
    OAUTH_MFA_STATUS_PATH,
    OAUTH_SERVICE,
    OAUTH_TOKEN_PATH,
)
from .tokens import Tokens, TokenStore
from .types import AsyncCaptchaSolver, AsyncCodeProvider, Captcha, CaptchaSolver

REFRESH_MARGIN = 30.0
CAPTCHA_ATTEMPTS = 3
INVALID_CAPTCHA = "INVALID_CAPTCHA"

_MFA_REQUIRED = (
    "Two-factor authentication is enabled for this account: pass otp= to the client "
    "(a code, a callable or whitebird.otp.TOTP) or call login(code=...)"
)
_CAPTCHA_REQUIRED = (
    "The account has no two-factor authentication, so Whitebird asks for a captcha at sign-in: "
    "pass captcha= a solver or enable two-factor authentication"
)

logger = logging.getLogger("whitebird")


@dataclass(frozen=True)
class Credentials:
    """What the client may use to log in on its own."""

    username: str | None = None
    password: str | None = None
    otp: AsyncCodeProvider | None = None

    @property
    def can_login(self) -> bool:
        return bool(self.username and self.password)

    def code(self) -> str | None:
        """Returns a one-time code from the configured provider, if any."""
        code = self._provide()
        if inspect.isawaitable(code):
            if inspect.iscoroutine(code):
                code.close()
            raise TypeError("otp returned an awaitable: coroutine functions need AsyncWhitebird")
        return code

    async def acode(self) -> str | None:
        """Returns a one-time code, awaiting it if the provider is a coroutine function."""
        code = self._provide()
        return await code if inspect.isawaitable(code) else code

    def _provide(self) -> str | Awaitable[str] | None:
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
        self.credentials = credentials
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

    def _needs_code(self, mfa_enabled: bool | None, *, has_solver: bool) -> bool:
        """Whether a password login sends a one-time code, given the account's 2FA status.

        Accounts without 2FA sign in through a captcha instead, so an unknown status means 2FA
        exactly when a code provider is configured.

        Raises:
            CaptchaRequiredError: The account has no 2FA and no captcha solver is configured.
        """
        if mfa_enabled is False and not has_solver:
            raise CaptchaRequiredError(_CAPTCHA_REQUIRED)
        return self.credentials.otp is not None if mfa_enabled is None else mfa_enabled

    def _password_request(self, code: str | None, answer: str | None = None) -> httpx.Request:
        username, password = self._username(), self.credentials.password
        form = {"grant_type": "password", "username": username, "password": password or ""}
        if code:
            form["code"] = code
        return self._token_request(form, {"captcha": answer} if answer else {})

    def _mfa_check_request(self) -> httpx.Request:
        return self._build_request(
            "POST",
            self._config.url(OAUTH_SERVICE, OAUTH_MFA_STATUS_PATH),
            json={"username": self._username()},
            headers={"User-Agent": USER_AGENT},
            timeout=self._config.timeout,
        )

    def _captcha_request(self, request_id: str) -> httpx.Request:
        return self._build_request(
            "GET",
            self._config.url(OAUTH_SERVICE, OAUTH_CAPTCHA_PATH),
            params={"requestId": request_id},
            headers={"User-Agent": USER_AGENT},
            timeout=self._config.timeout,
        )

    def _token_request(
        self, form: dict[str, str], headers: dict[str, str] | None = None
    ) -> httpx.Request:
        return self._build_request(
            "POST",
            self._config.url(OAUTH_SERVICE, OAUTH_TOKEN_PATH),
            data=form,
            headers={
                "Authorization": f"Basic {OAUTH_BASIC}",
                "User-Agent": USER_AGENT,
                **(headers or {}),
            },
            timeout=self._config.timeout,
        )

    def _username(self) -> str:
        if not self.credentials.can_login:
            raise AuthenticationError(
                "Not logged in: pass username and password to the client, "
                "or a token store that holds a session"
            )
        return (self.credentials.username or "").lower()

    def _accept(self, response: httpx.Response, *, code_sent: bool) -> Tokens:
        tokens = _parse_tokens(response, code_sent=code_sent)
        self._tokens = tokens
        self._store.save(tokens)
        return tokens


class SyncAuthenticator(_Authenticator):
    """Provides access tokens to the sync engine; thread-safe."""

    def __init__(
        self,
        http: httpx.Client,
        config: Config,
        credentials: Credentials,
        store: TokenStore,
        captcha: CaptchaSolver | None = None,
    ) -> None:
        super().__init__(http, config, credentials, store)
        self._http = http
        self._captcha = captcha
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
                if not self.credentials.can_login:
                    raise
                logger.info("Refresh token rejected, logging in again")
        return self._login(None)

    def _login(self, code: str | None) -> Tokens:
        if code is None and self._needs_code(
            self._mfa_status(), has_solver=self._captcha is not None
        ):
            code = self.credentials.code()
            if code is None:
                raise MFARequiredError(_MFA_REQUIRED)
        solver = self._captcha if code is None else None
        attempt = 1
        while True:
            answer = self._solve_captcha(solver) if solver else None
            request = self._password_request(code, answer)
            try:
                response = self._send(request, login=True)
            except CaptchaRequiredError as error:
                if not _retry_captcha(error, answer, attempt):
                    raise
            else:
                return self._accept(response, code_sent=code is not None)
            attempt += 1

    def _solve_captcha(self, solver: CaptchaSolver) -> str:
        request_id = str(uuid.uuid4())
        captcha = _parse_captcha(request_id, self._send(self._captcha_request(request_id)))
        return _captcha_answer(captcha, solver(captcha))

    def _mfa_status(self) -> bool | None:
        """Asks whether the account has 2FA enabled; `None` if the check fails."""
        try:
            return _mfa_enabled(self._send(self._mfa_check_request()))
        except APIError:
            return None

    def _send(self, request: httpx.Request, *, login: bool = False) -> httpx.Response:
        try:
            response = self._http.send(request)
        except httpx.TransportError as error:
            raise TransportError(str(error) or type(error).__name__) from error
        if response.is_error:
            raise error_from_response(response, login=login)
        return response


class AsyncAuthenticator(_Authenticator):
    """Provides access tokens to the async engine; safe for concurrent tasks."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        config: Config,
        credentials: Credentials,
        store: TokenStore,
        captcha: AsyncCaptchaSolver | None = None,
    ) -> None:
        super().__init__(http, config, credentials, store)
        self._http = http
        self._captcha = captcha
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
                if not self.credentials.can_login:
                    raise
                logger.info("Refresh token rejected, logging in again")
        return await self._login(None)

    async def _login(self, code: str | None) -> Tokens:
        if code is None and self._needs_code(
            await self._mfa_status(), has_solver=self._captcha is not None
        ):
            code = await self.credentials.acode()
            if code is None:
                raise MFARequiredError(_MFA_REQUIRED)
        solver = self._captcha if code is None else None
        attempt = 1
        while True:
            answer = await self._solve_captcha(solver) if solver else None
            request = self._password_request(code, answer)
            try:
                response = await self._send(request, login=True)
            except CaptchaRequiredError as error:
                if not _retry_captcha(error, answer, attempt):
                    raise
            else:
                return self._accept(response, code_sent=code is not None)
            attempt += 1

    async def _solve_captcha(self, solver: AsyncCaptchaSolver) -> str:
        request_id = str(uuid.uuid4())
        captcha = _parse_captcha(request_id, await self._send(self._captcha_request(request_id)))
        position = solver(captcha)
        if inspect.isawaitable(position):
            position = await position
        return _captcha_answer(captcha, position)

    async def _mfa_status(self) -> bool | None:
        """Asks whether the account has 2FA enabled; `None` if the check fails."""
        try:
            return _mfa_enabled(await self._send(self._mfa_check_request()))
        except APIError:
            return None

    async def _send(self, request: httpx.Request, *, login: bool = False) -> httpx.Response:
        try:
            response = await self._http.send(request)
        except httpx.TransportError as error:
            raise TransportError(str(error) or type(error).__name__) from error
        if response.is_error:
            raise error_from_response(response, login=login)
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


def _parse_captcha(request_id: str, response: httpx.Response) -> Captcha:
    body = _json_object(response)
    try:
        return Captcha(
            request_id=request_id,
            background_image=_image(body["backgroundImage"]),
            puzzle_piece=_image(body["puzzlePiece"]),
            position_y=int(body["positionY"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise APIError(
            "The captcha endpoint answered with an unexpected body",
            status_code=response.status_code,
            body=body,
            response=response,
        ) from error


def _image(value: object) -> bytes:
    if not isinstance(value, str):
        raise TypeError(f"expected a base64 image, got {type(value).__name__}")
    return base64.b64decode(value.rpartition("base64,")[2])


def _captcha_answer(captcha: Captcha, position: int) -> str:
    answer = {"sliderPosition": position, "requestId": captcha.request_id}
    return base64.b64encode(json.dumps(answer, separators=(",", ":")).encode()).decode()


def _retry_captcha(error: CaptchaRequiredError, answer: str | None, attempt: int) -> bool:
    status = captcha_status(error.body) or ""
    if answer and attempt < CAPTCHA_ATTEMPTS and status.upper() == INVALID_CAPTCHA:
        logger.info("Captcha answer rejected, solving a new captcha")
        return True
    return False


def _mfa_enabled(response: httpx.Response) -> bool | None:
    enabled = _json_object(response).get("mfaEnabled")
    return enabled if isinstance(enabled, bool) else None


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
