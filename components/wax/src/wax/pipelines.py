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
# Edge semantics live on the SOURCE OUTPUT INDEX of a stock `main` connection:
# output 0 is labeled `continue` (ordering, an `after` edge), output 1 is
# labeled `requires` (gating, a `requires` edge). Both are stock
# NodeConnectionType.main — n8n cannot wire (let alone render) a made-up
# connection type, so the kind must travel as the port index, and any other
# index or type is rejected rather than guessed at.
N8N_OUTPUT_KINDS = {0: "after", 1: "requires"}


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


def _skip_when_error(skip: Any) -> Optional[str]:
    """`skip_when` has its own shape: exactly {field, equals}, one clause.

    The runner evaluates it as a dict (passes.run reads condition["field"] and
    condition["equals"]); validating it with the list-shaped `when` validator
    rejected every legitimate skip_while node at compile, so the two shapes get
    dedicated validators that match what run time actually consumes.
    """
    if skip is None:
        return None
    if (not isinstance(skip, dict) or set(skip) != {"field", "equals"}
            or not isinstance(skip.get("field"), str)):
        return "invalid skip_when: expected exactly {field: <frontmatter key>, equals: <value>}"
    return None


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

    # Authored edges are PER NODE: a node that names `requires`/`after` owns its
    # edges exactly as authored; a node that names neither inherits the
    # registry's edges. Mixed YAML is therefore explicit about its intent
    # instead of silently dropping (or silently inventing) edges, and an n8n
    # import — which writes the keys onto EVERY node, empty lists included —
    # is always fully authored, so a workflow with no connections compiles to
    # a graph with no edges rather than inheriting the registry's.
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
        for field, validator in (("when", _when_error), ("skip_when", _skip_when_error)):
            if node.get(field) is not None:
                why = validator(node[field])
                if why:
                    raise PipelineError(f"{name}: pass {slug}: {why}")
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
        # Per-node authored edges: a node that names the key owns its edge list
        # (an import writes both keys, empty lists included); a node that names
        # neither inherits the registry's edges for that key.
        req = list(node.get("requires") or []) if "requires" in node else list(ep.get("requires") or [])
        aft = list(node.get("after") or []) if "after" in node else list(ep.get("after") or [])
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

    # Folders are validated RAW, before any ~ / $VAR resolution: a relative
    # path would silently resolve against the compiling process's cwd and
    # freeze the wrong absolute folder into the definition.
    raw_folders = doc.get("folders") or []
    if not isinstance(raw_folders, list) or any(not isinstance(f, str) for f in raw_folders):
        raise PipelineError(f"{name}: folders must be a list of path strings")
    for raw in raw_folders:
        if not (raw.startswith("~") or Path(raw).is_absolute()):
            raise PipelineError(
                f"{name}: folder {raw!r} must be absolute (or start with ~) BEFORE "
                "expansion; a relative folder would resolve against the compiler's cwd")
    folders = [_expand_folder(f) for f in raw_folders]
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
      policy / enabled / description / version (the Configure (Authoring)
      operation is where those parameters live in the editor);
    - Wax Pass nodes (per-pass node types or the generic Wax Enrichment Pass)
      → one graph node each, slug fixed, optional when/skipWhen conditions;
    - stock ``main`` connections carry the edge kind on the SOURCE OUTPUT
      INDEX: output 0 (labeled ``continue``) → ordering (``after``) edges,
      output 1 (labeled ``requires``) → gating (``requires``) edges;
    - an export with no connections compiles to a graph with NO edges — the
      registry's edges are never inherited by an import;
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
    description = _static(_n8n_param(wrapper, "description"), what="description") or workflow_name
    version_param = _static(_n8n_param(wrapper, "version"), what="pipeline version")
    if version_param is not None and (isinstance(version_param, bool)
                                      or not isinstance(version_param, (int, float))
                                      or int(version_param) < 1):
        raise PipelineError("pipeline version must be a positive integer")

    def _folders(value: Any) -> list[str]:
        """Folders arrive either as a plain list of strings or in the n8n
        editor's fixedCollection export shape ({folderValues: [{folder}]}) —
        the shape the Configure (Authoring) operation produces."""
        if value is None:
            return []
        if isinstance(value, dict):
            entries = value.get("folderValues")
            if not isinstance(entries, list):
                raise PipelineError("folders collection must contain 'folderValues'")
            out = []
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("folder"), str):
                    raise PipelineError("each folders entry must be {folder: <path>}")
                out.append(entry["folder"])
            return out
        if isinstance(value, list) and all(isinstance(f, str) for f in value):
            return list(value)
        raise PipelineError("folders must be a list of absolute paths or the editor's "
                            "Folders collection")

    folders = _folders(folders_param)

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

    # Connections → authored edges. `name` is the n8n node identity, and edge
    # KIND rides the SOURCE OUTPUT INDEX of a stock `main` connection: output 0
    # (labeled `continue`) maps to an `after` edge, output 1 (labeled
    # `requires`) to a `requires` edge. Every node ends up with explicit
    # requires/after keys — empty lists included — so an export with NO
    # connections compiles to a graph with NO edges, never the registry's.
    by_name = {str(n.get("name")): n for n in pass_nodes}
    wrapper_names = {str(w.get("name")) for w in wrappers}
    edges_by_slug = {entry["slug"]: {"requires": [], "after": []} for entry in doc_nodes}
    connections = export.get("connections", {})
    if not isinstance(connections, dict):
        raise PipelineError("'connections' must be an object")
    for source_name, kinds in connections.items():
        if str(source_name) in wrapper_names:
            continue  # wrapper node wires are annotations, not graph edges
        source = by_name.get(str(source_name))
        if source is None:
            raise PipelineError(f"connection from unknown node {source_name!r}")
        if not isinstance(kinds, dict):
            raise PipelineError(f"connections of {source_name!r} must be an object")
        for kind, outputs in kinds.items():
            if kind != "main":
                raise PipelineError(
                    f"unsupported connection type {kind!r} on {source_name!r}; Wax pass "
                    "nodes expose only stock 'main' outputs — output 0 'continue' orders, "
                    "output 1 'requires' gates")
            if not isinstance(outputs, list) or not outputs:
                raise PipelineError(
                    f"malformed connections on {source_name!r}: 'main' must be a non-empty "
                    "array of output groups ([[targets of output 0], [targets of output 1]])")
            for out_index, group in enumerate(outputs):
                if out_index not in N8N_OUTPUT_KINDS:
                    raise PipelineError(
                        f"connection on {source_name!r} uses source output index {out_index}; "
                        "Wax pass nodes have exactly two outputs (0 continue, 1 requires)")
                if not isinstance(group, list):
                    raise PipelineError(
                        f"malformed connection group on {source_name!r} output {out_index}: "
                        "expected an array of link objects")
                for link in group:
                    if not isinstance(link, dict):
                        raise PipelineError(
                            f"malformed connection link on {source_name!r} output {out_index}: "
                            "expected an object like {node, type, index}")
                    link_type = link.get("type", "main")
                    if link_type != "main":
                        raise PipelineError(
                            f"unsupported link type {link_type!r} on {source_name!r} output "
                            f"{out_index}; Wax pass node inputs are stock 'main'")
                    target_index = link.get("index", 0)
                    if (isinstance(target_index, bool) or not isinstance(target_index, int)
                            or target_index != 0):
                        raise PipelineError(
                            f"connection into {str(link.get('node'))!r} targets input index "
                            f"{target_index!r}; a Wax pass node has exactly one input")
                    if str(link.get("node")) in wrapper_names:
                        continue  # a wire into the wrapper is an annotation
                    target = by_name.get(str(link.get("node")))
                    if target is None:
                        raise PipelineError(
                            f"main connection from {source_name!r} to unknown node "
                            f"{link.get('node')!r}")
                    edges = edges_by_slug[_n8n_slug(target)]
                    edges[N8N_OUTPUT_KINDS[out_index]].append(_n8n_slug(source))
    for entry in doc_nodes:
        edges = edges_by_slug[entry["slug"]]
        # Dedup order-preserving: a wire drawn twice is still one edge.
        entry["requires"] = list(dict.fromkeys(edges["requires"]))
        entry["after"] = list(dict.fromkeys(edges["after"]))

    doc: dict[str, Any] = {
        "name": name,
        "description": str(description),
        "trigger": trigger,
        "enabled": True if enabled is True else False,
        "failure_policy": policy,
        "folders": folders,
        "passes": doc_nodes,
        **({"version": int(version_param)} if version_param is not None else {}),
    }
    definition = _validate(name, doc, reg)
    # Imported versions are SYSTEM-managed: an identical re-import is a no-op,
    # and a changed export increments past whatever is stored — never silently
    # rewriting history under the same version, never downgrading.
    existing = ledger.connect().execute(
        "SELECT version, definition_id FROM pipelines WHERE name=?", (name,)).fetchone()
    if existing is not None:
        if existing["definition_id"] == _definition_id(definition):
            return {"imported": name, "workflow": workflow_name,
                    "version": int(existing["version"] or 1),
                    "definition_id": existing["definition_id"],
                    "trigger": definition["trigger"], "unchanged": True,
                    "nodes": len(definition["passes"]), "edges": len(definition["edges"])}
        definition["version"] = max(int(definition["version"] or 1),
                                    int(existing["version"] or 1) + 1)
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
                    "definition_id": chosen["definition_id"],
                    "failure_policy": chosen["definition"].get("failure_policy", "independent")})


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
                 doc_id: Optional[str] = None, explicit: bool = False) -> dict[str, Any]:
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

    ``explicit`` marks an operator action (``wax documents run``, a named
    ``documents scan``, ``backfill``): an explicitly invoked pipeline runs
    even while compiled ``enabled: false`` — the toggle gates AUTOMATIC pickup
    (the worker's scan, plan authority), not a human naming the pipeline.
    Cross-process exclusion is a real ``flock`` held for the whole run: a
    crashed holder releases it with its process, and no staleness window lets
    a live run be stolen. The DB ``processing`` state remains the visible
    progress marker, claimed only while the lock is held.
    """
    current = _pipeline(name)
    definition = current["definition"]
    if not definition.get("enabled") and not explicit:
        return {"pipeline": name, "skipped": "pipeline is not enabled "
                "(compiled enabled: false); enable it or invoke it explicitly"}
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
    if explicit and not definition.get("enabled"):
        out["explicit"] = "running a compiled-disabled pipeline (operator action)"
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
                          "(re-arm it with `wax documents retry`)")
        return out
    lock = documents.document_run_lock(binding_id, doc_id)
    try:
        acquired = lock.acquire()
    except OSError as exc:
        raise PipelineError(f"cannot open the document run lock: {exc}") from exc
    if not acquired:
        out["skipped"] = "another run owns this document (run lock held)"
        return out
    try:
        # force: we hold the exclusive lock, so any `processing` state visible
        # here belongs to a crashed holder whose flock died with its process.
        if not documents.claim_run(binding_id, doc_id, force=True):
            out["skipped"] = "document state does not admit a run"
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
                                     "failed", error=str(exc), reason_code="run_error")
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
        # The receipt carries a reason CODE, never the raw error text: exception
        # prose and stderr tails can embed secrets, and receipts surface through
        # the catalog (Holocene/n8n read it). Detail stays in the operator ledger.
        first_reason = next((str(r.get("reason_code") or "failed")
                             for r in out["results"] if r.get("state") == "failed"), None)
        if halted:
            first_reason = "halt_policy"
        if terminal:
            documents.record_receipt(binding_id, doc_id, name, current["definition_id"],
                                     "completed")
            documents.release_run(binding_id, doc_id, "processed")
        else:
            documents.record_receipt(binding_id, doc_id, name, current["definition_id"],
                                     "halted" if halted else "failed",
                                     error=str(next((r.get("error") or r.get("reason_code") or ""
                                                     for r in out["results"]
                                                     if r.get("state") == "failed"), "")),
                                     reason_code=first_reason)
            documents.release_run(binding_id, doc_id, "failed",
                                  error=str(first_reason or ""))
        # Own-write detection after EVERY run, terminal or not: a failing run
        # still writes its pass notes, and an unrecorded hash change is how a
        # pipeline re-registers its own output as a new arrival forever.
        documents.note_own_write(binding_id, doc_id, path)
        return out
    finally:
        lock.release()


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
