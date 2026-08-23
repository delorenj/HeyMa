"""Spawning, stopping and finalizing the encoder.

Two invariants carry the whole design:

1. rec.json is durable BEFORE ffmpeg can emit a single byte. Achieved by
   forking, writing+fsyncing the sentinel in the child, and only then exec'ing.
   A partial file can therefore never exist without its identity sentinel.

2. The file enters ~/HeyMa/inbox by atomic rename, only after the encoder has
   exited 0 AND ffprobe confirms real duration. Nothing polls file size, ever —
   size-polling is what moved a still-open 16.5-hour recording and destroyed it.

The encoder runs in its own transient systemd scope (verified: systemd-run
--scope exec's in place, so our pid IS ffmpeg's and wait() still works), which
keeps `systemctl --user restart waxd` from killing an in-flight capture.

STOPPING IS NOT DONE WITH SIGNALS. Measured on this box, ffmpeg catches SIGINT
and SIGTERM (SigCgt bit 2 set) but does not act on either during a pulse
capture: still running after 15 s, output 0 bytes and unreadable by ffprobe.
Writing "q" to its stdin exits rc=0 in ~0.2 s with a complete, valid file.
Stdin is therefore a FIFO whose path is recorded in rec.json, so a restarted
waxd or a cold CLI can stop a capture it never owned. Signals remain only as a
last-resort escalation, and a SIGKILLed capture is treated as error-partial
rather than published.
"""

import errno
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from . import paths, procutil, rename, sentinel, state

QUIT_GRACE_S = 30.0      # 'q' normally returns in ~0.2s; generous for a long capture's trailer
SIGTERM_GRACE_S = 10.0
MIN_VALID_DURATION_S = 0.5


class CaptureError(RuntimeError):
    pass


def _ffmpeg_argv(source: str, out: Path, rid: str, *, channels: int, rate: int, bitrate: str) -> list[str]:
    # Deliberately NO -nostdin: stdin is our control channel (see module docstring).
    return [
        "systemd-run", "--user", "--scope", "--collect", "--quiet",
        "--unit", f"wax-{rid}",
        "--",
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        # Flush every packet to disk instead of filling a ~32 KiB AVIO buffer
        # first. Measured: without this a SIGKILLed capture leaves a 0-byte
        # file — the buffer dies with the process. The audio is the
        # irreplaceable artifact, so we trade a few syscalls per second (at
        # 32 kbps this is nothing) for a partial that is always current and
        # therefore always salvageable.
        "-flush_packets", "1",
        "-f", "pulse", "-i", source,
        "-ac", str(channels), "-ar", str(rate),
        "-c:a", "libopus", "-b:a", bitrate,
        # Segment muxer: each closed segment carries its own trailer and is
        # independently valid, so a hard kill costs only the in-flight segment
        # instead of everything since the last 256 KiB buffer flush.
        "-f", "segment",
        "-segment_time", str(paths.SEGMENT_SECONDS),
        "-segment_format", "ogg",
        "-reset_timestamps", "1",
        str(out),
    ]


