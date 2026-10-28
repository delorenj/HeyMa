"""Status and doctor data for the `transcriptions` store (design D.8).

`snapshot()` is for `wax status` / `ledger.enrich`: it reads SQLite only (the
queue, the last tick) so status never waits on Postgres. States:
`disabled` (WAX_TRANSCRIPTS=0 or no DSN), `ok`, `lagging` (oldest queued >
600 s) and `error` (oldest queued > WAX_TRANSCRIPTS_LAG_FAIL_S, an embed
backlog older than 6 h, or a model digest mismatch). Green is not evidence.

`probes()` is for `wax doctor`: seven checks against the live store, each a
`{"probe", "status", "detail", "fix"?}` row in the shape `bin/wax` uses.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

from .. import ledger
from . import config, db, embed, migrate, outbox

PASS, WARN, FAIL = "pass", "warn", "fail"
LAST_TICK = "transcriptions.last_tick"


def _age_s(iso: Optional[str], now: Optional[dt.datetime] = None) -> Optional[float]:
    if not iso:
        return None
    try:
        stamp = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return max(0.0, ((now or dt.datetime.now(dt.timezone.utc)) - stamp).total_seconds())


def last_tick() -> dict[str, Any]:
    try:
        return json.loads(ledger.meta_get(LAST_TICK) or "{}")
    except (TypeError, ValueError):
        return {}


def snapshot(now: Optional[dt.datetime] = None) -> dict[str, Any]:
    """Queue + last tick, from SQLite only. Never raises."""
    out: dict[str, Any] = {"state": "ok"}
    try:
        backlog = outbox.backlog()
    except Exception as exc:  # noqa: BLE001
        return {"state": "error", "reason": f"queue unreadable: {type(exc).__name__}: {exc}"}
    tick = last_tick()
    oldest = _age_s(backlog.get("oldest_created_at"), now)
    out.update({"queued": backlog["queued"], "oldest_queued_s": None if oldest is None else int(oldest),
                "rejected": backlog["rejected"], "last_tick": tick,
                "embed_pending": (tick.get("embed") or {}).get("chunks_pending"),
                "doc_pending": (tick.get("embed") or {}).get("docs_pending")})
    if not config.enabled():
        out.update(state="disabled", reason="WAX_TRANSCRIPTS=0")
        return out
    if not db.configured("app"):
        out.update(state="disabled", reason="WAX_TRANSCRIPTS_DSN is unset (capture only)")
        # Capture-only still queues; a growing queue with no DSN is expected, not an error.
        return out
    reasons = []
    if oldest is not None and oldest > config.lag_fail_s():
        reasons.append(f"oldest queued snapshot is {int(oldest)}s old (> {int(config.lag_fail_s())}s)")
    embed_info = tick.get("embed") or {}
    embed_age = _age_s(embed_info.get("oldest_pending_at"), now)
    if embed_age is not None and embed_age > config.EMBED_FORCE_AFTER_S:
        reasons.append(f"embed backlog is {int(embed_age)}s old (> {int(config.EMBED_FORCE_AFTER_S)}s)")
    if (tick.get("embed_error") or {}).get("reason_code") in ("digest_mismatch", "model_registry_mismatch"):
        reasons.append(f"model: {tick['embed_error'].get('detail')}")
    if reasons:
        out.update(state="error", reason="; ".join(reasons))
    elif oldest is not None and oldest > config.LAG_WARN_S:
        out.update(state="lagging", reason=f"oldest queued snapshot is {int(oldest)}s old")
    elif tick.get("state") == "unreachable":
        # The last waxd tick could not connect: nothing is projected or embedded right now, even
        # when the queue is still young or empty. Status must not read green over that.
        out.update(state="lagging", reason=f"store unreachable at last tick: {tick.get('error')}")
    elif not tick.get("at"):
        out.update(state="lagging", reason="no wax-transcriptions tick recorded yet (is waxd running this code?)")
    elif tick.get("state") == "disabled":
        # This process sees a DSN (env or installed unit) but waxd's last tick did not: the unit was
        # changed after waxd started. Nothing drains or embeds until it restarts.
        out.update(state="lagging", reason=f"waxd's last tick says disabled ({tick.get('reason')}); "
                                           "systemctl --user restart waxd to apply the unit")
    else:
        tick_age = _age_s(tick.get("at"), now)
        stale_after = max(config.LAG_WARN_S, 10 * config.tick_s())
        if tick_age is not None and tick_age > stale_after:
            out.update(state="lagging", reason=f"last wax-transcriptions tick was {int(tick_age)}s ago "
                                               f"(> {int(stale_after)}s): is waxd running?")
    return out


def _probe(name: str, status: str, detail: str, fix: str = "") -> dict[str, Any]:
    return {"probe": name, "status": status, "detail": detail, **({"fix": fix} if fix else {})}


CANONICAL_ANN = ("EXPLAIN (FORMAT JSON) SELECT chunk_id FROM tx.chunk_vector_current "
                 "ORDER BY embedding <#> %s::halfvec LIMIT 200")


def _plan_names(plan: Any) -> set[str]:
    names: set[str] = set()
    stack = [plan]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "Index Name" in node:
                names.add(str(node["Index Name"]))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return names


def _ann_plan(conn: Any) -> Any:
    probe = embed.halfvec_literal([1.0] + [0.0] * (embed.DIMS - 1))
    plan = conn.execute(CANONICAL_ANN, (probe,)).fetchone()[0]
    return json.loads(plan) if isinstance(plan, str) else plan


def ann_uses_hnsw(conn: Any) -> tuple[bool, str]:
    names = _plan_names(_ann_plan(conn))
    return "chunk_vector_current_hnsw" in names, ", ".join(sorted(names)) or "no index"


# tx.search runs with `SET hnsw.ef_search = 200`, and pgvector prices an HNSW scan's startup in
# proportion to ef_search. On 2026-10-28, at 7,309 chunks, that made an exact seq scan + top-N sort
# (cost 7,716) cheaper than the index (7,226 startup + per-row) for LIMIT 200, so the unfiltered
# vector leg is exact until the table grows past the crossover. That is correct and ~11 ms; the
# probe says which path search really takes instead of implying HNSW (design G.6).
SEARCH_EF_SEARCH = 200


def search_vector_path(conn: Any) -> str:
    """'hnsw' or 'exact' for the canonical ANN query under tx.search's own ef_search."""
    with conn.transaction():
        conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(SEARCH_EF_SEARCH),))
        names = _plan_names(_ann_plan(conn))
    return "hnsw" if "chunk_vector_current_hnsw" in names else "exact"


