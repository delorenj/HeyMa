"""Projector: SQLite snapshots -> Postgres versions (design D.4).

One Postgres transaction per snapshot. The steps:

  1. extract (pure) and verify `frontmatter_block + body == note_text`;
  2. resolve the identity `FOR UPDATE` (wax_item_id, then note_relpath), insert
     it if absent, coalesce identity columns (observed facts: newest wins;
     transcribe-time facts: first non-null wins);
  3. follow a rename (`note_relpath` moves, `transcript_path` keeps the old one);
  4. insert the content-addressed body once (and its speakers);
  5. `versioning.decide()` against the head;
  6. mint: the version row (the trigger assigns `version_no` and advances the
     head pointer), its children, `transcript_current`, and - only when the
     head body changed - the chunk swap plus vectors from the embedding cache;
  7. pass snapshots also record `pass_run`;
  8. commit (the deferred FKs prove head and pointer agree), then mark SQLite.

Connections are autocommit; every atomic unit is an explicit
`conn.transaction()` block, so no statement can leave an implicit transaction
open around the next block.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from .. import operations, paths
from . import chunking, config, db, extract, outbox, versioning

log = logging.getLogger("wax.transcriptions.store")


class ProjectionConflict(RuntimeError):
    """Two identities claim one note path; left pending for an operator."""


@dataclass
class Outcome:
    decision: str                      # mint | no_change | skip | duplicate | pass_run
    transcript_id: Optional[int] = None
    version_id: Optional[int] = None
    version_no: Optional[int] = None
    chunks_swapped: bool = False
    chunk_count: Optional[int] = None


# ---------------------------------------------------------------- helpers --

def _jsonb(value: Any) -> Any:
    from psycopg.types.json import Jsonb
    return None if value is None else Jsonb(extract.to_jsonable(value))


def _uuid(value: Any) -> Optional[uuid.UUID]:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> Optional[dt.datetime]:
    stamp, _ = extract.parse_ts(value)
    return stamp


def vault_root() -> str:
    """Wax's own spelling of the vault ('/home/delorenj/d/Transcripts'), never resolved."""
    return str(paths.VAULT)


def relpath_for(md_path: Any, root: Optional[str] = None) -> str:
    """The note path relative to the vault root; the bare name when it lies elsewhere."""
    path = Path(str(md_path))
    base = Path(root or vault_root())
    for candidate in (base, base.resolve()):
        try:
            return path.relative_to(candidate).as_posix()
        except ValueError:
            pass
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except (ValueError, OSError):
        return path.name


_url_cache: dict[tuple[str, str, str], Optional[str]] = {}


def audio_url(bucket: Optional[str], key: Optional[str]) -> Optional[str]:
    """Unsigned path-style URL, the `finalize.audio_url` formula, without ever shelling out to mc."""
    if not bucket or not key:
        return None
    endpoint = (config.s3_endpoint() or "").rstrip("/")
    cache_key = (endpoint, bucket, key)
    if cache_key in _url_cache:
        return _url_cache[cache_key]
    url = None
    if endpoint:
        parts = urlsplit(endpoint)
        if parts.scheme in ("http", "https") and parts.hostname and not parts.username and not parts.password:
            url = endpoint + "/" + quote(bucket, safe="") + "/" + quote(key, safe="/")
    _url_cache[cache_key] = url
    return url


def active_model_id(conn: Any) -> Optional[int]:
    row = conn.execute("SELECT model_id FROM tx.embedding_model WHERE is_active").fetchone()
    return int(row[0]) if row else None


# ---------------------------------------------------------------- identity --

_IDENTITY_SELECT = ("SELECT transcript_id, wax_item_id, note_relpath, current_version_id, origin "
                    "FROM tx.transcript WHERE {} FOR UPDATE")


def _identity_values(ext: extract.Extraction) -> dict[str, Any]:
    values = dict(ext.identity)
    values["audio_https_url"] = audio_url(values.get("audio_s3_bucket"), values.get("audio_s3_key"))
    return values


