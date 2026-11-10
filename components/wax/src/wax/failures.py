"""`wax failures`: file each recent Wax failure once, as an urgent HEYMA ticket.

Wax degrades by RECORDING, not by crashing: a failed pass, an exhausted document
receipt, a failed item, a stale outbox row all sit in `var/wax.db` where `wax
status` counts them and nobody reads them. This module turns them into fleet
work: a deterministic collector (no LLM, no network) over the ledger, folded into
one signature per (kind, pass-or-pipeline, reason_code), and a sweep that files
each signature as ONE Todo/urgent ticket on the HEYMA board through `px`. The
board's own lanes do the rest: heyma-pm grooms it, a delegated worker fixes it.

Three parts, kept apart so the read side can never write:

- collect()  reads through the definitions Wax already has (the pass gate and the
             document exclusions behind ledger.passes_failed, documents states,
             events.UNPUBLISHED, hygiene summaries, calls.health) and returns
             failure rows. No px, no writes.
- group()    folds ticket-worthy rows into signatures, newest failure first.
- sweep()    the only writer: `failure_tickets` (created lazily HERE, never by
             waxd) is the durable dedupe, and px is the only side effect.

Ticket-worthy means FINAL (nothing will retry it: an exhausted pass or receipt, a
failed/suspect item, a terminal fetch_error, a stale outbox row, a rejected
snapshot, a hygiene job error, a worker exception) or still failing after the
grace (default 1 h: waxd sweeps passes every 900 s and scans documents every
120 s, so a failure standing after an hour will not heal itself).

Never ticket-worthy: dependency_failed (its upstream gets the ticket),
voicemail_empty (a caller hung up at the beep), call outcomes such as
CALL_REJECTED (a person's choice; calls are never collected), documents that
were superseded, trashed or vanished, and passes that are switched off (registry
or operator) or failed at a version older than the registry's.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from . import component, documents, events, hygiene, ledger, paths
from . import settings as operator_settings

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

DEFAULT_SINCE_S = 7 * 86400.0
DEFAULT_GRACE_S = 3600.0
DEFAULT_MAX_NEW = 5
SHOW_SUBJECTS = 20          # affected subjects listed per ticket / comment
DETAIL_CHARS = 200          # one line of detail per subject, never a stderr tail
TITLE_MAX = 119             # keep titles under 120 characters
PX_TIMEOUT_S = 60.0
PX_DEFAULT = Path.home() / ".local" / "bin" / "px"
LABELS = "bug,wax-failure"
MARKER_PREFIX = "wax-failure"
CLOSED_GROUPS = ("completed", "cancelled")

TABLE = "failure_tickets"
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS failure_tickets ("
    "fp TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 1, kind TEXT NOT NULL, "
    "slug TEXT NOT NULL, reason_code TEXT NOT NULL, ticket_ref TEXT, ticket_id TEXT, "
    "status TEXT NOT NULL, subjects TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, last_error TEXT)"
)

# What the "slug" of each kind is, for the ticket body.
_SLUG_NOUN = {
    "pass": "Pass", "document": "Pipeline", "item": "Item state", "recording": "Recording kind",
    "outbox": "Outbox", "snapshot": "Store", "hygiene": "Hygiene job", "worker": "Machine",
}


# ------------------------------------------------------------------ time ----

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.IGNORECASE)
_UNIT_S = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 7 * 86400.0}


def parse_duration(text: Any) -> float:
    """`30m`, `1h`, `7d`, `2w` (or bare seconds) -> seconds."""
    match = _DURATION.match(str(text))
    if not match:
        raise ValueError(f"not a duration: {text!r} (use 30m, 1h, 7d or 2w)")
    return float(match.group(1)) * _UNIT_S[match.group(2).lower()]


def human_duration(seconds: float) -> str:
    seconds = float(seconds)
    for unit, size in (("w", 7 * 86400), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{seconds:g}s"


def _epoch(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _iso(epoch: float) -> str:
    return (datetime.fromtimestamp(epoch, timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _recent(at: Any, cutoff: float) -> bool:
    seen = _epoch(at)
    return seen is not None and seen >= cutoff


# --------------------------------------------------------------- hygiene ----
# Anything that reaches a ticket passes through _line(): first non-blank line
# only, credential-shaped runs redacted, length-capped. A ticket is shared
# state on a board; it never carries env, transcript bodies or stderr tails.

_SECRETS = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)\b((?:api[_-]?)?keys?|tokens?|secrets?|passwords?|passwd|authorization|credentials?)"
                r"(\s*[:=]\s*)(\"?)[^\s\"',;&]+"), r"\1\2\3[redacted]"),
    (re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|xox[abp])[-_][A-Za-z0-9_-]{8,}"), "[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[redacted]"),
    # An opaque run of 32+ letters AND digits with no dash or slash: an API key,
    # not a path or a dash-joined file name (telnyx-<uuid>-<epoch>.mp3 is the most
    # useful pointer a call-metadata ticket has). All-lowercase hex is exempt: in
    # Wax that is a sha256 or a definition id, a pointer, and the key shapes that
    # wrap hex (sk-..., plane_api_...) are caught above or by their letters.
    (re.compile(r"(?<![A-Za-z0-9_])(?![0-9a-f]+(?![A-Za-z0-9_]))(?=[A-Za-z0-9_]*\d)(?=[A-Za-z0-9_]*[A-Za-z])"
                r"[A-Za-z0-9_]{32,}(?![A-Za-z0-9_])"), "[redacted]"),
)


def _scrub(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def _line(text: Any, limit: int = DETAIL_CHARS) -> str:
    """First non-blank line, scrubbed, printable, at most `limit` characters."""
    first = next((s.strip() for s in str(text or "").splitlines() if s.strip()), "")
    if not first:
        return ""
    first = _scrub(first).replace("`", "'").replace(f"[{MARKER_PREFIX}:", f"{MARKER_PREFIX}:")
    first = "".join(ch for ch in first if ch.isprintable())
    return first if len(first) <= limit else first[: limit - 3].rstrip() + "..."


# ------------------------------------------------------------- identity ----

def fingerprint(kind: str, slug: str, reason_code: str) -> str:
    """The signature's stable id: sha256("kind|slug|reason")[:12]."""
    return hashlib.sha256(f"{kind}|{slug}|{reason_code}".encode()).hexdigest()[:12]


def marker(fp: str, generation: int) -> str:
    """The px --marker TEXT. px stores and searches it as `[TEXT]`."""
    return f"{MARKER_PREFIX}:{fp}.{int(generation)}"


# ------------------------------------------------------------- collector ----

