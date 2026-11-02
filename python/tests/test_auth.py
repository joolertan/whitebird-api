import asyncio
import logging
import threading
import time

import httpx
import pytest
import respx

from tests.support import (
    API,
    MFA_URL,
    TOKEN_URL,
    AsyncClientFactory,
    ClientFactory,
    form,
    jwt,
    session,
    tokens,
)
from whitebird import (
    AuthenticationError,
    BadRequestError,
    CodeProvider,
    MemoryTokenStore,
    MFARequiredError,
    RateLimitError,
    ServerError,
    Tokens,
    TransportError,
)
from whitebird._generated.services import OAUTH_BASIC
from whitebird.otp import TOTP

ASK_CODE = httpx.Response(200, json={"mfaEnabled": True})


@pytest.fixture
def api(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(f"{API}/me").respond(json={"ok": True})


@pytest.fixture
def two_factor(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.post(MFA_URL).respond(json={"mfaEnabled": True})


def grants(route: respx.Route) -> list[dict[str, str]]:
    return [form(call.request) for call in route.calls]


def test_two_factor_login_asks_for_the_code_after_the_password(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    api: respx.Route,
    two_factor: respx.Route,
) -> None:
    access_token = jwt()
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[ASK_CODE, tokens(access_token)])
    client = make_client("Me@Example.com", "secret", otp="123456")

    assert client.request("api", "GET", "/me") == {"ok": True}

    assert two_factor.calls.last.request.content == b'{"username":"Me@Example.com"}'
    password = {"grant_type": "password", "username": "me@example.com", "password": "secret"}
    assert grants(token) == [password, {**password, "code": "123456"}]
    for call in token.calls:
        assert call.request.headers["authorization"] == f"Basic {OAUTH_BASIC}"
        assert "captcha" not in call.request.headers
    assert api.calls.last.request.headers["authorization"] == f"Bearer {access_token}"


def test_two_factor_login_without_a_code_step(
    make_client: ClientFactory, respx_mock: respx.MockRouter, two_factor: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens())
    asked: list[str] = []

    def ask() -> str:
        asked.append("code")
        return "123456"

    client = make_client("me@example.com", "secret", otp=ask)

    client.login()

    assert "code" not in form(token.calls.last.request)
    assert token.call_count == 1
    assert not asked


@pytest.mark.parametrize("otp", [" 123456\n", lambda: "123456 "], ids=["static code", "callable"])
def test_codes_are_trimmed(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    two_factor: respx.Route,
    otp: CodeProvider,
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[ASK_CODE, tokens()])

    make_client("me@example.com", "secret", otp=otp).login()

    assert form(token.calls.last.request)["code"] == "123456"


def test_login_with_totp(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    api: respx.Route,
    two_factor: respx.Route,
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[ASK_CODE, tokens()])
    otp = TOTP("JBSWY3DPEHPK3PXP")
    client = make_client("me@example.com", "secret", otp=otp)

    client.request("api", "GET", "/me")

    assert form(token.calls.last.request)["code"] in {otp.at(time.time() + d) for d in (-30, 0)}


def test_explicit_login_with_code(
    make_client: ClientFactory, respx_mock: respx.MockRouter, two_factor: respx.Route
) -> None:
    store = MemoryTokenStore()
    access_token = jwt()
    token = respx_mock.post(TOKEN_URL).mock(
        side_effect=[ASK_CODE, tokens(access_token, "refresh-9")]
    )
    client = make_client("me@example.com", "secret", otp="000000", token_store=store)

    client.login(" 654321 ")

    assert [grant.get("code") for grant in grants(token)] == [None, "654321"]
    saved = store.load()
    assert saved is not None
    assert (saved.access_token, saved.refresh_token) == (access_token, "refresh-9")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(400, json={"code": 400, "status": "NOT_FOUND", "message": "No user"}),
    ],
    ids=["404", "NOT_FOUND"],
)
def test_unknown_username_sends_no_password(
    make_client: ClientFactory, respx_mock: respx.MockRouter, response: httpx.Response
) -> None:
    respx_mock.post(MFA_URL).mock(return_value=response)
    token = respx_mock.post(TOKEN_URL)

    with pytest.raises(AuthenticationError, match="no Whitebird account") as raised:
        make_client("nobody@example.com", "secret", captcha=lambda _: 1).login()

    assert raised.value.status_code == response.status_code
    assert not token.called


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (httpx.Response(503, json={"message": "Down"}), ServerError),
        (httpx.ConnectError("refused"), TransportError),
    ],
    ids=["server error", "network error"],
)
def test_failed_status_check_sends_no_password(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    side_effect: httpx.Response | Exception,
    error: type[Exception],
) -> None:
    respx_mock.post(MFA_URL).mock(side_effect=[side_effect])
    token = respx_mock.post(TOKEN_URL)

    with pytest.raises(error):
        make_client("me@example.com", "secret", otp="123456", captcha=lambda _: 1).login()
    assert not token.called


