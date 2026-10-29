"""Harvest ticket links: `px harvest` artifacts -> tx.harvest_* (docs/HOLOCENE-TRANSCRIPTS-TAB.md B.3-B.5).

Pilot writes one `actionitems.json` per harvested document (kind `px.harvest.actionitems`,
version 2) under its configured `harvest.outputDir`, and records every board it filed an
item on in that item's `publish` map. Pilot has no database and must not depend on Wax, so
Wax reads those files: `wax transcripts link-harvest [PATH...]` by hand after a publish, and
the same `link()` as a backstop from `wax transcripts sync` and the waxd tick's reconcile
cadence over `$WAX_HARVEST_DIR` (default `~/.local/share/pilot/harvest`; `0`/`off` disables).

Rules, mirroring pilot's `readArtifact` (B.4):

- an artifact must be `kind == px.harvest.actionitems`, `version == 2`, carry an `items`
  list, and every item's `gate.state` must be `clear` or `held`; anything else is reported
  as `invalid` and never ingested;
- `harvest_id` is the key. An artifact whose sha256 is already stored for its harvest_id is
  a no-op; a changed one replaces that run's items and tickets in one transaction;
- the scan stats every file and skips one whose (path, bytes, mtime_ns) is already
  recorded, so a quiet directory is never re-read;
- a run is matched to a transcript by `document.wax_item_id`, else by
  `document.body_sha256` against the head body when exactly one transcript has it (an
  ambiguous body is left unmatched, never guessed). Unmatched runs are kept, re-tried on
  every link, and listed by `--report`;
- `status: dry-run` runs and `publish[...].here` test tickets are stored and excluded
  by the `tx.transcript_tickets` view.

Runs as `transcriptions_app` (the only role with write grants on these tables).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config, db

KIND = "px.harvest.actionitems"
VERSION = 2
ARTIFACT_NAME = "actionitems.json"
DIR_ENV = "WAX_HARVEST_DIR"
DEFAULT_DIR = "~/.local/share/pilot/harvest"
QUOTE_CHARS = 400
GATE_STATES = ("clear", "held")
ITEM_STATUSES = ("firm", "tentative")
PUBLISH_STATUSES = ("unpublished", "sent", "published", "adopted", "blocked", "failed")
RUN_STATUSES = ("complete", "dry-run")
LOCK_KEY = 0x7478_6876_7374  # 'txhvst': one link-harvest writer at a time per cluster
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ITEM_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_OFF = {"0", "false", "no", "off", "none"}


class ArtifactError(ValueError):
    """The file is not a version-2 px harvest artifact Wax can ingest."""


# ---------------------------------------------------------------- reading --

def harvest_dir() -> Optional[Path]:
    """The scanned directory, or None when `$WAX_HARVEST_DIR` is 0/off."""
    raw = config.value(DIR_ENV, DEFAULT_DIR) or DEFAULT_DIR
    if raw.strip().lower() in _OFF:
        return None
    return Path(os.path.expanduser(raw.strip()))


def _when(value: Any, what: str, *, required: bool) -> Optional[dt.datetime]:
    if value in (None, ""):
        if required:
            raise ArtifactError(f"{what} is missing")
        return None
    text = str(value).strip()
    try:
        stamp = dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        if required:
            raise ArtifactError(f"{what} is not an ISO timestamp: {text[:40]!r}") from None
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def _text(value: Any, limit: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    if not text.strip():
        return None
    return text[:limit] if limit else text


def check(artifact: Any, where: str = "artifact") -> dict[str, Any]:
    """Pilot's readArtifact rules. Returns the artifact or raises ArtifactError."""
    if not isinstance(artifact, dict) or artifact.get("kind") != KIND:
        kind = artifact.get("kind") if isinstance(artifact, dict) else None
        raise ArtifactError(f"{where} is not a px harvest artifact (kind {kind or 'missing'})")
    if artifact.get("version") != VERSION:
        raise ArtifactError(f"{where} is artifact version {artifact.get('version')}; wax reads version {VERSION}"
                            + (" (re-harvest it with px harvest --force)" if artifact.get("version") == 1 else ""))
    items = artifact.get("items")
    if not isinstance(items, list):
        raise ArtifactError(f"{where} has no items list")
    for item in items:
        gate = (item or {}).get("gate") if isinstance(item, dict) else None
        state = gate.get("state") if isinstance(gate, dict) else None
        if state not in GATE_STATES:
            raise ArtifactError(f"{where} has an item with an invalid gate state ({state or 'missing'})")
    return artifact


