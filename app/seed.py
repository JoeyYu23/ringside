"""Seed accounts, brokers and SYNTHETIC call history so memory and insights have something to show.

Everything here is made up and stored with synthetic=1; the UI says so.
"""
from __future__ import annotations

import random
import time

from .memory import Memory

BROKERS = [("alex", "Alex Chen", "Harbor Insurance"), ("priya", "Priya Natarajan", "Harbor Insurance"), ("marcus", "Marcus Bell", "Harbor Insurance")]
ACCOUNTS = [
    dict(id="brooklyn-auto", company="Brooklyn Auto Group", industry="Auto dealer", dm_name="Dan Russo", gk_first="Maria", renewal_month="March", phone="+1 718 555 0142"),
    dict(id="greenpoint-fab", company="Greenpoint Fabrication", industry="Manufacturing", dm_name="Lena Kowalski", gk_first="Sam", phone="+1 718 555 0177"),
    dict(id="harborview-dental", company="Harborview Dental", industry="Healthcare", dm_name="Amy Liu", gk_first="Rosa", renewal_month="October", email="amy@harborviewdental.com", phone="+1 718 555 0110"),
    dict(id="sunset-logistics", company="Sunset Park Logistics", industry="Trucking", dm_name="Omar Haddad", phone="+1 718 555 0163"),
    dict(id="bayridge-hospitality", company="Bay Ridge Hospitality Group", industry="Restaurants", dm_name="Nick Petrakis", gk_first="Elena", renewal_month="June", phone="+1 718 555 0128"),
    dict(id="flatbush-peds", company="Flatbush Pediatrics", industry="Healthcare", dm_name="Wei Chen", gk_first="Tanya", phone="+1 718 555 0199"),
]
DAY = 86400
# hand-written history for the demo accounts: (account, broker, days_ago, outcome, summary, debrief extras)
HISTORY = [
    ("brooklyn-auto", "alex", 24, "gatekeeper_block", "gatekeeper Maria → 'send an email to info@' → email sent, no reply",
     {"failed_lines": ["gk_send_email.direct_email"], "worked_lines": [], "failed_stage": "gatekeeper", "talk_ratio": 0.58, "fillers": 4,
      "next_time": "Don't take the email route again — ask when Dan is usually at his desk, then ask for him by name.", "headline": "Gatekeeper sent us to the info@ inbox; we agreed and lost the call."}),
    ("greenpoint-fab", "priya", 11, "objection_unresolved", "reached Lena → 'not interested' twice → never asked the renewal month",
     {"failed_lines": ["obj_not_interested.genuine"], "worked_lines": ["gk_what_regarding.named"], "failed_stage": "objection", "talk_ratio": 0.71, "fillers": 9,
      "next_time": "Ask the renewal month before anything else; you talked 71% of the call.", "headline": "Got to Lena, pitched too long, two 'not interested's, no renewal date."}),
    ("harborview-dental", "alex", 16, "meeting_booked", "Rosa transferred → Dr. Liu → renewal October → booked Thu 2 PM",
     {"failed_lines": [], "worked_lines": ["gk_what_regarding.renewal_named", "renewal_given.tie_to_meeting"], "failed_stage": None, "talk_ratio": 0.44, "fillers": 2,
      "next_time": "Meeting is booked — this is a follow-up, not a cold call.", "headline": "Clean call: renewal → two times → booked."}),
    ("bayridge-hospitality", "marcus", 6, "callback_agreed", "Nick in the middle of service → call back Tuesday morning",
     {"failed_lines": [], "worked_lines": ["obj_no_time.one_question"], "failed_stage": None, "talk_ratio": 0.5, "fillers": 3,
      "next_time": "He asked for Tuesday morning — open with that, don't re-pitch.", "headline": "Bad time; he chose Tuesday morning himself."}),
    ("flatbush-peds", "marcus", 40, "do_not_call", "Tanya: 'take us off your list'",
     {"failed_lines": ["gk_greeting.dm_only"], "worked_lines": [], "failed_stage": "gatekeeper", "talk_ratio": 0.6, "fillers": 1,
      "next_time": "DO NOT CALL — they asked to be removed on the last call.", "headline": "Do-not-call request."}),
]
OPENERS = ["gk_greeting.named_dm", "gk_greeting.dm_only", "gk_greeting.ask_owner"]
OBJ_LINES = {"gk_what_regarding": ["gk_what_regarding.renewal_named", "gk_what_regarding.named", "gk_what_regarding.generic"],
             "gk_send_email": ["gk_send_email.direct_email", "gk_send_email.callback_time", "gk_send_email.name_first"],
             "obj_all_set": ["obj_all_set.reflex", "obj_all_set.genuine"], "obj_have_broker": ["obj_have_broker.keep_them", "obj_have_broker.second_look"],
             "obj_no_time": ["obj_no_time.one_question", "obj_no_time.reschedule"], "obj_not_interested": ["obj_not_interested.reflex", "obj_not_interested.genuine"]}
