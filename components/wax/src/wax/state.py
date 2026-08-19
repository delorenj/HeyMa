"""Pure state derivation for both folder machines.

Every function here is a function of the filesystem and /proc only. Nothing
consults daemon memory, so `wax state --cold` from a process that has never run
returns the same answer `waxd` would — which is the whole point: the state that
matters most (`error-partial`) is by definition the one that follows an
uncontrolled exit, exactly when in-memory state is gone.
"""

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from . import paths, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

MEDIA_SUFFIXES = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".aac", ".mov", ".mp4", ".mkv", ".webm", ".wma", ".aiff"}

# ---------------------------------------------------------------- stream ----

STREAM_STATES = ("ready", "recording", "not-ready", "error-partial", "error")


def _pactl(args: list[str]) -> str:
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def preflight() -> dict[str, Any]:
    """Can we start a capture right now? Drives `not-ready` clause (b)."""
    problems: list[str] = []

    source = _pactl(["get-default-source"])
    if not source:
        problems.append("no_default_source")
    else:
        listing = _pactl(["list", "short", "sources"])
        if source not in listing:
            problems.append("default_source_absent")

    try:
        free = shutil.disk_usage(paths.AUDIO).free
    except OSError:
        free, problems = 0, problems + ["disk_unreadable"]
    if free < paths.MIN_FREE_BYTES:
        problems.append("disk_low")

    for d in (paths.STREAM, paths.INBOX):
        if not os.access(d, os.W_OK):
            problems.append(f"not_writable:{d.name}")

    return {
        "ok": not problems,
        "problems": problems,
        "source": source or None,
        "free_bytes": free,
    }


