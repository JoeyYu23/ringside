"""Real-audio end-to-end: two browser-style seats fed with synthesized speech (macOS `say`) -> server VAD + whisper -> coach cues.

Runs against a live server (default http://127.0.0.1:8765). Prints STT and cue latencies; exits non-zero if the key
gatekeeper -> decision maker -> soft-yes cues do not fire.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

import websockets

from tests.audio_util import tts_pcm16k

BASE = os.environ.get("COACH_BASE", "http://127.0.0.1:8765")
WS = BASE.replace("http", "ws", 1)
FRAME = 640  # 20 ms of 16 kHz PCM16
SPEED = float(os.environ.get("E2E_SPEED", "3"))  # feed audio faster than real time (the VAD runs on the audio clock)
SCRIPT = [  # (seat, text, expected situation for prospect lines)
    ("prospect", "Brooklyn Auto Group, this is Maria.", "gk_greeting"),
    ("broker", "Hi Maria, Alex Chen with Harbor Insurance calling for Dan. Is he in?", None),
    ("prospect", "What is this regarding?", "gk_what_regarding"),
    ("broker", "The March renewal. Dan will know. Is he available?", None),
    ("prospect", "Can you just send an email to info at brooklyn auto dot com?", "gk_send_email"),
    ("broker", "Will do. What time is Dan usually free? I'll call back then instead.", None),
    ("prospect", "Hold on, let me see if he's free.", "gk_transfer"),
    ("prospect", "This is Dan.", "dm_identified"),
    ("broker", "Dan, Alex Chen with Harbor Insurance. I'll be brief, got twenty seconds?", None),
    ("prospect", "Sure, go ahead.", "dm_permission_granted"),
    ("broker", "Your policy renews in March. A second set of eyes before then, worth fifteen minutes?", None),
    ("prospect", "We're all set, we have a broker.", "obj_have_broker"),
    ("broker", "Keep them. A second look before March costs you nothing. Fifteen minutes?", None),
    ("prospect", "Alright, fine. When?", "soft_yes"),
]


def voice(seat: str) -> str:
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    if seat == "broker":
        for v in ("Daniel", "Fred", "Alex"):
            if v in out:
                return v
    return "Samantha"


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req).read())


async def drain(ws) -> None:
    try:
        async for _ in ws:
            pass
    except Exception:  # noqa: BLE001
        pass


async def main() -> int:
    c = post("/api/calls", {"account_id": "brooklyn-auto", "broker_id": "alex", "mode": "human"})
    cid = c["call_id"]
    print(f"call {cid} (human mode, both seats synthetic audio) | brief: {c['brief'][:70]}…")
    events: asyncio.Queue = asyncio.Queue()
    ev_ws = await websockets.connect(f"{WS}/ws/events/{cid}", max_size=None)

    async def pump_events() -> None:
        async for raw in ev_ws:
            events.put_nowait(json.loads(raw))

    ev_task = asyncio.create_task(pump_events())
    broker = await websockets.connect(f"{WS}/ws/seat/{cid}/broker", max_size=None)
    prospect = await websockets.connect(f"{WS}/ws/seat/{cid}/prospect", max_size=None)
    drains = [asyncio.create_task(drain(broker)), asyncio.create_task(drain(prospect))]
    seats = {"broker": broker, "prospect": prospect}
    # wait for STT to be ready
    t_end = time.time() + 60
    while time.time() < t_end:
        e = await asyncio.wait_for(events.get(), timeout=60)
        if e.get("type") == "event" and e.get("kind") == "stt_ready":
            break
    voices = {"prospect": voice("prospect"), "broker": voice("broker")}
    print("voices:", voices)
    ok = True
    stt_lat, cue_lat = [], []
    for seat, text, expected in SCRIPT:
        pcm = tts_pcm16k(text, "en") if seat == "prospect" else _tts(text, voices["broker"])
        ws = seats[seat]
        # stream 20 ms frames, then 900 ms of silence so the VAD closes the utterance
        for i in range(0, len(pcm), FRAME * 2):
            await ws.send(pcm[i:i + FRAME * 2])
            await asyncio.sleep(0.02 / SPEED)
        for _ in range(45):
            await ws.send(bytes(FRAME * 2))
            await asyncio.sleep(0.02 / SPEED)
        t_sent = time.time()
        got_tx, got_cue = None, None
        while time.time() - t_sent < 15:
            try:
                e = await asyncio.wait_for(events.get(), timeout=15)
            except asyncio.TimeoutError:
                break
            if e.get("type") == "transcript" and e.get("speaker") == seat and got_tx is None:
                got_tx = e
                stt_lat.append(time.time() - t_sent)
            elif e.get("type") == "cue" and got_tx is not None and e.get("kind") != "info":
                got_cue = e
                cue_lat.append(e.get("latency_ms", 0))
                break
            elif e.get("type") == "state" and got_tx is not None and seat == "broker":
                break
            elif e.get("type") == "state" and got_tx is not None and expected is None:
                break
        heard = got_tx["text"] if got_tx else "(no transcript)"
        sit = got_cue["situation"] if got_cue else "-"
        mark = "" if expected is None else ("OK " if sit == expected else "MISS")
        if expected and sit != expected:
            ok = False
        print(f"  {mark:<4} [{seat[:4]}] said: {text[:46]:<46} heard: {heard[:46]:<46} -> {sit:<22} {('| ' + got_cue['text'][:60]) if got_cue else ''}")
    await broker.send(json.dumps({"type": "hangup"}))
    debrief = None
    t0 = time.time()
    while time.time() - t0 < 20:
        e = await asyncio.wait_for(events.get(), timeout=20)
        if e.get("type") == "ended":
            debrief = e["debrief"]
            break
    print(f"ended: outcome={debrief and debrief.get('outcome')} | {debrief and debrief.get('headline')}")
    if stt_lat:
        print(f"STT latency after end of speech: median {sorted(stt_lat)[len(stt_lat)//2]*1000:.0f} ms, max {max(stt_lat)*1000:.0f} ms (audio fed at {SPEED}x)")
    if cue_lat:
        print(f"coach latency after transcript: max {max(cue_lat):.2f} ms")
    for t in drains + [ev_task]:
        t.cancel()
    await ev_ws.close(); await broker.close(); await prospect.close()
    return 0 if ok and debrief and debrief.get("outcome") in ("meeting_soft_yes", "meeting_booked") else 1


def _tts(text: str, v: str) -> bytes:
    import hashlib
    from pathlib import Path
    d = Path(__file__).resolve().parent / "audio"; d.mkdir(exist_ok=True)
    key = hashlib.md5(f"{v}:{text}".encode()).hexdigest()[:12]
    raw = d / f"{key}.raw"
    if not raw.exists():
        aiff = d / f"{key}.aiff"
        subprocess.run(["say", "-v", v, "-r", "170", "-o", str(aiff), text], check=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-ac", "1", "-ar", "16000", "-f", "s16le", "-acodec", "pcm_s16le", str(raw)], check=True)
        aiff.unlink()
    return raw.read_bytes()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
