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

from . import (archive, config, desktop, documents, dropoff, fence, finalize, ledger, passes, paths, procutil,
               rename, sanity, sentinel, state, transcribe_adapter)
from . import junk

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


def _next_step(item_id: Optional[str], cause: Optional[str] = None) -> str:
    """The operator verbs for a failed item, for every notification and evidence line."""
    return ledger.next_step(item_id, cause)


def _announce_item_failure(subject: str, headline: str, detail: str,
                           item_id: Optional[str] = None, cause: Optional[str] = None) -> None:
    """Make ONE item's failure audible at the desk, not just true in the database.

    Uses desktop.notify rather than desktop.notify_stage_failure on purpose: a
    stage failure is a dead provider repeating identically on every item and is
    worth exactly one notification, whereas an item that could not be archived
    or transcribed is a specific recording at risk and each one is news. The body
    ends with the next step, so the notification is actionable on its own.
    """
    desktop.ding("failed")
    desktop.notify(f"Wax: {headline}",
                   f"{subject}\n{_first_line(detail, 300)}\nnext: {_next_step(item_id, cause)}")


def _announce_silent(item_id: str, name: str, source: str, rejection: "junk.Rejection",
                     levels: Optional[dict[str, Any]], duration_s: Optional[float]) -> None:
    """Tell the operator once per source that a recording was silent: a mic problem, not a Wax fault."""
    try:
        body = (f"{name}, {sanity.format_duration(duration_s)}, "
                + (f"mean {levels['mean_db']:.1f} dB / peak {levels['max_db']:.1f} dB "
                   f"(speech is about {junk.SPEECH_REFERENCE_DB} dB). " if levels else "")
                + "Check the input (mic or recording app). Parked in skipped/silent/; "
                  f"wax retry {item_id} forces transcription.")
        desktop.notify_silent_recording(source, body)
    except Exception as exc:  # noqa: BLE001 - notification is best effort
        log.warning("item %s: silent-recording notification failed: %s", item_id, exc)


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
        _announce_item_failure(subject or item_id, f"item {to_state} ({cause})", evidence, item_id, cause)
        if "\nnext:" not in evidence:
            # Second line, so every row that shows only the first line stays short.
            evidence = f"{evidence}\nnext: {_next_step(item_id, cause)}"
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

    Returns (item_id, path, action) where action is "process", "park" or "park-junk".

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
        if ledger.fence_of(item_id) is not None:
            # `wax split` has this one; it must not be picked up (or starve the queue).
            continue
        if st in WORK_STATES:
            return item_id, p, "process"
        if st == "complete":
            return item_id, p, "park"
        if st == "skipped" and _duplicate_call_redrop(conn, item_id, p):
            return item_id, p, "park-twin-redrop"
        if st == "skipped" and _junk_redrop(conn, item_id, p):
            return item_id, p, "park-junk"
        if st in ("suspect", "failed", "skipped"):
            # Poison items remain visible and preserved in the inbox until an
            # explicit operator action. Continue scanning so they never wedge
            # healthy work behind them.
            continue
    return None


def _duplicate_call_redrop(conn, item_id: str, path: Path) -> bool:
    row = conn.execute("SELECT path,duplicate_of_call_session_id FROM items WHERE item_id=?", (item_id,)).fetchone()
    if not row or not row["duplicate_of_call_session_id"] or Path(row["path"]) == path:
        return False
    last = conn.execute("SELECT cause_code FROM transitions WHERE machine='item' AND subject=? "
                        "ORDER BY seq DESC LIMIT 1", (item_id,)).fetchone()
    return bool(last) and last["cause_code"] in ("duplicate_call", "duplicate_call_redropped")

