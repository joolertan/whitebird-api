"""Server-Sent Events streams with automatic reconnection."""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from dataclasses import dataclass
from types import TracebackType
from typing import Generic, Self, TypeVar

import httpx

from ._exceptions import RateLimitError, ServerError, TransportError

__all__ = ["AsyncEventStream", "EventStream", "ServerSentEvent"]

T = TypeVar("T")

RECONNECT_DELAY = 1.0
MAX_RECONNECT_DELAY = 30.0
_TRANSIENT = (TransportError, RateLimitError, ServerError)
_DISCONNECTED = (httpx.HTTPError, httpx.StreamError)


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
    """Incremental parser of the `text/event-stream` format (WHATWG HTML, 9.2.6)."""

    def __init__(self) -> None:
        self.last_event_id: str | None = None
        self.retry: int | None = None
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
            self.last_event_id = value or None
        elif name == "retry" and value.isdigit():
            self.retry = self._block_retry = int(value)
        return None

    def reset(self) -> None:
        self._event, self._data, self._block_retry = "", [], None

    def _dispatch(self) -> ServerSentEvent[str] | None:
        event = None
        if self._data:
            event = ServerSentEvent(
                "\n".join(self._data),
                self._event or "message",
                self.last_event_id,
                self._block_retry,
            )
        self.reset()
        return event

    def delay(self, failures: int = 0) -> float:
        base = RECONNECT_DELAY if self.retry is None else self.retry / 1000
        if not failures:
            return base
        return min(base * 2.0 ** (failures - 1), MAX_RECONNECT_DELAY) * random.uniform(0.75, 1.0)


class EventStream(Generic[T]):
    """Iterator over the events of a stream that reconnects when the connection drops.

    Reconnection resumes from the last received event id. Network failures, HTTP 429 and 5xx
    are retried with exponential backoff; other errors (e.g. 401 after a token refresh, 403)
    end the iteration by raising. Close the stream, or use it as a context manager, to stop.

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
        self._closed = False
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
        """Closes the connection and ends the iteration. Safe to call from another thread."""
        self._closed = True
        if self._response is not None:
            self._response.close()
        # Fails while another thread is inside next(); closing the response already stops it.
        with contextlib.suppress(ValueError):
            self._events.close()

    def _run(self) -> Generator[ServerSentEvent[T]]:
        decoder = _Decoder()
        failures = 0
        while not self._closed:
            try:
                self._response = response = self._connect(decoder.last_event_id)
            except _TRANSIENT:
                failures += 1
                time.sleep(decoder.delay(failures))
                continue
            failures = 0
            try:
                for line in response.iter_lines():
                    event = decoder.feed(line)
                    if event is not None:
                        yield ServerSentEvent(
                            self._parse(event.data), event.event, event.id, event.retry
                        )
            except _DISCONNECTED:
                pass
            finally:
                response.close()
                decoder.reset()
            if not self._closed:
                time.sleep(decoder.delay())


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
        self._closed = False
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
        """Closes the connection and ends the iteration."""
        self._closed = True
        if self._response is not None:
            await self._response.aclose()
        await self._events.aclose()

    async def _run(self) -> AsyncGenerator[ServerSentEvent[T]]:
        decoder = _Decoder()
        failures = 0
        while not self._closed:
            try:
                self._response = response = await self._connect(decoder.last_event_id)
            except _TRANSIENT:
                failures += 1
                await asyncio.sleep(decoder.delay(failures))
                continue
            failures = 0
            try:
                async for line in response.aiter_lines():
                    event = decoder.feed(line)
                    if event is not None:
                        yield ServerSentEvent(
                            self._parse(event.data), event.event, event.id, event.retry
                        )
            except _DISCONNECTED:
                pass
            finally:
                await response.aclose()
                decoder.reset()
            if not self._closed:
                await asyncio.sleep(decoder.delay())
