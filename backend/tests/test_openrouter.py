from __future__ import annotations

import json

import httpx
import pytest

from frootio import openrouter


async def test_chat_requests_usage_and_returns_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "some/model",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "cost": 0.00042},
            },
        )

    res = await openrouter.chat(
        [{"role": "user", "content": "hello"}], model="some/model", transport=httpx.MockTransport(handle)
    )
    assert res.content == "hi"
    assert res.cost_usd == pytest.approx(0.00042)
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["usage"] == {"include": True}
    assert seen["body"]["model"] == "some/model"


async def test_chat_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    transport = httpx.MockTransport(lambda r: httpx.Response(402, json={"error": {"message": "no credit"}}))
    with pytest.raises(openrouter.OpenRouterError, match="402"):
        await openrouter.chat([{"role": "user", "content": "x"}], transport=transport)


async def test_chat_requires_key() -> None:
    with pytest.raises(openrouter.OpenRouterError, match="OPENROUTER_API_KEY"):
        await openrouter.chat([{"role": "user", "content": "x"}])