def _insert_identity(conn: Any, values: dict[str, Any]) -> int:
    columns = [c for c in values if c != "note_missing_at"]
    sql = (f"INSERT INTO tx.transcript ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) "
           "RETURNING transcript_id")
    return int(conn.execute(sql, [values[c] for c in columns]).fetchone()[0])


def _update_identity(conn: Any, transcript_id: int, values: dict[str, Any], *, observed: bool = True) -> None:
    sets, params = [], []
    for column in extract.OBSERVED_IDENTITY + ("audio_https_url",):
        if column in values:
            sets.append(f"{column} = COALESCE(%s, {column})")
            params.append(values[column])
    for column in extract.FIRST_IDENTITY:
        if column in values:
            sets.append(f"{column} = COALESCE({column}, %s)")
            params.append(values[column])
    if values.get("wax_item_id"):
        sets.append("wax_item_id = COALESCE(wax_item_id, %s)")
        params.append(values["wax_item_id"])
    if "vault_root" in values and values["vault_root"]:
        sets.append("vault_root = %s")
        params.append(values["vault_root"])
    if observed:
        sets.append("note_missing_at = NULL")
    sets.append("updated_at = now()")
    params.append(transcript_id)
    conn.execute(f"UPDATE tx.transcript SET {', '.join(sets)} WHERE transcript_id = %s", params)


def _upsert_path(conn: Any, transcript_id: int, relpath: str, role: str = "canonical",
                 first_version_id: Optional[int] = None) -> None:
    conn.execute(
        "INSERT INTO tx.transcript_path (transcript_id, note_relpath, role, first_version_id) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (transcript_id, note_relpath) DO UPDATE SET "
        "last_seen_at = now(), "
        "role = CASE WHEN EXCLUDED.role = 'canonical' THEN 'canonical' ELSE tx.transcript_path.role END, "
        "first_version_id = COALESCE(tx.transcript_path.first_version_id, EXCLUDED.first_version_id)",
        (transcript_id, relpath, role, first_version_id))


