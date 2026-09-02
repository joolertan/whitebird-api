import json
from typing import Any

import httpx
import pytest
import respx

from tests.support import API
from whitebird import AsyncPager, AsyncWhitebird, Page, SyncPager, Whitebird
from whitebird.models import WhitebirdModel


class Order(WhitebirdModel):
    order_id: int


def spring_page(index: int, size: int, total: int, **metadata: Any) -> dict[str, Any]:
    start = index * size
    ids = range(start, min(start + size, total))
    return {
        "content": [{"orderId": i} for i in ids],
        "totalElements": total,
        "size": size,
        **metadata,
    }


def serve(total: int, size: int, **metadata: Any) -> Any:
    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page") or json.loads(request.content)["page"])
        return httpx.Response(200, json=spring_page(page, size, total, number=page, **metadata))

    return respond


def pager(client: Whitebird, **params: Any) -> SyncPager[Order]:
    return client._engine.paginate(
        "api", "GET", "/orders", params={"page": 0, **params}, cast_to=Page[Order]
    )


def async_pager(client: AsyncWhitebird, **params: Any) -> AsyncPager[Order]:
    return client._engine.paginate(
        "api", "GET", "/orders", params={"page": 0, **params}, cast_to=Page[Order]
    )


def requested_pages(route: respx.Route) -> list[int]:
    return [int(call.request.url.params["page"]) for call in route.calls]


@pytest.mark.parametrize(
    "metadata",
    [
        {"totalPages": 3, "last": False},
        {"totalPages": 3},
        {},
    ],
    ids=["last flag", "total pages", "no metadata"],
)
def test_iterates_over_every_page(
    client: Whitebird, respx_mock: respx.MockRouter, metadata: dict[str, Any]
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        extra = {**metadata, "last": page == 2} if "last" in metadata else metadata
        return httpx.Response(200, json=spring_page(page, 2, 5, **extra))

    route = respx_mock.get(f"{API}/orders").mock(side_effect=respond)

    assert [order.order_id for order in pager(client, size=2)] == [0, 1, 2, 3, 4]
    expected = [0, 1, 2, 3] if not metadata else [0, 1, 2]
    assert requested_pages(route) == expected
    assert route.calls.last.request.url.params["size"] == "2"


def test_iteration_is_lazy(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{API}/orders").mock(side_effect=serve(total=10, size=2))

    for order in pager(client, size=2):
        if order.order_id == 2:
            break

    assert requested_pages(route) == [0, 1]


def test_starts_at_the_requested_page(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{API}/orders").mock(side_effect=serve(total=6, size=2, totalPages=3))

    assert [order.order_id for order in pager(client, page=1, size=2)] == [2, 3, 4, 5]
    assert requested_pages(route) == [1, 2]


def test_pages_and_single_page(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/orders").mock(side_effect=serve(total=5, size=2, totalPages=3))

    pages = list(pager(client, size=2).pages())
    assert [page.number for page in pages] == [0, 1, 2]
    assert [page.has_next for page in pages] == [True, True, False]
    assert pages[0].total_elements == 5

    page = pager(client, size=2).page(1)
    assert [order.order_id for order in page.content] == [2, 3]
    assert (page.number, page.total_pages) == (1, 3)


def test_page_number_is_filled_in(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/orders").respond(json={"content": [], "totalElements": 0})

    page = pager(client).page(4)

    assert page.number == 4
    assert not page.has_next


def test_pagination_in_body(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{API}/orders/search").mock(
        side_effect=serve(total=3, size=2, totalPages=2)
    )

    orders = client._engine.paginate(
        "api",
        "POST",
        "/orders/search",
        json={"page": None, "size": 2, "status": "NEW"},
        page_in="body",
        cast_to=Page[Order],
    )

    assert [order.order_id for order in orders] == [0, 1, 2]
    assert [json.loads(call.request.content) for call in route.calls] == [
        {"page": 0, "size": 2, "status": "NEW"},
        {"page": 1, "size": 2, "status": "NEW"},
    ]


async def test_async_iteration(async_client: AsyncWhitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{API}/orders").mock(side_effect=serve(total=5, size=2, totalPages=3))

    assert [order.order_id async for order in async_pager(async_client, size=2)] == [0, 1, 2, 3, 4]
    assert requested_pages(route) == [0, 1, 2]


async def test_async_early_stop_pages_and_single_page(
    async_client: AsyncWhitebird, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(f"{API}/orders").mock(side_effect=serve(total=10, size=2))

    async for order in async_pager(async_client, size=2):
        if order.order_id == 1:
            break
    assert requested_pages(route) == [0]

    numbers = [page.number async for page in async_pager(async_client, page=3, size=2).pages()]
    assert numbers == [3, 4, 5]

    page = await async_pager(async_client, size=2).page(2)
    assert [order.order_id for order in page.content] == [4, 5]
