import json
from decimal import Decimal

import httpx
import pytest
import respx

from tests.support import API, BASE_URLS
from whitebird import (
    APIError,
    AsyncWhitebird,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    MFARequiredError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
    TransportError,
    VerificationRequiredError,
    Whitebird,
)
from whitebird._config import USER_AGENT
from whitebird.models import WhitebirdModel


class Item(WhitebirdModel):
    item_id: str
    price: Decimal | None = None


def test_get_is_retried_on_server_errors(
    client: Whitebird, respx_mock: respx.MockRouter, delays: list[float]
) -> None:
    route = respx_mock.get(f"{API}/items").mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, json=[])]
    )

    assert client.request("api", "GET", "/items") == []
    assert route.call_count == 3
    assert len(delays) == 2
    assert 0.375 <= delays[0] <= 0.5
    assert 0.75 <= delays[1] <= 1.0


def test_retry_after_is_honoured(
    client: Whitebird, respx_mock: respx.MockRouter, delays: list[float]
) -> None:
    respx_mock.get(f"{API}/items").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json=[]),
        ]
    )

    client.request("api", "GET", "/items")

    assert delays == [2.0]


def test_retries_are_limited(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{API}/items").respond(503, json={"message": "Down"})

    with pytest.raises(ServerError, match="Down"):
        client.request("api", "GET", "/items")
    assert route.call_count == 3


def test_post_is_not_retried(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{API}/items").respond(503)

    with pytest.raises(ServerError):
        client.request("api", "POST", "/items", json={})
    assert route.call_count == 1


def test_post_marked_idempotent_is_retried(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{API}/search").mock(
        side_effect=[httpx.Response(504), httpx.Response(200, json=[])]
    )

    assert client.request("api", "POST", "/search", json={}, idempotent=True) == []
    assert route.call_count == 2


def test_max_retries_option(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{API}/items").respond(503)

    with pytest.raises(ServerError):
        client.request("api", "GET", "/items", max_retries=0)
    assert route.call_count == 1


def test_connection_errors_are_retried_then_raised(
    client: Whitebird, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(f"{API}/items").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(TransportError, match="refused"):
        client.request("api", "GET", "/items")
    assert route.call_count == 3


def test_connection_error_then_success(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/items").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=[1])]
    )

    assert client.request("api", "GET", "/items") == [1]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, BadRequestError),
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (418, APIError),
        (429, RateLimitError),
        (500, ServerError),
    ],
)
def test_errors_by_status(
    client: Whitebird, respx_mock: respx.MockRouter, status: int, error: type[APIError]
) -> None:
    body = {"code": 1001, "status": "SOMETHING_WRONG", "message": "Nope", "fields": [{"f": 1}]}
    respx_mock.post(f"{API}/items").respond(status, json=body)

    with pytest.raises(error) as raised:
        client.request("api", "POST", "/items", authenticated=False)

    assert type(raised.value) is error
    assert raised.value.status_code == status
    assert raised.value.code == 1001
    assert raised.value.error_code == "SOMETHING_WRONG"
    assert raised.value.message == "Nope"
    assert raised.value.fields == [{"f": 1}]
    assert raised.value.body == body
    assert str(raised.value) == f"{status} SOMETHING_WRONG: Nope"


def test_rate_limit_error_has_retry_after(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{API}/items").respond(429, headers={"Retry-After": "7"})

    with pytest.raises(RateLimitError) as raised:
        client.request("api", "POST", "/items")
    assert raised.value.retry_after == 7.0


@pytest.mark.parametrize(
    ("status", "kind"),
    [("SMS_VERIFICATION_REQUIRED", "sms"), ("LIVENESS_VERIFICATION_REQUIRED", "liveness")],
)
def test_verification_required(
    client: Whitebird, respx_mock: respx.MockRouter, status: str, kind: str
) -> None:
    respx_mock.post(f"{API}/withdraw").respond(
        400, json={"code": 400, "status": status, "message": "Verify first"}
    )

    with pytest.raises(VerificationRequiredError) as raised:
        client.request("api", "POST", "/withdraw")
    assert isinstance(raised.value, BadRequestError)
    assert raised.value.kind == kind


@pytest.mark.parametrize(
    "body",
    [
        {"code": 403, "status": "FORBIDDEN", "message": "Operation denied, requires mfa checking"},
        {"code": 400, "status": "MFA_CHECKING_REQUIRED", "message": "MFA"},
    ],
)
def test_operation_mfa_required(
    client: Whitebird, respx_mock: respx.MockRouter, body: dict[str, object]
) -> None:
    respx_mock.post(f"{API}/withdraw").respond(body["code"], json=body)  # type: ignore[arg-type]

    with pytest.raises(MFARequiredError):
        client.request("api", "POST", "/withdraw")


def test_error_with_text_body(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/items").respond(404, text="no such page")

    with pytest.raises(NotFoundError, match="no such page"):
        client.request("api", "GET", "/items")


def test_mfa_token_is_sent_while_set(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{API}/items").respond(json=[])

    client.mfa_token = "mfa-1"
    client.request("api", "GET", "/items")
    assert route.calls.last.request.headers["x-mfa-token"] == "mfa-1"

    client.mfa_token = None
    client.request("api", "GET", "/items")
    assert "x-mfa-token" not in route.calls.last.request.headers


def test_retcode_envelope_is_unwrapped(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/tickers").respond(
        json={"retCode": 0, "retMsg": "OK", "result": [{"itemId": "a", "price": 0.1}]}
    )

    items = client.request(
        "api", "GET", "/tickers", envelope="retcode", cast_to=list[Item], authenticated=False
    )

    assert items == [Item(item_id="a", price=Decimal("0.1"))]


def test_retcode_error_on_http_200(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/tickers").respond(
        json={"retCode": 10001, "retMsg": "params error", "result": {}}
    )

    with pytest.raises(APIError) as raised:
        client.request("api", "GET", "/tickers", envelope="retcode")
    assert (raised.value.code, raised.value.message) == (10001, "params error")
    assert raised.value.status_code == 200


def test_request_escape_hatch(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{API}/accounts/a%2Fb/items").respond(
        content=b'{"itemId": "x", "price": 1.000000000000000001, "extra": true}',
        headers={"content-type": "application/json"},
    )

    item = client.request(
        "api",
        "POST",
        "/accounts/{accountId}/items",
        path_params={"accountId": "a/b"},
        params={"tags": ["x", "y"], "active": True, "skip": None},
        json={"price": Decimal("0.1000000000000000001"), "note": None, "item": Item(item_id="i")},
        headers={"X-Trace": "t", "X-Skip": None},
        cast_to=Item,
        extra_headers={"X-Extra": "e"},
    )

    assert item.price == Decimal("1.000000000000000001")
    assert item.model_extra == {"extra": True}
    request = route.calls.last.request
    assert request.url.query == b"tags=x&tags=y&active=true"
    assert request.content == b'{"price":0.1000000000000000001,"item":{"itemId":"i"}}'
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-trace"] == "t"
    assert request.headers["x-extra"] == "e"
    assert "x-skip" not in request.headers
    assert request.headers["user-agent"] == USER_AGENT


def test_request_decodes_by_content(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/json").respond(json={"amount": 1.5})
    respx_mock.get(f"{API}/text").respond(text="plain")
    respx_mock.get(f"{API}/empty").respond(204)
    respx_mock.get(f"{API}/file").respond(
        content=b"%PDF", headers={"content-type": "application/pdf"}
    )

    assert client.request("api", "GET", "/json") == {"amount": Decimal("1.5")}
    assert client.request("api", "GET", "/text") == "plain"
    assert client.request("api", "GET", "/empty") is None
    assert client.request("api", "GET", "/file", cast_to=bytes) == b"%PDF"


def test_form_and_multipart(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{API}/upload").respond(204)

    client.request("api", "POST", "/upload", data={"name": "avatar"}, files={"file": b"img"})

    request = route.calls.last.request
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b'name="file"' in request.content
    assert b"img" in request.content


def test_form_body(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{API}/form").respond(204)

    client.request("api", "POST", "/form", data={"amount": Decimal("1.50"), "memo": None})

    assert route.calls.last.request.content == b"amount=1.50"


def test_timeout_option(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{API}/items").respond(json=[])

    client.request("api", "GET", "/items", timeout=5)

    assert route.calls.last.request.extensions["timeout"]["read"] == 5


def test_base_urls_and_environments(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get("https://override.test/api/v1/x").respond(json={})
    with Whitebird(base_urls={"market_data": "https://override.test/"}) as client:
        client.request("market_data", "GET", "/api/v1/x", authenticated=False)
    assert route.called

    demo = Whitebird(environment="demo")
    assert demo._engine.config.base_urls["trading"] == "https://trading-demo-service.whitebird.io"
    assert demo._engine.config.base_urls["brokerage"] == "https://brokerage-service.whitebird.io"
    demo.close()


def test_unknown_service_and_environment() -> None:
    with (
        Whitebird(base_urls=BASE_URLS) as client,
        pytest.raises(ValueError, match="unknown service"),
    ):
        client.request("nope", "GET", "/")
    with pytest.raises(ValueError, match="unknown environment"):
        Whitebird(environment="staging")  # type: ignore[arg-type]


async def test_async_request_retries_and_errors(
    async_client: AsyncWhitebird, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{API}/items").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=[{"itemId": "a"}])]
    )
    respx_mock.get(f"{API}/missing").respond(404, json={"message": "Missing"})

    assert await async_client.request("api", "GET", "/items", cast_to=list[Item]) == [
        Item(item_id="a")
    ]
    with pytest.raises(NotFoundError, match="Missing"):
        await async_client.request("api", "GET", "/missing")


async def test_async_mfa_token(async_client: AsyncWhitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{API}/items").respond(json=[])

    async_client.mfa_token = "mfa-2"
    await async_client.request("api", "GET", "/items")

    assert route.calls.last.request.headers["x-mfa-token"] == "mfa-2"


def test_json_encoding_is_exact() -> None:
    from whitebird import _json

    encoded = _json.dumps({"a": [Decimal("1E+2"), 1.5, True, None], "b": Item(item_id="x")})
    assert encoded == '{"a":[100,1.5,true,null],"b":{"itemId":"x"}}'
    assert json.loads(encoded)["a"][0] == 100
