"""Groq (OpenAI-compatible chat completions, JSON mode). Fastest option for the real-time classifier fallback."""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

BASE = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
FAST_MODEL = os.environ.get("GROQ_FAST_MODEL", "openai/gpt-oss-20b")
BIG_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
_client: httpx.AsyncClient | None = None


def available() -> bool:
    return bool(os.environ.get("GROQ_API_KEY"))


def _c() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=BASE, timeout=30.0, headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"})
    return _client


async def json_completion(system: str, prompt: str, schema: dict[str, Any] | None = None, model: str | None = None, temperature: float = 0.0, max_tokens: int = 4000) -> dict[str, Any]:
    """JSON-mode completion. The schema is shown to the model in the prompt (Groq validates JSON, not the schema)."""
    sys_text = system + ("\n\nRespond with a single JSON object matching this JSON schema exactly:\n" + json.dumps(schema) if schema else "\n\nRespond with a single JSON object.")
    body = {"model": model or FAST_MODEL, "temperature": temperature, "max_tokens": max_tokens, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": sys_text}, {"role": "user", "content": prompt}]}
    r = await _c().post("/chat/completions", json=body)
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])
