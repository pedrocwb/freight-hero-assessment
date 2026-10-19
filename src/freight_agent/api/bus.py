"""In-memory pub/sub for the event stream, and the Server-Sent Events wire format.

One process, one simulation, a handful of browser tabs: a broker would be overkill. Each
subscriber gets its own bounded queue, so a slow tab never slows the simulation down or
grows memory without limit: when its queue is full, its oldest message is dropped. The
UI refetches on the next `clock` event, so a dropped message costs a stale panel for one
step, not a wrong one.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel

HEARTBEAT = ": heartbeat\n\n"
CONNECTED = ": connected\n\n"
DEFAULT_MAX_QUEUED = 256


def sse_message(event: str, data: BaseModel | Mapping[str, Any]) -> str:
    """`event: <name>` and one `data:` line of JSON. JSON escapes newlines, so the data
    always fits on one line, as the SSE format needs."""
    payload = data.model_dump_json() if isinstance(data, BaseModel) else json.dumps(data)
    return f"event: {event}\ndata: {payload}\n\n"


class Subscription:
    """One subscriber's bounded queue of formatted messages."""

    def __init__(self, max_queued: int) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queued)
        self.dropped = 0

    def put(self, message: str) -> None:
        if self._queue.full():
            self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(message)

    async def next(self, timeout_s: float) -> str | None:
        """The next message, or None when none arrives within the timeout."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout_s)
        except TimeoutError:
            return None


class EventBus:
    def __init__(self, max_queued: int = DEFAULT_MAX_QUEUED) -> None:
        self._max_queued = max_queued
        self._subscriptions: set[Subscription] = set()

    @property
    def subscribers(self) -> int:
        return len(self._subscriptions)

    @contextmanager
    def subscribe(self) -> Iterator[Subscription]:
        subscription = Subscription(self._max_queued)
        self._subscriptions.add(subscription)
        try:
            yield subscription
        finally:
            self._subscriptions.discard(subscription)

    def publish(self, event: str, data: BaseModel | Mapping[str, Any]) -> None:
        if not self._subscriptions:
            return
        message = sse_message(event, data)
        for subscription in tuple(self._subscriptions):
            subscription.put(message)

    async def stream(
        self, initial: Callable[[], Awaitable[list[str]]], *, heartbeat_s: float
    ) -> AsyncGenerator[str]:
        """An SSE body: a comment, the `initial` messages, then every published message.

        It subscribes before it reads `initial`, so nothing published in between is lost.
        A comment goes out after `heartbeat_s` of silence, which keeps proxies from closing
        the connection and lets the server notice a client that went away.
        """
        with self.subscribe() as subscription:
            yield CONNECTED
            for message in await initial():
                yield message
            while True:
                message = await subscription.next(heartbeat_s)
                yield HEARTBEAT if message is None else message
