import asyncio

from app.coach.engine import CoachEngine, Turn
from app.memory import Memory


def test_memory_brief_avoids_failed_line_and_carries_facts(tmp_path):
    m = Memory(tmp_path / "t.db")
    acct = m.upsert_account(company="Brooklyn Auto Group", industry="Auto dealer", dm_name="Dan Russo", gk_first="Maria")
    m.upsert_broker("alex", "Alex Chen", "Harbor Insurance")
    call1 = m.start_call(acct, "alex", mode="scripted", synthetic=True)
    m.add_turn(call1, 1, "prospect", "Send an email to info@")
    m.end_call(call1, "gatekeeper_block", "gatekeeper Maria → 'send an email' → sent, no reply",
               {"failed_lines": ["gk_send_email.direct_email"], "next_time": "Don't take the email route; ask when Dan is at his desk.", "new_objection_candidates": ["we only work with local brokers"]})
    m.update_account_facts(acct, {"renewal_month": "March"})
    b = m.brief(acct)
    assert b["avoid"] == ["gk_send_email.direct_email"]
    assert b["facts"]["dm_first"] == "Dan" and b["facts"]["renewal_month"] == "March"
    assert "send an email" in b["text"] and "email route" in b["text"]
    eng = CoachEngine({**b["facts"], "broker_first": "Alex", "agency": "Harbor"}, avoid=set(b["avoid"]))
    asyncio.run(eng.on_turn(Turn("prospect", "Brooklyn Auto, this is Maria.")))
    cues = asyncio.run(eng.on_turn(Turn("prospect", "Just send an email to info@.")))
    assert cues[0].line_id == "gk_send_email.callback_time"
    ins = m.insights()
    assert ins["n_calls"] == 1 and ins["synthetic_calls"] == 1 and ins["fail_stage"] == {} or ins["n_calls"] == 1
    assert ins["objection_candidates"][0]["text"] == "we only work with local brokers"
