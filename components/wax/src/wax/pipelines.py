"""Versioned pipelines: authored graphs compiled into the execution authority.

Approved decisions, restated as code:

- **n8n (or a hand edit) is only the authoring source.** A graph lives in
  ``config/pipelines.d/<name>.yaml`` or in an exported n8n workflow JSON, and
  neither does anything by itself.
- **The Wax CLI compiles it** — ``wax pipeline compile`` for YAML,
  ``wax pipeline import`` for an n8n export — validating it against the pass
  registry and storing a canonical, content-addressed definition
  (``definition_id``) in the ledger. Nothing executes from an authored file.
- **The compiled definition is FROZEN and is the only thing that executes.**
  Each node carries the full runnable pass definition (command, env, edges,
  conditions, capabilities, version) as it was at compile time; the edge list
  is stored, not re-derived from the live registry. A registry edit therefore
  changes nothing until an explicit recompile — and ``registry_drift`` in the
  catalog says exactly which frozen nodes no longer match the registry.
- **Recompilation is explicit.** An edited source does not silently change a
  running pipeline; ``drift`` is always computable because both identities —
  authored and compiled — are content hashes.
- **Wax owns graph execution and retries.** Document scenarios run through
  :func:`run_document`, which reuses the pass runner (dependency gating,
  attempts, quarantine, no-clobber) rather than growing a second one. n8n
  never executes the graph; an exported schedule/trigger node is rejected at
  import precisely so native execution cannot become a second scheduler.
- **Transcription is wired, not duplicated.** With exactly one compiled,
  enabled ``trigger: transcription`` pipeline, NEW audio plans are minted from
  its frozen definition (``transcription_authority``); with none, the legacy
  live-registry plan stands; with more than one the request is refused and the
  legacy plan is used — an ambiguous graph is never fabricated as active.
  Items with stored plans keep them; historical completions are immutable.

A definition cannot compile unless every named pass is registered, every
``requires`` edge names a node IN the graph (a missing required dependency is
a loud error, never a silent drop), the edge set is acyclic, the trigger is
known and its folder rules hold. Compile failures are loud and total for the
named pipeline — never a silent partial graph.
"""

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from . import component, documents, ledger, operations, passes, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

PIPELINES_DIR = Path(os.environ.get("WAX_PIPELINES_DIR", component.CONFIG / "pipelines.d"))
TRIGGERS = {"transcription", "documents", "manual"}
FAILURE_POLICIES = {"independent", "halt"}
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# n8n node types this package owns; anything else in an export (except sticky
# notes, which are annotations) is unsupported and rejected.
N8N_PACKAGE = "n8n-nodes-heyma"
N8N_WRAPPER_TYPE = f"{N8N_PACKAGE}.waxPipeline"
N8N_EP_TYPE = f"{N8N_PACKAGE}.waxEp"
N8N_PASS_TYPE_PREFIX = f"{N8N_PACKAGE}.wax-pass-"
N8N_ANNOTATION_TYPES = {"n8n-nodes-base.stickyNote"}
# Edge policies: a `main` connection orders (continue); a `requirement`
# connection gates (the target `requires` the source).
N8N_EDGE_KINDS = {"main": "after", "requirement": "requires"}


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
            "source": row["source"] if "source" in row.keys() else "yaml",
            "source_ref": row["source_ref"] if "source_ref" in row.keys() else None,
            "authored_id": row["authored_id"] if "authored_id" in row.keys() else None,
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


def _node_edges(definition: dict[str, Any]) -> list[dict[str, str]]:
    return [e for e in (definition.get("edges") or []) if isinstance(e, dict)]


