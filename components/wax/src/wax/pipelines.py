"""Versioned pipelines: authored graphs compiled into the execution authority.

Approved decisions, restated as code:

- **n8n (or a hand edit) is only the authoring source.** A graph lives in
  ``config/pipelines.d/<name>.yaml`` and does nothing by itself.
- **The Wax CLI compiles it**, validating it against the live pass registry and
  storing a canonical, content-addressed definition (``definition_id``) in the
  ledger. Nothing executes from an authored file.
- **Recompilation is explicit** (``wax pipeline compile``). An edited YAML does
  not silently change a running pipeline; ``drift`` is always computable
  because both identities — authored and compiled — are content hashes.
- **Wax owns graph execution and retries.** Document scenarios run through
  :func:`run_document`, which reuses the pass runner (dependency gating,
  attempts, quarantine, no-clobber) rather than growing a second one.
- **Transcription stays where it was.** A ``trigger: transcription`` pipeline
  is a compiled *view* of the registry plan the worker already executes; it
  never becomes a second scheduler, and its snapshot says so.

A definition cannot compile unless every named pass is registered, the induced
graph (each pass's ``requires``/``after`` restricted to the pipeline's nodes)
is acyclic, the trigger is known and its folder rules hold. Compile failures
are loud and total for the named pipeline — never a silent partial graph.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml

from . import component, documents, ledger, operations, passes, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

PIPELINES_DIR = Path(os.environ.get("WAX_PIPELINES_DIR", component.CONFIG / "pipelines.d"))
TRIGGERS = {"transcription", "documents", "manual"}
FAILURE_POLICIES = {"independent", "halt"}
NAME_RE = __import__("re").compile(r"^[a-z][a-z0-9-]*$")


class PipelineError(RuntimeError):
    pass


# ------------------------------------------------------------- authoring ----

def authored() -> dict[str, dict[str, Any]]:
    """Load every authored pipeline definition from pipelines.d/*.yaml."""
    out: dict[str, dict[str, Any]] = {}
    if not PIPELINES_DIR.is_dir():
        return out
    for f in sorted(PIPELINES_DIR.glob("*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError as e:
            out[f.stem] = {"name": f.stem, "error": f"unparseable: {e}"}
            continue
        if not isinstance(doc, dict):
            out[f.stem] = {"name": f.stem, "error": "definition must be a mapping"}
            continue
        name = str(doc.get("name") or f.stem)
        doc.setdefault("name", name)
        doc.setdefault("enabled", False)
        doc["_path"] = str(f)
        out[name] = doc
    return out


def compiled() -> dict[str, dict[str, Any]]:
    """The compiled (authoritative) definitions, keyed by name."""
    rows = ledger.connect().execute("SELECT * FROM pipelines ORDER BY name").fetchall()
    out = {}
    for row in rows:
        try:
            definition = json.loads(row["definition"])
        except (TypeError, ValueError):
            definition = {}
        out[row["name"]] = {
            "name": row["name"], "version": int(row["version"] or 1),
            "definition_id": row["definition_id"], "definition": definition,
            "compiled_at": row["compiled_at"],
        }
    return out


# ---------------------------------------------------------------- compile ----

def _node_slugs(definition: dict[str, Any]) -> list[str]:
    """Pass slugs a compiled definition names, in order, deduplicated."""
    out: list[str] = []
    for node in definition.get("passes") or []:
        slug = node.get("slug") if isinstance(node, dict) else node
        if isinstance(slug, str) and slug not in out:
            out.append(slug)
    return out


def _canonical(definition: dict[str, Any]) -> str:
    safe = {k: v for k, v in definition.items() if not k.startswith("_")}
    return json.dumps(safe, sort_keys=True, default=str)


def _definition_id(definition: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(definition).encode()).hexdigest()


def _expand_folder(folder: str) -> str:
    return str(Path(os.path.expandvars(os.path.expanduser(str(folder)))).resolve())


def _validate(name: str, doc: dict[str, Any], reg: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not NAME_RE.match(name):
        raise PipelineError(f"invalid pipeline name {name!r}: expected lowercase slug")
    trigger = doc.get("trigger")
    if trigger not in TRIGGERS:
        raise PipelineError(f"{name}: trigger must be one of {sorted(TRIGGERS)}, got {trigger!r}")
    policy = doc.get("failure_policy", "independent")
    if policy not in FAILURE_POLICIES:
        raise PipelineError(f"{name}: failure_policy must be one of {sorted(FAILURE_POLICIES)}")
    nodes_in = doc.get("passes")
    if not isinstance(nodes_in, list) or not nodes_in:
        raise PipelineError(f"{name}: passes must be a non-empty list")
    nodes: list[dict[str, Any]] = []
    slugs: list[str] = []
    for node in nodes_in:
        if isinstance(node, str):
            node = {"slug": node}
        if not isinstance(node, dict) or not isinstance(node.get("slug"), str):
            raise PipelineError(
                f"{name}: each pass entry must be a slug or a {{slug, when}} object")
        slug = node["slug"]
        if node.get("when") is not None:
            err = passes._when_error(node["when"])
            if err:
                raise PipelineError(f"{name}: pass {slug}: {err}")
        if slug not in slugs:
            slugs.append(slug)
            nodes.append({"slug": slug,
                          **({"when": node["when"]} if node.get("when") is not None else {})})
    unknown = [s for s in slugs if s not in reg]
    if unknown:
        raise PipelineError(f"{name}: unknown pass slug(s) {unknown}; see `wax ep list`")
    folders = [_expand_folder(f) for f in (doc.get("folders") or [])]
    if not all(isinstance(f, str) and Path(f).is_absolute() for f in folders):
        raise PipelineError(f"{name}: folders must be absolute paths")
    if trigger == "documents" and not folders:
        raise PipelineError(f"{name}: trigger 'documents' requires at least one folder")
    if trigger != "documents" and folders:
        raise PipelineError(f"{name}: folders are only valid with trigger 'documents'")
    # The induced graph: each node's registry requires/after edges, restricted
    # to the nodes this pipeline actually contains. An edge leaving the set is
    # not an error (the dependency simply is not part of this pipeline's
    # execution), but a cycle inside the set is.
    graph = {slug: [d for d in list(reg[slug].get("requires") or []) + list(reg[slug].get("after") or [])
                   if d in slugs] for slug in slugs}
    state: dict[str, int] = {}

    def visit(slug: str, stack: list[str]) -> None:
        state[slug] = 1
        for dep in graph[slug]:
            if state.get(dep) == 1:
                cycle = " -> ".join(stack + [slug, dep])
                raise PipelineError(f"{name}: cyclic pass dependency in pipeline: {cycle}")
            if state.get(dep) is None:
                visit(dep, stack + [slug])
        state[slug] = 2

    for slug in slugs:
        if state.get(slug) is None:
            visit(slug, [])
    return {
        "name": name,
        "version": int(doc.get("version") or 1),
        "description": doc.get("description") or "",
        "trigger": trigger,
        "enabled": bool(doc.get("enabled")),
        "failure_policy": policy,
        "folders": folders,
        "passes": nodes,
    }


def _authored_slugs(doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for node in doc.get("passes") or []:
        slug = node.get("slug") if isinstance(node, dict) else node
        if isinstance(slug, str):
            out.append(slug)
    return out


def compile(names: Optional[list[str]] = None) -> dict[str, Any]:
    """Validate and store authored definitions. Explicit, idempotent, loud.

    Compiling a ``documents`` pipeline also creates (never seeds) one folder
    binding per authored folder, and disables bindings whose folders were
    removed from the definition — folder state follows the compiled graph.
    """
    reg = passes.registry()
    sources = authored()
    wanted = names or sorted(sources)
    out: dict[str, Any] = {"compiled": [], "unchanged": [], "failed": {}}
    for name in wanted:
        doc = sources.get(name)
        if doc is None:
            out["failed"][name] = f"no authored definition at {PIPELINES_DIR / (name + '.yaml')}"
            continue
        if doc.get("error"):
            out["failed"][name] = doc["error"]
            continue
        try:
            definition = _validate(name, doc, reg)
        except PipelineError as e:
            out["failed"][name] = str(e)
            continue
        definition_id = _definition_id(definition)
        encoded = json.dumps(definition, sort_keys=True, default=str)
        existing = ledger.connect().execute(
            "SELECT definition_id FROM pipelines WHERE name=?", (name,)).fetchone()
        if existing and existing["definition_id"] == definition_id:
            out["unchanged"].append({"name": name, "definition_id": definition_id})
        else:
            ledger.connect().execute(
                "INSERT INTO pipelines(name,version,definition_id,definition,compiled_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET version=excluded.version, "
                "definition_id=excluded.definition_id, definition=excluded.definition, "
                "compiled_at=excluded.compiled_at",
                (name, definition["version"], definition_id, encoded, sentinel.utcnow()),
            )
            out["compiled"].append({"name": name, "version": definition["version"],
                                    "definition_id": definition_id})
        if definition["trigger"] == "documents":
            _sync_bindings(name, definition["folders"])
    if not wanted:
        out["note"] = f"no authored pipelines under {PIPELINES_DIR}"
    return out


def _sync_bindings(pipeline: str, folders: list[str]) -> None:
    """Bindings exist exactly for the compiled folder set; nothing is seeded."""
    keep: set[str] = set()
    for folder in folders:
        binding = documents.ensure_binding(pipeline, folder)
        keep.add(binding["binding_id"])
    for binding in documents.bindings(pipeline=pipeline):
        if binding["binding_id"] not in keep:
            ledger.connect().execute(
                "UPDATE document_bindings SET enabled=0, updated_at=? WHERE binding_id=?",
                (sentinel.utcnow(), binding["binding_id"]),
            )
            log.info("binding %s disabled: folder no longer in pipeline %s",
                     binding["binding_id"], pipeline)


def drift(name: str) -> dict[str, Any]:
    """Authored vs compiled identity for one pipeline. Content hashes both ways."""
    source = authored().get(name)
    current = compiled().get(name)
    authored_id = None
    if source and not source.get("error"):
        try:
            authored_id = _definition_id(_validate(name, source, passes.registry()))
        except PipelineError as e:
            return {"name": name, "state": "invalid", "detail": str(e)}
    if current is None and authored_id is None:
        return {"name": name, "state": "unknown"}
    if current is None:
        return {"name": name, "state": "uncompiled", "authored_id": authored_id}
    if authored_id is None:
        return {"name": name, "state": "authored_missing", "compiled_id": current["definition_id"],
                "detail": "compiled definition has no authored source file"}
    if authored_id != current["definition_id"]:
        return {"name": name, "state": "drifted", "authored_id": authored_id,
                "compiled_id": current["definition_id"],
                "detail": "authored graph changed; `wax pipeline compile` to adopt it"}
    return {"name": name, "state": "in_sync", "definition_id": authored_id}


# ------------------------------------------------------------- execution ----

def _pipeline(name: str) -> dict[str, Any]:
    """The compiled definition — the only thing that may execute."""
    current = compiled().get(name)
    if current is None:
        raise PipelineError(f"pipeline {name!r} is not compiled; run `wax pipeline compile {name}`")
    return current


def _record_skip(doc_id: str, slug: str, reason: str, detail: str = "",
                 ep: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    version = int((ep or {}).get("version") or 1)
    passes._record(doc_id, slug, "skipped", version=version,
                   attempt=passes._next_attempt(doc_id, slug),
                   detail=detail, reason_code=reason, definition=ep)
    md = passes.md_for(doc_id)
    if md is not None:
        try:
            passes._write_pass_note(md, doc_id, slug, {
                "state": "skipped", "at": sentinel.utcnow(), "version": version,
                "reason_code": reason,
                **({"detail": detail} if detail else {}),
            })
        except OSError:
            pass
    return {"item_id": doc_id, "ep_slug": slug, "state": "skipped",
            "reason_code": reason, **({"detail": detail} if detail else {})}


def run_document(name: str, path: Path) -> dict[str, Any]:
    """Run one compiled pipeline over one Markdown document.

    The pipeline module is the document scenario's ONLY executor: it resolves
    the compiled definition, registers the document (content-addressed, no
    transcripts row), enforces the one-effective-run receipt, and drives the
    ordinary pass runner — so dependency gating, attempts, quarantine and
    no-clobber protections are the same machinery audio uses.
    """
    current = _pipeline(name)
    definition = current["definition"]
    if not definition.get("enabled"):
        return {"pipeline": name, "skipped": "pipeline is not enabled (authored enabled: false)"}
    if definition.get("trigger") not in ("documents", "manual"):
        return {"pipeline": name, "skipped": f"trigger {definition.get('trigger')!r} is not a document scenario"}

    doc = documents.register(path, origin=f"pipeline:{name}")
    if doc is None:
        raise PipelineError(f"cannot read document {path}")
    doc_id = doc["doc_id"]
    out: dict[str, Any] = {"pipeline": name, "doc_id": doc_id, "path": str(path),
                           "definition_id": current["definition_id"],
                           "version": current["version"],
                           "drift": drift(name)}
    if doc.get("processed_definition_id") == current["definition_id"]:
        out["skipped"] = "already processed at this definition (duplicate arrival)"
        return out

    reg = passes.registry()
    bad = passes.quarantined(reg)
    slugs = _node_slugs(definition)
    # passes.ordered() yields only enabled, non-quarantined passes; disabled and
    # quarantined nodes still belong to the graph and must record a visible
    # availability skip, so they are appended after the executable order.
    runnable = [s for s in passes.ordered(reg) if s in slugs]
    ordered_slugs = runnable + [s for s in slugs if s not in set(runnable)]
    nodes = {n["slug"]: n for n in definition.get("passes") or [] if isinstance(n, dict)}
    previous = {row["ep_slug"]: dict(row) for row in ledger.connect().execute(
        "SELECT * FROM passes WHERE item_id=?", (doc_id,)).fetchall()}
    results: list[dict[str, Any]] = []
    halted = False
    for slug in ordered_slugs:
        ep = dict(reg.get(slug) or {})
        version = int(ep.get("version") or 1)
        # Availability is surfaced, never silently dropped: a disabled or
        # quarantined node records a visible skip instead of running.
        if slug in bad:
            results.append(_record_skip(doc_id, slug, "quarantined", bad[slug], ep=ep))
            continue
        if not ep.get("enabled"):
            results.append(_record_skip(doc_id, slug, "disabled",
                                        "pass is disabled in the registry", ep=ep))
            continue
        # A per-node `when` overrides the registry condition for this graph
        # only; the recorded definition_id captures the merged definition.
        node_when = (nodes.get(slug) or {}).get("when")
        if node_when is not None:
            ep["when"] = node_when
        definition_id = operations.definition_id(ep)
        prior = previous.get(slug)
        if (prior and prior["state"] in ("completed", "skipped")
                and int(prior["version"] or 1) == version
                and prior.get("definition_id") in (None, definition_id)):
            # One effective run per revision/definition: a re-drop of content
            # that already completed this definition is a no-op, while a
            # FAILED pass stays retryable through `wax documents run|backfill`.
            results.append({"item_id": doc_id, "ep_slug": slug, "version": version,
                            "state": prior["state"], "skipped": "already completed at this version"})
            continue
        try:
            results.append(passes.run(doc_id, slug, definition=ep or None))
        except passes.PassError as e:
            results.append({"item_id": doc_id, "ep_slug": slug, "state": "failed",
                            "error": str(e), "reason_code": "run_error"})
        state = results[-1].get("state")
        if definition.get("failure_policy") == "halt" and state == "failed":
            halted = True
            break
    # The processed receipt lands only on a fully-terminal run; a document
    # with a failed pass stays unprocessed, so backfill and a manual re-run
    # keep retrying exactly the failed nodes (completed ones replay as no-ops).
    terminal = all(r.get("state") in ("completed", "skipped") for r in results) and results
    if terminal and not halted:
        documents.mark_processed(doc_id, name, current["definition_id"])
    out["results"] = results
    out["completed"] = sum(1 for r in results if r.get("state") == "completed")
    out["failed"] = sum(1 for r in results if r.get("state") == "failed")
    out["skipped_count"] = sum(1 for r in results if r.get("state") == "skipped")
    if halted:
        out["halted"] = True
    return out
