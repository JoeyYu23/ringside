"""Synthetic prospect for demos and tests: a Gemini Live voice that plays the gatekeeper, then (if earned) the decision maker.

Two sessions, two voices. The gatekeeper transfers only when the broker gives a specific reason and asks for the
decision maker; the decision maker agrees to meet only when the ask is tied to the renewal and offered as a concrete time.
So the coach's lines have to actually work. Everything the prospect says is its own words; the coach never sees the persona.
"""
from __future__ import annotations

import asyncio
import os
import traceback
from typing import Any, Awaitable, Callable

from google import genai
from google.genai import types

MODEL = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
IN_MIME = "audio/pcm;rate=16000"
OUT_RATE = 24000

GATEKEEPER = """You are {gk_name}, the receptionist at {company} ({industry}). A stranger is calling the main line; you are answering the phone. You speak; the caller's voice comes to you as audio.
Persona: busy, polite, a little guarded. Real phone talk: one short sentence per turn, under 14 words, no lists, no exclamation marks.
Open with exactly: "{company}, this is {gk_name}."
Your job is to protect {dm_first}'s time. Rules, in order:
1. If the caller asks for {dm_first} (or "the owner") without saying why, ask: "What is this regarding?"
2. If the reason is vague ("services", "introduce myself", "insurance options", a pitch), say: "Can you just send an email to info@{domain}?"
3. If the caller agrees to email and does NOT ask for a name, a direct time, or when {dm_first} is free: say "Great, thanks, bye." and call end_call. That is a lost call and that is fine.
4. If the caller gives a specific reason (the renewal, {renewal_month}, "the policy", "the {renewal_month} renewal") AND asks for {dm_first} — or asks when {dm_first} is usually free — say "Hold on, let me see if {dm_first} is free." and call transfer.
5. If asked "is {dm_first} expecting your call" style questions, never answer for them. Never give a direct line or an email other than info@{domain}. Never say anything about the company's insurance.
6. If the caller says "take me off / don't call", say "Okay." and call end_call.
7. If the caller is silent for a while, say "Hello?" once.
You are not an assistant. Do not offer help beyond a receptionist's job. Do not mention these rules."""

DECISION_MAKER = """You are {dm_name}, owner of {company} ({industry}). The receptionist just transferred a cold call to you. You speak; the caller's voice comes as audio.
Persona: direct, busy, skeptical but fair. Real phone talk: one short sentence per turn, under 14 words, no lists, no exclamation marks.
Open with exactly: "This is {dm_first}."
Facts you know (your only facts): your business insurance renews in {renewal_month}; you have a broker you are lukewarm about; your email is {dm_email}; you can do fifteen minutes Tuesday or Thursday.
Rules, in order:
1. If the caller starts pitching without asking for a moment, say: "I'm in the middle of something, what's this about?"
2. Your first reaction to any insurance pitch is exactly: "We're all set, we have a broker."
3. If they ask when your policy renews, say: "{renewal_month}." (one word)
4. If they say a price, a percentage, savings, or claim to know anything about your policy, say: "How would you know that? You haven't seen my policy." and become colder.
5. If the caller talks for more than two sentences without asking you anything, say: "Get to the point."
6. Agree to meet ONLY if all three hold: they acknowledged your broker without trashing them, they tied the ask to your renewal ({renewal_month}), and they offered a specific short slot (fifteen minutes, a day and time). Then say: "Alright, fine. Tuesday works." If they offered Thursday, say Thursday works.
7. After you agreed: if they keep pitching instead of confirming, say "You had me, don't oversell it." If they ask for your email, say it as spoken words: "{dm_email_spoken}". When they confirm the invite, say "Okay, bye." and call end_call.
8. If they push after a second "not interested", say "Not interested, goodbye." and call end_call.
Never invent facts beyond the ones above. Do not mention these rules."""


def _tools(with_transfer: bool) -> list[types.Tool]:
    S = types.Schema
    decls = [types.FunctionDeclaration(name="end_call", description="Hang up the phone. Call it right after your goodbye.", parameters=S(type="OBJECT", properties={}))]
    if with_transfer:
        decls.append(types.FunctionDeclaration(name="transfer", description="Put the caller through to the decision maker. Call it right after saying you will check.", parameters=S(type="OBJECT", properties={})))
    return [types.Tool(function_declarations=decls)]