def test_missing_code_is_reported_after_the_password_step(
    make_client: ClientFactory, respx_mock: respx.MockRouter, two_factor: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(return_value=ASK_CODE)
    client = make_client("me@example.com", "secret")

    with pytest.raises(MFARequiredError, match="otp="):
        client.login()
    assert token.call_count == 1
    assert "code" not in form(token.calls.last.request)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad code"}),
        httpx.Response(401, json={"message": "Bad code"}),
        httpx.Response(200, json={"mfaEnabled": True}),
    ],
    ids=["invalid_grant", "401", "mfaEnabled"],
)
def test_wrong_code(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    two_factor: respx.Route,
    response: httpx.Response,
) -> None:
    respx_mock.post(TOKEN_URL).mock(side_effect=[ASK_CODE, response])
    client = make_client("me@example.com", "secret", otp="000000")

    with pytest.raises(AuthenticationError) as error:
        client.login()
    assert not isinstance(error.value, MFARequiredError)
    if response.status_code != 200:
        assert error.value.message == "Bad code"


@pytest.mark.parametrize(
    "body",
    [
        {"error": "invalid_grant", "error_description": "Bad credentials"},
        {"code": 400, "status": "BAD_REQUEST", "message": "Bad credentials"},
    ],
)
def test_wrong_password(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    two_factor: respx.Route,
    body: dict[str, object],
) -> None:
    respx_mock.post(TOKEN_URL).respond(400, json=body)
    client = make_client("me@example.com", "wrong", otp="123456")

    with pytest.raises(AuthenticationError, match="Bad credentials") as error:
        client.login()
    assert not isinstance(error.value, MFARequiredError)


@pytest.mark.parametrize(
    ("message", "error"),
    [("Username is blocked", BadRequestError), ("Too many requests", RateLimitError)],
)
def test_blocked_sign_in_is_not_a_wrong_password(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    two_factor: respx.Route,
    message: str,
    error: type[Exception],
) -> None:
    respx_mock.post(TOKEN_URL).respond(400, json={"code": 400, "message": message})

    with pytest.raises(error, match=message) as raised:
        make_client("me@example.com", "secret").login()
    assert not isinstance(raised.value, AuthenticationError)


def test_authenticated_call_without_credentials(
    make_client: ClientFactory, api: respx.Route
) -> None:
    client = make_client()

    with pytest.raises(AuthenticationError, match="Not logged in"):
        client.request("api", "GET", "/me")
    assert not api.called


def test_public_call_needs_no_credentials(make_client: ClientFactory, api: respx.Route) -> None:
    client = make_client()

    assert client.request("api", "GET", "/me", authenticated=False) == {"ok": True}
    assert "authorization" not in api.calls.last.request.headers


def test_proactive_refresh(
    make_client: ClientFactory, respx_mock: respx.MockRouter, api: respx.Route
) -> None:
    store = MemoryTokenStore(session(expires_in=50, refresh_token="refresh-0"))
    fresh = jwt()
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens(fresh, "refresh-1"))
    client = make_client("me@example.com", "secret", token_store=store)

    client.request("api", "GET", "/me")

    assert form(token.calls.last.request) == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-0",
    }
    assert api.calls.last.request.headers["authorization"] == f"Bearer {fresh}"
    saved = store.load()
    assert saved is not None
    assert (saved.access_token, saved.refresh_token) == (fresh, "refresh-1")


