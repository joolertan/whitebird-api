"""Pages of paginated listings and the pagers that walk through them."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Generic, TypeVar

from ._models import WhitebirdModel

__all__ = ["AsyncPager", "Page", "SyncPager"]

T = TypeVar("T")


class Page(WhitebirdModel, Generic[T]):
    """One page of a paginated listing, as returned by Whitebird (a Spring Data page)."""

    content: list[T]
    """Items of this page."""
    total_elements: int
    """Number of items across all pages."""
    total_pages: int | None = None
    """Number of pages."""
    number: int | None = None
    """Zero-based index of this page."""
    size: int | None = None
    """Requested page size."""
    number_of_elements: int | None = None
    """Number of items on this page."""
    first: bool | None = None
    """Whether this is the first page."""
    last: bool | None = None
    """Whether this is the last page."""

    @property
    def has_next(self) -> bool:
        """Whether another page follows this one."""
        if not self.content:
            return False
        if self.last is not None:
            return not self.last
        if self.total_pages is not None and self.number is not None:
            return self.number + 1 < self.total_pages
        return True


class SyncPager(Generic[T]):
    """Iterates over the items of a paginated listing, fetching pages lazily.

    Iteration starts at the page passed to the method (the first page by default) and stops
    after the last one. Each page is fetched when the iteration reaches it; a failed fetch
    raises, and iterating again fetches it again.

    Examples:
        ```python
        for order in client.brokerage.otc_orders.list(size=100):
            print(order.uuid)

        page = client.brokerage.otc_orders.list(size=100).page(0)
        print(page.total_elements)
        ```
    """

    def __init__(self, fetch: Callable[[int], Page[T]], start: int = 0) -> None:
        self._fetch = fetch
        self._start = start

    def __iter__(self) -> Iterator[T]:
        for page in self.pages():
            yield from page.content

    def pages(self) -> Iterator[Page[T]]:
        """Yields whole pages, with their metadata, from the starting page to the last one.

        Stops early if Whitebird answers with another page than the one requested, which
        means the endpoint ignores the page number.
        """
        number = self._start
        while True:
            page = self._fetch(number)
            if page.number is not None and page.number != number:
                return
            yield page
            if not page.has_next:
                return
            number += 1

    def page(self, number: int) -> Page[T]:
        """Fetches a single page.

        Args:
            number: Zero-based page index.

        Returns:
            The page with its items and metadata.
        """
        return self._fetch(number)


class AsyncPager(Generic[T]):
    """Asynchronously iterates over the items of a paginated listing, fetching pages lazily.

    Examples:
        ```python
        async for order in client.brokerage.otc_orders.list(size=100):
            print(order.uuid)

        page = await client.brokerage.otc_orders.list(size=100).page(0)
        ```
    """

    def __init__(self, fetch: Callable[[int], Awaitable[Page[T]]], start: int = 0) -> None:
        self._fetch = fetch
        self._start = start

    async def __aiter__(self) -> AsyncIterator[T]:
        async for page in self.pages():
            for item in page.content:
                yield item

    async def pages(self) -> AsyncIterator[Page[T]]:
        """Yields whole pages, with their metadata, from the starting page to the last one.

        Stops early if Whitebird answers with another page than the one requested.
        """
        number = self._start
        while True:
            page = await self._fetch(number)
            if page.number is not None and page.number != number:
                return
            yield page
            if not page.has_next:
                return
            number += 1

    async def page(self, number: int) -> Page[T]:
        """Fetches a single page.

        Args:
            number: Zero-based page index.

        Returns:
            The page with its items and metadata.
        """
        return await self._fetch(number)
