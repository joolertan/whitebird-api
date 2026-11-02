"""Server-Sent Events streams with automatic reconnection."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from dataclasses import dataclass
from types import TracebackType
from typing import Generic, Self, TypeVar

import httpx

from . import _http
from ._exceptions import RateLimitError, ServerError, TransportError

__all__ = ["AsyncEventStream", "EventStream", "ServerSentEvent"]

T = TypeVar("T")

RECONNECT_DELAY = 1.0
MAX_RECONNECT_DELAY = 30.0
_TRANSIENT = (TransportError, RateLimitError, ServerError)
_DISCONNECTED = (httpx.HTTPError, httpx.StreamError)
_CLOSING_ERRORS = (httpx.HTTPError, httpx.StreamError, OSError, RuntimeError)
_EVENT_STREAM = "text/event-stream"

logger = logging.getLogger("whitebird")


@dataclass(frozen=True)
class ServerSentEvent(Generic[T]):
    """An event received from a stream.

    Attributes:
        data: Event payload, decoded from JSON and validated against the stream's event type.
        event: Event name, e.g. `order-updates`; `message` when the server did not name it.
        id: Last event id seen on the stream, sent back as `Last-Event-ID` on reconnection.
        retry: Reconnection delay in milliseconds requested along with this event.
    """

    data: T
    event: str = "message"
    id: str | None = None
    retry: int | None = None


class _Decoder:
    """Incremental parser of the `text/event-stream` format (WHATWG HTML, 9.2.6).

    `last_event_id` changes when an event is dispatched, so that a connection lost in the
    middle of an event resumes from the previous one.
    """

    def __init__(self) -> None:
        self.last_event_id: str | None = None
        self.retry: int | None = None
        self._id: str | None = None
        self._event = ""
        self._data: list[str] = []
        self._block_retry: int | None = None

    def feed(self, line: str) -> ServerSentEvent[str] | None:
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        elif name == "id" and "\0" not in value:
            self._id = value or None
        elif name == "retry" and value.isdigit():
            self.retry = self._block_retry = int(value)
        return None

    def reset(self) -> None:
        """Drops a partly received event, e.g. when the connection is lost."""
        self._event, self._data, self._block_retry = "", [], None
        self._id = self.last_event_id

    def _dispatch(self) -> ServerSentEvent[str] | None:
        self.last_event_id = self._id
        event = None
        if self._data:
            event = ServerSentEvent(
                "\n".join(self._data),
                self._event or "message",
                self.last_event_id,
                self._block_retry,
            )
        self._event, self._data, self._block_retry = "", [], None
        return event

    def delay(self, failures: int = 0) -> float:
        base = RECONNECT_DELAY if self.retry is None else self.retry / 1000
        if not failures:
            return base
        return _http.backoff(failures - 1, base, max(base, MAX_RECONNECT_DELAY))


def _usable(response: httpx.Response) -> bool:
    """Tells whether a connected stream can be read; logs why not."""
    content_type = response.headers.get("content-type", "")
    if content_type.split(";")[0].strip().lower() == _EVENT_STREAM:
        return True
    logger.warning("The stream answered with %r instead of events, reconnecting", content_type)
    return False


def _parsed(parse: Callable[[str], T], event: ServerSentEvent[str]) -> ServerSentEvent[T] | None:
    """Decodes the data of an event; None, with a warning, for data that does not fit."""
    try:
        data = parse(event.data)
    except ValueError as error:
        logger.warning("Skipping a %r event that could not be decoded: %s", event.event, error)
        return None
    return ServerSentEvent(data, event.event, event.id, event.retry)


def _interrupt(response: httpx.Response | None) -> None:
    """Closes a response, unblocking a thread that waits for its next bytes."""
    if response is None:
        return
    network = response.extensions.get("network_stream")
    sock = network.get_extra_info("socket") if network is not None else None
    if isinstance(sock, socket.socket):
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(*_CLOSING_ERRORS):
        response.close()


class EventStream(Generic[T]):
    """Iterator over the events of a stream that reconnects when the connection drops.

    Reconnection resumes from the last received event id. Network failures, HTTP 429 and 5xx,
    and answers that are not an event stream are retried with exponential backoff; other
    errors (e.g. 401 after a token refresh, 403) end the iteration by raising, and HTTP 204
    ends it quietly. Events whose data does not fit the event type are skipped with a warning
    on the `whitebird` logger. Close the stream, or use it as a context manager, to stop.

    Examples:
        ```python
        with client.notifications.stream() as events:
            for event in events:
                print(event.event, event.data)
        ```
    """

    def __init__(
        self, connect: Callable[[str | None], httpx.Response], parse: Callable[[str], T]
    ) -> None:
        self._connect = connect
        self._parse = parse
        self._response: httpx.Response | None = None
        self._closed = threading.Event()
        self._events = self._run()

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> ServerSentEvent[T]:
        return next(self._events)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Closes the connection and ends the iteration.

        Safe to call from another thread: an iteration waiting for the next event returns.
        """
        self._closed.set()
        _interrupt(self._response)
        # Fails while another thread is inside next(); closing the response already stops it.
        with contextlib.suppress(ValueError):
            self._events.close()

    def _run(self) -> Generator[ServerSentEvent[T]]:
        decoder = _Decoder()
        failures = 0
        while not self._closed.is_set():
            try:
                self._response = response = self._connect(decoder.last_event_id)
            except _TRANSIENT:
                failures += 1
                self._closed.wait(decoder.delay(failures))
                continue
            if self._closed.is_set() or response.status_code == 204:
                response.close()
                return
            if not _usable(response):
                response.close()
                failures += 1
                self._closed.wait(decoder.delay(failures))
                continue
            failures = 0
            try:
                for line in response.iter_lines():
                    event = decoder.feed(line)
                    if event is not None and (parsed := _parsed(self._parse, event)):
                        yield parsed
            except _DISCONNECTED:
                pass
            finally:
                with contextlib.suppress(*_CLOSING_ERRORS):
                    response.close()
                decoder.reset()
            self._closed.wait(decoder.delay())