def test_expiry_is_read_from_the_access_token(
    make_client: ClientFactory, respx_mock: respx.MockRouter, api: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens())
    client = make_client(token_store=MemoryTokenStore(Tokens(jwt(30), "refresh-0")))

    client.request("api", "GET", "/me")

    assert form(token.calls.last.request)["grant_type"] == "refresh_token"


def test_refresh_reply_without_refresh_token_keeps_the_old_one(
    make_client: ClientFactory, respx_mock: respx.MockRouter, api: respx.Route
) -> None:
    store = MemoryTokenStore(session(expires_in=-10, refresh_token="refresh-0"))
    respx_mock.post(TOKEN_URL).respond(json={"access_token": jwt()})

    make_client(token_store=store).request("api", "GET", "/me")

    saved = store.load()
    assert saved is not None
    assert saved.refresh_token == "refresh-0"


def test_expired_refresh_token_is_not_sent(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    api: respx.Route,
    two_factor: respx.Route,
) -> None:
    store = MemoryTokenStore(session(expires_in=-10, refresh_token=jwt(-5)))
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens())

    make_client("me@example.com", "secret", token_store=store).request("api", "GET", "/me")

    assert [grant["grant_type"] for grant in grants(token)] == ["password"]


def test_short_lived_token_is_used_when_it_cannot_be_renewed(
    make_client: ClientFactory, respx_mock: respx.MockRouter, api: respx.Route
) -> None:
    store = MemoryTokenStore(session(expires_in=20, refresh_token=None))
    token = respx_mock.post(TOKEN_URL)

    make_client(token_store=store).request("api", "GET", "/me")

    assert not token.called
    assert api.calls.last.request.headers["authorization"].endswith(store.load().access_token)  # type: ignore[union-attr]


def test_reactive_refresh_on_401(make_client: ClientFactory, respx_mock: respx.MockRouter) -> None:
    stale = session()
    fresh = jwt()
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens(fresh))
    api = respx_mock.get(f"{API}/me").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"ok": True})]
    )
    client = make_client(token_store=MemoryTokenStore(stale))

    assert client.request("api", "GET", "/me") == {"ok": True}

    assert token.call_count == 1
    assert [call.request.headers["authorization"] for call in api.calls] == [
        f"Bearer {stale.access_token}",
        f"Bearer {fresh}",
    ]


def test_refresh_happens_once_per_request(
    make_client: ClientFactory, respx_mock: respx.MockRouter
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens())
    api = respx_mock.get(f"{API}/me").respond(401, json={"message": "Unauthorized"})
    client = make_client(token_store=MemoryTokenStore(session()))

    with pytest.raises(AuthenticationError, match="Unauthorized"):
        client.request("api", "GET", "/me")
    assert (token.call_count, api.call_count) == (1, 2)


@pytest.mark.parametrize("status", [400, 401])
def test_rejected_refresh_forgets_the_session_and_logs_in(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    api: respx.Route,
    two_factor: respx.Route,
    status: int,
) -> None:
    class Store(MemoryTokenStore):
        cleared = 0

        def clear(self) -> None:
            self.cleared += 1
            super().clear()

    fresh = jwt()
    token = respx_mock.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(status, json={"error": "invalid_grant"}), tokens(fresh)]
    )
    store = Store(session(expires_in=-60))
    client = make_client("me@example.com", "secret", token_store=store)
    client.mfa_token = "mfa-1"

    client.request("api", "GET", "/me")

    assert [grant["grant_type"] for grant in grants(token)] == ["refresh_token", "password"]
    assert store.cleared == 1
    assert store.load().access_token == fresh  # type: ignore[union-attr]
    assert client.mfa_token is None


def test_rejected_refresh_without_password(
    make_client: ClientFactory, respx_mock: respx.MockRouter, api: respx.Route
) -> None:
    respx_mock.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    store = MemoryTokenStore(session(expires_in=-60))
    client = make_client(token_store=store)

    with pytest.raises(AuthenticationError, match="session has expired"):
        client.request("api", "GET", "/me")
    assert not api.called
    assert store.load() is None


