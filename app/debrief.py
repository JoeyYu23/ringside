"""Post-call debrief: rule-based instantly, then a transcript-grounded LLM version. CRM values must be quoted from the transcript."""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field

MODEL = os.environ.get("COACH_LLM_MODEL", "claude-opus-5")
OUTCOMES = ("meeting_booked", "meeting_soft_yes", "callback_agreed", "send_info", "gatekeeper_block", "objection_unresolved", "not_interested", "do_not_call", "no_outcome")
Outcome = Literal[OUTCOMES]


class CRM(BaseModel):
    contact_name: str | None = Field(None, description="Decision maker's name exactly as said on the call, else null")
    contact_role: str | None = None
    gatekeeper_name: str | None = None
    email: str | None = Field(None, description="Only if an email address was spoken on the call")
    phone: str | None = None
    renewal_month: str | None = Field(None, description="Month named on the call for the policy renewal, else null")
    current_broker_or_carrier: str | None = Field(None, description="Only if the prospect named one")
    next_step: str | None = None
    next_step_when: str | None = None


class ObjectionNote(BaseModel):
    quote: str = Field(description="The prospect's words, verbatim")
    kind: str
    response_used: str = Field(description="What the broker said next, briefly")
    converted: bool = Field(description="Did the conversation move forward after the response?")


class Debrief(BaseModel):
    outcome: Outcome
    headline: str = Field(description="One sentence: what happened, in plain words")
    what_happened: list[str] = Field(description="2-4 short bullets, chronological")
    what_worked: list[str]
    what_didnt: list[str]
    one_improvement: str = Field(description="The single most valuable change for next time, one sentence, specific")
    next_time: str = Field(description="One line the coach should show this broker before dialing this account again")
    crm: CRM
    objections: list[ObjectionNote]
    new_objection_candidates: list[str] = Field(description="Objections the prospect raised that are not in the known list; verbatim; empty if none")


SYSTEM = """You debrief an insurance broker's outbound cold call from its transcript. Be concrete and short; the broker reads this in 20 seconds.
Rules:
- Use only what is in the transcript. Every CRM field must be something a speaker actually said; if it was not said, use null. Never infer an email, a name, a month, a carrier or a phone number.
- Quote objections verbatim.
- 'what_worked' / 'what_didnt' are about the broker's moves, not the prospect's mood.
- Known objection kinds: all_set, not_interested, have_broker, no_time, send_info, renewal_far, price, coverage_question, call_back_later, bad_experience, send_email, not_available, what_regarding, who_reach, expecting, take_message. Anything else goes in new_objection_candidates.
- The transcript comes from speech recognition: names may be misheard and speakers may overlap. Do not 'correct' a name to something that was not said."""


def _line_desc(line_id: str) -> str:
    from .coach.playbook import load
    ln = load().lines.get(line_id)
    return f"{ln.label} → “{ln.text}”" if ln else line_id


def _fallback(rule: dict[str, Any], transcript: list[dict], facts: dict) -> dict[str, Any]:
    outcome = rule["outcome"]
    heads = {"meeting_booked": "Meeting booked.", "meeting_soft_yes": "They agreed to meet; time not locked.", "gatekeeper_block": "Never got past the gatekeeper.",
             "objection_unresolved": "Reached the decision maker; objection not resolved.", "do_not_call": "Do-not-call request.", "no_outcome": "No outcome."}
    from .coach.playbook import load
    happened = [load().label(t["situation"]) for t in rule["timeline"][:4]]
    return {"outcome": outcome, "headline": heads.get(outcome, outcome), "what_happened": happened or ["No recognisable stages."],
            "what_worked": [_line_desc(l) for l in rule["worked_lines"][:3]], "what_didnt": [_line_desc(l) for l in rule["failed_lines"][:3]],
            "one_improvement": rule["one_improvement"], "next_time": rule["one_improvement"],
            "crm": {"contact_name": facts.get("dm_name") or facts.get("dm_first"), "gatekeeper_name": facts.get("gk_first"), "email": facts.get("email"),
                    "renewal_month": facts.get("renewal_month"), "next_step": "Send invite" if outcome == "meeting_booked" else None},
            "objections": [{"quote": "", "kind": k.replace("obj_", ""), "response_used": "", "converted": False} for k in rule["objections"]],
            "new_objection_candidates": [], "source": "rules", **{k: rule[k] for k in ("worked_lines", "failed_lines", "failed_stage", "talk_ratio", "fillers", "stage_reached", "timeline", "duration_s")}}


