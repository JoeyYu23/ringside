"""Live check (needs ANTHROPIC_API_KEY): LLM fallback classifier + grounded debrief on the demo transcript."""
import asyncio, json, time
from app.coach.engine import CoachEngine, Turn
from app.coach.llm import classify_llm
from app.debrief import debrief_call
from tests.test_engine import DEMO, FACTS

async def main():
    t = time.perf_counter()
    c = await classify_llm("Honestly this week is a total zoo over here, the auditors are in.", {"stage": "discovery", "role": "dm", "meeting_asked": False, "soft_yes": False, "objections": {}})
    print(f"classify_llm: {c.situation if c else None} when={c.when if c else None} in {time.perf_counter()-t:.2f}s")
    t = time.perf_counter()
    c2 = await classify_llm("Yeah my nephew plays hockey on Thursdays so that's a whole thing.", {"stage": "discovery", "role": "dm", "meeting_asked": False, "soft_yes": False, "objections": {}})
    print(f"classify_llm (should be None): {c2.situation if c2 else None} in {time.perf_counter()-t:.2f}s")
    eng = CoachEngine(FACTS, avoid={"gk_send_email.direct_email"})
    transcript = []
    for i, (sp, tx) in enumerate(DEMO, 1):
        await eng.on_turn(Turn(sp, tx))
        transcript.append({"speaker": sp, "text": tx, "seq": i})
    t = time.perf_counter()
    d = await debrief_call(transcript, eng.rule_debrief(), eng.facts, [c.as_dict() for c in eng.cues])
    print(f"debrief source={d.get('source')} in {time.perf_counter()-t:.1f}s err={d.get('llm_error')}")
    print(json.dumps({k: d.get(k) for k in ("outcome", "headline", "what_happened", "what_worked", "what_didnt", "one_improvement", "next_time", "crm", "unverified", "new_objection_candidates")}, indent=1))

asyncio.run(main())
