"""In-process SSE pub/sub.

`publish({"type": ..., ...})` fans an event out to every `/api/events` client.
Types in use: job, take, snippet, spend, assembly. Safe to call from the event
loop or from threadpool threads (sync endpoints).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

KEEPALIVE_SECONDS = 15.0
QUEUE_MAX = 1000


class Subscriber:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(QUEUE_MAX)
        self.overflowed = False

    def _put(self, event: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # A stalled client: drop it rather than grow without bound.
            self.overflowed = True


_subscribers: set[Subscriber] = set()


def subscribe() -> Subscriber:
    sub = Subscriber(asyncio.get_running_loop())
    _subscribers.add(sub)
    return sub


def unsubscribe(sub: Subscriber) -> None:
    _subscribers.discard(sub)


def publish(event: dict[str, Any]) -> None:
    if "type" not in event:
        raise ValueError("event needs a 'type'")
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    for sub in list(_subscribers):
        if sub.loop is current:
            sub._put(event)
        elif not sub.loop.is_closed():
            sub.loop.call_soon_threadsafe(sub._put, event)


def format_event(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


async def stream(sub: Subscriber, keepalive: float = KEEPALIVE_SECONDS) -> AsyncIterator[str]:
    """Yield SSE frames for one subscriber until it overflows or is cancelled."""
    try:
        yield ": connected\n\n"
        while not sub.overflowed:
            try:
                event = await asyncio.wait_for(sub.queue.get(), keepalive)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield format_event(event)
    finally:
        unsubscribe(sub)


router = APIRouter()


@router.get("/events")
async def events_endpoint(request: Request) -> StreamingResponse:
    sub = subscribe()

    async def gen() -> AsyncIterator[str]:
        async with aclosing(stream(sub)) as frames:
            async for frame in frames:
                if await request.is_disconnected():
                    break
                yield frame

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
