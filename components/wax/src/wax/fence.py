"""Halt ONE item's in-flight work, and refuse its stale results (`wax split`, HEYMA-18).

Two halves, both scoped to a single item id so the queue and every other item
carry on:

* the **fence** (`item_fences`, see ledger.Fenced): once raised, the worker skips
  the item, `ledger.set_item_state` refuses to move it, passes and finalize refuse
  it, and the transcriber and passes check it after their child returns and before
  they publish. A result that arrives late is therefore dropped, not applied.
* **child tracking** (`item_children`): the worker spawns the transcriber and each
  pass through `run_tracked`, which records the child's identity (pid, starttime,
  boot id) so `halt_children` can stop exactly the processes of the targeted item.
  The identity check keeps a recycled pid from ever being signalled.

Nothing here schedules anything; `wax split` calls these once, inside one command.
"""

import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any, Optional, Sequence

from . import ledger, paths, procutil, sentinel
from .ledger import Fenced  # noqa: F401  (re-exported: callers catch fence.Fenced)

KILL_GRACE_S = 5.0

# What a waxd that honours the fence puts in its status snapshot (socket and state.json,
# `daemon.capabilities`). A waxd started before the fence existed never wrote the field, so its
# absence is the proof that its worker ignores `item_fences` and does not track its children:
# `wax split` refuses to halt an item under such a daemon (see `require_fence_support`).
CAPABILITY = "item-fence/1"
PROBE_TIMEOUT_S = 3.0


def daemon_advert(pid: Optional[int] = None) -> dict[str, Any]:
    """The `daemon` block waxd publishes in every snapshot it serves or mirrors."""
    return {"pid": os.getpid() if pid is None else pid, "capabilities": [CAPABILITY]}


def raise_fence(item_id: str, split_id: str, reason: str = "split") -> bool:
    """Fence the item. True when this call raised it, False when it was already fenced."""
    cursor = ledger.connect().execute(
        "INSERT OR IGNORE INTO item_fences(item_id,split_id,reason,created_at) VALUES(?,?,?,?)",
        (item_id, split_id, reason, sentinel.utcnow()))
    return cursor.rowcount == 1


def lift_fence(item_id: str, split_id: str) -> None:
    """Remove OUR fence (a halt that timed out must not strand the item)."""
    ledger.connect().execute("DELETE FROM item_fences WHERE item_id=? AND split_id=?", (item_id, split_id))


def run_tracked(item_id: Optional[str], argv: Sequence[str], *, env: Optional[dict[str, str]] = None,
                timeout: Optional[float] = None) -> "subprocess.CompletedProcess[str]":
    """`subprocess.run(capture_output=True, text=True)`, with the child registered to `item_id`.

    On timeout the whole process tree is stopped (the old code killed only the
    direct child, which for the transcriber shim left python running) and
    TimeoutExpired is raised as before.
    """
    proc = subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    _register(item_id, proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            procutil.kill_tree(proc.pid, grace_s=KILL_GRACE_S)
            proc.communicate()
            raise
        except BaseException:
            procutil.kill_tree(proc.pid, grace_s=KILL_GRACE_S)
            proc.wait()
            raise
    finally:
        _unregister(item_id, proc.pid)
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)


def _register(item_id: Optional[str], proc: "subprocess.Popen[Any]") -> None:
    if not item_id:
        return
    try:
        ledger.connect().execute(
            "INSERT OR REPLACE INTO item_children(item_id,pid,starttime,boot_id,argv0,started_at) "
            "VALUES(?,?,?,?,?,?)",
            (item_id, proc.pid, procutil.proc_starttime(proc.pid), procutil.boot_id(),
             os.path.basename(str(proc.args[0])) if proc.args else None, sentinel.utcnow()))
    except Exception:  # noqa: BLE001 - tracking is best effort; it must never stop a transcription
        pass


def _unregister(item_id: Optional[str], pid: int) -> None:
    if not item_id:
        return
    try:
        ledger.connect().execute("DELETE FROM item_children WHERE item_id=? AND pid=?", (item_id, pid))
    except Exception:  # noqa: BLE001
        pass


