"""falapi maps fal_client's queue API; no network calls are made here."""

from __future__ import annotations

from typing import Any

import fal_client
import pytest

from frootio import falapi


class FakeClient:
    def __init__(self, st: Any) -> None:
        self.st = st
        self.calls: list[tuple[str, ...]] = []

    async def status(self, endpoint: str, request_id: str, *, with_logs: bool = False) -> Any:
        self.calls.append(("status", endpoint, request_id))
        return self.st

    async def submit(self, endpoint: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(("submit", endpoint))
        return type("Handle", (), {"request_id": "req-1"})()


@pytest.mark.parametrize(
    ("st", "expected"),
    [
        (fal_client.Queued(position=3), falapi.FalStatus("queued", queue_position=3)),
        (fal_client.InProgress(logs=None), falapi.FalStatus("running")),
        (
            fal_client.Completed(logs=None, metrics={"t": 1}, error="safety", error_type="content_policy"),
            falapi.FalStatus("completed", metrics={"t": 1}, error="safety", error_type="content_policy"),
        ),
    ],
)
async def test_status_mapping(monkeypatch: pytest.MonkeyPatch, st: Any, expected: falapi.FalStatus) -> None:
    fake = FakeClient(st)
    monkeypatch.setattr(falapi, "_client", lambda: fake)
    assert await falapi.status("a/b", "r1") == expected
    assert fake.calls == [("status", "a/b", "r1")]


async def test_submit_returns_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient(None)
    monkeypatch.setattr(falapi, "_client", lambda: fake)
    assert await falapi.submit("a/b", {"prompt": "x"}) == "req-1"


async def test_requires_key() -> None:
    assert falapi.is_configured() is False
    with pytest.raises(falapi.FalNotConfigured):
        await falapi.submit("a/b", {})
