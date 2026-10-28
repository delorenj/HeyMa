"""SQLite capture queue for the `transcriptions` store (design D.2).

Lives in the Wax ledger (`var/wax.db`), created by `_ensure()` in the style of
`events._ensure`. A row is the durable record of one version (the full note
text at capture, because the vault has no git and the next pass overwrites the
file) or of one pass run. "Projected" means committed in Postgres; a row whose
projection failed stays pending with `attempts`/`last_error`, except a row the
extractor rejects outright (`rejected: ...`), which is marked so it cannot
block the rows after it.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from .. import ledger, sentinel

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript_snapshots (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    md_path         TEXT,
    note_text       TEXT,
    payload         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    projected_at    TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS transcript_snapshots_pending ON transcript_snapshots(projected_at, item_id, seq);
"""

BREAKER_KEY = "transcriptions.sync_breaker_until"
REJECTED_PREFIX = "rejected: "


def _conn():
    # Both statements are IF NOT EXISTS and cost microseconds; running them on
    # every call (as events._ensure does) survives a ledger swapped under tests.
    conn = ledger.connect()
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    return conn


def _ensure() -> None:
    _conn()


def enqueue(*, item_id: str, kind: str, idempotency_key: str, md_path: Optional[str],
            note_text: Optional[str], payload: dict[str, Any]) -> Optional[int]:
    """INSERT OR IGNORE one snapshot; returns its seq, or None when it was already queued."""
    if kind not in ("version", "pass_run"):
        raise ValueError(f"unknown snapshot kind {kind!r}")
    cur = _conn().execute(
        "INSERT OR IGNORE INTO transcript_snapshots(item_id,kind,idempotency_key,md_path,note_text,payload,"
        "created_at) VALUES(?,?,?,?,?,?,?)",
        (item_id, kind, idempotency_key, md_path, note_text,
         json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str), sentinel.utcnow()))
    return cur.lastrowid if cur.rowcount else None


def _row(row) -> dict[str, Any]:
    out = dict(row)
    try:
        out["payload"] = json.loads(out.get("payload") or "{}")
    except (TypeError, ValueError):
        out["payload"] = {}
    return out


def pending(item_id: Optional[str] = None, *, limit: int = 200) -> list[dict[str, Any]]:
    """Unprojected snapshots in seq order, for one item or all."""
    conn = _conn()
    if item_id is None:
        rows = conn.execute("SELECT * FROM transcript_snapshots WHERE projected_at IS NULL "
                            "ORDER BY seq LIMIT ?", (int(limit),)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM transcript_snapshots WHERE projected_at IS NULL AND item_id=? "
                            "ORDER BY seq LIMIT ?", (item_id, int(limit))).fetchall()
    return [_row(r) for r in rows]


def pending_items(*, limit: int = 200) -> list[str]:
    """Items with pending snapshots, oldest first."""
    rows = _conn().execute(
        "SELECT item_id, MIN(seq) AS first FROM transcript_snapshots WHERE projected_at IS NULL "
        "GROUP BY item_id ORDER BY first LIMIT ?", (int(limit),)).fetchall()
    return [r["item_id"] for r in rows]


def has_pending(item_id: str) -> bool:
    return _conn().execute("SELECT 1 FROM transcript_snapshots WHERE projected_at IS NULL AND item_id=? LIMIT 1",
                           (item_id,)).fetchone() is not None


def mark(seq: int, *, error: Optional[str] = None) -> None:
    """Mark projected (optionally with a non-blocking rejection note)."""
    _conn().execute("UPDATE transcript_snapshots SET projected_at=?, last_error=COALESCE(?, last_error) "
                    "WHERE seq=?", (sentinel.utcnow(), error, int(seq)))


def fail(seq: int, error: str) -> None:
    _conn().execute("UPDATE transcript_snapshots SET attempts=attempts+1, last_error=? WHERE seq=?",
                    (str(error)[:500], int(seq)))


def backlog() -> dict[str, Any]:
    """{queued, oldest_created_at, attempts_max, rejected} for status (SQLite only)."""
    conn = _conn()
    row = conn.execute("SELECT COUNT(*) AS n, MIN(created_at) AS oldest, MAX(attempts) AS attempts "
                       "FROM transcript_snapshots WHERE projected_at IS NULL").fetchone()
    rejected = conn.execute("SELECT COUNT(*) FROM transcript_snapshots WHERE projected_at IS NOT NULL "
                            "AND last_error LIKE ?", (REJECTED_PREFIX + "%",)).fetchone()[0]
    return {"queued": int(row["n"] or 0), "oldest_created_at": row["oldest"],
            "attempts_max": int(row["attempts"] or 0), "rejected": int(rejected or 0)}


# ---------------------------------------------------------------- breaker --

def breaker_open(now: Optional[float] = None) -> bool:
    try:
        until = float(ledger.meta_get(BREAKER_KEY) or 0)
    except (TypeError, ValueError):
        until = 0.0
    return (now or time.time()) < until


def trip_breaker(seconds: float, now: Optional[float] = None) -> None:
    ledger.meta_set(BREAKER_KEY, f"{(now or time.time()) + float(seconds):.3f}")


def reset_breaker() -> None:
    ledger.meta_set(BREAKER_KEY, "0")
