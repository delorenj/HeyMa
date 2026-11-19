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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

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


# ------------------------------------------------------------ quiet speech ----
#
# "Silent" by whole-file level is not always silence. On 2026-11-18 ten Mac Voice
# Memos measured mean -70..-82 dB (all parked as junk_silent), yet a +40 dB copy
# of one transcribed to 504 coherent words: the input level was ~35 dB too low,
# and faster-whisper's VAD threw the speech away. Speech MOVES: half-second
# windows of it sit well above the room's noise floor, while a muted or dead input
# is flat. So a level-silent recording is profiled, and if enough windows rise
# QUIET_SPEECH_RANGE_DB above its own floor it is quiet speech: gain is applied to
# a private copy and THAT is transcribed. True silence (flat) is still parked.
PROFILE_WINDOW_S = 0.5
QUIET_SPEECH_RANGE_DB = 10.0       # p99 window must clear the p10 floor by this much
QUIET_SPEECH_ACTIVE_ABOVE_DB = 8.0  # a window this far over the floor is "active"
QUIET_SPEECH_MIN_ACTIVE_S = 3.0    # ...and at least this much audio must be active
QUIET_SPEECH_MIN_ACTIVE_FRAC = 0.02  # ...and at least this share of it (a few clicks are not speech)
QUIET_SPEECH_ABS_MIN_DB = -95.0    # loud windows below this are digital silence, never speech
GAIN_TARGET_DB = -24.0             # where the p99 window lands (speech is about -25 dB)
GAIN_MAX_DB = 50.0
# Calibration (2026-11-19). Positives: all ten parked memos, each confirmed by transcribing a
# +40 dB copy (72..4830 words, coherent): range 11.6..19 dB, active >= 17%. Negatives: digital
# silence, 60 Hz hum, flat white noise (0 dB range), the live Yeti's silent segments (~1% active).
# A modulated pink noise passes the profile ON PURPOSE: the profile only nominates, and the ASR
# (vad_filter) decides. Gained noise transcribes to 0 words, and an empty transcript of
# level-silent audio is parked as junk_silent exactly as before, so a false nomination costs one
# GPU run, never a note.


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def speech_profile(path: Path, *, timeout: float = 600.0) -> Optional[dict[str, Any]]:
    """Half-second RMS windows of a (mono, 16 kHz) mix: floor, loud level and active seconds.

    Returns {floor_db, loud_db, active_s, windows, quiet_speech, gain_db}, or None when ffmpeg
    cannot say (FAILS OPEN: None keeps today's verdict). ``loud_db`` is the p99 window;
    ``gain_db`` lifts it to GAIN_TARGET_DB, capped at GAIN_MAX_DB.
    """
    samples = int(16000 * PROFILE_WINDOW_S)
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af",
             f"aresample=16000,aformat=channel_layouts=mono,asetnsamples=n={samples}:p=0,"
             "astats=metadata=1:reset=1,"
             "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    levels: list[float] = []
    for line in (r.stdout or "").splitlines():
        if "RMS_level=" not in line:
            continue
        raw = line.split("=", 1)[1].strip().lower()
        try:
            value = _FLOOR_DB if "inf" in raw else max(float(raw), _FLOOR_DB)
        except ValueError:
            continue
        levels.append(value)
    if r.returncode != 0 or len(levels) < 4:
        return None
    return judge_profile(levels)


def judge_profile(levels: list[float]) -> dict[str, Any]:
    """The quiet-speech verdict for half-second RMS windows (dB). Pure, so it is testable."""
    floor_db = _percentile(levels, 0.10)
    loud_db = _percentile(levels, 0.99)
    bar = floor_db + QUIET_SPEECH_ACTIVE_ABOVE_DB
    active_s = sum(1 for v in levels if v >= bar) * PROFILE_WINDOW_S
    duration_s = len(levels) * PROFILE_WINDOW_S
    quiet_speech = (loud_db - floor_db >= QUIET_SPEECH_RANGE_DB
                    and loud_db >= QUIET_SPEECH_ABS_MIN_DB
                    and active_s >= max(QUIET_SPEECH_MIN_ACTIVE_S,
                                        QUIET_SPEECH_MIN_ACTIVE_FRAC * duration_s))
    gain_db = round(min(GAIN_MAX_DB, max(0.0, GAIN_TARGET_DB - loud_db)), 1)
    return {"floor_db": round(floor_db, 1), "loud_db": round(loud_db, 1),
            "active_s": active_s, "windows": len(levels),
            "quiet_speech": quiet_speech, "gain_db": gain_db}


def apply_gain(src: Path, dest: Path, gain_db: float, *, timeout: float = 1800.0) -> Optional[Path]:
    """Write `src` with +gain_db (peak-limited, so clicks cannot clip) to `dest`: FLAC, mono, 16 kHz, s16.

    Mono 16 kHz is what Whisper, Sortformer and the voice embedder hear anyway, and it keeps the
    copy small: a 79-minute memo is ~68 MB, where 48 kHz stereo 24-bit was 574 MB and tripped
    MAX_AUDIO_FILE_SIZE_FOR_TRANSCRIPTION (300 MB) in bin/transcribe. `dest` gets `src`'s mtime,
    because the note's name and its `captured` stamp come from the audio's mtime. None (and no
    `dest`) on any failure.
    """
    import os
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-y", "-i", str(src),
             "-vn", "-af", f"volume={gain_db:.1f}dB,alimiter=limit=0.95:level=false",
             "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", "-c:a", "flac", str(dest)],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        dest.unlink(missing_ok=True)
        return None
    if r.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        return None
    st = src.stat()
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
    return dest


@contextmanager
def gained_audio(src: Path, gain_db: Optional[float], workdir: Path) -> Iterator[Optional[Path]]:
    """A private +gain_db copy of `src` for as long as the block runs, then gone.

    Yields None when no gain is asked for or the copy cannot be written (the caller then hears
    `src` itself). Shared by every stage that HEARS a quiet recording: ASR (worker), the
    diarization pass and voice-id, so none of them works on audio 35-50 dB below speech.
    """
    if not gain_db:
        yield None
        return
    import os
    import tempfile
    workdir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f"{src.stem}.", suffix=".flac", dir=workdir)
    os.close(fd)
    dest = Path(name)
    out = None
    try:
        out = apply_gain(src, dest, float(gain_db))
        yield out
    finally:
        dest.unlink(missing_ok=True)
        try:
            workdir.rmdir()
        except OSError:
            pass


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