def resolve_identity(conn: Any, *, wax_item_id: Optional[str], relpath: str,
                     transcript_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    """The identity row (locked FOR UPDATE), or None. Matching order: forced id, wax_item_id, path."""
    cols = ("transcript_id", "wax_item_id", "note_relpath", "current_version_id", "origin")
    if transcript_id is not None:
        row = conn.execute(_IDENTITY_SELECT.format("transcript_id = %s"), (transcript_id,)).fetchone()
        return dict(zip(cols, row)) if row else None
    if wax_item_id:
        row = conn.execute(_IDENTITY_SELECT.format("wax_item_id = %s"), (wax_item_id,)).fetchone()
        if row:
            return dict(zip(cols, row))
    row = conn.execute(_IDENTITY_SELECT.format("note_relpath = %s"), (relpath,)).fetchone()
    if row:
        found = dict(zip(cols, row))
        if wax_item_id and found["wax_item_id"] and found["wax_item_id"] != wax_item_id:
            raise ProjectionConflict(f"{relpath} belongs to item {found['wax_item_id']}, not {wax_item_id}")
        return found
    return None


def _move(conn: Any, identity: dict[str, Any], relpath: str) -> None:
    """Follow a rename: the identity's canonical path moves, the old one stays in history."""
    old = identity["note_relpath"]
    if old == relpath:
        return
    other = conn.execute("SELECT transcript_id, wax_item_id FROM tx.transcript "
                         "WHERE note_relpath = %s AND transcript_id <> %s FOR UPDATE",
                         (relpath, identity["transcript_id"])).fetchone()
    if other:
        raise ProjectionConflict(f"rename of transcript {identity['transcript_id']} to {relpath} collides "
                                 f"with transcript {other[0]}")
    _upsert_path(conn, identity["transcript_id"], old, "canonical")
    conn.execute("UPDATE tx.transcript SET note_relpath = %s, updated_at = now() WHERE transcript_id = %s",
                 (relpath, identity["transcript_id"]))
    identity["note_relpath"] = relpath


# ---------------------------------------------------------------- content --

def _insert_body(conn: Any, ext: extract.Extraction) -> bool:
    row = ext.body_row
    columns = list(extract.TRANSCRIPT_BODY_COLUMNS)
    inserted = conn.execute(
        f"INSERT INTO tx.transcript_body ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) "
        "ON CONFLICT (body_sha256) DO NOTHING RETURNING body_sha256",
        [row[c] for c in columns]).fetchone()
    if inserted and ext.speakers:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tx.body_speaker (body_sha256, speaker_label, turn_count, word_count, "
                "first_start_s, last_start_s) VALUES (%s, %s, %s, %s, %s, %s)",
                [(ext.body_sha256, s["speaker_label"], s["turn_count"], s["word_count"],
                  s["first_start_s"], s["last_start_s"]) for s in ext.speakers])
    return bool(inserted)


def head(conn: Any, transcript_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT c.version_id, c.version_no, c.produced_by, c.body_sha256, c.content_sha256, "
        "c.doc_input_sha256, c.chunk_count, v.pass_version "
        "FROM tx.transcript_current c JOIN tx.transcript_version v ON v.version_id = c.version_id "
        "WHERE c.transcript_id = %s", (transcript_id,)).fetchone()
    if not row:
        return None
    keys = ("version_id", "version_no", "produced_by", "body_sha256", "content_sha256",
            "doc_input_sha256", "chunk_count", "pass_version")
    return dict(zip(keys, row))


_VERSION_PASS_COLUMNS = ("pass_version", "pass_attempt", "pass_state", "pass_reason_code", "command_id",
                         "definition_id", "plan_id", "pipeline_name", "pipeline_definition_id", "model",
                         "served_by", "route", "fell_through", "evidence", "changed_fields",
                         "pass_duration_s")


def _version_row(ext: extract.Extraction, *, transcript_id: int, key: str, producer_kind: str,
                 produced_by: str, facts: dict[str, Any], observed_at: dt.datetime) -> dict[str, Any]:
    row: dict[str, Any] = {
        "transcript_id": transcript_id, "idempotency_key": key, "producer_kind": producer_kind,
        "produced_by": produced_by, "observed_at": observed_at, "note_relpath": ext.relpath,
        "note_sha256": ext.note_sha256, "note_bytes": ext.note_bytes,
        "frontmatter_block": ext.frontmatter_block, "frontmatter": _jsonb(ext.frontmatter_json),
        "meta_sha256": ext.meta_sha256, "body_sha256": ext.body_sha256, "content_sha256": ext.content_sha256,
    }
    for column in _VERSION_PASS_COLUMNS:
        value = facts.get(column)
        if column in ("fell_through", "evidence"):
            value = _jsonb(value)
        elif column == "command_id":
            value = _uuid(value)
        elif column == "changed_fields":
            value = [str(v) for v in (value or [])]
        row[column] = value
    for column, value in ext.version.items():
        row[column] = _jsonb(value) if column == "vault_extra" else value
    return row


def _insert_version(conn: Any, row: dict[str, Any]) -> Optional[tuple[int, int]]:
    columns = list(row)
    found = conn.execute(
        f"INSERT INTO tx.transcript_version ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) "
        "ON CONFLICT (transcript_id, idempotency_key) DO NOTHING RETURNING version_id, version_no",
        [row[c] for c in columns]).fetchone()
    return (int(found[0]), int(found[1])) if found else None


def _insert_children(conn: Any, version_id: int, ext: extract.Extraction) -> None:
    with conn.cursor() as cur:
        if ext.tag_rows:
            cur.executemany("INSERT INTO tx.version_tag (version_id, source, ordinal, tag) VALUES (%s, %s, %s, %s)",
                            [(version_id, s, i, t) for s, i, t in ext.tag_rows])
        if ext.project_rows:
            cur.executemany("INSERT INTO tx.version_project (version_id, ordinal, project_id, name, probability) "
                            "VALUES (%s, %s, %s, %s, %s)", [(version_id, *r) for r in ext.project_rows])
        if ext.ticket_rows:
            cur.executemany("INSERT INTO tx.version_ticket (version_id, ordinal, project_id, description, "
                            "transcript_excerpt) VALUES (%s, %s, %s, %s, %s)",
                            [(version_id, *r) for r in ext.ticket_rows])
        if ext.pass_states:
            cur.executemany(
                "INSERT INTO tx.version_pass_state (version_id, slug, state, at, pass_version, attempt, command_id, "
                "reason_code, detail, model, served_by, route, fell_through) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(version_id, p["slug"], p["state"], p["at"], p["pass_version"], p["attempt"], _uuid(p["command_id"]),
                  p["reason_code"], p["detail"], p["model"], p["served_by"], p["route"], _jsonb(p["fell_through"]))
                 for p in ext.pass_states])