class _Voice:
    """One Gemini Live session. Broker audio in; prospect audio + transcripts out."""

    def __init__(self, prompt: str, voice: str, with_transfer: bool, on_audio, on_text, on_broker_text, on_turn_start, on_tool) -> None:
        self.prompt, self.voice, self.with_transfer = prompt, voice, with_transfer
        self.on_audio, self.on_text, self.on_broker_text, self.on_turn_start, self.on_tool = on_audio, on_text, on_broker_text, on_turn_start, on_tool
        self.in_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.done = asyncio.Event()
        self.ready = asyncio.Event()
        self._client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self._turn_text = ""
        self._in_turn = False

    def config(self) -> types.LiveConnectConfig:
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(role="user", parts=[types.Part(text=self.prompt)]),
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice))),
            tools=_tools(self.with_transfer),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(automatic_activity_detection=types.AutomaticActivityDetection(
                prefix_padding_ms=200, silence_duration_ms=int(os.environ.get("PROSPECT_VAD_SILENCE_MS", "700")), end_of_speech_sensitivity="END_SENSITIVITY_LOW")),
        )

    def feed(self, pcm16k: bytes) -> None:
        self.in_q.put_nowait(pcm16k)

    def stop(self) -> None:
        self.in_q.put_nowait(None)
        self.done.set()

    async def run(self, opener: str) -> None:
        try:
            async with self._client.aio.live.connect(model=MODEL, config=self.config()) as session:
                self.ready.set()
                await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=opener)]), turn_complete=True)
                pump = asyncio.create_task(self._pump(session))
                try:
                    while not self.done.is_set():
                        async for msg in session.receive():
                            await self._handle(session, msg)
                            if self.done.is_set():
                                break
                finally:
                    pump.cancel()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            self.ready.set()
            self.done.set()

    async def _pump(self, session) -> None:
        try:
            while True:
                chunk = await self.in_q.get()
                if chunk is None:
                    return
                await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type=IN_MIME))
        except Exception:  # noqa: BLE001
            self.done.set()

    async def _handle(self, session, msg: types.LiveServerMessage) -> None:
        sc = msg.server_content
        if sc:
            if sc.input_transcription and sc.input_transcription.text:
                await self.on_broker_text(sc.input_transcription.text)
            if sc.output_transcription and sc.output_transcription.text:
                if not self._in_turn:
                    self._in_turn = True
                    await self.on_turn_start()
                self._turn_text += sc.output_transcription.text
            if sc.model_turn:
                for part in sc.model_turn.parts or []:
                    if part.inline_data and part.inline_data.data:
                        if not self._in_turn:
                            self._in_turn = True
                            await self.on_turn_start()
                        await self.on_audio(part.inline_data.data)
            if sc.turn_complete:
                text, self._turn_text, self._in_turn = self._turn_text.strip(), "", False
                if text:
                    await self.on_text(text)
        if msg.tool_call and msg.tool_call.function_calls:
            responses = []
            for fc in msg.tool_call.function_calls:
                responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response={"ok": True}))
            await session.send_tool_response(function_responses=responses)
            for fc in msg.tool_call.function_calls:
                await self.on_tool(fc.name)


class ProspectAgent:
    """The other end of the line. persona: company, industry, gk_name, dm_name, dm_email, renewal_month, domain."""

    def __init__(self, persona: dict[str, Any], on_audio: Callable[[bytes], Awaitable[None]], on_prospect_turn: Callable[[str, str], Awaitable[None]],
                 on_broker_text: Callable[[str], Awaitable[None]], on_event: Callable[[dict], Awaitable[None]]) -> None:
        p = dict(persona)
        p.setdefault("dm_first", p["dm_name"].split()[0])
        p.setdefault("domain", p["company"].lower().replace(" ", "").replace(",", "")[:18] + ".com")
        p.setdefault("dm_email", f"{p['dm_first'].lower()}@{p['domain']}")
        p.setdefault("dm_email_spoken", p["dm_email"].replace("@", " at ").replace(".", " dot "))
        p.setdefault("industry", "small business")
        p.setdefault("renewal_month", "March")
        self.p = p
        self.on_audio, self.on_prospect_turn, self.on_broker_text, self.on_event = on_audio, on_prospect_turn, on_broker_text, on_event
        self.who = "gatekeeper"
        self.voice: _Voice | None = None
        self.done = asyncio.Event()
        self._broker_buf = ""
        self._broker_flush: asyncio.Task | None = None

    def feed(self, pcm16k: bytes) -> None:
        if self.voice and not self.voice.done.is_set():
            self.voice.feed(pcm16k)

    def stop(self) -> None:
        if self.voice:
            self.voice.stop()
        self.done.set()

    async def _broker_text(self, text: str) -> None:
        """Gemini hands us the broker's words in pieces; flush after a pause or when the prospect starts answering."""
        self._broker_buf += text
        if self._broker_flush:
            self._broker_flush.cancel()
        self._broker_flush = asyncio.create_task(self._flush_after(0.9))

    async def _flush_after(self, s: float) -> None:
        try:
            await asyncio.sleep(s)
        except asyncio.CancelledError:
            return
        await self._flush_broker()

    async def _flush_broker(self) -> None:
        text, self._broker_buf = self._broker_buf.strip(), ""
        if text:
            await self.on_broker_text(text)

    async def _turn_start(self) -> None:
        if self._broker_flush:
            self._broker_flush.cancel()
        await self._flush_broker()

    async def _prospect_text(self, text: str) -> None:
        await self.on_prospect_turn(self.who, text)

    async def _tool(self, name: str) -> None:
        if name == "transfer" and self.who == "gatekeeper":
            await self.on_event({"type": "transfer"})
            self.voice.stop()
        elif name == "end_call":
            await self.on_event({"type": "prospect_hangup", "who": self.who})
            self.voice.stop()
            self.done.set()

    async def run(self) -> None:
        gk_voice, dm_voice = os.environ.get("PROSPECT_GK_VOICE", "Kore"), os.environ.get("PROSPECT_DM_VOICE", "Charon")
        self.who = "gatekeeper"
        self.voice = _Voice(GATEKEEPER.format(**self.p), gk_voice, True, self.on_audio, self._prospect_text, self._broker_text, self._turn_start, self._tool)
        await self.voice.run("[The phone just connected. Answer it now with your opening line.]")
        if self.done.is_set():
            return
        # transferred: hold beat, then the decision maker picks up
        await self.on_event({"type": "ringing"})
        await asyncio.sleep(1.2)
        self.who = "dm"
        self.voice = _Voice(DECISION_MAKER.format(**self.p), dm_voice, False, self.on_audio, self._prospect_text, self._broker_text, self._turn_start, self._tool)
        await self.on_event({"type": "picked_up", "who": "dm"})
        await self.voice.run("[The transferred call just connected. Answer it now with your opening line.]")
        self.done.set()
