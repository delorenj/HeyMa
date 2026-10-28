"""Search, show and history over the `transcriptions` store (design C.4, D.7).

`run()` embeds the query (read-through cache; the RO role reads but never
writes it) and calls `tx.search`, which reads only the head (`*_current`)
tables, so every hit is the latest version. With Ollama down, or `lexical=True`,
the query runs keyword + title only and says so (`mode: lexical`).
"""

from __future__ import annotations

import datetime as dt
import decimal
import re
import uuid
from typing import Any, Iterable, Optional

from . import config, db, embed

_PARAMS = {  # python name -> tx.search parameter
    "limit": "p_limit", "since": "p_since", "until": "p_until", "classifications": "p_classifications",
    "tags": "p_tags_any", "projects": "p_project_ids_any", "origins": "p_origins",
    "min_duration": "p_min_duration_s", "max_duration": "p_max_duration_s", "min_words": "p_min_words",
    "exact_below": "p_exact_below", "k_chunks": "p_k_chunks", "k_docs": "p_k_docs",
}
_RESULT_COLUMNS = ("transcript_id", "wax_item_id", "version_id", "version_no", "produced_by", "display_title",
                   "summary", "classification", "tags", "origin", "effective_at", "audio_duration_s", "note_path",
                   "audio_s3_uri", "audio_https_url", "score", "rank_chunk_vec", "rank_chunk_fts",
                   "rank_doc_vec", "rank_doc_lex", "best_chunk_id", "best_chunk_ordinal",
                   "best_chunk_start_s", "snippet")


def parse_when(value: Any) -> Optional[dt.datetime]:
    """ISO date/datetime from the CLI; naive values are local wall clock (WAX_TRANSCRIPTS_LOCAL_TZ)."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=config.local_tz())
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=config.local_tz())
    text = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            day = dt.date.fromisoformat(text)
            return dt.datetime(day.year, day.month, day.day, tzinfo=config.local_tz())
        stamp = dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        raise ValueError(f"not an ISO date/datetime: {value!r}") from None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=config.local_tz())


def _list(value: Optional[Iterable[str]]) -> Optional[list[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    items = [str(v) for v in value if str(v).strip()]
    return items or None


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in row.items():
        if isinstance(value, (dt.datetime, dt.date)):
            out[key] = value.isoformat()
        elif isinstance(value, decimal.Decimal):
            out[key] = float(value)
        elif isinstance(value, uuid.UUID):
            out[key] = str(value)
        else:
            out[key] = value
    return out


def run(query: str, *, limit: int = 10, since: Any = None, until: Any = None,
        classifications: Optional[Iterable[str]] = None, tags: Optional[Iterable[str]] = None,
        projects: Optional[Iterable[str]] = None, origins: Optional[Iterable[str]] = None,
        min_duration: Optional[float] = None, max_duration: Optional[float] = None,
        min_words: Optional[int] = None, lexical: bool = False, conn: Any = None,
        embedder: Optional[embed.OllamaEmbedder] = None, **tuning: Any) -> dict[str, Any]:
    """Hybrid (or lexical) search over head versions. Returns {mode, query, results, ...}."""
    query = (query or "").strip()
    if not query:
        raise ValueError("empty search query")
    own = conn is None
    if own:
        conn = db.connect("app", statement_timeout_ms=db.SEARCH_STATEMENT_TIMEOUT_MS, autocommit=True)
    out: dict[str, Any] = {"query": query, "mode": "lexical"}
    try:
        vector = None
        if not lexical:
            try:
                vector = embed.query_vector(conn, query, embedder=embedder)
                out["mode"] = "hybrid"
            except embed.EmbedError as exc:
                out["degraded"] = f"{exc.reason_code}: {exc}"
        args: dict[str, Any] = {"limit": int(limit), "since": parse_when(since), "until": parse_when(until),
                                "classifications": _list(classifications), "tags": _list(tags),
                                "projects": _list(projects), "origins": _list(origins),
                                "min_duration": min_duration, "max_duration": max_duration,
                                "min_words": min_words}
        args.update({k: v for k, v in tuning.items() if k in _PARAMS})
        named = ["p_query => %s"]
        params: list[Any] = [query]
        if vector is not None:
            named.append("p_query_vec => %s::halfvec")
            params.append(vector)
        for name, value in args.items():
            if value is None:
                continue
            cast = {"p_since": "::timestamptz", "p_until": "::timestamptz", "p_min_duration_s": "::numeric",
                    "p_max_duration_s": "::numeric", "p_classifications": "::text[]", "p_tags_any": "::text[]",
                    "p_project_ids_any": "::text[]", "p_origins": "::text[]"}.get(_PARAMS[name], "::integer")
            named.append(f"{_PARAMS[name]} => %s{cast}")
            params.append(value)
        rows = conn.execute(f"SELECT * FROM tx.search({', '.join(named)})", params).fetchall()
        out["results"] = [_jsonable(dict(zip(_RESULT_COLUMNS, row))) for row in rows]
        out["count"] = len(out["results"])
    finally:
        if own:
            conn.close()
    return out


# ---------------------------------------------------------------- show/history --

def resolve(conn: Any, ref: str) -> Optional[int]:
    """transcript_id for a wax item id, '#<transcript_id>', or a note filename/path (exact, then fuzzy)."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    if ref.startswith("#") and ref[1:].isdigit():
        row = conn.execute("SELECT transcript_id FROM tx.transcript WHERE transcript_id = %s", (int(ref[1:]),)).fetchone()
        return row[0] if row else None
    if re.fullmatch(r"[0-9a-f]{16}", ref):
        row = conn.execute("SELECT transcript_id FROM tx.transcript WHERE wax_item_id = %s", (ref,)).fetchone()
        if row:
            return row[0]
    name = ref.rsplit("/", 1)[-1]
    for candidate in (ref, name, name if name.endswith(".md") else name + ".md"):
        row = conn.execute("SELECT transcript_id FROM tx.transcript WHERE note_relpath = %s", (candidate,)).fetchone()
        if row:
            return row[0]
        row = conn.execute("SELECT transcript_id FROM tx.transcript_path WHERE note_relpath = %s "
                           "ORDER BY last_seen_at DESC LIMIT 1", (candidate,)).fetchone()
        if row:
            return row[0]
    row = conn.execute("SELECT transcript_id FROM tx.transcript WHERE note_relpath %% %s "
                       "ORDER BY similarity(note_relpath, %s) DESC, transcript_id LIMIT 1", (name, name)).fetchone()
    return row[0] if row else None


