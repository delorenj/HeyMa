"""SQLite ledger: per-item state and an append-only transition log.

The filesystem stays the source of truth for *what exists* — a file in the
inbox is real whether or not the ledger knows about it, and the ledger is
rebuildable from disk plus S3 sidecars. The ledger's job is the things the
filesystem cannot express: which enrichment passes have run, what was backed
up where, and an ordered history of every state change so "it broke again" is
answerable instead of a shrug.

Identity is content, not path: item_id = sha256(bytes)[:16]. That kills two
bugs from the old pipeline at the root — n8n minting a fresh transcription_id
on every reprocess, and mtime-derived S3 keys producing byte-identical twins.

Hashing every file every tick would be absurd (there are multi-GB recordings
here), so `files_seen` caches (path, size, mtime_ns) -> item_id and we only
hash when that triple changes.
"""

import hashlib
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from . import paths, sentinel

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS items (
    item_id     TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL,
    path        TEXT NOT NULL,
    orig_name   TEXT,
    bytes       INTEGER,
    duration_s  REAL,
    origin      TEXT,
    state       TEXT NOT NULL DEFAULT 'pending',
    first_seen  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_state ON items(state);

-- Cheap identity cache so we never re-hash an unchanged file.
CREATE TABLE IF NOT EXISTS files_seen (
    path      TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    item_id   TEXT NOT NULL,
    sha256    TEXT
);

-- Append-only. Never updated, never deleted.
CREATE TABLE IF NOT EXISTS transitions (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    machine    TEXT NOT NULL,
    subject    TEXT,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    cause_code TEXT,
    evidence   TEXT,
    at         TEXT NOT NULL,
    generation INTEGER
);
CREATE INDEX IF NOT EXISTS transitions_machine ON transitions(machine, seq);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS backups (
    item_id     TEXT NOT NULL,
    s3_key      TEXT NOT NULL,
    bucket      TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    verified_at TEXT,
    method      TEXT,
    PRIMARY KEY (item_id, s3_key)
);

CREATE TABLE IF NOT EXISTS transcripts (
    item_id        TEXT PRIMARY KEY,
    md_path        TEXT NOT NULL,
    audio_duration REAL,
    asr_duration   REAL,
    duration_ratio REAL,
    word_count     INTEGER,
    diarized       INTEGER,
    engine_model   TEXT,
    created_at     TEXT NOT NULL
);

-- One row per (item, enrichment pass). Passes are INDEPENDENT: no pass may
-- gate another, so failure of one must never block the rest.
CREATE TABLE IF NOT EXISTS passes (
    item_id    TEXT NOT NULL,
    ep_slug    TEXT NOT NULL,
    state      TEXT NOT NULL,
    attempt    INTEGER NOT NULL DEFAULT 1,
    command_id TEXT,
    updated_at TEXT NOT NULL,
    detail     TEXT,
    PRIMARY KEY (item_id, ep_slug)
);
"""

_local = threading.local()


def connect() -> sqlite3.Connection:
    """One connection per thread; waxd polls on the main loop while the CLI reads."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        paths.VAR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(paths.DB, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        _local.conn = conn
    return conn


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def identify(path: Path) -> Optional[str]:
    """item_id for a file, hashing only when (size, mtime_ns) has changed."""
    try:
        st = path.stat()
    except OSError:
        return None
    conn = connect()
    row = conn.execute(
        "SELECT item_id FROM files_seen WHERE path=? AND size=? AND mtime_ns=?",
        (str(path), st.st_size, st.st_mtime_ns),
    ).fetchone()
    if row:
        return row["item_id"]

    digest = sha256_file(path)
    item_id = digest[:16]
    # Persist the FULL digest, not just the 16-char prefix. Storing only the
    # prefix meant upsert_item could not reuse it and re-hashed every brand-new
    # file a second time — measured 2x sha256 per file, which at 4.2 GB is
    # ~9 wasted seconds and 950 MB read twice for one recording.
    conn.execute(
        "INSERT INTO files_seen(path,size,mtime_ns,item_id,sha256) VALUES(?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, "
        "item_id=excluded.item_id, sha256=excluded.sha256",
        (str(path), st.st_size, st.st_mtime_ns, item_id, digest),
    )
    return item_id


def cached_sha(path: Path) -> Optional[str]:
    """Full digest for a file, computing and backfilling it if the cache lacks one.

    Rows written before files_seen gained a sha256 column carry NULL, and
    identify() returns early on any cache hit — so a NULL would silently
    propagate as "no digest". That defeated the migration's content-dedup and
    made a re-run non-idempotent. Backfill instead of returning None.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    conn = connect()
    row = conn.execute(
        "SELECT sha256 FROM files_seen WHERE path=? AND size=? AND mtime_ns=?",
        (str(path), st.st_size, st.st_mtime_ns),
    ).fetchone()
    if row and row["sha256"]:
        return row["sha256"]
    digest = sha256_file(path)
    conn.execute(
        "INSERT INTO files_seen(path,size,mtime_ns,item_id,sha256) VALUES(?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, "
        "item_id=excluded.item_id, sha256=excluded.sha256",
        (str(path), st.st_size, st.st_mtime_ns, digest[:16], digest),
    )
    return digest


def upsert_item(path: Path, *, origin: str = "manual") -> Optional[str]:
    """Register a file as a pipeline item. Idempotent on content."""
    item_id = identify(path)
    if item_id is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    now = sentinel.utcnow()
    conn = connect()
    existing = conn.execute("SELECT item_id, path FROM items WHERE item_id=?", (item_id,)).fetchone()
    if existing:
        # Same content seen at another path (moved, or a duplicate copy). Keep
        # the original state — reprocessing identical bytes is exactly what
        # content-addressing prevents. Only re-point `path` when the recorded
        # one is GONE: with two copies present, "track the newest path" makes
        # them fight and rewrites the row on every tick forever.
        if existing["path"] != str(path) and not Path(existing["path"]).exists():
            conn.execute("UPDATE items SET path=?, updated_at=? WHERE item_id=?", (str(path), now, item_id))
        return item_id

    digest = conn.execute("SELECT item_id FROM files_seen WHERE path=?", (str(path),)).fetchone()
    conn.execute(
        "INSERT INTO items(item_id,sha256,path,orig_name,bytes,origin,state,first_seen,updated_at) "
        "VALUES(?,?,?,?,?,?,'pending',?,?)",
        (item_id, _full_sha(path, item_id),
         str(path), path.name, st.st_size, origin, now, now),
    )
    record_transition("item", item_id, None, "pending", "discovered", f"origin={origin} path={path}")
    _emit_safe("file", "recorded", {
        "item_id": item_id, "orig_name": path.name, "bytes": st.st_size,
        "origin": origin, "path": str(path),
    }, ordering_key=item_id)
    return item_id


def _full_sha(path: Path, item_id: str) -> str:
    """Full digest, preferring any cache over re-reading the file."""
    cached = cached_sha(path)
    if cached:
        return cached
    row = connect().execute("SELECT sha256 FROM items WHERE item_id=?", (item_id,)).fetchone()
    return row["sha256"] if row else sha256_file(path)


def set_item_state(item_id: str, to_state: str, *, cause: str = "", evidence: str = "") -> None:
    conn = connect()
    row = conn.execute("SELECT state FROM items WHERE item_id=?", (item_id,)).fetchone()
    frm = row["state"] if row else None
    if frm == to_state:
        return
    conn.execute("UPDATE items SET state=?, updated_at=? WHERE item_id=?",
                 (to_state, sentinel.utcnow(), item_id))
    record_transition("item", item_id, frm, to_state, cause, evidence)
    ent_act = _ITEM_EVENTS.get(to_state)
    if ent_act:
        _emit_safe(ent_act[0], ent_act[1], {
            "item_id": item_id, "from_state": frm, "to_state": to_state,
            "cause_code": cause, "evidence": evidence,
        }, ordering_key=item_id)


# ---------------------------------------------------------------- meta ----

def generation() -> int:
    row = connect().execute("SELECT v FROM meta WHERE k='generation'").fetchone()
    return int(row["v"]) if row else 0


def bump_generation() -> int:
    conn = connect()
    g = generation() + 1
    conn.execute("INSERT INTO meta(k,v) VALUES('generation',?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(g),))
    return g


def record_transition(machine: str, subject: Optional[str], from_state: Optional[str],
                      to_state: str, cause_code: str = "", evidence: str = "") -> None:
    connect().execute(
        "INSERT INTO transitions(machine,subject,from_state,to_state,cause_code,evidence,at,generation) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (machine, subject, from_state, to_state, cause_code or None, evidence or None,
         sentinel.utcnow(), generation()),
    )


def last_state(machine: str, subject: Optional[str] = None) -> Optional[str]:
    conn = connect()
    if subject is None:
        row = conn.execute(
            "SELECT to_state FROM transitions WHERE machine=? ORDER BY seq DESC LIMIT 1", (machine,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT to_state FROM transitions WHERE machine=? AND subject=? ORDER BY seq DESC LIMIT 1",
            (machine, subject),
        ).fetchone()
    return row["to_state"] if row else None


def note_machine(machine: str, snap: dict[str, Any]) -> bool:
    """Record a machine transition if its state actually changed. True if logged."""
    to_state = snap.get("state")
    if not to_state:
        return False
    frm = last_state(machine)
    if frm == to_state:
        return False
    gen = bump_generation()
    record_transition(machine, None, frm, to_state,
                      snap.get("cause_code") or "", snap.get("evidence") or "")
    _emit_safe("status", "updated", {
        "machine": machine, "from": frm, "to": to_state,
        "cause_code": snap.get("cause_code"), "evidence": snap.get("evidence"),
        "generation": gen, "pending": snap.get("pending"),
    })
    return True


def _emit_safe(entity: str, action: str, data: dict[str, Any], **kw: Any) -> None:
    """Queue a Bloodbank event. Publishing is fail-open by design: a bus
    problem must never break recording or corrupt the ledger."""
    try:
        from . import events
        events.emit(entity, action, data, **kw)
    except Exception:  # noqa: BLE001
        pass


# Which item transition maps to which Bloodbank event.
_ITEM_EVENTS = {
    "pending": ("file", "recorded"),
    "archived": ("file", "sent"),
    "transcribed": ("transcription", "completed"),
    "complete": ("file", "closed"),
    "suspect": ("transcription", "failed"),
    "failed": ("transcription", "failed"),
}


# --------------------------------------------------------- reconciliation ----

def reconcile_inbox(origin: str = "manual") -> dict[str, Any]:
    """Mint ledger rows for anything sitting in the inbox.

    This is the INBOX IS INBOX guarantee: a file dropped in by hand, by
    Syncthing, by a script, or by Wax itself is all the same thing — an item.
    """
    from . import state as state_mod

    minted, present = [], []
    for p in state_mod.inbox_items():
        item_id = upsert_item(p, origin=origin)
        if item_id is None:
            continue
        present.append(item_id)
        row = connect().execute(
            "SELECT first_seen, updated_at FROM items WHERE item_id=?", (item_id,)
        ).fetchone()
        if row and row["first_seen"] == row["updated_at"]:
            minted.append(item_id)

    return {"present": len(present), "minted": len(minted), "item_ids": present}


def counts() -> dict[str, int]:
    conn = connect()
    out = {r["state"]: r["n"] for r in
           conn.execute("SELECT state, COUNT(*) AS n FROM items GROUP BY state")}
    out["total"] = sum(out.values())
    return out


def tray_items(active_item: Optional[str] = None,
               active_stage: Optional[str] = None) -> list[dict[str, Any]]:
    """Current inbox plus completed items not yet dismissed from the tray."""
    conn = connect()
    marker = conn.execute("SELECT v FROM meta WHERE k='tray_completed_after'").fetchone()
    completed_after = marker["v"] if marker else ""
    queued_rows = conn.execute(
        "SELECT item_id,orig_name,path,bytes,duration_s,state,updated_at FROM items "
        "WHERE state IN ('pending','archived','transcribed','failed','suspect')"
    ).fetchall()
    completed = conn.execute(
        "SELECT item_id,orig_name,path,bytes,duration_s,state,updated_at FROM items "
        "WHERE state='complete' AND updated_at>? ORDER BY updated_at DESC",
        (completed_after,),
    ).fetchall()
    # Match worker.next_item() exactly: it walks state.inbox_items(), whose
    # ordering is the lexical Path order. Ledger first_seen order is not the
    # processing order and made the tray actively misleading.
    queued_by_path = {row["path"]: row for row in queued_rows}
    queued = [queued_by_path[str(path)] for path in sorted(paths.INBOX.iterdir())
              if str(path) in queued_by_path] if paths.INBOX.exists() else []
    out = []
    for row in [*queued, *completed]:
        item = dict(row)
        # Old ledgers can contain resumable states whose audio was parked in a
        # previous runtime root. They are audit history, not actionable queue
        # rows, and must not expose a Skip action that can never succeed.
        if item["state"] != "complete" and Path(item["path"]).parent != paths.INBOX:
            continue
        if item["state"] in ("failed", "suspect"):
            reason = conn.execute(
                "SELECT cause_code,evidence FROM transitions WHERE subject=? "
                "ORDER BY seq DESC LIMIT 1", (item["item_id"],)
            ).fetchone()
            item["error"] = dict(reason) if reason else {}
        item["active"] = item["item_id"] == active_item
        item["stage"] = active_stage if item["active"] else None
        out.append(item)
    # Defensive: a resumed/nonstandard active item is always visually first,
    # even if a future worker policy changes its selection order.
    return sorted(out, key=lambda item: (
        not item["active"],
        item["state"] in ("failed", "suspect", "complete"),
        item["state"] == "complete",
    ))


def clear_tray_completed() -> None:
    """Persistently dismiss completed rows without deleting ledger history."""
    connect().execute(
        "INSERT INTO meta(k,v) VALUES('tray_completed_after',?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (sentinel.utcnow(),),
    )


def history(limit: int = 20, machine: Optional[str] = None) -> list[dict[str, Any]]:
    conn = connect()
    if machine:
        rows = conn.execute(
            "SELECT * FROM transitions WHERE machine=? ORDER BY seq DESC LIMIT ?", (machine, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM transitions ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def enrich(snap: dict[str, Any]) -> dict[str, Any]:
    """Layer ledger facts onto a pure filesystem snapshot.

    Kept separate so state.py stays a pure function of disk + /proc: the cold
    path must keep working when the DB is missing, locked, or being rebuilt.
    """
    try:
        conn = connect()
        rows = conn.execute("SELECT COUNT(*) AS n FROM items WHERE state='pending'").fetchone()["n"]
        inbox = snap.setdefault("inbox", {})
        fs_entries = inbox.get("pending", 0)
        inbox["ledger_rows"] = rows
        inbox["reconciled"] = (rows == fs_entries)
        snap["generation"] = generation()
        snap["items"] = counts()
        # Keep `wax status` and the state.json mirror reporting the SAME fields;
        # a key present in one and absent in the other reads as an error.
        try:
            from . import events
            snap["outbox_backlog"] = events.backlog()
        except Exception as e:  # noqa: BLE001
            snap["outbox_error"] = f"{type(e).__name__}: {e}"
    except sqlite3.Error as e:
        snap["ledger_error"] = str(e)
    return snap
