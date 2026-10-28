"""Schema migration runner for the `transcriptions` store (design D.10).

Applied as `transcriptions_owner`. Each file in `migrations/` runs in its own
transaction under a session advisory lock, and is recorded in
`tx.schema_migrations(version, sha256, applied_at)`. An applied file whose
sha256 has changed is refused: history is append-only, so a change is a new
numbered file, never an edit.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

from . import db

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
LOCK_KEY = 0x7478_6D69_6772  # 'txmigr' as an int8; one runner at a time per cluster

BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS tx;
CREATE TABLE IF NOT EXISTS tx.schema_migrations (
    version    text        PRIMARY KEY,
    sha256     text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


class MigrationError(RuntimeError):
    """An applied migration changed on disk, or a migration failed to apply."""


def files(directory: Optional[Path] = None) -> list[tuple[str, Path, str]]:
    """(version, path, sha256) for every `NNNN_*.sql`, in version order."""
    root = Path(directory or MIGRATIONS)
    out = []
    for path in sorted(root.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        out.append((path.stem, path, hashlib.sha256(path.read_bytes()).hexdigest()))
    return out


def latest_version(directory: Optional[Path] = None) -> Optional[str]:
    found = files(directory)
    return found[-1][0] if found else None


def applied(conn: Any) -> dict[str, str]:
    """version -> sha256 of what the database has applied ({} before the first run)."""
    row = conn.execute("SELECT to_regclass('tx.schema_migrations') IS NOT NULL").fetchone()
    if not row or not row[0]:
        return {}
    return {version: sha for version, sha in conn.execute(
        "SELECT version, sha256 FROM tx.schema_migrations ORDER BY version")}


def status(conn: Any, directory: Optional[Path] = None) -> dict[str, Any]:
    """What is applied versus what is on disk; `current` when nothing is pending or changed."""
    done = applied(conn)
    pending, changed = [], []
    for version, _path, sha in files(directory):
        if version not in done:
            pending.append(version)
        elif done[version] != sha:
            changed.append(version)
    return {"applied": sorted(done), "pending": pending, "changed": changed,
            "latest": latest_version(directory), "current": not pending and not changed}


def apply(*, dry_run: bool = False, conn: Any = None, directory: Optional[Path] = None) -> dict[str, Any]:
    """Apply every pending migration as the owner role. Returns what ran (or would run)."""
    own = conn is None
    if own:
        conn = db.connect("owner", statement_timeout_ms=0, autocommit=True)
    try:
        conn.autocommit = True
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            return _apply_locked(conn, dry_run=dry_run, directory=directory)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    finally:
        if own:
            conn.close()


def _apply_locked(conn: Any, *, dry_run: bool, directory: Optional[Path]) -> dict[str, Any]:
    done = applied(conn)
    plan = files(directory)
    changed = [version for version, _p, sha in plan if version in done and done[version] != sha]
    if changed:
        raise MigrationError(
            f"applied migration(s) changed on disk: {', '.join(changed)}; history is append-only, "
            "so restore the applied file and add a new numbered migration instead")
    pending = [(version, path, sha) for version, path, sha in plan if version not in done]
    out: dict[str, Any] = {"dry_run": dry_run, "applied": [], "pending": [v for v, _p, _s in pending],
                           "already_applied": sorted(done)}
    if dry_run or not pending:
        return out
    if not done:
        with conn.transaction():
            conn.execute(BOOTSTRAP)
    for version, path, sha in pending:
        sql = path.read_text(encoding="utf-8")
        try:
            with conn.transaction():
                # No parameters: psycopg sends the whole file through the simple
                # query protocol, so multi-statement files and `%` are safe.
                conn.execute(sql)
                conn.execute("INSERT INTO tx.schema_migrations(version, sha256) VALUES (%s, %s)",
                             (version, sha))
        except db.psycopg().Error as exc:
            raise MigrationError(f"{path.name} failed and was rolled back: "
                                 f"{type(exc).__name__}: {str(exc).strip()[:400]}") from None
        out["applied"].append(version)
    out["pending"] = []
    return out
