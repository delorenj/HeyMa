"""Deterministic audio fixtures for `wax split` (HEYMA-18): speech-like bursts over a room-noise bed.

Not a test file (no `_test` suffix). numpy synthesizes the signal, ffmpeg encodes it the way Wax
captures (mono Opus in Ogg at 48 kHz, 32 kbps), so detection sees what real captures look like.
Everything is seeded: the same arguments always produce the same bytes of PCM.

A timeline is a list of (kind, seconds) steps:
  ("talk", s)   speech: utterances of 1.5-6 s separated by 0.3-1.2 s breaths, for s seconds
  ("pause", s)  a quiet gap inside a meeting (room noise only)
  ("quiet", s)  the overrun: room noise only
  ("click", s)  room noise with one short loud click in the middle (a door, a cough)
"""

import json
import subprocess
import wave
from pathlib import Path

import numpy as np

RATE = 16000


def _db(level_dbfs: float) -> float:
    return 10 ** (level_dbfs / 20.0)


def _room(seconds: float, rng, dbfs: float) -> np.ndarray:
    """Hum plus lowpassed noise at `dbfs` RMS: a quiet room or an HVAC bed."""
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    noise = rng.standard_normal(n).astype(np.float32)
    kernel = np.ones(24, dtype=np.float32) / 24          # crude lowpass: rumble, not hiss
    noise = np.convolve(noise, kernel, mode="same")
    bed = 0.6 * noise / (noise.std() + 1e-9) + 0.4 * np.sin(2 * np.pi * 60 * t).astype(np.float32)
    slow = 1.0 + 0.08 * np.sin(2 * np.pi * 0.05 * t).astype(np.float32)   # the bed breathes a little
    bed = bed * slow
    return (bed / (bed.std() + 1e-9) * _db(dbfs)).astype(np.float32)


def _utterance(seconds: float, rng, dbfs: float) -> np.ndarray:
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    f0 = rng.uniform(100, 190)
    voiced = sum(np.sin(2 * np.pi * f0 * h * t + rng.uniform(0, 6.28)) / h for h in range(1, 14))
    syllables = np.abs(np.sin(np.pi * rng.uniform(3.0, 5.0) * t)) ** 0.7
    fade = np.minimum(1.0, np.minimum(t, seconds - t) / 0.05)
    sig = (voiced * syllables * fade).astype(np.float32)
    return sig / (np.sqrt((sig ** 2).mean()) + 1e-9) * _db(dbfs)


def _talk(seconds: float, rng, dbfs: float) -> np.ndarray:
    out, filled = [], 0.0
    while filled < seconds:
        length = min(rng.uniform(1.5, 6.0), seconds - filled)
        out.append(_utterance(max(length, 0.2), rng, dbfs + rng.uniform(-3, 3)))
        filled += length
        gap = min(rng.uniform(0.3, 1.2), max(seconds - filled, 0.0))
        out.append(np.zeros(int(gap * RATE), dtype=np.float32))
        filled += gap
    sig = np.concatenate(out)
    return sig[: int(seconds * RATE)] if len(sig) >= int(seconds * RATE) else np.pad(sig, (0, int(seconds * RATE) - len(sig)))


def synth(timeline: list[tuple[str, float]], *, seed: int = 18, room_dbfs: float = -62.0,
          speech_dbfs: float = -24.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    parts = []
    for kind, seconds in timeline:
        bed = _room(seconds, rng, room_dbfs)
        if kind == "talk":
            bed = bed + _talk(seconds, rng, speech_dbfs)
        elif kind == "click":
            at = int(len(bed) / 2)
            bed[at:at + int(0.05 * RATE)] += 0.5 * rng.standard_normal(int(0.05 * RATE)).astype(np.float32)
        elif kind not in ("pause", "quiet"):
            raise ValueError(kind)
        parts.append(bed)
    return np.clip(np.concatenate(parts), -1.0, 1.0)


def write_ogg(pcm: np.ndarray, out: Path) -> Path:
    """float32 mono @16k -> Wax's capture format (Opus in Ogg, 48 kHz mono, 32 kbps)."""
    wav = out.with_suffix(".wav")
    with wave.open(str(wav), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(RATE)
        fh.writeframes((pcm * 32767).astype("<i2").tobytes())
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-ac", "1",
                    "-ar", "48000", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", str(out)], check=True)
    wav.unlink()
    return out


def timelines(scale: float = 1.0) -> dict[str, dict]:
    """The named fixtures. `scale` shrinks every duration (tests), 1.0 is real-meeting size.

    Each entry carries the room/speech levels it needs and the truth: where the overrun starts
    (`overrun_at_s`, None when the fixture has no overrun).
    """
    s = lambda x: x * scale   # noqa: E731
    meeting = [("talk", s(120)), ("pause", s(45)), ("talk", s(150)), ("pause", s(90)), ("talk", s(140)),
               ("pause", s(30)), ("talk", s(60))]                      # ~10 min of meeting with ordinary pauses
    meeting_s = sum(x for _, x in meeting)
    return {
        "meeting_overrun_quiet": {"timeline": meeting + [("quiet", s(1500))], "room_dbfs": -62.0,
                                  "overrun_at_s": meeting_s},
        "meeting_overrun_hvac": {"timeline": meeting + [("quiet", s(1500))], "room_dbfs": -46.0,
                                 "overrun_at_s": meeting_s},
        "meeting_overrun_with_click": {"timeline": meeting + [("click", s(700)), ("quiet", s(800))],
                                       "room_dbfs": -62.0, "overrun_at_s": meeting_s},
        "continuous_activity": {"timeline": [("talk", s(1800))], "room_dbfs": -62.0, "overrun_at_s": None},
        "all_quiet": {"timeline": [("quiet", s(1800))], "room_dbfs": -66.0, "overrun_at_s": None},
        "all_quiet_hvac": {"timeline": [("quiet", s(1800))], "room_dbfs": -46.0, "overrun_at_s": None},
        "meeting_with_pauses": {"timeline": meeting + [("quiet", s(40))], "room_dbfs": -62.0,
                                "overrun_at_s": None},
        "meeting_break_then_more": {"timeline": meeting + [("quiet", s(420)), ("talk", s(300)), ("pause", s(5))],
                                    "room_dbfs": -62.0, "overrun_at_s": None},
    }


def build(name: str, out_dir: Path, *, scale: float = 1.0) -> dict:
    """Render one named fixture to `out_dir/<name>.ogg`; returns its characteristics (also saved as JSON)."""
    spec = timelines(scale)[name]
    out_dir.mkdir(parents=True, exist_ok=True)
    pcm = synth(spec["timeline"], room_dbfs=spec["room_dbfs"])
    ogg = write_ogg(pcm, out_dir / f"{name}.ogg")
    info = {"name": name, "file": ogg.name, "scale": scale, "seconds": round(len(pcm) / RATE, 2),
            "timeline": spec["timeline"], "room_dbfs": spec["room_dbfs"], "speech_dbfs": -24.0,
            "overrun_at_s": spec["overrun_at_s"], "bytes": ogg.stat().st_size,
            "format": "ogg/opus mono 48000 Hz 32k (as captured by wax)", "seed": 18}
    (out_dir / f"{name}.fixture.json").write_text(json.dumps(info, indent=2))
    return info