def halt_children(item_id: str) -> list[dict[str, Any]]:
    """Stop every live tracked child of this item. Returns what was signalled.

    A row whose pid no longer matches its recorded starttime and boot id is stale
    (the child exited, or the pid was reused) and is only forgotten, never signalled.
    """
    conn = ledger.connect()
    halted = []
    for row in conn.execute("SELECT * FROM item_children WHERE item_id=?", (item_id,)).fetchall():
        same = (row["boot_id"] == procutil.boot_id() and row["starttime"] is not None
                and procutil.proc_starttime(row["pid"]) == row["starttime"])
        if same:
            signalled = procutil.kill_tree(row["pid"], grace_s=KILL_GRACE_S)
            halted.append({"pid": row["pid"], "argv0": row["argv0"], "signalled": signalled})
        conn.execute("DELETE FROM item_children WHERE item_id=? AND pid=?", (item_id, row["pid"]))
    return halted


# ---------------------------------------------------------------------------------------------------------
# Is the daemon that owns the worker new enough to honour the fence?


def _daemon_pid() -> Optional[int]:
    """Pid of the running waxd, from its singleton lock file, verified against /proc."""
    try:
        pid = int((paths.VAR / "waxd.singleton.lock").read_text().split()[0])
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError, IndexError):
        return None
    return pid if b"waxd" in cmdline else None


def _read_socket() -> Optional[dict[str, Any]]:
    """The live snapshot over waxd's status socket (a read, exactly what `wax status` does)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(PROBE_TIMEOUT_S)
            sock.connect(str(paths.SOCK))
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        doc = json.loads(b"".join(chunks))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def daemon_probe() -> dict[str, Any]:
    """Is a waxd running, and which capabilities does it advertise? Read-only.

    The socket is the live answer; state.json is the fallback when it does not answer. A snapshot
    that names a different pid than the running daemon is stale (an earlier daemon wrote it) and
    counts as no answer.
    """
    pid = _daemon_pid()
    if pid is None:
        return {"running": False, "pid": None, "capabilities": [], "source": None}
    for source, snapshot in (("socket", _read_socket()), ("state.json", sentinel.read_json(paths.STATE_JSON))):
        daemon = (snapshot or {}).get("daemon")
        if isinstance(daemon, dict) and daemon.get("pid") == pid:
            caps = daemon.get("capabilities")
            return {"running": True, "pid": pid, "source": source,
                    "capabilities": [str(c) for c in caps] if isinstance(caps, list) else []}
        if snapshot and source == "socket":
            # The live daemon answered and did not advertise: it is the old code. Do not fall back to a mirror.
            return {"running": True, "pid": pid, "source": source, "capabilities": []}
    return {"running": True, "pid": pid, "source": None, "capabilities": []}


def require_fence_support() -> dict[str, Any]:
    """Refuse to halt an item under a waxd that cannot honour the fence. Returns the probe on success.

    Why: a waxd from before the fence runs a worker that never reads `item_fences`, does not track its
    transcriber or pass children (so `halt_children` finds nothing to stop) and publishes whatever
    returns. Under it a split would raise a fence nothing obeys, wait out `--halt-timeout-s` while the
    item keeps transcribing, and either time out (nothing cut) or, if the item finishes in the window,
    cut a recording whose unsplit transcript has already been published. No daemon at all is fine: no
    worker exists to race. Live captures and items that need no halt never call this.
    """
    probe = daemon_probe()
    if probe["running"] and CAPABILITY not in probe["capabilities"]:
        raise DaemonTooOld(
            f"refusing: the running waxd (pid {probe['pid']}) predates the split fence (its status does not "
            f"advertise {CAPABILITY}), so it would not stop transcribing this item and could publish a "
            "transcript of the unsplit recording. Restart waxd first (`systemctl --user restart waxd`; a capture "
            "in progress is not touched, its encoder runs in its own scope), then run `wax split` again. "
            "Nothing was fenced, halted, cut or moved.")
    return probe


class DaemonTooOld(RuntimeError):
    """The running waxd does not advertise fence support; `split.SplitError` wraps it for the CLI."""
