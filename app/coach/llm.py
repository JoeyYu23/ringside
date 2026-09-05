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
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


async def classify_llm(text: str, snapshot: dict) -> Classification | None:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    state = {k: snapshot.get(k) for k in ("stage", "role", "meeting_asked", "soft_yes", "objections")}
    resp = await _client.messages.create(
        model=MODEL, max_tokens=120,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"Call state: {json.dumps(state)}\nProspect said: {text!r}"}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "low"},
    )
    if resp.stop_reason == "refusal":
        return None
    raw = next((b.text for b in resp.content if b.type == "text"), "{}")
    d = json.loads(raw)
    if d.get("situation") in (None, "none"):
        return None
    return Classification(situation=d["situation"], role_hint=None if d.get("role") == "unknown" else d.get("role"),
                          when="reflex" if d.get("reflex") else "genuine", source="llm", signals={"negative"} if d["situation"].startswith("obj_") else set())
