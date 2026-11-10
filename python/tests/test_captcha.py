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

REQUIRED_CAPTCHA = {"code": 400, "status": "REQUIRED_CAPTCHA", "message": "Captcha required"}
INVALID_CAPTCHA = {"code": 400, "status": "INVALID_CAPTCHA", "message": "Wrong captcha"}
ASK_CODE = {"mfaEnabled": True}


def required() -> httpx.Response:
    return httpx.Response(400, json=REQUIRED_CAPTCHA)


def rejected() -> httpx.Response:
    return httpx.Response(400, json=INVALID_CAPTCHA)


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


def test_password_is_sent_without_a_captcha_first(
    make_client: ClientFactory, respx_mock: respx.MockRouter, generate: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens())
    asked: list[Captcha] = []

    def solve(captcha: Captcha) -> int:
        asked.append(captcha)
        return 1

    make_client("me@example.com", "secret", captcha=solve).login()

    assert token.call_count == 1
    assert "captcha" not in token.calls.last.request.headers
    assert not asked
    assert not generate.called


def test_captcha_is_solved_when_asked_for(
    make_client: ClientFactory, respx_mock: respx.MockRouter, generate: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[required(), tokens()])
    captchas: list[Captcha] = []

    def solve(captcha: Captcha) -> int:
        captchas.append(captcha)
        return 117

    make_client("me@example.com", "secret", captcha=solve).login()

    first, second = (call.request for call in token.calls)
    assert "captcha" not in first.headers
    request_id = generate.calls.last.request.url.params["requestId"]
    assert uuid.UUID(request_id).version == 4
    assert captchas == [Captcha(request_id, b"background", b"piece", 42)]
    header = base64.b64decode(second.headers["captcha"])
    assert header == f'{{"sliderPosition":"117","requestId":"{request_id}"}}'.encode()
    assert form(second) == form(first)
    assert "code" not in form(second)


