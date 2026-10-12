"""OpenRouter chat completions returning content and the billed cost.

Requests `usage: {include: true}` so the response's `usage.cost` (USD) can be
logged to the spend table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from . import config

API_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_SECONDS = 300.0


class OpenRouterError(RuntimeError):
    pass


@dataclass
class ChatResult:
    content: str
    cost_usd: float
    model: str
    usage: dict[str, Any]
    raw: dict[str, Any]


async def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    response_format: dict[str, Any] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ChatResult:
    s = config.settings()
    if not s.openrouter_api_key:
        raise OpenRouterError("OPENROUTER_API_KEY is not set")
    body: dict[str, Any] = {
        "model": model or s.openrouter_model,
        "messages": messages,
        "usage": {"include": True},
    }
    if response_format is not None:
        body["response_format"] = response_format
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    headers = {
        "Authorization": f"Bearer {s.openrouter_api_key}",
        "X-Title": "frootio",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport) as client:
        resp = await client.post(API_URL, json=body, headers=headers)
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code >= 400 or not isinstance(data, dict):
        detail = data.get("error") if isinstance(data, dict) else resp.text[:500]
        raise OpenRouterError(f"OpenRouter HTTP {resp.status_code}: {detail}")
    if "error" in data:
        raise OpenRouterError(f"OpenRouter error: {data['error']}")
    choices = data.get("choices") or []
    if not choices:
        raise OpenRouterError("OpenRouter returned no choices")
    content = choices[0].get("message", {}).get("content") or ""
    usage = data.get("usage") or {}
    return ChatResult(
        content=content,
        cost_usd=float(usage.get("cost") or 0.0),
        model=data.get("model", body["model"]),
        usage=usage,
        raw=data,
    )
