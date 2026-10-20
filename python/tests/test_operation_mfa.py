import json
from typing import Any

import httpx
import pytest
import respx

from tests.support import API, CONFIRM_URL, AsyncClientFactory, ClientFactory, session
from whitebird import APIError, AsyncWhitebird, MemoryTokenStore, MFARequiredError, Whitebird

WITHDRAW = f"{API}/withdraw"
REFUSED = httpx.Response(
    403, json={"code": 403, "status": "FORBIDDEN", "message": "Operation requires mfa checking"}
)


def withdraw(client: Whitebird) -> Any:
    return client.request("api", "POST", "/withdraw", json={}, mfa_operation="WITHDRAWAL")


def with_otp(make_client: ClientFactory) -> Whitebird:
    return make_client(token_store=MemoryTokenStore(session()), otp="123456")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"mfaToken": "mfa-1"}),
        httpx.Response(200, json="mfa-1"),
        httpx.Response(200, text="mfa-1\n"),
        httpx.Response(200, json={}, headers={"x-mfa-token": "mfa-1"}),
    ],
    ids=["field", "json string", "text", "header"],
)
def test_confirm_operation(
    make_client: ClientFactory, respx_mock: respx.MockRouter, response: httpx.Response
) -> None:
    confirm = respx_mock.post(CONFIRM_URL).mock(return_value=response)
    client = with_otp(make_client)

    assert client.confirm_operation("WITHDRAWAL") == "mfa-1"

    assert client.mfa_token == "mfa-1"
    request = confirm.calls.last.request
    assert json.loads(request.content) == {"operation": "WITHDRAWAL", "mfaCode": "123456"}
    assert request.headers["authorization"].startswith("Bearer ")


def test_confirm_operation_with_code(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    confirm = respx_mock.post(CONFIRM_URL).respond(json={"mfaToken": "mfa-1"})

    client.confirm_operation("WITHDRAWAL", code="654321")

    assert json.loads(confirm.calls.last.request.content)["mfaCode"] == "654321"


def test_confirm_operation_needs_a_code(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    confirm = respx_mock.post(CONFIRM_URL).respond(json={"mfaToken": "mfa-1"})

    with pytest.raises(MFARequiredError, match="one-time code"):
        client.confirm_operation("WITHDRAWAL")
    assert not confirm.called


def test_confirm_operation_without_token(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    respx_mock.post(CONFIRM_URL).respond(204)

    with pytest.raises(APIError, match="without a token"):
        client.confirm_operation("WITHDRAWAL", code="123456")
    assert client.mfa_token is None


def test_token_is_obtained_before_the_operation(
    make_client: ClientFactory, respx_mock: respx.MockRouter
) -> None:
    confirm = respx_mock.post(CONFIRM_URL).respond(json={"mfaToken": "mfa-1"})
    route = respx_mock.post(WITHDRAW).respond(json={"id": 1})
    client = with_otp(make_client)

    assert withdraw(client) == {"id": 1}
    withdraw(client)

    assert confirm.call_count == 1
    assert [call.request.headers["x-mfa-token"] for call in route.calls] == ["mfa-1", "mfa-1"]


def test_refused_operation_is_confirmed_and_retried_once(
    make_client: ClientFactory, respx_mock: respx.MockRouter
) -> None:
    confirm = respx_mock.post(CONFIRM_URL).mock(
        side_effect=[httpx.Response(200, json={"mfaToken": f"mfa-{n}"}) for n in (1, 2)]
    )
    route = respx_mock.post(WITHDRAW).mock(side_effect=[REFUSED, REFUSED])
    client = with_otp(make_client)
    client.mfa_token = "expired"

    with pytest.raises(MFARequiredError):
        withdraw(client)

    assert confirm.call_count == 1
    assert [call.request.headers["x-mfa-token"] for call in route.calls] == ["expired", "mfa-1"]


def test_refused_operation_without_otp(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    confirm = respx_mock.post(CONFIRM_URL).respond(json={"mfaToken": "mfa-1"})
    route = respx_mock.post(WITHDRAW).mock(return_value=REFUSED)

    with pytest.raises(MFARequiredError):
        withdraw(client)

    assert not confirm.called
    assert route.call_count == 1


async def test_async_operation_mfa(
    make_async_client: AsyncClientFactory, respx_mock: respx.MockRouter
) -> None:
    confirm = respx_mock.post(CONFIRM_URL).mock(
        side_effect=[httpx.Response(200, json={"mfaToken": f"mfa-{n}"}) for n in (1, 2, 3)]
    )
    route = respx_mock.post(WITHDRAW).mock(side_effect=[REFUSED, httpx.Response(204)])
    client: AsyncWhitebird = make_async_client(
        token_store=MemoryTokenStore(session()), otp="123456"
    )

    await client.request("api", "POST", "/withdraw", json={}, mfa_operation="WITHDRAWAL")

    assert [call.request.headers["x-mfa-token"] for call in route.calls] == ["mfa-1", "mfa-2"]
    assert await client.confirm_operation("WITHDRAWAL", code="111111") == "mfa-3"
    assert json.loads(confirm.calls.last.request.content)["mfaCode"] == "111111"


async def test_async_confirm_operation_awaits_the_code_provider(
    make_async_client: AsyncClientFactory, respx_mock: respx.MockRouter
) -> None:
    confirm = respx_mock.post(CONFIRM_URL).respond(json={"mfaToken": "mfa-1"})

    async def ask() -> str:
        return "135790"

    client: AsyncWhitebird = make_async_client(token_store=MemoryTokenStore(session()), otp=ask)

    assert await client.confirm_operation("WITHDRAWAL") == "mfa-1"
    assert json.loads(confirm.calls.last.request.content)["mfaCode"] == "135790"