def _probe_duration(path: Path) -> Optional[float]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        return float(r.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def stream_state(*, run_preflight: bool = True) -> dict[str, Any]:
    """Derive the ~/HeyMa/stream machine purely from disk + /proc."""
    orphans = sentinel.orphan_captures()
    rids = sentinel.list_captures()

    # Adjudicate each rid that still has sentinels present.
    for rid in rids:
        rec = sentinel.read_json(paths.rec_path(rid))
        if rec is None:
            continue  # handled by the orphan sweep below
        stop = sentinel.read_json(paths.stop_path(rid))
        alive = sentinel.encoder_alive(rec)

        if stop is None:
            if alive:
                return _s("recording", rid=rid, rec=rec,
                          evidence="rec.json present, no .stop, encoder alive")
            # Encoder gone and nobody asked it to stop: uninstructed exit.
            return _s("error-partial", rid=rid, rec=rec,
                      cause_code="uninstructed_exit",
                      evidence=f"rec.json present, no .stop, pid {rec.get('pid')} fails alive()",
                      partial_bytes=sentinel.segment_bytes(rid),
                  segments=len(sentinel.segments(rid)))

        # A stop was requested: we are finalizing (clause a) unless the
        # finalizer itself died or blew its deadline.
        if not sentinel.finalizer_alive(stop):
            return _s("error-partial", rid=rid, rec=rec,
                      cause_code="finalizer_died",
                      evidence=f"stop present, owner pid {stop.get('owner_pid')} not alive",
                      partial_bytes=sentinel.segment_bytes(rid),
                  segments=len(sentinel.segments(rid)))
        if time.time() > float(stop.get("deadline_epoch") or 0):
            return _s("error-partial", rid=rid, rec=rec,
                      cause_code="finalize_deadline_expired",
                      evidence=f"stop present, deadline {stop.get('deadline_epoch')} passed",
                      partial_bytes=sentinel.segment_bytes(rid),
                  segments=len(sentinel.segments(rid)))
        return _s("not-ready", rid=rid, rec=rec, clause="a",
                  cause_code="finalizing",
                  evidence="explicit stop requested, finalizer alive, within deadline",
                  partial_bytes=sentinel.segment_bytes(rid),
                  segments=len(sentinel.segments(rid)))

    if orphans:
        # A .partial with no rec.json cannot be attributed to any capture. We
        # refuse to guess; this needs a human.
        return _s("error", cause_code="orphan_partial",
                  evidence=f"{len(orphans)} capture artifact(s) with no rec.json: "
                           f"{', '.join(p.name for p in orphans[:3])}")

    pf = preflight() if run_preflight else {"ok": True, "problems": [], "source": None, "free_bytes": None}
    if not pf["ok"]:
        return _s("not-ready", clause="b", cause_code=pf["problems"][0],
                  evidence=f"stream dir clean but preflight failed: {pf['problems']}",
                  preflight=pf)
    return _s("ready", evidence="no sentinels, no partials, preflight ok", preflight=pf)


def _size(p: Path) -> Optional[int]:
    try:
        return p.stat().st_size
    except OSError:
        return None


def _s(state: str, **extra: Any) -> dict[str, Any]:
    rec = extra.pop("rec", None)
    out: dict[str, Any] = {"state": state, "clause": None, "cause_code": None, "rid": None}
    out.update(extra)
    if rec:
        out.setdefault("started_at", rec.get("started_at"))
        out.setdefault("device_source", rec.get("device_source"))
        out.setdefault("target_name", rec.get("target_name"))
    return out


# ----------------------------------------------------------------- inbox ----

INBOX_STATES = ("ready-and-waiting", "ready-and-active", "error", "stopped")

# How long a file may sit unclaimed before it counts as stranded rather than
# merely queued. The worker polls every 5s, so a full minute of silence means
# something is genuinely wrong.
QUEUE_GRACE_S = 60.0

ENABLED_FLAG = paths.VAR / "pipeline.enabled"
CLAIM_FILE = paths.VAR / "inbox.claim"


def pipeline_enabled() -> bool:
    return ENABLED_FLAG.exists()


def _active_claim() -> Optional[dict[str, Any]]:
    """The item currently being worked, if the worker holding it is alive.

    A claim whose owner is dead is not activity — it is exactly the stranded
    work that must read as `error`, not as `ready-and-active`.
    """
    claim = sentinel.read_json(CLAIM_FILE)
    if not claim:
        return None
    if not sentinel.finalizer_alive(claim):
        return None
    return claim


# Syncthing's own bookkeeping, which lives INSIDE the inbox but is not inbox
# content. `.stversions` is the dangerous one: versioning on this folder is
# staggered with 365d retention, so it holds a copy of every version of every
# file ever synced here — descending into it would adopt the entire deletion
# history as fresh work. The names are redundant with the dot-prefix rule
# below and are kept explicit so these two stay excluded even if that rule is
# ever relaxed.
SYNC_PRIVATE_DIRS = (".stfolder", ".stversions")

# inbox_items() runs on the waxd poll loop, so this is emitted once per CHANGE
# rather than once per call — an unconditional line would bury the journal and
# nobody would read it.
_nested_logged: Optional[int] = None


def inbox_items() -> list[Path]:
    """Every media file under the inbox, at ANY depth.

    Recursive, not flat. reconcile.scan_local() has always walked the inbox
    with rglob while this walked it with iterdir, so the two halves of the
    system disagreed about what the inbox held: 5 files totalling
    1,074,201,440 B under inbox/2025 and inbox/2026 (measured 2026-08-19) had
    no ledger row, could never be returned by worker.next_item(), and the tray
    truthfully reported the inbox empty for as long as they sat there.
    """
    global _nested_logged
    if not paths.INBOX.exists():
        return []

    items: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(paths.INBOX):
        # Prune in place, so we never DESCEND — filtering the results instead
        # would still stat every version in .stversions on every poll. The
        # dot-prefix test rather than a fixed name list is deliberate:
        # Syncthing renames markers aside on a folder reset, and this inbox is
        # carrying a `.stfolder.removed-20260629-053824` that a name-only list
        # would have walked straight into.
        dirnames[:] = [d for d in dirnames
                       if d not in SYNC_PRIVATE_DIRS and not d.startswith(".")]
        here = Path(dirpath)
        for name in filenames:
            if name.startswith(".") or Path(name).suffix.lower() not in MEDIA_SUFFIXES:
                continue
            p = here / name
            if p.is_file():
                items.append(p)
    items.sort()

    nested = sum(1 for p in items if p.parent != paths.INBOX)
    if nested != _nested_logged:
        _nested_logged = nested
        if nested:
            log.info("inbox holds %d media file(s) in subdirectories of %d total; "
                     "they are queue entries like any other", nested, len(items))
    return items


def inbox_state() -> dict[str, Any]:
    items = inbox_items()
    pending = len(items)
    claim = _active_claim()
    enabled = pipeline_enabled()

    if not enabled:
        # Disabled is an operator-selected stopped state, regardless of queue
        # depth. Calling a deliberately paused queue an error makes the tray
        # claim the system failed when it did exactly what was requested.
        evidence = (
            f"pipeline paused; {pending} item(s) waiting"
            if pending
            else "pipeline paused; inbox empty"
        )
        return {"state": "stopped", "cause_code": "scheduler_disabled", "pending": pending,
                "active_item": None, "evidence": evidence}

    if claim:
        return {"state": "ready-and-active", "cause_code": None, "pending": pending,
                "active_item": claim.get("item"), "active_stage": claim.get("stage"),
                "active_elapsed_s": round(time.time() - float(claim.get("claimed_epoch") or time.time()), 1),
                "evidence": f"worker pid {claim.get('owner_pid')} alive on {claim.get('item')}"}

    if pending:
        # A file that JUST landed is queued, not stranded — the worker polls
        # every few seconds. Without this grace window every recording flashes
        # the tray yellow on its way in, which trains you to ignore yellow,
        # which is exactly how a real failure goes unnoticed.
        # ctime (not mtime) is when the file arrived at THIS path, so a
        # timestamp-preserving copy still reads as newly-arrived.
        try:
            waited = min(time.time() - p.stat().st_ctime for p in items)
        except OSError:
            waited = QUEUE_GRACE_S + 1
        if waited < QUEUE_GRACE_S:
            return {"state": "ready-and-active", "cause_code": "queued", "pending": pending,
                    "active_item": None, "active_stage": "queued",
                    "evidence": f"{pending} item(s) queued, youngest waiting {waited:.0f}s"}
        return {"state": "error", "cause_code": "stranded_work", "pending": pending,
                "active_item": None,
                "evidence": f"{pending} item(s) in inbox, no live worker claim for {waited:.0f}s"}

    # Deliberately NOT "no errors". This function reads the inbox listing and
    # the enable flag and nothing else — it never touches the passes table or
    # transcripts.diarized — so the old "inbox empty, scheduler enabled, no
    # errors" asserted a fact it had not checked. It was measured printing
    # exactly that five seconds after a title-slug failure was written to the
    # ledger, and it printed it every day of a week-long diarization outage.
    # An evidence string may only claim what it inspected; stage health rides
    # on the snapshot's own health blocks, added by ledger.enrich().
    return {"state": "ready-and-waiting", "cause_code": None, "pending": 0,
            "active_item": None, "evidence": "inbox empty, scheduler enabled"}


def snapshot(*, run_preflight: bool = True) -> dict[str, Any]:
    return {
        "updated_at": sentinel.utcnow(),
        "stream": stream_state(run_preflight=run_preflight),
        "inbox": inbox_state(),
    }
