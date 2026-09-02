import asyncio
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
from whitebird import AuthenticationError, CodeProvider, MemoryTokenStore, MFARequiredError
from whitebird._generated.services import OAUTH_BASIC
from whitebird.otp import TOTP


@pytest.fixture
def api(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(f"{API}/me").respond(json={"ok": True})


def test_login_without_two_factor(
    make_client: ClientFactory, respx_mock: respx.MockRouter, api: respx.Route
) -> None:
    mfa = respx_mock.post(MFA_URL).respond(json={"mfaEnabled": False})
    access_token = jwt()
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens(access_token))
    client = make_client("Me@Example.com", "secret")

    assert client.request("api", "GET", "/me") == {"ok": True}

    assert mfa.calls.last.request.content == b'{"username":"me@example.com"}'
    request = token.calls.last.request
    assert request.headers["authorization"] == f"Basic {OAUTH_BASIC}"
    assert form(request) == {
        "grant_type": "password",
        "username": "me@example.com",
        "password": "secret",
    }
    assert api.calls.last.request.headers["authorization"] == f"Bearer {access_token}"


@pytest.mark.parametrize("otp", ["123456", lambda: "123456"], ids=["static code", "callable"])
def test_login_with_code_provider(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    api: respx.Route,
    otp: CodeProvider,
) -> None:
    mfa = respx_mock.post(MFA_URL)
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens())
    client = make_client("me@example.com", "secret", otp=otp)

    client.request("api", "GET", "/me")

    assert not mfa.called
    assert form(token.calls.last.request)["code"] == "123456"


def test_login_with_totp(
    make_client: ClientFactory, respx_mock: respx.MockRouter, api: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens())
    otp = TOTP("JBSWY3DPEHPK3PXP")
    client = make_client("me@example.com", "secret", otp=otp)

    client.request("api", "GET", "/me")

    assert form(token.calls.last.request)["code"] in {otp.at(time.time() + d) for d in (-30, 0)}


def test_explicit_login_with_code(make_client: ClientFactory, respx_mock: respx.MockRouter) -> None:
    store = MemoryTokenStore()
    access_token = jwt()
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens(access_token, "refresh-9"))
    client = make_client("me@example.com", "secret", token_store=store)

    client.login("654321")

    assert form(token.calls.last.request)["code"] == "654321"
    saved = store.load()
    assert saved is not None
    assert (saved.access_token, saved.refresh_token) == (access_token, "refresh-9")


def test_mfa_required_is_detected_before_login(
    make_client: ClientFactory, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(MFA_URL).respond(json={"mfaEnabled": True})
    token = respx_mock.post(TOKEN_URL)
    client = make_client("me@example.com", "secret")

    with pytest.raises(MFARequiredError, match="otp="):
        client.login()
    assert not token.called


def test_mfa_required_from_token_endpoint(
    make_client: ClientFactory, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(MFA_URL).respond(404)
    respx_mock.post(TOKEN_URL).respond(json={"mfaEnabled": True})
    client = make_client("me@example.com", "secret")

    with pytest.raises(MFARequiredError):
        client.login()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad code"}),
        httpx.Response(200, json={"mfaEnabled": True}),
    ],
    ids=["invalid_grant", "mfaEnabled"],
)
def test_wrong_code(
    make_client: ClientFactory, respx_mock: respx.MockRouter, response: httpx.Response
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=response)
    client = make_client("me@example.com", "secret", otp="000000")

    with pytest.raises(AuthenticationError) as error:
        client.login()
    assert not isinstance(error.value, MFARequiredError)
    if response.status_code == 400:
        assert error.value.message == "Bad code"
        assert error.value.error_code == "invalid_grant"


def test_wrong_password(make_client: ClientFactory, respx_mock: respx.MockRouter) -> None:
    respx_mock.post(MFA_URL).respond(json={"mfaEnabled": False})
    respx_mock.post(TOKEN_URL).respond(
        400, json={"error": "invalid_grant", "error_description": "Bad credentials"}
    )
    client = make_client("me@example.com", "wrong")

    with pytest.raises(AuthenticationError, match="Bad credentials") as error:
        client.login()
    assert not isinstance(error.value, MFARequiredError)


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
    store = MemoryTokenStore(session(expires_in=10, refresh_token="refresh-0"))
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


def test_rejected_refresh_falls_back_to_password(
    make_client: ClientFactory, respx_mock: respx.MockRouter, api: respx.Route
) -> None:
    respx_mock.post(MFA_URL).respond(json={"mfaEnabled": False})
    token = respx_mock.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(400, json={"error": "invalid_grant"}), tokens()]
    )
    store = MemoryTokenStore(session(expires_in=-60))
    client = make_client("me@example.com", "secret", token_store=store)

    client.request("api", "GET", "/me")

    assert [form(call.request)["grant_type"] for call in token.calls] == [
        "refresh_token",
        "password",
    ]


def test_rejected_refresh_without_password(
    make_client: ClientFactory, respx_mock: respx.MockRouter, api: respx.Route
) -> None:
    respx_mock.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    client = make_client(token_store=MemoryTokenStore(session(expires_in=-60)))

    with pytest.raises(AuthenticationError):
        client.request("api", "GET", "/me")
    assert not api.called


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
    make_async_client: AsyncClientFactory, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(MFA_URL).respond(json={"mfaEnabled": False})
    first, second = jwt(subject="first"), jwt(subject="second")
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[tokens(first), tokens(second)])
    api = respx_mock.get(f"{API}/me").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"ok": True})]
    )
    client = make_async_client("me@example.com", "secret")

    assert await client.request("api", "GET", "/me") == {"ok": True}

    assert [form(call.request)["grant_type"] for call in token.calls] == [
        "password",
        "refresh_token",
    ]
    assert api.calls.last.request.headers["authorization"] == f"Bearer {second}"


async def test_async_mfa_required(
    make_async_client: AsyncClientFactory, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(MFA_URL).respond(json={"mfaEnabled": True})
    client = make_async_client("me@example.com", "secret")

    with pytest.raises(MFARequiredError):
        await client.login()
