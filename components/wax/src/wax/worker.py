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

Two things moved here from elsewhere, for the same reason. The end-of-item chime
used to fire inside transcribe_adapter.transcribe(), i.e. before enrichment, so
it announced "complete" for five days of items whose title-slug pass then 404'd.
The archive<->transcript link used to hang off that same pass returning a slug,
so the outage silently switched off the sidecar projection and the S3 tags too.
Both now key off what the item ACTUALLY did, at the only point where that is
known: the end.
"""

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import (archive, config, desktop, documents, dropoff, finalize, ledger, passes, paths, procutil, rename, sanity,
               sentinel, state, transcribe_adapter)

# Contract B: name only, handler configured exactly once in bin/waxd. Before
# this existed there was no `import logging` anywhere under src/wax, and 22 h of
# uptime across ~46 items produced a journal holding systemd's "Started" line
# and a libayatana deprecation warning — nothing else. Two consecutive
# 100%-failure outages hid there for a week each.
log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

POLL_S = 5.0
# The scheduled sweep interval. `wax ep sweep` used to be the ONLY re-drive for
# a failed pass, and it was manual — a dead provider meant items parked in
# enrichment_pending until somebody happened to type it. The worker now sweeps
# on a timer so transient failure is transient by default.
SWEEP_INTERVAL_S = float(os.environ.get("WAX_SWEEP_INTERVAL_S", "900"))
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


def _first_line(text: str, limit: int = 200) -> str:
    """Head of a failure detail: enough to diagnose, short enough for one line."""
    lines = (text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def _announce_item_failure(subject: str, headline: str, detail: str) -> None:
    """Make ONE item's failure audible at the desk, not just true in the database.

    Uses desktop.notify rather than desktop.notify_stage_failure on purpose: a
    stage failure is a dead provider repeating identically on every item and is
    worth exactly one notification, whereas an item that could not be archived
    or transcribed is a specific recording at risk and each one is news.
    """
    desktop.ding("failed")
    desktop.notify(f"Wax: {headline}", f"{subject}\n{_first_line(detail, 300)}")


def _set_state(item_id: str, to_state: str, *, cause: str, evidence: str,
               subject: Optional[str] = None) -> None:
    """ledger.set_item_state, but never silently.

    Every state change this module makes goes through here, so "the worker moved
    an item to failed" cannot happen without a journal line and a chime. The old
    call sites wrote the transition into SQLite and nothing else — which is how
    both outages ran at 100% failure behind a green tray.
    """
    if to_state in ("failed", "suspect"):
        log.error("item %s -> %s (%s): %s", item_id, to_state, cause, _first_line(evidence, 300))
        _announce_item_failure(subject or item_id, f"item {to_state} ({cause})", evidence)
    else:
        log.info("item %s -> %s (%s): %s", item_id, to_state, cause, _first_line(evidence, 200))
    ledger.set_item_state(item_id, to_state, cause=cause, evidence=evidence)


def _diarization_degraded(item_id: str, transcribe_result: dict[str, Any]) -> Optional[bool]:
    """Did we ask for speaker turns and get none? None means "cannot tell"."""
    if "diarization_degraded" in transcribe_result:
        return bool(transcribe_result["diarization_degraded"])
    # transcribe_adapter does not return the flag today, so fall back to the one
    # column the ledger actually has: transcripts.diarized, 0/1.
    row = ledger.connect().execute(
        "SELECT diarized FROM transcripts WHERE item_id=?", (item_id,)).fetchone()
    if row is None or "diarized" not in row.keys():
        return None
    return not row["diarized"]


def _report_diarization(item_id: str, name: str, transcribe_result: dict[str, Any]) -> None:
    """Say out loud when the speaker track came back empty.

    Diarization degrades by returning [] instead of failing, so the item reaches
    `transcribed` either way and every human surface reads clean. It produced
    zero turns on every recording for a week after commit 1d21e8b deleted the
    vendored whisperlivekit tree, and nothing anywhere said so.

    It is a STAGE, not an item: a missing backend fails identically on every
    recording, so the desktop alarm dedups and a recovery re-arms it.
    """
    mode = transcribe_adapter.diarization_mode()
    if mode == "disabled":
        return
    degraded = _diarization_degraded(item_id, transcribe_result)
    if degraded is None:
        return
    if not degraded:
        desktop.clear_stage_failure("diarization")
        return
    log.warning("diarization requested but absent for %s (item %s, WAX_DIARIZATION=%s)",
                name, item_id, mode)
    desktop.notify_stage_failure("diarization", "no_speaker_turns", item=name,
                                 detail=f"WAX_DIARIZATION mode={mode}")


def _transcript_path(item_id: str) -> Optional[Path]:
    row = ledger.connect().execute(
        "SELECT md_path FROM transcripts WHERE item_id=?", (item_id,)).fetchone()
    if row is None or "md_path" not in row.keys() or not row["md_path"]:
        return None
    return Path(row["md_path"])


def _link_archive(item_id: str) -> Optional[dict[str, Any]]:
    """Project the transcript back onto its archived audio.

    Gated on EVIDENCE — a transcript row for a backed-up item — rather than on
    an enrichment pass having returned a slug. The old gate lived in passes.py
    (`if refs and (requested_slug or result.get("link_audio"))`), so the
    title-slug outage silently switched off the sidecar projection and the S3
    tags for every recording it touched. That is the half of the archive design
    that makes "which audio has no transcript?" answerable from S3 alone, and it
    must not depend on an LLM being reachable.

    Called after run_auto so a pass that renamed the note is reflected.
    """
    md = _transcript_path(item_id)
    if md is None or not md.is_file():
        return None
    if not archive.is_backed_up(item_id):
        return None
    try:
        linked = archive.link_transcript(item_id, md)
    except (archive.ArchiveError, OSError) as exc:
        # Linkage projects facts that are already durable elsewhere. It has to be
        # loud, and it must never hold verified audio in the inbox.
        log.warning("archive link failed for item %s (%s): %s",
                    item_id, md.name, _first_line(str(exc), 300))
        return {"item_id": item_id, "error": str(exc)[:400]}
    log.info("linked %s to %d archived object(s)%s", md.name, len(linked["s3_keys"]),
             "" if linked["transcript"]["slugged"] else " (un-slugged; note stem used)")
    return linked


def _log_enrichment(item_id: str, name: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One WARNING per non-completed pass. Returns the failures.

    This is the line that did not exist. On 2026-08-15, the first time
    title-slug got a 404 for the deleted qwen3.6:latest model, the journal would
    have carried `WARNING wax.worker: enrichment pass title-slug failed ...
    reason_code=missing_model` on the very first item — instead of staying empty
    for five days while every run failed behind a green tray and a `wax status`
    that printed "no errors".
    """
    failed = [entry for entry in results if entry.get("state") not in ("completed", "skipped")]
    for entry in failed:
        # Every field here is one passes.run() actually returns. `attempt` is
        # not in that return shape, and a permanent "attempt ?" is noise wearing
        # the costume of information.
        log.warning("enrichment pass %s %s for %s (item %s): reason_code=%s %s",
                    entry.get("ep_slug", "?"), entry.get("state", "?"), name, item_id,
                    entry.get("reason_code") or "unknown",
                    _first_line(str(entry.get("error") or ""), 300))
    return failed


