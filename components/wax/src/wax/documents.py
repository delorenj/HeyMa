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

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

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

# Terminal document states written by hygiene (src/wax/hygiene.py): the note was
# moved to the vault trash, or its file vanished. Both are history, not work: no
# scan retries them, a path they once held is free again, and a failed pass left
# behind by one is not a live failure (ledger.passes_failed).
VANISHED_STATES = ("trashed", "gone")

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

    Revised content at a known path — a human edit — registers as a NEW
    document when an operator runs it explicitly: the old row stays as
    history and the new content runs under its own identity. Automatic
    scans never get here — a known path is not an arrival regardless of
    bytes (`_arrivals`); this is the explicit `wax documents run` path.
    """
    # Rows hold absolute paths: a relative one means "relative to whoever typed
    # it", and every later reader (waxd's hygiene sweeps among them) has a
    # different cwd. normpath only; symlinks keep the spelling they were given.
    path = Path(os.path.abspath(path))
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
    # Atomic registration: INSERT-then-reselect, not SELECT-then-INSERT — two
    # concurrent scans (worker + CLI) of the same arrival race exactly here,
    # and the losing SELECT must not crash on a UNIQUE constraint; it must
    # observe the winner's row and agree this was not an arrival.
    cursor = conn.execute(
        "INSERT INTO documents(binding_id,doc_id,path,sha256,current_sha256,bytes,"
        "origin,state,first_seen,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?) "
        "ON CONFLICT(binding_id,doc_id) DO NOTHING",
        (binding_id, doc_id, str(path), digest, digest, size, origin, now, now),
    )
    row = dict(conn.execute(
        "SELECT * FROM documents WHERE binding_id=? AND doc_id=?",
        (binding_id, doc_id)).fetchone())
    return row, cursor.rowcount == 1


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

class DocumentRunLock:
    """A cross-process ``flock`` held for one document's whole run.

    The DB ``processing`` state is the VISIBLE progress marker; THIS is the
    exclusion authority. A time-based claim (``processing`` older than N
    seconds) could be stolen out from under a live run that simply took longer
    than N — a real ``flock`` cannot: it dies with the holding process (a
    crash releases it immediately, no timer to expire) and blocks any second
    claimant, same process or not, for exactly as long as the run lives.
    """

    def __init__(self, path: Path):
        self._path = path
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        """Try to take the lock without blocking. False = someone holds it."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        try:
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"pid": os.getpid(),
                                     "at": sentinel.utcnow()}).encode())
        except OSError:
            pass  # the lock, not the label, is the contract
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None


def document_run_lock(binding_id: str, doc_id: str) -> DocumentRunLock:
    """The per-(binding, document) run lock under var/doc-locks/."""
    from . import paths
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", binding_id or "unbound")
    return DocumentRunLock(paths.VAR / "doc-locks" / f"{safe}__{doc_id}.lock")


def run_lock_held(binding_id: str, doc_id: str) -> bool:
    """Whether a live run currently holds this document's lock (never guesses
    from timestamps: it probes the actual flock)."""
    lock = document_run_lock(binding_id, doc_id)
    try:
        if not lock.acquire():
            return True
    except OSError:
        return False
    lock.release()
    return False


