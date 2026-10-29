"""`wax transcripts browse`: the read-only listing Holocene's Transcripts tab spawns (design C.1, C.2).

docs/HOLOCENE-TRANSCRIPTS-TAB.md C.2 is the contract. Output is versioned (`schema: 1`):

    {schema, generated_at, store: {state, latest_migration}, counts, rows, projects, truncated}
    --id N  -> {schema, generated_at, store, row, tickets, pass_history}

It connects as `transcriptions_ro` (SELECT only) with a 5 s statement timeout and never calls sync,
embed, migrate or link-harvest. It reads the head (`tx.transcript_current` + its version) of
searchable notes that still exist, and only ever returns the fields listed in ROW_FIELDS,
TICKET_FIELDS and PASS_FIELDS: never the body, `frontmatter_block`, the full `frontmatter`,
`note_path`, an S3 URL or a DSN. From the version frontmatter it lifts exactly three keys
(`classification-detail`, `classification-confidence`, `classification-source`), which the v2
classifier writes; a note without them reads null.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any, Optional

from .. import frontmatter
from . import db, harvest
from .search import parse_when

SCHEMA = 1
STATEMENT_TIMEOUT_MS = 5000
DEFAULT_LIMIT = 1000
MAX_LIMIT = 2000
KINDS = frontmatter.CLASSIFICATIONS
VIEWS = (*KINDS, "unsorted", "all")
HERMES = "hermes-stt"
# A real ticket exists on a board for these publish states; the rest never reached one.
FILED = ("published", "adopted")

ROW_FIELDS = ("transcript_id", "wax_item_id", "classification", "classification_detail",
              "classification_confidence", "classification_source", "title", "summary", "effective_at",
              "duration_s", "word_count", "speaker_count", "origin", "source_hint", "tags", "project_ids",
              "projects", "language", "diarized", "audio_available", "harvest")
HARVEST_FIELDS = ("state", "run_count", "ticket_count", "item_count", "last_harvested_at")
TICKET_FIELDS = ("harvest_id", "harvested_at", "item_id", "title", "category", "item_status", "gate_state",
                 "quote", "provider", "board_id", "publish_status", "ticket_ref", "ticket_url", "project_id",
                 "lane", "published_at")
PASS_FIELDS = ("slug", "pass_version", "state", "finished_at")
COUNT_KEYS = (*KINDS, "unsorted", "hidden_hermes")

_UTC = dt.timezone.utc

_SELECT = """
SELECT t.transcript_id, t.wax_item_id,
       CASE WHEN c.classification = ANY(%(kinds)s) THEN c.classification END,
       v.frontmatter -> 'classification-detail', v.frontmatter -> 'classification-confidence',
       v.frontmatter -> 'classification-source',
       c.display_title, c.summary, t.effective_at,
       coalesce(t.audio_duration_s, t.asr_duration_s, v.fm_audio_duration_s, v.stamped_duration_s),
       c.word_count, c.speaker_count, t.origin, t.audio_source_hint, c.tags, c.project_ids, v.projects,
       coalesce(t.language, v.fm_language), c.diarized, (t.audio_s3_key IS NOT NULL)
  FROM tx.transcript t
  JOIN tx.transcript_current c USING (transcript_id)
  JOIN tx.transcript_version v ON v.version_id = c.version_id
"""
_SELECT_NAMES = ("transcript_id", "wax_item_id", "classification", "classification_detail",
                 "classification_confidence", "classification_source", "title", "summary", "effective_at",
                 "duration_s", "word_count", "speaker_count", "origin", "source_hint", "tags", "project_ids",
                 "projects", "language", "diarized", "audio_available")
_FROM_COUNTS = """
  FROM tx.transcript t
  JOIN tx.transcript_current c USING (transcript_id)