def _upsert_current(conn: Any, transcript_id: int, version_id: int, version_no: int, produced_by: str,
                    ext: extract.Extraction) -> None:
    values = {
        "transcript_id": transcript_id, "version_id": version_id, "version_no": version_no,
        "produced_by": produced_by, "body_sha256": ext.body_sha256, "content_sha256": ext.content_sha256,
        "display_title": ext.display_title, "title": ext.version.get("title"),
        "title_slug": ext.version.get("title_slug"), "summary": ext.version.get("summary"),
        "classification": ext.version.get("classification"), "tags": ext.current_tags,
        "tags_text": " ".join(ext.current_tags), "project_ids": ext.version.get("project_ids") or [],
        "diarized": ext.version.get("diarized"), "speaker_count": ext.body_row["speaker_count"],
        "word_count": ext.word_count, "doc_input": ext.doc_input, "doc_input_sha256": ext.doc_input_sha256,
    }
    columns = list(values)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != "transcript_id")
    conn.execute(
        f"INSERT INTO tx.transcript_current ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) "
        f"ON CONFLICT (transcript_id) DO UPDATE SET {updates}, updated_at = now()",
        [values[c] for c in columns])


def swap_chunks(conn: Any, transcript_id: int, ext: extract.Extraction, *, force: bool = False) -> Optional[int]:
    """Make chunk_current hold exactly this body's chunks; returns the new count, None if unchanged."""
    total, matching = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE body_sha256 = %s AND chunker_version = %s) "
        "FROM tx.chunk_current WHERE transcript_id = %s",
        (ext.body_sha256, chunking.CHUNKER_VERSION, transcript_id)).fetchone()
    if not ext.searchable:
        if total:
            conn.execute("DELETE FROM tx.chunk_current WHERE transcript_id = %s", (transcript_id,))
            return 0
        return None
    chunks = None
    if not force and total and total == matching:
        return None
    if not force and total == 0:
        chunks = ext.chunks()
        if not chunks:
            return None
    chunks = chunks if chunks is not None else ext.chunks()
    if total:
        conn.execute("DELETE FROM tx.chunk_current WHERE transcript_id = %s", (transcript_id,))
    if chunks:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tx.chunk_current (transcript_id, body_sha256, chunker_version, ordinal, kind, "
                "char_start, char_end, text, search_text, search_sha256, token_estimate, start_s, end_s, "
                "speakers, turn_count) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(transcript_id, ext.body_sha256, chunking.CHUNKER_VERSION, c.ordinal, c.kind, c.char_start,
                  c.char_end, c.text, c.search_text, c.search_sha256, c.token_estimate, c.start_s, c.end_s,
                  c.speakers, c.turn_count) for c in chunks])
        model_id = active_model_id(conn)
        if model_id is not None:
            conn.execute(
                "INSERT INTO tx.chunk_vector_current (chunk_id, transcript_id, model_id, embedding) "
                "SELECT c.chunk_id, c.transcript_id, e.model_id, e.embedding "
                "  FROM tx.chunk_current c "
                "  JOIN tx.embedding_cache e ON e.model_id = %s AND e.input_sha256 = c.search_sha256 "
                " WHERE c.transcript_id = %s", (model_id, transcript_id))
    return len(chunks)


