from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from frootio import db, events, jobs


@pytest.fixture(autouse=True)
def clean_handlers() -> Iterator[None]:
    before = dict(jobs._handlers)
    yield
    jobs._handlers.clear()
    jobs._handlers.update(before)


@pytest.fixture
async def worker(conn: sqlite3.Connection) -> AsyncIterator[jobs.Worker]:
    w = await jobs.start(poll_interval=0.05)
    yield w
    await jobs.stop()


async def wait_status(job_id: int, *statuses: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        job = jobs.get(job_id)
        assert job is not None
        if job["status"] in statuses:
            return job
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"job {job_id} stuck in {job['status']}, wanted {statuses}")
        await asyncio.sleep(0.02)


async def test_handler_runs_and_result_is_stored(conn: sqlite3.Connection, worker: jobs.Worker) -> None:
    sub = events.subscribe()

    @jobs.handler("t_add")
    async def add(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return {"sum": payload["a"] + payload["b"]}

    job_id = jobs.enqueue(conn, "t_add", {"a": 2, "b": 3})
    job = await wait_status(job_id, "done")
    assert job["result"] == {"sum": 5}
    assert job["attempts"] == 1
    seen = []
    while not sub.queue.empty():
        ev = sub.queue.get_nowait()
        if ev["type"] == "job" and ev["id"] == job_id:
            seen.append(ev["status"])
    events.unsubscribe(sub)
    assert seen == ["queued", "running", "done"]


async def test_handler_exception_marks_failed(conn: sqlite3.Connection, worker: jobs.Worker) -> None:
    @jobs.handler("t_boom")
    async def boom(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("nope")

    job = await wait_status(jobs.enqueue(conn, "t_boom"), "failed")
    assert job["error"] == "ValueError: nope"


async def test_unknown_kind_fails(conn: sqlite3.Connection, worker: jobs.Worker) -> None:
    job = await wait_status(jobs.enqueue(conn, "t_nobody_handles_this"), "failed")
    assert "no handler" in job["error"]


async def test_parallel_jobs_run_concurrently(conn: sqlite3.Connection, worker: jobs.Worker) -> None:
    running = 0
    peak = 0
    gate = asyncio.Event()

    @jobs.handler("t_par")
    async def par(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        if running == 3:
            gate.set()
        await asyncio.wait_for(gate.wait(), 5)
        running -= 1
        return {}

    ids = [jobs.enqueue(conn, "t_par") for _ in range(3)]
    for i in ids:
        await wait_status(i, "done")
    assert peak == 3


async def test_resume_polls_instead_of_resubmitting(conn: sqlite3.Connection) -> None:
    """A job left `running` with a fal_request_id is resumed without a new submit."""
    calls: list[str] = []

    @jobs.handler("t_fal")
    async def fal_like(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = jobs.get(job_id)["fal_request_id"]
        if request_id is None:
            calls.append("submit")
            request_id = f"req-{payload['n']}"
            jobs.set_fal_request_id(job_id, request_id)
        else:
            calls.append("poll")
        return {"request_id": request_id}

    ts = db.now()
    job_id = conn.execute(
        "INSERT INTO jobs (kind, payload, status, fal_request_id, attempts, created_at, updated_at)"
        " VALUES ('t_fal', '{\"n\": 7}', 'running', 'req-7', 1, ?, ?)",
        (ts, ts),
    ).lastrowid
    conn.commit()

    await jobs.start(poll_interval=0.05)
    try:
        job = await wait_status(job_id, "done")
        assert calls == ["poll"]
        assert job["result"] == {"request_id": "req-7"}
        assert job["attempts"] == 2

        fresh = await wait_status(jobs.enqueue(conn, "t_fal", {"n": 8}), "done")
        assert calls == ["poll", "submit"]
        assert fresh["fal_request_id"] == "req-8"
    finally:
        await jobs.stop()


async def test_shutdown_leaves_job_running_for_resume(conn: sqlite3.Connection) -> None:
    started = asyncio.Event()
    release: asyncio.Event | None = asyncio.Event()

    @jobs.handler("t_long")
    async def long(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if jobs.get(job_id)["fal_request_id"] is None:
            jobs.set_fal_request_id(job_id, "req-long")
        started.set()
        if release is not None:
            await release.wait()
        return {"finished": True}

    await jobs.start(poll_interval=0.05)
    job_id = jobs.enqueue(conn, "t_long")
    await asyncio.wait_for(started.wait(), 5)
    await jobs.stop()
    job = jobs.get(job_id)
    assert job["status"] == "running"
    assert job["fal_request_id"] == "req-long"

    release = None  # the "new process" handler completes immediately
    await jobs.start(poll_interval=0.05)
    try:
        job = await wait_status(job_id, "done")
        assert job["result"] == {"finished": True}
    finally:
        await jobs.stop()


async def test_cancel_running_job(conn: sqlite3.Connection, worker: jobs.Worker) -> None:
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    @jobs.handler("t_cancel")
    async def blocker(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cleaned_up.set()  # e.g. cancel the fal request
            raise
        return {}

    job_id = jobs.enqueue(conn, "t_cancel")
    await asyncio.wait_for(started.wait(), 5)
    assert jobs.cancel(job_id) is True
    await wait_status(job_id, "cancelled")
    assert cleaned_up.is_set()
    assert jobs.cancel(job_id) is False


async def test_cancel_queued_job_without_worker(conn: sqlite3.Connection) -> None:
    job_id = jobs.enqueue(conn, "t_whatever")
    assert jobs.cancel(job_id) is True
    assert jobs.get(job_id)["status"] == "cancelled"


async def test_run_heavy_echo() -> None:
    out = await jobs.run_heavy("echo", {"x": [1, 2], "s": "ü"})
    assert out["echo"] == {"x": [1, 2], "s": "ü"}


async def test_run_heavy_is_serialized() -> None:
    a, b = await asyncio.gather(jobs.run_heavy("echo", {"i": 1}), jobs.run_heavy("echo", {"i": 2}))
    assert a["echo"] == {"i": 1} and b["echo"] == {"i": 2}
    assert a["pid"] != b["pid"]


async def test_run_heavy_waits_for_semaphore() -> None:
    sem = jobs._heavy_semaphore()
    await sem.acquire()
    task = asyncio.create_task(jobs.run_heavy("echo", {}))
    await asyncio.sleep(0.2)
    assert not task.done()  # blocked on the single heavy slot
    sem.release()
    assert (await asyncio.wait_for(task, 30))["echo"] == {}


async def test_run_heavy_unknown_task_raises() -> None:
    with pytest.raises(jobs.HeavyError, match="unknown heavy task"):
        await jobs.run_heavy("no_such_task", {})