"""


# ---------------------------------------------------------------- values --

def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=_UTC)
        return stamp.astimezone(_UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fm_text(value: Any) -> Optional[str]:
    """A jsonb scalar from the version frontmatter as text (psycopg hands jsonb back as Python)."""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _confidence(value: Any) -> Optional[float]:
    number = _num(value if not isinstance(value, str) else value.strip() or None)
    return round(number, 3) if number is not None else None


def _row(record: dict[str, Any], harvest_by_id: dict[int, dict[str, Any]]) -> dict[str, Any]:
    ids = [str(p) for p in record.get("project_ids") or []]
    names = list(record.get("projects") or [])
    project_names = [str(names[i]) if i < len(names) and names[i] else pid for i, pid in enumerate(ids)]
    duration = _num(record.get("duration_s"))
    row = {
        "transcript_id": int(record["transcript_id"]),
        "wax_item_id": record.get("wax_item_id"),
        "classification": record.get("classification"),
        "classification_detail": _fm_text(record.get("classification_detail")),
        "classification_confidence": _confidence(record.get("classification_confidence")),
        "classification_source": _fm_text(record.get("classification_source")),
        "title": record.get("title"),
        "summary": record.get("summary"),
        "effective_at": _iso(record.get("effective_at")),
        "duration_s": round(duration, 3) if duration is not None else None,
        "word_count": record.get("word_count"),
        "speaker_count": record.get("speaker_count"),
        "origin": record.get("origin"),
        "source_hint": record.get("source_hint"),
        "tags": [str(t) for t in record.get("tags") or []],
        "project_ids": ids,
        "projects": project_names,
        "language": record.get("language"),
        "diarized": record.get("diarized"),
        "audio_available": bool(record.get("audio_available")),
        "harvest": harvest_by_id.get(int(record["transcript_id"])) or _no_harvest(),
    }
    return {k: row[k] for k in ROW_FIELDS}


def _no_harvest() -> dict[str, Any]:
    return {"state": "none", "run_count": 0, "ticket_count": 0, "item_count": 0, "last_harvested_at": None}


def _harvest_state(run_count: int, item_count: int, ticket_count: int) -> str:
    if not run_count:
        return "none"
    if ticket_count:
        return "harvested"
    return "no_items" if not item_count else "unfiled"


# ---------------------------------------------------------------- queries --

def _filters(*, include_hermes: bool, since: Any, until: Any, project: Optional[str],
             transcript_id: Optional[int] = None) -> tuple[list[str], dict[str, Any]]:
    clauses = ["t.searchable", "t.note_missing_at IS NULL"]
    params: dict[str, Any] = {"kinds": list(KINDS), "hermes": HERMES}
    if transcript_id is not None:
        clauses.append("t.transcript_id = %(transcript_id)s")
        params["transcript_id"] = int(transcript_id)
        return clauses, params
    if not include_hermes:
        clauses.append("t.origin <> %(hermes)s")
    when = parse_when(since)
    if when is not None:
        clauses.append("t.effective_at >= %(since)s")
        params["since"] = when
    when = parse_when(until)
    if when is not None:
        clauses.append("t.effective_at < %(until)s")
        params["until"] = when
    if project:
        clauses.append("%(project)s = ANY(c.project_ids)")
        params["project"] = str(project)
    return clauses, params


def _view_clause(view: str) -> Optional[str]:
    if view == "all":
        return None
    if view == "unsorted":
        return "(c.classification IS NULL OR NOT (c.classification = ANY(%(kinds)s)))"
    return "c.classification = %(view)s"


def _select(conn: Any, clauses: list[str], params: dict[str, Any], *, limit: Optional[int] = None
            ) -> list[dict[str, Any]]:
    sql = _SELECT + " WHERE " + " AND ".join(clauses) + \
        " ORDER BY t.effective_at DESC NULLS LAST, t.transcript_id DESC"
    if limit is not None:
        sql += " LIMIT %(limit)s"
        params = {**params, "limit": int(limit)}
    return [dict(zip(_SELECT_NAMES, r)) for r in conn.execute(sql, params).fetchall()]


def _harvest_summary(conn: Any, migrated: bool, transcript_id: Optional[int] = None) -> dict[int, dict[str, Any]]:
    """transcript_id -> harvest block, from the newest complete run (run_count counts all of them)."""
    if not migrated:
        return {}
    where = "r.transcript_id IS NOT NULL AND r.status = 'complete'"
    params: dict[str, Any] = {"filed": list(FILED)}
    if transcript_id is not None:
        where += " AND r.transcript_id = %(transcript_id)s"
        params["transcript_id"] = int(transcript_id)
    rows = conn.execute(f"""
        WITH runs AS (
            SELECT r.transcript_id, r.harvest_id, r.item_count, r.created_at,
                   row_number() OVER (PARTITION BY r.transcript_id ORDER BY r.created_at DESC, r.harvest_id DESC) AS rn,
                   count(*) OVER (PARTITION BY r.transcript_id) AS run_count,
                   max(r.created_at) OVER (PARTITION BY r.transcript_id) AS last_at
              FROM tx.harvest_run r WHERE {where})
        SELECT runs.transcript_id, runs.run_count, runs.item_count, runs.last_at,
               (SELECT count(*) FROM tx.harvest_ticket k
                 WHERE k.harvest_id = runs.harvest_id AND NOT k.is_test AND k.publish_status = ANY(%(filed)s))
          FROM runs WHERE runs.rn = 1""", params).fetchall()
    out = {}
    for tid, run_count, item_count, last_at, ticket_count in rows:
        out[int(tid)] = {"state": _harvest_state(int(run_count), int(item_count), int(ticket_count)),
                         "run_count": int(run_count), "ticket_count": int(ticket_count),
                         "item_count": int(item_count), "last_harvested_at": _iso(last_at)}
    return out


def _store(conn: Any) -> tuple[dict[str, Any], bool]:
    latest = conn.execute("SELECT max(version) FROM tx.schema_migrations").fetchone()[0]
    return ({"state": "ok", "latest_migration": str(latest)[:4] if latest else None}, harvest.migrated(conn))


def _envelope() -> dict[str, Any]:
    return {"schema": SCHEMA, "generated_at": _iso(dt.datetime.now(_UTC))}


def unreachable(detail: str) -> dict[str, Any]:
    """The payload the CLI prints (beside `error:` and exit 2) when the store cannot be read."""
    return {**_envelope(), "store": {"state": "unreachable", "detail": str(detail)[:300]},
            "counts": {}, "rows": [], "projects": [], "truncated": False}


def _connect(conn: Any) -> tuple[Any, bool]:
    if conn is not None:
        return conn, False
    return db.connect("ro", statement_timeout_ms=STATEMENT_TIMEOUT_MS, autocommit=True), True


def run(*, view: str = "all", include_hermes: bool = False, limit: int = DEFAULT_LIMIT, since: Any = None,
        until: Any = None, project: Optional[str] = None, conn: Any = None) -> dict[str, Any]:
    """The list call: counts per kind, rows of `view` (newest first), and per-project counts."""
    if view not in VIEWS:
        raise ValueError(f"unknown view {view!r}; expected one of {', '.join(VIEWS)}")
    limit = int(limit)
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"--limit must be 1..{MAX_LIMIT}")
    conn, own = _connect(conn)
    try:
        store, migrated = _store(conn)
        clauses, params = _filters(include_hermes=include_hermes, since=since, until=until, project=project)
        # Counts ignore the view (they label the toggles) and the hermes filter (to count what it hides).
        count_clauses, _ = _filters(include_hermes=True, since=since, until=until, project=project)
        counts = {k: 0 for k in COUNT_KEYS}
        for is_hermes, kind, n in conn.execute(
                "SELECT t.origin = %(hermes)s, CASE WHEN c.classification = ANY(%(kinds)s) THEN c.classification "
                "ELSE 'unsorted' END, count(*)" + _FROM_COUNTS + " WHERE " + " AND ".join(count_clauses)
                + " GROUP BY 1, 2", params).fetchall():
            if is_hermes and not include_hermes:
                counts["hidden_hermes"] += int(n)
            else:
                counts[kind] += int(n)
        view_clause = _view_clause(view)
        row_clauses = clauses + ([view_clause] if view_clause else [])
        row_params = {**params, "view": view}
        records = _select(conn, row_clauses, row_params, limit=limit + 1)
        truncated = len(records) > limit
        records = records[:limit]
        harvest_by_id = _harvest_summary(conn, migrated)
        projects = [{"project_id": pid, "name": name or pid, "count": int(n)} for pid, name, n in conn.execute(
            "SELECT p.project_id, max(p.name), count(DISTINCT t.transcript_id)" + _FROM_COUNTS
            + " JOIN tx.transcript_version v ON v.version_id = c.version_id "
            + " CROSS JOIN LATERAL unnest(c.project_ids, v.projects) AS p(project_id, name) "
            + " WHERE " + " AND ".join(row_clauses + ["p.project_id IS NOT NULL"])
            + " GROUP BY p.project_id ORDER BY 3 DESC, 1", row_params).fetchall()]
        return {**_envelope(), "store": store, "counts": counts,
                "rows": [_row(r, harvest_by_id) for r in records], "projects": projects, "truncated": truncated}
    finally:
        if own:
            conn.close()


def detail(transcript_id: int, *, conn: Any = None) -> dict[str, Any]:
    """The detail call: one row (any origin, hermes included) plus its tickets and pass history.
    Raises LookupError when no searchable, present note has that id."""
    conn, own = _connect(conn)
    try:
        store, migrated = _store(conn)
        clauses, params = _filters(include_hermes=True, since=None, until=None, project=None,
                                   transcript_id=int(transcript_id))
        records = _select(conn, clauses, params, limit=1)
        if not records:
            raise LookupError(f"no transcript #{transcript_id}")
        harvest_by_id = _harvest_summary(conn, migrated, transcript_id=int(transcript_id))
        tickets = []
        if migrated:
            cur = conn.execute(
                "SELECT " + ", ".join(TICKET_FIELDS) + " FROM tx.transcript_tickets WHERE transcript_id = %s "
                "ORDER BY harvested_at DESC, harvest_id DESC, ordinal, provider NULLS FIRST, board_id NULLS FIRST",
                (int(transcript_id),))
            for values in cur.fetchall():
                ticket = dict(zip(TICKET_FIELDS, values))
                for key in ("harvested_at", "published_at"):
                    ticket[key] = _iso(ticket[key])
                tickets.append(ticket)
        history = []
        for slug, version, state, finished in conn.execute(
                "SELECT slug, pass_version, state, finished_at FROM tx.pass_run WHERE transcript_id = %s "
                "ORDER BY finished_at, pass_run_id", (int(transcript_id),)).fetchall():
            history.append({"slug": slug, "pass_version": version, "state": state, "finished_at": _iso(finished)})
        return {**_envelope(), "store": store, "row": _row(records[0], harvest_by_id), "tickets": tickets,
                "pass_history": history}
    finally:
        if own:
            conn.close()