def sync_doc_vector(conn: Any, transcript_id: int, ext: extract.Extraction) -> None:
    """Keep doc_vector on the head's doc_input: fill from cache or drop the stale row."""
    if not ext.searchable or not ext.doc_input_sha256:
        conn.execute("DELETE FROM tx.doc_vector WHERE transcript_id = %s", (transcript_id,))
        return
    conn.execute("DELETE FROM tx.doc_vector WHERE transcript_id = %s AND input_sha256 <> %s",
                 (transcript_id, ext.doc_input_sha256))
    model_id = active_model_id(conn)
    if model_id is not None:
        conn.execute(
            "INSERT INTO tx.doc_vector (transcript_id, input_sha256, model_id, embedding) "
            "SELECT %s, e.input_sha256, e.model_id, e.embedding FROM tx.embedding_cache e "
            " WHERE e.model_id = %s AND e.input_sha256 = %s ON CONFLICT (transcript_id) DO NOTHING",
            (transcript_id, model_id, ext.doc_input_sha256))


def _pass_run(conn: Any, *, transcript_id: int, key: str, facts: dict[str, Any], finished_at: dt.datetime,
              version_id: Optional[int], outcome: Optional[str], source: str = "hook") -> bool:
    row = conn.execute(
        "INSERT INTO tx.pass_run (transcript_id, idempotency_key, slug, pass_version, attempt, command_id, state, "
        "reason_code, detail, definition_id, plan_id, pipeline_name, model, served_by, route, fell_through, "
        "evidence, changed_fields, duration_s, finished_at, version_id, outcome, source) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (idempotency_key) DO NOTHING RETURNING pass_run_id",
        (transcript_id, key, facts.get("slug"), facts.get("pass_version"), facts.get("attempt"),
         _uuid(facts.get("command_id")), facts.get("state"), facts.get("reason_code"),
         (facts.get("detail") or None) and str(facts.get("detail"))[:300], facts.get("definition_id"),
         facts.get("plan_id"), facts.get("pipeline_name"), facts.get("model"), facts.get("served_by"),
         facts.get("route"), _jsonb(facts.get("fell_through")), _jsonb(facts.get("evidence")),
         [str(v) for v in facts.get("changed_fields") or []], facts.get("duration_s"), finished_at,
         version_id, outcome, source)).fetchone()
    return row is not None


# ---------------------------------------------------------------- snapshot --