def _quote(item: dict[str, Any]) -> Optional[str]:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    spans = evidence.get("spans")
    if isinstance(spans, list):
        for span in spans:
            if isinstance(span, dict) and _text(span.get("quote")):
                return _text(span["quote"], QUOTE_CHARS)
    return _text(evidence.get("quote"), QUOTE_CHARS)        # version-1 shape, should one ever pass


def _project(item: dict[str, Any]) -> Optional[str]:
    """pilot groups.effective(): an owner override wins over the classifier."""
    override = item.get("override") if isinstance(item.get("override"), dict) else {}
    if _text(override.get("project")):
        return str(override["project"])
    cls = item.get("classification") if isinstance(item.get("classification"), dict) else {}
    return _text(cls.get("project"))


def parse(artifact: dict[str, Any], *, path: str, sha256: str, size: Optional[int] = None,
          mtime_ns: Optional[int] = None) -> dict[str, Any]:
    """A checked artifact -> {run, items, tickets, warnings} rows (pure; no database)."""
    check(artifact, path)
    harvest_id = _text(artifact.get("harvest_id"))
    if not harvest_id:
        raise ArtifactError(f"{path} has no harvest_id")
    status = artifact.get("status")
    if status not in RUN_STATUSES:
        raise ArtifactError(f"{path} has status {status!r}; expected one of {RUN_STATUSES}")
    document = artifact.get("document") if isinstance(artifact.get("document"), dict) else {}
    s3 = document.get("s3") if isinstance(document.get("s3"), dict) else {}
    body_sha = _text(document.get("body_sha256"))
    wax_item = _text(document.get("wax_item_id"))
    warnings: list[str] = []
    items, tickets, seen = [], [], set()
    for ordinal, item in enumerate(artifact["items"], start=1):
        item_id = _text(item.get("id"))
        title = _text(item.get("title"))
        if not item_id or not title:
            warnings.append(f"item {ordinal} has no id or title; skipped")
            continue
        if item_id in seen:
            warnings.append(f"item id {item_id} repeats; the later one was skipped")
            continue
        seen.add(item_id)
        status_ = item.get("status") if item.get("status") in ITEM_STATUSES else None
        items.append({"item_id": item_id, "ordinal": ordinal, "title": title, "category": _text(item.get("category")),
                      "status": status_, "gate_state": item["gate"]["state"], "project_id": _project(item),
                      "group_kind": _text(item.get("group")) if isinstance(item.get("group"), str) else None,
                      "quote": _quote(item)})
        publish = item.get("publish") if isinstance(item.get("publish"), dict) else {}
        for key, block in publish.items():
            if not isinstance(block, dict):
                continue
            provider, _sep, board = str(key).partition(":")
            provider = _text(block.get("provider")) or provider
            board = _text(block.get("board")) or board
            state = block.get("status")
            if not provider or not board:
                warnings.append(f"item {item_id}: publish key {key!r} names no provider/board; skipped")
                continue
            if state not in PUBLISH_STATUSES:
                warnings.append(f"item {item_id}: publish status {state!r} on {key} is unknown; skipped")
                continue
            tickets.append({"item_id": item_id, "provider": provider, "board_id": board, "publish_status": state,
                            "ticket_ref": _text(block.get("ref")), "ticket_url": _text(block.get("url")),
                            "project_id": _text(block.get("project")), "lane": _text(block.get("lane")),
                            "published_at": _when(block.get("at"), "publish.at", required=False)
                            if state == "published" else None,
                            "is_test": bool(block.get("here"))})
    run = {
        "harvest_id": harvest_id, "status": status, "document_path": _text(document.get("path")),
        "body_sha256": body_sha if body_sha and _SHA_RE.match(body_sha) else None,
        "wax_item_id": wax_item if wax_item and _ITEM_ID_RE.match(wax_item) else None,
        "s3_uri": _text(s3.get("uri")), "artifact_path": path, "artifact_sha256": sha256,
        "artifact_bytes": size, "artifact_mtime_ns": mtime_ns,
        "item_count": len(items), "clear_count": sum(1 for i in items if i["gate_state"] == "clear"),
        "held_count": sum(1 for i in items if i["gate_state"] == "held"),
        "created_at": _when(artifact.get("created_at"), "created_at", required=True),
        "updated_at": _when(artifact.get("updated_at") or artifact.get("created_at"), "updated_at", required=True),
    }
    return {"run": run, "items": items, "tickets": tickets, "warnings": warnings}


