"""FastAPI server: one CallSession per call — seats (browser mic / synthetic prospect / phone), the coach, the overlay
event stream, the post-call debrief and account memory."""
from __future__ import annotations

import asyncio
import audioop
import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from . import seed as seed_mod  # noqa: E402
from .coach import llm as llm_mod  # noqa: E402
from .coach.engine import CoachEngine, Turn  # noqa: E402
from .debrief import _fallback, debrief_call  # noqa: E402
from .memory import DB_PATH, Memory  # noqa: E402
from .stt import SeatSTT  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
SCRIPTS = Path(__file__).resolve().parent.parent / "data" / "scripts"
app = FastAPI(title="Sales Coach")
memory = Memory(os.environ.get("COACH_DB") or DB_PATH)
if not memory.accounts():
    seed_mod.seed(memory)
SESSIONS: dict[str, "CallSession"] = {}


@app.on_event("startup")
async def _warm_stt() -> None:
    """Load the whisper model once at boot so the first utterance of the first call is not the slow one."""
    if os.environ.get("COACH_WARM_STT", "1") != "0":
        from . import stt
        asyncio.get_event_loop().run_in_executor(None, stt.engine().warm)
PHONE_WAITING: list[str] = []
OUTCOME_LABEL = {"meeting_booked": "meeting booked", "meeting_soft_yes": "soft yes, time not locked", "callback_agreed": "callback agreed", "send_info": "asked for info",
                 "gatekeeper_block": "gatekeeper block", "objection_unresolved": "objection unresolved", "not_interested": "not interested", "do_not_call": "do not call", "no_outcome": "no outcome"}


def summarize(rule: dict, facts: dict) -> str:
    bits = []
    sits = [t["situation"] for t in rule.get("timeline", [])]
    gk = facts.get("gk_first")
    gk_moves = [s for s in sits if s.startswith("gk_") and s not in ("gk_greeting", "gk_transfer")]
    if gk_moves:
        bits.append(f"gatekeeper {gk or ''} → '{gk_moves[0].replace('gk_', '').replace('_', ' ')}'".replace("  ", " "))
    elif any(s.startswith("gk_") for s in sits):
        bits.append(f"gatekeeper {gk or ''}".strip())
    if any(s == "dm_identified" for s in sits):
        bits.append(f"reached {facts.get('dm_first') or 'the decision maker'}")
    objs = [s.replace("obj_", "").replace("_", " ") for s in sits if s.startswith("obj_")]
    if objs:
        bits.append("'" + "', '".join(dict.fromkeys(objs)) + "'")
    if facts.get("renewal_month"):
        bits.append(f"renewal {facts['renewal_month']}")
    bits.append(OUTCOME_LABEL.get(rule["outcome"], rule["outcome"]))
    return " → ".join(bits)


