"""Local embeddings through Ollama (design C.2), and the embed sweep (design D.5).

`qwen3-embedding:8b` runs on the local Ollama at `num_ctx=2048` (5.7 GB VRAM,
~85 ms warm), pinned by its full digest. Vectors are Matryoshka-truncated to
2048 dimensions and L2-normalised, so `<#>` with `halfvec_ip_ops` ranks like
cosine. Queries get the instruction prefix; documents never do. The cache key
is `(model_id, sha256(exact input))`, so a query can never collide with a chunk.

The model constants live here, not in the environment: changing them without a
migration that adds a model row and re-embeds would corrupt the index.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, Sequence

from . import config, db, gates

MODEL_NAME = "qwen3-embedding:8b"
MODEL_DIGEST = "64b933495768fbd3b87c20583d379728a07471e0c66733a9df87cd1901b3c44b"
DIMS = 2048
INSTRUCTION_VERSION = 1
QUERY_INSTRUCTION = ("Given a search query, retrieve relevant passages from the user's voice-memo "
                     "and meeting transcripts")
NUM_CTX = 2048
MAX_BATCH = 8
NORM_TOLERANCE = 1e-3


class EmbedError(RuntimeError):
    def __init__(self, message: str, reason_code: str = "embed_failed"):
        super().__init__(message)
        self.reason_code = reason_code


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def query_input(query: str) -> str:
    return f"Instruct: {QUERY_INSTRUCTION}\nQuery: {query}"


def halfvec_literal(vector: Sequence[float]) -> str:
    """'[v1,v2,...]' with `.6g` components: the text form `'...'::halfvec` accepts."""
    return "[" + ",".join(format(float(x), ".6g") for x in vector) + "]"


def normalise(vector: Sequence[float]) -> list[float]:
    """Validate length and unit norm; renormalise a vector that is off by more than 1e-3."""
    values = [float(x) for x in vector]
    if len(values) != DIMS:
        raise EmbedError(f"embedding has {len(values)} dimensions, expected {DIMS}", "bad_dimensions")
    if any(math.isnan(x) or math.isinf(x) for x in values):
        raise EmbedError("embedding contains NaN/inf", "bad_vector")
    norm = math.sqrt(sum(x * x for x in values))
    if norm == 0:
        raise EmbedError("embedding is the zero vector", "bad_vector")
    if abs(norm - 1.0) >= NORM_TOLERANCE:
        values = [x / norm for x in values]
    return values


class OllamaEmbedder:
    """POST {url}/api/embed in batches of <= 8, with the pinned-digest check."""

    def __init__(self, url: Optional[str] = None, *, batch: Optional[int] = None,
                 keep_alive: Optional[str] = None, timeout: Optional[float] = None):
        self.url = (url or config.embed_url()).rstrip("/")
        self.batch = max(1, min(MAX_BATCH, int(batch or config.embed_batch())))
        self.keep_alive = keep_alive or config.embed_keep_alive()
        self.timeout = float(timeout or config.embed_timeout_s())
        self.calls = 0
        self.inputs = 0

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise EmbedError(f"ollama {path} answered HTTP {exc.code}: {detail}", "ollama_http") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise EmbedError(f"ollama {self.url} unreachable: {exc}", "ollama_unreachable") from None
        except ValueError as exc:
            raise EmbedError(f"ollama {path} returned invalid JSON: {exc}", "ollama_bad_response") from None

    def served_digest(self) -> Optional[str]:
        tags = self._request("GET", "/api/tags")
        for model in (tags or {}).get("models") or []:
            if model.get("name") == MODEL_NAME or model.get("model") == MODEL_NAME:
                return str(model.get("digest") or "")
        return None

    def check_digest(self) -> None:
        digest = self.served_digest()
        if digest is None:
            raise EmbedError(f"ollama at {self.url} does not serve {MODEL_NAME}", "model_missing")
        if digest != MODEL_DIGEST:
            raise EmbedError(f"ollama serves {MODEL_NAME} digest {digest[:12]}, the store pins "
                             f"{MODEL_DIGEST[:12]}; refusing to embed", "digest_mismatch")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch):
            batch = list(texts[start:start + self.batch])
            body = {"model": MODEL_NAME, "input": batch, "dimensions": DIMS, "truncate": True,
                    "keep_alive": self.keep_alive, "options": {"num_ctx": NUM_CTX}}
            answer = self._request("POST", "/api/embed", body)
            self.calls += 1
            self.inputs += len(batch)
            vectors = (answer or {}).get("embeddings")
            if not isinstance(vectors, list) or len(vectors) != len(batch):
                raise EmbedError(f"ollama returned {len(vectors) if isinstance(vectors, list) else 'no'} "
                                 f"embeddings for {len(batch)} inputs", "ollama_bad_response")
            out.extend(normalise(v) for v in vectors)
        return out


# ---------------------------------------------------------------- registry --

def active_model(conn: Any) -> dict[str, Any]:
    row = conn.execute("SELECT model_id, name, digest, dims, instruction_version, query_instruction "
                       "FROM tx.embedding_model WHERE is_active").fetchone()
    if not row:
        raise EmbedError("tx.embedding_model has no active row", "model_registry_missing")
    model = dict(zip(("model_id", "name", "digest", "dims", "instruction_version", "query_instruction"), row))
    expected = (MODEL_NAME, MODEL_DIGEST, DIMS, INSTRUCTION_VERSION, QUERY_INSTRUCTION)
    found = (model["name"], model["digest"], model["dims"], model["instruction_version"], model["query_instruction"])
    if found != expected:
        raise EmbedError(f"active tx.embedding_model row {found[:4]} does not match the code constants "
                         f"{expected[:4]}; a model change is a migration", "model_registry_mismatch")
    return model


def cache_get(conn: Any, model_id: int, input_sha: str) -> Optional[str]:
    row = conn.execute("SELECT embedding::text FROM tx.embedding_cache WHERE model_id = %s AND input_sha256 = %s",
                       (model_id, input_sha)).fetchone()
    return row[0] if row else None


def cache_put(conn: Any, model_id: int, input_sha: str, kind: str, chars: int, literal: str) -> None:
    conn.execute("INSERT INTO tx.embedding_cache (model_id, input_sha256, input_kind, input_chars, embedding) "
                 "VALUES (%s, %s, %s, %s, %s::halfvec) ON CONFLICT (model_id, input_sha256) DO NOTHING",
                 (model_id, input_sha, kind, chars, literal))


def can_write_cache(conn: Any) -> bool:
    row = conn.execute("SELECT has_table_privilege('tx.embedding_cache', 'INSERT')").fetchone()
    return bool(row and row[0])


def query_vector(conn: Any, query: str, *, embedder: Optional[OllamaEmbedder] = None,
                 cache: Optional[bool] = None) -> str:
    """The halfvec literal for a query, read through the cache (written only by roles allowed to)."""
    model = active_model(conn)
    text = query_input(query)
    digest = sha256(text)
    hit = cache_get(conn, model["model_id"], digest)
    if hit:
        return hit
    embedder = embedder or OllamaEmbedder()
    literal = halfvec_literal(embedder.embed([text])[0])
    if cache is None:
        cache = can_write_cache(conn)
    if cache:
        with conn.transaction():
            cache_put(conn, model["model_id"], digest, "query", len(text), literal)
    return literal


# ---------------------------------------------------------------- sweep --

_PENDING_CHUNKS = """
SELECT c.chunk_id, c.transcript_id, c.search_text, c.search_sha256, t.wax_item_id,
       (SELECT min(v.created_at) FROM tx.transcript_version v
         WHERE v.transcript_id = c.transcript_id AND v.body_sha256 = c.body_sha256) AS body_since
  FROM tx.chunk_current c
  JOIN tx.transcript t USING (transcript_id)
 WHERE t.searchable
   AND NOT EXISTS (SELECT 1 FROM tx.chunk_vector_current v WHERE v.chunk_id = c.chunk_id)
 ORDER BY t.effective_at DESC NULLS LAST, c.transcript_id, c.ordinal
 LIMIT %s