class AsyncEventStream(Generic[T]):
    """Async iterator over the events of a stream that reconnects when the connection drops.

    Behaves like `EventStream`; close it with `aclose()` or use it as an async context manager.

    Examples:
        ```python
        async with client.notifications.stream() as events:
            async for event in events:
                print(event.event, event.data)
        ```
    """

    def __init__(
        self,
        connect: Callable[[str | None], Awaitable[httpx.Response]],
        parse: Callable[[str], T],
    ) -> None:
        self._connect = connect
        self._parse = parse
        self._response: httpx.Response | None = None
        self._closed = asyncio.Event()
        self._events = self._run()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> ServerSentEvent[T]:
        return await anext(self._events)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Closes the connection and ends the iteration; may be called from another task."""
        self._closed.set()
        if self._response is not None:
            with contextlib.suppress(*_CLOSING_ERRORS):
                await self._response.aclose()
        # Fails while another task is inside anext(); closing the response already stops it.
        with contextlib.suppress(RuntimeError):
            await self._events.aclose()

    async def _wait(self, delay: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._closed.wait(), delay)

    async def _run(self) -> AsyncGenerator[ServerSentEvent[T]]:
        decoder = _Decoder()
        failures = 0
        while not self._closed.is_set():
            try:
                self._response = response = await self._connect(decoder.last_event_id)
            except _TRANSIENT:
                failures += 1
                await self._wait(decoder.delay(failures))
                continue
            if self._closed.is_set() or response.status_code == 204:
                await response.aclose()
                return
            if not _usable(response):
                await response.aclose()
                failures += 1
                await self._wait(decoder.delay(failures))
                continue
            failures = 0
            try:
                async for line in response.aiter_lines():
                    event = decoder.feed(line)
                    if event is not None and (parsed := _parsed(self._parse, event)):
                        yield parsed
            except _DISCONNECTED:
                pass
            finally:
                with contextlib.suppress(*_CLOSING_ERRORS):
                    await response.aclose()
                decoder.reset()
            await self._wait(decoder.delay())