class CallSession:
    def __init__(self, call_id: str, account: dict, broker: dict, mode: str) -> None:
        self.id, self.account, self.broker, self.mode = call_id, account, broker, mode
        self.brief = memory.brief(account["id"])
        facts = {**self.brief["facts"], "company": account["company"], "broker_first": broker["first"], "agency": broker["agency"]}
        self.engine = CoachEngine(facts, avoid=set(self.brief["avoid"]), llm=llm_mod.classify_llm if llm_mod.available() else None,
                                  llm_timeout_s=float(os.environ.get("COACH_LLM_TIMEOUT_S", "2.0")))
        self.subs: set[asyncio.Queue] = set()
        self.seats: dict[str, WebSocket] = {}
        self.stt: dict[str, SeatSTT] = {}
        self.agent = None
        self.agent_task: asyncio.Task | None = None
        self.phone_send = None
        self.transcript: list[dict] = []
        self.events: list[dict] = []
        self.started = time.time()
        self.t0 = time.monotonic()
        self.running = self.ending = False
        self.ended: float | None = None
        self.debrief: dict | None = None
        self.out_rate = 24000 if mode == "synthetic" else 16000
        memory.start_call(account["id"], broker["id"], mode=mode, synthetic=(mode in ("synthetic", "scripted")), call_id=call_id)

    # ---- pub/sub ---------------------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)

    def publish(self, event: dict) -> None:
        event.setdefault("t", round(time.monotonic() - self.t0, 2))
        self.events.append(event)
        for q in list(self.subs):
            q.put_nowait(event)

    def state_event(self) -> dict:
        return {"type": "state", **self.engine.snapshot()}

    def hello(self, seat: str | None = None) -> dict:
        return {"type": "hello", "call_id": self.id, "seat": seat, "mode": self.mode, "out_rate": self.out_rate, "account": self.account, "broker": self.broker,
                "brief": {"text": self.brief["text"], "avoid": self.brief["avoid"], "n_calls": self.brief["n_calls"]}, "state": self.engine.snapshot(),
                "running": self.running, "ended": self.ended is not None, "debrief": self.debrief, "transcript": self.transcript[-12:],
                "cues": [c.as_dict() for c in self.engine.cues[-3:]], "do_not_call": "DO NOT CALL" in self.brief["text"]}

    # ---- lifecycle ---------------------------------------------------------------------------
    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        cue = self.engine.brief(self.brief["text"])
        if cue:
            memory.add_cue(self.id, cue.as_dict())
            self.publish({"type": "cue", **cue.as_dict()})
        self.publish(self.state_event())
        if self.mode == "synthetic":
            from .prospect import ProspectAgent
            a = self.account
            persona = {"company": a["company"], "industry": a.get("industry") or "small business", "gk_name": a.get("gk_first") or "Maria",
                       "dm_name": a.get("dm_name") or "Dan Russo", "renewal_month": a.get("renewal_month") or "March"}
            self.agent = ProspectAgent(persona, self._agent_audio, self._agent_turn, self._agent_broker_text, self._agent_event)
            self.agent_task = asyncio.create_task(self.agent.run())
        else:
            for seat in ("broker", "prospect"):
                speaker = seat
                self.stt[seat] = SeatSTT(speaker, self._make_final(speaker), self._make_partial(speaker) if seat == "broker" else None)
                self.stt[seat].start()
            self.publish({"type": "event", "kind": "stt_ready"})

    def _make_final(self, speaker: str):
        async def on_final(text: str, t0: float, t1: float) -> None:
            await self.on_turn(speaker, text, t0, t1, stt_ms=self.stt[speaker].last_latency_ms if speaker in self.stt else None)
        return on_final

    def _make_partial(self, speaker: str):
        async def on_partial(text: str, elapsed: float) -> None:
            self.publish({"type": "partial", "speaker": speaker, "text": text, "elapsed": round(elapsed, 1)})
            for c in self.engine.on_broker_partial(text, elapsed):
                memory.add_cue(self.id, c.as_dict())
                self.publish({"type": "cue", **c.as_dict()})
        return on_partial

    async def seat_audio(self, seat: str, pcm: bytes) -> None:
        if not self.running or self.ended:
            return
        if self.mode == "synthetic":
            if seat == "broker" and self.agent:
                self.agent.feed(pcm)
            return
        if seat in self.stt:
            await self.stt[seat].feed(pcm)
        other = "prospect" if seat == "broker" else "broker"
        ws = self.seats.get(other)
        if ws is not None:
            try:
                await ws.send_bytes(pcm)
            except Exception:  # noqa: BLE001
                pass
        if seat == "broker" and self.phone_send is not None:
            await self.phone_send(pcm)

    async def on_turn(self, speaker: str, text: str, t0: float | None = None, t1: float | None = None, stt_ms: float | None = None) -> list[dict]:
        now = time.monotonic() - self.t0
        turn = Turn(speaker=speaker, text=text, t0=t0 if t0 is not None else now, t1=t1 if t1 is not None else now)
        cues = await self.engine.on_turn(turn)
        rec = {"speaker": speaker, "text": text, "seq": turn.seq, "t0": round(turn.t0, 2), "t1": round(turn.t1, 2), "stt_ms": stt_ms}
        self.transcript.append(rec)
        memory.add_turn(self.id, turn.seq, speaker, text, turn.t0, turn.t1)
        self.publish({"type": "transcript", **rec})
        out = []
        for c in cues:
            d = c.as_dict()
            memory.add_cue(self.id, d)
            self.publish({"type": "cue", **d})
            out.append(d)
        self.publish(self.state_event())
        return out

    # ---- synthetic prospect callbacks -------------------------------------------------------
    async def _agent_audio(self, pcm24: bytes) -> None:
        ws = self.seats.get("broker")
        if ws is not None:
            try:
                await ws.send_bytes(pcm24)
            except Exception:  # noqa: BLE001
                pass

    async def _agent_turn(self, who: str, text: str) -> None:
        await self.on_turn("prospect", text)

    async def _agent_broker_text(self, text: str) -> None:
        await self.on_turn("broker", text)

    async def _agent_event(self, ev: dict) -> None:
        self.publish({"type": "event", "kind": ev.get("type"), **{k: v for k, v in ev.items() if k != "type"}})
        if ev.get("type") == "prospect_hangup":
            await self.end("prospect hung up")

    # ---- end ----------------------------------------------------------------------------------
    async def end(self, reason: str = "hangup") -> dict:
        if self.ending:
            return self.debrief or {}
        self.ending = True
        self.ended = time.time()
        if self.agent:
            self.agent.stop()
        for s in self.stt.values():
            await s.stop()
        rule = self.engine.rule_debrief()
        base = _fallback(rule, self.transcript, self.engine.facts)
        base["summary"] = summarize(rule, self.engine.facts)
        self.debrief = base
        memory.end_call(self.id, rule["outcome"], base["summary"], base, ended=self.ended)
        memory.update_account_facts(self.account["id"], self.engine.facts)
        self.publish({"type": "ended", "reason": reason, "debrief": base, "state": self.engine.snapshot()})
        asyncio.create_task(self._llm_debrief(rule))
        return base

    async def _llm_debrief(self, rule: dict) -> None:
        d = await debrief_call(self.transcript, rule, self.engine.facts, [c.as_dict() for c in self.engine.cues])
        if d.get("source") == "llm":
            d["summary"] = summarize(rule, self.engine.facts)   # the memory line stays short and structured
            self.debrief = d
            memory.end_call(self.id, d.get("outcome") or rule["outcome"], d["summary"], d, ended=self.ended)
            crm = d.get("crm") or {}
            memory.update_account_facts(self.account["id"], {"email": crm.get("email"), "renewal_month": crm.get("renewal_month")})
        self.publish({"type": "debrief", "debrief": d})
        for ws in list(self.seats.values()):
            try:
                await ws.send_text(json.dumps({"type": "debrief", "debrief": d}))
            except Exception:  # noqa: BLE001
                pass