def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _row(kind: str, slug: Any, reason: Any, subject: Any, *, at: Any, final: bool,
         ref: Any = None, label: Any = None, attempts: Any = None, exhausted_at: Any = None,
         detail: Any = None, hints: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {
        "kind": kind, "slug": str(slug or "unknown"), "reason_code": str(reason or "unknown"),
        "subject": str(subject), "ref": str(ref or subject), "label": str(label) if label else None,
        "attempts": int(attempts) if attempts is not None else None, "exhausted_at": exhausted_at,
        "at": at, "final": bool(final), "detail": _line(detail),
        "hints": {k: v for k, v in (hints or {}).items() if v not in (None, "")},
    }


def _subject_context(conn: sqlite3.Connection, key: str) -> tuple[Optional[str], dict[str, Any]]:
    """(label, command hints) for one pass-ledger key: an audio item or a document."""
    if documents.is_document_key(key):
        binding, doc = documents.split_key(key)
        row = conn.execute("SELECT path FROM documents WHERE binding_id=? AND doc_id=?",
                           (binding, doc)).fetchone()
        receipt = conn.execute(
            "SELECT pipeline FROM document_receipts WHERE binding_id=? AND doc_id=? "
            "ORDER BY last_attempt_at DESC LIMIT 1", (binding, doc)).fetchone()
        path = row["path"] if row else None
        return path, {"document": True, "doc_id": doc, "binding_id": binding, "path": path,
                      "pipeline": receipt["pipeline"] if receipt else None}
    row = conn.execute("SELECT i.orig_name, t.md_path FROM items i LEFT JOIN transcripts t USING(item_id) "
                       "WHERE i.item_id=?", (key,)).fetchone()
    if not row:
        return None, {"item_id": key}
    return row["orig_name"] or (Path(row["md_path"]).name if row["md_path"] else None), \
        {"item_id": key, "note": row["md_path"]}


def _collect_passes(conn, tables, *, cutoff, now, since_s, grace_s) -> list[dict[str, Any]]:
    """Failed pass rows on the SAME definition `wax status` uses (ledger.passes_failed):
    an enabled pass at its current registry version, behind no vanished or superseded
    document and no pruned transcript. Unlike passes_failed, exhausted rows are the point here, not noise."""
    if "passes" not in tables:
        return []
    # _current_pass_gate() swallows a broken registry as "nothing failed"; for a
    # collector that is a lie, so let the registry load raise first.
    ledger._enabled_pass_versions()
    gate, params = ledger._current_pass_gate()
    if gate == "0":
        return []
    switched_off = operator_settings.disabled_passes()
    exclusions = (f" AND item_id NOT IN ({ledger._VANISHED_DOC_KEYS})"
                  f" AND item_id NOT IN ({ledger._SUPERSEDED_DOC_KEYS})") if "documents" in tables else ""
    if "transcripts" in tables and "pruned_at" in _columns(conn, "transcripts"):
        exclusions += f" AND item_id NOT IN ({ledger._PRUNED_ITEMS})"
    exhausted = "exhausted_at" if "exhausted_at" in _columns(conn, "passes") else "NULL AS exhausted_at"
    rows = conn.execute(
        f"SELECT item_id, ep_slug, attempt, reason_code, {exhausted}, updated_at, detail FROM passes "
        f"WHERE state='failed' AND {gate}{exclusions} "
        "AND COALESCE(reason_code,'') != 'dependency_failed'", params).fetchall()
    out = []
    for r in rows:
        if r["ep_slug"] in switched_off or not _recent(r["updated_at"], cutoff):
            continue
        label, hints = _subject_context(conn, r["item_id"])
        out.append(_row("pass", r["ep_slug"], r["reason_code"] or "failed", f"{r['item_id']}/{r['ep_slug']}",
                        at=r["updated_at"], final=bool(r["exhausted_at"]), ref=r["item_id"], label=label,
                        attempts=r["attempt"], exhausted_at=r["exhausted_at"], detail=r["detail"],
                        hints={**hints, "slug": r["ep_slug"]}))
    return out


def _collect_documents(conn, tables, *, cutoff, now, since_s, grace_s) -> list[dict[str, Any]]:
    """Failed document receipts whose document is still failed and not superseded.

    A receipt whose document also carries a failed pass row is left to that pass
    row (kind `pass`): one root cause, one ticket, at the pass where it failed.
    What remains is a pipeline-level fault (a run crash, a halt) with no pass to
    blame. A document whose state is processed, trashed or gone is not failing."""
    if not {"document_receipts", "documents"} <= tables:
        return []
    superseded = {row[0] for row in conn.execute(ledger._SUPERSEDED_DOC_KEYS)}
    blamed = ({row[0] for row in conn.execute("SELECT DISTINCT item_id FROM passes WHERE state='failed'")}
              if "passes" in tables else set())
    reason = "r.reason_code" if "reason_code" in _columns(conn, "document_receipts") else "NULL AS reason_code"
    rows = conn.execute(
        "SELECT r.binding_id, r.doc_id, r.pipeline, r.definition_id, r.state, r.attempts, "
        f"r.last_attempt_at, r.exhausted_at, r.last_error, {reason}, d.path "
        "FROM document_receipts r JOIN documents d ON d.binding_id=r.binding_id AND d.doc_id=r.doc_id "
        "WHERE r.state IN ('failed','halted') AND d.state='failed'").fetchall()
    out = []
    for r in rows:
        key = documents.doc_key(r["binding_id"], r["doc_id"])
        if key in superseded or key in blamed or not _recent(r["last_attempt_at"], cutoff):
            continue
        subject = "|".join((r["binding_id"], r["doc_id"], r["pipeline"], r["definition_id"]))
        out.append(_row("document", r["pipeline"], r["reason_code"] or r["state"], subject,
                        at=r["last_attempt_at"], final=bool(r["exhausted_at"]), ref=key, label=r["path"],
                        attempts=r["attempts"], exhausted_at=r["exhausted_at"], detail=r["last_error"],
                        hints={"document": True, "doc_id": r["doc_id"], "binding_id": r["binding_id"],
                               "pipeline": r["pipeline"], "path": r["path"]}))
    return out


def _in_inbox(path: Any) -> bool:
    try:
        p = Path(str(path))
        p.resolve().relative_to(paths.INBOX.resolve())
        return p.is_file()
    except (OSError, ValueError):
        return False


def _collect_items(conn, tables, *, cutoff, now, since_s, grace_s) -> list[dict[str, Any]]:
    """failed/suspect items: final by design (the worker never auto-retries them)."""
    if "items" not in tables:
        return []
    out = []
    for r in conn.execute("SELECT item_id, state, orig_name, path, updated_at FROM items "
                          "WHERE state IN ('failed','suspect')").fetchall():
        cause = conn.execute(
            "SELECT cause_code, evidence, at FROM transitions WHERE machine='item' AND subject=? "
            "AND to_state=? ORDER BY seq DESC LIMIT 1", (r["item_id"], r["state"])).fetchone()
        at = cause["at"] if cause else r["updated_at"]
        if not _recent(at, cutoff):
            continue
        out.append(_row("item", r["state"], cause["cause_code"] if cause else None, r["item_id"], at=at,
                        final=True, ref=r["item_id"], label=r["orig_name"],
                        detail=cause["evidence"] if cause else None,
                        hints={"item_id": r["item_id"], "path": r["path"],
                               "in_inbox": _in_inbox(r["path"])}))
    return out


def _collect_recordings(conn, tables, *, cutoff, now, since_s, grace_s) -> list[dict[str, Any]]:
    """Call audio Wax could not get: a terminal fetch_error other than voicemail_empty
    (by design: nothing was said), and voicemails calls.health() reports unfetched
    once they are past the grace (the Candybar sync fetches every 10 minutes and
    the Telnyx link dies after 7 days)."""
    out: list[dict[str, Any]] = []
    if "call_recordings" in tables and "fetch_error" in _columns(conn, "call_recordings"):
        for r in conn.execute(
                "SELECT recording_key, kind, call_session_id, fetch_error, updated_at FROM call_recordings "
                "WHERE fetch_error IS NOT NULL AND fetch_error != 'voicemail_empty'").fetchall():
            if not _recent(r["updated_at"], cutoff):
                continue
            out.append(_row("recording", r["kind"], r["fetch_error"], r["recording_key"], at=r["updated_at"],
                            final=True, ref=r["recording_key"], label=r["call_session_id"],
                            detail=f"fetch_error {r['fetch_error']}",
                            hints={"call_session_id": r["call_session_id"]}))
    if "call_events" in tables:
        from . import calls
        unfetched = calls.health(days=max(1, math.ceil(since_s / 86400)))["voicemail_unfetched"]
        for event_id in unfetched:
            ev = conn.execute("SELECT occurred_at, call_session_id FROM call_events WHERE event_id=?",
                              (event_id,)).fetchone()
            if not ev or not _recent(ev["occurred_at"], cutoff):
                continue
            out.append(_row("recording", "voicemail", "voicemail_unfetched", f"vm:{event_id}",
                            at=ev["occurred_at"], final=False, ref=f"vm:{event_id}", label=ev["call_session_id"],
                            detail="voicemail event carries a recording URL but no audio reached the inbox",
                            hints={"call_session_id": ev["call_session_id"]}))
    if "call_recordings" in tables and "items" in tables:
        from . import calls
        # Trunk call audio that never became an item: the 2026-11-07 paused-intake outage.
        for r in calls.undelivered_recordings(conn):
            if not _recent(r["ended_at"], cutoff):
                continue
            fix = "candybar resume telnyx-s3-intake" if r["has_object"] else "wax calls backfill"
            out.append(_row("recording", "trunk", "recording_undelivered", r["recording_key"],
                            at=r["ended_at"], final=False, ref=r["recording_key"], label=r["call_session_id"],
                            detail=f"call recording {r['finding']} never reached the inbox ({fix})",
                            hints={"call_session_id": r["call_session_id"], "call_leg_id": r["call_leg_id"]}))
    return out


def _collect_outbox(conn, tables, *, cutoff, now, since_s, grace_s) -> list[dict[str, Any]]:
    """Events still owed to the bus past events.OUTBOX_STALE_S (doctor's FAIL line)."""
    if "outbox" not in tables:
        return []
    owed = events.UNPUBLISHED if "suppressed_reason" in _columns(conn, "outbox") else "published_at IS NULL"
    out = []
    for r in conn.execute(f"SELECT id, subject, created_at, attempts, last_error FROM outbox WHERE {owed} "
                          "AND created_at <= ? ORDER BY id", (_iso(now - events.OUTBOX_STALE_S),)).fetchall():
        if not _recent(r["created_at"], cutoff):
            continue
        out.append(_row("outbox", "bloodbank", "outbox_stale", r["id"], at=r["created_at"], final=True,
                        ref=f"outbox#{r['id']}", label=r["subject"], attempts=r["attempts"],
                        detail=r["last_error"] or "never acknowledged by the bus"))
    return out


def _collect_snapshots(conn, tables, *, cutoff, now, since_s, grace_s) -> list[dict[str, Any]]:
    """transcript_snapshots the store rejected (final), or that keep failing to project.

    A projected row may keep the last_error of an earlier transient failure
    (mark() COALESCEs it), so only a rejection note counts once projected."""
    if "transcript_snapshots" not in tables:
        return []
    from .transcriptions import outbox as snapshots
    out = []
    for r in conn.execute(
            "SELECT seq, item_id, kind, created_at, projected_at, attempts, last_error FROM transcript_snapshots "
            "WHERE last_error IS NOT NULL AND (projected_at IS NULL OR last_error LIKE ?)",
            (snapshots.REJECTED_PREFIX + "%",)).fetchall():
        rejected = r["projected_at"] is not None
        at = r["projected_at"] if rejected else r["created_at"]
        if not _recent(at, cutoff):
            continue
        out.append(_row("snapshot", "transcriptions", "snapshot_rejected" if rejected else "projection_failed",
                        r["seq"], at=at, final=rejected, ref=f"snapshot#{r['seq']}",
                        label=f"{r['item_id']} ({r['kind']})", attempts=r["attempts"], detail=r["last_error"],
                        hints={"item_id": r["item_id"]}))
    return out


def _collect_hygiene(conn, tables, *, cutoff, now, since_s, grace_s) -> list[dict[str, Any]]:
    """A document-hygiene job whose last recorded run errored (hygiene.load_summary)."""
    out = []
    for kind in ("empty", "untitled"):
        summary = hygiene.load_summary(kind)
        if not summary or not summary.get("last_error"):
            continue
        at = summary.get("last_run_at")
        if not _recent(at, cutoff):
            continue
        # Keyed by job AND day: the summary holds only the latest run, so a bare
        # job key could never be "new" again, and a fixed-then-recurring error
        # would never reopen a closed ticket.
        out.append(_row("hygiene", kind, "sweep_error", f"{kind}@{str(at)[:10]}", at=at, final=True,
                        ref=f"{kind} sweep {str(at)[:10]}", label=", ".join(summary.get("folders") or []),
                        detail=summary["last_error"]))
    return out


def _collect_worker(conn, tables, *, cutoff, now, since_s, grace_s) -> list[dict[str, Any]]:
    """`inbox` machine errors: every worker_exception (a crashed tick is a bug), and
    a stranded_work episode only when it lasted past the grace.

    stranded_work is mostly the gap between two items: state.inbox_state measures
    the wait from the youngest file's ctime, not from when the last claim ended,
    so the queue reads "stranded" for the second it takes the worker to claim
    the next item (the live ledger: 30 of 31 episodes healed in 1-77 s). Only an
    episode that outlives the grace means the worker stopped taking work. An
    ongoing episode with nothing actionable in the inbox is the failed-items-only
    case (note_machine runs before enrich() relabels it), which item tickets
    already cover."""
    if "transitions" not in tables:
        return []
    out = []
    actionable: Optional[int] = None
    for r in conn.execute(
            "SELECT seq, cause_code, evidence, at FROM transitions WHERE machine='inbox' AND to_state='error' "
            "AND cause_code IN ('worker_exception','stranded_work') AND at >= ? ORDER BY seq",
            (_iso(cutoff),)).fetchall():
        detail, final = r["evidence"], True
        if r["cause_code"] == "stranded_work":
            nxt = conn.execute("SELECT at FROM transitions WHERE machine='inbox' AND seq>? ORDER BY seq LIMIT 1",
                               (r["seq"],)).fetchone()
            if nxt:
                lasted = (_epoch(nxt["at"]) or now) - (_epoch(r["at"]) or now)
                if lasted < grace_s:
                    continue
                detail = f"{r['evidence'] or 'stranded'}; the worker resumed after {lasted:.0f}s"
            else:
                if actionable is None:
                    actionable = int(ledger.inbox_counts().get("actionable", 0))
                if not actionable:
                    continue
                final = False
        out.append(_row("worker", "inbox", r["cause_code"], r["seq"], at=r["at"], final=final,
                        ref=f"transition#{r['seq']}", detail=detail))
    return out


_COLLECTORS: tuple[tuple[str, Callable[..., list[dict[str, Any]]]], ...] = (
    ("pass", _collect_passes), ("document", _collect_documents), ("item", _collect_items),
    ("recording", _collect_recordings), ("outbox", _collect_outbox), ("snapshot", _collect_snapshots),
    ("hygiene", _collect_hygiene), ("worker", _collect_worker),
)


def collect(conn: Optional[sqlite3.Connection] = None, now: Optional[float] = None,
            since_s: float = DEFAULT_SINCE_S, grace_s: float = DEFAULT_GRACE_S, *,
            errors: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Every failure whose own timestamp falls within `since_s`, newest first.

    Read-only. Each row carries `ticketable` (final, or failing for at least
    `grace_s`). With an `errors` list, a collector that raises is recorded there
    and the others still report; without one, the exception propagates.
    """
    conn = conn if conn is not None else ledger.connect()
    now = time.time() if now is None else float(now)
    ctx = {"cutoff": now - float(since_s), "now": now, "since_s": float(since_s), "grace_s": float(grace_s)}
    tables = _tables(conn)
    rows: list[dict[str, Any]] = []
    for kind, collector in _COLLECTORS:
        try:
            rows.extend(collector(conn, tables, **ctx))
        except Exception as exc:  # noqa: BLE001 - one broken source must not blind the others
            if errors is None:
                raise
            message = f"{kind}: {type(exc).__name__}: {_line(str(exc), 300)}"
            log.warning("failures: collector %s", message)
            errors.append(message)
    for r in rows:
        seen = _epoch(r["at"])
        r["ticketable"] = r["final"] or (seen is not None and now - seen >= float(grace_s))
    rows.sort(key=lambda r: (-(_epoch(r["at"]) or 0.0), r["kind"], r["subject"]))
    return rows


def group(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ticket-worthy rows -> one signature per (kind, slug, reason_code), newest first."""
    signatures: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: (-(_epoch(r["at"]) or 0.0), r["subject"])):
        if not r.get("ticketable"):
            continue
        key = (r["kind"], r["slug"], r["reason_code"])
        sig = signatures.setdefault(key, {"fp": fingerprint(*key), "kind": key[0], "slug": key[1],
                                          "reason_code": key[2], "subjects": [], "_keys": set()})
        if r["subject"] in sig["_keys"]:
            continue
        sig["_keys"].add(r["subject"])
        sig["subjects"].append(r)
    out = []
    for sig in signatures.values():
        sig.pop("_keys")
        stamps = sorted((r["at"] for r in sig["subjects"] if _epoch(r["at"]) is not None), key=_epoch)
        # An exhausted row re-run later has a last attempt newer than its
        # exhaustion; the earliest known failure is the honest "first seen".
        earliest = sorted((at for r in sig["subjects"] for at in (r["at"], r.get("exhausted_at"))
                           if _epoch(at) is not None), key=_epoch)
        sig["affected"] = len(sig["subjects"])
        sig["final"] = sum(1 for r in sig["subjects"] if r["final"])
        sig["first_seen"] = earliest[0] if earliest else None
        sig["last_seen"] = stamps[-1] if stamps else None
        out.append(sig)
    out.sort(key=lambda s: (-(_epoch(s["last_seen"]) or 0.0), s["fp"]))
    return out


# ---------------------------------------------------------------- ticket ----

def title(sig: dict[str, Any]) -> str:
    """`[wax] <kind> <slug>: <reason_code> (<n> affected)`, under 120 characters."""
    tail = f" ({sig['affected']} affected)"
    head = f"[wax] {sig['kind']} {sig['slug']}: {sig['reason_code']}"
    if len(head) + len(tail) > TITLE_MAX:
        head = head[: TITLE_MAX - len(tail) - 3].rstrip() + "..."
    return head + tail


def _subject_line(r: dict[str, Any]) -> str:
    meta = []
    if r.get("attempts") is not None:
        meta.append(f"attempts {r['attempts']}")
    if r.get("exhausted_at"):
        meta.append(f"exhausted {r['exhausted_at']}")
    meta.append(f"last failed {r['at']}")
    line = f"`{r['ref']}`"
    if r.get("label"):
        line += f" {_line(r['label'], 160)}"
    line += f" ({', '.join(meta)})"
    if r.get("detail"):
        line += f": {r['detail']}"
    return line


def _subject_lines(subjects: list[dict[str, Any]]) -> list[str]:
    lines = [f"{i}. {_subject_line(r)}" for i, r in enumerate(subjects[:SHOW_SUBJECTS], 1)]
    if len(subjects) > SHOW_SUBJECTS:
        lines.append(f"... and {len(subjects) - SHOW_SUBJECTS} more not shown (`wax failures --json` lists all).")
    return lines


def _journal(at: Any) -> str:
    seen = _epoch(at)
    since = (datetime.fromtimestamp(seen - 300, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
             if seen is not None else "today")
    return f'`journalctl --user -u waxd.service --since "{since} UTC"`'


def _document_run(h: dict[str, Any]) -> Optional[str]:
    if not h.get("pipeline") or not h.get("path"):
        return None
    binding = f" --binding {h['binding_id']}" if h.get("binding_id") else ""
    return f"`wax documents run {h['pipeline']} '{h['path']}'{binding}`"


def _rerun(r: dict[str, Any]) -> Optional[str]:
    """The exact command that re-drives one subject, when there is one."""
    h, kind = r.get("hints") or {}, r["kind"]
    if kind == "pass":
        if h.get("document"):
            return _document_run(h) or f"`wax ep run {r['slug']} {r['ref']}`"
        return f"`wax ep run {r['slug']} {r['ref']}`"
    if kind == "document":
        if r.get("exhausted_at") and h.get("binding_id"):
            return f"`wax documents retry {h['pipeline']} --doc {h['doc_id']}`, then `wax documents scan {h['pipeline']}`"
        return _document_run(h)
    if kind == "item":
        return f"`wax retry {r['ref']}`" if h.get("in_inbox") else None
    return None


def _look_at(sig: dict[str, Any]) -> list[str]:
    kind, slug, subjects = sig["kind"], sig["slug"], sig["subjects"]
    head = subjects[:3]
    out: list[str] = []
    if kind == "pass":
        out += [f"`wax ep status {r['ref']}`" for r in head]
        out.append("`wax ep list` (enabled, version and outcome tally per pass)")
        for rel in (f"config/passes.d/{slug}.yaml", f"config/passes.d/bin/{slug}"):
            if (component.ROOT / rel).exists():
                out.append(f"`components/wax/{rel}`")
        if any((r.get("hints") or {}).get("document") for r in subjects):
            out.append("`wax documents list --state failed`")
        out.append("`wax doctor` (registry, provider route and Jev probes)")
    elif kind == "document":
        bindings = sorted({(r.get("hints") or {}).get("binding_id") or "" for r in subjects})
        out += [f"`wax documents list --binding {b} --state failed`" if b else "`wax documents list --state failed`"
                for b in bindings[:3]]
        out.append(f"`wax pipeline show {slug}`")
        out += [f"`wax ep status {r['ref']}`" for r in head]
    elif kind == "item":
        out += ["`wax state inbox`", "`wax history --machine item -n 20`", "`wax items`"]
        out.append("the `log retained:` path in each detail line (per-item transcription log under var/logs/)")
    elif kind == "recording":
        sessions = [(r.get("hints") or {}).get("call_session_id") for r in head]
        out += [f"`wax calls show {s}`" for s in sessions if s]
        out += ["`wax calls sync --since 2d` (fetches recordings and voicemail it can still reach)",
                "`wax doctor` (Telnyx probes)"]
    elif kind == "outbox":
        out += ["`wax events backlog`", "`wax events drain`", "`wax doctor` (NATS and outbox probes)"]
    elif kind == "snapshot":
        out += ["`wax status` (the transcripts line)", "`wax transcripts sync --dry-run`",
                "`wax doctor` (the seven transcriptions-store probes)",
                "`components/wax/docs/TRANSCRIPTIONS-STORE.md`"]
    elif kind == "hygiene":
        out += [f"`wax documents sweep-{slug} --dry-run`", "`wax doctor` (document hygiene probe)",
                "`components/wax/src/wax/hygiene.py`"]
    elif kind == "worker":
        out += ["`wax state inbox`", "`wax history --machine inbox -n 20`", "`wax status`",
                "`components/wax/src/wax/worker.py`"]
    reruns = [(r, _rerun(r)) for r in head]
    out += [f"re-run `{r['ref']}`: {cmd}" for r, cmd in reruns if cmd]
    out.append(_journal(sig["first_seen"]))
    return out


_RERUN_RULE = {
    "pass:audio": "`wax ep run {slug} <item_id>` (then `wax ep status <item_id>` shows `{slug}` completed)",
    "pass:document": "`wax documents run <pipeline> <note>`, with `--binding <id>` for a bound note (then "
                     "`wax ep status <doc key>` shows `{slug}` completed)",
    "document": "`wax documents retry {slug} --doc <doc_id>` then `wax documents scan {slug}` for an exhausted "
                "bound receipt, else `wax documents run {slug} <note>` (then `wax documents list --state failed` "
                "no longer lists it)",
    "item": "`wax retry <item_id>`, after which the worker carries it to `complete` (`wax items`)",
    "recording": "`wax calls sync --since 2d`, after which the audio is in the inbox (`wax calls show <session>`)",
    "outbox": "`wax events drain`, after which `wax events backlog` reports 0",
    "snapshot": "`wax transcripts sync`, after which `wax status` no longer reports transcripts lagging/error",
    "hygiene": "`wax documents sweep-{slug}`, which then completes with no last_error",
    "worker": "the queue draining again (`wax state inbox` reports ready-and-waiting or ready-and-active)",
}


def _rerun_rule(sig: dict[str, Any]) -> str:
    if sig["kind"] != "pass":
        return _RERUN_RULE[sig["kind"]].format(slug=sig["slug"])
    shapes = sorted({"document" if (r.get("hints") or {}).get("document") else "audio" for r in sig["subjects"]})
    return "; ".join(_RERUN_RULE[f"pass:{shape}"].format(slug=sig["slug"]) for shape in shapes)


def _criteria(sig: dict[str, Any]) -> list[str]:
    return [
        f"The root cause of `{sig['reason_code']}` in {sig['kind']} `{sig['slug']}` is identified and fixed, "
        "with a regression test under `components/wax/tests/` that fails without the fix; a cause outside "
        "Wax (a provider, the bus, Telnyx) is instead linked here as the ticket that owns it.",
        "Every affected subject above is either re-run to completion or recorded on this ticket as "
        f"unrecoverable with evidence. Re-run: {_rerun_rule(sig)}.",
        f"`wax failures --json` no longer lists signature `{sig['fp']}`, and `wax status` no longer reports it.",
    ]


# Repo-relative path of the incident procedure in HeyMa's hub skill (skills/heyma).
# skill_hub_test checks this file exists, so a move of the sub-skill fails the suite.
DOCTOR_SKILL = "skills/heyma/subskills/doctor/SUBSKILL.md"


def body(sig: dict[str, Any], *, generation: int, marker_text: str,
         previous_ref: Optional[str] = None) -> str:
    """The ticket description: pointer-rich, cheap, written for the triage agent."""
    lines = [f"Filed by `wax failures sweep` (deterministic, no LLM). Start with the `heyma` hub's doctor "
             f"sub-skill (`{DOCTOR_SKILL}`, or `mise run wax:doctor`), then the commands under Look at.", ""]
    if previous_ref:
        lines += [f"Regression: {previous_ref} was closed (or deleted) and this signature is failing again "
                  f"(generation {generation}).", ""]
    lines += [
        f"- Kind: {sig['kind']}",
        f"- {_SLUG_NOUN.get(sig['kind'], 'Slug')}: {sig['slug']}",
        f"- reason_code: {sig['reason_code']}",
        f"- First seen: {sig['first_seen']}",
        f"- Last seen: {sig['last_seen']}",
        f"- Affected: {sig['affected']} ({sig['final']} final, {sig['affected'] - sig['final']} past the grace)",
        f"- Signature: {sig['fp']} (generation {generation})",
        "", "## Affected", "", *_subject_lines(sig["subjects"]),
        "", "## Look at", "", *[f"- {item}" for item in _look_at(sig)],
        "", "## Acceptance criteria", "", *[f"{i}. {c}" for i, c in enumerate(_criteria(sig), 1)],
        "", f"[{marker_text}]",
    ]
    return "\n".join(lines)


def comment_text(sig: dict[str, Any], new_subjects: list[dict[str, Any]], now: float) -> str:
    return "\n".join([f"{len(new_subjects)} more affected (`wax failures sweep`, {_iso(now)}):", "",
                      *_subject_lines(new_subjects)])


# -------------------------------------------------------------------- px ----

class PxError(RuntimeError):
    """A px call that did not demonstrably succeed. One line, never a stderr tail.

    `code` is px's machine-readable refusal code when its --json answer carried
    one (`not_found`, ...), and `reason` px's own one-line error text."""

    def __init__(self, message: str, *, code: Optional[str] = None, reason: Optional[str] = None):
        super().__init__(message)
        self.code, self.reason = code, reason


# The ticket a filed row points at is gone from the board (deleted, or its board
# lost it). px >= the --marker release answers `code: not_found`; older px only
# says so in words, so the words count too.
_NOT_FOUND = re.compile(r"^no ticket \S+ (?:in workspace|on this board)\b")
GONE = "deleted"

# px resolves its board from the cwd's .project.json, but PLANE_BOARD and
# PLANE_WORKSPACE in the environment OUTRANK that binding (pilot config.js). A
# sweep run from an agent shell bound to another project would file there.
_BINDING_ENV = ("PLANE_BOARD", "PLANE_WORKSPACE")

LOCK_NAME = "failures-sweep.lock"


def px_path() -> Path:
    """$WAX_PX or ~/.local/bin/px, absolute: PATH never decides which px runs."""
    raw = os.environ.get("WAX_PX") or str(PX_DEFAULT)
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        raise PxError(f"WAX_PX must be an absolute path, got {raw!r}")
    return path


def repo_root() -> Path:
    """The HeyMa checkout: px resolves the HEYMA board from its `.project.json`."""
    for parent in (component.ROOT, *component.ROOT.parents):
        if (parent / ".project.json").is_file():
            return parent
    return paths.HEYMA


def _parse_json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Tolerate a stray line before the document: take the last top-level object.
    found, decoder = None, json.JSONDecoder()
    for match in re.finditer(r"(?m)^\{", text):
        try:
            obj, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            found = obj
    return found


def _first_str(*values: Any) -> Optional[str]:
    return next((v for v in values if isinstance(v, str) and v), None)


def created_from(doc: dict[str, Any]) -> dict[str, Any]:
    """Normalize `px task create --json`: {ok, created:{ticket..}, ref, id, adopted, was_created}.

    Read defensively: `created` is the ticket OBJECT there (was_created is the
    bool), but a bare boolean `created` and a nested `ticket` are accepted too.
    """
    inner = doc.get("created") if isinstance(doc.get("created"), dict) else {}
    ticket = doc.get("ticket") if isinstance(doc.get("ticket"), dict) else {}
    ref = _first_str(doc.get("ref"), inner.get("ref"), ticket.get("ref"))
    if not ref:
        raise PxError("px task create: answered ok but named no ticket ref")
    adopted = doc["adopted"] if isinstance(doc.get("adopted"), bool) else bool(inner.get("adopted"))
    if isinstance(doc.get("was_created"), bool):
        made = doc["was_created"]
    elif isinstance(doc.get("created"), bool):
        made = doc["created"]
    else:
        made = bool(inner.get("created", not adopted))
    return {"ref": ref, "id": _first_str(doc.get("id"), inner.get("id"), ticket.get("id")),
            "created": made, "adopted": adopted, "url": _first_str(inner.get("url"), doc.get("url"))}


class Px:
    """The only way this module reaches the board. Every call: absolute exe, cwd =
    the HeyMa repo (its .project.json binds HEYMA), a hard timeout, output captured."""

    def __init__(self, exe: Optional[Path] = None, cwd: Optional[Path] = None,
                 timeout: float = PX_TIMEOUT_S):
        self.exe, self.cwd, self.timeout = exe, cwd, float(timeout)
        self.calls = 0

    def run(self, *argv: str) -> dict[str, Any]:
        verb = " ".join(argv[:2]) if argv[:1] == ("task",) else (argv[0] if argv else "")
        exe = Path(self.exe) if self.exe else px_path()
        if not (exe.is_file() and os.access(exe, os.X_OK)):
            raise PxError(f"px {verb}: not an executable file: {exe}")
        self.calls += 1
        env = {k: v for k, v in os.environ.items() if k not in _BINDING_ENV}
        try:
            proc = subprocess.run([str(exe), *argv], cwd=str(self.cwd or repo_root()), capture_output=True,
                                  text=True, timeout=self.timeout, stdin=subprocess.DEVNULL, env=env)
        except subprocess.TimeoutExpired:
            raise PxError(f"px {verb}: timed out after {self.timeout:g}s") from None
        except OSError as exc:
            raise PxError(f"px {verb}: {type(exc).__name__}: {exc}") from None
        doc = _parse_json(proc.stdout)
        if proc.returncode != 0 or not isinstance(doc, dict) or doc.get("ok") is not True:
            said = _line(doc.get("error"), 200) if isinstance(doc, dict) else ""
            code = doc.get("code") if isinstance(doc, dict) and isinstance(doc.get("code"), str) else None
            why = said or _line(proc.stderr, 200) or _line(proc.stdout, 200) or "no output"
            raise PxError(f"px {verb}: exit {proc.returncode}: {why}", code=code, reason=said or None)
        return doc

    def create(self, title_text: str, body_text: str, marker_text: str) -> dict[str, Any]:
        return created_from(self.run("task", "create", title_text, "--state", "Todo", "--priority", "urgent",
                                     "-l", LABELS, "-d", body_text, "--marker", marker_text, "--json"))

    def show(self, ref: str) -> dict[str, Any]:
        """{ref, state_group, state}; state_group GONE when the board no longer has
        the ticket, so a deleted ticket is treated like a closed one instead of
        failing every later sweep on the same `px show`."""
        try:
            doc = self.run("show", ref, "--json")
        except PxError as exc:
            if exc.code == "not_found" or _NOT_FOUND.match(exc.reason or ""):
                return {"ref": ref, "state_group": GONE, "state": None}
            raise
        ticket = doc.get("ticket") if isinstance(doc.get("ticket"), dict) else doc
        group_name = _first_str(ticket.get("state_group"), ticket.get("group"))
        if not group_name:
            raise PxError(f"px show {ref}: the answer has no state_group")
        return {"ref": _first_str(ticket.get("ref")) or ref, "state_group": group_name,
                "state": _first_str(ticket.get("state_name"), ticket.get("lane"))}

    def comment(self, ref: str, message: str) -> dict[str, Any]:
        return self.run("comment", ref, "-m", message, "--json")


# ---------------------------------------------------------------- ledger ----

def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)


def _keys(raw: Any) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(k) for k in value] if isinstance(value, list) else []