@pytest.mark.parametrize(
    "side_effect",
    [httpx.Response(503, json={"message": "Down"}), httpx.ReadTimeout("slow")],
    ids=["server error", "timeout"],
)
def test_failed_refresh_keeps_the_session(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    api: respx.Route,
    two_factor: respx.Route,
    side_effect: httpx.Response | Exception,
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[side_effect])
    stale = session(expires_in=-60)
    store = MemoryTokenStore(stale)
    client = make_client("me@example.com", "secret", token_store=store)

    with pytest.raises((ServerError, TransportError)):
        client.request("api", "GET", "/me")

    assert token.call_count == 1
    assert store.load() == stale
    assert not two_factor.called


def test_session_renewed_elsewhere_is_adopted(
    make_client: ClientFactory, respx_mock: respx.MockRouter, api: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL)
    fresh = session()

    class Shared(MemoryTokenStore):
        loads = 0

        def load(self) -> Tokens | None:
            self.loads += 1
            # Another process refreshed the session after this client loaded it.
            return super().load() if self.loads == 1 else fresh

    shared = Shared(session(expires_in=-60))
    client = make_client(token_store=shared)

    client.request("api", "GET", "/me")

    assert not token.called
    assert api.calls.last.request.headers["authorization"] == f"Bearer {fresh.access_token}"


def test_store_failures(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    api: respx.Route,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Flaky(MemoryTokenStore):
        loads = 0

        def load(self) -> Tokens | None:
            self.loads += 1
            if self.loads == 1:
                raise OSError("store unavailable")
            return super().load()

        def save(self, tokens: Tokens) -> None:
            raise OSError("read-only")

    respx_mock.post(TOKEN_URL).mock(return_value=tokens())
    store = Flaky(session(expires_in=-60))
    client = make_client(token_store=store)

    with pytest.raises(OSError, match="store unavailable"):
        client.request("api", "GET", "/me")
    with caplog.at_level(logging.WARNING, logger="whitebird"):
        assert client.request("api", "GET", "/me") == {"ok": True}

    assert "Could not save the session" in caplog.text


def test_logout_ends_the_session(
    make_client: ClientFactory, respx_mock: respx.MockRouter, two_factor: respx.Route
) -> None:
    logout = respx_mock.post(f"{API}/logout").respond(500)
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens())
    store = MemoryTokenStore(session())
    client = make_client("me@example.com", "secret", token_store=store)
    client.mfa_token = "mfa-1"

    with pytest.raises(ServerError):
        client._engine.request("api", "POST", "/logout", session="end")

    assert logout.called
    assert store.load() is None
    assert client.mfa_token is None
    client.login()
    assert token.call_count == 1


def test_session_ended_during_a_refresh_is_not_restored(
    make_client: ClientFactory, respx_mock: respx.MockRouter, api: respx.Route
) -> None:
    store = MemoryTokenStore(session(expires_in=-60))
    client = make_client(token_store=store)

    def refresh(request: httpx.Request) -> httpx.Response:
        client._engine._auth.end_session()  # a logout on another thread meanwhile
        return tokens()

    respx_mock.post(TOKEN_URL).mock(side_effect=refresh)

    client.request("api", "GET", "/me")

    assert store.load() is None


def test_password_change_replaces_the_session(
    make_client: ClientFactory, respx_mock: respx.MockRouter
) -> None:
    fresh = jwt()
    respx_mock.put(f"{API}/password").respond(json={"access_token": fresh, "expires_in": 3600})
    store = MemoryTokenStore(session(refresh_token="refresh-0"))
    client = make_client(token_store=store)

    body = client._engine.request("api", "PUT", "/password", json={}, session="replace")

    assert body == {"access_token": fresh, "expires_in": 3600}
    saved = store.load()
    assert saved is not None
    assert (saved.access_token, saved.refresh_token) == (fresh, "refresh-0")


def test_password_change_keeps_the_new_password_for_later_logins(
    make_client: ClientFactory, respx_mock: respx.MockRouter, two_factor: respx.Route
) -> None:
    respx_mock.put(f"{API}/password").respond(json={"access_token": jwt()})
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens())
    client = make_client("me@example.com", "old", token_store=MemoryTokenStore(session()))

    client._engine.request(
        "api",
        "PUT",
        "/password",
        json={"oldPassword": "old", "newPassword": "new"},
        session="replace",
    )
    client.login()

    assert grants(token)[-1]["password"] == "new"