def _lan_ip() -> str:
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:  # noqa: BLE001
        return "localhost"


def judge_urls(request: Request, call_id: str) -> dict[str, str]:
    """Where the judge opens the prospect seat: the tunnel (https, works on phones) or the LAN address."""
    pub = os.environ.get("PUBLIC_HOST")
    port = request.url.port or 8080
    out = {"lan": f"http://{_lan_ip()}:{port}/prospect/{call_id}"}
    if pub:
        out["public"] = f"https://{pub}/prospect/{call_id}"
    return out


def get_session(call_id: str) -> CallSession:
    s = SESSIONS.get(call_id)
    if s is None:
        raise HTTPException(404, "no such call")
    return s


# ---- pages -----------------------------------------------------------------------------------------
def page(name: str) -> HTMLResponse:
    return HTMLResponse((STATIC / name).read_text())


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return page("index.html")


@app.get("/call/{call_id}", response_class=HTMLResponse)
async def call_page(call_id: str) -> HTMLResponse:
    return page("call.html")


@app.get("/overlay/{call_id}", response_class=HTMLResponse)
async def overlay_page(call_id: str) -> HTMLResponse:
    return page("overlay.html")


@app.get("/prospect/{call_id}", response_class=HTMLResponse)
async def prospect_page(call_id: str) -> HTMLResponse:
    return page("prospect.html")


@app.get("/insights", response_class=HTMLResponse)
async def insights_page() -> HTMLResponse:
    return page("insights.html")


@app.get("/calls/{call_id}", response_class=HTMLResponse)
async def call_record_page(call_id: str) -> HTMLResponse:
    return page("record.html")


@app.get("/favicon.ico")
async def favicon() -> Response:
    svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='#0a0b0d'/><rect x='6' y='8' width='4' height='16' fill='#ffb020'/><rect x='13' y='8' width='13' height='3' fill='#f3efe4'/><rect x='13' y='15' width='9' height='3' fill='#f3efe4'/><rect x='13' y='22' width='11' height='3' fill='#f3efe4'/></svg>"
    return Response(svg, media_type="image/svg+xml")


@app.get("/static/{name}")
async def static_file(name: str) -> FileResponse:
    p = STATIC / name
    if not p.exists() or ".." in name:
        raise HTTPException(404)
    return FileResponse(p)


# ---- api ---------------------------------------------------------------------------------------------
@app.get("/api/accounts")
async def api_accounts() -> JSONResponse:
    out = []
    for a in memory.accounts():
        b = memory.brief(a["id"])
        out.append({**a, "brief": b["text"], "avoid": b["avoid"], "do_not_call": "DO NOT CALL" in b["text"]})
    return JSONResponse({"accounts": out, "brokers": memory.brokers(), "capabilities": {"synthetic": bool(os.environ.get("GEMINI_API_KEY")), "llm": llm_mod.available(), "phone": bool(os.environ.get("PUBLIC_HOST")),
                                                                                           "providers": {"claude": bool(os.environ.get("ANTHROPIC_API_KEY")), "groq": bool(os.environ.get("GROQ_API_KEY")), "gemini": bool(os.environ.get("GEMINI_API_KEY"))}}})


