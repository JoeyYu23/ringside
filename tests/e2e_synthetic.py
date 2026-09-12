"""Synthetic-prospect end-to-end: a scripted broker (macOS `say`) talks to the Gemini Live gatekeeper/decision maker.

Proves the demo path without a human: the gatekeeper transfers only because the broker's (coach-fed) lines name the
renewal and the decision maker; the DM agrees only to the concrete fifteen-minute ask. Needs GEMINI_API_KEY + a server.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request

import websockets

from tests.e2e_audio import FRAME, _tts, post, voice

BASE = os.environ.get("COACH_BASE", "http://127.0.0.1:8765")
WS = BASE.replace("http", "ws", 1)
# what the broker says after each prospect turn; keyed by what the coach is expected to have shown
BROKER_LINES = [
    ("gk_greeting", "Hi Maria, Alex Chen with Harbor Insurance. Is Dan in?"),
    ("gk_what_regarding", "It's about the March renewal, Dan will know. Is he available?"),
    ("gk_send_email", "Will do. What time is Dan usually free? I'll call back then instead."),
    ("gk_expecting", "Not today, it's about the March renewal. Dan will want to hear it. Is he in?"),
    ("gk_not_available", "No problem. When is Dan usually at his desk? I'll call back then."),
    ("dm_identified", "Dan, Alex Chen with Harbor Insurance. I'll be brief, got twenty seconds?"),
    ("dm_permission_granted", "Your policy renews in March. A second set of eyes before then, worth fifteen minutes?"),
    ("obj_all_set", "Most people I call are. When is your renewal? I'll call ninety days before, not now."),
    ("obj_have_broker", "Keep them. A second look before March costs you nothing. Fifteen minutes Tuesday at ten?"),
    ("renewal_given", "March, so we'd want to look soon. Fifteen minutes, Tuesday at ten or Thursday at two?"),
    ("obj_no_time", "Then one question and I'm gone: when does your policy renew?"),
    ("obj_not_interested", "Fair, I called out of the blue. Thirty seconds, then you decide?"),
    ("soft_yes", "Tuesday at ten, fifteen minutes. What's the best email for the invite?"),
    ("meeting_confirmed", "Invite is on its way. Thanks Dan, talk Tuesday."),
]
FALLBACK = "Sorry, say that again?"


async def main() -> int:
    c = post("/api/calls", {"account_id": "brooklyn-auto", "broker_id": "alex", "mode": "synthetic"})
    cid = c["call_id"]
    print(f"call {cid} (synthetic prospect)")
    events: asyncio.Queue = asyncio.Queue()
    ev_ws = await websockets.connect(f"{WS}/ws/events/{cid}", max_size=None)

    async def pump() -> None:
        async for raw in ev_ws:
            events.put_nowait(json.loads(raw))

    pump_task = asyncio.create_task(pump())
    broker = await websockets.connect(f"{WS}/ws/seat/{cid}/broker", max_size=None)
    audio_bytes = 0

    async def drain() -> None:
        nonlocal audio_bytes
        async for m in broker:
            if isinstance(m, (bytes, bytearray)):
                audio_bytes += len(m)

    drain_task = asyncio.create_task(drain())
    v = voice("broker")
    said, cues, prospect_turns = [], [], []
    last_cue = None
    outcome = None
    t_start = time.time()
    silence_task = None

    async def keep_silence() -> None:  # a real mic sends silence between sentences; the prospect's VAD needs it
        while True:
            await broker.send(bytes(FRAME * 2))
            await asyncio.sleep(0.02)

    silence_task = asyncio.create_task(keep_silence())
    try:
        while time.time() - t_start < 240:
            try:
                e = await asyncio.wait_for(events.get(), timeout=40)
            except asyncio.TimeoutError:
                print("  (no events for 40 s)")
                break
            t = e.get("type")
            if t == "transcript":
                who = e["speaker"]
                if who == "prospect":
                    prospect_turns.append(e["text"])
                    print(f"  [prospect] {e['text']}")
                else:
                    print(f"  [broker heard as] {e['text']}")
            elif t == "cue" and e.get("kind") != "info":
                last_cue = e
                cues.append(e["situation"])
                print(f"     coach -> ({e['situation']}) {e['text']}")
                line = dict(BROKER_LINES).get(e["situation"])
                if e["kind"] in ("say", "stop", "ask") and line:
                    if e["situation"] == "gk_transfer":
                        continue  # say nothing while being transferred
                    await asyncio.sleep(0.6)
                    silence_task.cancel()
                    pcm = _tts(line, v)
                    for i in range(0, len(pcm), FRAME * 2):
                        await broker.send(pcm[i:i + FRAME * 2])
                        await asyncio.sleep(0.02)
                    silence_task = asyncio.create_task(keep_silence())
                    said.append(line)
                    print(f"  [broker says] {line}")
            elif t == "event":
                print(f"  · {e.get('kind')}")
            elif t == "ended":
                outcome = e["debrief"]["outcome"]
                print(f"ended: {outcome} — {e['debrief'].get('headline')}")
                break
    finally:
        silence_task.cancel()
        if outcome is None:
            await broker.send(json.dumps({"type": "hangup"}))
            try:
                while True:
                    e = await asyncio.wait_for(events.get(), timeout=10)
                    if e.get("type") == "ended":
                        outcome = e["debrief"]["outcome"]
                        print(f"ended (hangup): {outcome}")
                        break
            except asyncio.TimeoutError:
                pass
        pump_task.cancel(); drain_task.cancel()
        await ev_ws.close(); await broker.close()
    print(f"prospect audio received: {audio_bytes/48000:.1f} s | prospect turns: {len(prospect_turns)} | cues: {cues}")
    transferred = "dm_identified" in cues
    print("gatekeeper transferred:", transferred, "| outcome:", outcome)
    return 0 if transferred and outcome in ("meeting_soft_yes", "meeting_booked", "callback_agreed") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
