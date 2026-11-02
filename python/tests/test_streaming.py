import asyncio
import itertools
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from tests.support import API, TOKEN_URL, session, tokens
from whitebird import (
    AsyncWhitebird,
    AuthenticationError,
    EventStream,
    MemoryTokenStore,
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
            httpx.Response(200, stream=Dropping(), headers={"content-type": "text/event-stream"}),
            sse('id: 2\ndata: {"orderId": "b"}\n\n'),
        ]
    )

    with stream(client) as events:
        received = [event.id for event in itertools.islice(events, 2)]

    assert received == ["1", "2"]
    assert route.calls.last.request.headers["last-event-id"] == "1"


def test_last_event_id_changes_when_the_event_is_complete(
    client: Whitebird, respx_mock: respx.MockRouter
) -> None:
    class Dropping(httpx.SyncByteStream):
        def __iter__(self) -> Any:
            yield b'id: 1\ndata: {"orderId": "a"}\n\nid: 2\ndata: {"orderId": "b"}\n'
            raise httpx.ReadError("connection reset")

    route = respx_mock.get(STREAM).mock(
        side_effect=[
            httpx.Response(200, stream=Dropping(), headers={"content-type": "text/event-stream"}),
            sse('id: 2\ndata: {"orderId": "b"}\n\n'),
        ]
    )

    with stream(client) as events:
        received = [(event.id, event.data) for event in itertools.islice(events, 2)]

    assert received == [("1", Update(order_id="a")), ("2", Update(order_id="b"))]
    assert route.calls.last.request.headers["last-event-id"] == "1"


def test_events_that_do_not_fit_are_skipped(
    client: Whitebird, respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    respx_mock.get(STREAM).mock(
        return_value=sse(
            "event: sse-status\ndata: CONNECTED\n\n"
            'data: {"orderId": ["not", "a", "string"]}\n\n'
            'data: {"orderId": "a"}\n\n'
        )
    )

    with stream(client) as events:
        event = next(events)

    assert event.data == Update(order_id="a")
    assert "Skipping a 'sse-status' event" in caplog.text


def test_no_content_ends_the_stream(client: Whitebird, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(STREAM).respond(204)

    with stream(client) as events:
        assert list(events) == []
    assert route.call_count == 1


def test_answer_that_is_not_a_stream_is_retried(
    client: Whitebird, respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    route = respx_mock.get(STREAM).mock(
        side_effect=[
            httpx.Response(200, html="<html>maintenance</html>"),
            sse('data: {"orderId": "a"}\n\n'),
        ]
    )

    with stream(client) as events:
        assert next(events).data == Update(order_id="a")
    assert route.call_count == 2
    assert "instead of events" in caplog.text


def test_reconnect_delay_is_bounded() -> None:
    decoder = _Decoder()
    decoder.retry = 2000
    assert decoder.delay() == 2.0
    assert all(0 < decoder.delay(failures) <= 30.0 for failures in (1, 40, 2000, 10**9))


def test_language_is_sent_with_the_stream(respx_mock: respx.MockRouter) -> None:
    from tests.support import BASE_URLS, session
    from whitebird import MemoryTokenStore

    route = respx_mock.get(STREAM).mock(return_value=sse('data: {"orderId": "a"}\n\n'))
    client = Whitebird(token_store=MemoryTokenStore(session()), language="en", base_urls=BASE_URLS)

    with stream(client) as events:
        next(events)
    client.close()

    assert route.calls.last.request.headers["accept-language"] == "en"


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


@pytest.fixture
def idle_server() -> Iterator[str]:
    """A local server that sends one event per connection, then stays silent."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    connections: list[socket.socket] = []

    def serve() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            connections.append(connection)
            connection.recv(65536)
            body = b'id: 1\ndata: {"orderId": "a"}\n\n'
            connection.sendall(
                b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                b"transfer-encoding: chunked\r\n\r\n"
                + f"{len(body):x}\r\n".encode()
                + body
                + b"\r\n"
            )

    threading.Thread(target=serve, daemon=True).start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    listener.close()
    for connection in connections:
        connection.close()


def test_close_from_another_thread_stops_a_waiting_reader(idle_server: str) -> None:
    with Whitebird(
        token_store=MemoryTokenStore(session()), base_urls={"api": idle_server}
    ) as client:
        events = client._engine.stream("api", "GET", "/sse/connect", cast_to=Update)
        assert next(events).data == Update(order_id="a")
        rest: list[Any] = []
        reader = threading.Thread(target=lambda: rest.extend(events), daemon=True)
        reader.start()
        time.sleep(0.1)

        events.close()
        reader.join(5)

    assert not reader.is_alive()
    assert rest == []


async def test_aclose_from_another_task_stops_a_waiting_reader(idle_server: str) -> None:
    async with AsyncWhitebird(
        token_store=MemoryTokenStore(session()), base_urls={"api": idle_server}
    ) as client:
        events = client._engine.stream("api", "GET", "/sse/connect", cast_to=Update)
        assert (await anext(events)).data == Update(order_id="a")
        reader = asyncio.create_task(asyncio.wait_for(_drain(events), 5))
        await asyncio.sleep(0.1)

        await events.aclose()

        assert await reader == []


async def _drain(events: Any) -> list[Any]:
    return [event async for event in events]