def _quit_encoder(rid: str) -> bool:
    """Ask the encoder to finish cleanly. Returns False if the FIFO is gone."""
    ctl = paths.ctl_path(rid)
    if not ctl.exists():
        return False
    try:
        fd = os.open(ctl, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, b"q")
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def start(*, trigger: str = "cli", label: Optional[str] = None,
          channels: int = 1, rate: int = 48000, bitrate: str = "32k") -> dict[str, Any]:
    """Begin a capture. Returns the rec sentinel contents."""
    pf = state.preflight()
    if not pf["ok"]:
        raise CaptureError(f"preflight failed: {pf['problems']}")
    source = pf["source"]

    cur = state.stream_state(run_preflight=False)
    if cur["state"] != "ready":
        raise CaptureError(f"stream is '{cur['state']}' ({cur.get('cause_code')}), refusing to start")

    paths.ensure_dirs()
    rid = sentinel.new_rid()
    segdir = paths.segdir(rid)
    segdir.mkdir(parents=True, exist_ok=True)
    partial = segdir / "seg-%05d.ogg"
    slug = f"{label}" if label else "rec"
    target_name = f"{rid.rsplit('-', 1)[0]}-{slug}.ogg"

    logdir = paths.LOGS / rid
    logdir.mkdir(parents=True, exist_ok=True)
    logfh = open(logdir / "encoder.log", "ab", buffering=0)

    # Pre-render the sentinel in the PARENT so the forked child performs only
    # string splicing + write + fsync + exec. Calling into json/logging after
    # fork in a threaded process risks inheriting a held lock.
    template = json.dumps({
        "rid": rid,
        "pid": "__PID__",
        "starttime": "__STARTTIME__",
        "boot_id": procutil.boot_id(),
        "started_at": sentinel.utcnow(),
        "device_source": source,
        "segdir": str(paths.segdir(rid)),
        "ctl_path": str(paths.ctl_path(rid)),
        "target_name": target_name,
        "codec": "libopus",
        "bitrate": bitrate,
        "channels": channels,
        "sample_rate": rate,
        "trigger": trigger,
        "label": label,
    }, indent=2, sort_keys=True)

    argv = _ffmpeg_argv(source, partial, rid, channels=channels, rate=rate, bitrate=bitrate)
    recfile = paths.rec_path(rid)

    ctl = paths.ctl_path(rid)
    ctl.unlink(missing_ok=True)
    os.mkfifo(ctl, 0o600)

    pid = os.fork()
    if pid == 0:  # ---- child ----
        try:
            me = os.getpid()
            st = open(f"/proc/{me}/stat").read()
            starttime = st[st.rindex(")") + 2:].split()[19]
            payload = template.replace('"__PID__"', str(me)).replace('"__STARTTIME__"', starttime)

            tmp = str(recfile) + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            os.write(fd, payload.encode())
            os.fsync(fd)
            os.close(fd)
            os.rename(tmp, recfile)
            dfd = os.open(str(paths.STREAM), os.O_RDONLY | os.O_DIRECTORY)
            os.fsync(dfd)
            os.close(dfd)

            # O_RDWR on a FIFO never blocks and keeps a writer end open, so
            # ffmpeg's stdin never hits EOF while no controller is attached.
            cfd = os.open(str(ctl), os.O_RDWR)
            os.dup2(cfd, 0)
            os.dup2(logfh.fileno(), 1)
            os.dup2(logfh.fileno(), 2)
            os.execvp(argv[0], argv)
        except BaseException:
            os._exit(127)
    # ---- parent ----
    logfh.close()

    # The sentinel is written by the child; wait briefly for it to land so the
    # caller never observes a capture that exists but cannot be adjudicated.
    for _ in range(200):
        if recfile.exists():
            break
        if _reaped(pid):
            raise CaptureError(f"encoder exited before writing sentinel; see {logdir/'encoder.log'}")
        time.sleep(0.01)
    else:
        raise CaptureError("timed out waiting for rec.json")

    # rec.json lands BEFORE exec, so for a brief window /proc/<pid>/exe is still
    # python. Wait for the exec to complete, otherwise a status check racing
    # start() would see exe != ffmpeg and misreport a healthy capture as
    # error-partial.
    for _ in range(500):
        if procutil.proc_exe_name(pid) == "ffmpeg":
            break
        if _reaped(pid):
            raise CaptureError(f"encoder died during exec; see {logdir/'encoder.log'}")
        time.sleep(0.01)
    else:
        raise CaptureError("encoder did not exec ffmpeg within 5s")

    rec = sentinel.read_json(recfile) or {}
    rec["_logdir"] = str(logdir)
    _emit("session", "started", {
        "capture_id": rid, "started_at": rec.get("started_at"),
        "device_source": source, "codec": "libopus", "bitrate": bitrate,
        "sample_rate_hz": rate, "channels": channels, "trigger": trigger,
        "segment_seconds": paths.SEGMENT_SECONDS,
    }, ordering_key=rid)
    return rec


def _emit(entity: str, action: str, data: dict[str, Any], **kw: Any) -> None:
    """Fail-open: a bus problem must never interrupt a capture."""
    try:
        from . import events
        events.emit(entity, action, data, **kw)
    except Exception:  # noqa: BLE001
        pass