def test_rejected_answer_is_retried_with_a_new_captcha(
    make_client: ClientFactory, respx_mock: respx.MockRouter, generate: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(
        side_effect=[required(), rejected(), rejected(), tokens()]
    )

    make_client("me@example.com", "secret", captcha=lambda _: 1).login()

    request_ids = {answer(call.request)["requestId"] for call in token.calls[1:]}
    assert len(request_ids) == generate.call_count == 3


def test_captcha_attempts_are_limited(
    make_client: ClientFactory, respx_mock: respx.MockRouter, generate: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(
        side_effect=[required(), rejected(), rejected(), rejected()]
    )

    with pytest.raises(CaptchaRequiredError, match="Wrong captcha"):
        make_client("me@example.com", "secret", captcha=lambda _: 1).login()
    assert token.call_count == 4
    assert generate.call_count == 3


@pytest.mark.parametrize(
    "body",
    [REQUIRED_CAPTCHA, {"error": "invalid_captcha", "error_description": "Solve the captcha"}],
    ids=["status", "oauth error"],
)
def test_captcha_without_a_solver(
    make_client: ClientFactory,
    respx_mock: respx.MockRouter,
    generate: respx.Route,
    body: dict[str, Any],
) -> None:
    token = respx_mock.post(TOKEN_URL).respond(400, json=body)

    with pytest.raises(CaptchaRequiredError, match="captcha= a solver") as raised:
        make_client("me@example.com", "secret").login()

    assert isinstance(raised.value, AuthenticationError)
    assert raised.value.status_code == 400
    assert raised.value.body == body
    assert token.call_count == 1
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
    make_client: ClientFactory, generate: respx.Route
) -> None:
    asked: list[Captcha] = []

    def solve(captcha: Captcha) -> int:
        asked.append(captcha)
        return 1

    with pytest.raises(AuthenticationError, match="Not logged in"):
        make_client(captcha=solve).login()

    assert not asked
    assert not generate.called


def test_code_step_after_the_captcha_solves_a_new_one(
    make_client: ClientFactory, respx_mock: respx.MockRouter, generate: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(
        side_effect=[required(), httpx.Response(200, json=ASK_CODE), tokens()]
    )

    make_client("me@example.com", "secret", otp="123456", captcha=lambda _: 3.6).login()

    first, second, third = (call.request for call in token.calls)
    assert "captcha" not in first.headers
    assert answer(second)["sliderPosition"] == "4"
    assert "code" not in form(second)
    assert form(third)["code"] == "123456"
    assert answer(third)["requestId"] != answer(second)["requestId"]
    assert generate.call_count == 2


def test_code_step_without_a_captcha(
    make_client: ClientFactory, respx_mock: respx.MockRouter, generate: respx.Route
) -> None:
    ask_code = httpx.Response(200, json=ASK_CODE)
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[ask_code, ask_code, tokens()])

    with pytest.raises(MFARequiredError):
        make_client("me@example.com", "secret", captcha=lambda _: 1).login()
    make_client("me@example.com", "secret", otp="123456", captcha=lambda _: 1).login()

    assert all("captcha" not in call.request.headers for call in token.calls)
    assert form(token.calls.last.request)["code"] == "123456"
    assert not generate.called


def test_solver_must_return_a_number(
    make_client: ClientFactory, respx_mock: respx.MockRouter, generate: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[required()])

    with pytest.raises(TypeError, match="slider position"):
        make_client("me@example.com", "secret", captcha=lambda _: None).login()
    assert token.call_count == 1


def test_missing_solver_is_reported_before_the_code_is_asked_for(
    make_client: ClientFactory, respx_mock: respx.MockRouter
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[required()])
    asked: list[str] = []

    def ask() -> str:
        asked.append("code")
        return "123456"

    with pytest.raises(CaptchaRequiredError, match="captcha= a solver"):
        make_client("me@example.com", "secret", otp=ask).login()

    assert not asked
    assert token.call_count == 1


async def test_async_missing_solver(
    make_async_client: AsyncClientFactory, respx_mock: respx.MockRouter
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[required()])

    async def ask() -> str:
        raise AssertionError("the code provider must not be called")

    with pytest.raises(CaptchaRequiredError, match="captcha= a solver"):
        await make_async_client("me@example.com", "secret", otp=ask).login()

    assert token.call_count == 1


@pytest.mark.parametrize("asynchronous", [True, False], ids=["coroutine", "function"])
async def test_async_captcha_solver(
    make_async_client: AsyncClientFactory,
    respx_mock: respx.MockRouter,
    generate: respx.Route,
    asynchronous: bool,
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[required(), rejected(), tokens()])

    async def solve_later(captcha: Captcha) -> int:
        return captcha.position_y

    solve = solve_later if asynchronous else (lambda captcha: captcha.position_y)
    await make_async_client("me@example.com", "secret", captcha=solve).login()

    assert "captcha" not in token.calls[0].request.headers
    assert [answer(call.request)["sliderPosition"] for call in token.calls[1:]] == ["42", "42"]


async def test_async_code_step_after_the_captcha(
    make_async_client: AsyncClientFactory, respx_mock: respx.MockRouter, generate: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(
        side_effect=[required(), httpx.Response(200, json=ASK_CODE), tokens()]
    )

    await make_async_client("me@example.com", "secret", otp="123456", captcha=lambda _: 7).login()

    assert form(token.calls.last.request)["code"] == "123456"
    assert answer(token.calls.last.request)["sliderPosition"] == "7"
    assert generate.call_count == 2


def test_each_login_starts_without_a_captcha(
    make_client: ClientFactory, respx_mock: respx.MockRouter, generate: respx.Route
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(side_effect=[required(), tokens(), tokens()])
    client = make_client("me@example.com", "secret", captcha=lambda _: 5)

    client.login()
    client.login()

    assert "captcha" in token.calls[1].request.headers
    assert "captcha" not in token.calls[2].request.headers
    assert generate.call_count == 1