def _facts(payload: dict[str, Any]) -> dict[str, Any]:
    """Pass facts flattened into version/pass_run column names."""
    p = dict(payload.get("pass") or {})
    plan = payload.get("plan") or {}
    evidence = extract.clean_evidence(p.get("evidence"))
    fell = None
    if isinstance(evidence, dict) and isinstance(evidence.get("fallbacks"), list):
        fell = [{"model": str(i.get("model")), "reason_code": str(i.get("reason_code") or "")}
                for i in evidence["fallbacks"] if isinstance(i, dict) and i.get("model")] or None
    if fell is None:
        fell = extract.parse_fell_through(p.get("fell_through"))
    model = p.get("model") or (evidence.get("model") if isinstance(evidence, dict) else None)
    served = p.get("served_by") or (evidence.get("served_by") if isinstance(evidence, dict) else None)
    route = p.get("route") or (evidence.get("route") if isinstance(evidence, dict) else None)
    return {
        "slug": p.get("slug"), "pass_version": p.get("pass_version"), "attempt": p.get("attempt"),
        "pass_attempt": p.get("attempt"), "state": p.get("state"),
        "pass_state": p.get("state") if p.get("state") in ("completed", "skipped") else None,
        "reason_code": p.get("reason_code"),
        "pass_reason_code": p.get("reason_code") if p.get("state") == "skipped" else None,
        "detail": p.get("detail"), "command_id": p.get("command_id"), "definition_id": p.get("definition_id"),
        "plan_id": plan.get("plan_id"), "pipeline_name": plan.get("pipeline_name"),
        "pipeline_definition_id": plan.get("pipeline_definition_id"),
        "model": str(model)[:200] if model else None, "served_by": str(served)[:200] if served else None,
        "route": str(route)[:200] if route else None, "fell_through": fell, "evidence": evidence,
        "changed_fields": p.get("changed_fields") or [], "duration_s": p.get("duration_s"),
        "pass_duration_s": p.get("duration_s"),
    }


def extraction_for(snap: dict[str, Any]) -> extract.Extraction:
    payload = snap.get("payload") or {}
    root = payload.get("vault_root") or vault_root()
    relpath = payload.get("relpath") or relpath_for(snap.get("md_path"), root)
    evidence = (payload.get("pass") or {}).get("evidence")
    return extract.from_note(
        snap["note_text"], relpath, ledger=payload.get("ledger"), asr=payload.get("asr"),
        sidecar=payload.get("sidecar"), item_id=snap.get("item_id") if payload.get("item_is_wax", True) else None,
        evidence=evidence if isinstance(evidence, dict) else None, stat=payload.get("note_stat"),
        vault_root=root, ledger_maps_note=bool(payload.get("ledger_maps_note")), tz=config.local_tz())


def bound_statements(conn: Any) -> None:
    """Cap every statement of the current transaction at the pass path's 5 s.

    A snapshot is applied under the item's flock, and a pass blocks on that flock. The drain,
    reconcile and `wax transcripts sync` run on connections opened with a 60 s statement_timeout
    (for their own bulk reads), so without this a slow statement there could hold a pass for up to
    60 s per statement instead of D.9's 5 s. Transaction-local: the connection's default returns
    at COMMIT/ROLLBACK.
    """
    conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(db.DEFAULT_STATEMENT_TIMEOUT_MS),))