def _announce_done(name: str, results: list[dict[str, Any]]) -> None:
    """Chime ONCE, at the end of the item, about the outcome it actually had.

    The completion chime used to fire inside transcribe_adapter.transcribe() —
    before a single enrichment pass had run — so for five days it announced
    "complete" on items whose title-slug pass was about to 404. A completion
    sound that can be wrong about completion is worse than no sound at all.

    A pass that came back clean re-arms the desktop alarm for its slug, so a
    provider that breaks, is fixed, and breaks again is not silent the second
    time.
    """
    failed = [entry for entry in results if entry.get("state") not in ("completed", "skipped")]
    for entry in results:
        # A skip entry ("already completed at this version") means the pass did
        # not run at all, so it is evidence of nothing about the provider. Only
        # a fresh success re-arms the alarm.
        if entry.get("state") == "completed" and not entry.get("skipped"):
            desktop.clear_stage_failure(str(entry.get("ep_slug") or ""))
    if not failed:
        desktop.ding("complete")
        return
    desktop.ding("failed")
    for entry in failed:
        desktop.notify_stage_failure(
            str(entry.get("ep_slug") or "?"), str(entry.get("reason_code") or "unknown"),
            item=name, detail=_first_line(str(entry.get("error") or ""), 300))


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
    # items.path is the CANONICAL audio location (the archived original). The
    # duplicate copy is evidence, never the item's identity: rewriting path here
    # once pointed a finished item at archive/duplicates/.
    row = ledger.connect().execute(
        "SELECT md_path FROM transcripts WHERE item_id=?", (item_id,)).fetchone()
    md_path = row["md_path"] if row and row["md_path"] else None
    note = Path(md_path).name if md_path else None
    dup = str(moved).replace(str(paths.AUDIO), "~/HeyMa")
    evidence = f"duplicate={dup}" + (f" transcript={md_path}" if md_path else " transcript=none")
    log.info("item %s re-dropped as %s -> parked %s (already_processed)%s", item_id, path.name,
             dup, f"; transcript {note}" if note else "")
    # complete->complete is a no-op for set_item_state (same-state transitions
    # are swallowed), so the re-drop gets its own durable transitions row.
    try:
        ledger.record_transition("item", item_id, "complete", "complete",
                                 "already_processed", evidence)
    except Exception as e:  # noqa: BLE001 - the audio is already safely parked
        log.warning("item %s: could not record already_processed transition: %s", item_id, e)
    try:
        desktop.notify("Wax: already transcribed",
                       f"Already transcribed → {note or 'transcript not recorded'}\n{path.name}",
                       urgency="normal")
    except Exception as e:  # noqa: BLE001 - notification is best effort
        log.warning("item %s: already_processed notification failed: %s", item_id, e)
    return {"item_id": item_id, "parked_duplicate": str(moved), "transcript": md_path}


