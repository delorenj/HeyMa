"""Document enrichment context: ordinary Markdown files enriched in place.

A document is NOT a transcript. It has no audio, no ``transcripts`` row, and no
archived source object, and the one rule that keeps this scenario honest is
that it never acquires any of those by implication: ``passes.run`` resolves the
note through this module instead, and result application in document mode
stamps no transcript provenance. The pass ledger rows key on ``doc_id``, the
same table audio items use, so gating, attempts, retries and outcome tallies
behave identically.

Folder bindings are new-arrivals-only by approved decision:

- the first scan of a binding SEEDS it — every existing file becomes a known
  document and none of them is processed;
- later scans process only files that appeared after the seed;
- files Wax itself writes (staging temps, atomic-rename ``.tmp`` sidecars,
  hidden files) are ignored so a pass output can never re-trigger its own
  pipeline;
- covering the seeded history needs an explicit ``wax documents backfill``.

Identity is content, mirroring items: ``doc_id = "doc-" + sha256(bytes)[:16]``,
so a re-dropped copy of an already-processed document is one document, and the
processed receipt (pipeline + definition id) is what makes a duplicate arrival
a no-op rather than a second effective run.
"""

import hashlib
import logging
import os
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


def is_document_key(key: str) -> bool:
    """Whether a pass-ledger key names a document rather than an audio item."""
    return isinstance(key, str) and key.startswith(DOC_PREFIX)


def _doc_id(digest: str) -> str:
    return DOC_PREFIX + digest[:16]


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


def register(path: Path, *, origin: str = "manual") -> Optional[dict[str, Any]]:
    """Register a Markdown file as a document. Idempotent on content.

    Registration never processes: it only makes the file KNOWN, which is what
    seeding means. Returns the document row (fresh or existing).
    """
    try:
        digest = sha256_file(path)
    except OSError:
        return None
    doc_id = _doc_id(digest)
    now = sentinel.utcnow()
    conn = ledger.connect()
    existing = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    if existing:
        # Same content at a new path is a duplicate arrival, not a new document.
        # Re-point only when the recorded path is gone, exactly like items.
        if existing["path"] != str(path) and not Path(existing["path"]).exists():
            conn.execute("UPDATE documents SET path=?, updated_at=? WHERE doc_id=?",
                         (str(path), now, doc_id))
        return dict(existing)
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    conn.execute(
        "INSERT INTO documents(doc_id,sha256,path,bytes,origin,first_seen,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (doc_id, digest, str(path), size, origin, now, now),
    )
    return dict(conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone())


def md_for(doc_id: str) -> Optional[Path]:
    """The document's note path, or None. Never consults transcripts."""
    row = ledger.connect().execute(
        "SELECT path FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    if not row:
        return None
    p = Path(row["path"])
    return p if p.is_file() else None


def get(doc_id: str) -> Optional[dict[str, Any]]:
    row = ledger.connect().execute(
        "SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    return dict(row) if row else None


def list_documents(*, pipeline: Optional[str] = None,
                   unprocessed_only: bool = False, limit: int = 200) -> list[dict[str, Any]]:
    """Bounded document listing for discovery surfaces."""
    sql = "SELECT * FROM documents"
    clauses: list[str] = []
    params: list[Any] = []
    if pipeline:
        clauses.append("processed_pipeline=?")
        params.append(pipeline)
    if unprocessed_only:
        clauses.append("processed_at IS NULL")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))
    return [dict(r) for r in ledger.connect().execute(sql, params).fetchall()]


def mark_processed(doc_id: str, pipeline: str, definition_id: str) -> None:
    """The one-effective-run receipt: this content ran this definition once."""
    ledger.connect().execute(
        "UPDATE documents SET processed_at=?, processed_pipeline=?, "
        "processed_definition_id=?, updated_at=? WHERE doc_id=?",
        (sentinel.utcnow(), pipeline, definition_id, sentinel.utcnow(), doc_id),
    )


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

    Unseeded by construction: seeded_at stays NULL until the first scan, and
    compile() calls this only with folders the pipeline currently declares.
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
    """Register every existing file as a known document; process nothing."""
    folder = Path(binding["folder"])
    known = 0
    for path in _iter_documents(folder):
        if register(path, origin="binding-seed") is not None:
            known += 1
    now = sentinel.utcnow()
    ledger.connect().execute(
        "UPDATE document_bindings SET seeded_at=?, documents_seen=?, updated_at=? "
        "WHERE binding_id=?",
        (now, known, now, binding["binding_id"]),
    )
    return {"binding_id": binding["binding_id"], "folder": str(folder),
            "seeded": known, "seeded_at": now, "processed": 0}


