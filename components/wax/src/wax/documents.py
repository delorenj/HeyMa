"""Document enrichment context: ordinary Markdown files enriched in place.

A document is NOT a transcript. It has no audio, no ``transcripts`` row, and no
archived source object, and the one rule that keeps this scenario honest is
that it never acquires any of those by implication: ``passes.run`` resolves the
note through this module instead, and result application in document mode
stamps no transcript provenance. The pass ledger rows key on the binding-scoped
document key (``doc-<sha16>`` plus ``@<binding_id>`` when a folder binding
owns the run), so gating, attempts, retries and outcome tallies behave
identically to audio — while two pipelines over overlapping folders keep
genuinely separate outcomes.

Folder bindings are new-arrivals-only by approved decision:

- the first scan of an EXISTING folder seeds it — every file becomes a known,
  ``seeded`` document and none of them is processed; a missing folder is
  reported and never claims to be seeded;
- later scans process only content the binding has never seen, plus failed
  arrivals still inside their attempt budget (recovery, not just discovery);
- files Wax itself writes (staging temps, atomic-rename ``.tmp`` sidecars,
  hidden files) are ignored so a pass output can never re-trigger its own
  pipeline — and the hash a pass's own frontmatter write produces is recorded
  as ``current_sha256`` on the SAME document, so enriching a file can never
  re-register it as a new arrival;
- covering the seeded history needs an explicit ``wax documents backfill``.

Identity is content AT FIRST SIGHT, tracked per binding: ``doc_id =
"doc-" + sha256(bytes)[:16]``. A human edit after processing is genuinely new
content (a new document); Wax's own writes are not. The durable receipt — one
row per (binding, doc, pipeline, compiled definition) — is what makes a
duplicate arrival a no-op while a failed arrival stays retryable.
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from . import ledger, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

DOC_PREFIX = "doc-"
# Extensions a binding considers documents. Anything else in the folder is
# ignored rather than rejected, so a binding can point at a mixed directory.
DOC_SUFFIXES = {".md", ".markdown"}
# Wax's own write shapes: pass staging (``.wax-ep-*``), atomic-rename temps and
# capture partials. Reading one of these as an arrival is how a pipeline could
# observe its own output forever.
IGNORED_SUFFIXES = {".tmp", ".partial", ".ogg.partial"}
IGNORED_PREFIXES = (".wax-", ".")

# A run that crashed mid-flight leaves `processing` behind; a claim older than
# this is reclaimable by the next scan instead of wedging the binding forever.
STALE_RUN_S = float(os.environ.get("WAX_DOCUMENT_STALE_RUN_S", "900"))
# Failed arrivals retry until this bound, then the receipt is exhausted and the
# scan stops picking them up (visible in the catalog, dead-lettered here).
MAX_ATTEMPTS = int(os.environ.get("WAX_DOCUMENT_MAX_ATTEMPTS", "3"))
# The worker's scan cadence (seconds). Also reported through the catalog so a
# hosted surface can tell "waiting for the next scan" from "nobody scans".
SCAN_INTERVAL_S = float(os.environ.get("WAX_DOCUMENT_SCAN_INTERVAL_S", "120"))


def is_document_key(key: str) -> bool:
    """Whether a pass-ledger key names a document rather than an audio item."""
    return isinstance(key, str) and key.split("@", 1)[0].startswith(DOC_PREFIX)


def doc_key(binding_id: str, doc_id: str) -> str:
    """The pass-ledger key for one document under one binding ("" = unbound)."""
    return f"{doc_id}@{binding_id}" if binding_id else doc_id


def split_key(key: str) -> tuple[str, str]:
    """(binding_id, doc_id) for a pass-ledger document key."""
    if "@" in key:
        doc, _, binding = key.rpartition("@")
        return binding, doc
    return "", key


def _doc_id(digest: str) -> str:
    return DOC_PREFIX + digest[:16]


def _iso_at(epoch: float) -> str:
    from datetime import datetime, timezone
    return (datetime.fromtimestamp(epoch, timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _parse_iso(value: Any) -> Optional[float]:
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def ignored(path: Path) -> bool:
    """Whether a binding must ignore this file: hidden, temp, or not a doc."""
    name = path.name
    return (name.startswith(IGNORED_PREFIXES)
            or path.suffix in IGNORED_SUFFIXES
            or path.suffix.lower() not in DOC_SUFFIXES)


def register(path: Path, *, binding_id: str = "", origin: str = "manual",
             adopt_doc_id: Optional[str] = None,
             ) -> tuple[dict[str, Any], bool]:
    """Register a Markdown file as a known document under one binding.

    Returns (row, is_arrival). Registration never processes: it only makes the
    content KNOWN. Known means any of:

    - the binding already tracks this content (``doc_id``);
    - the binding tracks this PATH and the file's hash matches either its
      first-sight hash or its ``current_sha256`` (Wax's own frontmatter write);
    - ``adopt_doc_id`` names an existing row (a retry): the CURRENT bytes are
      adopted onto that row's identity, so a retried run continues the same
      document instead of minting a new one.

    Revised content at a known path — a human edit — is a NEW document: the
    old row stays as history and the new content arrives.
    """
    try:
        digest = sha256_file(path)
        size = path.stat().st_size
    except OSError:
        return {"error": f"cannot read {path}"}, False
    now = sentinel.utcnow()
    conn = ledger.connect()
    if adopt_doc_id:
        row = conn.execute(
            "SELECT * FROM documents WHERE binding_id=? AND doc_id=?",
            (binding_id, adopt_doc_id)).fetchone()
        if row is not None:
            # A retry adopts the current bytes (a failed run writes its pass
            # notes, so the hash always moves) onto the row it is retrying.
            conn.execute(
                "UPDATE documents SET current_sha256=?, bytes=?, updated_at=? "
                "WHERE binding_id=? AND doc_id=?",
                (digest, size, now, binding_id, adopt_doc_id))
            adopted = dict(row)
            adopted["current_sha256"] = digest
            return adopted, False
    doc_id = _doc_id(digest)
    by_content = conn.execute(
        "SELECT * FROM documents WHERE binding_id=? AND doc_id=?",
        (binding_id, doc_id)).fetchone()
    if by_content is not None:
        row = dict(by_content)
        # Same content at a new path is a duplicate arrival. Re-point only when
        # the recorded path is gone, exactly like items, so two live copies
        # cannot fight over the row.
        if row["path"] != str(path) and not Path(row["path"]).exists():
            conn.execute("UPDATE documents SET path=?, updated_at=? "
                         "WHERE binding_id=? AND doc_id=?",
                         (str(path), now, binding_id, doc_id))
            row["path"] = str(path)
        if row["path"] == str(path) and digest != (row["current_sha256"] or row["sha256"]) \
                and digest != row["sha256"]:
            # Known content whose file hash moved (e.g. re-encoded). Keep the
            # first-sight identity; track the current bytes.
            conn.execute("UPDATE documents SET current_sha256=?, updated_at=? "
                         "WHERE binding_id=? AND doc_id=?",
                         (digest, now, binding_id, doc_id))
            row["current_sha256"] = digest
        return row, False
    conn.execute(
        "INSERT INTO documents(binding_id,doc_id,path,sha256,current_sha256,bytes,"
        "origin,state,first_seen,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)",
        (binding_id, doc_id, str(path), digest, digest, size, origin, now, now),
    )
    row = dict(conn.execute(
        "SELECT * FROM documents WHERE binding_id=? AND doc_id=?",
        (binding_id, doc_id)).fetchone())
    return row, True


def md_for(key: str) -> Optional[Path]:
    """The document's note path, or None. Never consults transcripts."""
    binding_id, doc_id = split_key(key)
    row = ledger.connect().execute(
        "SELECT path FROM documents WHERE binding_id=? AND doc_id=?",
        (binding_id, doc_id)).fetchone()
    if not row:
        return None
    p = Path(row["path"])
    return p if p.is_file() else None


def get(key: str) -> Optional[dict[str, Any]]:
    binding_id, doc_id = split_key(key)
    row = ledger.connect().execute(
        "SELECT * FROM documents WHERE binding_id=? AND doc_id=?",
        (binding_id, doc_id)).fetchone()
    return dict(row) if row else None


def list_documents(*, binding_id: Optional[str] = None, state: Optional[str] = None,
                   limit: int = 200) -> list[dict[str, Any]]:
    """Bounded document listing for discovery surfaces."""
    sql = "SELECT * FROM documents"
    clauses: list[str] = []
    params: list[Any] = []
    if binding_id is not None:
        clauses.append("binding_id=?")
        params.append(binding_id)
    if state:
        clauses.append("state=?")
        params.append(state)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))
    return [dict(r) for r in ledger.connect().execute(sql, params).fetchall()]


