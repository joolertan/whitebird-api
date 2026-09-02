import itertools
from typing import Any

import httpx
import pytest
import respx

from tests.support import API, TOKEN_URL, tokens
from whitebird import (
    AsyncWhitebird,
    AuthenticationError,
    EventStream,
    PermissionDeniedError,
    ServerSentEvent,
    Whitebird,
)
from whitebird.models import WhitebirdModel
from whitebird.streaming import _Decoder

STREAM = f"{API}/sse/connect"


class Update(WhitebirdModel):
    order_id: str | None = None
    status: str | None = None


def decode(text: str) -> list[ServerSentEvent[str]]:
    decoder = _Decoder()
    events = [decoder.feed(line) for line in text.splitlines()]
    return [event for event in events if event is not None]


def sse(body: str) -> httpx.Response:
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def stream(client: Whitebird, cast_to: Any = Update) -> EventStream[Any]:
    return client._engine.stream("api", "GET", "/sse/connect", cast_to=cast_to)


def test_decoder_parses_fields() -> None:
    events = decode(
        ": comment\n"
        "event: order-updates\n"
        "id: 1\n"
        "retry: 2500\n"
        "data: first\n"
        "data:second\n"
        "\n"
        "data: no name\n"
        "\n"
        "id\n"
        "event: ignored\n"
        "\n"
        "data: after reset\n"
        "\n"
    )

    assert events == [
        ServerSentEvent("first\nsecond", "order-updates", "1", 2500),
        ServerSentEvent("no name", "message", "1"),
        ServerSentEvent("after reset", "message", None),
    ]


def test_decoder_needs_a_blank_line_to_dispatch() -> None:
    assert decode("data: pending") == []


def test_typed_events(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(STREAM).mock(
        return_value=sse(
            'event: order-updates\nid: 7\ndata: {"orderId": "o1", "status": "FILLED"}\n\n'
            "event: sse-status\ndata: connected\n\n"
        )
    )

    with stream(client, cast_to=None) as events:
        first, second = next(events), next(events)

    assert first == ServerSentEvent({"orderId": "o1", "status": "FILLED"}, "order-updates", "7")
    assert second.data == "connected"
    request = route.calls.last.request
    assert request.headers["accept"] == "text/event-stream"
    assert request.headers["authorization"].startswith("Bearer ")
    assert request.extensions["timeout"]["read"] is None


def test_reconnects_with_last_event_id(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(STREAM).mock(
        side_effect=[
            sse('id: 1\ndata: {"orderId": "a"}\n\nid: 2\ndata: {"orderId": "b"}\n\n'),
            httpx.ConnectError("down"),
            httpx.Response(503),
            sse('id: 3\ndata: {"orderId": "c"}\n\n'),
        ]
    )

    with stream(client) as events:
        received = [event.data for event in itertools.islice(events, 3)]

    assert received == [Update(order_id="a"), Update(order_id="b"), Update(order_id="c")]
    assert [call.request.headers.get("last-event-id") for call in route.calls] == [
        None,
        "2",
        "2",
        "2",
    ]


def test_dropped_connection_is_resumed(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    class Dropping(httpx.SyncByteStream):
        def __iter__(self) -> Any:
            yield b'id: 1\ndata: {"orderId": "a"}\n\n'
            raise httpx.ReadError("connection reset")

    route = respx_mock.get(STREAM).mock(
        side_effect=[
            httpx.Response(200, stream=Dropping()),
            sse('id: 2\ndata: {"orderId": "b"}\n\n'),
        ]
    )

    with stream(client) as events:
        received = [event.id for event in itertools.islice(events, 2)]

    assert received == ["1", "2"]
    assert route.calls.last.request.headers["last-event-id"] == "1"


def test_unauthorized_stream_refreshes_once_then_stops(
    client: Whitebird, respx_mock: respx.MockRouter
) -> None:
    token = respx_mock.post(TOKEN_URL).mock(return_value=tokens())
    route = respx_mock.get(STREAM).respond(401)

    with pytest.raises(AuthenticationError), stream(client) as events:
        next(events)
    assert (token.call_count, route.call_count) == (1, 2)


def test_forbidden_stream_stops(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(STREAM).respond(403)

    with pytest.raises(PermissionDeniedError), stream(client) as events:
        next(events)


def test_close_ends_iteration(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(STREAM).mock(return_value=sse('data: {"orderId": "a"}\n\n' * 3))
    events = stream(client)

    assert next(events).data == Update(order_id="a")
    events.close()

    assert list(events) == []


async def test_async_stream_reconnects(
    async_client: AsyncWhitebird, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(STREAM).mock(
        side_effect=[
            sse('event: order-updates\nid: 1\ndata: {"orderId": "a"}\n\n'),
            httpx.Response(502),
            sse('id: 2\ndata: {"orderId": "b"}\n\n'),
        ]
    )

    received = []
    async with async_client._engine.stream("api", "GET", "/sse/connect", cast_to=Update) as events:
        async for event in events:
            received.append((event.event, event.data))
            if len(received) == 2:
                break

    assert received == [("order-updates", Update(order_id="a")), ("message", Update(order_id="b"))]
    assert route.calls.last.request.headers["last-event-id"] == "1"


async def test_async_forbidden_stream_stops(
    async_client: AsyncWhitebird, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(STREAM).respond(403)

    with pytest.raises(PermissionDeniedError):
        async with async_client._engine.stream("api", "GET", "/sse/connect") as events:
            await anext(events)