def _identity(conn: Any, transcript_id: int) -> dict[str, Any]:
    cur = conn.execute("SELECT * FROM tx.transcript WHERE transcript_id = %s", (transcript_id,))
    names = [d.name for d in cur.description]
    return _jsonable(dict(zip(names, cur.fetchone())))


def show(ref: str, *, version: Optional[int] = None, raw: bool = False, conn: Any = None) -> dict[str, Any]:
    """Identity plus one version (latest by default); `raw` adds the exact note bytes."""
    own = conn is None
    if own:
        conn = db.connect("app", statement_timeout_ms=db.SEARCH_STATEMENT_TIMEOUT_MS, autocommit=True)
    try:
        transcript_id = resolve(conn, ref)
        if transcript_id is None:
            raise LookupError(f"no transcript matches {ref!r}")
        identity = _identity(conn, transcript_id)
        if version is None:
            where, params = "v.version_id = t.current_version_id", (transcript_id,)
        else:
            where, params = "v.version_no = %s", (transcript_id, int(version))
        cur = conn.execute(
            "SELECT v.*, b.body, b.word_count AS body_word_count, b.speaker_count "
            "  FROM tx.transcript t JOIN tx.transcript_version v ON v.transcript_id = t.transcript_id "
            "  JOIN tx.transcript_body b ON b.body_sha256 = v.body_sha256 "
            f" WHERE t.transcript_id = %s AND {where}", params)
        row = cur.fetchone()
        out: dict[str, Any] = {"transcript": identity, "version": None}
        if row is not None:
            record = dict(zip([d.name for d in cur.description], row))
            body = record.pop("body")
            block = record["frontmatter_block"]
            out["version"] = _jsonable(record)
            out["content"] = body
            if raw:
                out["raw"] = block + body
        latest = conn.execute("SELECT max(version_no) FROM tx.transcript_version WHERE transcript_id = %s",
                              (transcript_id,)).fetchone()[0]
        out["latest_version_no"] = latest
        return out
    finally:
        if own:
            conn.close()


def history(ref: str, *, conn: Any = None) -> dict[str, Any]:
    """Every version (with body_changed) and every pass run of one transcript."""
    own = conn is None
    if own:
        conn = db.connect("app", statement_timeout_ms=db.SEARCH_STATEMENT_TIMEOUT_MS, autocommit=True)
    try:
        transcript_id = resolve(conn, ref)
        if transcript_id is None:
            raise LookupError(f"no transcript matches {ref!r}")
        cur = conn.execute("SELECT * FROM tx.transcript_history WHERE transcript_id = %s ORDER BY version_no",
                           (transcript_id,))
        names = [d.name for d in cur.description]
        versions = [_jsonable(dict(zip(names, r))) for r in cur.fetchall()]
        cur = conn.execute("SELECT pass_run_id, slug, pass_version, attempt, command_id, state, reason_code, "
                           "detail, model, served_by, duration_s, finished_at, version_id, outcome, source "
                           "FROM tx.pass_run WHERE transcript_id = %s ORDER BY finished_at, pass_run_id",
                           (transcript_id,))
        names = [d.name for d in cur.description]
        runs = [_jsonable(dict(zip(names, r))) for r in cur.fetchall()]
        paths = [_jsonable(dict(zip(("note_relpath", "role", "first_seen_at", "last_seen_at"), r))) for r in
                 conn.execute("SELECT note_relpath, role, first_seen_at, last_seen_at FROM tx.transcript_path "
                              "WHERE transcript_id = %s ORDER BY first_seen_at", (transcript_id,)).fetchall()]
        return {"transcript": _identity(conn, transcript_id), "versions": versions, "pass_runs": runs,
                "paths": paths}
    finally:
        if own:
            conn.close()