# -------------------------------------------------- run lock + receipts ----

def claim_run(binding_id: str, doc_id: str) -> bool:
    """Take the per-document run lock. False if another run owns it.

    The compare-and-set is the whole concurrency story: a scan (worker or CLI)
    and a manual ``documents run`` can race, and exactly one of them flips
    ``pending``/``failed``/stale-``processing`` to ``processing``. A crash
    leaves ``processing`` behind with ``run_started_at``; older than
    STALE_RUN_S it is reclaimable, so a wedged claim cannot park a document
    forever.
    """
    conn = ledger.connect()
    now = sentinel.utcnow()
    cursor = conn.execute(
        "UPDATE documents SET state='processing', run_started_at=?, updated_at=? "
        "WHERE binding_id=? AND doc_id=? AND (state IN ('pending','failed','seeded') "
        "OR (state='processing' AND run_started_at IS NOT NULL AND run_started_at < ?))",
        (now, now, binding_id, doc_id, _iso_at(time.time() - STALE_RUN_S)),
    )
    return cursor.rowcount == 1


def release_run(binding_id: str, doc_id: str, state: str, *, error: str = "") -> None:
    conn = ledger.connect()
    now = sentinel.utcnow()
    attempts_bump = 1 if state in ("failed", "halted") else 0
    conn.execute(
        "UPDATE documents SET state=?, attempts=attempts+?, last_attempt_at=?, "
        "last_error=?, run_started_at=NULL, updated_at=? WHERE binding_id=? AND doc_id=?",
        (state, attempts_bump, now, error[:500] if error else None, now,
         binding_id, doc_id),
    )