def claim_run(binding_id: str, doc_id: str, *, force: bool = False) -> bool:
    """Flip the document to the visible ``processing`` state.

    ``force=True`` is only valid while the caller holds the flock: any
    ``processing`` state seen under the lock belongs to a crashed holder, so
    the stale-timer does not apply. Without force, a claim older than
    STALE_RUN_S is reclaimable so a wedged row cannot park a document forever.
    A ``trashed``/``gone`` row is claimable too: the only way to reach one is an
    operator naming a file that is back on disk (``wax documents run``), and
    scans never select those states.
    """
    conn = ledger.connect()
    now = sentinel.utcnow()
    stale_before = _iso_at(time.time() - STALE_RUN_S)
    cursor = conn.execute(
        "UPDATE documents SET state='processing', run_started_at=?, updated_at=? "
        "WHERE binding_id=? AND doc_id=? AND (state IN "
        "('pending','failed','seeded','trashed','gone') "
        "OR (state='processing' AND run_started_at IS NOT NULL "
        "AND (run_started_at < ? OR ?)))",
        (now, now, binding_id, doc_id, stale_before, 1 if force else 0),
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


# ------------------------------------------------- hygiene ledger helpers ----

def rows_for_path(path: Path) -> list[dict[str, Any]]:
    """Every documents row (any binding, any state) recorded at this file.

    Rows are matched on the recorded path OR on the same real file: a binding
    stores the path it was configured with, a manual run the path it was
    handed, and either may reach one file through a symlinked prefix.
    """
    real = os.path.realpath(path)
    name = os.path.basename(str(path))
    like = "%/" + re.sub(r"([\\%_])", r"\\\1", name)
    rows = ledger.connect().execute(
        "SELECT * FROM documents WHERE path=? OR path LIKE ? ESCAPE '\\'",
        (str(path), like)).fetchall()
    return [dict(r) for r in rows
            if r["path"] == str(path) or os.path.realpath(r["path"]) == real]


@contextlib.contextmanager
def hold_run_locks(rows: list[dict[str, Any]]):
    """Take every row's run lock, non-blocking and all-or-nothing.

    Yields True when ALL are held (and keeps them until the block exits), False
    when any is owned by a live run — in which case nothing stays held. The
    flock is the exclusion authority, exactly as in run_document.
    """
    held: list[DocumentRunLock] = []
    ok = True
    try:
        for binding_id, doc_id in sorted({(r["binding_id"], r["doc_id"]) for r in rows}):
            lock = document_run_lock(binding_id, doc_id)
            if not lock.acquire():
                ok = False
                break
            held.append(lock)
        yield ok
    finally:
        for lock in held:
            lock.release()


def mark_state(binding_id: str, doc_id: str, state: str) -> None:
    """Move one document row to a (terminal) state without touching attempts."""
    ledger.connect().execute(
        "UPDATE documents SET state=?, run_started_at=NULL, updated_at=? "
        "WHERE binding_id=? AND doc_id=?",
        (state, sentinel.utcnow(), binding_id, doc_id))


def repoint(binding_id: str, doc_id: str, path: Path, digest: Optional[str]) -> None:
    """Record that a document's file moved: its new path and current bytes."""
    ledger.connect().execute(
        "UPDATE documents SET path=?, current_sha256=COALESCE(?, current_sha256), "
        "updated_at=? WHERE binding_id=? AND doc_id=?",
        (str(path), digest, sentinel.utcnow(), binding_id, doc_id))


def _real_if_absent(path: str) -> str:
    """The real spelling of a path that may not exist: the deepest existing
    ancestor resolved, the missing tail appended (so a symlinked prefix such as
    ``~/d`` compares equal to the swept folder's real path)."""
    head = os.path.normpath(path)
    tail: list[str] = []
    while head not in ("", os.sep) and not os.path.lexists(head):
        head, name = os.path.split(head)
        tail.insert(0, name)
    return os.path.join(os.path.realpath(head or os.sep), *tail)


def file_absent(path: str, roots: Iterable[str]) -> bool:
    """Whether a row's file is PROVABLY gone: absolute, lstat says ENOENT, and
    the path lies under one of ``roots``.

    ``roots`` are folders the caller has just verified are present (a mounted
    vault, an existing directory), so an unmounted or missing tree can never
    read as a pile of deleted notes. Anything else — a relative path (it would
    be resolved against waxd's cwd, not the cwd it was typed in), a path
    outside every root, an lstat failure other than "no such file" (EACCES, EIO,
    a stale mount) — is UNKNOWN, and unknown is never absent.
    """
    if not os.path.isabs(path):
        return False
    try:
        os.lstat(path)
        return False
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError:
        return False
    real = _real_if_absent(path)
    return any(real == root or real.startswith(root.rstrip(os.sep) + os.sep)
               for root in roots)


def missing_rows(roots: Iterable[str]) -> list[dict[str, Any]]:
    """Live document rows whose file is provably absent under ``roots``."""
    roots = list(roots)
    if not roots:
        return []
    rows = ledger.connect().execute(
        "SELECT * FROM documents WHERE state NOT IN ('trashed','gone')").fetchall()
    return [dict(r) for r in rows if file_absent(r["path"], roots)]


def mark_gone(binding_id: str, doc_id: str, roots: Iterable[str], *,
              dry_run: bool = False) -> Optional[dict[str, Any]]:
    """Mark one vanished document ``gone``, or return None when it should stay.

    A row stays live forever otherwise (nothing in the ledger notices a deleted
    or moved file), and a failed pass behind it reads as a failure nobody can
    fix. Under the run lock — skipped when a live run holds it — the row is
    READ AGAIN and judged by its current state and CURRENT path: a re-point
    (a rename) that landed since the caller's snapshot saves it.
    """
    lock = document_run_lock(binding_id, doc_id)
    if not lock.acquire():
        return None
    try:
        row = ledger.connect().execute(
            "SELECT * FROM documents WHERE binding_id=? AND doc_id=?",
            (binding_id, doc_id)).fetchone()
        if row is None or row["state"] in VANISHED_STATES:
            return None
        if not file_absent(row["path"], roots):
            return None
        if not dry_run:
            mark_state(binding_id, doc_id, "gone")
        return {"binding_id": binding_id, "doc_id": doc_id,
                "path": row["path"], "previous_state": row["state"]}
    finally:
        lock.release()


def _revive_gone(binding_id: str, path: Path, digest: str) -> bool:
    """Bring back a ``gone`` row whose file reappeared at its path unchanged.

    ``gone`` is a bookkeeping guess ("the file was missing when hygiene looked"),
    so bytes equal to the row's first-sight or current hash prove the guess was
    wrong. The state returns to what the row's receipts say it earned: completed
    is ``processed``, a failed/halted attempt is ``failed`` (still retryable),
    and a document no pipeline ever finished is ``seeded`` (known, not queued).
    """
    conn = ledger.connect()
    revived = False
    for row in conn.execute(
            "SELECT * FROM documents WHERE binding_id=? AND path=? AND state='gone'",
            (binding_id, str(path))).fetchall():
        if digest not in (row["current_sha256"], row["sha256"]):
            continue
        states = {r["state"] for r in conn.execute(
            "SELECT state FROM document_receipts WHERE binding_id=? AND doc_id=?",
            (binding_id, row["doc_id"])).fetchall()}
        state = ("processed" if "completed" in states
                 else "failed" if states else "seeded")
        conn.execute("UPDATE documents SET state=?, updated_at=? "
                     "WHERE binding_id=? AND doc_id=?",
                     (state, sentinel.utcnow(), binding_id, row["doc_id"]))
        revived = True
    return revived


def receipt(binding_id: str, doc_id: str, pipeline: str, definition_id: str,
            ) -> Optional[dict[str, Any]]:
    row = ledger.connect().execute(
        "SELECT * FROM document_receipts WHERE binding_id=? AND doc_id=? "
        "AND pipeline=? AND definition_id=?",
        (binding_id, doc_id, pipeline, definition_id)).fetchone()
    return dict(row) if row else None


def record_receipt(binding_id: str, doc_id: str, pipeline: str, definition_id: str,
                   state: str, *, error: str = "",
                   reason_code: Optional[str] = None) -> dict[str, Any]:
    """Upsert the durable per-(binding, doc, pipeline, definition) receipt.

    ``completed`` is terminal (a duplicate arrival is a no-op); ``failed`` and
    ``halted`` count attempts and dead-letter at MAX_ATTEMPTS with
    ``exhausted_at`` — after which the scan stops retrying this arrival.
    ``error`` is operator-only detail (the ledger is where humans look);
    ``reason_code`` is the machine cause the CATALOG may show — raw
    exception/stderr text never reaches a hosted surface through a receipt.
    """
    conn = ledger.connect()
    now = sentinel.utcnow()
    conn.execute(
        "INSERT INTO document_receipts(binding_id,doc_id,pipeline,definition_id,state,"
        "attempts,last_attempt_at,completed_at,exhausted_at,last_error,reason_code) "
        "VALUES(?,?,?,?,?,1,?,?,NULL,?,?) "
        "ON CONFLICT(binding_id,doc_id,pipeline,definition_id) DO UPDATE SET "
        "state=excluded.state, attempts=document_receipts.attempts+1, "
        "last_attempt_at=excluded.last_attempt_at, "
        "completed_at=COALESCE(excluded.completed_at, document_receipts.completed_at), "
        "exhausted_at=CASE WHEN excluded.state IN ('failed','halted') "
        "AND document_receipts.attempts+1>=? THEN excluded.last_attempt_at "
        "ELSE document_receipts.exhausted_at END, "
        "last_error=excluded.last_error, reason_code=excluded.reason_code",
        (binding_id, doc_id, pipeline, definition_id, state, now,
         now if state == "completed" else None, error[:500] if error else None,
         reason_code, MAX_ATTEMPTS),
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
    """Failed documents under this binding still inside their attempt bound.

    Also admits a ``processing`` row older than STALE_RUN_S: its holder
    crashed (a live holder keeps the flock, and the scan re-checks the actual
    lock before adopting it — a long run is never stolen on a timer alone).
    """
    rows = ledger.connect().execute(
        "SELECT d.* FROM documents d LEFT JOIN document_receipts r "
        "ON r.binding_id=d.binding_id AND r.doc_id=d.doc_id "
        "WHERE d.binding_id=? AND (d.state='failed' "
        "OR (d.state='processing' AND d.run_started_at IS NOT NULL "
        "AND d.run_started_at < ?)) "
        "AND d.attempts < ? AND r.exhausted_at IS NULL "
        "ORDER BY d.updated_at LIMIT ?",
        (binding_id, _iso_at(time.time() - STALE_RUN_S), max_attempts,
         max(1, min(int(limit), 500)))).fetchall()
    return [dict(r) for r in rows]


def retry_exhausted(*, pipeline: Optional[str] = None,
                    doc_id: Optional[str] = None) -> dict[str, Any]:
    """Explicitly re-arm exhausted document receipts. No ledger surgery.

    Semantics: for every matching exhausted receipt — the dead-letter state
    reached after MAX_ATTEMPTS failed attempts — clear ``exhausted_at`` and
    reset its attempt budget to zero, and reset the document row's attempt
    counter so the binding's scan picks the arrival up again on its next pass
    (or immediately via an explicit ``wax documents scan <pipeline>``). The
    receipt's history (completed runs elsewhere, last_error detail) is kept.
    """
    conn = ledger.connect()
    sql = ("SELECT r.binding_id, r.doc_id, r.pipeline, r.definition_id, r.attempts "
           "FROM document_receipts r WHERE r.exhausted_at IS NOT NULL")
    params: list[Any] = []
    if pipeline:
        sql += " AND r.pipeline=?"
        params.append(pipeline)
    if doc_id:
        sql += " AND r.doc_id=?"
        params.append(doc_id)
    rows = conn.execute(sql, params).fetchall()
    reset: list[dict[str, Any]] = []
    now = sentinel.utcnow()
    for row in rows:
        conn.execute(
            "UPDATE document_receipts SET exhausted_at=NULL, attempts=0, "
            "last_attempt_at=? WHERE binding_id=? AND doc_id=? AND pipeline=? "
            "AND definition_id=?",
            (now, row["binding_id"], row["doc_id"], row["pipeline"],
             row["definition_id"]),
        )
        conn.execute(
            "UPDATE documents SET attempts=0, updated_at=? "
            "WHERE binding_id=? AND doc_id=? AND state IN ('failed','processing')",
            (now, row["binding_id"], row["doc_id"]),
        )
        reset.append({"binding_id": row["binding_id"], "doc_id": row["doc_id"],
                      "pipeline": row["pipeline"], "definition_id": row["definition_id"],
                      "previous_attempts": int(row["attempts"] or 0)})
    return {"reset": len(reset), "receipts": reset,
            "detail": "attempt budgets re-armed; the next scan (or an explicit "
                      "`wax documents scan <pipeline>`) retries them"}


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
    rather than silently counting as covered history. An UNREADABLE file is
    equally not a completed seed: the error is reported, ``seeded_at`` stays
    unset, and the next scan retries the seed — claiming a folder seeded when
    a file could not even be hashed is how history goes quietly uncovered.
    """
    folder = Path(binding["folder"])
    if not folder.is_dir():
        return {"binding_id": binding["binding_id"], "folder": str(folder),
                "action": "folder_missing", "seeded": 0,
                "detail": f"{folder} does not exist; binding stays unseeded"}
    known = 0
    errors: list[dict[str, str]] = []
    for path in _iter_documents(folder):
        row, _arrival = register(path, binding_id=binding["binding_id"],
                                 origin="binding-seed")
        if "error" in row:
            errors.append({"path": str(path), "error": str(row["error"])})
            continue
        ledger.connect().execute(
            "UPDATE documents SET state='seeded', updated_at=? "
            "WHERE binding_id=? AND doc_id=? AND state='pending'",
            (sentinel.utcnow(), binding["binding_id"], row["doc_id"]))
        known += 1
    if errors:
        return {"binding_id": binding["binding_id"], "folder": str(folder),
                "action": "seed_error", "seeded": known,
                "error_count": len(errors), "errors": errors[:20],
                "detail": "unreadable file(s); binding stays unseeded and the "
                          "next scan retries the seed"}
    now = sentinel.utcnow()
    ledger.connect().execute(
        "UPDATE document_bindings SET seeded_at=?, documents_seen=?, updated_at=? "
        "WHERE binding_id=?",
        (now, known, now, binding["binding_id"]),
    )
    return {"binding_id": binding["binding_id"], "folder": str(folder),
            "action": "seeded", "seeded": known, "seeded_at": now, "processed": 0}


def _arrivals(binding: dict[str, Any]) -> list[Path]:
    """NEW PATHS this binding has never seen — and nothing else.

    Arrival is a path question, not a content question: a known path is
    never an arrival, whatever its bytes. That keeps unattended scans away
    from human edits to notes Wax has already seen (an edit stays put until
    an operator runs it explicitly via ``wax documents run``), and Wax's own
    frontmatter writes are invisible twice over (the path is known AND the
    post-run hash is recorded as ``current_sha256``). The content lookup only
    suppresses a MOVE — the same bytes at a genuinely new path are content
    this binding already ran, so a scan re-claims nothing. Both lookups are
    per binding, which is what lets a second binding over the same folder
    run the same content.
    """
    conn = ledger.connect()
    folder = Path(binding["folder"])
    out = []
    for path in _iter_documents(folder):
        # A trashed/gone row has written this path off: whatever sits there now
        # is a different file (a fresh Untitled.md), judged on its bytes below.
        row = conn.execute(
            "SELECT * FROM documents WHERE binding_id=? AND path=? "
            "AND state NOT IN ('trashed','gone') ORDER BY updated_at DESC",
            (binding["binding_id"], str(path))).fetchone()
        try:
            digest = sha256_file(path)
        except OSError:
            continue
        if row is not None:
            # A known path is never an arrival, regardless of bytes: human
            # edits are deliberately NOT auto-processed. `wax documents run`
            # is the explicit path for edits; retried failures still adopt
            # their own row in _scan_binding (and never consume a human
            # change — a moved digest supersedes the retry instead).
            continue
        if _revive_gone(binding["binding_id"], path, digest):
            continue  # the file was only missing for a while: still the same note
        if conn.execute(
                "SELECT 1 FROM documents WHERE binding_id=? AND doc_id=?",
                (binding["binding_id"], _doc_id(digest))).fetchone():
            continue  # a move: this content is already known to this binding
        out.append(path)
    return out


def _run_one(binding_id: str, pipeline: str, path: Path,
             doc_id: Optional[str] = None, *,
             explicit: bool = False) -> dict[str, Any]:
    from . import pipelines  # lazy: pipelines imports this module

    row, arrival = register(path, binding_id=binding_id or "",
                            origin="binding-scan" if binding_id else "manual",
                            adopt_doc_id=doc_id)
    if "error" in row:
        return {"path": str(path), **row}
    run = pipelines.run_document(pipeline, path, binding_id=binding_id or "",
                                 doc_id=row["doc_id"], explicit=explicit)
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
    binding whose PIPELINE is not compiled-and-enabled is refused — unless the
    pipeline was NAMED on this call, which is an explicit operator action and
    runs the compiled graph even while authored ``enabled: false`` (the toggle
    gates automatic pickup, not a human invoking the pipeline). Registering
    arrivals for an uncompiled pipeline is always refused: they would be
    swallowed into the seed where only an explicit backfill could reach them.
    Execution belongs to the pipeline module (the one graph authority); this
    decides WHAT runs.
    """
    from . import pipelines  # noqa: F401 - readiness check below

    explicit = pipeline is not None
    rows = [b for b in bindings(pipeline=pipeline) if b["enabled"]]
    refused: list[dict[str, Any]] = []
    for binding in list(rows):
        try:
            current = pipelines.compiled().get(binding["pipeline"])
            ready = bool(current
                         and current["definition"].get("trigger") == "documents"
                         and (current["definition"].get("enabled") or explicit))
        except Exception as exc:  # noqa: BLE001 - discovery-grade call, stay alive
            ready = False
        if not ready:
            rows.remove(binding)
            refused.append({"binding_id": binding["binding_id"],
                            "pipeline": binding["pipeline"],
                            "reason": "pipeline is not compiled with trigger: documents "
                                      "and enabled" + ("" if explicit else
                                                       " (or name the pipeline explicitly "
                                                       "to run it while disabled)")})
    out: dict[str, Any] = {"bindings": [], "processed": 0,
                           **({"refused": refused} if refused else {})}
    for binding in rows:
        try:
            out["bindings"].append(_scan_binding(binding, max_attempts, explicit))
        except Exception as exc:  # noqa: BLE001 - one binding never stops the rest
            log.exception("scan of binding %s failed: %s", binding["binding_id"], exc)
            out["bindings"].append({
                "binding_id": binding["binding_id"], "folder": binding["folder"],
                "action": "error", "error": f"{type(exc).__name__}: {exc}"[:400],
            })
    out["processed"] = sum(
        b.get("processed", 0) for b in out["bindings"] if isinstance(b, dict))
    return out


def _scan_binding(binding: dict[str, Any], max_attempts: int,
                  explicit: bool) -> dict[str, Any]:
    """One binding's scan slice. Per-DOCUMENT failures are isolated: one
    document raising never stops its siblings in the same scan."""
    if binding["seeded_at"] is None:
        return seed(binding)
    folder = Path(binding["folder"])
    if not folder.is_dir():
        return {
            "binding_id": binding["binding_id"], "folder": str(folder),
            "action": "folder_missing",
            "detail": "bound folder does not exist",
        }
    # Preliminary pass: a PDF becomes a note beside it, which this same scan
    # then sees as an ordinary new arrival.
    from . import pdf_intake  # lazy: keeps the converter out of import-time paths
    try:
        pdfs = pdf_intake.convert_binding(binding)
    except Exception as exc:  # noqa: BLE001 - intake never stops the binding's own scan
        log.exception("pdf intake failed for %s: %s", folder, exc)
        pdfs = [{"converted": False, "error": f"{type(exc).__name__}: {exc}"[:400]}]
    arrivals = _arrivals(binding)
    retry_docs = retryable(binding["binding_id"], max_attempts=max_attempts)
    results = []

    def _isolated(run, *, path: Path, label: str) -> None:
        try:
            results.append(run())
        except Exception as exc:  # noqa: BLE001 - isolate per document
            log.exception("document scan could not run %s (%s): %s",
                          path, label, exc)
            results.append({"path": str(path), "failed": True,
                            "error": f"{type(exc).__name__}: {exc}"[:400]})

    for path in arrivals:
        _isolated(lambda p=path: _run_one(binding["binding_id"],
                                          binding["pipeline"], p, explicit=explicit),
                  path=path, label="arrival")
    for doc in retry_docs:
        path = Path(doc["path"])
        if run_lock_held(binding["binding_id"], doc["doc_id"]):
            # A live run owns it right now (its flock is held): never adopt a
            # document out from under a running pipeline.
            continue
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
        _isolated(lambda d=doc, p=path: _run_one(
            binding["binding_id"], binding["pipeline"], p,
            doc_id=d["doc_id"], explicit=explicit),
            path=path, label="retry")
    processed = sum(1 for r in results if not r.get("skipped") and not r.get("error"))
    now = sentinel.utcnow()
    ledger.connect().execute(
        "UPDATE document_bindings SET last_scan_at=?, "
        "documents_seen=documents_seen+?, documents_processed=documents_processed+?, "
        "updated_at=? WHERE binding_id=?",
        (now, len(arrivals), processed, now, binding["binding_id"]),
    )
    return {
        "binding_id": binding["binding_id"], "folder": str(folder),
        "action": "scanned", "arrivals": len(arrivals),
        "retried": len(retry_docs), "processed": processed,
        "results": results, "last_scan_at": now,
        **({"pdf_intake": pdfs} if pdfs else {}),
    }


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
        run = _run_one(doc["binding_id"], pipeline, path, doc_id=doc["doc_id"],
                       explicit=True)
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
        binding["documents_trashed"] = counts.get("trashed", 0)
        binding["documents_gone"] = counts.get("gone", 0)
        binding["documents_exhausted"] = int(conn.execute(
            "SELECT COUNT(*) AS n FROM document_receipts WHERE binding_id=? "
            "AND exhausted_at IS NOT NULL", (binding["binding_id"],)).fetchone()["n"])
    total = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()
    return {"bindings": rows, "documents_total": int(total["n"] if total else 0),
            "scan_interval_s": SCAN_INTERVAL_S, "max_attempts": MAX_ATTEMPTS}


def worker_heartbeat_path() -> Path:
    from . import paths
    return paths.VAR / "worker-heartbeat.json"


def write_worker_heartbeat(*, tick: int = 0, scanning_documents: bool = False,
                           busy: Optional[str] = None,
                           busy_since: Optional[str] = None,
                           pid: Optional[int] = None) -> None:
    """The worker's liveness proof, written by a dedicated live thread.

    The beat is written on a timer by the worker's OWN heartbeat thread while
    the worker is alive — including mid-``run_once``, which can hold a
    transcription for the better part of an hour — so a fresh file means a
    live thread wrote it, not that a loop counter once reached a number. The
    catalog reads this to say 'worker alive' vs 'nobody scans' — the
    difference between a healthy idle binding and a dead scheduler.
    """
    path = worker_heartbeat_path()
    payload = {"at": sentinel.utcnow(), "tick": tick,
               "pid": pid if pid is not None else os.getpid(),
               "busy": busy,
               "pipeline_enabled": _pipeline_enabled_cached(),
               "documents": {"interval_s": SCAN_INTERVAL_S,
                             "scanning": scanning_documents or busy == "documents"}}
    if busy_since:
        payload["busy_since"] = busy_since
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
        "pid": payload.get("pid"),
        "busy": payload.get("busy"),
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