def read(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """(parsed rows, file facts) for one artifact file. Raises ArtifactError or OSError."""
    resolved = Path(path).resolve()
    st = resolved.stat()
    raw = resolved.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    try:
        artifact = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArtifactError(f"{resolved} is not JSON: {exc}") from None
    facts = {"path": str(resolved), "bytes": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha}
    return parse(artifact, path=str(resolved), sha256=sha, size=st.st_size, mtime_ns=st.st_mtime_ns), facts


def discover(paths: Optional[Iterable[Any]] = None) -> tuple[list[Path], Optional[Path]]:
    """(artifact files, scanned dir). Explicit paths: a file is taken as is, a directory is searched
    for `actionitems.json`. No paths: the configured directory, or nothing when it is off/missing."""
    found: list[Path] = []
    scanned = None
    targets = [Path(os.path.expanduser(str(p))) for p in paths or []]
    if not targets:
        scanned = harvest_dir()
        targets = [scanned] if scanned is not None else []
    for target in targets:
        if target.is_dir():
            found.extend(sorted(p for p in target.rglob(ARTIFACT_NAME) if p.is_file()))
        elif target.exists() or paths:
            found.append(target)
    unique, seen = [], set()
    for p in found:
        key = str(p.resolve()) if p.exists() else str(p)
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique, scanned


# ---------------------------------------------------------------- the store --

def migrated(conn: Any) -> bool:
    return bool(conn.execute("SELECT to_regclass('tx.harvest_run') IS NOT NULL").fetchone()[0])


def _recorded_stats(conn: Any, paths: list[str]) -> set[tuple[str, int, int]]:
    if not paths:
        return set()
    return {(r[0], int(r[1]), int(r[2])) for r in conn.execute(
        "SELECT artifact_path, artifact_bytes, artifact_mtime_ns FROM tx.harvest_run "
        "WHERE artifact_path = ANY(%s) AND artifact_bytes IS NOT NULL AND artifact_mtime_ns IS NOT NULL",
        (paths,))}


_RUN_COLUMNS = ("harvest_id", "status", "document_path", "body_sha256", "wax_item_id", "s3_uri", "artifact_path",
                "artifact_sha256", "artifact_bytes", "artifact_mtime_ns", "item_count", "clear_count", "held_count",
                "created_at", "updated_at")
_ITEM_COLUMNS = ("item_id", "ordinal", "title", "category", "status", "gate_state", "project_id", "group_kind", "quote")
_TICKET_COLUMNS = ("item_id", "provider", "board_id", "publish_status", "ticket_ref", "ticket_url", "project_id",
                   "lane", "published_at", "is_test")


def store_run(conn: Any, parsed: dict[str, Any]) -> str:
    """Upsert one parsed artifact inside the caller's transaction: 'inserted' | 'replaced' |
    'refreshed' (same content, new stat) | 'unchanged'."""
    run = parsed["run"]
    row = conn.execute("SELECT artifact_sha256, artifact_bytes, artifact_mtime_ns, artifact_path, updated_at "
                       "FROM tx.harvest_run WHERE harvest_id = %s FOR UPDATE", (run["harvest_id"],)).fetchone()
    if row is not None and row[3] != run["artifact_path"] and Path(row[3]).is_file():
        # A second file claims a harvest_id whose recorded artifact is still on disk (a copy, a backup
        # kept inside the scanned directory). Flipping between them on every scan would rewrite the
        # run each tick, so the recorded file stays authoritative unless this one is strictly newer.
        if row[0] == run["artifact_sha256"] or run["updated_at"] <= row[4]:
            return "unchanged"
    if row is not None and row[0] == run["artifact_sha256"]:
        if (row[1], row[2]) == (run["artifact_bytes"], run["artifact_mtime_ns"]) or run["artifact_bytes"] is None:
            return "unchanged"
        conn.execute("UPDATE tx.harvest_run SET artifact_path = %s, artifact_bytes = %s, artifact_mtime_ns = %s "
                     "WHERE harvest_id = %s", (run["artifact_path"], run["artifact_bytes"], run["artifact_mtime_ns"],
                                               run["harvest_id"]))
        return "refreshed"
    if row is not None:
        conn.execute("DELETE FROM tx.harvest_item WHERE harvest_id = %s", (run["harvest_id"],))   # cascades tickets
    names = ", ".join(_RUN_COLUMNS)
    marks = ", ".join(["%s"] * len(_RUN_COLUMNS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _RUN_COLUMNS if c != "harvest_id")
    conn.execute(f"INSERT INTO tx.harvest_run ({names}) VALUES ({marks}) "
                 f"ON CONFLICT (harvest_id) DO UPDATE SET {updates}, transcript_id = NULL, match_method = NULL, "
                 "ingested_at = now()", [run[c] for c in _RUN_COLUMNS])
    with conn.cursor() as cur:
        if parsed["items"]:
            cur.executemany(
                f"INSERT INTO tx.harvest_item (harvest_id, {', '.join(_ITEM_COLUMNS)}) "
                f"VALUES (%s, {', '.join(['%s'] * len(_ITEM_COLUMNS))})",
                [[run["harvest_id"], *[i[c] for c in _ITEM_COLUMNS]] for i in parsed["items"]])
        if parsed["tickets"]:
            cur.executemany(
                f"INSERT INTO tx.harvest_ticket (harvest_id, {', '.join(_TICKET_COLUMNS)}) "
                f"VALUES (%s, {', '.join(['%s'] * len(_TICKET_COLUMNS))}) "
                "ON CONFLICT (harvest_id, item_id, provider, board_id) DO NOTHING",
                [[run["harvest_id"], *[t[c] for c in _TICKET_COLUMNS]] for t in parsed["tickets"]])
    return "inserted" if row is None else "replaced"


def relink(conn: Any) -> int:
    """Match every unmatched run (B.4): wax item id first, then a body sha exactly one head has."""
    by_item = conn.execute(
        "UPDATE tx.harvest_run r SET transcript_id = t.transcript_id, match_method = 'wax_item_id' "
        "  FROM tx.transcript t "
        " WHERE r.transcript_id IS NULL AND r.wax_item_id IS NOT NULL AND t.wax_item_id = r.wax_item_id").rowcount
    by_body = conn.execute(
        "UPDATE tx.harvest_run r SET transcript_id = m.transcript_id, match_method = 'body_sha256' "
        "  FROM (SELECT body_sha256, min(transcript_id) AS transcript_id FROM tx.transcript_current "
        "         GROUP BY body_sha256 HAVING count(*) = 1) m "
        " WHERE r.transcript_id IS NULL AND r.body_sha256 IS NOT NULL AND r.body_sha256 = m.body_sha256").rowcount
    return int(by_item or 0) + int(by_body or 0)


def link(paths: Optional[Iterable[Any]] = None, *, dry_run: bool = False, report: bool = False,
         conn: Any = None) -> dict[str, Any]:
    """Ingest artifacts (explicit `paths`, else the configured directory) and match them.

    Returns counts plus `invalid` (files that are not version-2 artifacts) and `errors` (store
    failures). With `dry_run` every write is rolled back. Raises StoreUnavailable on an outage.
    """
    explicit = list(paths or [])
    files, scanned = discover(explicit)
    out: dict[str, Any] = {"dir": str(scanned) if scanned else None, "dry_run": dry_run, "files": len(files),
                           "unchanged": 0, "inserted": 0, "replaced": 0, "refreshed": 0, "relinked": 0,
                           "invalid": [], "warnings": [], "errors": []}
    if not explicit and scanned is None:
        out["dir_state"] = "off"
    elif not explicit:
        out["dir_state"] = "ok" if scanned.is_dir() else "missing"
    own = conn is None
    if own:
        conn = db.connect("app", statement_timeout_ms=30000, autocommit=True)
    try:
        if not migrated(conn):
            out["state"] = "not_migrated"
            return out
        out["state"] = "ok"
        resolved = []
        for f in files:
            try:
                resolved.append((f, f.resolve(), f.stat()))
            except OSError as exc:
                out["invalid"].append({"path": str(f), "reason": f"{type(exc).__name__}: {exc.strerror or exc}"})
        recorded = set() if explicit else _recorded_stats(conn, [str(r) for _f, r, _s in resolved])
        for _f, real, st in resolved:
            if (str(real), st.st_size, st.st_mtime_ns) in recorded:
                out["unchanged"] += 1
                continue
            try:
                parsed, _facts = read(real)
            except (ArtifactError, OSError) as exc:
                out["invalid"].append({"path": str(real), "reason": str(exc)[:300]})
                continue
            out["warnings"].extend(f"{parsed['run']['harvest_id']}: {w}" for w in parsed["warnings"])
            try:
                with conn.transaction():
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                    outcome = store_run(conn, parsed)
                    if dry_run:
                        raise _Rollback(outcome)
            except _Rollback as rb:
                outcome = rb.outcome
            except db.psycopg().Error as exc:
                out["errors"].append({"path": str(real), "error": f"{type(exc).__name__}: {str(exc).strip()[:300]}"})
                continue
            out[outcome] += 1
        try:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                out["relinked"] = relink(conn)
                if dry_run:
                    raise _Rollback("relink")
        except _Rollback:
            pass
        if report:
            out["report"] = summary(conn)
        return out
    finally:
        if own:
            conn.close()


class _Rollback(Exception):
    def __init__(self, outcome: str):
        super().__init__(outcome)
        self.outcome = outcome


def summary(conn: Any) -> dict[str, Any]:
    """What is stored: runs by status and match, items, tickets, and every unmatched run."""
    one = lambda sql: int(conn.execute(sql).fetchone()[0] or 0)  # noqa: E731
    by_status = {r[0]: int(r[1]) for r in conn.execute(
        "SELECT status, count(*) FROM tx.harvest_run GROUP BY status ORDER BY status")}
    by_method = {(r[0] or "unmatched"): int(r[1]) for r in conn.execute(
        "SELECT match_method, count(*) FROM tx.harvest_run GROUP BY match_method ORDER BY match_method")}
    unmatched = [{"harvest_id": r[0], "status": r[1], "document_path": r[2], "wax_item_id": r[3],
                  "body_sha256": r[4], "artifact_path": r[5]} for r in conn.execute(
        "SELECT harvest_id, status, document_path, wax_item_id, body_sha256, artifact_path FROM tx.harvest_run "
        "WHERE transcript_id IS NULL ORDER BY created_at, harvest_id")]
    return {
        "runs": sum(by_status.values()), "runs_by_status": by_status, "runs_by_match": by_method,
        "items": one("SELECT count(*) FROM tx.harvest_item"),
        "tickets": one("SELECT count(*) FROM tx.harvest_ticket"),
        "test_tickets": one("SELECT count(*) FROM tx.harvest_ticket WHERE is_test"),
        "transcripts_with_runs": one("SELECT count(DISTINCT transcript_id) FROM tx.harvest_run "
                                     "WHERE status = 'complete' AND transcript_id IS NOT NULL"),
        "visible_rows": one("SELECT count(*) FROM tx.transcript_tickets"),
        "unmatched": unmatched,
    }


def parity(conn: Any) -> dict[str, Any]:
    """For `sync --check`: artifacts on disk the store has not taken in yet (stat, then sha)."""
    files, scanned = discover()
    out: dict[str, Any] = {"dir": str(scanned) if scanned else None, "artifacts": len(files), "pending": 0,
                           "unmatched_runs": 0}
    if scanned is None:
        out["state"] = "off"
        return out
    if not migrated(conn):
        out["state"] = "not_migrated"
        return out
    out["state"] = "ok" if scanned.is_dir() else "missing"
    stats = []
    for f in files:
        try:
            real = f.resolve()
            st = real.stat()
            stats.append((real, st))
        except OSError:
            continue
    recorded = _recorded_stats(conn, [str(r) for r, _s in stats])
    changed = [real for real, st in stats if (str(real), st.st_size, st.st_mtime_ns) not in recorded]
    shas, invalid = [], 0
    for real in changed:
        try:
            parsed, _facts = read(real)
        except (ArtifactError, OSError):
            invalid += 1             # never ingestible: reported by link, not drift
            continue
        shas.append((parsed["run"]["harvest_id"], parsed["run"]["artifact_sha256"]))
    if shas:
        known = {(r[0], r[1]) for r in conn.execute(
            "SELECT harvest_id, artifact_sha256 FROM tx.harvest_run WHERE harvest_id = ANY(%s)",
            ([h for h, _s in shas],))}
        out["pending"] = sum(1 for pair in shas if pair not in known)
    out["invalid"] = invalid
    out["unmatched_runs"] = int(conn.execute(
        "SELECT count(*) FROM tx.harvest_run WHERE transcript_id IS NULL").fetchone()[0])
    return out