def note_own_write(binding_id: str, doc_id: str, path: Path) -> Optional[str]:
    """After a run, record the file's post-enrichment hash as our own write.

    This is the fix for rerun loops: a pass applies frontmatter, the file hash
    changes, and without this the next scan would register "new content" and
    enrich the same document forever. Returns the recorded hash.
    """
    try:
        digest = sha256_file(path)
    except OSError:
        return None
    ledger.connect().execute(
        "UPDATE documents SET current_sha256=?, updated_at=? WHERE binding_id=? AND doc_id=?",
        (digest, sentinel.utcnow(), binding_id, doc_id),
    )
    return digest


def receipt(binding_id: str, doc_id: str, pipeline: str, definition_id: str,
            ) -> Optional[dict[str, Any]]:
    row = ledger.connect().execute(
        "SELECT * FROM document_receipts WHERE binding_id=? AND doc_id=? "
        "AND pipeline=? AND definition_id=?",
        (binding_id, doc_id, pipeline, definition_id)).fetchone()
    return dict(row) if row else None


def record_receipt(binding_id: str, doc_id: str, pipeline: str, definition_id: str,
                   state: str, *, error: str = "") -> dict[str, Any]:
    """Upsert the durable per-(binding, doc, pipeline, definition) receipt.

    ``completed`` is terminal (a duplicate arrival is a no-op); ``failed`` and
    ``halted`` count attempts and dead-letter at MAX_ATTEMPTS with
    ``exhausted_at`` — after which the scan stops retrying this arrival.
    """
    conn = ledger.connect()
    now = sentinel.utcnow()
    conn.execute(
        "INSERT INTO document_receipts(binding_id,doc_id,pipeline,definition_id,state,"
        "attempts,last_attempt_at,completed_at,exhausted_at,last_error) "
        "VALUES(?,?,?,?,?,1,?,?,NULL,?) "
        "ON CONFLICT(binding_id,doc_id,pipeline,definition_id) DO UPDATE SET "
        "state=excluded.state, attempts=document_receipts.attempts+1, "
        "last_attempt_at=excluded.last_attempt_at, "
        "completed_at=COALESCE(excluded.completed_at, document_receipts.completed_at), "
        "exhausted_at=CASE WHEN excluded.state IN ('failed','halted') "
        "AND document_receipts.attempts+1>=? THEN excluded.last_attempt_at "
        "ELSE document_receipts.exhausted_at END, "
        "last_error=excluded.last_error",
        (binding_id, doc_id, pipeline, definition_id, state, now,
         now if state == "completed" else None, error[:500] if error else None,
         MAX_ATTEMPTS),
    )
    row = conn.execute(
        "SELECT * FROM document_receipts WHERE binding_id=? AND doc_id=? "
        "AND pipeline=? AND definition_id=?",
        (binding_id, doc_id, pipeline, definition_id)).fetchone()
    return dict(row) if row else {}