def apply_snapshot(conn: Any, snap: dict[str, Any]) -> Outcome:
    """Project one snapshot inside the caller's transaction (design D.4 steps 1-7)."""
    bound_statements(conn)
    payload = snap.get("payload") or {}
    observed_at = _ts(snap.get("created_at")) or dt.datetime.now(dt.timezone.utc)
    facts = _facts(payload)
    if snap["kind"] == "pass_run":
        return _apply_pass_run(conn, snap, facts, observed_at)

    ext = extraction_for(snap)
    values = _identity_values(ext)
    identity = resolve_identity(conn, wax_item_id=ext.wax_item_id, relpath=ext.relpath,
                                transcript_id=payload.get("transcript_id"))
    if identity is None:
        transcript_id = _insert_identity(conn, values)
        identity = {"transcript_id": transcript_id, "note_relpath": ext.relpath, "wax_item_id": ext.wax_item_id}
    else:
        transcript_id = identity["transcript_id"]
        _move(conn, identity, ext.relpath)
        _update_identity(conn, transcript_id, values)

    _insert_body(conn, ext)
    current = head(conn, transcript_id)
    producer_kind = payload.get("producer_kind") or "pass"
    produced_by = payload.get("produced_by") or facts.get("slug") or producer_kind
    pass_version = facts.get("pass_version") if producer_kind == "pass" else None
    decision = versioning.decide(current, kind=producer_kind, produced_by=produced_by,
                                 pass_version=pass_version, content_sha256=ext.content_sha256)
    if producer_kind == "reconcile":
        key = versioning.reconcile_key(ext.content_sha256, current["version_no"] if current else None)
    elif producer_kind == "backfill":
        key = versioning.backfill_key(ext.note_sha256)
    else:
        key = snap["idempotency_key"]

    outcome = Outcome(decision=decision, transcript_id=transcript_id)
    if decision == versioning.MINT:
        row = _version_row(ext, transcript_id=transcript_id, key=key, producer_kind=producer_kind,
                           produced_by=produced_by, facts=facts if producer_kind == "pass" else
                           ({"pass_attempt": (payload.get("transcription") or {}).get("attempt")}
                            if producer_kind == "transcription" else {}),
                           observed_at=observed_at)
        minted = _insert_version(conn, row)
        if minted is None:
            existing = conn.execute("SELECT version_id, version_no FROM tx.transcript_version "
                                    "WHERE transcript_id = %s AND idempotency_key = %s",
                                    (transcript_id, key)).fetchone()
            outcome.decision = "duplicate"
            outcome.version_id, outcome.version_no = (int(existing[0]), int(existing[1])) if existing else (None, None)
        else:
            version_id, version_no = minted
            outcome.version_id, outcome.version_no = version_id, version_no
            _insert_children(conn, version_id, ext)
            _upsert_current(conn, transcript_id, version_id, version_no, produced_by, ext)
            body_changed = current is None or current["body_sha256"] != ext.body_sha256
            count = swap_chunks(conn, transcript_id, ext, force=body_changed)
            if count is not None:
                outcome.chunks_swapped = True
                outcome.chunk_count = count
                conn.execute("UPDATE tx.transcript_current SET chunk_count = %s WHERE transcript_id = %s",
                             (count, transcript_id))
            if current is None or current.get("doc_input_sha256") != ext.doc_input_sha256 or not ext.searchable:
                sync_doc_vector(conn, transcript_id, ext)
            _upsert_path(conn, transcript_id, ext.relpath, "canonical", version_id)
    else:
        _upsert_path(conn, transcript_id, ext.relpath, "canonical", None)

    if producer_kind == "pass":
        result = {"mint": "version", "duplicate": "version", "no_change": "no_change", "skip": "no_change"}[
            outcome.decision]
        _pass_run(conn, transcript_id=transcript_id, key=snap["idempotency_key"], facts=facts,
                  finished_at=observed_at, version_id=outcome.version_id, outcome=result)
    return outcome


def _apply_pass_run(conn: Any, snap: dict[str, Any], facts: dict[str, Any], observed_at: dt.datetime) -> Outcome:
    """A failed pass: history only, never a version."""
    item_id = snap.get("item_id")
    payload = snap.get("payload") or {}
    identity = None
    if item_id and extract.ITEM_ID_RE.match(item_id):
        row = conn.execute(_IDENTITY_SELECT.format("wax_item_id = %s"), (item_id,)).fetchone()
        identity = row[0] if row else None
    if identity is None:
        md_path = snap.get("md_path")
        if not md_path or not (item_id and extract.ITEM_ID_RE.match(item_id)):
            return Outcome(decision="pass_run")
        root = payload.get("vault_root") or vault_root()
        relpath = payload.get("relpath") or relpath_for(md_path, root)
        clash = conn.execute(_IDENTITY_SELECT.format("note_relpath = %s"), (relpath,)).fetchone()
        if clash:
            if clash[1] and clash[1] != item_id:
                raise ProjectionConflict(f"{relpath} belongs to item {clash[1]}, not {item_id}")
            identity = clash[0]
            conn.execute("UPDATE tx.transcript SET wax_item_id = COALESCE(wax_item_id, %s) WHERE transcript_id = %s",
                         (item_id, identity))
        else:
            # A failure before the note was ever versioned: the identity exists so the
            # failure is visible; reconcile gives it its backfill version.
            identity = _insert_identity(conn, {"wax_item_id": item_id, "origin": "wax", "vault_root": root,
                                               "note_relpath": relpath})
    _pass_run(conn, transcript_id=identity, key=snap["idempotency_key"], facts=facts, finished_at=observed_at,
              version_id=None, outcome="not_applied")
    return Outcome(decision="pass_run", transcript_id=identity)