@app.post("/api/calls")
async def api_create_call(request: Request) -> JSONResponse:
    body = await request.json()
    account = memory.account(body.get("account_id") or "")
    broker = memory.broker(body.get("broker_id") or "")
    mode = body.get("mode") or "synthetic"
    if not account or not broker or mode not in ("synthetic", "human", "phone", "scripted"):
        raise HTTPException(400, "account_id, broker_id and a valid mode are required")
    if mode == "synthetic" and not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(400, "synthetic prospect needs GEMINI_API_KEY; use human, phone or scripted mode")
    call_id = "c" + uuid.uuid4().hex[:8]
    s = CallSession(call_id, account, broker, mode)
    SESSIONS[call_id] = s
    if mode == "phone":
        PHONE_WAITING.append(call_id)
    if mode == "scripted":
        await s.start()
    return JSONResponse({"call_id": call_id, "mode": mode, "brief": s.brief["text"], "avoid": s.brief["avoid"],
                         "urls": {"call": f"/call/{call_id}", "overlay": f"/overlay/{call_id}", "prospect": f"/prospect/{call_id}"}})


@app.get("/api/calls/{call_id}")
async def api_call(call_id: str, request: Request) -> JSONResponse:
    s = SESSIONS.get(call_id)
    if s:
        return JSONResponse({**s.hello(), "transcript": s.transcript, "cues": [c.as_dict() for c in s.engine.cues], "events": s.events[-50:], "judge_urls": judge_urls(request, call_id)})
    rec = memory.call(call_id)
    if not rec:
        raise HTTPException(404)
    return JSONResponse({"call_id": call_id, "record": rec, "account": memory.account(rec["account_id"]), "broker": memory.broker(rec["broker_id"])})


@app.get("/api/calls")
async def api_calls(account_id: str | None = None) -> JSONResponse:
    return JSONResponse({"calls": memory.calls(account_id, limit=100)})


@app.post("/api/calls/{call_id}/turn")
async def api_turn(call_id: str, request: Request) -> JSONResponse:
    s = get_session(call_id)
    if not s.running:
        await s.start()
    body = await request.json()
    speaker = body.get("speaker")
    if speaker not in ("prospect", "broker") or not str(body.get("text", "")).strip():
        raise HTTPException(400, "speaker and text required")
    t = time.perf_counter()
    cues = await s.on_turn(speaker, body["text"].strip(), body.get("t0"), body.get("t1"))
    return JSONResponse({"cues": cues, "state": s.engine.snapshot(), "server_ms": round((time.perf_counter() - t) * 1000, 2)})


@app.post("/api/calls/{call_id}/end")
async def api_end(call_id: str) -> JSONResponse:
    s = get_session(call_id)
    d = await s.end("api")
    return JSONResponse({"debrief": d, "state": s.engine.snapshot()})


@app.get("/api/scripts")
async def api_scripts() -> JSONResponse:
    out = []
    for p in sorted(SCRIPTS.glob("*.json")):
        d = json.loads(p.read_text())
        out.append({"name": p.stem, "title": d.get("title", p.stem), "turns": len(d.get("turns", []))})
    return JSONResponse({"scripts": out})


@app.post("/api/calls/{call_id}/script")
async def api_script(call_id: str, request: Request) -> JSONResponse:
    """Play a scripted call through the coach with human pacing (demo fallback when no microphone / no keys)."""
    s = get_session(call_id)
    body = await request.json()
    p = SCRIPTS / f"{body.get('name', 'brooklyn_auto')}.json"
    if not p.exists():
        raise HTTPException(404, "no such script")
    script = json.loads(p.read_text())
    speed = float(body.get("speed", 1.0))

    async def play() -> None:
        if not s.running:
            await s.start()
        for turn in script["turns"]:
            await asyncio.sleep(float(turn.get("delay", 1.6)) / speed)
            await s.on_turn(turn["speaker"], turn["text"])
        await asyncio.sleep(1.0 / speed)
        await s.end("script finished")

    asyncio.create_task(play())
    return JSONResponse({"ok": True, "turns": len(script["turns"])})


@app.get("/api/insights")
async def api_insights() -> JSONResponse:
    return JSONResponse(memory.insights())


@app.get("/api/playbook")
async def api_playbook() -> Response:
    return Response((Path(__file__).resolve().parent / "coach" / "playbook.yaml").read_text(), media_type="text/plain; charset=utf-8")


# ---- websockets ------------------------------------------------------------------------------------------
async def _relay(q: asyncio.Queue, ws: WebSocket) -> None:
    while True:
        item = await q.get()
        await ws.send_text(json.dumps(item, ensure_ascii=False, default=str))


