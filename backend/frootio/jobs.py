"""SQLite-backed job queue with asyncio workers inside the FastAPI process.

Handlers:

    @jobs.handler("kind")
    async def h(job_id: int, payload: dict) -> dict: ...

Every job runs as its own asyncio task (so N takes run in parallel). On
startup, jobs left `running` by a previous process are resumed by calling their
handler again: handlers MUST check `jobs.get(job_id)["fal_request_id"]` and
poll instead of resubmitting. Save the request id with `set_fal_request_id`
right after submitting.

Heavy work goes through `await run_heavy(task, args)`: a `python -m
frootio.heavy` subprocess, one at a time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from . import config, db, events

log = logging.getLogger(__name__)

Handler = Callable[[int, dict[str, Any]], Awaitable[dict[str, Any] | None]]
TERMINAL = ("done", "failed", "cancelled")

_handlers: dict[str, Handler] = {}
_worker: Worker | None = None


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        if kind in _handlers and _handlers[kind] is not fn:
            raise ValueError(f"job handler for {kind!r} already registered")
        _handlers[kind] = fn
        return fn

    return register


# --- DB helpers ----------------------------------------------------------------


def _publish(job_id: int, kind: str, status: str, **extra: Any) -> None:
    events.publish({"type": "job", "id": job_id, "kind": kind, "status": status, **extra})


def enqueue(conn, kind: str, payload: dict[str, Any] | None = None) -> int:
    """Insert a queued job (committing `conn`) and wake the worker."""
    ts = db.now()
    cur = conn.execute(
        "INSERT INTO jobs (kind, payload, status, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?)",
        (kind, db.dumps(payload or {}), ts, ts),
    )
    conn.commit()
    job_id = cur.lastrowid
    _publish(job_id, kind, "queued")
    if _worker is not None:
        _worker.wake()
    return job_id


def get(job_id: int) -> dict[str, Any] | None:
    conn = db.connect()
    try:
        job = db.row(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    finally:
        conn.close()
    if job is not None:
        job["payload"] = db.loads(job["payload"], {})
        job["result"] = db.loads(job["result"])
    return job


def set_fal_request_id(job_id: int, request_id: str) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE jobs SET fal_request_id = ?, updated_at = ? WHERE id = ?",
            (request_id, db.now(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def _finish(
    job_id: int,
    status: str,
    *,
    result: Any = None,
    error: str | None = None,
    only_if_running: bool = False,
) -> None:
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE jobs SET status = ?, result = ?, error = ?, updated_at = ? WHERE id = ?"
            + (" AND status = 'running'" if only_if_running else ""),
            (status, None if result is None else db.dumps(result), error, db.now(), job_id),
        )
        conn.commit()
        kind_row = conn.execute("SELECT kind FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    if cur.rowcount and kind_row is not None:
        _publish(job_id, kind_row[0], status, **({"error": error} if error else {}))


def cancel(job_id: int) -> bool:
    """Cancel a queued or running job. Thread-safe. Returns False if already finished.

    A running handler gets CancelledError at its current await; it may clean up
    (e.g. cancel its fal request) before re-raising. The job ends `cancelled`.
    """
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ? AND status = 'queued'",
            (db.now(), job_id),
        )
        conn.commit()
        if cur.rowcount:
            kind = conn.execute("SELECT kind FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
            _publish(job_id, kind, "cancelled")
            return True
        status = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    if status is None or status[0] != "running":
        return False
    if _worker is None:
        # No worker in this process: nothing is executing it, so finish it here.
        _finish(job_id, "cancelled", only_if_running=True)
        return True
    _worker.request_cancel(job_id)
    return True


# --- worker --------------------------------------------------------------------


class Worker:
    def __init__(self, max_concurrent: int, poll_interval: float) -> None:
        self.max_concurrent = max_concurrent
        self.poll_interval = poll_interval
        self.loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._cancel_requested: set[int] = set()
        self._stopping = False
        self._main: asyncio.Task[None] | None = None

    def wake(self) -> None:
        self.loop.call_soon_threadsafe(self._wake.set)

    def request_cancel(self, job_id: int) -> None:
        self.loop.call_soon_threadsafe(self._cancel_task, job_id)

    def _cancel_task(self, job_id: int) -> None:
        task = self._tasks.get(job_id)
        if task is None:
            _finish(job_id, "cancelled", only_if_running=True)
            return
        self._cancel_requested.add(job_id)
        task.cancel()

    def start(self) -> None:
        conn = db.connect()
        try:
            stale = conn.execute(
                "SELECT id, kind, payload FROM jobs WHERE status = 'running' ORDER BY id"
            ).fetchall()
            for r in stale:
                conn.execute(
                    "UPDATE jobs SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
                    (db.now(), r["id"]),
                )
            conn.commit()
        finally:
            conn.close()
        for r in stale:
            log.info("resuming job %s (%s)", r["id"], r["kind"])
            self._spawn(r["id"], r["kind"], db.loads(r["payload"], {}))
        self._main = asyncio.create_task(self._run(), name="frootio-jobs")

    async def stop(self) -> None:
        """Stop without touching job state: running jobs stay `running` and resume next start."""
        self._stopping = True
        tasks = [t for t in (self._main, *self._tasks.values()) if t is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            self._claim()
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_interval)
            except TimeoutError:
                pass
            self._wake.clear()

    def _claim(self) -> None:
        slots = self.max_concurrent - len(self._tasks)
        if slots <= 0:
            return
        conn = db.connect()
        try:
            candidates = conn.execute(
                "SELECT id, kind, payload FROM jobs WHERE status = 'queued' ORDER BY id LIMIT ?",
                (slots,),
            ).fetchall()
            claimed = []
            for r in candidates:
                cur = conn.execute(
                    "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ?"
                    " WHERE id = ? AND status = 'queued'",
                    (db.now(), r["id"]),
                )
                if cur.rowcount:
                    claimed.append(r)
            conn.commit()
        finally:
            conn.close()
        for r in claimed:
            self._spawn(r["id"], r["kind"], db.loads(r["payload"], {}))

    def _spawn(self, job_id: int, kind: str, payload: dict[str, Any]) -> None:
        task = asyncio.create_task(self._execute(job_id, kind, payload), name=f"job-{job_id}")
        self._tasks[job_id] = task

        def done(_: asyncio.Task[None]) -> None:
            self._tasks.pop(job_id, None)
            self._cancel_requested.discard(job_id)
            if not self._stopping:
                self._wake.set()

        task.add_done_callback(done)

    async def _execute(self, job_id: int, kind: str, payload: dict[str, Any]) -> None:
        fn = _handlers.get(kind)
        if fn is None:
            _finish(job_id, "failed", error=f"no handler registered for job kind {kind!r}")
            return
        _publish(job_id, kind, "running")
        try:
            result = await fn(job_id, payload)
        except asyncio.CancelledError:
            if job_id in self._cancel_requested:
                _finish(job_id, "cancelled")
                return
            raise  # shutdown: leave it `running` so it resumes on next start
        except Exception as exc:
            log.exception("job %s (%s) failed", job_id, kind)
            _finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
            return
        _finish(job_id, "done", result=result or {})


async def start(
    max_concurrent: int = config.JOB_MAX_CONCURRENT,
    poll_interval: float = config.JOB_POLL_INTERVAL_SECONDS,
) -> Worker:
    global _worker
    if _worker is not None:
        raise RuntimeError("job worker already running")
    _worker = Worker(max_concurrent, poll_interval)
    _worker.start()
    return _worker


async def stop() -> None:
    global _worker
    worker, _worker = _worker, None
    if worker is not None:
        await worker.stop()


# --- heavy subprocess runner -----------------------------------------------------


class HeavyError(RuntimeError):
    def __init__(self, task: str, returncode: int, stderr: str) -> None:
        self.task, self.returncode, self.stderr = task, returncode, stderr
        tail = stderr.strip().splitlines()[-5:]
        super().__init__(f"heavy task {task!r} exited {returncode}: {' | '.join(tail)}")


_heavy_sems: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _heavy_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    for stale in [lp for lp in _heavy_sems if lp.is_closed()]:
        del _heavy_sems[stale]
    return _heavy_sems.setdefault(loop, asyncio.Semaphore(1))


async def run_heavy(task_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run `python -m frootio.heavy <task> <json>` (one at a time) and return its JSON result."""
    async with _heavy_semaphore():
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "frootio.heavy",
            task_name,
            json.dumps(args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
    err_text = stderr.decode(errors="replace")
    if proc.returncode != 0:
        raise HeavyError(task_name, proc.returncode, err_text)
    if err_text.strip():
        log.debug("heavy %s stderr:\n%s", task_name, err_text)
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise HeavyError(task_name, proc.returncode, f"invalid JSON result: {exc}\n{err_text}") from exc
    if not isinstance(result, dict):
        raise HeavyError(task_name, proc.returncode, "result is not a JSON object")
    return result
