"""Channel-separated transcription for dual-channel phone-call recordings.

A Telnyx dual-channel recording puts the first call leg on channel A and the
rest on channel B, so the carrier already knows who spoke. Transcribing each
channel on its own and interleaving by time gives speaker attribution that
cannot flip mid-sentence, which a Sortformer guess over a mono mixdown can.

Everything here is pure or shells out to ffmpeg/ffprobe only; there is no model,
GPU or network dependency, so scripts/transcribe.py (which also runs in the
diarization venv) and the tests can use it freely.

Detection rule (all must hold, otherwise the normal mono path runs):
  1. the file name starts with ``telnyx-`` and not ``telnyx-vm-`` (voicemail);
  2. the first audio stream has exactly two channels;
  3. both channels' RMS level is above ``SILENT_DB`` (-60 dB). Old one-sided
     recordings carry digital silence (~-90 dB) on channel 1.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

try:  # the labels come from another module that may not exist yet
    from wax.calls import channel_parties  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised through the tests via patching
    channel_parties = None

SILENT_DB = -60.0
DEFAULT_LABELS = ("Channel A", "Channel B")
_PREFIX = "telnyx-"
_VOICEMAIL_PREFIX = "telnyx-vm-"
# astats prints -inf for digital silence; clamp to something comparable.
_FLOOR_DB = -120.0


def enabled(env: Optional[dict] = None) -> bool:
    """``WAX_CHANNEL_SPLIT=0`` is the rollback switch."""
    value = (env if env is not None else os.environ).get("WAX_CHANNEL_SPLIT", "1")
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def is_call_name(name: str) -> bool:
    lowered = Path(name).name.lower()
    return lowered.startswith(_PREFIX) and not lowered.startswith(_VOICEMAIL_PREFIX)


def stream_channels(path: Path) -> Optional[int]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=channels", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        return int(r.stdout.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def parse_astats(text: str) -> list[float]:
    """Per-channel RMS levels (dB) from ffmpeg's astats log, overall block excluded."""
    levels: dict[int, float] = {}
    current: Optional[int] = None
    for line in text.splitlines():
        m = re.search(r"Channel:\s*(\d+)", line)
        if m:
            current = int(m.group(1))
            continue
        if "Overall" in line:
            current = None
            continue
        m = re.search(r"RMS level dB:\s*(-?inf|-?[\d.]+)", line)
        if m and current is not None:
            raw = m.group(1)
            levels[current] = _FLOOR_DB if raw.endswith("inf") else max(_FLOOR_DB, float(raw))
    return [levels[k] for k in sorted(levels)]


def channel_levels(path: Path) -> list[float]:
    """RMS level of each channel in dB. Empty when it cannot be measured."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
             "-af", "astats=metadata=0:reset=0", "-f", "null", "-"],
            capture_output=True, text=True, timeout=3600,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_astats(r.stderr or "")


def party_labels(audio_name: str) -> tuple[str, str]:
    """Names for channel 0 / channel 1; never raises, never returns blanks."""
    if channel_parties is not None:
        try:
            pair = channel_parties(audio_name)
        except Exception:  # noqa: BLE001 - labels are cosmetic; the split must not die for them
            pair = None
        if pair and len(pair) == 2:
            a, b = (str(pair[0] or "").strip(), str(pair[1] or "").strip())
            if a and b and a != b:
                return a, b
    return DEFAULT_LABELS


@dataclass
class Plan:
    use: bool
    reason: str
    levels: list[float] = field(default_factory=list)
    labels: tuple[str, str] = DEFAULT_LABELS


def plan(audio: Path | str,
         *,
         channels_fn: Callable[[Path], Optional[int]] = stream_channels,
         levels_fn: Callable[[Path], list[float]] = channel_levels) -> Plan:
    """Decide whether this file is transcribed per channel."""
    path = Path(audio)
    name = path.name.lower()
    if name.startswith(_VOICEMAIL_PREFIX):
        return Plan(False, "voicemail")
    if not name.startswith(_PREFIX):
        return Plan(False, "not_telnyx_call")
    if not enabled():
        return Plan(False, "disabled")
    count = channels_fn(path)
    if count != 2:
        return Plan(False, "probe_failed" if count is None else "not_stereo")
    levels = levels_fn(path)
    if len(levels) != 2:
        return Plan(False, "levels_unavailable", levels)
    if min(levels) <= SILENT_DB:
        return Plan(False, "channel_silent", levels)
    return Plan(True, "channel_separated", levels, party_labels(path.name))


def extract_channel(audio: Path | str, index: int, out: Path | str, sample_rate: int = 16000) -> Path:
    """Write one channel as a mono 16-bit wav (what Whisper resamples to anyway)."""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(audio), "-af", f"pan=mono|c0=c{int(index)}",
         "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(out)],
        capture_output=True, text=True, timeout=3600,
    )
    if r.returncode != 0 or not Path(out).is_file():
        raise RuntimeError(f"ffmpeg could not extract channel {index}: {(r.stderr or '')[-300:]}")
    return Path(out)


def merge_segments(per_channel: list[list[dict]], labels: tuple[str, str]) -> list[dict]:
    """Interleave per-channel segments by start time, stamping who spoke.

    Ties go to channel 0 (the first leg), keeping the order deterministic.
    """
    merged: list[dict] = []
    for index, segments in enumerate(per_channel):
        for seg in segments:
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            merged.append({**seg, "text": text, "speaker": index, "speaker_label": labels[index]})
    merged.sort(key=lambda s: (float(s["start"]), s["speaker"], float(s["end"])))
    return merged


def speaker_turns(segments: list[dict]) -> list[dict]:
    """Coalesce consecutive same-speaker segments into turns (start of the first)."""
    turns: list[dict] = []
    for seg in segments:
        if turns and turns[-1]["speaker"] == seg["speaker"]:
            turns[-1]["text"] += " " + seg["text"]
            turns[-1]["end"] = max(turns[-1]["end"], seg["end"])
        else:
            turns.append({"speaker": seg["speaker"], "speaker_label": seg["speaker_label"],
                          "start": seg["start"], "end": seg["end"], "text": seg["text"]})
    return turns
