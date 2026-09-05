"""The coach: turns in, one glanceable cue out. Pure Python, no I/O, microsecond rule path.

Never invents: every cue's text is a playbook line rendered only with facts we actually hold.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from .classify import Classification, classify_prospect, read_broker, words
from .playbook import Playbook, load

STAGES = ["intro", "gatekeeper", "discovery", "objection", "close", "ended"]
NEGATIVE = {"negative", "blocked"}
RAMBLE_WORDS = 60
RAMBLE_SECONDS = 20.0
FILLER_LIMIT = 3
CUE_MIN_GAP_S = 1.2

LlmClassifier = Callable[[str, dict[str, Any]], Awaitable[Classification | None]]


@dataclass
class Turn:
    speaker: str                 # prospect | broker
    text: str
    t0: float = 0.0
    t1: float = 0.0
    seq: int = 0
    final: bool = True


@dataclass
class Cue:
    seq: int
    line_id: str
    situation: str
    label: str
    text: str
    kind: str                    # say | stop | ask | info
    stage: str
    role: str
    source: str                  # rule | llm | memory
    latency_ms: float
    t: float
    turn_seq: int | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def meeting_slots(now: datetime | None = None) -> dict[str, str]:
    """Two concrete proposals the broker can offer: next Tuesday 10 AM and next Thursday 2 PM."""
    now = now or datetime.now()
    def nxt(weekday: int, hour: int) -> datetime:
        d = now + timedelta(days=1)
        while d.weekday() != weekday:
            d += timedelta(days=1)
        return d.replace(hour=hour, minute=0, second=0, microsecond=0)
    a, b = nxt(1, 10), nxt(3, 14)
    if b < a:
        a, b = b, a
    fmt = lambda d: f"{d.strftime('%A')} at {d.strftime('%I').lstrip('0')}"
    return {"time_1": fmt(a), "time_2": fmt(b)}


class CoachEngine:
    def __init__(self, facts: dict[str, Any] | None = None, avoid: set[str] | None = None, playbook: Playbook | None = None,
                 llm: LlmClassifier | None = None, clock: Callable[[], float] = time.monotonic, llm_timeout_s: float = 1.5) -> None:
        self.pb = playbook or load()
        self.facts: dict[str, Any] = {k: v for k, v in (facts or {}).items() if v}
        self.facts.setdefault("broker_first", "the broker")
        self.facts.setdefault("agency", "the agency")
        for k, v in meeting_slots().items():
            self.facts.setdefault(k, v)
        self.avoid: set[str] = set(avoid or ())
        self.llm, self.llm_timeout_s, self.clock = llm, llm_timeout_s, clock
        # state
        self.stage, self.role = "intro", "unknown"
        self.turns: list[Turn] = []
        self.cues: list[Cue] = []
        self.objection_counts: Counter = Counter()
        self.situations: list[tuple[int, str]] = []   # (turn seq, situation)
        self.soft_yes = self.meeting_asked = self.meeting_confirmed = False
        self.permission_asked = self.permission_granted = self.asked_renewal = self.expecting_dm = False
        self.momentum = 0.0
        self.broker_words = self.prospect_words = 0
        self.broker_s = self.prospect_s = 0.0
        self.fillers = 0
        self.used: set[str] = set()
        self.outcome_hint: str | None = None
        self._seq = 0
        self._last_cue_t = -1e9
        self._last_line: str | None = None
        self.started = self.clock()

    # ---- helpers ---------------------------------------------------------------------
    def _cue(self, situation: str, source: str, t_in: float, when: str = "any", turn_seq: int | None = None, force: bool = False, extra_facts: dict | None = None) -> Cue | None:
        facts = dict(self.facts)
        if extra_facts:
            facts.update(extra_facts)
        pick = self.pb.choose(situation, facts, when=when, exclude=self.avoid, used=self.used)
        if pick is None:
            return None
        line, text = pick
        now = self.clock()
        if not force and line.kind == "say" and (now - self._last_cue_t) < CUE_MIN_GAP_S and self._last_line == line.id:
            return None
        self._seq += 1
        cue = Cue(seq=self._seq, line_id=line.id, situation=situation, label=self.pb.label(situation), text=text, kind=line.kind,
                  stage=self.stage, role=self.role, source=source, latency_ms=round((now - t_in) * 1000, 2), t=now - self.started, turn_seq=turn_seq)
        self.cues.append(cue)
        self.used.add(line.id)
        self._last_cue_t, self._last_line = now, line.id
        return cue

    def _bump(self, signal: float) -> None:
        self.momentum = round(0.6 * self.momentum + 0.4 * signal, 3)

    def _set_stage(self, stage: str) -> None:
        if stage != self.stage and stage in STAGES:
            self.stage = stage

    # ---- pre-call ---------------------------------------------------------------------
    def brief(self, text: str) -> Cue | None:
        """Account memory shown before the dial (kind=info, never spoken)."""
        return self._cue("memory_brief", "memory", self.clock(), extra_facts={"brief": text}, force=True)

    # ---- turns --------------------------------------------------------------------------
    async def on_turn(self, turn: Turn) -> list[Cue]:
        t_in = self.clock()
        turn.seq = turn.seq or len(self.turns) + 1
        self.turns.append(turn)
        dur = max(0.0, (turn.t1 or 0.0) - (turn.t0 or 0.0))
        if turn.speaker == "broker":
            self.broker_words += words(turn.text)
            self.broker_s += dur
            return self._on_broker(turn, t_in)
        self.prospect_words += words(turn.text)
        self.prospect_s += dur
        return await self._on_prospect(turn, t_in)

    def _on_broker(self, turn: Turn, t_in: float) -> list[Cue]:
        r = read_broker(turn.text)
        out: list[Cue] = []
        self.fillers += r.fillers
        if r.renewal_ask:
            self.asked_renewal = True
        if r.permission_ask and self.role == "dm":
            self.permission_asked = True
        if r.meeting_ask and self.role == "dm":
            self.meeting_asked = True
            self._set_stage("close")
        if r.numbers:
            c = self._cue("broker_numbers", "rule", t_in, turn_seq=turn.seq, force=True)
            if c: out.append(c)
        if self.soft_yes and not self.meeting_confirmed and r.word_count > 18 and not r.meeting_ask:
            c = self._cue("broker_pitching_after_yes", "rule", t_in, turn_seq=turn.seq, force=True)
            if c: out.append(c)
        elif r.word_count > RAMBLE_WORDS or ((turn.t1 - turn.t0) > RAMBLE_SECONDS and r.word_count > 25):
            c = self._cue("broker_ramble", "rule", t_in, turn_seq=turn.seq, force=True)
            if c: out.append(c)
        elif r.fillers >= FILLER_LIMIT:
            c = self._cue("broker_filler", "rule", t_in, turn_seq=turn.seq)
            if c: out.append(c)
        return out

    def on_broker_partial(self, text: str, elapsed_s: float) -> list[Cue]:
        """Interim transcript while the broker is still talking: catch a monologue before it ends."""
        if elapsed_s > RAMBLE_SECONDS and words(text) > 40:
            c = self._cue("broker_ramble", "rule", self.clock(), force=True)
            return [c] if c else []
        return []

    async def _on_prospect(self, turn: Turn, t_in: float) -> list[Cue]:
        c = classify_prospect(turn.text, self)
        if c.situation is None and "hold" not in c.signals and self.llm is not None and words(turn.text) >= 4:
            try:
                alt = await asyncio.wait_for(self.llm(turn.text, self.snapshot()), timeout=self.llm_timeout_s)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — silence beats a late or wrong cue
                alt = None
            if alt and alt.situation in self.pb.situations and not (self.role == "dm" and alt.situation.startswith("gk_")):
                c = alt          # a gatekeeper situation cannot happen once the decision maker is on the line
                c.source = alt.source if alt.source.startswith("llm") else "llm"
        return self._apply(c, turn, t_in)

    def _apply(self, c: Classification, turn: Turn, t_in: float) -> list[Cue]:
        out: list[Cue] = []
        for k, v in c.facts.items():
            if v and (not self.facts.get(k) or (k == "email" and self.role == "dm")):
                self.facts[k] = v
        if c.role_hint == "gatekeeper" and self.role != "dm":
            self.role = "gatekeeper"
            if self.stage == "intro":
                self._set_stage("gatekeeper")
        elif c.role_hint == "dm":
            self.role = "dm"
            self.expecting_dm = False
            if self.stage in ("intro", "gatekeeper"):
                self._set_stage("discovery")
        sit = c.situation
        if sit is None:
            if self.role == "dm" and self.stage == "discovery" and not self.asked_renewal and not self.facts.get("renewal_month") and self.permission_granted:
                cue = self._cue("ask_discovery", "rule", t_in, turn_seq=turn.seq)
                if cue: out.append(cue)
            return out
        self.situations.append((turn.seq, sit))
        if sit.startswith("obj_"):
            self.objection_counts[sit] += 1
            if self.role == "dm":
                self._set_stage("objection")
        elif self.role == "dm" and self.stage == "objection" and sit not in ("hard_no",):
            self._set_stage("discovery")
        if sit == "gk_transfer":
            self.expecting_dm = True
        if sit == "dm_identified":
            self.permission_asked = True   # the line we hand the broker asks for twenty seconds
        if sit == "dm_permission_granted":
            self.permission_granted = True
        if sit == "soft_yes":
            self.soft_yes = True
            self._set_stage("close")
            self.outcome_hint = "meeting_soft_yes"
        if sit == "meeting_confirmed":
            self.meeting_confirmed = True
            self._set_stage("close")
            self.outcome_hint = "meeting_booked"
        if sit == "hard_no":
            self.outcome_hint = "do_not_call"
        if sit in ("gk_not_available", "gk_take_message") and not self.outcome_hint:
            self.outcome_hint = "gatekeeper_block"
        if sit == "renewal_given" and self.role == "dm":
            self.permission_granted = True
        # momentum
        if c.signals & NEGATIVE or "final" in c.signals and sit == "hard_no":
            self._bump(-1.0)
        elif "positive" in c.signals:
            self._bump(1.0)
        else:
            self._bump(0.0)
        cue = self._cue(sit, c.source, t_in, when=c.when, turn_seq=turn.seq, force=(sit in ("hard_no", "soft_yes", "meeting_confirmed")))
        if cue:
            out.append(cue)
        # nudge toward the ask once the renewal is known and no ask has been made
        if self.role == "dm" and sit in ("dm_permission_granted",) and self.facts.get("renewal_month") and not self.meeting_asked and not cue:
            a = self._cue("ask_meeting", "rule", t_in, turn_seq=turn.seq)
            if a: out.append(a)
        return out

    # ---- views --------------------------------------------------------------------------
    def talk_ratio(self) -> float:
        """Share of the conversation that is the broker talking (0..1); words when no timings."""
        if self.broker_s + self.prospect_s > 1.0:
            return round(self.broker_s / (self.broker_s + self.prospect_s), 3)
        tot = self.broker_words + self.prospect_words
        return round(self.broker_words / tot, 3) if tot else 0.0

    def snapshot(self) -> dict[str, Any]:
        return {"stage": self.stage, "role": self.role, "momentum": self.momentum, "talk_ratio": self.talk_ratio(),
                "soft_yes": self.soft_yes, "meeting_asked": self.meeting_asked, "meeting_confirmed": self.meeting_confirmed,
                "facts": {k: v for k, v in self.facts.items() if k in ("company", "dm_first", "gk_first", "renewal_month", "email", "industry")},
                "objections": dict(self.objection_counts), "outcome_hint": self.outcome_hint, "fillers": self.fillers,
                "turns": len(self.turns), "elapsed_s": round(self.clock() - self.started, 1)}

    def rule_debrief(self) -> dict[str, Any]:
        """Deterministic debrief: outcome, timeline, which lines were followed by a positive next turn."""
        outcome = self.outcome_hint or ("gatekeeper_block" if self.role == "gatekeeper" and self.stage in ("gatekeeper", "intro") else "no_outcome")
        if self.meeting_confirmed:
            outcome = "meeting_booked"
        elif self.soft_yes:
            outcome = "meeting_soft_yes"
        elif self.outcome_hint == "do_not_call":
            outcome = "do_not_call"
        elif self.role == "dm" and self.objection_counts and outcome == "no_outcome":
            outcome = "objection_unresolved"
        worked, failed = [], []
        won = outcome in ("meeting_booked", "meeting_soft_yes", "callback_agreed")
        progress = {"gk_transfer", "dm_identified", "dm_permission_granted", "renewal_given", "soft_yes", "meeting_confirmed"}
        for cue in self.cues:
            if cue.kind != "say" or cue.turn_seq is None:
                continue
            following = [s for seq, s in self.situations if seq > cue.turn_seq][:2]
            if not following:
                continue
            if following[0] in progress or (len(following) > 1 and following[1] in progress):
                worked.append(cue.line_id)      # the conversation moved forward within two prospect turns
            elif not won and (following[0].startswith("obj_") or following[0] in ("gk_not_available", "gk_take_message", "gk_send_email", "hard_no", "gk_all_set")):
                failed.append(cue.line_id)      # only a lost call teaches us what not to repeat
        failed_stage = None
        if outcome in ("gatekeeper_block", "objection_unresolved", "no_outcome", "do_not_call"):
            failed_stage = "gatekeeper" if self.role != "dm" else ("close" if self.meeting_asked else ("objection" if self.objection_counts else "discovery"))
        improvements = []
        tr = self.talk_ratio()
        if tr > 0.65:
            improvements.append(f"You talked {int(tr*100)}% of the call — ask a question every two sentences.")
        if self.role == "dm" and not self.meeting_asked:
            improvements.append("You never asked for the meeting. Offer two times as soon as the renewal comes up.")
        if self.soft_yes and any(c.situation == "broker_pitching_after_yes" for c in self.cues):
            improvements.append("They said yes and you kept pitching. Confirm the time and stop.")
        if self.fillers >= 6:
            improvements.append(f"{self.fillers} filler words — pause instead of 'um'.")
        if self.role != "dm" and self.objection_counts.get("gk_send_email", 0) + sum(1 for _, s in self.situations if s == "gk_send_email") >= 1 and not self.facts.get("dm_first"):
            improvements.append("Get the decision maker's name before you agree to email.")
        if not improvements:
            improvements.append("Keep the opener; ask for the meeting one turn earlier.")
        return {"outcome": outcome, "worked_lines": worked, "failed_lines": failed, "failed_stage": failed_stage,
                "one_improvement": improvements[0], "improvements": improvements, "talk_ratio": tr, "fillers": self.fillers,
                "objections": dict(self.objection_counts), "facts": {k: v for k, v in self.facts.items() if k in ("dm_first", "gk_first", "renewal_month", "email")},
                "stage_reached": self.stage, "timeline": [{"turn": seq, "situation": s} for seq, s in self.situations], "duration_s": round(self.clock() - self.started, 1)}
