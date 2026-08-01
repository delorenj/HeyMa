"""The sentinel protocol: on-disk evidence that outlives the daemon.

`waxd` owning the encoder gives correct answers while `waxd` is alive. Sentinels
give correct answers when it is not — after a SIGKILL, an OOM, or a reboot, a
process that has never run before can read these files and recompute the state.

Two writes bracket every capture:

  <rid>.rec.json   fsynced BEFORE the first audio byte, so a partial can never
                   exist without the identity needed to adjudicate it.
  <rid>.stop       fsynced BEFORE any signal is sent, so an exit is always
                   classifiable as instructed vs uninstructed. Its absence next
                   to a dead encoder is the definition of `error-partial`.

<rid>.fin.json records the outcome and is advisory only.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import paths, procutil

# How long a finalizer may hold the stream in `not-ready` before we call it dead.
# Generous: stopping a 16-hour capture means ffmpeg flushing and writing an Ogg
# trailer, plus our own ffprobe.
FINALIZE_DEADLINE_S = 180.0


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_rid(when: Optional[float] = None) -> str:
    """Sortable, human-readable, collision-resistant capture id."""
    t = time.localtime(when if when is not None else time.time())
    return f"{time.strftime('%Y%m%d-%H%M%S', t)}-{os.urandom(3).hex()}"


def _write_json_durable(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON and make both the bytes and the directory entry durable.

    Written to a temp name and renamed so a reader never observes a half-written
    sentinel — a truncated rec.json would be indistinguishable from a missing
    one and would misclassify a live recording as an error.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.rename(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def write_rec(rid: str, *, pid: int, starttime: int, device_source: str,
              target_name: str, trigger: str, label: Optional[str] = None,
              codec: str = "libopus", bitrate: str = "32k",
              channels: int = 1, sample_rate: int = 48000) -> Path:
    p = paths.rec_path(rid)
    _write_json_durable(p, {
        "rid": rid,
        "pid": pid,
        "starttime": starttime,
        "boot_id": procutil.boot_id(),
        "started_at": utcnow(),
        "started_monotonic": time.time(),
        "device_source": device_source,
        "partial_path": str(paths.partial_path(rid)),
        "target_name": target_name,
        "codec": codec,
        "bitrate": bitrate,
        "channels": channels,
        "sample_rate": sample_rate,
        "trigger": trigger,
        "label": label,
    })
    return p


def write_stop(rid: str, *, reason: str = "explicit") -> Path:
    """Record intent to stop BEFORE signalling. Never skip this."""
    me = os.getpid()
    p = paths.stop_path(rid)
    _write_json_durable(p, {
        "rid": rid,
        "reason": reason,
        "stop_requested_at": utcnow(),
        "owner_pid": me,
        "owner_boot_id": procutil.boot_id(),
        "owner_starttime": procutil.proc_starttime(me),
        "deadline_epoch": time.time() + FINALIZE_DEADLINE_S,
    })
    return p


def write_fin(rid: str, **fields: Any) -> Path:
    p = paths.fin_path(rid)
    _write_json_durable(p, {"rid": rid, "finished_at": utcnow(), **fields})
    return p


def encoder_alive(rec: dict[str, Any]) -> bool:
    return procutil.is_alive(
        int(rec.get("pid") or 0),
        int(rec.get("starttime") or -1),
        str(rec.get("boot_id") or ""),
        exe_name="ffmpeg",
    )


def finalizer_alive(stop: dict[str, Any]) -> bool:
    """Is the process that asked for the stop still around to finish the job?

    Deliberately does NOT pin the exe name: the finalizer is waxd (python3) or a
    one-shot `wax` CLI, and either is legitimate.
    """
    pid = int(stop.get("owner_pid") or 0)
    st = stop.get("owner_starttime")
    if not pid or st is None:
        return False
    if str(stop.get("owner_boot_id") or "") != procutil.boot_id():
        return False
    return procutil.proc_starttime(pid) == int(st)


def list_captures() -> list[str]:
    """Every rid with any sentinel, segment dir or partial present, newest first."""
    rids: set[str] = set()
    for p in paths.STREAM.glob("*.rec.json"):
        rids.add(p.name[: -len(".rec.json")])
    for p in paths.STREAM.glob("*.segs"):
        rids.add(p.name[: -len(".segs")])
    for p in paths.STREAM.glob("*.ogg.partial"):
        rids.add(p.name[: -len(".ogg.partial")])
    for p in paths.STREAM.glob("*.stop"):
        rids.add(p.name[: -len(".stop")])
    return sorted(rids, reverse=True)


def segments(rid: str) -> list[Path]:
    """Completed + in-flight segments for a capture, in recording order."""
    d = paths.segdir(rid)
    if not d.is_dir():
        return []
    return sorted(d.glob("seg-*.ogg"))


def segment_bytes(rid: str) -> int:
    total = 0
    for p in segments(rid):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def orphan_captures() -> list[Path]:
    """Segment dirs / partials with no rec.json — unadjudicable, needs a human.

    Grace period so we never race our own pre-spawn window, where the segment
    dir may momentarily exist before rec.json lands.
    """
    out = []
    now = time.time()
    candidates = list(paths.STREAM.glob("*.segs")) + list(paths.STREAM.glob("*.ogg.partial"))
    for p in candidates:
        rid = p.name[: -len(".segs")] if p.name.endswith(".segs") else p.name[: -len(".ogg.partial")]
        if paths.rec_path(rid).exists():
            continue
        try:
            if now - p.stat().st_mtime > 10.0:
                out.append(p)
        except FileNotFoundError:
            continue
    return out
