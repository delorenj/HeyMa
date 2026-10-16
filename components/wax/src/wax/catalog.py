"""Safe discovery: what the enrichment system IS, without what it must not leak.

Two views, both read-only:

- :func:`pass_catalog` — every registered pass with its purpose, capability
  declaration, availability (and WHY it is unavailable), run conditions,
  dependency edges, emitted event types and a bounded outcome tally.
- :func:`pipeline_catalog` — every pipeline: authored vs compiled identity,
  drift, trigger, folder bindings, the node/edge graph with per-node
  applicability, failure policy and truthful outcome counts.

The Never list is enforced by construction: no ``env`` (secrets live there),
no command argv, no result bodies, no raw stderr, no ledger detail prose —
only reason codes and counts. ``wax ep status`` remains the operator surface
that shows detail; this module is what Holocene and n8n are allowed to read,
so a key added here is a key a hosted surface may display.

Outcome tallies distinguish the states the boundary list names: availability,
enabled, automatic, running, skipped, failed and exhausted.
"""

import json
import logging
from typing import Any, Optional

from . import documents, ledger, passes, pipelines, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

# Everything a catalog pass row may carry. Anything else a registry entry grows
# stays invisible here until deliberately added — the allowlist is the policy.
_PASS_FIELDS = (
    "slug", "description", "version", "enabled", "auto", "kind",
    "capabilities", "requires", "after", "emits", "timeout_s",
)


def _availability(ep: dict[str, Any], quarantine: Optional[str]) -> dict[str, str]:
    """Why a pass can or cannot run, in one machine-readable pair."""
    if ep.get("error"):
        return {"state": "unparseable", "reason": str(ep["error"])}
    if quarantine:
        return {"state": "quarantined", "reason": quarantine}
    if not ep.get("enabled"):
        if _is_placeholder(ep):
            return {"state": "placeholder", "reason":
                    "registered but not implemented; enabled: false"}
        return {"state": "disabled", "reason": "enabled: false in its registry yaml"}
    return {"state": "available", "reason": ""}


def _is_placeholder(ep: dict[str, Any]) -> bool:
    return str(ep.get("description") or "").startswith("TODO")


def _pass_outcomes(slug: str, version: int, tally: dict) -> dict[str, int]:
    """Bounded per-state counts at the CURRENT version (stale ones separate)."""
    out = {"completed": 0, "failed": 0, "skipped": 0, "running": 0,
           "exhausted": 0, "stale": 0}
    for (row_slug, row_state, row_version), n in tally.items():
        if row_slug != slug:
            continue
        if row_version != version:
            out["stale"] += n
        elif row_state == "failed":
            out["failed"] += n
        else:
            out[row_state] = out.get(row_state, 0) + n
    return out


def _tally() -> dict[tuple[str, str, int], int]:
    """(slug, state, version) -> row count, plus exhausted flags folded in."""
    out: dict[tuple[str, str, int], int] = {}
    try:
        for row in ledger.connect().execute(
                "SELECT ep_slug, state, version, COUNT(*) AS n FROM passes "
                "GROUP BY ep_slug, state, version"):
            out[(row["ep_slug"], row["state"], int(row["version"] or 1))] = row["n"]
        for row in ledger.connect().execute(
                "SELECT ep_slug, version, COUNT(*) AS n FROM passes "
                "WHERE exhausted_at IS NOT NULL GROUP BY ep_slug, version"):
            out[(row["ep_slug"], "exhausted", int(row["version"] or 1))] = row["n"]
    except Exception as exc:  # noqa: BLE001 - discovery must not die on a ledger fault
        log.warning("pass outcome tally unavailable: %s: %s", type(exc).__name__, exc)
    return out


def pass_catalog(reg: Optional[dict[str, dict[str, Any]]] = None) -> dict[str, Any]:
    """Every registered pass: purpose, capability, availability, edges, outcomes."""
    reg = reg if reg is not None else passes.registry()
    bad = passes.quarantined(reg)
    tally = _tally()
    rows = []
    for slug, ep in sorted(reg.items()):
        version = int(ep.get("version") or 1)
        row: dict[str, Any] = {field: ep.get(field) for field in _PASS_FIELDS}
        row["slug"] = slug
        row["version"] = version
        row["enabled"] = bool(ep.get("enabled"))
        row["auto"] = bool(ep.get("auto"))
        row["capabilities"] = list(ep.get("capabilities") or [])
        row["requires"] = list(ep.get("requires") or [])
        row["after"] = list(ep.get("after") or [])
        row["emits"] = list(ep.get("emits") or [])
        conditions: dict[str, Any] = {}
        if ep.get("when"):
            conditions["when"] = ep["when"]
        if ep.get("skip_when"):
            conditions["skip_when"] = ep["skip_when"]
        row["conditions"] = conditions or None
        quarantine = bad.get(slug)
        row["availability"] = _availability(ep, quarantine)
        row["quarantined"] = quarantine
        row["outcomes"] = _pass_outcomes(slug, version, tally)
        rows.append(row)
    return {
        "generated_at": sentinel.utcnow(),
        "passes": rows,
        "counts": {
            "registered": len(rows),
            "enabled": sum(1 for r in rows if r["enabled"]),
            "automatic": sum(1 for r in rows if r["enabled"] and r["auto"]),
            "quarantined": sum(1 for r in rows if r["quarantined"]),
            "failing": sum(1 for r in rows if r["outcomes"]["failed"]),
        },
    }


# ---------------------------------------------------------------- pipelines --


