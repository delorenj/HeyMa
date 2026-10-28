"""The versioned `transcriptions` store for Wax (design: docs/TRANSCRIPTIONS-STORE.md).

Public API. Nothing here imports psycopg; every store operation that needs
Postgres imports it lazily and raises `StoreUnavailable` (a RuntimeError) when
it cannot be used, which `bin/wax` turns into `error: ...` and exit 2.

The two hooks never raise and never fail the caller:

    record_transcription(...)   from transcribe_adapter, after the note is published
    record_pass(...)            from passes.run, after the note's pass entry is written

Each enqueues a full snapshot into SQLite (`outbox.py`, the durable record of
the version), then - if a DSN is configured and the sync breaker is closed -
projects the item's pending snapshots into Postgres. Any failure trips the
breaker and is logged; the `wax-transcriptions` thread (`tick()`) drains the
rest, reconciles the vault and embeds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, db, embed, extract, gates, health, migrate, outbox, reconcile, search, store, versioning
from .db import StoreUnavailable

__all__ = ["StoreUnavailable", "record_transcription", "record_pass", "sync", "tick", "yield_gpu", "show", "history",
           "config", "db", "embed", "extract", "gates", "health", "migrate", "outbox", "reconcile", "search",
           "store", "versioning"]

log = logging.getLogger("wax.transcriptions")

show = search.show
history = search.history

DETAIL_CHARS = 300


# ---------------------------------------------------------------- facts --

def _safe(fn: Callable[[], Any]) -> Any:
    """One payload field; anything odd (a MagicMock ledger in tests) degrades to None."""
    try:
        value = fn()
        json.dumps(value, default=str)
        return value
    except Exception:  # noqa: BLE001
        return None


def _row(sql: str, *params: Any) -> Optional[dict[str, Any]]:
    from .. import ledger
    row = ledger.connect().execute(sql, params).fetchone()
    return {k: row[k] for k in row.keys()} if row is not None else None


def _rows(sql: str, *params: Any) -> list[dict[str, Any]]:
    from .. import ledger
    return [{k: r[k] for k in r.keys()} for r in ledger.connect().execute(sql, params).fetchall()]


def ledger_facts(item_id: str) -> dict[str, Any]:
    return {
        "item": _safe(lambda: _row("SELECT * FROM items WHERE item_id=?", item_id)),
        "transcript": _safe(lambda: _row("SELECT * FROM transcripts WHERE item_id=?", item_id)),
        "backups": _safe(lambda: _rows("SELECT * FROM backups WHERE item_id=? ORDER BY verified_at DESC, s3_key",
                                       item_id)) or [],
    }


def plan_facts(item_id: str) -> dict[str, Any]:
    row = _safe(lambda: _row("SELECT plan_id, source FROM processing_plans WHERE item_id=?", item_id)) or {}
    provenance: dict[str, Any] = {}
    try:
        provenance = json.loads(row.get("source") or "{}") if row else {}
    except (TypeError, ValueError, AttributeError):
        provenance = {}
    if not isinstance(provenance, dict):
        provenance = {}
    return {"plan_id": row.get("plan_id") if row else None,
            "pipeline_name": provenance.get("name") if provenance.get("kind") == "pipeline" else None,
            "pipeline_definition_id": provenance.get("definition_id")}


def _asr(asr_path: Any) -> dict[str, Any]:
    try:
        with open(asr_path, encoding="utf-8") as handle:
            return extract.asr_summary(json.load(handle))
    except Exception:  # noqa: BLE001
        return {}


def _read_note(md_path: Path) -> tuple[str, dict[str, int]]:
    st = md_path.stat()
    return md_path.read_bytes().decode("utf-8", errors="replace"), {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _is_audio_item(item_id: Any) -> bool:
    # Documents (`doc-<sha16>@binding`) never get a transcripts row and are out of scope.
    return isinstance(item_id, str) and bool(extract.ITEM_ID_RE.match(item_id))


def _project(item_id: str) -> None:
    """Best-effort synchronous projection behind the breaker; never raises."""
    try:
        if not db.configured("app") or outbox.breaker_open():
            return
        store.project_item(item_id)
    except Exception as exc:  # noqa: BLE001 - the store must never cost a pass
        try:
            outbox.trip_breaker(config.sync_breaker_s())
        except Exception:  # noqa: BLE001
            pass
        log.warning("transcriptions projection deferred for %s (breaker open %ss): %s: %s", item_id,
                    int(config.sync_breaker_s()), type(exc).__name__, str(exc)[:300])


# ---------------------------------------------------------------- hooks --

def record_transcription(*, item_id: str, md_path: Path, meta: dict, verdict: dict, asr_path: Path,
                         attempt: int, processing_seconds: float) -> None:
    """Version a freshly published transcript note. Never raises."""
    try:
        if not config.enabled() or not _is_audio_item(item_id):
            return
        path = Path(md_path)
        text, stat = _read_note(path)
        root = store.vault_root()
        payload = {
            "producer_kind": "transcription", "produced_by": "transcription",
            "transcription": {
                "attempt": int(attempt), "processing_seconds": _safe(lambda: float(processing_seconds)),
                "verdict": _safe(lambda: {k: verdict.get(k) for k in
                                          ("ok", "reason_code", "audio_duration_s", "asr_duration_s",
                                           "duration_ratio")}),
                "meta": _safe(lambda: {k: meta.get(k) for k in
                                       ("model", "word_count", "diarized", "language", "diarization_requested",
                                        "diarization_device_requested", "diarization_device")}),
            },
            "ledger": ledger_facts(item_id), "plan": plan_facts(item_id), "asr": _asr(asr_path),
            "note_stat": stat, "vault_root": root, "relpath": store.relpath_for(path, root),
            "ledger_maps_note": True,
        }
        key = versioning.transcription_key(item_id, int(attempt), hashlib.sha256(text.encode("utf-8")).hexdigest())
        outbox.enqueue(item_id=item_id, kind="version", idempotency_key=key, md_path=str(path), note_text=text,
                       payload=payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("transcriptions capture failed for %s: %s: %s", item_id, type(exc).__name__, exc)
        return
    _project(item_id)


def record_pass(*, item_id: str, slug: str, kind: str, state: str, md_path: Optional[Path], pass_version: int,
                attempt: int, command_id: Optional[str], reason_code: Optional[str], detail: str,
                definition: Optional[dict], pass_result: Optional[dict], changed_fields: list[str],
                duration_s: Optional[float], pre_rename_md: Optional[Path] = None) -> None:
    """Version the note after a pass (kind='version') or record a failed run (kind='pass_run'). Never raises."""
    try:
        if not config.enabled() or not _is_audio_item(item_id):
            return
        if kind not in ("version", "pass_run"):
            raise ValueError(f"unknown capture kind {kind!r}")
        text = None
        stat = None
        path = Path(md_path) if md_path else None
        if kind == "version":
            if path is None:
                raise ValueError("a version capture needs the note path")
            text, stat = _read_note(path)
        evidence = None
        if isinstance(pass_result, dict):
            evidence = extract.clean_evidence(pass_result.get("evidence"))

        def definition_id() -> Optional[str]:
            from .. import operations
            return operations.definition_id(definition) if definition else None

        facts = {
            "slug": slug, "pass_version": _safe(lambda: int(pass_version)), "attempt": _safe(lambda: int(attempt)),
            "command_id": command_id, "state": state, "reason_code": reason_code,
            "detail": (detail or "")[:DETAIL_CHARS], "definition_id": _safe(definition_id),
            "changed_fields": _safe(lambda: [str(f) for f in changed_fields or []]) or [],
            "duration_s": _safe(lambda: float(duration_s) if duration_s is not None else None),
            "pre_rename_path": str(pre_rename_md) if pre_rename_md else None,
            "evidence": _safe(lambda: evidence),
        }
        root = store.vault_root()
        payload = {
            "producer_kind": "pass", "produced_by": slug, "pass": facts,
            "ledger": ledger_facts(item_id), "plan": plan_facts(item_id),
            "vault_root": root, "relpath": store.relpath_for(path, root) if path else None,
            "note_stat": stat, "ledger_maps_note": True,
        }
        key = versioning.pass_snapshot_key(item_id=item_id, slug=slug, state=state,
                                           pass_version=int(pass_version or 1), attempt=int(attempt or 1),
                                           command_id=command_id, reason_code=reason_code)
        outbox.enqueue(item_id=item_id, kind=kind, idempotency_key=key, md_path=str(path) if path else None,
                       note_text=text, payload=payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("transcriptions capture failed for %s/%s: %s: %s", item_id, slug, type(exc).__name__, exc)
        return
    _project(item_id)


# ---------------------------------------------------------------- operations --

def sync(*, dry_run: bool = False, check: bool = False, embed_all: bool = False, limit: int = 0,
         **kwargs: Any) -> dict[str, Any]:
    """`wax transcripts sync`: project the queue, reconcile vault + ledger, optionally embed.

    Raises StoreUnavailable when the store cannot be reached. `drift`/`errors`
    in the result are what the CLI turns into exit 2.
    """
    if "embed" in kwargs:          # the CLI flag is spelled --embed
        embed_all = bool(kwargs.pop("embed"))
    if kwargs:
        raise TypeError(f"unexpected arguments: {sorted(kwargs)}")
    if check:
        return reconcile.parity()
    out: dict[str, Any] = {"dry_run": dry_run, "errors": []}
    conn = db.connect("app", statement_timeout_ms=60000, autocommit=True)
    try:
        if not dry_run:
            projected = store.project_pending(limit=100000, conn=conn)
            out["projection"] = projected
            out["errors"].extend(projected.get("errors") or [])
        swept = reconcile.sweep(dry_run=dry_run, limit=limit, conn=conn)
        out["reconcile"] = swept
        out["errors"].extend(swept.get("errors") or [])
        if embed_all and not dry_run:
            out["embed"] = embed.sweep(until_empty=True, conn=conn)
    finally:
        conn.close()
    return out


def tick(gpu_busy: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """One `wax-transcriptions` thread iteration (design D.5). Raises only on a store outage."""
    from .. import ledger, sentinel
    if not config.enabled():
        stats: dict[str, Any] = {"state": "disabled", "reason": "WAX_TRANSCRIPTS=0", "at": sentinel.utcnow()}
        ledger.meta_set(health.LAST_TICK, json.dumps(stats))
        return stats
    if not db.configured("app"):
        stats = {"state": "disabled", "reason": "no DSN", "at": sentinel.utcnow()}
        ledger.meta_set(health.LAST_TICK, json.dumps(stats))
        return stats
    started = time.time()
    stats = {"state": "ok", "at": sentinel.utcnow()}
    try:
        conn = db.connect("app", statement_timeout_ms=60000, autocommit=True)
    except Exception as exc:
        # Say so where `wax status` looks, then let waxd's condition log it once.
        try:
            ledger.meta_set(health.LAST_TICK, json.dumps({
                "state": "unreachable", "at": stats["at"],
                "error": f"{type(exc).__name__}: {str(exc)[:300]}"}))
        except Exception:  # noqa: BLE001
            pass
        raise
    try:
        stats["projection"] = store.project_pending(limit=200, conn=conn)
        outbox.reset_breaker()      # the store answered: the sync path may try again
        last = health._age_s((_json(ledger.meta_get(reconcile.LAST_RECONCILE)) or {}).get("at"))
        if last is None or last >= config.reconcile_interval_s():
            swept = reconcile.sweep(conn=conn)
            stats["reconcile"] = {k: v for k, v in swept.items() if k != "errors"}
            stats["reconcile"]["errors"] = len(swept.get("errors") or [])
        busy = False
        try:
            busy = bool(gpu_busy()) if gpu_busy else False
        except Exception:  # noqa: BLE001
            busy = False
        if busy:
            stats["embed"] = {"deferred": "gpu_busy", **embed.pending_counts(conn)}
        else:
            try:
                stats["embed"] = embed.sweep(max_batches=8, conn=conn, stop=gpu_busy)
            except embed.EmbedError as exc:
                stats["embed_error"] = {"reason_code": exc.reason_code, "detail": str(exc)[:300]}
                stats["embed"] = embed.pending_counts(conn)
        if busy or (stats.get("embed") or {}).get("stopped"):
            stats["gpu_yielded"] = yield_gpu()
    finally:
        conn.close()
        stats["seconds"] = round(time.time() - started, 2)
        try:
            ledger.meta_set(health.LAST_TICK, json.dumps(stats, default=str))
        except Exception:  # noqa: BLE001
            pass
    return stats


def yield_gpu() -> bool:
    """Unload the embedding model from VRAM because Wax is transcribing or diarizing. Never raises.

    `inbox.active_item` gates when the store may START embedding, but Ollama keeps the model
    resident for `keep_alive` afterwards. On 2026-10-28 that resident 5.3 GB left 3,864 MB free,
    under bin/transcribe's 5,000 MB floor (it then waits 30 s and falls back to CPU int8). waxd
    calls this as soon as an item becomes active; the unload is a no-op when nothing is loaded.
    """
    if not config.enabled():
        return False
    try:
        embed.OllamaEmbedder(timeout=5).unload()
        return True
    except Exception as exc:  # noqa: BLE001 - the GPU courtesy must never cost anything else
        log.debug("transcriptions: embedder unload failed: %s: %s", type(exc).__name__, exc)
        return False


def _json(raw: Optional[str]) -> Any:
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