def tickets(conn: Optional[sqlite3.Connection] = None) -> dict[str, dict[str, Any]]:
    """{fp: stored row}, or {} before the first sweep created the table."""
    conn = conn if conn is not None else ledger.connect()
    if TABLE not in _tables(conn):
        return {}
    out = {}
    for row in conn.execute("SELECT * FROM failure_tickets").fetchall():
        record = dict(row)
        record["subjects"] = _keys(record["subjects"])
        out[record["fp"]] = record
    return out


def _save(conn: sqlite3.Connection, sig: dict[str, Any], now: float, **fields: Any) -> None:
    if "subjects" in fields:
        fields["subjects"] = json.dumps(sorted(set(fields["subjects"])))
    stamp = _iso(now)
    exists = conn.execute("SELECT 1 FROM failure_tickets WHERE fp=?", (sig["fp"],)).fetchone()
    if exists:
        names = sorted(fields)
        conn.execute(f"UPDATE failure_tickets SET {', '.join(f'{n}=?' for n in names)}, updated_at=? WHERE fp=?",
                     (*(fields[n] for n in names), stamp, sig["fp"]))
        return
    row = {"generation": 1, "status": "uncertain", "subjects": "[]", "ticket_ref": None, "ticket_id": None,
           "last_error": None, **fields, "fp": sig["fp"], "kind": sig["kind"], "slug": sig["slug"],
           "reason_code": sig["reason_code"], "created_at": stamp, "updated_at": stamp}
    names = sorted(row)
    conn.execute(f"INSERT INTO failure_tickets({', '.join(names)}) VALUES({', '.join('?' for _ in names)})",
                 tuple(row[n] for n in names))


