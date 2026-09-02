from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from tests.support import BASE_URLS, AsyncClientFactory, ClientFactory, session
from whitebird import AsyncWhitebird, MemoryTokenStore, Whitebird, _http, streaming


@pytest.fixture(autouse=True)
def delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Records the retry delays the engine computes and skips the actual waiting."""
    recorded: list[float] = []
    compute = _http.retry_delay

    def record(attempt: int, response: httpx.Response | None = None) -> float:
        recorded.append(compute(attempt, response))
        return 0.0

    monkeypatch.setattr(_http, "retry_delay", record)
    monkeypatch.setattr(streaming, "RECONNECT_DELAY", 0.0)
    return recorded


@pytest.fixture
def make_client() -> Iterator[ClientFactory]:
    clients: list[Whitebird] = []

    def make(*args: Any, **kwargs: Any) -> Whitebird:
        kwargs.setdefault("base_urls", BASE_URLS)
        clients.append(Whitebird(*args, **kwargs))
        return clients[-1]

    yield make
    for client in clients:
        client.close()


@pytest.fixture
async def make_async_client() -> AsyncIterator[AsyncClientFactory]:
    clients: list[AsyncWhitebird] = []

    def make(*args: Any, **kwargs: Any) -> AsyncWhitebird:
        kwargs.setdefault("base_urls", BASE_URLS)
        clients.append(AsyncWhitebird(*args, **kwargs))
        return clients[-1]

    yield make
    for client in clients:
        await client.aclose()


@pytest.fixture
def client(make_client: ClientFactory) -> Whitebird:
    """A client holding a valid session."""
    return make_client(token_store=MemoryTokenStore(session()))


@pytest.fixture
def async_client(make_async_client: AsyncClientFactory) -> AsyncWhitebird:
    """An async client holding a valid session."""
    return make_async_client(token_store=MemoryTokenStore(session()))