def _frozen_graph(definition: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """(slug -> runnable frozen definition with edge-derived requires/after, topo order).

    This is the ONE execution view: the stored edges drive both ordering and
    gating, and the stored registry snapshots are the definitions that run.
    """
    nodes: dict[str, dict[str, Any]] = {}
    for node in definition.get("passes") or []:
        if not isinstance(node, dict) or not isinstance(node.get("slug"), str):
            continue
        ep = dict(node.get("registry") or {})
        ep.setdefault("requires", [])
        ep.setdefault("after", [])
        nodes[node["slug"]] = ep
    requires: dict[str, list[str]] = {slug: [] for slug in nodes}
    after: dict[str, list[str]] = {slug: [] for slug in nodes}
    for edge in _node_edges(definition):
        src, dst, kind = edge.get("from"), edge.get("to"), edge.get("kind", "after")
        if src in nodes and dst in nodes:
            (requires if kind == "requires" else after)[dst].append(src)
    for slug, ep in nodes.items():
        ep["requires"] = requires[slug]
        ep["after"] = after[slug]
    order = _topo(nodes, requires, after)
    return nodes, order


def _topo(nodes: dict[str, dict[str, Any]], requires: dict[str, list[str]],
          after: dict[str, list[str]]) -> list[str]:
    state: dict[str, int] = {}
    out: list[str] = []

    def visit(slug: str, stack: list[str]) -> None:
        state[slug] = 1
        for dep in list(requires.get(slug, [])) + list(after.get(slug, [])):
            if dep not in nodes:
                continue
            if state.get(dep) == 1:
                raise PipelineError("cyclic pass dependency: "
                                    + " -> ".join(stack + [slug, dep]))
            if state.get(dep) is None:
                visit(dep, stack + [slug])
        state[slug] = 2
        out.append(slug)

    for slug in sorted(nodes):
        if state.get(slug) is None:
            visit(slug, [])
    return out


def _canonical(definition: dict[str, Any]) -> str:
    safe = {k: v for k, v in definition.items() if not k.startswith("_")}
    return json.dumps(safe, sort_keys=True, default=str)


def _definition_id(definition: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(definition).encode()).hexdigest()


def _expand_folder(folder: str) -> str:
    return str(Path(os.path.expandvars(os.path.expanduser(str(folder)))).resolve())


def _when_error(when: Any) -> Optional[str]:
    try:
        return passes._when_error(when)
    except Exception as exc:  # noqa: BLE001 - malformed structures are loud
        return f"invalid when: {exc}"


def _validate(name: str, doc: dict[str, Any], reg: dict[str, dict[str, Any]],
              ) -> dict[str, Any]:
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

    # Authored-edge mode: the author drew the graph (n8n import, or YAML that
    # names per-node requires/after). Registry mode: edges are induced from the
    # registry and frozen as-is.
    authored_edges = any(
        isinstance(node, dict) and ("requires" in node or "after" in node)
        for node in nodes_in if isinstance(node, dict)) or bool(doc.get("edges"))

    nodes: list[dict[str, Any]] = []
    slugs: list[str] = []
    for node in nodes_in:
        if isinstance(node, str):
            node = {"slug": node}
        if not isinstance(node, dict) or not isinstance(node.get("slug"), str):
            raise PipelineError(
                f"{name}: each pass entry must be a slug or a {{slug, when, requires, after}} object")
        slug = node["slug"]
        if slug in slugs:
            raise PipelineError(f"{name}: pass {slug!r} appears twice; node identity "
                                "must be unique inside one graph")
        for field in ("when", "skip_when"):
            if node.get(field) is not None and _when_error(node[field]):
                raise PipelineError(f"{name}: pass {slug}: {_when_error(node[field])}")
        slugs.append(slug)
        entry: dict[str, Any] = {"slug": slug}
        if isinstance(node.get("name"), str) and node["name"]:
            entry["name"] = node["name"]  # authoring identity (n8n node name)
        nodes.append((entry, node))

    unknown = [s for s in slugs if s not in reg]
    if unknown:
        raise PipelineError(f"{name}: unknown pass slug(s) {unknown}; see `wax ep list`")

    # Freeze the runnable definition of every node: the registry entry as it
    # stands NOW (minus file plumbing), with the authored conditions merged in.
    frozen_nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    after_external: dict[str, list[str]] = {}
    for entry, node in nodes:
        slug = entry["slug"]
        ep = {k: v for k, v in (reg.get(slug) or {}).items()
              if not str(k).startswith("_")}
        ep["version"] = int(ep.get("version") or 1)
        if node.get("when") is not None:
            ep["when"] = node["when"]
        if node.get("skip_when") is not None:
            ep["skip_when"] = node["skip_when"]
        frozen = dict(entry)
        frozen["registry"] = ep
        frozen_nodes.append(frozen)
        req = list(node.get("requires") or []) if authored_edges else list(ep.get("requires") or [])
        aft = list(node.get("after") or []) if authored_edges else list(ep.get("after") or [])
        missing = [d for d in req if d not in slugs]
        if missing:
            raise PipelineError(
                f"{name}: pass {slug} requires {missing}, which is not in this graph; "
                "add the dependency node or drop the requirement — a missing required "
                "dependency is never silently dropped")
        for dep in req:
            edges.append({"from": dep, "to": slug, "kind": "requires"})
        outside = [d for d in aft if d not in slugs]
        if outside:
            after_external[slug] = outside  # ordering-only: vacuously satisfied
        for dep in aft:
            if dep in slugs:
                edges.append({"from": dep, "to": slug, "kind": "after"})

    folders = [_expand_folder(f) for f in (doc.get("folders") or [])]
    if not all(isinstance(f, str) and Path(f).is_absolute() for f in folders):
        raise PipelineError(f"{name}: folders must be absolute paths")
    if trigger == "documents" and not folders:
        raise PipelineError(f"{name}: trigger 'documents' requires at least one folder")
    if trigger != "documents" and folders:
        raise PipelineError(f"{name}: folders are only valid with trigger 'documents'")

    if trigger == "transcription":
        # A transcription pipeline becomes the audio PLAN authority when
        # enabled; a plan can only contain automatic passes (ensure_plan's
        # rule, enforced here at compile instead of at 3 a.m.).
        offenders = [n["slug"] for n in frozen_nodes
                     if not (n["registry"].get("auto") and n["registry"].get("enabled"))]
        if offenders:
            raise PipelineError(
                f"{name}: transcription pipelines may only compile automatic, enabled "
                f"passes; {offenders} are not")

    # Acyclicity over the frozen edge set.
    requires_map = {n["slug"]: [e["from"] for e in edges
                                if e["to"] == n["slug"] and e["kind"] == "requires"]
                    for n in frozen_nodes}
    after_map = {n["slug"]: [e["from"] for e in edges
                             if e["to"] == n["slug"] and e["kind"] == "after"]
                 for n in frozen_nodes}
    node_map = {n["slug"]: n["registry"] for n in frozen_nodes}
    _topo(node_map, requires_map, after_map)

    definition: dict[str, Any] = {
        "name": name,
        "version": int(doc.get("version") or 1),
        "description": doc.get("description") or "",
        "trigger": trigger,
        "enabled": bool(doc.get("enabled")),
        "failure_policy": policy,
        "folders": folders,
        "passes": frozen_nodes,
        "edges": edges,
    }
    if after_external:
        definition["after_external"] = after_external
    return definition


def _authored_slugs(doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for node in doc.get("passes") or []:
        slug = node.get("slug") if isinstance(node, dict) else node
        if isinstance(slug, str):
            out.append(slug)
    return out


def _store(name: str, definition: dict[str, Any], *, source: str,
           source_ref: str, authored_id: str) -> str:
    definition_id = _definition_id(definition)
    encoded = json.dumps(definition, sort_keys=True, default=str)
    ledger.connect().execute(
        "INSERT INTO pipelines(name,version,definition_id,definition,compiled_at,"
        "source,source_ref,authored_id) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET version=excluded.version, "
        "definition_id=excluded.definition_id, definition=excluded.definition, "
        "compiled_at=excluded.compiled_at, source=excluded.source, "
        "source_ref=excluded.source_ref, authored_id=excluded.authored_id",
        (name, definition["version"], definition_id, encoded, sentinel.utcnow(),
         source, source_ref, authored_id),
    )
    if definition["trigger"] == "documents":
        _sync_bindings(name, definition["folders"])
    return definition_id


def compile(names: Optional[list[str]] = None) -> dict[str, Any]:
    """Validate and store authored YAML definitions. Explicit, idempotent, loud.

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
            existing = ledger.connect().execute(
                "SELECT definition_id FROM pipelines WHERE name=?", (name,)).fetchone()
            definition = _validate(name, doc, reg)
            definition_id = _definition_id(definition)
            if existing and existing["definition_id"] == definition_id:
                out["unchanged"].append({"name": name, "definition_id": definition_id})
                continue
            _store(name, definition, source="yaml",
                   source_ref=str(doc.get("_path") or ""),
                   authored_id=_file_id(Path(doc.get("_path") or "")))
            out["compiled"].append({"name": name, "version": definition["version"],
                                    "definition_id": definition_id})
        except PipelineError as e:
            out["failed"][name] = str(e)
    if not wanted:
        out["note"] = f"no authored pipelines under {PIPELINES_DIR}"
    return out


def _file_id(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# ------------------------------------------------------------ n8n import ----

def _n8n_param(node: dict[str, Any], *names: str) -> Any:
    params = node.get("parameters") or {}
    for name in names:
        if isinstance(params, dict) and name in params:
            return params[name]
    return None


def _n8n_slug(node: dict[str, Any]) -> Optional[str]:
    ntype = str(node.get("type") or "")
    if ntype.startswith(N8N_PASS_TYPE_PREFIX):
        return ntype[len(N8N_PASS_TYPE_PREFIX):]
    if ntype == N8N_EP_TYPE:
        slug = _n8n_param(node, "pass")
        return slug if isinstance(slug, str) and slug else None
    return None


def _parse_n8n_condition(value: Any, *, node_name: str, field: str) -> Any:
    """A WaxEp `when`/`skipWhen` parameter: JSON-encoded condition list/object."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise PipelineError(f"node {node_name!r}: {field} must be a JSON string")
    try:
        parsed = json.loads(value)
    except ValueError:
        raise PipelineError(f"node {node_name!r}: {field} is not valid JSON: {value[:120]!r}")
    return parsed


def import_n8n(path: Path, *, name: Optional[str] = None) -> dict[str, Any]:
    """Compile a supported exported n8n workflow JSON into a Wax pipeline.

    The export is an AUTHORING artifact, nothing more: importing it compiles a
    frozen definition exactly like YAML authoring does. There is deliberately
    no support for n8n trigger/schedule/webhook nodes — an imported graph can
    never become a second scheduler, because the only executable triggers are
    Wax's own.

    Mapping:
    - exactly one Wax Pipeline wrapper node → trigger / folders / failure
      policy / enabled / description;
    - Wax Pass nodes (per-pass node types or the generic Wax Enrichment Pass)
      → one graph node each, slug fixed, optional when/skipWhen conditions;
    - ``main`` connections → ordering (``after``) edges — continue;
    - ``requirement`` connections → gating (``requires``) edges — gated;
    - sticky notes are annotations and ignored.

    Node identity is preserved: the n8n node NAME is stored on the node and
    appears in the catalog. Unsupported node types, dynamic (``=``-prefixed)
    parameters and unknown connection types are rejected loudly.
    """
    try:
        export = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise PipelineError(f"cannot read n8n export {path}: {e}")
    if not isinstance(export, dict) or not isinstance(export.get("nodes"), list):
        raise PipelineError("n8n export must be an object with a 'nodes' array")
    workflow_name = str(export.get("name") or path.stem)
    if name is None:
        name = re.sub(r"[^a-z0-9-]+", "-", workflow_name.lower()).strip("-")
    if not NAME_RE.match(name or ""):
        raise PipelineError(f"workflow name {workflow_name!r} is not a pipeline slug; "
                            "pass --name <slug>")

    reg = passes.registry()
    wrappers: list[dict[str, Any]] = []
    pass_nodes: list[dict[str, Any]] = []
    for node in export["nodes"]:
        if not isinstance(node, dict):
            raise PipelineError("every n8n node must be an object")
        ntype = str(node.get("type") or "")
        if ntype in N8N_ANNOTATION_TYPES:
            continue
        if ntype == N8N_WRAPPER_TYPE:
            wrappers.append(node)
            continue
        slug = _n8n_slug(node)
        if slug is not None:
            pass_nodes.append(node)
            continue
        raise PipelineError(
            f"unsupported node type {ntype!r} ({node.get('name')!r}): a Wax graph may "
            "only contain Wax Pipeline / Wax Pass nodes (sticky notes are ignored). "
            "n8n triggers and actions cannot join a Wax pipeline — Wax owns execution.")

    if len(wrappers) != 1:
        raise PipelineError(f"expected exactly one Wax Pipeline node, found {len(wrappers)}")
    wrapper = wrappers[0]
    if not pass_nodes:
        raise PipelineError("the workflow contains no Wax Pass nodes")

    def _static(value: Any, *, what: str) -> Any:
        if isinstance(value, str) and value.startswith("="):
            raise PipelineError(
                f"{what} is a dynamic expression ({value[:60]!r}); a Wax graph must be "
                "static — expressions are evaluated by Wax conditions at run time")
        return value

    trigger = _static(_n8n_param(wrapper, "trigger"), what="pipeline trigger")
    policy = _static(_n8n_param(wrapper, "failurePolicy"), what="failure policy") or "independent"
    enabled = _static(_n8n_param(wrapper, "enabled"), what="pipeline enabled")
    folders_param = _static(_n8n_param(wrapper, "folders"), what="folders")
    folders = folders_param if isinstance(folders_param, list) else []
    description = _static(_n8n_param(wrapper, "description"), what="description") or workflow_name

    doc_nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in pass_nodes:
        slug = _n8n_slug(node)
        slug = _static(slug, what=f"node {node.get('name')!r} pass")
        if slug in seen:
            raise PipelineError(f"pass {slug!r} appears on two nodes; one graph node "
                                "per pass (node identity must be unique)")
        seen.add(slug)
        entry: dict[str, Any] = {
            "slug": slug,
            "name": str(node.get("name") or slug),
            "when": _parse_n8n_condition(
                _static(_n8n_param(node, "when"), what=f"node {node.get('name')!r} when"),
                node_name=str(node.get("name")), field="when"),
            "skip_when": _parse_n8n_condition(
                _static(_n8n_param(node, "skipWhen"), what=f"node {node.get('name')!r} skipWhen"),
                node_name=str(node.get("name")), field="skipWhen"),
        }
        doc_nodes.append({k: v for k, v in entry.items() if v is not None})

    # Connections → authored edges. `name` is the n8n node identity.
    by_name = {str(n.get("name")): n for n in pass_nodes}
    connections = export.get("connections") or {}
    if not isinstance(connections, dict):
        raise PipelineError("'connections' must be an object")
    for source_name, kinds in connections.items():
        source = by_name.get(str(source_name))
        if source is None:
            if str(source_name) in {str(w.get("name")) for w in wrappers}:
                continue  # wrapper node wires are annotations, not graph edges
            raise PipelineError(f"connection from unknown node {source_name!r}")
        if not isinstance(kinds, dict):
            raise PipelineError(f"connections of {source_name!r} must be an object")
        for kind, targets in kinds.items():
            if kind not in N8N_EDGE_KINDS:
                raise PipelineError(
                    f"unsupported connection type {kind!r} on {source_name!r}; a Wax graph "
                    "uses 'main' (continue) and 'requirement' (gated) connections")
            if not isinstance(targets, list):
                continue
            for group in targets:
                if not isinstance(group, list):
                    continue
                for link in group:
                    if not isinstance(link, dict):
                        continue
                    target = by_name.get(str(link.get("node")))
                    if target is None:
                        raise PipelineError(
                            f"{kind!r} connection from {source_name!r} to unknown node "
                            f"{link.get('node')!r}")
                    target_slug = _n8n_slug(target)
                    field = "requires" if N8N_EDGE_KINDS[kind] == "requires" else "after"
                    for entry in doc_nodes:
                        if entry["slug"] == target_slug:
                            entry.setdefault(field, []).append(_n8n_slug(source))

    doc: dict[str, Any] = {
        "name": name,
        "description": str(description),
        "trigger": trigger,
        "enabled": True if enabled is True else False,
        "failure_policy": policy,
        "folders": folders,
        "passes": doc_nodes,
    }
    definition = _validate(name, doc, reg)
    definition_id = _store(name, definition, source="n8n",
                           source_ref=workflow_name, authored_id=_file_id(path))
    return {"imported": name, "workflow": workflow_name, "version": definition["version"],
            "definition_id": definition_id, "trigger": definition["trigger"],
            "nodes": len(definition["passes"]), "edges": len(definition["edges"])}


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


def registry_drift(definition: dict[str, Any],
                   reg: Optional[dict[str, dict[str, Any]]] = None) -> dict[str, Any]:
    """Which frozen nodes no longer match the live pass registry.

    This is informational drift, deliberately separate from authored-vs-
    compiled drift: freezing is the point of compiling, but an operator must
    be able to see that the registry moved on and a recompile would change the
    running graph.
    """
    reg = reg if reg is not None else passes.registry()
    changed: list[dict[str, Any]] = []
    for node in definition.get("passes") or []:
        if not isinstance(node, dict):
            continue
        slug = node.get("slug")
        frozen = node.get("registry") or {}
        live = reg.get(slug)
        if live is None:
            changed.append({"slug": slug, "change": "unregistered"})
            continue
        frozen_id = operations.definition_id(frozen)
        live_id = operations.definition_id({k: v for k, v in live.items()
                                            if not str(k).startswith("_")})
        if frozen_id != live_id or int(frozen.get("version") or 1) != int(live.get("version") or 1):
            changed.append({"slug": slug, "change": "definition",
                            "frozen_version": int(frozen.get("version") or 1),
                            "live_version": int(live.get("version") or 1)})
    return {"changed": changed, "count": len(changed)}


def drift(name: str) -> dict[str, Any]:
    """Authored vs compiled identity for one pipeline. Content hashes both ways."""
    current = compiled().get(name)
    if current and current.get("source") == "n8n":
        # The authored artifact lives in n8n, which Wax cannot read; the import
        # IS the compile, so authored-vs-compiled cannot drift. Identity is the
        # export that produced it; registry drift is reported separately.
        return {"name": name, "state": "in_sync", "source": "n8n",
                "source_ref": current.get("source_ref"),
                "definition_id": current["definition_id"],
                "detail": f"authored in n8n workflow {current.get('source_ref')!r}; "
                          "re-import to adopt a changed export"}
    source = authored().get(name)
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


# ------------------------------------------------ transcription authority ----

def transcription_authority() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """The plan authority for NEW audio items: one compiled pipeline, or the registry.

    Returns (definitions, provenance). provenance names what minted the plan
    ({"kind": "pipeline", name, definition_id} or {"kind": "registry"}) and is
    stored on the processing plan. With zero enabled compiled transcription
    pipelines the legacy live-registry plan stands; with more than one the
    choice is ambiguous, which is refused loudly (once) before falling back —
    an ambiguous graph is never silently fabricated as the active one.
    """
    reg = passes.registry()
    candidates = [p for p in compiled().values()
                  if p["definition"].get("trigger") == "transcription"
                  and p["definition"].get("enabled")]
    if not candidates:
        order = passes.ordered(reg)
        return ({slug: reg[slug] for slug in order if reg[slug].get("auto")},
                {"kind": "registry"})
    if len(candidates) > 1:
        names = sorted(p["name"] for p in candidates)
        message = (f"multiple enabled transcription pipelines {names}; refusing to "
                   "pick one — using the legacy registry plan")
        log.error(message)
        ledger.record_transition("pipeline", None, None, "error",
                                 "ambiguous_transcription_authority", message[:400])
        order = passes.ordered(reg)
        return ({slug: reg[slug] for slug in order if reg[slug].get("auto")},
                {"kind": "registry", "fallback_reason": message})
    chosen = candidates[0]
    nodes, _order = _frozen_graph(chosen["definition"])
    return (nodes, {"kind": "pipeline", "name": chosen["name"],
                    "definition_id": chosen["definition_id"]})


# ------------------------------------------------------------- execution ----

def _pipeline(name: str) -> dict[str, Any]:
    """The compiled definition — the only thing that may execute."""
    current = compiled().get(name)
    if current is None:
        raise PipelineError(f"pipeline {name!r} is not compiled; run `wax pipeline compile {name}`")
    return current


def _record_skip(item_key: str, slug: str, reason: str, detail: str = "",
                 ep: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    version = int((ep or {}).get("version") or 1)
    passes._record(item_key, slug, "skipped", version=version,
                   attempt=passes._next_attempt(item_key, slug),
                   detail=detail, reason_code=reason, definition=ep)
    md = passes.md_for(item_key)
    if md is not None:
        try:
            passes._write_pass_note(md, item_key, slug, {
                "state": "skipped", "at": sentinel.utcnow(), "version": version,
                "reason_code": reason,
                **({"detail": detail} if detail else {}),
            })
        except OSError:
            pass
    return {"item_id": item_key, "ep_slug": slug, "state": "skipped",
            "reason_code": reason, **({"detail": detail} if detail else {})}


def run_document(name: str, path: Path, *, binding_id: str = "",
                 doc_id: Optional[str] = None) -> dict[str, Any]:
    """Run one compiled pipeline over one Markdown document.

    The pipeline module is the document scenario's ONLY executor: it resolves
    the compiled FROZEN definition, registers the document (content-addressed
    per binding, no transcripts row), enforces the per-(binding, doc,
    pipeline, definition) receipt and the per-document run lock, and drives
    the ordinary pass runner — so dependency gating, attempts, quarantine and
    no-clobber protections are the same machinery audio uses, against the
    frozen graph rather than the live registry.

    ``doc_id`` adopts an existing registration (a scan retry or backfill): the
    run continues that document's identity instead of minting a new one.
    """
    current = _pipeline(name)
    definition = current["definition"]
    if not definition.get("enabled"):
        return {"pipeline": name, "skipped": "pipeline is not enabled "
                "(compiled enabled: false)"}
    if definition.get("trigger") not in ("documents", "manual"):
        return {"pipeline": name, "skipped": f"trigger {definition.get('trigger')!r} "
                "is not a document scenario"}

    row, _arrival = documents.register(path, binding_id=binding_id,
                                       origin=f"pipeline:{name}",
                                       adopt_doc_id=doc_id)
    if "error" in row:
        raise PipelineError(row["error"])
    doc_id = row["doc_id"]
    item_key = documents.doc_key(binding_id, doc_id)
    out: dict[str, Any] = {"pipeline": name, "doc_id": doc_id, "path": str(path),
                           "binding_id": binding_id, "item_key": item_key,
                           "definition_id": current["definition_id"],
                           "version": current["version"]}
    receipt = documents.receipt(binding_id, doc_id, name, current["definition_id"])
    if receipt is None and not binding_id:
        # An UNBOUND manual run defers to any binding that already completed
        # this content at this definition: dropping a copy of a processed file
        # somewhere is a duplicate arrival, a no-op. Bindings themselves never
        # suppress one another — per-binding receipts are the point.
        done = next((r for r in documents.receipts_for_doc(
            doc_id, name, current["definition_id"]) if r["state"] == "completed"), None)
        if done is not None:
            out["skipped"] = (f"already processed at this definition under binding "
                              f"{done['binding_id']} (duplicate arrival)")
            return out
    if receipt and receipt.get("state") == "completed":
        out["skipped"] = "already processed at this definition (duplicate arrival)"
        return out
    if receipt and receipt.get("exhausted_at"):
        out["skipped"] = (f"exhausted after {receipt.get('attempts')} attempts "
                          f"({(receipt.get('last_error') or '')[:120]})")
        return out
    if not documents.claim_run(binding_id, doc_id):
        out["skipped"] = "another run owns this document (run lock held)"
        return out
    try:
        out["results"] = _execute_frozen(item_key, definition)
    except _Halted as halt:
        out["results"] = halt.results
        out["halted"] = True
        out["halted_after"] = halt.after_slug
    except Exception as exc:  # noqa: BLE001 - the receipt must record any crash
        documents.release_run(binding_id, doc_id, "failed", error=str(exc))
        documents.record_receipt(binding_id, doc_id, name, current["definition_id"],
                                 "failed", error=str(exc))
        documents.note_own_write(binding_id, doc_id, path)
        raise
    completed = sum(1 for r in out["results"] if r.get("state") == "completed")
    failed = sum(1 for r in out["results"] if r.get("state") == "failed")
    skipped = sum(1 for r in out["results"] if r.get("state") == "skipped")
    halted = bool(out.get("halted"))
    out["completed"] = completed
    out["failed"] = failed
    out["skipped_count"] = skipped
    # The receipt lands only on a fully-terminal run; a document with a failed
    # pass stays unprocessed, so the scan and a manual re-run keep retrying
    # exactly the failed nodes (completed ones replay as no-ops).
    terminal = bool(out["results"]) and failed == 0 and not halted
    first_error = next((r.get("error") or r.get("reason_code") or ""
                        for r in out["results"] if r.get("state") == "failed"), "")
    if terminal:
        documents.record_receipt(binding_id, doc_id, name, current["definition_id"],
                                 "completed")
        documents.release_run(binding_id, doc_id, "processed")
    else:
        documents.record_receipt(binding_id, doc_id, name, current["definition_id"],
                                 "halted" if halted else "failed", error=str(first_error))
        documents.release_run(binding_id, doc_id, "failed", error=str(first_error))
    # Own-write detection after EVERY run, terminal or not: a failing run
    # still writes its pass notes, and an unrecorded hash change is how a
    # pipeline re-registers its own output as a new arrival forever.
    documents.note_own_write(binding_id, doc_id, path)
    return out


def _execute_frozen(item_key: str, definition: dict[str, Any]) -> list[dict[str, Any]]:
    """Drive the pass runner over the FROZEN graph: stored order, stored edges,
    stored definitions, stored conditions. The live registry is not consulted
    for execution (only `wax ep list` and the catalog compare against it)."""
    nodes, order = _frozen_graph(definition)
    dependency_versions = {slug: int(ep.get("version") or 1) for slug, ep in nodes.items()}
    previous = {row["ep_slug"]: dict(row) for row in ledger.connect().execute(
        "SELECT * FROM passes WHERE item_id=?", (item_key,)).fetchall()}
    results: list[dict[str, Any]] = []
    for slug in order:
        ep = nodes[slug]
        version = int(ep.get("version") or 1)
        # Availability is surfaced, never silently dropped: a node disabled at
        # compile time records a visible skip instead of running.
        if not ep.get("enabled"):
            results.append(_record_skip(item_key, slug, "disabled",
                                        "pass is disabled in the compiled definition", ep=ep))
            continue
        definition_id = operations.definition_id(ep)
        prior = previous.get(slug)
        if (prior and prior["state"] in ("completed", "skipped")
                and int(prior["version"] or 1) == version
                and prior.get("definition_id") in (None, definition_id)):
            # One effective run per revision/definition: a re-drop of content
            # that already completed this definition is a no-op, while a
            # FAILED pass stays retryable.
            results.append({"item_id": item_key, "ep_slug": slug, "version": version,
                            "state": prior["state"], "skipped": "already completed at this version"})
            continue
        try:
            results.append(passes.run(item_key, slug, definition=ep,
                                      dependency_versions=dependency_versions))
        except passes.PassError as e:
            results.append({"item_id": item_key, "ep_slug": slug, "state": "failed",
                            "error": str(e), "reason_code": "run_error"})
        if definition.get("failure_policy") == "halt" and results[-1].get("state") == "failed":
            # halt means stop: the loop ends here, and the run reports which
            # pass stopped it. The un-run tail stays un-run — truthfully.
            raise _Halted(results, slug)
    return results


class _Halted(RuntimeError):
    """Internal: failure_policy halt. Carries the results collected so far."""

    def __init__(self, results: list[dict[str, Any]], after_slug: str):
        super().__init__(f"failure_policy halt stopped the run after {after_slug}")
        self.results = results
        self.after_slug = after_slug