# ----------------------------------------------------------------- sweep ----

def _summary(sig: dict[str, Any]) -> dict[str, Any]:
    return {"fp": sig["fp"], "kind": sig["kind"], "slug": sig["slug"], "reason_code": sig["reason_code"],
            "affected": sig["affected"], "first_seen": sig["first_seen"], "last_seen": sig["last_seen"]}


def report(conn: Optional[sqlite3.Connection] = None, now: Optional[float] = None,
           since_s: float = DEFAULT_SINCE_S, grace_s: float = DEFAULT_GRACE_S) -> dict[str, Any]:
    """`wax failures [list]`: the collector, grouped, beside what is already ticketed. Never px."""
    conn = conn if conn is not None else ledger.connect()
    now = time.time() if now is None else float(now)
    errors: list[str] = []
    rows = collect(conn, now, since_s, grace_s, errors=errors)
    stored = tickets(conn)
    signatures = []
    for sig in group(rows):
        row = stored.get(sig["fp"])
        known = set(row["subjects"]) if row else set()
        signatures.append({
            **_summary(sig), "final": sig["final"], "title": title(sig),
            "ticket": None if not row else {
                "ref": row["ticket_ref"], "status": row["status"], "generation": row["generation"],
                "last_error": row["last_error"],
                "untracked": sum(1 for r in sig["subjects"] if r["subject"] not in known)},
            "subjects": [{k: r[k] for k in ("subject", "ref", "label", "at", "attempts", "exhausted_at",
                                            "final", "detail")} for r in sig["subjects"]],
        })
    waiting = [{k: r[k] for k in ("kind", "slug", "reason_code", "ref", "at")} for r in rows if not r["ticketable"]]
    return {"generated_at": _iso(now), "since_s": since_s, "grace_s": grace_s, "signatures": signatures,
            "waiting": waiting, "errors": errors}