def receipts_for_doc(doc_id: str, pipeline: str, definition_id: str,
                     ) -> list[dict[str, Any]]:
    """Every binding's receipt for one content identity. Discovery + the
    unbound manual-run deference (bindings never suppress one another)."""
    rows = ledger.connect().execute(
        "SELECT * FROM document_receipts WHERE doc_id=? AND pipeline=? AND definition_id=?",
        (doc_id, pipeline, definition_id)).fetchall()
    return [dict(r) for r in rows]


def retryable(binding_id: str, *, max_attempts: int = MAX_ATTEMPTS,
              limit: int = 50) -> list[dict[str, Any]]:
    """Failed documents under this binding still inside their attempt bound."""
    rows = ledger.connect().execute(
        "SELECT d.* FROM documents d LEFT JOIN document_receipts r "
        "ON r.binding_id=d.binding_id AND r.doc_id=d.doc_id "
        "WHERE d.binding_id=? AND d.state='failed' "
        "AND d.attempts < ? AND r.exhausted_at IS NULL "
        "ORDER BY d.updated_at LIMIT ?",
        (binding_id, max_attempts, max(1, min(int(limit), 500)))).fetchall()
    return [dict(r) for r in rows]


def recent_receipts(pipeline: Optional[str] = None, limit: int = 10,
                    ) -> list[dict[str, Any]]:
    """Bounded newest-first receipt view for discovery surfaces."""
    sql = ("SELECT r.*, d.path FROM document_receipts r "
           "LEFT JOIN documents d ON d.binding_id=r.binding_id AND d.doc_id=r.doc_id")
    params: list[Any] = []
    if pipeline:
        sql += " WHERE r.pipeline=?"
        params.append(pipeline)
    sql += " ORDER BY r.last_attempt_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 100)))
    return [dict(r) for r in ledger.connect().execute(sql, params).fetchall()]


# ------------------------------------------------------------- bindings ----