@passes.serialized
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
            _set_state(item_id, "archived", cause="s3_verified",
                       evidence=f"{a['s3_key']} ({a['bytes']} B, "
                                f"{a.get('verified_by', 'size')})", subject=path.name)
    except archive.ArchiveError as e:
        # Keep the source, stash a second local copy, and STOP. We do not
        # transcribe something we could not prove is backed up — but we also
        # never delete or move the original.
        stash = paths.RECOVERED / "unbacked"
        stash.mkdir(parents=True, exist_ok=True)
        dest = stash / path.name
        if not dest.exists():
            shutil.copy2(path, dest)
        _set_state(item_id, "failed", cause="archive_failed", evidence=str(e)[:400],
                   subject=path.name)
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
        _set_state(
            item_id,
            "skipped",
            cause="file_too_large_for_transcription",
            evidence=f"{size} bytes >= {limit}; archived then moved to {moved}",
            subject=path.name,
        )
        result["skipped"] = {
            "reason": "file_too_large_for_transcription",
            "bytes": size,
            "limit_bytes": limit,
            "path": str(moved),
        }
        return result

    # File size is not a proxy for compute. At 28.7 kbps, a 13.9-hour Ogg is
    # only 171 MiB and legitimately passes a 300 MB byte ceiling. Probe the
    # container and enforce an independent wall-clock duration ceiling before
    # Whisper allocates a model or takes the single transcription slot.
    duration_s = sanity.probe_duration(path)
    if duration_s is not None:
        conn.execute(
            "UPDATE items SET duration_s=?,updated_at=? WHERE item_id=?",
            (duration_s, sentinel.utcnow(), item_id),
        )
    if duration_s is not None and not config.transcription_duration_allowed(duration_s):
        limit_s = config.max_audio_duration_for_transcription()
        dest_dir = paths.SKIPPED / "overduration"
        dest_dir.mkdir(parents=True, exist_ok=True)
        moved = rename.move_noclobber(path, dest_dir / path.name)
        conn.execute("UPDATE items SET path=?,updated_at=? WHERE item_id=?",
                     (str(moved), sentinel.utcnow(), item_id))
        _set_state(
            item_id,
            "skipped",
            cause="audio_too_long_for_transcription",
            evidence=f"{duration_s:.3f}s >= {limit_s:.3f}s; archived then moved to {moved}",
            subject=path.name,
        )
        result["skipped"] = {
            "reason": "audio_too_long_for_transcription",
            "duration_s": duration_s,
            "limit_s": limit_s,
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
            if str(e).startswith("SANITY GATE"):
                # The `suspect` transition happened inside transcribe_adapter, so
                # _set_state never sees it. Log and chime it here or it is silent.
                log.error("item %s quarantined as suspect: %s", item_id, _first_line(str(e), 300))
                _announce_item_failure(path.name, "transcript failed the sanity gate", str(e))
            else:
                _set_state(item_id, "failed", cause="transcribe_failed",
                           evidence=str(e)[:400], subject=path.name)
            result["error"] = str(e)[:400]
            return result

    _report_diarization(item_id, path.name, result["transcribe"] or {})

    # ---- 3. independent enrichment passes -----------------------------
    # The audio remains in the inbox until every enabled auto pass has had an
    # attempt. A failed EP is recorded independently and never blocks another
    # EP or prevents the verified audio from being parked safely.
    _write_claim(item_id, path, "enrich")
    result["enrichment"] = passes.run_auto(item_id)
    _log_enrichment(item_id, path.name, result["enrichment"])

    # ---- 3b. publish what is ready ------------------------------------
    # Pass events wait only for their own requires-subgraph, not the whole
    # plan: a wedged sibling must not withhold events that are done.
    try:
        published = finalize.publish_pass_events(item_id)
        if published.get("pass_events"):
            result["pass_events"] = published
    except Exception as exc:  # noqa: BLE001 - publication must not strand the item
        log.warning("pass event publication failed for %s: %s: %s",
                    item_id, type(exc).__name__, exc)

    # ---- 4. link the archived audio to its transcript ------------------
    # Deliberately AFTER the passes (so a rename is picked up) and deliberately
    # NOT conditional on any of them having succeeded.
    link = _link_archive(item_id)
    if link is not None:
        result["archive_link"] = link

    # ---- 5. park the audio, but only against a LIVE re-verify ----------
    key = result["archive"]["s3_key"]
    if archive.remote_size(key) != path.stat().st_size:
        _set_state(item_id, "failed", cause="s3_reverify_failed",
                   evidence=f"{key} no longer matches local size", subject=path.name)
        result["error"] = "S3 re-verify failed; audio left in inbox"
        return result

    day = datetime.fromtimestamp(path.stat().st_mtime)
    dest_dir = paths.ARCHIVE / f"{day:%Y}" / f"{day:%m}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = rename.move_noclobber(path, dest_dir / path.name)
    ledger.connect().execute("UPDATE items SET path=?, updated_at=? WHERE item_id=?",
                             (str(moved), sentinel.utcnow(), item_id))
    result["parked"] = str(moved)
    result["completion"] = finalize.finalize(item_id)
    if not result["completion"].get("finalized"):
        _set_state(item_id, "enrichment_pending", cause="completion_withheld",
                   evidence=str(result["completion"]), subject=path.name)
    _announce_done(path.name, result["enrichment"])
    return result


def run_once() -> Optional[dict[str, Any]]:
    dropoff.copy_received()
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
        _set_state(item_id, "skipped", cause="operator_skip",
                   evidence=str(moved).replace(str(paths.AUDIO), "~/HeyMa"),
                   subject=source.name)
        return {"item_id": item_id, "state": "skipped", "path": str(moved)}


def retry_item(item_id: str) -> dict[str, Any]:
    """Explicitly requeue one preserved failed or suspect inbox item.

    Failed items are poison-proof by design: next_item() skips them so healthy
    work can continue. That also means a repaired dependency cannot make an
    item retry itself. The operator action is intentionally narrow: only a
    failed file that still exists inside the live inbox can return to pending.
    The ordinary worker then starts at archive again, whose content-addressed
    upload and live verification are idempotent.
    """
    with _SELECTION_LOCK:
        claim = state._active_claim()
        if claim and claim.get("item") == item_id:
            raise RuntimeError("the active item cannot be retried")
        conn = ledger.connect()
        row = conn.execute(
            "SELECT path,state,orig_name FROM items WHERE item_id=?", (item_id,)
        ).fetchone()
        if not row:
            raise RuntimeError(f"unknown queue item: {item_id}")
        if row["state"] not in ("failed", "suspect"):
            raise RuntimeError(f"item is not failed or suspect: {row['state']}")
        source = Path(row["path"])
        try:
            source.resolve().relative_to(paths.INBOX.resolve())
        except (OSError, ValueError) as exc:
            raise RuntimeError("failed item is not in the inbox") from exc
        if not source.is_file():
            raise RuntimeError("failed audio is missing")
        previous = conn.execute(
            "SELECT cause_code FROM transitions WHERE machine='item' AND subject=? "
            "ORDER BY seq DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        previous_cause = previous["cause_code"] if previous else None
        _set_state(
            item_id,
            "pending",
            cause="operator_retry",
            evidence=f"previous_cause={previous_cause or 'unknown'} path={source}",
            subject=row["orig_name"] or source.name,
        )
        return {
            "item_id": item_id,
            "state": "pending",
            "path": str(source),
            "previous_cause": previous_cause,
        }


def retry_failed_items() -> list[dict[str, Any]]:
    """Explicitly requeue all preserved failed or suspect items in the inbox.

    Returns the list of requeued items. Items missing from disk, outside the inbox,
    or currently claimed are skipped.
    """
    conn = ledger.connect()
    rows = conn.execute(
        "SELECT item_id, path FROM items WHERE state IN ('failed', 'suspect') ORDER BY updated_at"
    ).fetchall()
    requeued = []
    for row in rows:
        item_id = row["item_id"]
        source = Path(row["path"])
        try:
            source.resolve().relative_to(paths.INBOX.resolve())
        except (OSError, ValueError):
            continue
        if not source.is_file():
            continue
        claim = state._active_claim()
        if claim and claim.get("item") == item_id:
            continue
        try:
            requeued.append(retry_item(item_id))
        except RuntimeError:
            continue
    return requeued


@passes.serialized
def retry_failed_passes(item_id: str, slugs: tuple[str, ...]) -> dict[str, Any]:
    """Retry the named failed enrichment passes for one completed item.

    Tray rows can aggregate more than one independent pass failure. A click
    retries exactly those passes, never every enabled pass and never failures
    belonging to other recordings.
    """
    requested = tuple(dict.fromkeys(str(slug) for slug in slugs if slug))
    if not requested:
        raise RuntimeError("no failed passes were selected")

    conn = ledger.connect()
    item = conn.execute(
        "SELECT orig_name FROM items WHERE item_id=?", (item_id,)
    ).fetchone()
    if not item:
        raise RuntimeError(f"unknown queue item: {item_id}")
    placeholders = ",".join("?" for _ in requested)
    failed = {
        row["ep_slug"] for row in conn.execute(
            f"SELECT ep_slug FROM passes WHERE item_id=? AND state='failed' "
            f"AND ep_slug IN ({placeholders})",
            (item_id, *requested),
        ).fetchall()
    }
    not_failed = [slug for slug in requested if slug not in failed]
    if not_failed:
        raise RuntimeError(f"pass is no longer failed: {', '.join(not_failed)}")

    results: list[dict[str, Any]] = []
    for slug in requested:
        try:
            results.append(passes.run(item_id, slug))
        except passes.PassError as exc:
            results.append({
                "item_id": item_id,
                "ep_slug": slug,
                "state": "failed",
                "error": str(exc),
                "reason_code": "run_error",
            })

    name = item["orig_name"] or item_id
    failures = _log_enrichment(item_id, name, results)
    _announce_done(name, results)
    out: dict[str, Any] = {
        "item_id": item_id,
        "retried": len(results),
        "completed": len(results) - len(failures),
        "failed": len(failures),
        "results": results,
    }
    # A successful title/slug retry can rename the note, so refresh the archive
    # projection just as the ordinary worker does after automatic passes.
    if any(result.get("state") == "completed" for result in results):
        linked = _link_archive(item_id)
        if linked is not None:
            out["archive_link"] = linked
    plan = conn.execute("SELECT 1 FROM processing_plans WHERE item_id=?", (item_id,)).fetchone()
    if plan:
        out["remaining"] = passes.run_auto(item_id)
    # Retried passes may hold events: publish whatever its subgraph allows,
    # whether or not the rest of the plan is done.
    out["pass_events"] = finalize.publish_pass_events(item_id)
    if plan:
        out["completion"] = finalize.finalize(item_id)
    return out


def sweep_failed_passes(*, max_attempts: int = passes.DEFAULT_MAX_ATTEMPTS,
                        slug: Optional[str] = None, limit: int = 0,
                        min_age_s: float = 0.0) -> dict[str, Any]:
    """Re-drive stranded enrichment passes, dead-letter the exhausted, finalize.

    Shared by the worker's scheduled sweep and `wax ep sweep` (which keeps the
    dry-run/unknown-slug presentation layer). Emits the exhaustion dead-letters
    FIRST so a pass at its bound announces even when nothing will re-run it.

    An item with a STORED plan is re-driven against the plan's frozen
    definitions and dependency versions — the live registry moved on is drift
    to report, not a new graph to run mid-item. Plan-less items (legacy
    history) keep the live-registry path.

    A sweep spends at most ONE attempt per (item, slug). The follow-up
    `run_auto` that picks up un-run dependents used to re-run every failed
    pass again straight after the target loop had, so one sweep consumed two
    attempts and a 3-attempt budget burned in seconds. `min_age_s` is the
    minimum spacing since a pass's last attempt (the scheduled sweep passes
    its interval; passes younger than that are neither targets nor re-run by
    the follow-up walk).
    """
    def _stored_plan(item_id: str) -> dict[str, dict[str, Any]]:
        row = ledger.connect().execute(
            "SELECT definitions FROM processing_plans WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            return {}
        try:
            plan = json.loads(row["definitions"])
        except (TypeError, ValueError):
            return {}
        return plan if isinstance(plan, dict) else {}

    fired = passes.sweep_exhaustions(max_attempts)
    targets = ledger.failed_passes_for_sweep(max_attempts, min_age_s)
    if slug:
        targets = [t for t in targets if t[1] == slug]
    if limit > 0:
        targets = targets[:limit]
    out: dict[str, Any] = {
        "max_attempts": max_attempts, "slug": slug,
        "exhausted": fired,
        "candidates": len(targets),
        "targets": [{"item_id": i, "ep_slug": s, "attempt": a} for i, s, a in targets],
    }
    if not targets:
        return out
    plans: dict[str, dict[str, dict[str, Any]]] = {}
    results = []
    # Per item, slugs the follow-up run_auto must NOT re-run: a real (non-gate)
    # failure from the target loop has spent this tick's attempt.
    held: dict[str, set[str]] = {}
    for item_id, ep_slug, attempt in targets:
        try:
            # The attempt from the ledger is already incremented (its contract),
            # so it is handed straight through: re-using the recorded attempt
            # would replay a spent uuid5 command_id / idempotency key.
            plan = plans.setdefault(item_id, _stored_plan(item_id))
            frozen = plan.get(ep_slug)
            dependency_versions = (
                {s: int(p.get("version") or 1) for s, p in plan.items()}
                if frozen is not None else None)
            results.append(passes.run(item_id, ep_slug, attempt=attempt,
                                      definition=frozen,
                                      dependency_versions=dependency_versions))
        except passes.PassError as e:
            results.append({"item_id": item_id, "ep_slug": ep_slug, "state": "failed",
                            "error": str(e), "reason_code": "run_error"})
        last = results[-1]
        if last.get("state") == "failed" and last.get("reason_code") != "dependency_failed":
            held.setdefault(item_id, set()).add(ep_slug)
    # A failed row younger than the spacing was never eligible; the follow-up
    # walk must not run it either.
    cutoff = ledger._age_cutoff(min_age_s) if min_age_s > 0 else None
    publications: dict[str, Any] = {}
    completions: dict[str, Any] = {}
    for item_id in dict.fromkeys(item for item, _, _ in targets):
        plan = ledger.connect().execute(
            "SELECT 1 FROM processing_plans WHERE item_id=?", (item_id,)).fetchone()
        if plan:
            hold = set(held.get(item_id, ()))
            if cutoff is not None:
                hold.update(row["ep_slug"] for row in ledger.connect().execute(
                    "SELECT ep_slug FROM passes WHERE item_id=? AND state='failed' "
                    "AND updated_at > ?", (item_id, cutoff)).fetchall())
            passes.run_auto(item_id, hold=frozenset(hold))
        publications[item_id] = finalize.publish_pass_events(item_id)
        completions[item_id] = finalize.finalize(item_id)
    out["publications"] = publications
    out["completions"] = completions
    out["results"] = results
    out["completed"] = sum(1 for r in results if r.get("state") == "completed")
    out["failed"] = sum(1 for r in results if r.get("state") != "completed")
    return out


def _iso_epoch(epoch: float) -> str:
    from datetime import datetime, timezone
    return (datetime.fromtimestamp(epoch, timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


class Worker(threading.Thread):
    """Background drain loop. Only runs while `wax pipeline enable` is set.

    Liveness is proven by a dedicated heartbeat THREAD, not by the main loop's
    tick: a tick-stamped file stops being evidence the moment one item holds
    the loop for the length of a transcription, and an enabled branch that
    `continue`d past the heartbeat write meant the busiest worker was exactly
    the one whose heartbeat went stale. The thread beats every few seconds
    while the Worker object is alive — mid-run included — and its death (with
    the worker's) is what the catalog reads as "down".
    """

    HEARTBEAT_INTERVAL_S = 5.0

    def __init__(self):
        super().__init__(daemon=True, name="wax-worker")
        self._stop = threading.Event()
        # 0.0, not now: the first tick sweeps, because a failure that stranded
        # while waxd was down is exactly the failure the sweep exists for.
        self._last_sweep = 0.0
        # Same for document bindings: a file that arrived while waxd was down
        # deserves the first tick's scan, not a wait.
        self._last_doc_scan = 0.0
        self._tick = 0
        self._busy: Optional[str] = None
        self._busy_since_monotonic: Optional[float] = None
        self._state_lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()

    # -- shared liveness state (main loop writes, heartbeat thread reads) ----

    def _set_busy(self, what: Optional[str]) -> None:
        with self._state_lock:
            self._busy = what
            self._busy_since_monotonic = time.monotonic() if what else None

    def _liveness_snapshot(self) -> tuple[int, Optional[str], Optional[str]]:
        with self._state_lock:
            busy_since = None
            if self._busy and self._busy_since_monotonic is not None:
                busy_since = _iso_epoch(
                    time.time() - (time.monotonic() - self._busy_since_monotonic))
            return self._tick, self._busy, busy_since

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.HEARTBEAT_INTERVAL_S):
            tick, busy, busy_since = self._liveness_snapshot()
            try:
                documents.write_worker_heartbeat(
                    tick=tick, busy=busy, busy_since=busy_since)
            except Exception as exc:  # noqa: BLE001 - the beat must never kill waxd
                log.warning("worker heartbeat write failed: %s: %s",
                            type(exc).__name__, exc)

    def _scan_documents(self) -> None:
        """Bounded, worker-owned document scan: new arrivals + retryable failures.

        No overlapping runs by construction (single worker thread) plus the
        per-document flock, which also guards a concurrent CLI scan.
        """
        scanned = documents.scan()
        if scanned.get("processed") or scanned.get("refused"):
            log.info("document scan: %d processed, %d binding(s) refused",
                     scanned.get("processed", 0), len(scanned.get("refused") or []))

    def _tick_once(self) -> bool:
        """One loop iteration. Returns whether work happened (drain fast)."""
        self._tick += 1
        if not state.pipeline_enabled():
            self._set_busy(None)
            return False
        now = time.monotonic()
        if now - self._last_sweep >= SWEEP_INTERVAL_S:
            self._last_sweep = now
            self._set_busy("sweep")
            try:
                swept = sweep_failed_passes(min_age_s=SWEEP_INTERVAL_S)
                if swept.get("candidates") or swept.get("exhausted"):
                    log.info("scheduled sweep: %s candidate(s), %d exhaustion notice(s)",
                             swept["candidates"], len(swept.get("exhausted") or []))
            finally:
                self._set_busy(None)
        if now - self._last_doc_scan >= documents.SCAN_INTERVAL_S:
            self._last_doc_scan = now
            self._set_busy("documents")
            try:
                self._scan_documents()
            finally:
                self._set_busy(None)
        self._set_busy("inbox")
        try:
            return run_once() is not None
        finally:
            self._set_busy(None)

    def run(self) -> None:
        heartbeat = threading.Thread(target=self._heartbeat_loop,
                                     name="wax-worker-heartbeat", daemon=True)
        heartbeat.start()
        while not self._stop.is_set():
            try:
                worked = self._tick_once()
            except Exception as e:  # noqa: BLE001 - a worker crash must not kill waxd
                # A tick exception used to go into a snapshot field and nowhere
                # else. exc_info is the whole point: the traceback is the only
                # thing that makes an unexpected crash diagnosable after the fact.
                self._set_busy(None)
                log.exception("worker tick failed: %s", e)
                ledger.record_transition("inbox", None, None, "error", "worker_exception", str(e)[:400])
                _clear_claim()
                worked = False
            if not worked:
                self._stop.wait(POLL_S)
