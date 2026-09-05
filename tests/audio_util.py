"""Synthetic caller speech via macOS `say` -> 16 kHz PCM16 mono raw (cached under tests/audio)."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

AUDIO_DIR = Path(__file__).resolve().parent / "audio"
AUDIO_DIR.mkdir(exist_ok=True)
VOICES = {"zh": "Tingting", "en": "Samantha"}


def _voice(lang: str) -> str:
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    want = VOICES[lang]
    if want in out:
        return want
    tag = "zh_CN" if lang == "zh" else "en_US"
    for line in out.splitlines():
        if tag in line:
            return line.split(tag)[0].strip()
    raise RuntimeError(f"no {lang} voice installed")


def tts_pcm16k(text: str, lang: str) -> bytes:
    key = hashlib.md5(f"{lang}:{text}".encode()).hexdigest()[:12]
    raw = AUDIO_DIR / f"{key}.raw"
    if raw.exists():
        return raw.read_bytes()
    aiff = AUDIO_DIR / f"{key}.aiff"
    subprocess.run(["say", "-v", _voice(lang), "-r", "165", "-o", str(aiff), text], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-ac", "1", "-ar", "16000",
                    "-f", "s16le", "-acodec", "pcm_s16le", str(raw)], check=True)
    aiff.unlink()
    return raw.read_bytes()


def silence16k(seconds: float) -> bytes:
    return b"\x00\x00" * int(16000 * seconds)
