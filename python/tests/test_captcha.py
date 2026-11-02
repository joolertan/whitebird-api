import base64
import json
import uuid
from typing import Any

import httpx
import pytest
import respx

from tests.support import (
    API,
    CAPTCHA_URL,
    MFA_URL,
    TOKEN_URL,
    AsyncClientFactory,
    ClientFactory,
    form,
    tokens,
)
from whitebird import (
    AuthenticationError,
    BadRequestError,
    Captcha,
    CaptchaRequiredError,
    MFARequiredError,
    Whitebird,
)

INVALID_CAPTCHA = {"code": 400, "status": "INVALID_CAPTCHA", "message": "Wrong captcha"}


@pytest.fixture
def mfa(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.post(MFA_URL).respond(json={"mfaEnabled": False})


@pytest.fixture
def generate(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(CAPTCHA_URL).respond(
        json={
            "backgroundImage": base64.b64encode(b"background").decode(),
            "puzzlePiece": "data:image/png;base64," + base64.b64encode(b"piece").decode(),
            "positionY": 42,
        }
    )


def answer(request: httpx.Request) -> Any:
    return json.loads(base64.b64decode(request.headers["captcha"]))


def test_captcha_is_solved_before_login(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    mfa: respx.Route,
    generate: respx.Route,
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens())
    captchas: list[Captcha] = []

    def solve(captcha: Captcha) -> int:
        captchas.append(captcha)
        return 117

    make_client("me@example.com", "secret", captcha=solve).login()

    request_id = generate.calls.last.request.url.params["requestId"]
    assert uuid.UUID(request_id).version == 4
    assert captchas == [Captcha(request_id, b"background", b"piece", 42)]
    header = base64.b64decode(token.calls.last.request.headers["captcha"])
    assert header == f'{{"sliderPosition":"117","requestId":"{request_id}"}}'.encode()
    assert "code" not in form(token.calls.last.request)


def test_rejected_answer_is_retried_with_a_new_captcha(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    mfa: respx.Route,
    generate: respx.Route,
) -> None:
    rejected = httpx.Response(400, json=INVALID_CAPTCHA)
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[rejected, rejected, tokens()])

    make_client("me@example.com", "secret", captcha=lambda _: 1).login()

    request_ids = {answer(call.request)["requestId"] for call in token.calls}
    assert len(request_ids) == generate.call_count == 3


def test_captcha_attempts_are_limited(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    mfa: respx.Route,
    generate: respx.Route,
) -> None:
    token = respx_mock.post(TOKEN_URL).respond(400, json=INVALID_CAPTCHA)

    with pytest.raises(CaptchaRequiredError, match="Wrong captcha"):
        make_client("me@example.com", "secret", captcha=lambda _: 1).login()
    assert token.call_count == 3


@pytest.mark.parametrize(
    "body",
    [
        {"code": 400, "status": "CAPTCHA_REQUIRED", "message": "Solve the captcha"},
        {"error": "invalid_captcha", "error_description": "Solve the captcha"},
    ],
)
def test_captcha_demanded_by_the_token_endpoint(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    mfa: respx.Route,
    generate: respx.Route,
    body: dict[str, Any],
) -> None:
    mfa.respond(json={"mfaEnabled": True})
    token = respx_mock.post(TOKEN_URL).respond(400, json=body)

    with pytest.raises(CaptchaRequiredError, match="Solve the captcha") as raised:
        make_client("me@example.com", "secret", captcha=lambda _: 1).login()

    assert isinstance(raised.value, AuthenticationError)
    assert "captcha" not in token.calls.last.request.headers
    assert not generate.called


@pytest.mark.parametrize("status", ["INVALID_CAPTCHA", "INVALID_RECAPTCHA"])
def test_captcha_codes_of_other_endpoints_are_bad_requests(
    client: Whitebird, respx_mock: respx.MockRouter, status: str
) -> None:
    respx_mock.post(f"{API}/support").respond(400, json={"status": status, "message": "Captcha"})

    with pytest.raises(BadRequestError) as raised:
        client.request("api", "POST", "/support", json={})
    assert not isinstance(raised.value, CaptchaRequiredError)


def test_credentials_are_checked_before_the_captcha(
    make_client: ClientFactory, mfa: respx.Route, generate: respx.Route
) -> None:
    asked: list[Captcha] = []

    def solve(captcha: Captcha) -> int:
        asked.append(captcha)
        return 1

    with pytest.raises(AuthenticationError, match="Not logged in"):
        make_client(captcha=solve).login()

    assert not asked
    assert not mfa.called
    assert not generate.called


def test_code_step_after_the_captcha(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    mfa: respx.Route,
    generate: respx.Route,
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(200, json={"mfaEnabled": True}), tokens()]
    )

    make_client("me@example.com", "secret", otp="123456", captcha=lambda _: 3.6).login()

    first, second = (call.request for call in token.calls)
    assert answer(first)["sliderPosition"] == "4"
    assert form(second)["code"] == "123456"
    assert "captcha" not in second.headers
    assert generate.call_count == 1