def bindings(*, pipeline: Optional[str] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM document_bindings"
    params: list[Any] = []
    if pipeline:
        sql += " WHERE pipeline=?"
        params.append(pipeline)
    rows = ledger.connect().execute(sql + " ORDER BY binding_id", params).fetchall()
    out = []
    for row in rows:
        binding = dict(row)
        binding["enabled"] = bool(binding["enabled"])
        binding["folder_exists"] = Path(binding["folder"]).is_dir()
        out.append(binding)
    return out


def ensure_binding(pipeline: str, folder: str) -> dict[str, Any]:
    """Create (or keep) the binding row for one authored folder.

    Unseeded by construction: seeded_at stays NULL until a scan of an EXISTENT
    folder seeds it, and compile() calls this only with folders the pipeline
    currently declares.
    """
    binding_id = _binding_id(pipeline, folder)
    now = sentinel.utcnow()
    ledger.connect().execute(
        "INSERT INTO document_bindings(binding_id,pipeline,folder,enabled,created_at,updated_at) "
        "VALUES(?,?,?,1,?,?) ON CONFLICT(binding_id) DO UPDATE SET pipeline=excluded.pipeline, "
        "updated_at=excluded.updated_at",
        (binding_id, pipeline, folder, now, now),
    )
    return next(b for b in bindings() if b["binding_id"] == binding_id)


def drop_binding(binding_id: str) -> bool:
    conn = ledger.connect()
    cursor = conn.execute("DELETE FROM document_bindings WHERE binding_id=?", (binding_id,))
    return cursor.rowcount > 0


def _binding_id(pipeline: str, folder: str) -> str:
    digest = hashlib.sha256(f"{pipeline}:{folder}".encode()).hexdigest()[:12]
    slug = Path(folder).name or "folder"
    return f"{pipeline}-{slug}-{digest}"


def _iter_documents(folder: Path):
    if not folder.is_dir():
        return
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not ignored(path):
            yield path


def seed(binding: dict[str, Any]) -> dict[str, Any]:
    """Register every existing file as a known document; process nothing.

    A missing folder is NOT a seed: the binding stays unseeded so the day the
    folder appears, its existing files seed then (still without processing)
    rather than silently counting as covered history.
    """
    folder = Path(binding["folder"])
    if not folder.is_dir():
        return {"binding_id": binding["binding_id"], "folder": str(folder),
                "action": "folder_missing", "seeded": 0,
                "detail": f"{folder} does not exist; binding stays unseeded"}
    known = 0
    for path in _iter_documents(folder):
        row, _arrival = register(path, binding_id=binding["binding_id"],
                                 origin="binding-seed")
        if "error" not in row:
            ledger.connect().execute(
                "UPDATE documents SET state='seeded', updated_at=? "
                "WHERE binding_id=? AND doc_id=? AND state='pending'",
                (sentinel.utcnow(), binding["binding_id"], row["doc_id"]))
            known += 1
    now = sentinel.utcnow()
    ledger.connect().execute(
        "UPDATE document_bindings SET seeded_at=?, documents_seen=?, updated_at=? "
        "WHERE binding_id=?",
        (now, known, now, binding["binding_id"]),
    )
    return {"binding_id": binding["binding_id"], "folder": str(folder),
            "action": "seeded", "seeded": known, "seeded_at": now, "processed": 0}


def _arrivals(binding: dict[str, Any]) -> list[Path]:
    """Files whose content this binding has never seen.

    The content check is what makes identity durable: a path lookup alone
    misses a moved file, and a global content lookup would suppress a second
    binding over the same folder. Both lookups are per binding.
    """
    conn = ledger.connect()
    folder = Path(binding["folder"])
    out = []
    for path in _iter_documents(folder):
        row = conn.execute(
            "SELECT * FROM documents WHERE binding_id=? AND path=? ORDER BY updated_at DESC",
            (binding["binding_id"], str(path))).fetchone()
        try:
            digest = sha256_file(path)
        except OSError:
            continue
        if row is not None:
            if digest in (row["sha256"], row["current_sha256"] or row["sha256"]):
                continue  # same content (or our own write): not an arrival
            # Revised content at a known path: fall through — new content.
        if conn.execute(
                "SELECT 1 FROM documents WHERE binding_id=? AND doc_id=?",
                (binding["binding_id"], _doc_id(digest))).fetchone():
            continue  # this content is already known to this binding
        out.append(path)
    return out


def _run_one(binding_id: str, pipeline: str, path: Path,
             doc_id: Optional[str] = None) -> dict[str, Any]:
    from . import pipelines  # lazy: pipelines imports this module

    row, arrival = register(path, binding_id=binding_id or "",
                            origin="binding-scan" if binding_id else "manual",
                            adopt_doc_id=doc_id)
    if "error" in row:
        return {"path": str(path), **row}
    run = pipelines.run_document(pipeline, path, binding_id=binding_id or "",
                                 doc_id=row["doc_id"])
    run["doc_id"] = row["doc_id"]
    run["was_arrival"] = arrival
    return run


def scan(*, pipeline: Optional[str] = None,
         max_attempts: int = MAX_ATTEMPTS) -> dict[str, Any]:
    """Process new arrivals and retryable failures for every enabled binding.

    First contact with an EXISTENT folder seeds instead of processing — the
    approved new-arrivals-only default. Later scans process only content the
    binding has never seen, plus failed arrivals still inside their attempt
    budget: a provider outage recovers without a human typing anything. A
    binding whose PIPELINE is not compiled-and-enabled is refused outright;
    registering arrivals for a disabled pipeline would swallow them into the
    seed where only an explicit backfill could reach them. Execution belongs
    to the pipeline module (the one graph authority); this decides WHAT runs.
    """
    from . import pipelines  # noqa: F401 - readiness check below

    rows = [b for b in bindings(pipeline=pipeline) if b["enabled"]]
    refused: list[dict[str, Any]] = []
    for binding in list(rows):
        try:
            current = pipelines.compiled().get(binding["pipeline"])
            ready = bool(current and current["definition"].get("enabled")
                         and current["definition"].get("trigger") == "documents")
        except Exception as exc:  # noqa: BLE001 - discovery-grade call, stay alive
            ready = False
        if not ready:
            rows.remove(binding)
            refused.append({"binding_id": binding["binding_id"],
                            "pipeline": binding["pipeline"],
                            "reason": "pipeline is not compiled, enabled and trigger: documents"})
    out: dict[str, Any] = {"bindings": [], "processed": 0,
                           **({"refused": refused} if refused else {})}
    for binding in rows:
        if binding["seeded_at"] is None:
            seeded = seed(binding)
            out["bindings"].append(seeded)
            continue
        folder = Path(binding["folder"])
        if not folder.is_dir():
            out["bindings"].append({
                "binding_id": binding["binding_id"], "folder": str(folder),
                "action": "folder_missing",
                "detail": "bound folder does not exist",
            })
            continue
        arrivals = _arrivals(binding)
        retry_docs = retryable(binding["binding_id"], max_attempts=max_attempts)
        results = []
        for path in arrivals:
            results.append(_run_one(binding["binding_id"], binding["pipeline"], path))
        for doc in retry_docs:
            path = Path(doc["path"])
            if not path.is_file():
                release_run(binding["binding_id"], doc["doc_id"], "failed",
                            error="file vanished before retry")
                continue
            try:
                digest = sha256_file(path)
            except OSError:
                continue
            if digest not in (doc["sha256"], doc["current_sha256"] or doc["sha256"]):
                # Someone else rewrote the file since the failure: the current
                # bytes arrive as their own document; this stale row stops.
                release_run(binding["binding_id"], doc["doc_id"], "failed",
                            error="content changed since failure; superseded")
                continue
            results.append(_run_one(binding["binding_id"], binding["pipeline"],
                                    path, doc_id=doc["doc_id"]))
        processed = sum(1 for r in results if not r.get("skipped"))
        now = sentinel.utcnow()
        ledger.connect().execute(
            "UPDATE document_bindings SET last_scan_at=?, "
            "documents_seen=documents_seen+?, documents_processed=documents_processed+?, "
            "updated_at=? WHERE binding_id=?",
            (now, len(arrivals), processed, now, binding["binding_id"]),
        )
        out["bindings"].append({
            "binding_id": binding["binding_id"], "folder": str(folder),
            "action": "scanned", "arrivals": len(arrivals),
            "retried": len(retry_docs), "processed": processed,
            "results": results, "last_scan_at": now,
        })
        out["processed"] += processed
    return out


def backfill(*, pipeline: str, limit: int = 0) -> dict[str, Any]:
    """Explicitly process documents a binding seeded but never ran.

    The deliberate opposite of the new-arrivals default: nothing here happens
    without this command naming the pipeline.
    """
    bound = bindings(pipeline=pipeline)
    if not bound:
        return {"pipeline": pipeline, "processed": 0,
                "warning": "no binding; compile the pipeline first"}
    todo: list[dict[str, Any]] = []
    for binding in bound:
        todo.extend(list_documents(binding_id=binding["binding_id"], state="seeded",
                                   limit=1000))
    if limit and limit > 0:
        todo = todo[:limit]
    results = []
    for doc in todo:
        path = Path(doc["path"])
        if not path.is_file():
            continue
        run = _run_one(doc["binding_id"], pipeline, path, doc_id=doc["doc_id"])
        results.append(run)
    processed = sum(1 for r in results if not r.get("skipped"))
    return {"pipeline": pipeline, "candidates": len(todo), "processed": processed,
            "results": results}


def status(*, pipeline: Optional[str] = None) -> dict[str, Any]:
    """Bounded binding + document state for discovery surfaces."""
    conn = ledger.connect()
    rows = bindings(pipeline=pipeline)
    for binding in rows:
        counts = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM documents WHERE binding_id=? GROUP BY state",
            (binding["binding_id"],))}
        binding["documents"] = sum(counts.values())
        binding["documents_pending"] = counts.get("pending", 0)
        binding["documents_seeded"] = counts.get("seeded", 0)
        binding["documents_unprocessed"] = counts.get("pending", 0) + counts.get("seeded", 0)
        binding["documents_failed"] = counts.get("failed", 0)
        binding["documents_processed"] = counts.get("processed", 0)
        binding["documents_exhausted"] = int(conn.execute(
            "SELECT COUNT(*) AS n FROM document_receipts WHERE binding_id=? "
            "AND exhausted_at IS NOT NULL", (binding["binding_id"],)).fetchone()["n"])
    total = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()
    return {"bindings": rows, "documents_total": int(total["n"] if total else 0),
            "scan_interval_s": SCAN_INTERVAL_S, "max_attempts": MAX_ATTEMPTS}


