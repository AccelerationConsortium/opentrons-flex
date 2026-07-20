"""Small helpers for SiLA observable-property subscriptions."""

import asyncio
import collections.abc
import typing

_T = typing.TypeVar("_T")


async def stream_changes(
    read: collections.abc.Callable[[], _T],
    *,
    interval: float = 0.25,
) -> collections.abc.AsyncIterator[_T]:
    """Yield immediately and then whenever a polled value changes."""
    previous: object = object()
    while True:
        current = read()
        if current != previous:
            yield current
            previous = current
        await asyncio.sleep(interval)
