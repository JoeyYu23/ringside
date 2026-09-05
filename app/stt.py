"""Local speech-to-text per seat: energy VAD chops utterances, whisper (MLX) transcribes them. Offline, ~0.3 s per utterance."""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import numpy as np

RATE = 16000
FRAME_MS = 20
FRAME = RATE * FRAME_MS // 1000
MODEL = os.environ.get("COACH_STT_MODEL", "mlx-community/whisper-small.en-mlx")
HALLUCINATIONS = {"thank you.", "thanks for watching.", "you", "you.", "thank you", "bye.", ".", "", "uh", "um"}


@dataclass
class Utterance:
    pcm: bytes
    t0: float
    t1: float


class Segmenter:
    """Energy VAD with pre-roll and hangover. feed() returns finished utterances; `speaking_since` supports ramble detection."""

    def __init__(self, start_frames: int = 3, end_silence_ms: int = 550, pre_roll_ms: int = 240, max_s: float = 14.0, floor: float = 350.0) -> None:
        self.start_frames, self.end_frames = start_frames, end_silence_ms // FRAME_MS
        self.pre = pre_roll_ms // FRAME_MS
        self.max_frames = int(max_s * 1000 / FRAME_MS)
        self.floor = floor
        self.noise = floor
        self.buf = b""
        self.ring: list[bytes] = []
        self.active: list[bytes] = []
        self.voiced = self.silent = 0
        self.speaking_since: float | None = None
        self.t = 0.0

    def feed(self, pcm: bytes, now: float | None = None) -> list[Utterance]:
        out = []
        self.buf += pcm
        while len(self.buf) >= FRAME * 2:
            frame, self.buf = self.buf[:FRAME * 2], self.buf[FRAME * 2:]
            self.t += FRAME_MS / 1000
            x = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
            if self.speaking_since is None:
                self.noise = 0.97 * self.noise + 0.03 * rms
            thr = max(self.floor, self.noise * 2.5)
            voiced = rms > thr
            if self.speaking_since is None:
                self.ring.append(frame)
                self.ring = self.ring[-self.pre:]
                self.voiced = self.voiced + 1 if voiced else 0
                if self.voiced >= self.start_frames:
                    self.speaking_since = self.t - len(self.ring) * FRAME_MS / 1000
                    self.active = list(self.ring)
                    self.silent = 0
            else:
                self.active.append(frame)
                self.silent = 0 if voiced else self.silent + 1
                if self.silent >= self.end_frames or len(self.active) >= self.max_frames:
                    keep = len(self.active) - max(0, self.silent - 5)
                    pcm_out = b"".join(self.active[:keep])
                    t1 = self.t - self.silent * FRAME_MS / 1000
                    if len(pcm_out) >= RATE * 2 * 0.35:
                        out.append(Utterance(pcm_out, self.speaking_since, t1))
                    self.speaking_since, self.active, self.voiced, self.ring = None, [], 0, []
        return out

    def partial(self) -> bytes:
        return b"".join(self.active) if self.speaking_since is not None else b""


class LocalWhisper:
    def __init__(self, model: str = MODEL) -> None:
        self.model = model
        self._loaded = False

    def warm(self) -> None:
        import mlx_whisper  # lazy: heavy import
        mlx_whisper.transcribe(np.zeros(RATE, dtype=np.float32), path_or_hf_repo=self.model, language="en", fp16=True, condition_on_previous_text=False)
        self._loaded = True

    def transcribe(self, pcm: bytes) -> str:
        import mlx_whisper
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        r = mlx_whisper.transcribe(audio, path_or_hf_repo=self.model, language="en", fp16=True, condition_on_previous_text=False,
                                   no_speech_threshold=0.6, logprob_threshold=-1.0, compression_ratio_threshold=2.4)
        segs = [s for s in r.get("segments", []) if s.get("no_speech_prob", 0) < 0.8]
        text = " ".join(s["text"].strip() for s in segs).strip() or r.get("text", "").strip()
        if text.lower() in HALLUCINATIONS:
            return ""
        return text


_engine: LocalWhisper | None = None
_lock = asyncio.Lock()


def engine() -> LocalWhisper:
    global _engine
    if _engine is None:
        _engine = LocalWhisper()
    return _engine


class SeatSTT:
    """One seat's audio -> final turns (and partials while the speaker is still talking)."""

    def __init__(self, speaker: str, on_final: Callable[[str, float, float], Awaitable[None]],
                 on_partial: Callable[[str, float], Awaitable[None]] | None = None, partial_every_s: float = 2.5) -> None:
        self.speaker, self.on_final, self.on_partial = speaker, on_final, on_partial
        self.seg = Segmenter()
        self.partial_every_s = partial_every_s
        self._last_partial = 0.0
        self._q: asyncio.Queue[Utterance | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self.last_latency_ms = 0.0

    def start(self) -> None:
        self._task = asyncio.create_task(self._worker())

    async def _worker(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            u = await self._q.get()
            if u is None:
                return
            t = time.perf_counter()
            async with _lock:  # one whisper context
                text = await loop.run_in_executor(None, engine().transcribe, u.pcm)
            self.last_latency_ms = round((time.perf_counter() - t) * 1000, 1)
            if text:
                await self.on_final(text, u.t0, u.t1)

    async def feed(self, pcm: bytes) -> None:
        for u in self.seg.feed(pcm):
            self._q.put_nowait(u)
        if self.on_partial and self.seg.speaking_since is not None:
            elapsed = self.seg.t - self.seg.speaking_since
            if elapsed - self._last_partial >= self.partial_every_s and elapsed >= self.partial_every_s:
                self._last_partial = elapsed
                pcm_part = self.seg.partial()
                loop = asyncio.get_event_loop()
                async with _lock:
                    text = await loop.run_in_executor(None, engine().transcribe, pcm_part[-RATE * 2 * 12:])
                if text:
                    await self.on_partial(text, elapsed)
        elif self.seg.speaking_since is None:
            self._last_partial = 0.0

    def speaking_for(self) -> float:
        return 0.0 if self.seg.speaking_since is None else self.seg.t - self.seg.speaking_since

    async def stop(self) -> None:
        self._q.put_nowait(None)
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except Exception:  # noqa: BLE001
                pass
