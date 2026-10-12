from __future__ import annotations

import asyncio
import json
import threading
from contextlib import aclosing

import pytest

from frootio import events


async def test_publish_fans_out_to_all_subscribers() -> None:
    a, b = events.subscribe(), events.subscribe()
    try:
        events.publish({"type": "take", "id": 1})
        assert a.queue.get_nowait() == {"type": "take", "id": 1}
        assert b.queue.get_nowait() == {"type": "take", "id": 1}
    finally:
        events.unsubscribe(a)
        events.unsubscribe(b)


async def test_publish_from_worker_thread() -> None:
    sub = events.subscribe()
    try:
        t = threading.Thread(target=events.publish, args=({"type": "spend", "amount_usd": 1.2},))
        t.start()
        t.join()
        assert await asyncio.wait_for(sub.queue.get(), 2) == {"type": "spend", "amount_usd": 1.2}
    finally:
        events.unsubscribe(sub)


def test_publish_requires_type() -> None:
    with pytest.raises(ValueError):
        events.publish({"id": 1})


async def test_stream_formats_sse_and_unsubscribes() -> None:
    sub = events.subscribe()
    async with aclosing(events.stream(sub, keepalive=0.05)) as frames:
        assert await anext(frames) == ": connected\n\n"
        assert await anext(frames) == ": keepalive\n\n"
        events.publish({"type": "job", "id": 3, "status": "done"})
        frame = await anext(frames)
        assert frame.startswith("data: ") and frame.endswith("\n\n")
        assert json.loads(frame[len("data: ") :]) == {"type": "job", "id": 3, "status": "done"}
    assert sub not in events._subscribers


async def test_overflowing_subscriber_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events, "QUEUE_MAX", 2)
    sub = events.subscribe()
    for i in range(3):
        events.publish({"type": "job", "id": i})
    assert sub.overflowed
    frames = [f async for f in events.stream(sub)]
    assert frames == [": connected\n\n"]
    assert sub not in events._subscribers