def _reaped(pid: int) -> bool:
    """Non-blocking check for our own child having exited."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == pid
    except ChildProcessError:
        return True
    except OSError as e:
        return e.errno == errno.ECHILD


def _await_exit(rec: dict[str, Any], timeout: float) -> Optional[int]:
    """Wait for the encoder to exit; returns exit status if we could reap it.

    Handles both cases: the encoder is our child (waxd started it) and it is
    not (waxd restarted, or a one-shot CLI is finalizing someone else's
    capture). In the latter case we poll /proc identity instead.
    """
    pid = int(rec.get("pid") or 0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                return status
        except (ChildProcessError, OSError):
            if not sentinel.encoder_alive(rec):
                return None  # exited, status unknowable (not our child)
        if not sentinel.encoder_alive(rec):
            return None
        time.sleep(0.05)
    raise TimeoutError("encoder did not exit within grace period")


def stop(rid: str) -> dict[str, Any]:
    """Explicitly stop a capture and move the finished file into the inbox."""
    rec = sentinel.read_json(paths.rec_path(rid))
    if rec is None:
        raise CaptureError(f"no rec.json for {rid}")

    # Intent BEFORE signal. If we die between these two lines the state is
    # `not-ready` then `error-partial` on deadline — never a silent truncation.
    sentinel.write_stop(rid)

    if sentinel.encoder_alive(rec):
        _shutdown(rid, rec)

    return finalize(rid)


def _shutdown(rid: str, rec: dict[str, Any]) -> str:
    """Bring the encoder down as gently as possible. Returns how it went.

    Order matters and is measured, not assumed: 'q' finalizes cleanly in ~0.2 s;
    signals do not work at all here and only ever produce an unreadable file, so
    they are escalation of last resort and their use is reported so the caller
    can refuse to publish the result.
    """
    if _quit_encoder(rid):
        try:
            _await_exit(rec, QUIT_GRACE_S)
            return "clean"
        except TimeoutError:
            pass

    pid = int(rec.get("pid") or 0)
    for sig, grace in ((signal.SIGTERM, SIGTERM_GRACE_S), (signal.SIGKILL, 5.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return "exited"
        try:
            _await_exit(rec, grace)
            return "signalled"
        except TimeoutError:
            continue
    return "unresponsive"


def concat_segments(rid: str) -> tuple[Optional[Path], list[Path], list[Path]]:
    """Join every VALID segment into one staged file.

    Returns (staged_path, used, skipped). Invalid segments are skipped rather
    than aborting the join: after a hard kill the last segment is typically a
    0-byte stub, and losing the whole recording because its tail is unreadable
    would be exactly the failure mode this design exists to prevent.
    """
    segs = sentinel.segments(rid)
    if not segs:
        return None, [], []

    used, skipped = [], []
    for s in segs:
        d = state._probe_duration(s)
        (used if (d is not None and d > 0) else skipped).append(s)
    if not used:
        return None, [], skipped

    staged = paths.partial_path(rid)
    staged.unlink(missing_ok=True)
    listfile = paths.segdir(rid) / "concat.txt"
    listfile.write_text("".join(f"file '{p}'\n" for p in used))

    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-f", "concat", "-safe", "0", "-i", str(listfile),
         # -f ogg is required: the staged name ends in .partial, so ffmpeg
         # cannot infer the muxer from the extension.
         "-c", "copy", "-f", "ogg", str(staged)],
        capture_output=True, text=True, timeout=3600,
    )
    if r.returncode != 0 or not staged.exists():
        raise CaptureError(f"concat failed for {rid}: {r.stderr[-400:]}")
    return staged, used, skipped


def finalize(rid: str) -> dict[str, Any]:
    """Join the segments, validate, and atomically publish to the inbox."""
    rec = sentinel.read_json(paths.rec_path(rid))
    if rec is None:
        raise CaptureError(f"no rec.json for {rid}")

    if sentinel.encoder_alive(rec):
        raise CaptureError(f"encoder for {rid} still alive; refusing to finalize")

    partial, used, skipped = concat_segments(rid)
    if partial is None:
        sentinel.write_fin(rid, ok=False, reason="no_valid_segments",
                           segments=len(sentinel.segments(rid)))
        raise CaptureError(f"refusing to publish {rid}: no valid segments")

    dur = state._probe_duration(partial)
    if dur is None or dur <= MIN_VALID_DURATION_S:
        # Do NOT publish. Leave everything in place for salvage; the stream
        # machine will report error-partial and a human decides.
        sentinel.write_fin(rid, ok=False, reason="unprobeable_or_too_short",
                           probe_duration_s=dur, bytes=partial.stat().st_size)
        raise CaptureError(f"refusing to publish {rid}: duration={dur}")

    dest = paths.INBOX / str(rec.get("target_name") or f"{rid}.ogg")
    final = rename.move_noclobber(partial, dest)

    size = final.stat().st_size
    sentinel.write_fin(rid, ok=True, duration_s=dur, bytes=size, inbox_path=str(final),
                       segments_used=len(used), segments_skipped=len(skipped))

    # Sentinels retired only after the payload is safely in the inbox.
    for p in (paths.rec_path(rid), paths.stop_path(rid), paths.fin_path(rid), paths.ctl_path(rid)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    shutil.rmtree(paths.segdir(rid), ignore_errors=True)
    _emit("session", "ended", {
        "capture_id": rid, "duration_s": dur, "bytes": size,
        "inbox_path": str(final), "canonical_name": final.name,
        "segments_used": len(used), "segments_skipped": len(skipped),
    }, ordering_key=rid)
    return {"rid": rid, "path": str(final), "duration_s": dur, "bytes": size,
            "segments_used": len(used), "segments_skipped": len(skipped)}


def cancel(rid: str) -> dict[str, Any]:
    """Stop and discard: bytes go to recovered/canceled/, never the inbox."""
    rec = sentinel.read_json(paths.rec_path(rid))
    if rec is None:
        raise CaptureError(f"no rec.json for {rid}")
    sentinel.write_stop(rid, reason="cancel")
    if sentinel.encoder_alive(rec):
        _shutdown(rid, rec)

    dest_dir = paths.RECOVERED / "canceled"
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = None
    segs = sentinel.segments(rid)
    if segs:
        # Even a cancel keeps the bytes — "never delete" is not conditional on
        # the user changing their mind.
        d = dest_dir / rid
        d.mkdir(parents=True, exist_ok=True)
        for sp in segs:
            shutil.move(str(sp), str(d / sp.name))
        moved = str(d)
    shutil.rmtree(paths.segdir(rid), ignore_errors=True)
    paths.partial_path(rid).unlink(missing_ok=True)
    for p in (paths.rec_path(rid), paths.stop_path(rid), paths.fin_path(rid), paths.ctl_path(rid)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    return {"rid": rid, "discarded_to": moved}


def quiesce() -> dict[str, Any]:
    """Finish an active capture during an orderly session shutdown.

    The encoder scope survives a waxd restart, but a graphical-session restart
    also tears down PipeWire and its Pulse source graph.  A dedicated systemd
    stop hook calls this while the audio graph is still available.  Being idle
    is success so the hook is safe on every logout and reboot.
    """
    captures = sentinel.list_captures()
    if not captures:
        return {"action": "idle", "state": "ready"}

    current = state.stream_state(run_preflight=False)
    if current["state"] not in ("recording", "not-ready"):
        raise CaptureError(
            f"stream is {current['state']} ({current.get('cause_code')}); "
            "refusing automatic shutdown recovery"
        )
    rid = str(current.get("rid") or captures[0])
    result = stop(rid)
    return {"action": "stopped", **result}


def _preserve_orphan_evidence(rid: str) -> Path:
    """Move every surviving capture artifact out of ``stream/`` intact.

    Salvage publishes a remuxed copy, but the segment set is the closest thing
    to the original recording after an uninstructed exit.  It must not be
    removed merely because the remux succeeded: a skipped or subtly damaged
    segment may be the only copy of irreplaceable audio.  Keep the segments,
    concat manifest, staging partial, and lifecycle sentinels together under a
    collision-safe evidence directory.
    """
    parent = paths.RECOVERED / "orphans"
    parent.mkdir(parents=True, exist_ok=True)

    segdir = paths.segdir(rid)
    if segdir.is_dir():
        evidence = rename.move_noclobber(segdir, parent / rid)
    else:
        evidence = parent / rid
        for attempt in range(51):
            candidate = evidence if attempt == 0 else parent / f"{rid}-{attempt}"
            try:
                candidate.mkdir()
                evidence = candidate
                break
            except FileExistsError:
                continue
        else:
            raise CaptureError(f"could not allocate recovery directory for {rid}")

    for artifact in (
        paths.partial_path(rid),
        paths.rec_path(rid),
        paths.stop_path(rid),
        paths.fin_path(rid),
        paths.ctl_path(rid),
    ):
        if artifact.exists():
            rename.move_noclobber(artifact, evidence / artifact.name)
    return evidence


def salvage(rid: str) -> dict[str, Any]:
    """Recover an error-partial: keep the bytes, get out of the error state.

    Every completed segment is independently valid, so an uninstructed exit
    usually costs only the in-flight segment. Whatever survived is joined and
    published like any other item. The original segments and all sentinels are
    then retained under recovered/orphans/; salvage never deletes evidence.
    """
    rec = sentinel.read_json(paths.rec_path(rid)) or {}
    if sentinel.encoder_alive(rec):
        raise CaptureError(f"{rid} encoder still alive; not an error-partial")

    try:
        partial, used, skipped = concat_segments(rid)
    except CaptureError:
        partial, used, skipped = None, [], sentinel.segments(rid)

    if partial is None:
        evidence = _preserve_orphan_evidence(rid)
        result = {"rid": rid, "salvaged": True, "to": str(evidence), "published": False,
                  "evidence_path": str(evidence),
                  "reason": "no valid segments", "segments_skipped": len(skipped)}
    else:
        dur = state._probe_duration(partial)
        if dur is None or dur <= MIN_VALID_DURATION_S:
            evidence = _preserve_orphan_evidence(rid)
            result = {"rid": rid, "salvaged": True, "to": str(evidence), "duration_s": dur,
                      "evidence_path": str(evidence),
                      "published": False, "reason": "unprobeable"}
        else:
            name = str(rec.get("target_name") or f"{rid}.ogg")
            stem, _, ext = name.rpartition(".")
            moved = rename.move_noclobber(partial, paths.INBOX / f"{stem or name}-salvaged.{ext or 'ogg'}")
            evidence = _preserve_orphan_evidence(rid)
            result = {"rid": rid, "salvaged": True, "to": str(moved), "duration_s": dur,
                      "published": True, "segments_used": len(used),
                      "segments_skipped": len(skipped),
                      "evidence_path": str(evidence)}
    return result
