"""Abandoned-capture detection: a recording nobody is listening to must not outlive its owner.

The 2026-11-18 incident: a Yeti mic ran 16 h in near-silence. The next Ctrl+\\ press (toggle: recording
-> stop) became a STOP of that ghost capture instead of the start of an interview. waxd already noticed
(`capture_silent`) but only logged, and the condition flapped warn/clear all night because every noise blip
prints a silence_end. A single "silent now" bit cannot be trusted; a FRACTION over a long trailing window can.

Evidence is the encoder's own `silencedetect` output (-50 dB, d=5 s, see capture._ffmpeg_argv), replayed from
the top of `encoder.log`, so a waxd restarted mid-capture reaches the same verdict as one that never died.
The clock is the ENCODER'S timeline, not waxd's wall clock:

  silence_start / silence_end   pts in seconds since the filter started
  `Opening '...seg-N.ogg'`      segment N opened, so the capture is at least N * segment_seconds long

`now` is the larger of the last pts seen and the last opened segment's start, accurate to one segment (60 s),
which is nothing against a 3600 s window. An unclosed silence extends to `now`. A suspended laptop or a stalled
encoder stops both clocks together, so a pause in recording is never miscounted as silence.

Defaults (measured on the 32 real captures >= 60 min long in var/logs, 2026-11-19; the worst trailing-hour
silent fraction of each):
  WAX_ABANDON_WINDOW_S=3600      no meeting pause is an hour. 28 of the 32 never exceeded 0.32; the other four
                                 (three forgotten-to-stop overruns and the incident) reached 0.978-1.000, and
                                 nothing sits between, so any threshold from 0.4 to 0.97 separates them. The
                                 incident crosses 0.97 at 74 min instead of running 16 h. Stopping costs nothing
                                 audible: the trailing hour is >= 97% silent by definition.
  WAX_ABANDON_SILENT_FRACTION=0.97
  WAX_ABANDON_WINDOW_S=0         disables the whole feature.

A stop is NEVER a delete. It is the ordinary `wax rec stop`: finalize into inbox/, where the junk gate and
`wax split` already know what to do with the content.
"""

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import capture, paths, state

REASON = "abandoned_silent"

DEFAULT_WINDOW_S = 3600.0
DEFAULT_FRACTION = 0.97

_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)(?:\s*\|\s*silence_duration:\s*(-?\d+(?:\.\d+)?))?")
_SEG = re.compile(r"Opening '.*?seg-(\d+)\.ogg' for writing")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value == value else default  # NaN guard


@dataclass(frozen=True)
class Params:
    window_s: float = DEFAULT_WINDOW_S
    fraction: float = DEFAULT_FRACTION

    @property
    def enabled(self) -> bool:
        return self.window_s > 0 and 0 < self.fraction <= 1

    @classmethod
    def from_env(cls) -> "Params":
        return cls(window_s=_env_float("WAX_ABANDON_WINDOW_S", DEFAULT_WINDOW_S),
                   fraction=_env_float("WAX_ABANDON_SILENT_FRACTION", DEFAULT_FRACTION))


class Timeline:
    """Incremental parse of an encoder log into silent intervals on the encoder's own clock."""

    def __init__(self, segment_seconds: float = 60.0) -> None:
        self.segment_seconds = float(segment_seconds or 60.0)
        self.closed: list[tuple[float, float]] = []
        self.open_since: Optional[float] = None
        self.last_pts = 0.0
        self.last_segment = 0

    def feed(self, line: str) -> None:
        if "silence_" in line:
            m = _START.search(line)
            if m:
                pts = max(0.0, float(m.group(1)))
                if self.open_since is None:
                    self.open_since = pts
                self.last_pts = max(self.last_pts, pts)
                return
            m = _END.search(line)
            if m:
                end = max(0.0, float(m.group(1)))
                start = self.open_since
                if start is None and m.group(2) is not None:
                    start = end - max(0.0, float(m.group(2)))  # the start line was lost (trimmed log)
                if start is not None and end > start:
                    self.closed.append((start, end))
                self.open_since = None
                self.last_pts = max(self.last_pts, end)
            return
        m = _SEG.search(line)
        if m:
            self.last_segment = max(self.last_segment, int(m.group(1)))

    def now(self) -> float:
        return max(self.last_pts, self.last_segment * self.segment_seconds)

    def verdict(self, params: Params) -> dict[str, Any]:
        now = self.now()
        window = params.window_s
        start = now - window
        if start > 0:
            # `now` only grows, so anything that ended before the window opens can never count again.
            self.closed = [iv for iv in self.closed if iv[1] > start]
        silent = sum(max(0.0, min(b, now) - max(a, start, 0.0)) for a, b in self.closed)
        if self.open_since is not None:
            silent += max(0.0, now - max(self.open_since, start, 0.0))
        covered = min(now, window) if window > 0 else 0.0
        fraction = silent / covered if covered > 0 else 0.0
        return {
            "enabled": params.enabled,
            "elapsed_s": round(now, 1),
            "window_s": window,
            "silent_s": round(silent, 1),
            "fraction": round(fraction, 4),
            "threshold": params.fraction,
            # A capture younger than the window has no full window to be judged by, so it is never abandoned.
            "abandoned": bool(params.enabled and now >= window and fraction >= params.fraction),
        }


def replay(lines, params: Params, segment_seconds: float = 60.0) -> dict[str, Any]:
    """One-shot verdict for a whole log (tests, `wax doctor`-style probes)."""
    tl = Timeline(segment_seconds)
    for line in lines:
        tl.feed(line)
    return tl.verdict(params)


def _minutes(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 2 * 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 60} min"


def describe(verdict: dict[str, Any]) -> str:
    return (f"{REASON}: encoder silent {verdict['fraction'] * 100:.1f}% of the last "
            f"{_minutes(verdict['window_s'])} (threshold {verdict['threshold'] * 100:.0f}%), "
            f"capture {_minutes(verdict['elapsed_s'])} long")


def stop_abandoned(rid: str, verdict: dict[str, Any], *,
                   before_stop: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """The explicit stop, by the same path as `wax rec stop`: capture lock, then capture.stop(rid).

    The state is re-read UNDER the lock: a person who already stopped (or restarted) the capture between
    the verdict and now must not have a second, unwanted stop land on whatever is recording next.
    """
    detail = describe(verdict)
    paths.ensure_dirs()
    with capture.lifecycle_lock():
        cur = state.stream_state(run_preflight=False)
        if cur.get("state") != "recording" or cur.get("rid") != rid:
            return {"action": "skipped", "rid": rid, "state": cur.get("state"), "current_rid": cur.get("rid")}
        if before_stop is not None:
            before_stop(detail)
        out = capture.stop(rid, reason=REASON, detail=detail)
    return {"action": "stopped", "reason": REASON, "detail": detail, **out}


def notification(verdict: dict[str, Any], result: dict[str, Any]) -> tuple[str, str]:
    name = (result.get("path") or "").rsplit("/", 1)[-1]
    body = (f"The mic was silent {verdict['fraction'] * 100:.0f}% of the last {_minutes(verdict['window_s'])} "
            f"({_minutes(verdict['elapsed_s'])} recorded), so Wax stopped it and saved it to the inbox"
            f"{' as ' + name if name else ''}. Check the mic, then press record for a new one.")
    return "Wax: stopped an abandoned recording", body