def worker_heartbeat_path() -> Path:
    from . import paths
    return paths.VAR / "worker-heartbeat.json"


def write_worker_heartbeat(*, tick: int = 0, scanning_documents: bool = False) -> None:
    """The worker's liveness proof, one tiny file per loop iteration.

    The catalog reads this to say 'worker alive' vs 'nobody scans' — the
    difference between a healthy idle binding and a dead scheduler.
    """
    path = worker_heartbeat_path()
    payload = {"at": sentinel.utcnow(), "tick": tick,
               "pipeline_enabled": _pipeline_enabled_cached(),
               "documents": {"interval_s": SCAN_INTERVAL_S,
                             "scanning": scanning_documents}}
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
    except OSError:
        pass


def read_worker_heartbeat(now: Optional[float] = None) -> dict[str, Any]:
    """Heartbeat as a freshness-shaped block for the catalog. Never raises."""
    try:
        payload = json.loads(worker_heartbeat_path().read_text())
    except (OSError, ValueError):
        return {"state": "down",
                "note": "no worker heartbeat; waxd has never run this code"}
    at = payload.get("at")
    age = None if at is None else _parse_iso(at)
    if age is None:
        return {"state": "down", "note": "unreadable heartbeat", "heartbeat_at": at}
    age = max(0.0, (now or time.time()) - age)
    alive = age <= 30.0
    return {
        "state": "live" if alive else "down",
        "heartbeat_at": at,
        "age_seconds": round(age, 1),
        "tick": payload.get("tick"),
        "pipeline_enabled": bool(payload.get("pipeline_enabled")),
        "documents": payload.get("documents") or {},
        **({} if alive else {"note": f"last heartbeat {age:.0f}s ago; "
                                       "the worker thread is not running"}),
    }


def _pipeline_enabled_cached() -> bool:
    try:
        from . import state
        return bool(state.pipeline_enabled())
    except Exception:  # noqa: BLE001 - heartbeat must never fail
        return False