def _ground(d: dict[str, Any], transcript_text: str) -> dict[str, Any]:
    """Null any CRM value whose key token does not appear in the transcript (the code-level never-invent guard)."""
    low = transcript_text.lower()
    unverified = []
    crm = d.get("crm") or {}
    for k in ("contact_name", "gatekeeper_name", "email", "phone", "renewal_month", "current_broker_or_carrier"):
        v = crm.get(k)
        if not v:
            continue
        tokens = [x for x in re.findall(r"[a-z0-9@.]+", str(v).lower()) if len(x) > 1]
        ok = tokens and all(tok in low for tok in tokens)
        if k == "email":
            ok = str(v).lower() in low or re.sub(r"[@.]", " ", str(v).lower()).split()[0] in low
        if not ok:
            unverified.append({k: v})
            crm[k] = None
    d["crm"] = crm
    d["unverified"] = unverified
    return d


async def debrief_call(transcript: list[dict], rule: dict[str, Any], facts: dict, cues: list[dict] | None = None, timeout_s: float = 40.0) -> dict[str, Any]:
    """Returns the LLM debrief (grounded) or the rule-based fallback; never raises."""
    base = _fallback(rule, transcript, facts)
    from . import llm_gemini, llm_groq
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or llm_gemini.available() or llm_groq.available()):
        return base
    lines = [f"[{t['speaker']}] {t['text']}" for t in transcript]
    text = "\n".join(lines)
    cue_lines = "\n".join(f"- after turn {c.get('turn_seq')}: ({c['situation']}) {c['text']}" for c in (cues or []) if c.get("kind") == "say") or "(none)"
    prompt = (f"Account facts before the call (from CRM, may be stale): {json.dumps({k: v for k, v in facts.items() if k in ('company', 'dm_first', 'gk_first', 'renewal_month', 'industry')})}\n"
              f"Rule-based read of the call: outcome={rule['outcome']}, stage_reached={rule['stage_reached']}, talk_ratio={rule['talk_ratio']}, objections={rule['objections']}\n"
              f"Coach lines shown to the broker during the call:\n{cue_lines}\n\nTRANSCRIPT\n{text}")
    d: dict[str, Any] | None = None
    provider = "claude"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        try:
            client = anthropic.AsyncAnthropic()
            resp = await asyncio.wait_for(client.messages.parse(model=MODEL, max_tokens=4000, system=SYSTEM,
                                                                messages=[{"role": "user", "content": prompt}], output_format=Debrief,
                                                                output_config={"effort": "medium"}), timeout=timeout_s)
            if resp.stop_reason != "refusal" and resp.parsed_output is not None:
                d = resp.parsed_output.model_dump()
        except Exception as e:  # noqa: BLE001 — fall through to Gemini, then to rules
            base["llm_error"] = f"{type(e).__name__}: {e}"[:200]
    from . import llm_groq
    if d is None and llm_groq.available():
        try:
            provider = "groq"
            raw = await asyncio.wait_for(llm_groq.json_completion(SYSTEM, prompt, Debrief.model_json_schema(), model=llm_groq.BIG_MODEL, temperature=0.2), timeout=timeout_s)
            d = Debrief.model_validate(raw).model_dump()
        except Exception as e:  # noqa: BLE001
            base["llm_error"] = (base.get("llm_error", "") + f" | groq {type(e).__name__}: {e}")[:300]
            d = None
    if d is None and llm_gemini.available():
        try:
            provider = "gemini"
            raw = await asyncio.wait_for(llm_gemini.json_completion(SYSTEM, prompt, Debrief), timeout=timeout_s)
            d = Debrief.model_validate(raw).model_dump()
        except Exception as e:  # noqa: BLE001
            base["llm_error"] = (base.get("llm_error", "") + f" | gemini {type(e).__name__}: {e}")[:300]
    if d is None:
        return base
    d = _ground(d, text)
    d["source"] = "llm"
    d["provider"] = provider
    for k in ("worked_lines", "failed_lines", "failed_stage", "talk_ratio", "fillers", "stage_reached", "timeline", "duration_s"):
        d[k] = rule[k]
    return d
