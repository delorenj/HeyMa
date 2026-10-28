"""Reconcile the vault and the ledger into the store; backfill; parity (design D.6).

`sweep()` makes the store agree with the vault without ever writing either:

  * every `*.md` directly in `paths.VAULT` (non-recursive; never `*.suspect.md`,
    `.wax-ep-*` or `*.tmp`) is matched to an identity by `wax-item-id`, then by
    path, then (a hand rename) by the head body of an identity whose note is
    gone; otherwise it gets a new identity and one `backfill` version;
  * a changed `content_sha256` mints a `reconcile` version (Obsidian edits, a
    missed hook); the same content only refreshes the stat;
  * a second copy of a Wax note that is not the ledger's canonical
    `transcripts.md_path` is an `alias` path, never a version;
  * a ledger transcript whose note is missing gets an identity with
    `note_missing_at` and no version, so the gap stays visible; a vanished note
    gets `note_missing_at` and is never deleted;
  * pass history is imported from the outbox `task.*` events and the ledger
    `passes` rows, idempotently, from durable watermarks.

Wax items whose flock is held, or that still have queued snapshots, are left to
the projector. `dry_run` does all of it inside one transaction and rolls it
back. `check=True` only reports parity (drift => CLI exit 2).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Optional

from .. import frontmatter, ledger, operations, paths, sentinel
from . import db, embed, extract, outbox, store, versioning

log = logging.getLogger("wax.transcriptions.reconcile")

OUTBOX_WATERMARK = "transcriptions.outbox_watermark"
PASSES_WATERMARK = "transcriptions.ledger_passes_watermark"
LAST_RECONCILE = "transcriptions.last_reconcile"
TERMINAL = ("completed", "skipped", "failed")


class _DryRun(Exception):
    pass


def list_notes(vault: Optional[Path] = None) -> list[Path]:
    root = Path(vault or paths.VAULT)
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.glob("*.md")):
        name = path.name
        if name.startswith(".") or name.endswith(".suspect.md") or name.endswith(".tmp") or not path.is_file():
            continue
        out.append(path)
    return out


def read_note(path: Path) -> str:
    """Exact text (no newline translation, unlike read_text), so note_sha256 is the file's."""
    return path.read_bytes().decode("utf-8", errors="replace")


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in ledger.connect().execute(sql, params).fetchall()]


def _ledger_view(root: str) -> dict[str, Any]:
    transcripts = {r["item_id"]: r for r in _rows("SELECT * FROM transcripts")}
    items = {r["item_id"]: r for r in _rows("SELECT * FROM items")}
    backups: dict[str, list[dict[str, Any]]] = {}
    for r in _rows("SELECT * FROM backups"):
        backups.setdefault(r["item_id"], []).append(r)
    canonical = {}
    by_note = {}
    for item_id, row in transcripts.items():
        rel = store.relpath_for(row.get("md_path") or "", root)
        canonical[item_id] = rel
        by_note.setdefault(rel, item_id)
    return {"transcripts": transcripts, "items": items, "backups": backups,
            "canonical": canonical, "by_note": by_note}


def _asr(transcript: Optional[dict[str, Any]]) -> dict[str, Any]:
    path = (transcript or {}).get("asr_path")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return extract.asr_summary(json.load(handle))
    except (OSError, ValueError):
        return {}


def _sidecar(note: Path) -> dict[str, Any]:
    meta = note.with_suffix(".meta.json")
    if not meta.is_file():
        return {}
    try:
        doc = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _store_view(conn: Any) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT t.transcript_id, t.note_relpath, t.note_mtime_ns, t.note_size, t.wax_item_id, t.note_missing_at, "
        "       c.content_sha256, c.body_sha256, b.word_count "
        "  FROM tx.transcript t LEFT JOIN tx.transcript_current c USING (transcript_id) "
        "  LEFT JOIN tx.transcript_body b ON b.body_sha256 = c.body_sha256").fetchall()
    keys = ("transcript_id", "note_relpath", "note_mtime_ns", "note_size", "wax_item_id", "note_missing_at",
            "content_sha256", "body_sha256", "body_words")
    identities = [dict(zip(keys, r)) for r in rows]
    aliases = {(r[0], r[1]) for r in conn.execute(
        "SELECT transcript_id, note_relpath FROM tx.transcript_path WHERE role = 'alias'").fetchall()}
    return {"identities": identities,
            "by_relpath": {i["note_relpath"]: i for i in identities},
            "by_wax": {i["wax_item_id"]: i for i in identities if i["wax_item_id"]},
            "aliases": aliases}