def test_solver_must_return_a_number(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    mfa: respx.Route,
    generate: respx.Route,
) -> None:
    token = respx_mock.post(TOKEN_URL)

    with pytest.raises(TypeError, match="slider position"):
        make_client("me@example.com", "secret", captcha=lambda _: None).login()
    assert not token.called


def test_account_without_two_factor_needs_a_solver(
    make_client: ClientFactory, respx_mock: respx.MockRouter, mfa: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL)
    asked: list[str] = []

    def ask() -> str:
        asked.append("code")
        return "123456"

    with pytest.raises(CaptchaRequiredError, match="captcha= a solver"):
        make_client("me@example.com", "secret", otp=ask).login()

    assert mfa.called
    assert not asked
    assert not token.called


async def test_async_account_without_two_factor_needs_a_solver(
    make_async_client: AsyncClientFactory, respx_mock: respx.MockRouter, mfa: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL)

    async def ask() -> str:
        raise AssertionError("the code provider must not be called")

    with pytest.raises(CaptchaRequiredError, match="two-factor"):
        await make_async_client("me@example.com", "secret", otp=ask).login()

    assert not token.called


def test_no_captcha_for_two_factor_accounts(
    make_client: ClientFactory, respx_mock: respx.MockRouter, generate: respx.Route
) -> None:
    respx_mock.post(MFA_URL).respond(json={"mfaEnabled": True})
    ask_code = httpx.Response(200, json={"mfaEnabled": True})
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[ask_code, ask_code, tokens()])

    with pytest.raises(MFARequiredError):
        make_client("me@example.com", "secret", captcha=lambda _: 1).login()
    make_client("me@example.com", "secret", otp="123456", captcha=lambda _: 1).login()

    assert all("captcha" not in call.request.headers for call in token.calls)
    assert not generate.called


@pytest.mark.parametrize("asynchronous", [True, False], ids=["coroutine", "function"])
async def test_async_captcha_solver(
    make_async_client: AsyncClientFactory,
    respx_mock: respx.MockRouter,
    mfa: respx.Route,
    generate: respx.Route,
    asynchronous: bool,
) -> None:
    rejected = httpx.Response(400, json=INVALID_CAPTCHA)
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[rejected, tokens()])

    async def solve_later(captcha: Captcha) -> int:
        return captcha.position_y

    solve = solve_later if asynchronous else (lambda captcha: captcha.position_y)
    await make_async_client("me@example.com", "secret", captcha=solve).login()

    assert [answer(call.request)["sliderPosition"] for call in token.calls] == ["42", "42"]


def test_status_decides_between_captcha_and_code(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    generate: respx.Route,
) -> None:
    mfa = respx_mock.post(MFA_URL).respond(json={"mfaEnabled": False})
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens())
    client = make_client("me@example.com", "secret", otp="123456", captcha=lambda _: 5)

    client.login()
    assert "code" not in token.calls.last.request.content.decode()
    assert answer(token.calls.last.request)["sliderPosition"] == "5"

    mfa.respond(json={"mfaEnabled": True})
    token.mock(side_effect=[httpx.Response(200, json={"mfaEnabled": True}), tokens()])
    client.login()
    assert "code=123456" in token.calls.last.request.content.decode()
    assert "captcha" not in token.calls.last.request.headers
    assert generate.call_count == 1
