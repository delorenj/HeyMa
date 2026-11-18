"""The gate that would have caught a 16-second transcript of a 16.5-hour file.

The old pipeline had no post-run check at all, so `transcription.completed` was
emitted for a 0.03%-complete transcript and nobody found out for days.

Gate on CONTAINER durations only: an independent ffprobe of the audio versus the
duration faster-whisper reports for what it actually decoded. Deliberately NOT
on last-segment-end or words-per-minute — transcribe.py runs with vad_filter=True,
so the final segment ends at the last *speech*. A three-hour recording that goes
quiet after 25 minutes is legitimate, and a coverage-ratio gate would reject it.
"""

import re
import subprocess
from pathlib import Path
from typing import Any, Optional

MIN_RATIO = 0.95
ABS_TOLERANCE_S = 30.0
REL_TOLERANCE = 0.05

# Silent audio is measured, not inferred from an empty transcript. ONE definition for every
# source (mic, dropoff, Voice Memos, remote-audio, Telnyx intake). Calibrated on the
# 2026-11-18 incident: broken input measured mean -77..-87 dB, max -40..-53 dB; healthy
# speech is mean -23..-35 dB, max >= -11 dB. Both bounds must hold, so a long recording
# that is quiet but has a real peak (someone spoke once) is never "silent".
# channels.SILENT_DB (a Telnyx channel is dead below it) is this same mean bound.
SILENT_MEAN_DB = -60.0
SILENT_MAX_DB = -35.0
# volumedetect prints -inf for digital silence; clamp to something comparable.
_FLOOR_DB = -120.0
_VOLUME_RE = re.compile(r"(mean|max)_volume:\s*(-?inf|-?[0-9.]+)\s*dB", re.IGNORECASE)


def probe_duration(path: Path) -> Optional[float]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=600,
        )
        return float(r.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def audio_levels(path: Path, *, timeout: float = 600.0) -> Optional[dict[str, Any]]:
    """Measure the whole file's level: {mean_db, max_db, silent}, or None when it cannot be measured.

    ffmpeg ``volumedetect`` over every sample of every channel (a stereo Telnyx call is judged
    as one mix: one dead channel is normal and barely moves the mean). About 2 s for a
    79-minute recording. FAILS OPEN: a missing ffmpeg, a timeout, an unreadable file or
    output we cannot parse all return None, and None never parks or fails anything.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "volumedetect",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    found: dict[str, float] = {}
    for kind, raw in _VOLUME_RE.findall(r.stderr or ""):
        found[kind.lower()] = _FLOOR_DB if "inf" in raw.lower() else max(float(raw), _FLOOR_DB)
    if "mean" not in found or "max" not in found:
        return None
    mean_db, max_db = found["mean"], found["max"]
    return {"mean_db": round(mean_db, 1), "max_db": round(max_db, 1),
            "silent": mean_db <= SILENT_MEAN_DB and max_db <= SILENT_MAX_DB}


def format_duration(seconds: Optional[float]) -> str:
    """79m 8s, 42s, 1h 02m: the way a person says how long a recording is."""
    if seconds is None:
        return "unknown length"
    total = int(round(float(seconds)))
    if total >= 3600 * 3:
        return f"{total // 3600}h {total % 3600 // 60:02d}m"
    return f"{total // 60}m {total % 60}s" if total >= 60 else f"{total}s"


def check(audio: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Decide whether a transcript may be published.

    Returns {ok, reason_code, audio_duration_s, asr_duration_s, duration_ratio}.
    Unknowns fail closed: if we cannot prove the transcript covers the audio, we
    do not publish it.
    """
    audio_dur = probe_duration(audio)
    asr_dur = meta.get("duration_seconds")

    if audio_dur is None:
        return {"ok": False, "reason_code": "audio_unprobeable",
                "audio_duration_s": None, "asr_duration_s": asr_dur, "duration_ratio": None}
    if asr_dur is None:
        return {"ok": False, "reason_code": "asr_duration_missing",
                "audio_duration_s": audio_dur, "asr_duration_s": None, "duration_ratio": None}

    try:
        asr_dur = float(asr_dur)
    except (TypeError, ValueError):
        return {"ok": False, "reason_code": "asr_duration_unparseable",
                "audio_duration_s": audio_dur, "asr_duration_s": asr_dur, "duration_ratio": None}

    ratio = (asr_dur / audio_dur) if audio_dur > 0 else 0.0
    delta = abs(audio_dur - asr_dur)
    tolerance = max(ABS_TOLERANCE_S, audio_dur * REL_TOLERANCE)

    ok = ratio >= MIN_RATIO or delta <= tolerance
    out = {
        "ok": ok,
        "reason_code": None if ok else "duration_mismatch",
        "audio_duration_s": round(audio_dur, 3),
        "asr_duration_s": round(asr_dur, 3),
        "duration_ratio": round(ratio, 5),
        "delta_s": round(delta, 3),
        "tolerance_s": round(tolerance, 3),
    }
    if not ok and meta.get("word_count") == 0:
        out["reason_code"] = "empty_transcript"
    return out


def source_unchanged(audio: Path, before: tuple[int, int]) -> bool:
    """Re-stat the source after transcription.

    If the file grew while we were decoding it, the transcript describes a
    prefix of something that no longer exists — exactly the record_0016 case.
    """
    try:
        st = audio.stat()
    except OSError:
        return False
    return (st.st_size, st.st_mtime_ns) == before