def pipeline_catalog() -> dict[str, Any]:
    """Every pipeline with authored/compiled identity, drift, graph and outcomes.

    The graph shown is the FROZEN compiled graph (stored nodes and edges), not
    a re-derivation from the live registry; registry drift is reported beside
    it so an operator can see what a recompile would change.
    """
    reg = passes.registry()
    bad = passes.quarantined(reg)
    sources = pipelines.authored()
    current = pipelines.compiled()
    conn = ledger.connect()
    rows = []
    for name in sorted(set(sources) | set(current)):
        entry: dict[str, Any] = {"name": name}
        source = sources.get(name)
        compiled_def = current.get(name)
        if compiled_def is not None:
            entry["source"] = compiled_def.get("source") or "yaml"
            entry["source_ref"] = compiled_def.get("source_ref")
        elif source is not None:
            entry["source"] = "yaml"
        if source is not None:
            entry["description"] = source.get("description") or ""
            entry["authored_path"] = source.get("_path")
        if source and source.get("error"):
            entry["authored_error"] = source["error"]
        entry["drift"] = pipelines.drift(name)
        definition = (compiled_def or {}).get("definition") or {}
        if definition:
            slugs = pipelines._node_slugs(definition)
            entry.update({
                "version": compiled_def["version"],
                "definition_id": compiled_def["definition_id"],
                "compiled_at": compiled_def["compiled_at"],
                "trigger": definition.get("trigger"),
                "enabled": bool(definition.get("enabled")),
                "failure_policy": definition.get("failure_policy", "independent"),
                # With exactly one enabled transcription pipeline compiled, the
                # audio plan authority is that frozen definition; otherwise the
                # worker's registry plan stands and this is only a view.
                "executed_by": ("worker (compiled plan authority)" if definition.get("trigger") == "transcription"
                                and definition.get("enabled") else
                                "worker (registry plan)" if definition.get("trigger") == "transcription"
                                else "wax pipeline run/scan"),
            })
            document_scenario = definition.get("trigger") in ("documents", "manual")
            drift_map = {d["slug"]: d for d in
                         pipelines.registry_drift(definition, reg)["changed"]}
            nodes = []
            for node in definition.get("passes") or []:
                if not isinstance(node, dict):
                    continue
                slug = node.get("slug")
                frozen = node.get("registry") or {}
                live = reg.get(slug) or {}
                graph_node: dict[str, Any] = {
                    "slug": slug,
                    # Authoring identity (the n8n node name) preserved from import.
                    **({"name": node["name"]} if node.get("name") else {}),
                    "availability": _availability(
                        {"enabled": frozen.get("enabled"), "description": frozen.get("description"),
                         "error": None}, None),
                    "live_availability": _availability(live, bad.get(slug)),
                    "capabilities": list(frozen.get("capabilities") or []),
                    "version": int(frozen.get("version") or 1),
                }
                if graph_node["availability"]["state"] == "available":
                    graph_node["availability"]["reason"] = "compiled definition"
                conditions = {}
                if frozen.get("when") is not None:
                    conditions["when"] = frozen["when"]
                if frozen.get("skip_when") is not None:
                    conditions["skip_when"] = frozen["skip_when"]
                if conditions:
                    graph_node["conditions"] = conditions
                if slug in drift_map:
                    graph_node["registry_drift"] = drift_map[slug]
                if document_scenario:
                    graph_node["applicable"] = "document" in graph_node["capabilities"]
                nodes.append(graph_node)
            entry["nodes"] = nodes
            # The FROZEN edge list as compiled — never re-derived from the registry.
            entry["edges"] = [dict(e) for e in pipelines._node_edges(definition)]
            if definition.get("after_external"):
                entry["after_external"] = definition["after_external"]
            entry["registry_drift"] = pipelines.registry_drift(definition, reg)
            entry["bindings"] = [
                {"binding_id": b["binding_id"], "folder": b["folder"],
                 "enabled": b["enabled"], "seeded_at": b["seeded_at"],
                 "last_scan_at": b["last_scan_at"],
                 "folder_exists": b["folder_exists"]}
                for b in documents.bindings(pipeline=name)
            ]
            # Truthful outcomes: durable receipts under this pipeline, plus
            # bounded per-node state counts for its passes.
            try:
                processed = conn.execute(
                    "SELECT COUNT(*) AS n FROM document_receipts "
                    "WHERE pipeline=? AND state='completed'", (name,)).fetchone()
                entry["documents_processed"] = int(processed["n"] if processed else 0)
            except Exception:  # noqa: BLE001
                entry["documents_processed"] = 0
            entry["recent_runs"] = [
                {"doc_id": r["doc_id"], "path": r.get("path"),
                 "state": r["state"], "attempts": int(r["attempts"] or 0),
                 "last_attempt_at": r["last_attempt_at"], "completed_at": r["completed_at"],
                 "exhausted_at": r["exhausted_at"],
                 # reason_code ONLY. last_error is exception/stderr prose — it
                 # can embed secrets, and a key a hosted surface may display is
                 # a key one eventually will. Operator detail stays in the ledger.
                 **({"reason_code": r["reason_code"]}
                    if r.get("reason_code") else {})}
                for r in documents.recent_receipts(pipeline=name, limit=10)
            ]
            tally = _tally()
            entry["outcomes"] = {
                slug: _pass_outcomes(slug, int((reg.get(slug) or {}).get("version") or 1), tally)
                for slug in slugs
            }
        rows.append(entry)
    return {
        "generated_at": sentinel.utcnow(),
        "pipelines": rows,
        "document_status": documents.status(),
        "worker": documents.read_worker_heartbeat(),
    }


def snapshot() -> dict[str, Any]:
    """The one CLI read Holocene consumes: passes + pipelines, safe fields only."""
    return {
        "generated_at": sentinel.utcnow(),
        "passes": pass_catalog(),
        "pipelines": pipeline_catalog(),
    }