def sweep(*, dry_run: bool = False, limit: int = 0, check: bool = False, conn: Any = None,
          vault: Optional[Path] = None) -> dict[str, Any]:
    if check:
        return parity(conn=conn, vault=vault)
    own = conn is None
    if own:
        conn = db.connect("app", statement_timeout_ms=60000, autocommit=True)
    try:
        if dry_run:
            out: dict[str, Any] = {}
            try:
                with conn.transaction():
                    out = _sweep(conn, dry_run=True, limit=limit, vault=vault)
                    raise _DryRun()
            except _DryRun:
                pass
            return out
        return _sweep(conn, dry_run=False, limit=limit, vault=vault)
    finally:
        if own:
            conn.close()


def _sweep(conn: Any, *, dry_run: bool, limit: int, vault: Optional[Path]) -> dict[str, Any]:
    root = str(vault or paths.VAULT)
    files = list_notes(Path(root))
    on_disk = {p.name: p for p in files}
    led = _ledger_view(root)
    view = _store_view(conn)
    pending_items = set(outbox.pending_items(limit=100000))
    now_iso = sentinel.utcnow()
    stats: dict[str, Any] = {
        "dry_run": dry_run, "notes_on_disk": len(files), "unchanged": 0, "identities_created": 0,
        "backfill_versions": 0, "reconcile_versions": 0, "stat_only": 0, "aliases": 0, "renamed": 0,
        "skipped_busy": 0, "errors": [], "ledger_only_created": 0, "vanished_marked": 0, "processed": 0,
        "conflicts": 0,
    }
    # identities whose note is gone (now or before): candidates for a hand rename, by unique head body
    gone_by_body: dict[str, list[int]] = {}
    for ident in view["identities"]:
        if ident["note_relpath"] not in on_disk and ident["body_sha256"] and (ident["body_words"] or 0) >= 3:
            gone_by_body.setdefault(ident["body_sha256"], []).append(ident["transcript_id"])
    claimed: set[int] = set()
    seen_ids: set[int] = set()
    deferred_aliases: list[tuple[Path, str]] = []

    def process(path: Path) -> None:
        relpath = path.name
        st = path.stat()
        known = view["by_relpath"].get(relpath)
        if (known and known["note_missing_at"] is None and known["note_mtime_ns"] == st.st_mtime_ns
                and known["note_size"] == st.st_size and known["content_sha256"]):
            stats["unchanged"] += 1
            seen_ids.add(known["transcript_id"])
            return
        text = read_note(path)
        fm, body = frontmatter.split(text)
        fm_item = extract._text(fm.get(frontmatter.ITEM_KEY))
        fm_item = fm_item if fm_item and extract.ITEM_ID_RE.match(fm_item) else None
        item_id = fm_item or led["by_note"].get(relpath)
        if item_id and item_id in pending_items:
            stats["skipped_busy"] += 1
            return
        with ExitStack() as stack:
            if item_id:
                held = stack.enter_context(operations.try_item_lock(item_id))
                if not held:
                    stats["skipped_busy"] += 1
                    return
                # Decide only on what is true UNDER the flock. The note was read (and the pending set
                # taken at sweep start) before the lock: a pass can finish in between, leaving its own
                # snapshot queued (it must win: the projector owns that item) or projected (then the
                # text read above is older than the head, and minting it as `reconcile` would roll the
                # head back and leave two spurious versions in append-only history).
                if outbox.has_pending(item_id):
                    stats["skipped_busy"] += 1
                    return
                fresh = path.stat()
                if (fresh.st_mtime_ns, fresh.st_size) != (st.st_mtime_ns, st.st_size):
                    st = fresh
                    text = read_note(path)
                    fm, body = frontmatter.split(text)
                    again = extract._text(fm.get(frontmatter.ITEM_KEY))
                    again = again if again and extract.ITEM_ID_RE.match(again) else None
                    if again != fm_item:
                        stats["skipped_busy"] += 1      # its identity changed under us: next sweep
                        return
            # a second copy of a Wax note: alias, never a version
            if fm_item:
                ident = view["by_wax"].get(fm_item)
                canonical = led["canonical"].get(fm_item)
                alias = False
                if canonical and canonical != relpath and canonical in on_disk:
                    alias = True
                elif not canonical and ident and ident["note_relpath"] != relpath and ident["note_relpath"] in on_disk:
                    alias = True
                if alias:
                    if ident is None:
                        deferred_aliases.append((path, fm_item))
                        return
                    with conn.transaction():
                        store._upsert_path(conn, ident["transcript_id"], relpath, "alias")
                    stats["aliases"] += 1
                    return
            transcript_id = None
            if fm_item and fm_item in view["by_wax"]:
                transcript_id = view["by_wax"][fm_item]["transcript_id"]
            elif known:
                transcript_id = known["transcript_id"]
            else:
                body_sha = extract.sha256_text(body)
                candidates = [t for t in gone_by_body.get(body_sha, []) if t not in claimed]
                if len(candidates) == 1:
                    transcript_id = candidates[0]
                    claimed.add(transcript_id)
                    stats["renamed"] += 1
            ident = next((i for i in view["identities"] if i["transcript_id"] == transcript_id), None) \
                if transcript_id is not None else None
            producer = "reconcile" if ident and ident["content_sha256"] else "backfill"
            transcript = led["transcripts"].get(item_id) if item_id else None
            snap = {
                "seq": None, "item_id": item_id, "kind": "version", "idempotency_key": "",
                "md_path": str(path), "note_text": text, "created_at": now_iso,
                "payload": {
                    "producer_kind": producer, "produced_by": producer, "transcript_id": transcript_id,
                    "ledger": {"item": led["items"].get(item_id) if item_id else None,
                               "transcript": transcript,
                               "backups": led["backups"].get(item_id, []) if item_id else []},
                    "asr": _asr(transcript), "sidecar": _sidecar(path),
                    "note_stat": {"size": st.st_size, "mtime_ns": st.st_mtime_ns},
                    "vault_root": str(paths.VAULT) if vault is None else root, "relpath": relpath,
                    "ledger_maps_note": led["by_note"].get(relpath) == item_id and item_id is not None,
                    "item_is_wax": bool(item_id),
                },
            }
            try:
                with conn.transaction():
                    outcome = store.apply_snapshot(conn, snap)
            except (extract.ExtractError, store.ProjectionConflict) as exc:
                stats["errors"].append(f"{relpath}: {type(exc).__name__}: {exc}"[:300])
                if isinstance(exc, store.ProjectionConflict):
                    stats["conflicts"] += 1
                return
            stats["processed"] += 1
            if transcript_id is None:
                stats["identities_created"] += 1
            if outcome.transcript_id is not None:
                seen_ids.add(outcome.transcript_id)
                if fm_item:
                    view["by_wax"][fm_item] = {"transcript_id": outcome.transcript_id, "note_relpath": relpath,
                                               "content_sha256": True}
            if outcome.decision == versioning.MINT:
                stats["backfill_versions" if producer == "backfill" else "reconcile_versions"] += 1
            else:
                stats["stat_only"] += 1

    budget = limit if limit and limit > 0 else None
    for path in files:
        if budget is not None and stats["processed"] + stats["aliases"] >= budget:
            break
        try:
            process(path)
        except OSError as exc:
            stats["errors"].append(f"{path.name}: {type(exc).__name__}: {exc}"[:300])
        except Exception as exc:  # noqa: BLE001
            if db.is_outage(exc):
                raise
            stats["errors"].append(f"{path.name}: {type(exc).__name__}: {str(exc)[:200]}")
    for path, fm_item in deferred_aliases:
        ident = view["by_wax"].get(fm_item)
        if ident is None:
            continue
        with conn.transaction():
            store._upsert_path(conn, ident["transcript_id"], path.name, "alias")
        stats["aliases"] += 1

    complete = budget is None
    if complete:
        stats.update(_ledger_only(conn, led, root))
        stats["vanished_marked"] = _mark_vanished(conn, on_disk)
    history = import_history(conn, dry_run=dry_run)
    stats.update(history)
    if not dry_run:
        ledger.meta_set(LAST_RECONCILE, json.dumps({"at": sentinel.utcnow(), "processed": stats["processed"],
                                                     "errors": len(stats["errors"])}))
    return stats