def park_twin_redrop(item_id: str, path: Path) -> dict[str, Any]:
    """Preserve a repeat delivery without replacing its canonical parked audio."""
    dest_dir = paths.SKIPPED / "duplicate-call" / "duplicates"
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = rename.move_noclobber(path, dest_dir / path.name)
    try:
        ledger.record_transition("item", item_id, "skipped", "skipped", "duplicate_call_redropped",
                                 f"duplicate={moved}")
    except Exception as exc:  # noqa: BLE001 - the extra copy is already parked
        log.warning("could not journal twin re-drop of %s: %s", item_id, exc)
    return {"item_id": item_id, "parked": str(moved), "reason": "duplicate_call_redropped"}


def _junk_cause(conn, item_id: str) -> Optional[str]:
    """The item's newest transition cause if it is a junk-gate rejection (junk_*), else None."""
    last = conn.execute(
        "SELECT cause_code FROM transitions WHERE machine='item' AND subject=? "
        "ORDER BY seq DESC LIMIT 1", (item_id,)).fetchone()
    if not last or not junk.is_junk_cause(last["cause_code"]):
        return None
    if last["cause_code"] != "junk_redropped":
        return last["cause_code"]
    # A re-dropped copy was parked: the item is still what it was (junk_silent stays silent).
    row = conn.execute(
        "SELECT cause_code FROM transitions WHERE machine='item' AND subject=? "
        "AND cause_code LIKE 'junk\\_%' ESCAPE '\\' AND cause_code NOT IN ('junk_redropped', ?) "
        "ORDER BY seq DESC LIMIT 1", (item_id, junk.OVERRIDE_CAUSE)).fetchone()
    return row["cause_code"] if row else last["cause_code"]


def _junk_redrop(conn, item_id: str, path: Path) -> bool:
    """A copy of audio the junk gate already rejected has landed in the inbox again."""
    row = conn.execute("SELECT path FROM items WHERE item_id=?", (item_id,)).fetchone()
    if not row or Path(row["path"]) == path:
        return False
    return _junk_cause(conn, item_id) is not None


def park_junk_redrop(item_id: str, path: Path) -> dict[str, Any]:
    """Move a re-dropped copy of rejected junk beside the original. Never deletes."""
    cause = _junk_cause(ledger.connect(), item_id)
    reason = junk.SILENT_REASON if cause == junk.SILENT_CAUSE else None
    dest_dir = junk.reject_dir(reason) / "duplicates"
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = rename.move_noclobber(path, dest_dir / path.name)
    evidence = "duplicate=" + str(moved).replace(str(paths.AUDIO), "~/HeyMa")
    log.info("item %s re-dropped as %s after the junk gate rejected it -> parked %s",
             item_id, path.name, moved)
    try:
        ledger.record_transition("item", item_id, "skipped", "skipped", "junk_redropped", evidence)
    except Exception as e:  # noqa: BLE001 - the audio is already safely parked
        log.warning("could not journal junk re-drop of %s: %s", item_id, e)
    return {"item_id": item_id, "parked": str(moved), "reason": "junk_redropped"}


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
    """Archive, then transcribe, then park the audio. Never deletes anything.

    A `wax split` fence (docs/WAX-SPLIT.md) can halt the item between any two
    steps: every step re-checks it, and the ledger refuses state changes for a
    fenced item, so a late result is dropped here instead of being published.
    """
    try:
        return _process_item(item_id, path)
    except fence.Fenced as exc:
        log.warning("item %s halted by wax split: %s", item_id, exc)
        return {"item_id": item_id, "path": str(path), "fenced": str(exc)}


