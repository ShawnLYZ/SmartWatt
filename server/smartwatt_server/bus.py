"""Thread-to-asyncio bridge and WebSocket fan-out.

Paho's client runs on its own thread. This is the only place where a
payload crosses from that thread into the event loop, and it is
deliberately one-way and lossy at the client: a browser that stops draining
gets dropped rather than being allowed to back-pressure ingest.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class Subscriber:
    def __init__(self, max_queue: int) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self.dropped = False

    async def get(self) -> dict:
        return await self.queue.get()

    def offer(self, message: dict) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.dropped = True


class Bus:
    def __init__(self, max_queue: int = 64) -> None:
        self._max_queue = max_queue
        self._subscribers: list[Subscriber] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def client_count(self) -> int:
        return len(self._subscribers)

    async def subscribe(self) -> Subscriber:
        subscriber = Subscriber(self._max_queue)
        self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)

    def publish_threadsafe(self, kind: str, payload: dict) -> None:
        """Called from paho's thread. Never blocks and never raises."""
        if self._loop is None:
            return
        message = {"kind": kind, "payload": payload}
        try:
            self._loop.call_soon_threadsafe(self._fan_out, message)
        except RuntimeError:
            log.debug("event loop closed; dropping message")

    def _fan_out(self, message: dict) -> None:
        for subscriber in list(self._subscribers):
            subscriber.offer(message)