def _ledger_only(conn: Any, led: dict[str, Any], root: str) -> dict[str, Any]:
    created = conflicts = 0
    known = {r[0] for r in conn.execute("SELECT wax_item_id FROM tx.transcript WHERE wax_item_id IS NOT NULL")}
    for item_id, row in led["transcripts"].items():
        if item_id in known or not extract.ITEM_ID_RE.match(item_id or ""):
            continue
        md = row.get("md_path") or ""
        if md and os.path.exists(md):
            continue          # the note exists; the file walk owns it (or skipped it as busy)
        relpath = store.relpath_for(md, root)
        with conn.transaction():
            clash = conn.execute("SELECT 1 FROM tx.transcript WHERE note_relpath = %s", (relpath,)).fetchone()
            if clash:
                conflicts += 1
                continue
            conn.execute(
                "INSERT INTO tx.transcript (wax_item_id, origin, vault_root, note_relpath, note_missing_at, "
                "ledger_created_at, audio_duration_s, asr_duration_s, duration_ratio, asr_word_count, engine_model, "
                "asr_path, processing_seconds, ledger_origin, ledger_state, source_sha256) "
                "VALUES (%s, 'wax', %s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (item_id, str(paths.VAULT), relpath, extract.parse_ts(row.get("created_at"))[0],
                 row.get("audio_duration"), row.get("asr_duration"), row.get("duration_ratio"),
                 row.get("word_count"), row.get("engine_model"), row.get("asr_path"),
                 row.get("processing_seconds"), (led["items"].get(item_id) or {}).get("origin"),
                 (led["items"].get(item_id) or {}).get("state"),
                 extract._sha((led["items"].get(item_id) or {}).get("sha256"))))
            created += 1
    return {"ledger_only_created": created, "ledger_only_conflicts": conflicts}