"""

_PENDING_DOCS = """
SELECT c.transcript_id, c.doc_input, c.doc_input_sha256
  FROM tx.transcript_current c
  JOIN tx.transcript t USING (transcript_id)
 WHERE t.searchable AND c.doc_input IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM tx.doc_vector d
                    WHERE d.transcript_id = c.transcript_id AND d.input_sha256 = c.doc_input_sha256)
 ORDER BY t.effective_at DESC NULLS LAST, c.transcript_id
 LIMIT %s
"""


def pending_counts(conn: Any) -> dict[str, Any]:
    chunks, oldest = conn.execute(
        "SELECT count(*), min((SELECT min(v.created_at) FROM tx.transcript_version v "
        "                       WHERE v.transcript_id = c.transcript_id AND v.body_sha256 = c.body_sha256)) "
        "  FROM tx.chunk_current c JOIN tx.transcript t USING (transcript_id) "
        " WHERE t.searchable AND NOT EXISTS (SELECT 1 FROM tx.chunk_vector_current v WHERE v.chunk_id = c.chunk_id)"
    ).fetchone()
    docs = conn.execute(
        "SELECT count(*) FROM tx.transcript_current c JOIN tx.transcript t USING (transcript_id) "
        " WHERE t.searchable AND c.doc_input IS NOT NULL AND NOT EXISTS (SELECT 1 FROM tx.doc_vector d "
        "   WHERE d.transcript_id = c.transcript_id AND d.input_sha256 = c.doc_input_sha256)").fetchone()[0]
    return {"chunks_pending": int(chunks), "docs_pending": int(docs),
            "oldest_pending_at": oldest.isoformat() if oldest else None}


def _eligible(rows: list[tuple], settled: Callable[[Optional[str]], bool], now: dt.datetime) -> list[tuple]:
    out = []
    cache: dict[Optional[str], bool] = {}
    for row in rows:
        item = row[4]
        if item not in cache:
            try:
                cache[item] = settled(item)
            except Exception:  # noqa: BLE001 - an unreadable plan never blocks embedding
                cache[item] = True
        since = row[5]
        old = since is not None and (now - since).total_seconds() >= config.EMBED_FORCE_AFTER_S
        if cache[item] or old:
            out.append(row)
    return out


def sweep(*, max_batches: int = 8, until_empty: bool = False, limit: int = 0, conn: Any = None,
          embedder: Optional[OllamaEmbedder] = None,
          settled: Optional[Callable[[Optional[str]], bool]] = None) -> dict[str, Any]:
    """Embed pending chunk vectors (newest first), then doc vectors.

    Each batch is one Ollama call of <= 8 inputs and one transaction. Inputs
    already in the cache cost no call. `limit` caps the inputs embedded
    (0 = no cap); `until_empty` ignores `max_batches`.
    """
    own = conn is None
    if own:
        conn = db.connect("app", autocommit=True)
    embedder = embedder or OllamaEmbedder()
    settled = settled or gates.body_settled
    stats: dict[str, Any] = {"batches": 0, "embedded": 0, "from_cache": 0, "chunks_filled": 0, "docs_filled": 0,
                             "deferred": 0, "calls": 0}
    started = time.time()
    try:
        model = active_model(conn)
        embedder.check_digest()
        model_id = model["model_id"]
        batch = embedder.batch

        def budget_left() -> bool:
            if limit and stats["embedded"] >= limit:
                return False
            return until_empty or stats["batches"] < max_batches

        # ---- chunks
        skip: set[int] = set()
        deferred: set[int] = set()
        while budget_left():
            now = dt.datetime.now(dt.timezone.utc)
            fetch = max(batch * 8, 64)
            rows = [r for r in conn.execute(_PENDING_CHUNKS, (fetch + len(skip) + len(deferred),)).fetchall()
                    if r[0] not in skip and r[0] not in deferred]
            if not rows:
                break
            eligible = _eligible(rows, settled, now)
            keep = {r[0] for r in eligible}
            deferred.update(r[0] for r in rows if r[0] not in keep)
            stats["deferred"] = len(deferred)
            if not eligible:
                continue        # an unsettled body must not starve older, settled ones
            wanted: dict[str, str] = {}
            for row in eligible:
                wanted.setdefault(row[3], row[2])
                if len(wanted) >= batch:
                    break
            shas = list(wanted)
            cached = {r[0] for r in conn.execute(
                "SELECT input_sha256 FROM tx.embedding_cache WHERE model_id = %s AND input_sha256 = ANY(%s)",
                (model_id, shas)).fetchall()}
            todo = [s for s in shas if s not in cached]
            if limit:
                todo = todo[:max(0, limit - stats["embedded"])]
            vectors = embedder.embed([wanted[s] for s in todo]) if todo else []
            with conn.transaction():
                for sha, vector in zip(todo, vectors):
                    cache_put(conn, model_id, sha, "chunk", len(wanted[sha]), halfvec_literal(vector))
                filled = conn.execute(
                    "INSERT INTO tx.chunk_vector_current (chunk_id, transcript_id, model_id, embedding) "
                    "SELECT c.chunk_id, c.transcript_id, e.model_id, e.embedding "
                    "  FROM tx.chunk_current c "
                    "  JOIN tx.embedding_cache e ON e.model_id = %s AND e.input_sha256 = c.search_sha256 "
                    " WHERE c.search_sha256 = ANY(%s) "
                    "   AND NOT EXISTS (SELECT 1 FROM tx.chunk_vector_current v WHERE v.chunk_id = c.chunk_id) "
                    "ON CONFLICT (chunk_id) DO NOTHING", (model_id, shas)).rowcount
            stats["chunks_filled"] += max(0, filled)
            stats["embedded"] += len(todo)
            stats["from_cache"] += len(shas) - len(todo)
            if todo:
                stats["batches"] += 1
            if filled <= 0 and not todo:
                # Nothing could be filled (a chunk vanished mid-sweep): never spin on it.
                skip.update(r[0] for r in eligible if r[3] in wanted)

        # ---- doc vectors
        while budget_left():
            rows = conn.execute(_PENDING_DOCS, (batch,)).fetchall()
            if not rows:
                break
            shas = {r[2]: r[1] for r in rows}
            cached = {r[0] for r in conn.execute(
                "SELECT input_sha256 FROM tx.embedding_cache WHERE model_id = %s AND input_sha256 = ANY(%s)",
                (model_id, list(shas))).fetchall()}
            todo = [s for s in shas if s not in cached]
            if limit:
                todo = todo[:max(0, limit - stats["embedded"])]
            vectors = embedder.embed([shas[s] for s in todo]) if todo else []
            with conn.transaction():
                for sha, vector in zip(todo, vectors):
                    cache_put(conn, model_id, sha, "doc", len(shas[sha]), halfvec_literal(vector))
                filled = 0
                for transcript_id, _doc, sha in rows:
                    filled += max(0, conn.execute(
                        "INSERT INTO tx.doc_vector (transcript_id, input_sha256, model_id, embedding) "
                        "SELECT %s, e.input_sha256, e.model_id, e.embedding FROM tx.embedding_cache e "
                        " WHERE e.model_id = %s AND e.input_sha256 = %s "
                        "   AND EXISTS (SELECT 1 FROM tx.transcript_current c "
                        "                WHERE c.transcript_id = %s AND c.doc_input_sha256 = %s) "
                        "ON CONFLICT (transcript_id) DO UPDATE SET input_sha256 = EXCLUDED.input_sha256, "
                        "   model_id = EXCLUDED.model_id, embedding = EXCLUDED.embedding, embedded_at = now()",
                        (transcript_id, model_id, sha, transcript_id, sha)).rowcount)
            stats["docs_filled"] += filled
            stats["embedded"] += len(todo)
            stats["from_cache"] += len(shas) - len(todo)
            if todo:
                stats["batches"] += 1
            if not todo and filled == 0:
                break
        stats["calls"] = embedder.calls
        stats.update(pending_counts(conn))
    finally:
        if own:
            conn.close()
    stats["seconds"] = round(time.time() - started, 2)
    return stats
