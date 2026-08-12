"""The pipeline worker: drains the inbox, one item at a time.

Order is deliberate. **Archive before transcribe**, always. The audio is the
irreplaceable artifact and transcription is the slow, failure-prone part; if a
GPU job dies or the queue backs up, the recording must already be durable
somewhere other than this disk. The old pipeline had this right in principle
and wrong in practice — it "verified" the backup with `mc stat`, which is how a
262,144-byte stub stood in for a 16.5-hour recording.

Concurrency is 1. bin/transcribe takes ~/.cache/heyma-transcribe.lock itself
with a BLOCKING, un-timeouted flock, so waxd must never hold that file — a
parent holding it would deadlock its own child on every single job. We
serialise with our own in-process semaphore and let the script own its lock.

The claim file is what makes `inbox` read `ready-and-active` rather than
`error`: it carries the worker's pid/starttime/boot_id, so a claim whose owner
is dead is correctly seen as stranded work, not as activity.
"""

import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import archive, config, ledger, passes, paths, procutil, rename, sentinel, state, transcribe_adapter

POLL_S = 5.0
_SELECTION_LOCK = threading.Lock()


def _write_claim(item_id: str, path: Path, stage: str) -> None:
    me = os.getpid()
    doc = {
        "item": item_id,
        "path": str(path),
        "stage": stage,
        "claimed_epoch": time.time(),
        "owner_pid": me,
        "owner_boot_id": procutil.boot_id(),
        "owner_starttime": procutil.proc_starttime(me),
        "at": sentinel.utcnow(),
    }
    tmp = state.CLAIM_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True))
    os.replace(tmp, state.CLAIM_FILE)


def _clear_claim() -> None:
    state.CLAIM_FILE.unlink(missing_ok=True)


# States that still need pipeline work, and where each resumes.
WORK_STATES = ("pending", "archived", "transcribed")


def next_item() -> Optional[tuple[str, Path, str]]:
    """Oldest inbox file needing attention, and what to do with it.

    Returns (item_id, path, action) where action is "process" or "park".

    The `park` case matters more than it looks: a file whose CONTENT is already
    fully processed (a re-dropped copy, a restored backup) must still leave the
    inbox. Selecting only un-processed states leaves such a file sitting there
    forever, and the inbox reports `error` permanently — found exactly that way
    by re-dropping an already-transcribed recording.
    """
    conn = ledger.connect()
    for p in state.inbox_items():
        item_id = ledger.identify(p)
        if not item_id:
            continue
        row = conn.execute("SELECT state FROM items WHERE item_id=?", (item_id,)).fetchone()
        st = row["state"] if row else "pending"
        if st in WORK_STATES:
            return item_id, p, "process"
        if st == "complete":
            return item_id, p, "park"
        if st in ("suspect", "failed", "skipped"):
            # Poison items remain visible and preserved in the inbox until an
            # explicit operator action. Continue scanning so they never wedge
            # healthy work behind them.
            continue
    return None


def park_duplicate(item_id: str, path: Path) -> dict[str, Any]:
    """Move an already-processed copy out of the inbox. Never deletes."""
    dest_dir = paths.ARCHIVE / "duplicates"
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = rename.move_noclobber(path, dest_dir / path.name)
    ledger.connect().execute("UPDATE items SET path=?,updated_at=? WHERE item_id=?",
                             (str(moved), sentinel.utcnow(), item_id))
    ledger.set_item_state(item_id, "complete", cause="already_processed",
                          evidence=str(moved).replace(str(paths.AUDIO), "~/HeyMa"))
    return {"item_id": item_id, "parked_duplicate": str(moved)}


