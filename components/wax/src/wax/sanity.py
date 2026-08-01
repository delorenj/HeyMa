"""The gate that would have caught a 16-second transcript of a 16.5-hour file.

The old pipeline had no post-run check at all, so `transcription.completed` was
emitted for a 0.03%-complete transcript and nobody found out for days.

Gate on CONTAINER durations only: an independent ffprobe of the audio versus the
duration faster-whisper reports for what it actually decoded. Deliberately NOT
on last-segment-end or words-per-minute — transcribe.py runs with vad_filter=True,
so the final segment ends at the last *speech*. A three-hour recording that goes
quiet after 25 minutes is legitimate, and a coverage-ratio gate would reject it.
"""

import subprocess
from pathlib import Path
from typing import Any, Optional

MIN_RATIO = 0.95
ABS_TOLERANCE_S = 30.0
REL_TOLERANCE = 0.05


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
