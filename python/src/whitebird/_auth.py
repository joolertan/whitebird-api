"""OAuth password login and token refresh, renewed by a single caller at a time.

Logging in follows the sign-in form of the web application:

1. Send a password grant without a captcha. Whitebird answers with tokens, with
   `{"mfaEnabled": true}`, or with status `REQUIRED_CAPTCHA`.
2. Once a captcha was asked for, every grant of the login carries a newly solved slider captcha;
   a rejected answer (`INVALID_CAPTCHA`) is retried with a new captcha.
3. After `{"mfaEnabled": true}`, a second grant carries the one-time code.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
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

from ._config import Config
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
    OAUTH_SERVICE,
    OAUTH_TOKEN_PATH,
)
from .tokens import Tokens, TokenStore
from .types import AsyncCaptchaSolver, AsyncCodeProvider, Captcha, CaptchaSolver

# The web application renews the access token when less than a minute of it is left.
REFRESH_MARGIN = 60.0
CAPTCHA_ATTEMPTS = 3
INVALID_CAPTCHA = "INVALID_CAPTCHA"
NOT_FOUND = "NOT_FOUND"
# Answers of the token endpoint that mean the refresh token is no longer valid.
REJECTED_REFRESH = frozenset({400, 401})

_MFA_REQUIRED = (
    "Two-factor authentication is enabled for this account: pass otp= to the client "
    "(a code, a callable or whitebird.otp.TOTP) or call login(code=...)"
)
_CAPTCHA_REQUIRED = "Whitebird asks for a captcha to log in: pass captcha= a solver"
_NOT_LOGGED_IN = (
    "Not logged in: pass username and password to the client, or a token store that holds a session"
)
_SESSION_EXPIRED = (
    "The session has expired and its refresh token was rejected: pass username and password "
    "to the client to log in again"
)
_NO_ACCOUNT = "There is no Whitebird account with this username"

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
        return clean_code(code)

    async def acode(self) -> str | None:
        """Returns a one-time code, awaiting it if the provider is a coroutine function."""
        code = self._provide()
        return clean_code(await code if inspect.isawaitable(code) else code)

    def _provide(self) -> str | Awaitable[str] | None:
        if self.otp is None or isinstance(self.otp, str):
            return self.otp
        return self.otp()


def clean_code(code: object) -> str | None:
    """Trims a one-time code as the web application does; None when nothing is left."""
    if code is None:
        return None
    return str(code).strip() or None


class _Authenticator:
    """Session state and OAuth requests shared by the sync and async authenticators.

    The session is loaded from the token store on first use and reloaded before it is renewed,
    which adopts tokens saved meanwhile by another client sharing the store. Every login and
    refresh notes the session generation it started in, and does not store its tokens if the
    session was ended or replaced since (see `end_session`).
    """

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
        self._tokens: Tokens | None = None
        self._loaded = False
        self._generation = 0
        self.mfa_token: str | None = None
        """Operation MFA token, sent as `x-mfa-token` while set; forgotten with the session."""

    # Session state

    def _session(self) -> Tokens | None:
        if not self._loaded:
            self._tokens = _complete(self._store.load())
            self._loaded = True
        return self._tokens

    def _valid_token(self) -> str | None:
        tokens = self._session()
        if tokens is None or tokens.expires_within(REFRESH_MARGIN):
            return None
        return tokens.access_token

    def _reload(self) -> None:
        """Adopts tokens that another client saved to the store since they were loaded."""
        try:
            stored = _complete(self._store.load())
        except Exception:
            logger.warning("Could not load the session from the token store", exc_info=True)
            return
        current = self._tokens
        if stored is not None and (
            current is None
            or (stored.access_token, stored.refresh_token)
            != (current.access_token, current.refresh_token)
        ):
            self._tokens = stored

    def _usable(self, rejected: str | None) -> Tokens | None:
        """Returns the current tokens if they need no renewal: not rejected, not expiring."""
        tokens = self._tokens
        if tokens is None or tokens.access_token == rejected:
            return None
        if not tokens.expires_within(REFRESH_MARGIN):
            return tokens
        if not self.credentials.can_login and not _can_refresh(tokens):
            # Nothing to renew it with: use it until it actually expires.
            return None if tokens.expires_within(0) else tokens
        return None

    def _adopt(self, tokens: Tokens, generation: int) -> Tokens:
        if generation == self._generation:
            self._tokens = tokens
            self._loaded = True
            try:
                self._store.save(tokens)
            except Exception:
                logger.warning("Could not save the session to the token store", exc_info=True)
        return tokens

    def _forget(self) -> None:
        self._tokens = None
        self._loaded = True
        self.mfa_token = None
        try:
            self._store.clear()
        except Exception:
            logger.warning("Could not clear the token store", exc_info=True)

    def end_session(self) -> None:
        """Forgets the session (after a logout); a login or refresh in flight does not keep it."""
        self._generation += 1
        self._forget()

    @property
    def generation(self) -> int:
        """Changes whenever the session is ended or replaced."""
        return self._generation

    def replace_session(
        self, response: httpx.Response, generation: int, new_password: str | None = None
    ) -> None:
        """Stores the token pair that a call such as a password change answered with.

        `generation` is the session generation when the call was sent: if the session ended or
        was replaced meanwhile, the answer is ignored. `new_password` (the password change's
        `newPassword`) replaces the password used for later logins.
        """
        body = _json_object(response)
        if not _access_token(body):
            logger.warning("Expected a new token pair in the response, the session is unchanged")
            return
        if generation != self._generation:
            return
        self._generation += 1
        self._adopt(_tokens(body, self._tokens), self._generation)
        if new_password and self.credentials.password is not None:
            self.credentials = dataclasses.replace(self.credentials, password=new_password)

    # Requests

    def _require_credentials(self) -> None:
        if not self.credentials.can_login:
            raise AuthenticationError(_NOT_LOGGED_IN)

    def _refresh_request(self, tokens: Tokens) -> httpx.Request:
        form = {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token or ""}
        return self._token_request(form)

    def _password_request(
        self, code: str | None = None, answer: str | None = None
    ) -> httpx.Request:
        username = (self.credentials.username or "").lower()
        form = {
            "grant_type": "password",
            "username": username,
            "password": self.credentials.password or "",
        }
        if code:
            form["code"] = code
        return self._token_request(form, {"captcha": answer} if answer else {})

    def _captcha_request(self, request_id: str) -> httpx.Request:
        return self._build_request(
            "GET",
            self._config.url(OAUTH_SERVICE, OAUTH_CAPTCHA_PATH),
            params={"requestId": request_id},
            headers=self._config.headers(),
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
                **self._config.headers(),
                "Authorization": f"Basic {OAUTH_BASIC}",
                **(headers or {}),
            },
            timeout=self._config.timeout,
        )

    def _code_step(self, response: httpx.Response) -> bool:
        """Tells whether a password grant answered that the one-time code is needed."""
        body = _json_object(response)
        return not _access_token(body) and bool(body.get("mfaEnabled"))

    def _login_tokens(
        self, response: httpx.Response, generation: int, *, code_sent: bool
    ) -> Tokens:
        return self._adopt(_parse_tokens(response, code_sent=code_sent), generation)

    def _refreshed(self, response: httpx.Response, previous: Tokens, generation: int) -> Tokens:
        body = _json_object(response)
        if not _access_token(body):
            raise _without_token(response, body)
        return self._adopt(_tokens(body, previous), generation)

    def _rejected_refresh(self, error: APIError) -> None:
        """Forgets a session whose refresh token was rejected; raises if no login is possible."""
        logger.info("The refresh token was rejected")
        self._forget()
        if not self.credentials.can_login:
            raise AuthenticationError(
                _SESSION_EXPIRED,
                status_code=error.status_code,
                error_code=error.error_code,
                body=error.body,
                response=error.response,
            ) from error


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
                token = self._valid_token() or self._renew(None).access_token
        return token

    def refresh(self, rejected: str) -> None:
        """Renews the session after `rejected` got a 401, unless another call already did."""
        with self._lock:
            self._renew(rejected)

    def login(self, code: str | None = None) -> None:
        """Logs in with the password, even if a session exists."""
        with self._lock:
            self._login(clean_code(code), self._generation)

    def _renew(self, rejected: str | None) -> Tokens:
        generation = self._generation
        self._reload()
        tokens = self._usable(rejected)
        if tokens is not None:
            return tokens
        current = self._tokens
        if current is not None and _can_refresh(current):
            try:
                response = self._send(self._refresh_request(current), login=True)
            except APIError as error:
                if error.status_code not in REJECTED_REFRESH:
                    raise
                self._rejected_refresh(error)
            else:
                return self._refreshed(response, current, generation)
        return self._login(None, generation)

    def _login(self, code: str | None, generation: int) -> Tokens:
        self._require_credentials()
        login = _Login()
        response = self._grant(login, None)
        if not self._code_step(response):
            return self._login_tokens(response, generation, code_sent=False)
        code = code or self.credentials.code()
        if code is None:
            raise MFARequiredError(_MFA_REQUIRED)
        response = self._grant(login, code)
        return self._login_tokens(response, generation, code_sent=True)

    def _grant(self, login: _Login, code: str | None) -> httpx.Response:
        """Sends a password grant, with a solved captcha once Whitebird asked for one."""
        while True:
            answer = self._solve_captcha() if login.captcha else None
            try:
                return self._send(self._password_request(code, answer), login=True)
            except CaptchaRequiredError as error:
                login.rejected(error, solver=self._captcha is not None)
            except APIError as error:
                unknown = _unknown_account(error)
                if unknown is None:
                    raise
                raise unknown from error

    def _solve_captcha(self) -> str:
        assert self._captcha is not None
        request_id = str(uuid.uuid4())
        captcha = _parse_captcha(request_id, self._send(self._captcha_request(request_id)))
        return _captcha_answer(captcha, self._captcha(captcha))

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
                token = self._valid_token() or (await self._renew(None)).access_token
        return token

    async def refresh(self, rejected: str) -> None:
        """Renews the session after `rejected` got a 401, unless another task already did."""
        async with self._lock:
            await self._renew(rejected)

    async def login(self, code: str | None = None) -> None:
        """Logs in with the password, even if a session exists."""
        async with self._lock:
            await self._login(clean_code(code), self._generation)

    async def _renew(self, rejected: str | None) -> Tokens:
        generation = self._generation
        self._reload()
        tokens = self._usable(rejected)
        if tokens is not None:
            return tokens
        current = self._tokens
        if current is not None and _can_refresh(current):
            try:
                response = await self._send(self._refresh_request(current), login=True)
            except APIError as error:
                if error.status_code not in REJECTED_REFRESH:
                    raise
                self._rejected_refresh(error)
            else:
                return self._refreshed(response, current, generation)
        return await self._login(None, generation)

    async def _login(self, code: str | None, generation: int) -> Tokens:
        self._require_credentials()
        login = _Login()
        response = await self._grant(login, None)
        if not self._code_step(response):
            return self._login_tokens(response, generation, code_sent=False)
        code = code or await self.credentials.acode()
        if code is None:
            raise MFARequiredError(_MFA_REQUIRED)
        response = await self._grant(login, code)
        return self._login_tokens(response, generation, code_sent=True)

    async def _grant(self, login: _Login, code: str | None) -> httpx.Response:
        """Sends a password grant, with a solved captcha once Whitebird asked for one."""
        while True:
            answer = await self._solve_captcha() if login.captcha else None
            try:
                return await self._send(self._password_request(code, answer), login=True)
            except CaptchaRequiredError as error:
                login.rejected(error, solver=self._captcha is not None)
            except APIError as error:
                unknown = _unknown_account(error)
                if unknown is None:
                    raise
                raise unknown from error

    async def _solve_captcha(self) -> str:
        assert self._captcha is not None
        request_id = str(uuid.uuid4())
        captcha = _parse_captcha(request_id, await self._send(self._captcha_request(request_id)))
        position = self._captcha(captcha)
        if inspect.isawaitable(position):
            position = await position
        return _captcha_answer(captcha, position)

    async def _send(self, request: httpx.Request, *, login: bool = False) -> httpx.Response:
        try:
            response = await self._http.send(request)
        except httpx.TransportError as error:
            raise TransportError(str(error) or type(error).__name__) from error
        if response.is_error:
            raise error_from_response(response, login=login)
        return response


class _Login:
    """Captcha state of one login: whether Whitebird asked for one, and how many were rejected."""

    def __init__(self) -> None:
        self.captcha = False
        self._rejected = 0

    def rejected(self, error: CaptchaRequiredError, *, solver: bool) -> None:
        """Takes a captcha refusal of a grant; raises unless a new captcha should be solved."""
        if not self.captcha:
            self.captcha = True
            if not solver:
                raise CaptchaRequiredError(
                    _CAPTCHA_REQUIRED,
                    status_code=error.status_code,
                    error_code=error.error_code,
                    body=error.body,
                    response=error.response,
                ) from error
            return
        self._rejected += 1
        if not _retry_captcha(error, self._rejected):
            raise error


def _unknown_account(error: APIError) -> AuthenticationError | None:
    """A clearer error for a grant refused because the account does not exist."""
    if not (isinstance(error.body, dict) and error.body.get("status") == NOT_FOUND):
        return None
    return AuthenticationError(
        _NO_ACCOUNT,
        status_code=error.status_code,
        error_code=error.error_code,
        body=error.body,
        response=error.response,
    )


def _parse_tokens(response: httpx.Response, *, code_sent: bool) -> Tokens:
    body = _json_object(response)
    if _access_token(body):
        return _tokens(body)
    details: dict[str, Any] = {
        "status_code": response.status_code,
        "body": body,
        "response": response,
    }
    if body.get("mfaEnabled"):
        if code_sent:
            raise AuthenticationError("The one-time code was rejected", **details)
        raise MFARequiredError(_MFA_REQUIRED, **details)
    raise _without_token(response, body)


def _without_token(response: httpx.Response, body: dict[str, Any]) -> AuthenticationError:
    return AuthenticationError(
        "The token endpoint answered without an access token",
        status_code=response.status_code,
        body=body,
        response=response,
    )


def _access_token(body: dict[str, Any]) -> str | None:
    token = body.get("access_token")
    return token if isinstance(token, str) and token else None


def _tokens(body: dict[str, Any], previous: Tokens | None = None) -> Tokens:
    """Reads a token pair; a missing refresh token keeps the previous one."""
    access_token = _access_token(body) or ""
    refresh_token = body.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = previous.refresh_token if previous else None
    return Tokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=_expiry(access_token, body.get("expires_in")),
    )


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


def _captcha_answer(captcha: Captcha, position: object) -> str:
    try:
        pixels = round(float(position))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as error:
        raise TypeError(
            f"the captcha solver must return the slider position in pixels, got {position!r}"
        ) from error
    # The web application sends the value of its range input: an integer as a string.
    answer = {"sliderPosition": str(pixels), "requestId": captcha.request_id}
    return base64.b64encode(json.dumps(answer, separators=(",", ":")).encode()).decode()


def _retry_captcha(error: CaptchaRequiredError, attempt: int) -> bool:
    status = captcha_status(error.body) or ""
    if attempt < CAPTCHA_ATTEMPTS and status.upper() == INVALID_CAPTCHA:
        logger.info("Captcha answer rejected, solving a new captcha")
        return True
    return False


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _can_refresh(tokens: Tokens) -> bool:
    """Whether the session has a refresh token that has not expired yet."""
    if not tokens.refresh_token:
        return False
    expiry = jwt_expiry(tokens.refresh_token)
    return expiry is None or expiry > time.time()


def _complete(tokens: Tokens | None) -> Tokens | None:
    """Fills in the expiry of tokens that lack one from the `exp` claim of the access token."""
    if tokens is None or tokens.expires_at is not None:
        return tokens
    return dataclasses.replace(tokens, expires_at=jwt_expiry(tokens.access_token))


def jwt_expiry(token: str) -> float | None:
    """Reads the `exp` claim of a JWT, without verifying it; None if it is not a JWT."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expiry = claims["exp"]
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    if isinstance(expiry, bool) or not isinstance(expiry, int | float):
        return None
    return float(expiry)


def _expiry(access_token: str, expires_in: object) -> float | None:
    expiry = jwt_expiry(access_token)
    if expiry is not None:
        return expiry
    if isinstance(expires_in, int | float) and not isinstance(expires_in, bool):
        return time.time() + expires_in
    return None
