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
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from . import paths, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

# How many of the newest transcripts have to come back undiarized before we call
# diarization degraded. 5 is chosen to be longer than any plausible run of
# genuinely single-speaker recordings (a voice memo streak), yet short enough
# that a fix clears the flag within a day of normal recording volume — the
# whisperlivekit outage sat at 100% undiarized for a week with nothing to show
# for it.
DIARIZATION_SAMPLE = 5

# Authority is transcribe_adapter.transcribe_env(): diarization is ON unless
# WAX_DIARIZATION explicitly says otherwise. Duplicated rather than imported
# because transcribe_adapter imports this module.
_DIARIZATION_OFF = {"0", "false", "no", "off"}

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
    version    INTEGER NOT NULL DEFAULT 1,
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
        # CREATE TABLE IF NOT EXISTS does not evolve ledgers created by older
        # Wax versions. Pass versions are what let an upgraded EP run once
        # again without repeatedly rerunning an already-completed definition.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(passes)")}
        if "version" not in columns:
            try:
                conn.execute("ALTER TABLE passes ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            except sqlite3.OperationalError as exc:
                # A simultaneous CLI/daemon cold start may have won the same
                # additive migration after our PRAGMA read.
                if "duplicate column" not in str(exc).lower():
                    raise
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


def counts(live_only: bool = False) -> dict[str, int]:
    """Item states by count. `live_only` drops rows whose file is gone.

    Both views are legitimate and they answer different questions, so callers
    must pick deliberately. The unfiltered view is the HISTORICAL record — it
    includes items whose audio now exists only in S3, which is the normal end
    state for an archived recording and must not read as data loss. The live
    view is the only one comparable to inbox/queue counts, which are derived
    from disk: 3 rows still pointing under the retired /home/delorenj/audio/
    root pinned items.pending at 2 forever while inbox.pending and queue.total
    were 0, and every human reading that pair concluded the wrong thing.

    Filtering costs one stat() per row — measured 0.139 ms for all 206 rows via
    os.path.exists (Path.exists is 3.5x that), which is affordable at waxd's
    1 Hz tick.
    """
    conn = connect()
    if not live_only:
        out = {r["state"]: r["n"] for r in
               conn.execute("SELECT state, COUNT(*) AS n FROM items GROUP BY state")}
    else:
        out = {}
        for row in conn.execute("SELECT state, path FROM items"):
            if not os.path.exists(row["path"]):
                continue
            out[row["state"]] = out.get(row["state"], 0) + 1
    out["total"] = sum(out.values())
    return out


def inbox_counts() -> dict[str, int]:
    """Ledger states for files physically present in the live inbox."""
    from . import state as state_mod
    paths_now = [str(path) for path in state_mod.inbox_items()]
    out = {"total": len(paths_now), "actionable": 0, "failed": 0}
    if not paths_now:
        return out
    placeholders = ",".join("?" for _ in paths_now)
    rows = connect().execute(
        f"SELECT state,COUNT(*) AS n FROM items WHERE path IN ({placeholders}) GROUP BY state",
        paths_now,
    ).fetchall()
    by_state = {row["state"]: row["n"] for row in rows}
    out["actionable"] = sum(by_state.get(state, 0) for state in ("pending", "archived", "transcribed"))
    out["failed"] = sum(by_state.get(state, 0) for state in ("failed", "suspect"))
    out["known"] = sum(by_state.values())
    return out


# ------------------------------------------------------------ stage health ----
#
# Item state answers "did the file get through the pipeline"; it says nothing
# about whether the value-producing sub-stages actually produced value. Both
# outages of 2026-08 (a deleted Ollama model, a deleted whisperlivekit subtree)
# degraded by returning EMPTY rather than failing, so every item still reached
# `complete` and every human-facing surface stayed green for a week. These
# helpers give the snapshot somewhere to put that.


def _enabled_pass_versions() -> dict[str, int]:
    """{slug: version} for currently-ENABLED passes, from the YAML registry.

    Imported lazily: passes.py imports this module at module scope, so a
    top-level `from . import passes` is a genuine cycle.

    Not cached. registry() measures 1.46 ms per load (6 YAML files); at the 1 Hz
    waxd tick that is ~0.15% of a core, cheap enough to prefer freshness over a
    cache that could pin a stale version across an EP version bump — and clearing
    stale-version failures is precisely what the version gate below exists for.
    """
    from . import passes
    return {slug: int(ep.get("version") or 1)
            for slug, ep in passes.registry().items() if ep.get("enabled")}


def _current_pass_gate() -> tuple[str, list[Any]]:
    """SQL predicate + params matching pass rows of enabled passes AT their
    currently-registered version, and the empty predicate when none qualify.

    The version half is load-bearing: bumping an EP's version is how a fixed
    pass is declared different from the one that failed, so a v1 failure must
    stop counting the moment the registry says v2 — otherwise the 11 title-slug
    failures from the dead-model outage stay red forever and the signal is dead.

    No MAX(attempt) subquery is needed: passes' PRIMARY KEY is (item_id,
    ep_slug), so the single stored row per pair IS the latest attempt.
    """
    try:
        versions = _enabled_pass_versions()
    except Exception as exc:  # noqa: BLE001 - registry trouble must not blind status
        log.warning("pass registry unreadable, pass health suppressed: %s: %s",
                    type(exc).__name__, exc)
        return "0", []
    if not versions:
        return "0", []
    params: list[Any] = []
    for slug, version in sorted(versions.items()):
        params.extend((slug, version))
    return "(" + " OR ".join("(ep_slug=? AND version=?)" for _ in versions) + ")", params


def passes_failed() -> dict[str, Any]:
    """Distinct items whose latest attempt of a current, enabled pass failed."""
    gate, params = _current_pass_gate()
    rows = connect().execute(
        f"SELECT DISTINCT item_id, ep_slug FROM passes WHERE state='failed' AND {gate}",
        params,
    ).fetchall()
    return {
        "failed": len({row["item_id"] for row in rows}),
        "slugs": sorted({row["ep_slug"] for row in rows}),
    }


def diarization_health() -> dict[str, Any]:
    """Whether the newest transcripts came back with speakers attached."""
    if os.environ.get("WAX_DIARIZATION", "").lower() in _DIARIZATION_OFF:
        return {"degraded": False, "recent_undiarized": 0}
    rows = connect().execute(
        "SELECT diarized FROM transcripts ORDER BY created_at DESC LIMIT ?",
        (DIARIZATION_SAMPLE,),
    ).fetchall()
    # NULL counts as undiarized: it means no diarization evidence was recorded,
    # which is the same operational fact as a 0. (Only 4 such rows exist, all
    # from 2026-07-25 before transcribe_adapter started writing the column, and
    # created_at ordering keeps them out of the newest-5 window from now on.)
    undiarized = sum(1 for row in rows if not row["diarized"])
    return {
        # A short ledger must not read as degraded — with fewer than
        # DIARIZATION_SAMPLE transcripts there is no run to judge.
        "degraded": len(rows) == DIARIZATION_SAMPLE and undiarized == DIARIZATION_SAMPLE,
        "recent_undiarized": undiarized,
    }


def failed_passes_for_sweep(max_attempts: int) -> list[tuple[str, str, int]]:
    """Stranded failed passes worth re-driving, as (item_id, slug, next_attempt).

    The third element is the attempt number the caller should hand to
    `passes.run(...)` — i.e. the recorded attempt PLUS ONE, matching how
    run_auto numbers a retry. Passing back the recorded attempt would overwrite
    the failure row in place and lose the count that bounds this sweep.

    Deliberately NOT gated on version, unlike passes_failed(): a version bump
    means the definition changed, which is the strongest possible reason to run
    it again. Gated on `enabled` so a retired pass is never resurrected, and
    joined to transcripts because a pass has nothing to operate on until the
    markdown exists.
    """
    try:
        slugs = sorted(_enabled_pass_versions())
    except Exception as exc:  # noqa: BLE001
        log.warning("pass registry unreadable, sweep found nothing: %s: %s",
                    type(exc).__name__, exc)
        return []
    if not slugs:
        return []
    placeholders = ",".join("?" for _ in slugs)
    rows = connect().execute(
        "SELECT p.item_id, p.ep_slug, p.attempt FROM passes p JOIN transcripts t USING(item_id) "
        f"WHERE p.state='failed' AND p.ep_slug IN ({placeholders}) AND p.attempt < ? "
        "ORDER BY p.updated_at",
        (*slugs, max_attempts),
    ).fetchall()
    return [(row["item_id"], row["ep_slug"], int(row["attempt"] or 0) + 1) for row in rows]


def tray_items(active_item: Optional[str] = None,
               active_stage: Optional[str] = None) -> list[dict[str, Any]]:
    """Current inbox plus completed items not yet dismissed from the tray."""
    conn = connect()
    marker = conn.execute("SELECT v FROM meta WHERE k='tray_completed_after'").fetchone()
    completed_after = marker["v"] if marker else ""
    # Aggregate the pass rows BEFORE joining them. 46 of the 47 items with pass
    # history carry two rows each, so a plain `LEFT JOIN passes` fans every one
    # of them out into duplicate tray entries.
    gate, gate_params = _current_pass_gate()
    select = (
        "SELECT i.item_id,i.orig_name,i.path,i.bytes,i.duration_s,i.state,i.updated_at,"
        "t.md_path,pf.passes_failed,pf.passes_failed_slugs "
        "FROM items i LEFT JOIN transcripts t USING(item_id) "
        "LEFT JOIN (SELECT item_id, COUNT(*) AS passes_failed, "
        "group_concat(ep_slug) AS passes_failed_slugs FROM passes "
        f"WHERE state='failed' AND {gate} GROUP BY item_id) pf USING(item_id) "
    )
    queued_rows = conn.execute(
        select + "WHERE i.state IN ('pending','archived','transcribed','failed','suspect')",
        gate_params,
    ).fetchall()
    completed = conn.execute(
        select + "WHERE i.state='complete' AND i.updated_at>? AND t.md_path IS NOT NULL "
        "ORDER BY i.updated_at DESC",
        (*gate_params, completed_after),
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
        # group_concat's ordering is unspecified; sort so a tray row's badge is
        # stable between ticks instead of shuffling its slugs.
        item["passes_failed"] = int(item.get("passes_failed") or 0)
        item["passes_failed_slugs"] = sorted(
            slug for slug in (item.get("passes_failed_slugs") or "").split(",") if slug)
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
        inbox = snap.setdefault("inbox", {})
        fs_entries = inbox.get("pending", 0)
        queue = inbox_counts()
        inbox["ledger_rows"] = queue.get("known", 0)
        inbox["reconciled"] = (queue.get("known", 0) == fs_entries)
        snap["queue"] = queue
        # A failed item intentionally remains visible, but it is not queued
        # work and must never be diagnosed as a dead worker/stranded claim.
        if (inbox.get("state") == "error" and inbox.get("cause_code") == "stranded_work"
                and queue["failed"] and not queue["actionable"]):
            inbox["cause_code"] = "failed_items"
            inbox["evidence"] = f"{queue['failed']} failed item(s) preserved in inbox"
        snap["generation"] = generation()
        # `items` stays the historical record (every row ever minted, including
        # items whose audio now lives only in S3); `items_live` is the same
        # tally restricted to files still on disk. Only the latter is comparable
        # to `inbox`/`queue`, which are derived from disk — see counts().
        snap["items"] = counts()
        snap["items_live"] = counts(live_only=True)
        # Sub-stage health. enrich() runs on the 1 Hz waxd tick, so neither block
        # may raise: a broken pass registry must degrade to "nothing to report"
        # in the snapshot and shout in the journal, never take the tray down.
        try:
            snap["passes"] = passes_failed()
        except Exception as e:  # noqa: BLE001
            log.warning("pass health unavailable: %s: %s", type(e).__name__, e)
            snap["passes"] = {"failed": 0, "slugs": []}
        try:
            snap["diarization"] = diarization_health()
        except Exception as e:  # noqa: BLE001
            log.warning("diarization health unavailable: %s: %s", type(e).__name__, e)
            snap["diarization"] = {"degraded": False, "recent_undiarized": 0}
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