def _quiet_speech(item_id: str, path: Path, levels: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Is this level-silent RECORDING quiet speech? {gain_db, floor_db, loud_db, active_s, source} or None.

    None keeps today's verdict (true silence parks as junk_silent). Recordings only: a Telnyx
    call or a Groundwire recording (calls.audio_identity) is never re-levelled; its channels are
    judged by channels.py, and a dead leg is normal. Fails open to None on every error. Nothing
    is written or announced here: the gained copy exists only around the ASR run; the gain is
    recorded when that run starts and cleared if it fails or is rejected, and the operator is told
    only once it published a transcript (_quiet_speech_heard).
    sanity.speech_profile has the calibration story.
    """
    if not levels or not levels.get("silent") or not junk.enabled():
        return None
    try:
        row = ledger.connect().execute(
            "SELECT origin,orig_name FROM items WHERE item_id=?", (item_id,)).fetchone()
        source = ledger.source_of(row["origin"], row["orig_name"]) if row else "unknown"
        if source == "telnyx" or junk._call_facts(path.name):
            return None
        profile = sanity.speech_profile(path)
    except Exception as exc:  # noqa: BLE001 - the probe fails open
        log.warning("item %s: quiet-speech probe failed: %s: %s", item_id, type(exc).__name__, exc)
        return None
    if not profile or not profile["quiet_speech"]:
        if profile:
            log.info("item %s: silent and flat (floor %.1f dB, loud %.1f dB, %.1fs active): "
                     "no speech to lift", item_id, profile["floor_db"], profile["loud_db"],
                     profile["active_s"])
        return None
    log.warning("item %s: quiet input (mean %.1f dB, speech windows at %.1f dB over a %.1f dB floor, "
                "%.0fs of them): transcribing a +%.1f dB copy", item_id, levels["mean_db"],
                profile["loud_db"], profile["floor_db"], profile["active_s"], profile["gain_db"])
    return {**{k: profile[k] for k in ("gain_db", "floor_db", "loud_db", "active_s")}, "source": source}


def _record_gain(item_id: str, gain_db: Optional[float]) -> None:
    try:
        ledger.record_audio_gain(item_id, gain_db)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not stop a recording
        log.warning("item %s: could not record the applied gain: %s", item_id, exc)


def _quiet_speech_heard(item_id: str, name: str, quiet: dict[str, Any]) -> None:
    """A gained copy produced a published transcript: tell the operator, once per source."""
    try:
        desktop.notify_quiet_recording(quiet["source"], (
            f"{name}: the input was about {abs(round(quiet['loud_db'] - sanity.GAIN_TARGET_DB))} dB "
            f"too quiet (speech at {quiet['loud_db']:.0f} dB; normal is about {junk.SPEECH_REFERENCE_DB} dB). "
            f"Transcribed with +{quiet['gain_db']:.0f} dB of gain; raise the input level on that device."))
    except Exception as exc:  # noqa: BLE001 - notification is best effort
        log.warning("item %s: quiet-recording notification failed: %s", item_id, exc)


def _reject_junk(item_id: str, path: Path, rejection: "junk.Rejection",
                 stage: str, kept: Optional[Path] = None, *,
                 levels: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Park rejected audio (skipped/junk/, or skipped/silent/ for silence) and mark the item skipped.

    Never deletes. The audio is already archived to S3 (step 1). No vault note,
    transcripts row, store version or pass plan exists for it, so nothing
    downstream runs. One body for every rejection: the cause and the destination
    come from the Rejection, so `wax retry` (_release_junk) releases all of them.
    """
    dest_dir = junk.reject_dir(rejection.reason)
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = rename.move_noclobber(path, dest_dir / path.name)
    ledger.connect().execute("UPDATE items SET path=?,updated_at=? WHERE item_id=?",
                             (str(moved), sentinel.utcnow(), item_id))
    shown = str(moved).replace(str(paths.AUDIO), "~/HeyMa")
    silent = rejection.reason == junk.SILENT_REASON
    if silent:
        evidence = (f"{rejection.detail}; parked at {shown}; "
                    f"wax retry {item_id} forces transcription")
    else:
        words = "" if rejection.words is None else f" ({rejection.words} words)"
        evidence = (f"{stage}: {rejection.detail}{words}; archived then moved to {shown}"
                    + (f"; text kept at {kept}" if kept else ""))
    _set_state(item_id, "skipped", cause=rejection.cause, evidence=evidence, subject=path.name)
    if silent:
        # A gained copy that still held no words was silence after all: count it as silent.
        try:
            ledger.record_audio_gain(item_id, None)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not stop a recording
            log.warning("item %s: could not clear the applied gain: %s", item_id, exc)
        row = ledger.connect().execute(
            "SELECT origin,orig_name,duration_s FROM items WHERE item_id=?", (item_id,)).fetchone()
        source = ledger.source_of(row["origin"], row["orig_name"]) if row else "unknown"
        _announce_silent(item_id, path.name, source, rejection, levels, row["duration_s"] if row else None)
    return {"reason": rejection.cause, "stage": stage, "detail": rejection.detail,
            "words": rejection.words, "path": str(moved),
            **({"text": str(kept)} if kept else {}),
            **({"levels": levels} if levels else {})}


def _rearm_silent_alarm(item_id: str) -> None:
    """An audible recording from a source ends its silent run: the next silent one is news again."""
    try:
        row = ledger.connect().execute(
            "SELECT origin,orig_name FROM items WHERE item_id=?", (item_id,)).fetchone()
        if row:
            desktop.clear_silent_source(ledger.source_of(row["origin"], row["orig_name"]))
    except Exception as exc:  # noqa: BLE001 - best effort
        log.debug("could not re-arm silent alarm for %s: %s", item_id, exc)


def _published_note(conn, *, twin: dict[str, Any], exclude: Optional[str] = None) -> bool:
    """Suppress only when the exact matched primary (or persisted twin) has a note."""
    from . import calls
    session = twin["twin_of_session"]
    for row in conn.execute(
            "SELECT i.item_id,i.orig_name,i.duplicate_of_call_session_id,i.twin_of_recording_key,"
            "t.md_path,t.pruned_at FROM items i JOIN transcripts t ON t.item_id=i.item_id "
            "WHERE i.duplicate_of_call_session_id=? OR i.twin_of_recording_key=? OR i.orig_name LIKE ?",
            (session, twin["recording_key"], f"%-{twin['twin_of_leg']}-%")):
        if row["item_id"] == exclude or row["pruned_at"] or not Path(row["md_path"]).is_file():
            continue
        recording = calls.recording_for_audio(row["orig_name"] or "")
        if recording is None:
            continue
        if (recording["recording_key"] == twin["twin_of_recording_key"]
                and row["duplicate_of_call_session_id"] is None):
            return True
        if (recording["recording_key"] == twin["recording_key"]
                and row["twin_of_recording_key"] == twin["twin_of_recording_key"]
                and row["duplicate_of_call_session_id"] == session):
            return True
    return False

def _published_primary_twin(conn, *, name: str, item_id: str, session: str) -> bool:
    """A primary yields only to a note explicitly paired with its object key."""
    from . import calls
    recording = calls.recording_for_audio(name)
    if recording is None:
        return False
    for row in conn.execute(
            "SELECT i.item_id,i.orig_name,i.twin_of_recording_key,t.md_path,t.pruned_at "
            "FROM items i JOIN transcripts t ON t.item_id=i.item_id "
            "WHERE i.duplicate_of_call_session_id=? AND i.twin_of_recording_key=?",
            (session, recording["recording_key"])):
        if (row["item_id"] != item_id and not row["pruned_at"]
                and Path(row["md_path"]).is_file()):
            # The durable pair key survives a late CDR, but it must belong to
            # an actual distinct recording; an unresolvable note is not proof.
            other = calls.recording_for_audio(row["orig_name"] or "")
            if other is not None and other["recording_key"] != recording["recording_key"]:
                return True
    return False


def _park_twin(item_id: str, path: Path, session: str) -> dict[str, Any]:
    """Journal each collision-free destination before rename; retry can finish after a crash."""
    conn = ledger.connect()
    ledger.guard_fence(item_id)
    conn.execute("UPDATE items SET duplicate_of_call_session_id=?, updated_at=? WHERE item_id=?",
                 (session, sentinel.utcnow(), item_id))
    dest = paths.SKIPPED / "duplicate-call" / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    def journal(candidate: Path) -> None:
        conn.execute("UPDATE items SET path=?, updated_at=? WHERE item_id=?",
                     (str(candidate), sentinel.utcnow(), item_id))
    try:
        moved = rename.move_noclobber(path, dest, on_destination=journal)
    except Exception:
        # A failed attempt before the move leaves the original inbox file intact.
        # A failure after the move keeps its journaled destination for retry.
        if path.is_file():
            conn.execute("UPDATE items SET path=?, updated_at=? WHERE item_id=?",
                         (str(path), sentinel.utcnow(), item_id))
        raise
    _set_state(item_id, "skipped", cause="duplicate_call",
               evidence=f"duplicate of call {session}; audio preserved at {moved}", subject=path.name)
    return {"reason": "duplicate_call", "call_session_id": session, "path": str(moved)}


def _recover_parked_twin() -> Optional[dict[str, Any]]:
    """Complete only a journaled twin move whose actual parked audio exists."""
    conn = ledger.connect()
    for row in conn.execute("SELECT item_id,path,duplicate_of_call_session_id FROM items "
                            "WHERE state='archived' AND duplicate_of_call_session_id IS NOT NULL"):
        path = Path(row["path"])
        if path.parent != paths.SKIPPED / "duplicate-call" or not path.is_file():
            continue
        _set_state(row["item_id"], "skipped", cause="duplicate_call",
                   evidence=f"duplicate of call {row['duplicate_of_call_session_id']}; audio preserved at {path}",
                   subject=path.name)
        return {"item_id": row["item_id"], "skipped": {"reason": "duplicate_call",
                "call_session_id": row["duplicate_of_call_session_id"], "path": str(path)}}
    return None


def _skip_twin(item_id: str, path: Path, start_state: str) -> Optional[dict[str, Any]]:
    """Choose one note owner using real publication, not merely a CDR or recording row."""
    if start_state not in ("pending", "archived"):
        return None  # Existing transcripts and notes are HEYMA-26's operator decision.
    from . import calls
    if not calls.TELNYX_TRUNK.fullmatch(path.name):
        return None
    try:
        twin = calls.twin_of(path.name)
        conn = ledger.connect()
        recording = calls.recording_for_audio(path.name)
        call = calls.call_for_audio(path.name) if not (twin and twin["call"]) else None
    except Exception as exc:  # noqa: BLE001 - a broken calls ledger must not lose a recording
        log.warning("twin check failed for %s: %s: %s", path.name, type(exc).__name__, exc)
        return None
    if twin and twin["call"] and recording is not None and recording["recording_key"] == twin["recording_key"]:
        session = twin["twin_of_session"]
        if _published_note(conn, twin=twin, exclude=item_id):
            return _park_twin(item_id, path, session)
        # Persist the exact matched recording even if this twin's own CDR later
        # arrives and twin_of() correctly stops returning the old association.
        conn.execute("UPDATE items SET duplicate_of_call_session_id=?,twin_of_recording_key=?,updated_at=? "
                     "WHERE item_id=?", (session, twin["twin_of_recording_key"], sentinel.utcnow(), item_id))
        return None
    if call and call.get("call_session_id") and not call.get("duplicate_of_call_session_id") and recording is not None:
        session = call["call_session_id"]
        if _published_primary_twin(conn, name=path.name, item_id=item_id, session=session):
            return _park_twin(item_id, path, session)
    return None


def _process_item(item_id: str, path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"item_id": item_id, "path": str(path)}
    ledger.guard_fence(item_id)

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

    ledger.guard_fence(item_id)
    twin_skip = _skip_twin(item_id, path, start_state)
    if twin_skip is not None:
        result["skipped"] = twin_skip
        return result

    # Junk by structure (a Telnyx voicemail): archived, never transcribed (wax/junk.py).
    quiet: Optional[dict[str, Any]] = None
    if start_state != "transcribed":
        rejection = junk.before_asr(path, item_id)
        if rejection is not None:
            result["skipped"] = _reject_junk(item_id, path, rejection, "before ASR")
            return result

        # Silent audio, by MEASUREMENT, for every source: after archive + S3 verify (the audio
        # is safe) and before any GPU time. Fails open (levels None) when ffmpeg cannot say.
        levels = sanity.audio_levels(path)
        try:
            ledger.record_audio_levels(item_id, levels)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not stop a recording
            log.warning("item %s: could not record audio levels: %s", item_id, exc)
        if levels is not None:
            result["levels"] = levels
        quiet = _quiet_speech(item_id, path, levels)
        rejection = None if quiet else junk.silent_rejection(levels, duration_s, item_id)
        if rejection is not None:
            result["skipped"] = _reject_junk(item_id, path, rejection, "before ASR", levels=levels)
            return result
        if levels is not None and not levels["silent"]:
            _rearm_silent_alarm(item_id)

    # ---- 2. transcription (skipped if this item already has one) -------
    if start_state == "transcribed":
        result["transcribe"] = {"skipped": "already transcribed"}
    else:
        # The gained copy (quiet speech) lives only around this run, whatever happens in it.
        # A copy a killed waxd left behind is swept first: ASR is the first stage to use the dir.
        for stale in (paths.LOGS / item_id / "gain").glob("*.flac"):
            stale.unlink(missing_ok=True)
        with sanity.gained_audio(path, quiet["gain_db"] if quiet else None,
                                 paths.LOGS / item_id / "gain") as heard:
            if quiet and heard is None:
                log.warning("item %s: quiet speech found but the gained copy could not be written; "
                            "transcribing the original", item_id)
            _write_claim(item_id, path, "transcribe")
            if heard is not None:
                # Provisional, BEFORE the publish commit: a waxd killed between the publish and
                # the bookkeeping must not leave a transcribed quiet item whose later passes
                # (diarization, voice-id read items.audio_gain_db) hear the -75 dB original.
                # Every failure below clears it again.
                _record_gain(item_id, quiet["gain_db"])
            try:
                result["transcribe"] = transcribe_adapter.transcribe(
                    path, item_id=item_id, asr_audio=heard,
                    gain_db=quiet["gain_db"] if heard is not None else None)
            except transcribe_adapter.TranscriptRejected as e:
                # Junk by content ("Hello?", one word, a greeting): nothing was
                # published, so park the audio and stop before any pass is planned.
                if heard is not None:
                    _record_gain(item_id, None)
                result["skipped"] = _reject_junk(item_id, path, e.rejection, "after ASR", e.kept, levels=e.levels)
                return result
            except transcribe_adapter.TranscribeError as e:
                # The gate already moved a failed transcript aside and set the item
                # to `suspect`. The audio stays in the inbox so the failure stays
                # visible rather than being quietly filed away.
                if heard is not None:
                    _record_gain(item_id, None)
                if str(e).startswith("SANITY GATE"):
                    # The `suspect` transition happened inside transcribe_adapter, so
                    # _set_state never sees it. Log and chime it here or it is silent.
                    log.error("item %s quarantined as suspect: %s", item_id, _first_line(str(e), 300))
                    _announce_item_failure(path.name, "transcript failed the sanity gate", str(e), item_id)
                else:
                    _set_state(item_id, "failed", cause=getattr(e, "cause_code", "transcribe_failed"),
                               evidence=str(e)[:400], subject=path.name)
                result["error"] = str(e)[:400]
                return result
        if quiet and heard is not None:
            result["quiet_speech"] = quiet
            _quiet_speech_heard(item_id, path.name, quiet)

    _report_diarization(item_id, path.name, result["transcribe"] or {})

    ledger.guard_fence(item_id)

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

    ledger.guard_fence(item_id)

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
        recovered = _recover_parked_twin()
        if recovered is not None:
            return recovered
        nxt = next_item()
        if nxt is None:
            return None
        item_id, path, action = nxt
        _write_claim(item_id, path, "park" if action in ("park", "park-junk", "park-twin-redrop") else "claimed")
    try:
        if action == "park":
            return park_duplicate(item_id, path)
        if action == "park-junk":
            return park_junk_redrop(item_id, path)
        if action == "park-twin-redrop":
            return park_twin_redrop(item_id, path)
        return process(item_id, path)
    finally:
        _clear_claim()


SKIPPABLE_STATES = WORK_STATES + ("failed", "suspect")


def _last_cause(conn, item_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT cause_code,evidence FROM transitions WHERE machine='item' AND subject=? "
        "ORDER BY seq DESC LIMIT 1", (item_id,)).fetchone()
    return dict(row) if row else None


def _has_verified_backup(conn, item_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM backups WHERE item_id=? AND verified_at IS NOT NULL LIMIT 1",
        (item_id,)).fetchone() is not None


def _not_skippable(conn, item_id: str, row) -> str:
    """The refusal for an item `wax skip` cannot take, naming what to do instead."""
    st = row["state"]
    if st == "skipped":
        if _is_junk_rejected(conn, item_id):
            return (f"item {item_id} is already parked (skipped, junk gate): audio is in "
                    f"skipped/; `wax retry {item_id}` releases it back to the inbox")
        return f"item {item_id} is already skipped; its audio is in skipped/"
    if st in ("complete", "enrichment_pending"):
        return f"item {item_id} is {st}: nothing to skip (it has already been transcribed)"
    return (f"item {item_id} is {st}: only an inbox item that is waiting, failed or "
            f"suspect can be skipped (see wax queue)")


def skip_item(item_id: str) -> dict[str, Any]:
    """Preserve an inbox file outside the inbox and exclude it from processing.

    Takes a waiting item, and a failed or suspect one (`wax retry` is the other
    way out of those; without a verb that parks them they sit in the inbox
    forever). Never deletes: the audio moves to ``skipped/``. A failed item keeps
    its previous cause in the transition evidence, and when the ledger holds no
    verified S3 backup for it the result carries a ``warning`` saying the
    skipped/ copy is then the only one.
    """
    with _SELECTION_LOCK:
        claim = state._active_claim()
        if claim and claim.get("item") == item_id:
            raise RuntimeError("the active item cannot be skipped")
        conn = ledger.connect()
        row = conn.execute(
            "SELECT path,state,orig_name FROM items WHERE item_id=?", (item_id,)
        ).fetchone()
        if not row:
            raise RuntimeError(f"no item with id {item_id} (see wax queue)")
        if row["state"] not in SKIPPABLE_STATES:
            raise RuntimeError(_not_skippable(conn, item_id, row))
        source = Path(row["path"])
        try:
            source.resolve().relative_to(paths.INBOX.resolve())
        except (OSError, ValueError) as e:
            raise RuntimeError(f"item {item_id} ({row['state']}) is not in the inbox; "
                               "there is nothing to move") from e
        if not source.is_file():
            raise RuntimeError(f"item {item_id} ({row['state']}): its audio is missing from "
                               f"the inbox ({source.name}); nothing to skip")
        previous = _last_cause(conn, item_id) if row["state"] in ("failed", "suspect") else None
        backed_up = _has_verified_backup(conn, item_id)
        paths.SKIPPED.mkdir(parents=True, exist_ok=True)
        moved = rename.move_noclobber(source, paths.SKIPPED / source.name)
        conn.execute("UPDATE items SET path=?,updated_at=? WHERE item_id=?",
                     (str(moved), sentinel.utcnow(), item_id))
        shown = str(moved).replace(str(paths.AUDIO), "~/HeyMa")
        evidence = shown
        if previous:
            evidence = f"previous_cause={previous.get('cause_code') or 'unknown'} {shown}"
        _set_state(item_id, "skipped", cause="operator_skip", evidence=evidence,
                   subject=source.name)
        out: dict[str, Any] = {"item_id": item_id, "state": "skipped", "path": str(moved),
                               "name": row["orig_name"] or source.name,
                               "previous_state": row["state"]}
        if previous:
            out["previous_cause"] = previous.get("cause_code")
        if not backed_up:
            out["warning"] = (f"no verified S3 backup for this item: {shown} is the only copy "
                              "(it was moved, not deleted)")
        return out


def _is_junk_rejected(conn, item_id: str) -> bool:
    last = conn.execute(
        "SELECT cause_code FROM transitions WHERE machine='item' AND subject=? "
        "ORDER BY seq DESC LIMIT 1", (item_id,)).fetchone()
    return bool(last) and junk.is_junk_cause(last["cause_code"])


def _release_junk(conn, item_id: str, source: Path, orig_name: Optional[str]) -> dict[str, Any]:
    """Recover a junk false positive: audio back to the inbox, gate bypassed for this item.

    The ``junk_override`` transition is the durable marker the gate looks for
    (junk.overridden), so the item is not rejected again and a re-drop of its
    bytes is not parked as a junk duplicate.
    """
    resolved = None
    try:
        resolved = source.resolve()
    except OSError:
        pass
    if resolved is None or not any(resolved.is_relative_to(d.resolve()) for d in junk.reject_dirs()):
        raise RuntimeError("junk-rejected audio is not in skipped/junk or skipped/silent")
    if not source.is_file():
        raise RuntimeError("junk-rejected audio is missing")
    paths.INBOX.mkdir(parents=True, exist_ok=True)
    moved = rename.move_noclobber(source, paths.INBOX / source.name)
    conn.execute("UPDATE items SET path=?,updated_at=? WHERE item_id=?",
                 (str(moved), sentinel.utcnow(), item_id))
    was_silent = _junk_cause(conn, item_id) == junk.SILENT_CAUSE
    _set_state(item_id, "pending", cause=junk.OVERRIDE_CAUSE,
               evidence=(f"operator released a {'silent-audio' if was_silent else 'junk'} rejection; "
                         f"audio back at {moved}"),
               subject=orig_name or moved.name)
    return {"item_id": item_id, "state": "pending", "path": str(moved),
            "previous_cause": "junk_silent" if was_silent else "junk", "junk_override": True}


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
            raise RuntimeError(f"no item with id {item_id} (see wax queue)")
        source = Path(row["path"])
        if row["state"] == "skipped" and _is_junk_rejected(conn, item_id):
            return _release_junk(conn, item_id, source, row["orig_name"])
        if row["state"] not in ("failed", "suspect"):
            hint = ("it is already waiting for the worker" if row["state"] in WORK_STATES
                    else "only a failed or suspect inbox item can be retried (see wax queue)")
            raise RuntimeError(f"item {item_id} is {row['state']}: {hint}")
        try:
            source.resolve().relative_to(paths.INBOX.resolve())
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"failed item {item_id} is not in the inbox; "
                               "there is no file to retry") from exc
        if not source.is_file():
            raise RuntimeError(f"failed item {item_id}: its audio is missing from the inbox "
                               f"({source.name})")
        previous = _last_cause(conn, item_id)
        previous_cause = previous["cause_code"] if previous else None
        previous_evidence = ledger.first_line(previous.get("evidence")) if previous else ""
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
            "name": row["orig_name"] or source.name,
            "previous_cause": previous_cause,
            "previous_evidence": previous_evidence,
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
        self._halt = threading.Event()
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
        self._halt.set()

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
        while not self._halt.wait(self.HEARTBEAT_INTERVAL_S):
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
        while not self._halt.is_set():
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
                self._halt.wait(POLL_S)
