"""Small helpers for SiLA observable-property subscriptions."""

import asyncio
import collections.abc


async def stream_changes[T](
    read: collections.abc.Callable[[], T],
    *,
    interval: float = 0.25,
) -> collections.abc.AsyncIterator[T]:
    """Yield immediately and then whenever a polled value changes."""
    previous: object = object()
    while True:
        current = read()
        if current != previous:
            yield current
            previous = current
        await asyncio.sleep(interval)