# per-line 'worked' odds the synthetic generator uses (the point: the data has structure worth learning)
ODDS = {"gk_greeting.named_dm": 0.8, "gk_greeting.dm_only": 0.6, "gk_greeting.ask_owner": 0.4, "gk_what_regarding.renewal_named": 0.85, "gk_what_regarding.named": 0.65,
        "gk_what_regarding.generic": 0.4, "gk_send_email.direct_email": 0.25, "gk_send_email.callback_time": 0.7, "gk_send_email.name_first": 0.45,
        "obj_all_set.reflex": 0.65, "obj_all_set.genuine": 0.45, "obj_have_broker.keep_them": 0.55, "obj_have_broker.second_look": 0.75,
        "obj_no_time.one_question": 0.7, "obj_no_time.reschedule": 0.5, "obj_not_interested.reflex": 0.55, "obj_not_interested.genuine": 0.3}
BROKER_SKILL = {"alex": 0.15, "priya": -0.1, "marcus": 0.0}


def seed(m: Memory, extra_calls: int = 36, rng_seed: int = 7) -> None:
    rng = random.Random(rng_seed)
    for bid, name, agency in BROKERS:
        m.upsert_broker(bid, name, agency)
    for a in ACCOUNTS:
        m.upsert_account(**a)
    now = time.time()
    for acct, broker, days, outcome, summary, extra in HISTORY:
        cid = m.start_call(acct, broker, mode="seed", synthetic=True)
        for i, line in enumerate((extra.get("worked_lines") or []) + (extra.get("failed_lines") or []), 1):
            sit = line.split(".")[0]
            m.add_cue(cid, {"seq": i, "situation": sit, "line_id": line, "text": "", "kind": "say", "source": "rule", "latency_ms": 0, "t": i * 8.0, "turn_seq": i})
        d = {"outcome": outcome, "source": "seed", "stage_reached": "close" if outcome.startswith("meeting") else extra.get("failed_stage") or "discovery", "timeline": [], "duration_s": 90, **extra}
        m.end_call(cid, outcome, summary, d, ended=now - days * DAY)
    # broader synthetic history so team-level patterns exist
    for k in range(extra_calls):
        a = rng.choice([ACCOUNTS[1], ACCOUNTS[2], ACCOUNTS[4]])  # Sunset Park stays a first contact
        broker = rng.choice(BROKERS)[0]
        skill = BROKER_SKILL[broker]
        cid = m.start_call(a["id"], broker, mode="seed", synthetic=True)
        cues, worked, failed = [], [], []
        opener = rng.choice(OPENERS)
        cues.append(opener)
        ok = rng.random() < ODDS[opener] + skill
        (worked if ok else failed).append(opener)
        stage = "gatekeeper"
        outcome = "gatekeeper_block"
        if ok:
            gk_sit = rng.choice(["gk_what_regarding", "gk_send_email"])
            ln = rng.choice(OBJ_LINES[gk_sit]); cues.append(ln)
            ok2 = rng.random() < ODDS[ln] + skill
            (worked if ok2 else failed).append(ln)
            if ok2:
                stage = "discovery"
                obj_sit = rng.choice(["obj_all_set", "obj_have_broker", "obj_no_time", "obj_not_interested"])
                ln2 = rng.choice(OBJ_LINES[obj_sit]); cues.append(ln2)
                ok3 = rng.random() < ODDS[ln2] + skill
                (worked if ok3 else failed).append(ln2)
                if ok3:
                    asked = rng.random() < 0.75 + skill
                    if asked:
                        stage = "close"
                        outcome = rng.choice(["meeting_booked", "meeting_booked", "meeting_soft_yes", "callback_agreed"])
                    else:
                        outcome = "objection_unresolved"
                else:
                    stage, outcome = "objection", rng.choice(["objection_unresolved", "not_interested", "send_info"])
        for i, line in enumerate(cues, 1):
            m.add_cue(cid, {"seq": i, "situation": line.split(".")[0], "line_id": line, "text": "", "kind": "say", "source": "rule", "latency_ms": 0, "t": i * 9.0, "turn_seq": i})
        tr = min(0.85, max(0.3, rng.gauss(0.55 - skill * 0.5, 0.1)))
        d = {"outcome": outcome, "source": "seed", "worked_lines": worked, "failed_lines": failed, "stage_reached": stage,
             "failed_stage": None if outcome.startswith(("meeting", "callback")) else stage, "talk_ratio": round(tr, 2), "fillers": max(0, int(rng.gauss(5 - skill * 10, 3))),
             "meeting_asked": stage == "close", "timeline": [], "duration_s": rng.randint(40, 240),
             "new_objection_candidates": rng.choice([[], [], [], ["we only work with local brokers"], ["our captive handles that"], ["corporate decides insurance, not us"]])}
        m.end_call(cid, outcome, f"synthetic call · {outcome.replace('_', ' ')}", d, ended=now - rng.randint(2, 60) * DAY)


if __name__ == "__main__":
    import sys
    from .memory import DB_PATH
    path = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    mm = Memory(path)
    seed(mm)
    print(f"seeded {path}: {len(mm.accounts())} accounts, {mm.insights()['n_calls']} synthetic calls")