@app.websocket("/ws/seat/{call_id}/{seat}")
async def ws_seat(ws: WebSocket, call_id: str, seat: str) -> None:
    await ws.accept()
    s = SESSIONS.get(call_id)
    if s is None or seat not in ("broker", "prospect"):
        await ws.send_text(json.dumps({"type": "error", "text": "no such call"}))
        await ws.close()
        return
    s.seats[seat] = ws
    q = s.subscribe()
    relay = asyncio.create_task(_relay(q, ws))
    try:
        await ws.send_text(json.dumps(s.hello(seat), ensure_ascii=False, default=str))
        if seat == "broker" and not s.running:
            await s.start()
        s.publish({"type": "event", "kind": "seat_joined", "seat": seat})
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes"):
                await s.seat_audio(seat, msg["bytes"])
            elif msg.get("text"):
                m = json.loads(msg["text"])
                if m.get("type") == "hangup":
                    await s.end("hangup")
                    break
                if m.get("type") == "turn" and m.get("speaker") in ("prospect", "broker") and m.get("text"):
                    await s.on_turn(m["speaker"], m["text"])
                if m.get("type") == "stats":
                    print(f"[{call_id} {seat} mic] device={m.get('device')!r} muted={m.get('muted')} track={m.get('state')} ctx={m.get('ctx')} rate={m.get('rate')} tx={m.get('tx')} frames={m.get('frames')} peak={m.get('peak')} ua={m.get('ua')!r}", flush=True)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        relay.cancel()
        s.unsubscribe(q)
        if s.seats.get(seat) is ws:
            s.seats.pop(seat, None)
        s.publish({"type": "event", "kind": "seat_left", "seat": seat})
        if seat == "broker" and s.running and not s.ended:
            await s.end("broker left")


@app.websocket("/ws/events/{call_id}")
async def ws_events(ws: WebSocket, call_id: str) -> None:
    await ws.accept()
    s = SESSIONS.get(call_id)
    if s is None:
        await ws.send_text(json.dumps({"type": "error", "text": "no such call"}))
        await ws.close()
        return
    q = s.subscribe()
    relay = asyncio.create_task(_relay(q, ws))
    try:
        await ws.send_text(json.dumps(s.hello(None), ensure_ascii=False, default=str))
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        relay.cancel()
        s.unsubscribe(q)


# ---- Twilio: the judge's phone is the prospect seat --------------------------------------------------------
def _public_host(request: Request) -> str:
    return os.environ.get("PUBLIC_HOST") or request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost:8080")


@app.api_route("/twilio/voice", methods=["GET", "POST"])
async def twilio_voice(request: Request) -> Response:
    form = await request.form() if request.method == "POST" else request.query_params
    caller = form.get("From", "")
    host = _public_host(request)
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/ws/twilio">
      <Parameter name="from" value="{caller}"/>
    </Stream>
  </Connect>
  <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws/twilio")
async def ws_twilio(ws: WebSocket) -> None:
    """Twilio Media Streams (mulaw 8 kHz both ways) attached to the oldest call waiting for a phone prospect."""
    await ws.accept()
    stream_sid: str | None = None
    s: CallSession | None = None
    up_state = down_state = None

    async def send_to_phone(pcm16: bytes) -> None:
        nonlocal down_state
        pcm8, down_state = audioop.ratecv(pcm16, 2, 1, 16000, 8000, down_state)
        payload = base64.b64encode(audioop.lin2ulaw(pcm8, 2)).decode()
        await ws.send_text(json.dumps({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}}))

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            ev = msg.get("event")
            if ev == "start":
                stream_sid = msg["start"]["streamSid"]
                call_id = PHONE_WAITING.pop(0) if PHONE_WAITING else None
                s = SESSIONS.get(call_id) if call_id else None
                if s is None:
                    await ws.close()
                    return
                s.phone_send = send_to_phone
                if not s.running:
                    await s.start()
                s.publish({"type": "event", "kind": "phone_connected", "from": (msg["start"].get("customParameters") or {}).get("from")})
            elif ev == "media" and s:
                pcm8 = audioop.ulaw2lin(base64.b64decode(msg["media"]["payload"]), 2)
                pcm16, up_state = audioop.ratecv(pcm8, 2, 1, 8000, 16000, up_state)
                await s.seat_audio("prospect", pcm16)
            elif ev == "stop":
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if s:
            s.phone_send = None
            s.publish({"type": "event", "kind": "phone_disconnected"})
            if s.running and not s.ended:
                await s.end("phone hung up")
