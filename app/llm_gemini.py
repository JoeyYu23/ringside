"""Gemini text fallback for the two LLM jobs (used when the Anthropic call fails, e.g. no credit). Same schemas, same grounding."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from google import genai
from google.genai import types

MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-3.5-flash")
_client: genai.Client | None = None


def available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def _c() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


def _clean(schema: Any) -> Any:
    """Gemini's response_schema rejects additionalProperties; strip it recursively from dict schemas."""
    if isinstance(schema, dict):
        return {k: _clean(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_clean(x) for x in schema]
    return schema


async def json_completion(system: str, prompt: str, schema: dict[str, Any] | type, temperature: float = 0.2, max_tokens: int = 4000, think: bool = True) -> dict[str, Any]:
    cfg = types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json", response_schema=_clean(schema) if isinstance(schema, dict) else schema,
                                      temperature=temperature, max_output_tokens=max_tokens,
                                      thinking_config=None if think else types.ThinkingConfig(thinking_budget=0))
    resp = await asyncio.get_event_loop().run_in_executor(None, lambda: _c().models.generate_content(model=MODEL, contents=prompt, config=cfg))
    if not resp.text:
        raise RuntimeError(f"gemini returned no text (finish={resp.candidates[0].finish_reason if resp.candidates else None})")
    return json.loads(resp.text)