def test_password_change_answered_after_a_logout_is_ignored(
    make_client: ClientFactory, respx_mock: respx.MockRouter
) -> None:
    store = MemoryTokenStore(session())
    client = make_client("me@example.com", "old", token_store=store)

    def change(request: httpx.Request) -> httpx.Response:
        client._engine._auth.end_session()  # a logout on another thread meanwhile
        return httpx.Response(200, json={"access_token": jwt()})

    respx_mock.put(f"{API}/password").mock(side_effect=change)

    client._engine.request(
        "api", "PUT", "/password", json={"newPassword": "new"}, session="replace"
    )

    assert store.load() is None
    assert client._engine._auth.credentials.password == "old"


def test_concurrent_requests_share_one_refresh(
    make_client: ClientFactory, respx_mock: respx.MockRouter, api: respx.Route
) -> None:
    def slow_tokens(request: httpx.Request) -> httpx.Response:
        time.sleep(0.05)
        return tokens()

    token = respx_mock.post(TOKEN_URL).mock(side_effect=slow_tokens)
    client = make_client(token_store=MemoryTokenStore(session(expires_in=-60)))
    errors: list[Exception] = []

    def call() -> None:
        try:
            client.request("api", "GET", "/me")
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert token.call_count == 1
    assert api.call_count == 8


async def test_async_concurrent_requests_share_one_refresh(
    make_async_client: AsyncClientFactory, respx_mock: respx.MockRouter, api: respx.Route
) -> None:
    async def slow_tokens(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return tokens()

    token = respx_mock.post(TOKEN_URL).mock(side_effect=slow_tokens)
    client = make_async_client(token_store=MemoryTokenStore(session(expires_in=-60)))

    results = await asyncio.gather(*(client.request("api", "GET", "/me") for _ in range(8)))

    assert results == [{"ok": True}] * 8
    assert token.call_count == 1


async def test_async_login_and_reactive_refresh(
    make_async_client: AsyncClientFactory, respx_mock: respx.MockRouter, two_factor: respx.Route
) -> None:
    first, second = jwt(subject="first"), jwt(subject="second")
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[tokens(first), tokens(second)])
    api = respx_mock.get(f"{API}/me").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"ok": True})]
    )
    client = make_async_client("me@example.com", "secret")

    assert await client.request("api", "GET", "/me") == {"ok": True}

    assert [grant["grant_type"] for grant in grants(token)] == ["password", "refresh_token"]
    assert api.calls.last.request.headers["authorization"] == f"Bearer {second}"


async def test_async_mfa_required(
    make_async_client: AsyncClientFactory, respx_mock: respx.MockRouter, two_factor: respx.Route
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=ASK_CODE)
    client = make_async_client("me@example.com", "secret")

    with pytest.raises(MFARequiredError):
        await client.login()


async def test_async_login_awaits_the_code_provider(
    make_async_client: AsyncClientFactory, respx_mock: respx.MockRouter, two_factor: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[ASK_CODE, tokens()])

    async def ask() -> str:
        await asyncio.sleep(0)
        return "246810"

    client = make_async_client("me@example.com", "secret", otp=ask)

    await client.login()

    assert form(token.calls.last.request)["code"] == "246810"


async def test_async_logout_and_unknown_username(
    make_async_client: AsyncClientFactory, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(MFA_URL).respond(404)
    respx_mock.post(f"{API}/logout").respond(200)
    token = respx_mock.post(TOKEN_URL)
    store = MemoryTokenStore(session())
    client = make_async_client("nobody@example.com", "secret", token_store=store)

    await client._engine.request("api", "POST", "/logout", session="end")
    assert store.load() is None
    with pytest.raises(AuthenticationError, match="no Whitebird account"):
        await client.request("api", "GET", "/me")
    assert not token.called


def test_sync_client_rejects_a_coroutine_code_provider(
    make_client: ClientFactory, respx_mock: respx.MockRouter, two_factor: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(return_value=ASK_CODE)

    async def ask() -> str:
        return "246810"

    client = make_client("me@example.com", "secret", otp=ask)

    with pytest.raises(TypeError, match="AsyncWhitebird"):
        client.login()
    assert token.call_count == 1
