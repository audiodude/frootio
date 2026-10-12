"""Thin async wrapper over fal_client's queue API.

Keys come from `FAL_KEY`. Submitting returns the request id immediately so the
caller can persist it (`jobs.set_fal_request_id`) before polling; after a
restart the caller polls `status`/`result` by id instead of resubmitting.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import fal_client

from . import config

FalClientError = fal_client.FalClientError
FalClientHTTPError = fal_client.FalClientHTTPError


class FalNotConfigured(RuntimeError):
    pass


@dataclass
class FalStatus:
    state: Literal["queued", "running", "completed"]
    queue_position: int | None = None
    logs: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    # Set when the request completed with an error (validation, safety checker, model failure).
    error: str | None = None
    error_type: str | None = None


_clients: dict[asyncio.AbstractEventLoop, fal_client.AsyncClient] = {}


def _client() -> fal_client.AsyncClient:
    """One AsyncClient per event loop (its httpx client and locks are loop-bound)."""
    key = config.settings().fal_key
    if not key:
        raise FalNotConfigured("FAL_KEY is not set")
    loop = asyncio.get_running_loop()
    for stale in [lp for lp in _clients if lp.is_closed()]:
        del _clients[stale]
    client = _clients.get(loop)
    if client is None or client.key != key:
        client = _clients[loop] = fal_client.AsyncClient(key=key)
    return client


async def upload_file(path: Path | str) -> str:
    """Upload a local file to fal storage; returns its URL."""
    return await _client().upload_file(Path(path))


async def upload_bytes(data: bytes, content_type: str, file_name: str | None = None) -> str:
    return await _client().upload(data, content_type, file_name=file_name)


async def submit(endpoint: str, arguments: dict[str, Any]) -> str:
    """Queue a request and return its `request_id` (does not wait for the result)."""
    handle = await _client().submit(endpoint, arguments)
    return handle.request_id


async def status(endpoint: str, request_id: str, *, with_logs: bool = False) -> FalStatus:
    st = await _client().status(endpoint, request_id, with_logs=with_logs)
    if isinstance(st, fal_client.Queued):
        return FalStatus("queued", queue_position=st.position)
    if isinstance(st, fal_client.InProgress):
        return FalStatus("running", logs=st.logs or [])
    if isinstance(st, fal_client.Completed):
        return FalStatus(
            "completed",
            logs=st.logs or [],
            metrics=st.metrics or {},
            error=st.error,
            error_type=st.error_type,
        )
    raise ValueError(f"unexpected fal status {st!r}")


async def result(endpoint: str, request_id: str) -> dict[str, Any]:
    """Fetch the result of a completed request. Raises FalClientHTTPError on fal errors."""
    return await _client().result(endpoint, request_id)


async def cancel(endpoint: str, request_id: str) -> None:
    await _client().cancel(endpoint, request_id)


def is_configured() -> bool:
    return config.settings().fal_key is not None
