"""LLM fallback classifier: only for prospect utterances no rule recognises. Output is a situation id, never text."""
from __future__ import annotations

import json
import os

import anthropic

from .classify import Classification
from .playbook import load

MODEL = os.environ.get("COACH_LLM_MODEL", "claude-opus-5")
NOT_PROSPECT = {"memory_brief", "ask_discovery", "ask_meeting", "gk_greeting", "gk_transfer", "meeting_confirmed", "dm_permission_granted"}
SITUATIONS = [s for s in load().situations if not s.startswith("broker_") and s not in NOT_PROSPECT]
SYSTEM = (
    "You label one utterance from the OTHER side of an insurance broker's outbound cold call. "
    "Pick the single situation id that best describes what the prospect just said, or 'none' if nothing applies "
    "(small talk, unclear audio, a question the broker should simply answer). Prefer 'none' over a stretch. "
    "'reflex' means a short knee-jerk brush-off; false when they elaborate or repeat. Situation ids:\n"
    + "\n".join(f"- {s}: {load().label(s)}" for s in SITUATIONS)
)
SCHEMA = {"type": "object", "properties": {"situation": {"type": "string", "enum": SITUATIONS + ["none"]},
                                           "role": {"type": "string", "enum": ["gatekeeper", "dm", "unknown"]},
                                           "reflex": {"type": "boolean"}},
          "required": ["situation", "role", "reflex"], "additionalProperties": False}
_client: anthropic.AsyncAnthropic | None = None


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY"))


_anthropic_ok = True
PROVIDERS = [x.strip() for x in os.environ.get("COACH_CLASSIFIER_PROVIDERS", "groq,gemini,claude").split(",") if x.strip()]


async def _claude(user: str) -> dict | None:
    global _client, _anthropic_ok
    if not _anthropic_ok or not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    try:
        resp = await _client.messages.create(
            model=MODEL, max_tokens=120,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "low"},
        )
    except anthropic.BadRequestError as e:
        if "credit" in str(e).lower():
            _anthropic_ok = False   # stop paying the round-trip on every turn
        return None
    except Exception:  # noqa: BLE001
        return None
    if resp.stop_reason == "refusal":
        return None
    return json.loads(next((b.text for b in resp.content if b.type == "text"), "{}"))


async def _groq(user: str) -> dict | None:
    from .. import llm_groq
    if not llm_groq.available():
        return None
    try:
        return await llm_groq.json_completion(SYSTEM, user, SCHEMA, max_tokens=400)
    except Exception:  # noqa: BLE001
        return None


async def _gemini(user: str) -> dict | None:
    from .. import llm_gemini
    if not llm_gemini.available():
        return None
    try:
        return await llm_gemini.json_completion(SYSTEM, user, SCHEMA, temperature=0.0, max_tokens=512, think=False)
    except Exception:  # noqa: BLE001
        return None


_PROVIDER_FN = {"claude": _claude, "groq": _groq, "gemini": _gemini}


async def classify_llm(text: str, snapshot: dict) -> Classification | None:
    state = {k: snapshot.get(k) for k in ("stage", "role", "meeting_asked", "soft_yes", "objections")}
    user = f"Call state: {json.dumps(state)}\nProspect said: {text!r}"
    d = None
    for name in PROVIDERS:
        fn = _PROVIDER_FN.get(name)
        if fn is None:
            continue
        d = await fn(user)
        if d:
            d["_provider"] = name
            break
    if not d:
        return None
    if d.get("situation") in (None, "none"):
        return None
    if d["situation"] not in SITUATIONS:
        return None
    return Classification(situation=d["situation"], role_hint=None if d.get("role") == "unknown" else d.get("role"),
                          when="reflex" if d.get("reflex") else "genuine", source="llm:" + d.get("_provider", "?"), signals={"negative"} if d["situation"].startswith("obj_") else set())