@contextlib.contextmanager
def _sweep_lock() -> Iterator[bool]:
    """A non-blocking flock on var/failures-sweep.lock: True when held.

    Two sweeps at once (a Candybar run and a hand-run one) would both find no
    row, both ask px for the same marker before either created it, and file
    twins. The flock dies with its holder, so a crashed sweep never wedges it."""
    paths.VAR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(paths.VAR / LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def sweep(conn: Optional[sqlite3.Connection] = None, now: Optional[float] = None,
          since_s: float = DEFAULT_SINCE_S, grace_s: float = DEFAULT_GRACE_S, *,
          max_new: int = DEFAULT_MAX_NEW, dry_run: bool = False, px: Optional[Px] = None) -> dict[str, Any]:
    """File every ticket-worthy signature once (see _sweep). A real sweep holds
    var/failures-sweep.lock; while another holds it this one does nothing and
    says so (`locked: true`). A dry run writes nothing and takes no lock."""
    if dry_run:
        return _sweep(conn, now, since_s, grace_s, max_new=max_new, dry_run=True, px=px)
    with _sweep_lock() as held:
        if held:
            return _sweep(conn, now, since_s, grace_s, max_new=max_new, dry_run=False, px=px)
    log.warning("failures sweep: another sweep holds %s; nothing done", paths.VAR / LOCK_NAME)
    return {"generated_at": _iso(time.time() if now is None else float(now)), "dry_run": False,
            "since_s": since_s, "grace_s": grace_s, "max_new": int(max_new), "signatures": [], "capped": [],
            "collector_errors": [], "px_errors": 0, "px_calls": 0, "counts": {}, "locked": True}


def _sweep(conn: Optional[sqlite3.Connection] = None, now: Optional[float] = None,
           since_s: float = DEFAULT_SINCE_S, grace_s: float = DEFAULT_GRACE_S, *,
           max_new: int = DEFAULT_MAX_NEW, dry_run: bool = False, px: Optional[Px] = None) -> dict[str, Any]:
    """File every ticket-worthy signature once. Newest failure first.

    Per signature: no row -> create (at most `max_new` creates per run, and what the
    cap leaves is logged and returned, never silently dropped); `uncertain` (an
    earlier create failed or timed out) -> create again with the SAME marker, so px
    adopts the ticket if it exists; `filed` -> only subjects not yet on the ticket
    cost a px call: `px show`, then a comment, or a new generation when the ticket
    was closed or deleted. A px failure never raises: the row stays `uncertain` (or keeps its
    subjects for the next run) with `last_error`, and the sweep moves on.
    """
    conn = conn if conn is not None else ledger.connect()
    now = time.time() if now is None else float(now)
    errors: list[str] = []
    signatures = group(collect(conn, now, since_s, grace_s, errors=errors))
    stored = tickets(conn)
    if not dry_run:
        _ensure(conn)
    px = px if px is not None else Px()
    budget = max(0, int(max_new))
    out: dict[str, Any] = {"generated_at": _iso(now), "dry_run": bool(dry_run), "since_s": since_s,
                           "grace_s": grace_s, "max_new": int(max_new), "signatures": [], "capped": [],
                           "collector_errors": errors, "px_errors": 0}

    def file(sig: dict[str, Any], rec: dict[str, Any], *, generation: int, prior: Optional[list[str]],
             previous_ref: Optional[str], retry: bool) -> None:
        nonlocal budget
        mark = marker(sig["fp"], generation)
        rec.update(generation=generation, marker=mark)
        if budget <= 0:
            rec["action"] = "capped"
            out["capped"].append(_summary(sig))
            log.warning("failures sweep: --max %d reached; not filing %s %s: %s (%d affected, fp %s) "
                        "this run", int(max_new), sig["kind"], sig["slug"], sig["reason_code"],
                        sig["affected"], sig["fp"])
            return
        budget -= 1
        heading, text = title(sig), body(sig, generation=generation, marker_text=mark, previous_ref=previous_ref)
        if dry_run:
            rec.update(action="would_retry" if retry else ("would_refile" if previous_ref else "would_create"),
                       title=heading, body=text)
            return
        keys = [r["subject"] for r in sig["subjects"]]
        intent = prior if prior is not None else keys
        # Write-ahead: a crash or a timeout mid-call leaves `uncertain`, and the next
        # run asks px with the same marker instead of filing a twin.
        _save(conn, sig, now, generation=generation, status="uncertain", subjects=intent)
        try:
            made = px.create(heading, text, mark)
        except PxError as exc:
            _save(conn, sig, now, last_error=str(exc)[:500])
            raise
        # Adopted: the ticket on the board lists what the EARLIER attempt listed, so
        # keep that set and let the next run comment the difference.
        _save(conn, sig, now, status="filed", ticket_ref=made["ref"], ticket_id=made["id"],
              subjects=intent if made["adopted"] else keys, last_error=None)
        rec.update(ref=made["ref"], adopted=made["adopted"],
                   action="adopted" if made["adopted"] else ("refiled" if previous_ref else "created"))

    for sig in signatures:
        row = stored.get(sig["fp"])
        rec: dict[str, Any] = {**_summary(sig), "ref": row["ticket_ref"] if row else None,
                               "generation": row["generation"] if row else 1}
        out["signatures"].append(rec)
        try:
            if row is None:
                file(sig, rec, generation=1, prior=None, previous_ref=None, retry=False)
                continue
            if row["status"] != "filed":
                generation = int(row["generation"] or 1)
                file(sig, rec, generation=generation, prior=row["subjects"],
                     previous_ref=row["ticket_ref"] if generation > 1 else None, retry=True)
                continue
            known = set(row["subjects"])
            new = [r for r in sig["subjects"] if r["subject"] not in known]
            rec["new_subjects"] = len(new)
            if not new:
                rec["action"] = "unchanged"
                continue
            if dry_run:
                rec["action"] = "would_check"
                rec["detail"] = (f"px show {row['ticket_ref']}, then comment {len(new)} new subject(s), "
                                 "or file a new generation if it was closed")
                continue
            shown = px.show(row["ticket_ref"])
            rec["ticket_state_group"] = shown["state_group"]
            if shown["state_group"] in CLOSED_GROUPS or shown["state_group"] == GONE:
                file(sig, rec, generation=int(row["generation"] or 1) + 1, prior=None,
                     previous_ref=row["ticket_ref"], retry=False)
                continue
            px.comment(row["ticket_ref"], comment_text(sig, new, now))
            _save(conn, sig, now, subjects=[*row["subjects"], *(r["subject"] for r in new)], last_error=None)
            rec["action"] = "commented"
        except PxError as exc:
            out["px_errors"] += 1
            rec.update(action="error", error=str(exc))
            log.warning("failures sweep: %s %s: %s (fp %s): %s", sig["kind"], sig["slug"], sig["reason_code"],
                        sig["fp"], exc)
            if row is not None and row["status"] == "filed" and not dry_run:
                _save(conn, sig, now, last_error=str(exc)[:500])
    out["px_calls"] = getattr(px, "calls", None)
    counts: dict[str, int] = {}
    for rec in out["signatures"]:
        counts[rec.get("action", "unknown")] = counts.get(rec.get("action", "unknown"), 0) + 1
    out["counts"] = counts
    return out


# ------------------------------------------------------------- rendering ----

def _short(at: Any) -> str:
    return str(at or "?")[:16].replace("T", " ")


def render_list(rep: dict[str, Any]) -> str:
    lines = []
    for s in rep["signatures"]:
        ticket = s["ticket"]
        if ticket:
            status = ticket["status"] + (f" +{ticket['untracked']} new" if ticket["untracked"] else "")
            ref = ticket["ref"] or "-"
        else:
            status, ref = "not filed", "-"
        lines.append(f"{ref:<10} {status:<14} {s['kind']} {s['slug']}: {s['reason_code']}  "
                     f"({s['affected']} affected, last {_short(s['last_seen'])})  fp={s['fp']}")
    if not rep["signatures"]:
        lines.append(f"no ticket-worthy failures in the last {human_duration(rep['since_s'])}")
    filed = sum(1 for s in rep["signatures"] if s["ticket"] and s["ticket"]["status"] == "filed")
    lines.append(f"{len(rep['signatures'])} signature(s), {filed} filed; {len(rep['waiting'])} failure(s) still "
                 f"inside the {human_duration(rep['grace_s'])} grace")
    lines += [f"collector error: {e}" for e in rep["errors"]]
    return "\n".join(lines)


def render_sweep(res: dict[str, Any]) -> str:
    if res.get("locked"):
        return f"another `wax failures sweep` holds var/{LOCK_NAME}; nothing done this run"
    lines = []
    for rec in res["signatures"]:
        lines.append(f"{rec.get('action', '?'):<13} {rec.get('ref') or '-':<10} {rec['kind']} {rec['slug']}: "
                     f"{rec['reason_code']}  ({rec['affected']} affected)  fp={rec['fp']}")
        if rec.get("error"):
            lines.append(f"  error: {rec['error']}")
        if rec.get("detail"):
            lines.append(f"  {rec['detail']}")
    if res["dry_run"]:
        for rec in res["signatures"]:
            if rec.get("body"):
                lines += ["", f"--- {rec['title']}", f"--- marker [{rec['marker']}]", rec["body"]]
        lines.append("")
    if not res["signatures"]:
        lines.append(f"no ticket-worthy failures in the last {human_duration(res['since_s'])}")
    if res["capped"]:
        lines.append(f"--max {res['max_new']} reached: {len(res['capped'])} signature(s) not filed this run: "
                     + ", ".join(f"{c['kind']} {c['slug']}: {c['reason_code']}" for c in res["capped"]))
    counts = ", ".join(f"{n} {a}" for a, n in sorted(res["counts"].items())) or "nothing to do"
    lines.append(("dry run: " if res["dry_run"] else "") + counts
                 + (f"; {res['px_errors']} px error(s)" if res["px_errors"] else ""))
    lines += [f"collector error: {e}" for e in res["collector_errors"]]
    return "\n".join(lines)
