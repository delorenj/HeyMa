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

import os
import subprocess
from typing import Any, Optional, Sequence

from . import ledger, procutil, sentinel
from .ledger import Fenced  # noqa: F401  (re-exported: callers catch fence.Fenced)

KILL_GRACE_S = 5.0


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