def _claim_unseen(folder: Path) -> list[Path]:
    """Files present in the folder but not yet known to the binding.

    A path lookup is tried first (cheap), but an unknown path whose CONTENT is
    already a registered document is not an arrival — hashing a Markdown file
    costs milliseconds and stops a moved or duplicated file from being
    "newly arrived" on every scan forever.
    """
    conn = ledger.connect()
    out = []
    for path in _iter_documents(folder):
        if conn.execute("SELECT 1 FROM documents WHERE path=?", (str(path),)).fetchone():
            continue
        try:
            digest = sha256_file(path)
        except OSError:
            continue
        if conn.execute("SELECT 1 FROM documents WHERE doc_id=?", (_doc_id(digest),)).fetchone():
            continue
        out.append(path)
    return out


def scan(*, pipeline: Optional[str] = None) -> dict[str, Any]:
    """Process new arrivals for every enabled binding (or one pipeline's).

    First contact seeds instead of processing — that is the approved
    new-arrivals-only default. Later scans register and process only files the
    binding has never seen. A binding whose PIPELINE is not compiled-and-enabled
    is refused outright: registering arrivals for a disabled pipeline would
    silently swallow them into the seed, where only an explicit backfill could
    ever reach them. Execution belongs to the pipeline module (the one graph
    authority); this function only decides WHAT is new.
    """
    from . import pipelines  # lazy: pipelines imports this module

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
    if pipeline is not None and not rows and not refused:
        return {"pipeline": pipeline, "bindings": [], "processed": 0,
                "warning": "no enabled binding; compile the pipeline first"}
    out = {"bindings": [], "processed": 0, **({"refused": refused} if refused else {})}
    for binding in rows:
        if binding["seeded_at"] is None:
            seeded = seed(binding)
            seeded["action"] = "seeded"
            out["bindings"].append(seeded)
            continue
        folder = Path(binding["folder"])
        arrivals = _claim_unseen(folder)
        results = []
        for path in arrivals:
            doc = register(path, origin="binding-scan")
            if doc is None:
                continue
            run = pipelines.run_document(binding["pipeline"], path)
            run["doc_id"] = doc["doc_id"]
            results.append(run)
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
            "processed": processed, "results": results, "last_scan_at": now,
        })
        out["processed"] += processed
    return out


def backfill(*, pipeline: str, limit: int = 0) -> dict[str, Any]:
    """Explicitly process documents a binding seeded but never ran.

    The deliberate opposite of the new-arrivals default: nothing here happens
    without this command naming the pipeline.
    """
    from . import pipelines

    if not bindings(pipeline=pipeline):
        return {"pipeline": pipeline, "processed": 0,
                "warning": "no binding; compile the pipeline first"}
    todo = list_documents(pipeline=None, unprocessed_only=True, limit=1000)
    # Only documents under this pipeline's bound folders.
    folders = [Path(b["folder"]) for b in bindings(pipeline=pipeline)]
    todo = [d for d in todo
            if any(Path(d["path"]).is_relative_to(f) for f in folders if f.is_dir())]
    if limit and limit > 0:
        todo = todo[:limit]
    results = []
    for doc in todo:
        path = Path(doc["path"])
        if not path.is_file():
            continue
        run = pipelines.run_document(pipeline, path)
        run["doc_id"] = doc["doc_id"]
        results.append(run)
    processed = sum(1 for r in results if not r.get("skipped"))
    return {"pipeline": pipeline, "candidates": len(todo), "processed": processed,
            "results": results}


def status(*, pipeline: Optional[str] = None) -> dict[str, Any]:
    """Bounded binding + document state for discovery surfaces."""
    conn = ledger.connect()
    rows = bindings(pipeline=pipeline)
    for binding in rows:
        counts = conn.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN processed_at IS NULL THEN 1 ELSE 0 END) AS unprocessed "
            "FROM documents WHERE path LIKE ?",
            (str(Path(binding["folder"])) + "%",),
        ).fetchone()
        binding["documents"] = int(counts["n"] if counts else 0)
        binding["documents_unprocessed"] = int(counts["unprocessed"] or 0) if counts else 0
    total = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()
    return {"bindings": rows, "documents_total": int(total["n"] if total else 0)}