def _mark_vanished(conn: Any, on_disk: dict[str, Path]) -> int:
    rows = conn.execute("SELECT transcript_id, note_relpath FROM tx.transcript WHERE note_missing_at IS NULL"
                        ).fetchall()
    gone = [tid for tid, rel in rows if rel not in on_disk]
    if gone:
        with conn.transaction():
            conn.execute("UPDATE tx.transcript SET note_missing_at = now(), updated_at = now() "
                         "WHERE transcript_id = ANY(%s) AND note_missing_at IS NULL", (gone,))
    return len(gone)


# ---------------------------------------------------------------- history --

def _state_from_subject(subject: str) -> Optional[str]:
    for state in TERMINAL:
        if subject.endswith(f".audio.task.{state}"):
            return state
    return None


def import_history(conn: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """Outbox task.* events, then ledger passes rows, into pass_run (idempotent, watermarked)."""
    lconn = ledger.connect()
    wax_ids = {r[0]: r[1] for r in conn.execute(
        "SELECT wax_item_id, transcript_id FROM tx.transcript WHERE wax_item_id IS NOT NULL")}
    ledger_items = {r["item_id"] for r in lconn.execute("SELECT item_id FROM transcripts")}
    stats = {"history_outbox_imported": 0, "history_outbox_seen": 0, "history_ledger_imported": 0}
    try:
        mark = int(ledger.meta_get(OUTBOX_WATERMARK) or 0)
    except (TypeError, ValueError):
        mark = 0
    has_outbox = lconn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbox'").fetchone()
    rows = lconn.execute(
        "SELECT id, subject, envelope FROM outbox WHERE id > ? AND (subject LIKE '%.audio.task.completed' "
        "OR subject LIKE '%.audio.task.failed' OR subject LIKE '%.audio.task.skipped') ORDER BY id",
        (mark,)).fetchall() if has_outbox else []
    new_mark = mark
    blocked = None
    for row in rows:
        stats["history_outbox_seen"] += 1
        state = _state_from_subject(row["subject"])
        try:
            env = json.loads(row["envelope"])
        except (TypeError, ValueError):
            env = {}
        data = env.get("data") if isinstance(env.get("data"), dict) else {}
        item_id, cid = data.get("item_id"), data.get("command_id")
        tid = wax_ids.get(item_id)
        if tid is None:
            if item_id in ledger_items and blocked is None:
                blocked = row["id"]          # its identity is not there yet; retry from here
            if blocked is None:
                new_mark = row["id"]
            continue
        if not state or not cid:
            if blocked is None:
                new_mark = row["id"]
            continue
        led = lconn.execute("SELECT * FROM passes WHERE command_id = ?", (cid,)).fetchone()
        result = {}
        if led is not None:
            try:
                result = json.loads(led["result"] or "{}") if "result" in led.keys() else {}
            except (TypeError, ValueError):
                result = {}
        facts = {
            "slug": data.get("ep_slug"), "pass_version": data.get("pass_version"), "attempt": data.get("attempt"),
            "command_id": cid, "state": state,
            "reason_code": data.get("reason_code") or (led["reason_code"] if led is not None and "reason_code" in led.keys() else None),
            "detail": data.get("stderr_tail"), "changed_fields": data.get("changed_fields") or [],
            "duration_s": data.get("duration_s"),
            "definition_id": led["definition_id"] if led is not None and "definition_id" in led.keys() else None,
            "evidence": extract.clean_evidence(result.get("evidence")) if isinstance(result, dict) else None,
        }
        if state == "completed":
            facts["reason_code"] = None
        finished = extract.parse_ts(env.get("time"))[0] or dt.datetime.now(dt.timezone.utc)
        with conn.transaction():
            if store._pass_run(conn, transcript_id=tid, key=versioning.pass_key(cid), facts=facts,
                               finished_at=finished, version_id=None, outcome=None, source="outbox"):
                stats["history_outbox_imported"] += 1
        if blocked is None:
            new_mark = row["id"]

    try:
        pmark = ledger.meta_get(PASSES_WATERMARK) or ""
    except Exception:  # noqa: BLE001
        pmark = ""
    newest = pmark
    pblocked = False
    for led in lconn.execute("SELECT * FROM passes WHERE updated_at > ? AND state IN ('completed','skipped','failed') "
                             "ORDER BY updated_at", (pmark,)).fetchall():
        led = dict(led)
        tid = wax_ids.get(led["item_id"])
        if tid is None:
            # An item that will get an identity (it has a ledger transcript) holds the
            # watermark here; anything else (documents, vanished items) never will.
            if led["item_id"] in ledger_items:
                pblocked = True
            elif not pblocked:
                newest = max(newest, led["updated_at"] or "")
            continue
        try:
            result = json.loads(led.get("result") or "{}")
        except (TypeError, ValueError):
            result = {}
        key = versioning.pass_snapshot_key(item_id=led["item_id"], slug=led["ep_slug"], state=led["state"],
                                           pass_version=int(led.get("version") or 1),
                                           attempt=int(led.get("attempt") or 1), command_id=led.get("command_id"),
                                           reason_code=led.get("reason_code"))
        facts = {"slug": led["ep_slug"], "pass_version": led.get("version"), "attempt": led.get("attempt"),
                 "command_id": led.get("command_id"), "state": led["state"],
                 "reason_code": None if led["state"] == "completed" else led.get("reason_code"),
                 "detail": None if led["state"] == "completed" else led.get("detail"),
                 "definition_id": led.get("definition_id"),
                 "evidence": extract.clean_evidence(result.get("evidence")) if isinstance(result, dict) else None}
        finished = extract.parse_ts(led.get("updated_at"))[0] or dt.datetime.now(dt.timezone.utc)
        with conn.transaction():
            if store._pass_run(conn, transcript_id=tid, key=key, facts=facts, finished_at=finished,
                               version_id=None, outcome=None, source="ledger"):
                stats["history_ledger_imported"] += 1
        if not pblocked:
            newest = max(newest, led["updated_at"] or "")
    if not dry_run:
        ledger.meta_set(OUTBOX_WATERMARK, str(new_mark))
        if newest:
            ledger.meta_set(PASSES_WATERMARK, newest)
    stats["history_outbox_watermark"] = new_mark
    return stats


# ---------------------------------------------------------------- parity --

def parity(*, conn: Any = None, vault: Optional[Path] = None) -> dict[str, Any]:
    """Disk, ledger and store side by side; `drift` is true on any mismatch."""
    own = conn is None
    if own:
        conn = db.connect("app", statement_timeout_ms=60000, autocommit=True)
    try:
        root = str(vault or paths.VAULT)
        files = list_notes(Path(root))
        on_disk = {p.name for p in files}
        alias_paths = {r[0] for r in conn.execute(
            "SELECT DISTINCT note_relpath FROM tx.transcript_path WHERE role = 'alias'")} & on_disk
        by_origin = {r[0]: int(r[1]) for r in conn.execute(
            "SELECT origin, count(*) FROM tx.transcript GROUP BY origin ORDER BY origin")}
        one = lambda sql: int(conn.execute(sql).fetchone()[0])  # noqa: E731
        wax_ids = {r[0] for r in conn.execute("SELECT wax_item_id FROM tx.transcript WHERE wax_item_id IS NOT NULL")}
        ledger_ids = {r["item_id"] for r in ledger.connect().execute("SELECT item_id FROM transcripts")}
        pending = embed.pending_counts(conn)
        out = {
            "notes_on_disk": len(files),
            "canonical_notes": len(files) - len(alias_paths),
            "aliases": len(alias_paths),
            "identities": sum(by_origin.values()),
            "identities_by_origin": by_origin,
            "identities_with_wax_id": len(wax_ids),
            "identities_with_note": one("SELECT count(*) FROM tx.transcript WHERE note_missing_at IS NULL"),
            "identities_note_missing": one("SELECT count(*) FROM tx.transcript WHERE note_missing_at IS NOT NULL"),
            "ledger_transcripts": len(ledger_ids),
            "ledger_transcripts_without_identity": len(ledger_ids - wax_ids),
            "heads": one("SELECT count(*) FROM tx.transcript_current"),
            "heads_with_note": one("SELECT count(*) FROM tx.transcript_current c JOIN tx.transcript t "
                                   "USING (transcript_id) WHERE t.note_missing_at IS NULL"),
            "chunks": one("SELECT count(*) FROM tx.chunk_current"),
            "chunks_embedded": one("SELECT count(*) FROM tx.chunk_vector_current"),
            "doc_pending": pending["docs_pending"],
            "invariant_violations": one("SELECT count(*) FROM tx.check_invariants()"),
            "pending_snapshots": outbox.backlog()["queued"],
        }
        out["drift"] = bool(
            out["heads_with_note"] != out["identities_with_note"]
            or out["identities_with_note"] != out["canonical_notes"]
            or out["ledger_transcripts_without_identity"]
            or out["chunks_embedded"] != out["chunks"]
            or out["doc_pending"]
            or out["invariant_violations"])
        return out
    finally:
        if own:
            conn.close()