# ---------------------------------------------------------------- drain --

def _reject_note(snap: dict[str, Any], exc: Exception) -> str:
    return f"{outbox.REJECTED_PREFIX}{type(exc).__name__}: {str(exc)[:300]}"


def project_item(item_id: str, *, limit: int = 50, conn: Any = None) -> int:
    """Project this item's pending snapshots in seq order. Returns how many were applied.

    Stops at the first failure so later snapshots never overtake an earlier one.
    A snapshot the extractor rejects (an invalid classification) is marked
    `rejected:` instead, with a `not_applied` pass_run, so it cannot wedge the item.
    """
    with operations.try_item_lock(item_id) as held:
        # Re-entrant for a pass (it already holds the item); otherwise whoever
        # holds the item (a running pass, the drain) projects it instead.
        if not held:
            return 0
        return _project_locked(item_id, limit=limit, conn=conn)


def _project_locked(item_id: str, *, limit: int, conn: Any) -> int:
    rows = outbox.pending(item_id, limit=limit)
    if not rows:
        return 0
    own = conn is None
    if own:
        conn = db.connect("app", autocommit=True)
    applied = 0
    try:
        for snap in rows:
            try:
                with conn.transaction():
                    apply_snapshot(conn, snap)
            except extract.ExtractError as exc:
                _reject(conn, snap, exc)
                continue
            except Exception as exc:
                outbox.fail(snap["seq"], f"{type(exc).__name__}: {str(exc).strip()[:400]}")
                raise
            outbox.mark(snap["seq"])
            applied += 1
    finally:
        if own:
            conn.close()
    return applied


def _reject(conn: Any, snap: dict[str, Any], exc: Exception) -> None:
    note = _reject_note(snap, exc)
    log.warning("transcriptions: snapshot %s for %s rejected: %s", snap.get("seq"), snap.get("item_id"), exc)
    payload = snap.get("payload") or {}
    if payload.get("producer_kind") == "pass":
        try:
            with conn.transaction():
                bound_statements(conn)
                failed = dict(snap, kind="pass_run")
                facts = _facts(payload)
                facts["detail"] = f"{note}"[:300]
                _apply_pass_run(conn, failed, facts, _ts(snap.get("created_at")) or dt.datetime.now(dt.timezone.utc))
        except Exception as inner:  # noqa: BLE001
            log.warning("transcriptions: could not record the rejected pass run: %s", inner)
    outbox.mark(snap["seq"], error=note)


def project_pending(*, limit: int = 200, conn: Any = None) -> dict[str, Any]:
    """Drain every item's queue; an item whose flock is held (a pass is running) is skipped."""
    stats: dict[str, Any] = {"items": 0, "projected": 0, "skipped_locked": 0, "failed": 0, "errors": []}
    items = outbox.pending_items(limit=limit)
    if not items:
        return stats
    own = conn is None
    try:
        for item_id in items:
            with operations.try_item_lock(item_id) as held:
                if not held:
                    stats["skipped_locked"] += 1
                    continue
                if conn is None:
                    conn = db.connect("app", autocommit=True)
                stats["items"] += 1
                try:
                    stats["projected"] += project_item(item_id, conn=conn)
                except Exception as exc:  # noqa: BLE001
                    if db.is_outage(exc):
                        raise
                    stats["failed"] += 1
                    stats["errors"].append(f"{item_id}: {type(exc).__name__}: {str(exc)[:200]}")
    finally:
        if own and conn is not None:
            conn.close()
    return stats
