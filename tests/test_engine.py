"""Deterministic coach tests: scripted calls in, cues out. No audio, no network."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.coach.classify import Classification
from app.coach.engine import CoachEngine, Turn, meeting_slots
from app.coach.playbook import load

FACTS = {"company": "Brooklyn Auto Group", "dm_first": "Dan", "renewal_month": "March", "broker_first": "Alex", "agency": "Harbor Insurance",
         "time_1": "Tuesday at 10", "time_2": "Thursday at 2"}


def run(engine: CoachEngine, script: list[tuple[str, str]]):
    """Feed (speaker, text) turns; return [(text, [cue situations])]."""
    out = []
    for speaker, text in script:
        cues = asyncio.run(engine.on_turn(Turn(speaker=speaker, text=text)))
        out.append((speaker, text, cues))
    return out


DEMO = [
    ("prospect", "Brooklyn Auto Group, this is Maria."),
    ("broker", "Hi Maria, Alex Chen with Harbor Insurance calling for Dan. Is he in?"),
    ("prospect", "What is this regarding?"),
    ("broker", "It's about the March renewal, Dan will know. Is he available?"),
    ("prospect", "Can you just send an email to info@brooklynauto.com?"),
    ("broker", "Will do. What time is Dan usually free? I'll call back then instead."),
    ("prospect", "Hold on, let me see if he's free."),
    ("prospect", "This is Dan."),
    ("broker", "Dan, Alex Chen with Harbor Insurance. I'll be brief, got twenty seconds?"),
    ("prospect", "Sure, go ahead."),
    ("broker", "Your policy renews in March. A second set of eyes before then, worth fifteen minutes?"),
    ("prospect", "We're all set, we have a broker."),
    ("broker", "Keep them. A second look before March costs you nothing. Fifteen minutes?"),
    ("prospect", "Alright, fine. When?"),
    ("broker", "Tuesday at 10, fifteen minutes. What's the best email for the invite?"),
    ("prospect", "dan@brooklynauto.com"),
]


def test_demo_path_gatekeeper_to_booked_meeting():
    eng = CoachEngine(FACTS, avoid={"gk_send_email.direct_email"})
    res = run(eng, DEMO)
    sits = [[c.situation for c in cues] for _, _, cues in res]
    assert sits[0] == ["gk_greeting"] and eng.facts["gk_first"] == "Maria"
    assert res[0][2][0].text == "Hi Maria — Alex calling for Dan. Is Dan in?"
    assert sits[2] == ["gk_what_regarding"] and "March" in res[2][2][0].text
    # memory said the email route failed last time -> the callback variant, not the email one
    assert sits[4] == ["gk_send_email"] and res[4][2][0].line_id == "gk_send_email.callback_time"
    assert sits[6] == ["gk_transfer"] and res[6][2][0].kind == "stop"
    assert sits[7] == ["dm_identified"] and res[7][2][0].role == "dm" and res[7][2][0].stage == "discovery"
    assert sits[9] == ["dm_permission_granted"] and "March" in res[9][2][0].text
    assert eng.meeting_asked and eng.stage == "close" and eng.role == "dm"
    assert sits[11] == ["obj_have_broker"] and res[11][2][0].line_id == "obj_have_broker.second_look"
    assert sits[13] == ["soft_yes"] and res[13][2][0].kind == "stop" and "Tuesday at 10" in res[13][2][0].text
    assert sits[15] == ["meeting_confirmed"] and eng.facts["email"] == "dan@brooklynauto.com"
    d = eng.rule_debrief()
    assert d["outcome"] == "meeting_booked"
    assert "gk_send_email.callback_time" in d["worked_lines"]
    assert d["failed_lines"] == []  # a won call never feeds the avoid-list
    assert d["facts"]["email"] == "dan@brooklynauto.com"
    # broker turns never produce 'say' cues on this clean script
    assert all(not cues for sp, _, cues in res if sp == "broker")


def test_never_invents_every_cue_is_a_rendered_playbook_line():
    pb = load()
    eng = CoachEngine(FACTS)
    run(eng, DEMO)
    assert eng.cues, "expected cues"
    for cue in eng.cues:
        assert pb.is_rendered_line(cue.text, eng.facts) == cue.line_id
        for forbidden in ("$", "%", "premium", "carrier relationship"):
            assert forbidden not in cue.text.lower() or cue.situation == "broker_numbers"


def test_unknown_slot_never_guessed():
    eng = CoachEngine({"company": "Acme Logistics", "broker_first": "Alex", "agency": "Harbor"})  # no dm_first, no renewal
    res = run(eng, [("prospect", "Acme Logistics, this is Priya."), ("prospect", "What is this regarding?")])
    assert res[0][2][0].line_id == "gk_greeting.ask_owner"
    assert res[1][2][0].line_id == "gk_what_regarding.generic"
    assert "{" not in res[1][2][0].text


def test_rule_path_latency_under_5ms():
    eng = CoachEngine(FACTS)
    worst = 0.0
    for speaker, text in DEMO:
        t = time.perf_counter()
        asyncio.run(eng.on_turn(Turn(speaker=speaker, text=text)))
        worst = max(worst, time.perf_counter() - t)
    assert worst < 0.005, f"rule path took {worst*1000:.1f} ms"
    assert all(c.latency_ms < 5 for c in eng.cues)


def test_reflex_vs_genuine_objection():
    eng = CoachEngine(FACTS)
    run(eng, [("prospect", "This is Dan.")])
    r1 = run(eng, [("prospect", "Not interested.")])[0][2]
    assert r1[0].line_id == "obj_not_interested.reflex"
    r2 = run(eng, [("prospect", "Look, I told you, we're really not interested, please stop wasting my time.")])[0][2]
    assert r2[0].line_id == "obj_not_interested.genuine"
    assert eng.objection_counts["obj_not_interested"] == 2 and eng.stage == "objection"


def test_price_and_coverage_questions_get_the_dont_guess_line():
    eng = CoachEngine(FACTS)
    run(eng, [("prospect", "Speaking.")])
    price = run(eng, [("prospect", "How much cheaper can you get me?")])[0][2]
    assert price[0].situation == "obj_price" and "won't guess" in price[0].text
    cov = run(eng, [("prospect", "Do you guys even handle workers comp for auto dealers?")])[0][2]
    assert cov[0].situation == "obj_coverage_question" and "won't guess" in cov[0].text


def test_broker_nudges_ramble_numbers_pitching_after_yes():
    eng = CoachEngine(FACTS)
    run(eng, [("prospect", "This is Dan."), ("broker", "Got twenty seconds?"), ("prospect", "Sure.")])
    ramble = run(eng, [("broker", " ".join(["we do a lot of things for dealers"] * 12))])[0][2]
    assert [c.situation for c in ramble] == ["broker_ramble"] and ramble[0].kind == "stop"
    numbers = run(eng, [("broker", "I can probably save you 20% on your premium.")])[0][2]
    assert "broker_numbers" in [c.situation for c in numbers]
    run(eng, [("broker", "Fifteen minutes Tuesday at 10?"), ("prospect", "Yeah that works.")])
    assert eng.soft_yes
    pitch = run(eng, [("broker", "Great, and we also do umbrella, cyber, fleet, we have great markets, been in business thirty years, lots of dealers")])[0][2]
    assert [c.situation for c in pitch] == ["broker_pitching_after_yes"]


def test_silence_when_nothing_recognised_and_llm_fallback_stays_in_playbook():
    eng = CoachEngine(FACTS)
    run(eng, [("prospect", "This is Dan.")])
    quiet = run(eng, [("prospect", "Yeah my nephew plays hockey on Thursdays so that's a whole thing.")])[0][2]
    assert quiet == []  # nothing to say -> say nothing

    async def llm(text, snap):
        return Classification(situation="obj_no_time", role_hint="dm", when="reflex", source="llm")

    eng2 = CoachEngine(FACTS, llm=llm)
    run(eng2, [("prospect", "This is Dan.")])
    r = run(eng2, [("prospect", "Honestly this week is a total zoo over here, the auditors are in.")])[0][2]
    assert r and r[0].situation == "obj_no_time" and r[0].source == "llm"
    assert load().is_rendered_line(r[0].text, eng2.facts) == r[0].line_id

    async def slow(text, snap):
        await asyncio.sleep(5)
        return Classification(situation="obj_no_time")

    eng3 = CoachEngine(FACTS, llm=slow, llm_timeout_s=0.05)
    run(eng3, [("prospect", "This is Dan.")])
    assert run(eng3, [("prospect", "Honestly this week is a total zoo over here.")])[0][2] == []


def test_hard_no_and_gatekeeper_block_outcomes():
    eng = CoachEngine(FACTS)
    r = run(eng, [("prospect", "Brooklyn Auto, this is Maria."), ("prospect", "Take us off your list and don't call again.")])
    assert r[1][2][0].situation == "hard_no" and eng.rule_debrief()["outcome"] == "do_not_call"
    eng2 = CoachEngine(FACTS)
    r2 = run(eng2, [("prospect", "Brooklyn Auto, this is Maria."), ("prospect", "He's in a meeting, can I take a message?")])
    assert r2[1][2][0].situation in ("gk_not_available", "gk_take_message")
    d = eng2.rule_debrief()
    assert d["outcome"] == "gatekeeper_block" and d["failed_stage"] == "gatekeeper"
    assert "gk_greeting.named_dm" in d["failed_lines"]


def test_renewal_month_captured_and_meeting_ask_nudged():
    eng = CoachEngine({"company": "Acme", "dm_first": "Priya", "broker_first": "Alex", "agency": "Harbor", "time_1": "Tuesday at 10", "time_2": "Thursday at 2"})
    run(eng, [("prospect", "This is Priya."), ("broker", "Got twenty seconds?"), ("prospect", "Sure.")])
    r = run(eng, [("prospect", "Our policy comes up in September I think.")])[0][2]
    assert eng.facts["renewal_month"] == "September" and r[0].situation == "renewal_given" and "Tuesday at 10" in r[0].text
    far = CoachEngine(FACTS)
    run(far, [("prospect", "Speaking.")])
    rf = run(far, [("prospect", "We just renewed in March, it's not up for a year.")])[0][2]
    assert rf[0].situation == "obj_renewal_far"


def test_meeting_slots_are_next_tuesday_and_thursday():
    s = meeting_slots()
    assert s["time_1"].startswith(("Tuesday", "Thursday")) and s["time_2"].startswith(("Tuesday", "Thursday")) and s["time_1"] != s["time_2"]