def probes(*, conn: Any = None, embedder: Optional[embed.OllamaEmbedder] = None,
           now: Optional[dt.datetime] = None) -> list[dict[str, Any]]:
    """The seven doctor probes (D.8). Never raises; a probe that cannot run reports why."""
    out: list[dict[str, Any]] = []
    add = out.append
    if not config.enabled():
        return [_probe("transcripts store", WARN, "disabled (WAX_TRANSCRIPTS=0)")]
    if not db.configured("app") and conn is None:
        return [_probe("transcripts store", WARN, "WAX_TRANSCRIPTS_DSN is unset: capture only, nothing is projected",
                       "deploy/provision-transcriptions --apply, then set the waxd unit's DSN")]
    own = conn is None
    try:
        if own:
            conn = db.connect("app", autocommit=True)
    except Exception as exc:  # noqa: BLE001
        placeholder = db.UNIT_PLACEHOLDER in str(exc)
        return [_probe("transcripts store", FAIL, f"{type(exc).__name__}: {exc}",
                       "deploy/provision-transcriptions --apply; deploy/install-systemd-user" if placeholder
                       else "check postgresql@17-main and the waxd unit's WAX_TRANSCRIPTS_* lines")]
    try:
        # 1. reachable + schema current
        try:
            state = migrate.status(conn)
            add(_probe("transcripts store", PASS if state["current"] else FAIL,
                       f"schema at {state['applied'][-1] if state['applied'] else 'none'}"
                       + ("" if state["current"] else f"; pending {state['pending']} changed {state['changed']}"),
                       "" if state["current"] else "wax transcripts migrate"))
        except Exception as exc:  # noqa: BLE001
            add(_probe("transcripts store", FAIL, f"{type(exc).__name__}: {exc}"))
        # 2. projection drained
        backlog = outbox.backlog()
        oldest = _age_s(backlog.get("oldest_created_at"), now)
        drained = oldest is None or oldest <= config.lag_fail_s()
        add(_probe("transcripts projection drained", PASS if drained else FAIL,
                   f"{backlog['queued']} queued, oldest {int(oldest or 0)}s, {backlog['rejected']} rejected",
                   "" if drained else "wax transcripts sync"))
        # 3. parity: identities with wax ids vs canonical ledger notes
        try:
            wax_ids = {r[0] for r in conn.execute("SELECT wax_item_id FROM tx.transcript WHERE wax_item_id IS NOT NULL")}
            ledger_ids = {r["item_id"] for r in ledger.connect().execute("SELECT item_id FROM transcripts")}
            missing = len(ledger_ids - wax_ids)
            add(_probe("transcripts parity", PASS if not missing else WARN,
                       f"{len(wax_ids)} identities with a wax id; {missing} ledger transcript(s) without one",
                       "" if not missing else "wax transcripts sync"))
        except Exception as exc:  # noqa: BLE001
            add(_probe("transcripts parity", WARN, f"{type(exc).__name__}: {exc}"))
        # 4. invariants
        try:
            rows = conn.execute("SELECT check_name, transcript_id, detail FROM tx.check_invariants() LIMIT 5").fetchall()
            add(_probe("transcripts invariants", PASS if not rows else FAIL,
                       "tx.check_invariants() returned 0 rows" if not rows else
                       "; ".join(f"{r[0]}#{r[1]} {r[2]}" for r in rows)))
        except Exception as exc:  # noqa: BLE001
            add(_probe("transcripts invariants", FAIL, f"{type(exc).__name__}: {exc}"))
        # 5. embeddings current
        try:
            pending = embed.pending_counts(conn)
            age = _age_s(pending.get("oldest_pending_at"), now)
            fresh = age is None or age <= config.EMBED_STALE_WARN_S
            add(_probe("transcripts embeddings current", PASS if fresh and not pending["docs_pending"] else WARN,
                       f"{pending['chunks_pending']} chunk(s), {pending['docs_pending']} doc(s) pending"
                       + (f", oldest {int(age)}s" if age is not None else ""),
                       "" if fresh else "wax transcripts embed"))
        except Exception as exc:  # noqa: BLE001
            add(_probe("transcripts embeddings current", WARN, f"{type(exc).__name__}: {exc}"))
        # 6. Ollama serves the pinned digest
        try:
            (embedder or embed.OllamaEmbedder()).check_digest()
            add(_probe("transcripts embedder", PASS, f"{embed.MODEL_NAME} @ {embed.MODEL_DIGEST[:12]}"))
        except embed.EmbedError as exc:
            add(_probe("transcripts embedder", FAIL, f"{exc.reason_code}: {exc}",
                       f"ollama pull {embed.MODEL_NAME} (digest must be {embed.MODEL_DIGEST[:12]})"))
        # 7. the ANN query uses the HNSW index
        try:
            ok, names = ann_uses_hnsw(conn)
            detail = f"canonical ANN plan uses {names}"
            try:
                path = search_vector_path(conn)
                detail += ("; search (ef_search=%d) takes the HNSW index" if path == "hnsw" else
                           "; search (ef_search=%d) plans an exact scan at this size, cheaper than HNSW"
                           ) % SEARCH_EF_SEARCH
            except Exception as exc:  # noqa: BLE001 - informational only
                detail += f"; search path unknown ({type(exc).__name__})"
            add(_probe("transcripts vector index", PASS if ok else FAIL,
                       detail, "" if ok else "ANALYZE as owner; check chunk_vector_current_hnsw"))
        except Exception as exc:  # noqa: BLE001
            add(_probe("transcripts vector index", FAIL, f"{type(exc).__name__}: {exc}"))
    finally:
        if own and conn is not None:
            conn.close()
    return out