def process(item_id: str, path: Path) -> dict[str, Any]:
    """Archive, then transcribe, then park the audio. Never deletes anything."""
    result: dict[str, Any] = {"item_id": item_id, "path": str(path)}

    conn = ledger.connect()
    row = conn.execute("SELECT state FROM items WHERE item_id=?", (item_id,)).fetchone()
    start_state = row["state"] if row else "pending"

    # ---- 1. durability first -------------------------------------------
    if start_state == "pending":
        _write_claim(item_id, path, "archive")
    try:
        a = archive.archive(path, item_id=item_id)
        result["archive"] = a
        if start_state == "pending":
            ledger.set_item_state(item_id, "archived", cause="s3_verified",
                                  evidence=f"{a['s3_key']} ({a['bytes']} B)")
    except archive.ArchiveError as e:
        # Keep the source, stash a second local copy, and STOP. We do not
        # transcribe something we could not prove is backed up — but we also
        # never delete or move the original.
        stash = paths.RECOVERED / "unbacked"
        stash.mkdir(parents=True, exist_ok=True)
        dest = stash / path.name
        if not dest.exists():
            shutil.copy2(path, dest)
        ledger.set_item_state(item_id, "failed", cause="archive_failed", evidence=str(e)[:400])
        result["error"] = f"archive failed: {e}"
        return result

    # Durability and transcription policy are deliberately separate. Oversized
    # audio is archived first, then preserved outside the live queue without
    # ever starting Whisper. The adapter repeats this check so manual/direct
    # transcription calls cannot bypass the policy.
    size = path.stat().st_size
    if not config.transcription_size_allowed(size):
        limit = config.max_audio_file_size_for_transcription()
        dest_dir = paths.SKIPPED / "oversize"
        dest_dir.mkdir(parents=True, exist_ok=True)
        moved = rename.move_noclobber(path, dest_dir / path.name)
        conn.execute("UPDATE items SET path=?,updated_at=? WHERE item_id=?",
                     (str(moved), sentinel.utcnow(), item_id))
        ledger.set_item_state(
            item_id,
            "skipped",
            cause="file_too_large_for_transcription",
            evidence=f"{size} bytes >= {limit}; archived then moved to {moved}",
        )
        result["skipped"] = {
            "reason": "file_too_large_for_transcription",
            "bytes": size,
            "limit_bytes": limit,
            "path": str(moved),
        }
        return result

    # ---- 2. transcription (skipped if this item already has one) -------
    if start_state == "transcribed":
        result["transcribe"] = {"skipped": "already transcribed"}
    else:
        _write_claim(item_id, path, "transcribe")
        try:
            result["transcribe"] = transcribe_adapter.transcribe(path, item_id=item_id)
        except transcribe_adapter.TranscribeError as e:
            # The gate already moved a failed transcript aside and set the item
            # to `suspect`. The audio stays in the inbox so the failure stays
            # visible rather than being quietly filed away.
            if not str(e).startswith("SANITY GATE"):
                ledger.set_item_state(item_id, "failed", cause="transcribe_failed",
                                      evidence=str(e)[:400])
            result["error"] = str(e)[:400]
            return result

    # ---- 3. independent enrichment passes -----------------------------
    # The audio remains in the inbox until every enabled auto pass has had an
    # attempt. A failed EP is recorded independently and never blocks another
    # EP or prevents the verified audio from being parked safely.
    _write_claim(item_id, path, "enrich")
    result["enrichment"] = passes.run_auto(item_id)

    # ---- 4. park the audio, but only against a LIVE re-verify ----------
    key = result["archive"]["s3_key"]
    if archive.remote_size(key) != path.stat().st_size:
        ledger.set_item_state(item_id, "failed", cause="s3_reverify_failed",
                              evidence=f"{key} no longer matches local size")
        result["error"] = "S3 re-verify failed; audio left in inbox"
        return result

    day = datetime.fromtimestamp(path.stat().st_mtime)
    dest_dir = paths.ARCHIVE / f"{day:%Y}" / f"{day:%m}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = rename.move_noclobber(path, dest_dir / path.name)
    ledger.connect().execute("UPDATE items SET path=?, updated_at=? WHERE item_id=?",
                             (str(moved), sentinel.utcnow(), item_id))
    ledger.set_item_state(item_id, "complete", cause="parked",
                          evidence=str(moved).replace(str(paths.AUDIO), "~/HeyMa"))
    result["parked"] = str(moved)
    return result


def run_once() -> Optional[dict[str, Any]]:
    # Selection and operator skip share a short lock. The expensive work runs
    # outside it; the durable claim closes the race before the lock is released.
    with _SELECTION_LOCK:
        nxt = next_item()
        if nxt is None:
            return None
        item_id, path, action = nxt
        _write_claim(item_id, path, "park" if action == "park" else "claimed")
    try:
        if action == "park":
            return park_duplicate(item_id, path)
        return process(item_id, path)
    finally:
        _clear_claim()


def skip_item(item_id: str) -> dict[str, Any]:
    """Preserve a queued file outside the inbox and exclude it from processing."""
    with _SELECTION_LOCK:
        claim = state._active_claim()
        if claim and claim.get("item") == item_id:
            raise RuntimeError("the active item cannot be skipped")
        conn = ledger.connect()
        row = conn.execute(
            "SELECT path,state,orig_name FROM items WHERE item_id=?", (item_id,)
        ).fetchone()
        if not row:
            raise RuntimeError(f"unknown queue item: {item_id}")
        if row["state"] not in WORK_STATES:
            raise RuntimeError(f"item is not queued: {row['state']}")
        source = Path(row["path"])
        try:
            source.resolve().relative_to(paths.INBOX.resolve())
        except (OSError, ValueError) as e:
            raise RuntimeError("queued item is not in the inbox") from e
        if not source.is_file():
            raise RuntimeError("queued audio is missing")
        paths.SKIPPED.mkdir(parents=True, exist_ok=True)
        moved = rename.move_noclobber(source, paths.SKIPPED / source.name)
        conn.execute("UPDATE items SET path=?,updated_at=? WHERE item_id=?",
                     (str(moved), sentinel.utcnow(), item_id))
        ledger.set_item_state(item_id, "skipped", cause="operator_skip",
                              evidence=str(moved).replace(str(paths.AUDIO), "~/HeyMa"))
        return {"item_id": item_id, "state": "skipped", "path": str(moved)}


class Worker(threading.Thread):
    """Background drain loop. Only runs while `wax pipeline enable` is set."""

    def __init__(self):
        super().__init__(daemon=True, name="wax-worker")
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                if state.pipeline_enabled():
                    if run_once() is None:
                        self._stop.wait(POLL_S)
                    continue
            except Exception as e:  # noqa: BLE001 - a worker crash must not kill waxd
                ledger.record_transition("inbox", None, None, "error", "worker_exception", str(e)[:400])
                _clear_claim()
            self._stop.wait(POLL_S)
