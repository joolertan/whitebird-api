"""Base classes of the generated resource namespaces."""

from __future__ import annotations

from ._engine import AsyncEngine, SyncEngine


class SyncResource:
    """A group of operations of `Whitebird`, e.g. `client.brokerage.otc_orders`."""

    def __init__(self, engine: SyncEngine) -> None:
        self._engine = engine


class AsyncResource:
    """A group of operations of `AsyncWhitebird`, e.g. `client.brokerage.otc_orders`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
