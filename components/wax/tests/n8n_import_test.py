"""n8n graph authoring: exported workflow JSON compiled into Wax pipelines.

The export is an authoring artifact and nothing more — importing it must
compile the same frozen, content-addressed definition YAML authoring does,
preserve node identity, and reject loudly everything Wax cannot own:
unsupported node types (native triggers/actions), dynamic expressions,
cycles, duplicate passes, unknown connection types, malformed connection
shapes, wrong source/target output indexes, relative folders, and a
skip_when that is not the dict the runner evaluates.

Edge semantics ride the SOURCE OUTPUT INDEX of a stock ``main`` connection:
output 0 (labeled ``continue``) orders; output 1 (labeled ``requires``)
gates. An export with no connections compiles to a graph with NO edges —
never the registry's.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import catalog, documents, events, frontmatter, ledger, passes, paths, pipelines

from documents_test import _close_test_connection, _fake_frontmatters, _write_registry, _Scenario

EXAMPLES = Path(__file__).resolve().parent.parent.parent / "n8n-nodes-heyma" / "examples"


def _pass_node(name: str, slug: str, *, when: str | None = None,
               skip_when: str | None = None) -> dict:
    parameters = {"pass": slug}
    if when is not None:
        parameters["when"] = when
    if skip_when is not None:
        parameters["skipWhen"] = skip_when
    return {"name": name, "type": "n8n-nodes-heyma.waxEp",
            "typeVersion": 1, "parameters": parameters, "position": [0, 0]}


def _typed_pass_node(name: str, slug: str) -> dict:
    """A per-pass derived node type (the generated wax-pass-<slug> nodes)."""
    return {"name": name, "type": f"n8n-nodes-heyma.wax-pass-{slug}",
            "typeVersion": 1, "parameters": {}, "position": [0, 0]}


def _wrapper(*, trigger: str = "documents", folders: list | dict | str | None = None,
             enabled: bool = True, failure_policy: str = "independent",
             version: int | None = None) -> dict:
    parameters = {"trigger": trigger, "folders": folders if folders is not None else [],
                  "enabled": enabled, "failurePolicy": failure_policy}
    if version is not None:
        parameters["version"] = version
    return {"name": "Wax Pipeline", "type": "n8n-nodes-heyma.waxPipeline",
            "typeVersion": 1, "parameters": parameters, "position": [0, 0]}


def _export(tmp: Path, doc: dict) -> Path:
    path = tmp / "export.json"
    path.write_text(json.dumps(doc))
    return path


def _link(node: str, *, index: int = 0, type_: str = "main") -> dict:
    return {"node": node, "type": type_, "index": index}


class N8nImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.registry = self.root / "passes.d"
        self.pipes = self.root / "pipelines.d"
        self.docs = self.root / "docs"
        for d in (self.runtime, self.registry, self.pipes, self.docs):
            d.mkdir()
        _write_registry(self.registry)
        # beta: document-capable (used for gated-edge tests)
        beta = self.registry / "beta"
        beta.write_text("#!/bin/sh\nprintf '%s\\n' '" + json.dumps(
            {"wax_ep_version": 1, "frontmatter": {"summary": "beta"}}) + "'\n")
        beta.chmod(0o755)
        (self.registry / "beta.yaml").write_text(
            "slug: beta\nversion: 1\ndescription: beta\nenabled: true\nauto: false\n"
            "capabilities: [document]\nrequires: []\n"
            f"command: [{json.dumps(str(beta))}, \"{{md_path}}\"]\n"
        )
        # gamma: REGISTRY-declared requires alpha, to prove imports never
        # inherit registry edges (an unconnected export is an unconnected graph)
        gamma = self.registry / "gamma"
        gamma.write_text("#!/bin/sh\nprintf '%s\\n' '" + json.dumps(
            {"wax_ep_version": 1, "frontmatter": {"summary": "gamma"}}) + "'\n")
        gamma.chmod(0o755)
        (self.registry / "gamma.yaml").write_text(
            "slug: gamma\nversion: 1\ndescription: gamma\nenabled: true\nauto: false\n"
            "capabilities: [document]\nrequires: [alpha]\n"
            f"command: [{json.dumps(str(gamma))}, \"{{md_path}}\"]\n"
        )
        self.stack = [
            patch.object(paths, "DB", self.runtime / "wax.db"),
            patch.object(paths, "VAR", self.runtime),
            patch.object(passes, "REGISTRY_DIR", self.registry),
            patch.object(pipelines, "PIPELINES_DIR", self.pipes),
            patch.object(passes, "_run_frontmatters", side_effect=_fake_frontmatters),
            patch.object(events, "emit_ep_command", return_value="command-id"),
            patch.object(events, "emit"),
        ]
        for p in self.stack:
            p.start()
        _close_test_connection()

    def tearDown(self):
        for p in self.stack:
            p.stop()
        _close_test_connection()
        self.tmp.cleanup()

    def _ensure_auto_pass(self, slug: str) -> None:
        script = self.registry / slug
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        (self.registry / f"{slug}.yaml").write_text(
            f"slug: {slug}\nversion: 1\ndescription: {slug}\nenabled: true\n"
            "auto: true\ncapabilities: [audio, document]\nrequires: []\n"
            f"command: [{json.dumps(str(script))}, \"{{md_path}}\"]\n"
        )

    def _doc_export(self, *, folders: list | dict | str | None = None,
                    enabled: bool = True, version: int | None = None) -> dict:
        return {
            "name": "Wax Documents",
            "nodes": [
                _wrapper(trigger="documents", folders=folders or [str(self.docs)],
                         enabled=enabled, version=version),
                _typed_pass_node("Classify It", "alpha"),
                _pass_node("Enrich Beta", "beta"),
                {"name": "Notes", "type": "n8n-nodes-base.stickyNote",
                 "parameters": {"content": "gated vs continue"}, "position": [0, 0]},
            ],
            "connections": {
                # continue (ordering): Classify It -> Enrich Beta via output 0
                "Classify It": {"main": [[_link("Enrich Beta")]]},
            },
        }

    # ------------------------------------------------------------ compile ----

    def test_import_compiles_frozen_definition_with_node_identity(self):
        export = self._doc_export()
        # gated edge: Classify It's output 1 'requires' -> Enrich Beta
        export["connections"] = {"Classify It": {"main": [[], [_link("Enrich Beta")]]}}
        out = pipelines.import_n8n(_export(self.root, export))
        self.assertEqual(out["imported"], "wax-documents")
        self.assertEqual(out["workflow"], "Wax Documents")
        self.assertEqual(out["nodes"], 2)
        self.assertEqual(out["edges"], 1)  # the requires edge
        self.assertFalse(out.get("unchanged"))
        current = pipelines.compiled()["wax-documents"]
        self.assertEqual(current["source"], "n8n")
        self.assertEqual(current["source_ref"], "Wax Documents")
        # node identity preserved: the n8n node names live in the definition
        names = {n["slug"]: n.get("name") for n in current["definition"]["passes"]}
        self.assertEqual(names, {"alpha": "Classify It", "beta": "Enrich Beta"})
        # the frozen edge is stored, not re-derived
        self.assertEqual(current["definition"]["edges"],
                         [{"from": "alpha", "to": "beta", "kind": "requires"}])
        # import compiles the runnable definitions: registry command content
        alpha_frozen = next(n for n in current["definition"]["passes"]
                            if n["slug"] == "alpha")["registry"]
        self.assertIn("command", alpha_frozen)
        # the pipeline runs and its graph reflects the authored edges
        doc = self.docs / "note.md"
        doc.write_text("---\ntitle: T\n---\nBody.\n")
        run = pipelines.run_document("wax-documents", doc)
        self.assertEqual(run["completed"], 2)

    def test_no_connections_compiles_to_no_edges_never_registry_edges(self):
        # gamma REQUIRES alpha in the registry — an unconnected export of the
        # two must NOT inherit that edge: the author drew no wires.
        export = self._doc_export()
        export["nodes"] = [export["nodes"][0],
                           _typed_pass_node("Alpha", "alpha"),
                           _typed_pass_node("Gamma", "gamma")]
        export["connections"] = {}
        out = pipelines.import_n8n(_export(self.root, export))
        self.assertEqual(out["edges"], 0)
        definition = pipelines.compiled()["wax-documents"]["definition"]
        self.assertEqual(definition["edges"], [])
        # the frozen nodes carry the authored (empty) edges, not gamma's
        # registry requires — proven by a run: nothing is dependency-blocked
        doc = self.docs / "flat.md"
        doc.write_text("---\ntitle: Flat\n---\nBody.\n")
        run = pipelines.run_document("wax-documents", doc)
        self.assertEqual(run["failed"], 0)

    def test_yaml_mixed_edges_inherit_registry_only_where_unspecified(self):
        # One node authors its edges (alpha: explicitly NO after); the other
        # names neither key and must inherit the registry's (gamma requires
        # alpha), not silence it.
        (self.pipes / "mixed.yaml").write_text(json.dumps({
            "name": "mixed", "version": 1, "trigger": "manual", "enabled": True,
            "failure_policy": "independent",
            "passes": [
                {"slug": "alpha", "after": []},
                {"slug": "gamma"},
            ],
        }))
        out = pipelines.compile(["mixed"])
        self.assertEqual(out["failed"], {})
        edges = {(e["from"], e["to"], e["kind"])
                 for e in pipelines.compiled()["mixed"]["definition"]["edges"]}
        self.assertEqual(edges, {("alpha", "gamma", "requires")},
                         "gamma inherits its registry requires; alpha's "
                         "authored-empty after suppresses inheritance")

    def test_reimport_changed_export_is_a_new_definition(self):
        export = self._doc_export()
        path = _export(self.root, export)
        first = pipelines.import_n8n(path)
        # the authored graph changes in n8n: re-import adopts it explicitly
        export["nodes"].append(_pass_node("Diarize", "diarize"))
        second = pipelines.import_n8n(_export(self.root, export))
        self.assertNotEqual(first["definition_id"], second["definition_id"])
        # the frozen registry snapshot survives re-import (still runnable)
        self.assertEqual(pipelines.compiled()["wax-documents"]["definition"]["passes"][0]
                         ["registry"]["capabilities"], ["document"])

    def test_import_versions_increment_and_never_downgrade(self):
        export = self._doc_export(version=1)
        first = pipelines.import_n8n(_export(self.root, export))
        self.assertEqual(first["version"], 1)
        # an identical re-import is a stable no-op, not a silent bump
        again = pipelines.import_n8n(_export(self.root, export))
        self.assertTrue(again.get("unchanged"))
        self.assertEqual(again["definition_id"], first["definition_id"])
        # a changed export increments past the stored version
        export["nodes"].append(_pass_node("Diarize", "diarize"))
        second = pipelines.import_n8n(_export(self.root, export))
        self.assertEqual(second["version"], 2)
        # a higher authored version is honored
        export["nodes"].append(_typed_pass_node("Gamma", "gamma"))
        third = pipelines.import_n8n(_export(self.root, export))
        self.assertEqual(third["version"], 3)
        # a LOWER authored version never downgrades the stored one
        lower = self._doc_export(version=2)
        lower["nodes"].append(_pass_node("Diarize", "diarize"))
        lower["nodes"].append(_typed_pass_node("Gamma", "gamma"))
        fourth = pipelines.import_n8n(_export(self.root, lower))
        self.assertEqual(fourth["version"], 4)
        # a non-numeric version is rejected loudly
        bad = self._doc_export(version=1)
        bad["nodes"][0]["parameters"]["version"] = "two"
        with self.assertRaisesRegex(pipelines.PipelineError, "positive integer"):
            pipelines.import_n8n(_export(self.root, bad))

    # ------------------------------------------------------------ reject ----

    def test_import_rejects_unsupported_nodes_dynamic_and_structural_errors(self):
        # a native schedule trigger would make n8n a second scheduler
        bad = self._doc_export()
        bad["nodes"].append({"name": "Every 5m", "type": "n8n-nodes-base.scheduleTrigger",
                             "parameters": {}})
        with self.assertRaisesRegex(pipelines.PipelineError, "unsupported node type"):
            pipelines.import_n8n(_export(self.root, bad))

        # dynamic expressions are not a static graph
        dynamic = self._doc_export()
        dynamic["nodes"][2]["parameters"]["pass"] = "={{ $json.slug }}"
        with self.assertRaisesRegex(pipelines.PipelineError, "dynamic expression"):
            pipelines.import_n8n(_export(self.root, dynamic))

        # two wrappers: ambiguous graph identity
        two = self._doc_export()
        two["nodes"].append(_wrapper())
        with self.assertRaisesRegex(pipelines.PipelineError, "exactly one Wax Pipeline"):
            pipelines.import_n8n(_export(self.root, two))

        # the same pass on two nodes breaks node identity
        dup = self._doc_export()
        dup["nodes"].append(_pass_node("Again", "alpha"))
        with self.assertRaisesRegex(pipelines.PipelineError, "appears on two nodes"):
            pipelines.import_n8n(_export(self.root, dup))

        # unknown connection type
        weird = self._doc_export()
        weird["connections"] = {"Classify It": {
            "ai_languageModel": [[_link("Enrich Beta")]]}}
        with self.assertRaisesRegex(pipelines.PipelineError, "unsupported connection type"):
            pipelines.import_n8n(_export(self.root, weird))

        # a wire from a node that is not in the graph
        missing = self._doc_export()
        missing["connections"] = {"Ghost": {"main": [[_link("Enrich Beta")]]}}
        with self.assertRaisesRegex(pipelines.PipelineError, "unknown node"):
            pipelines.import_n8n(_export(self.root, missing))

        # a cycle through gated edges
        cycle = self._doc_export()
        cycle["connections"] = {
            "Classify It": {"main": [[], [_link("Enrich Beta")]]},
            "Enrich Beta": {"main": [[], [_link("Classify It")]]},
        }
        with self.assertRaisesRegex(pipelines.PipelineError, "cyclic"):
            pipelines.import_n8n(_export(self.root, cycle))

    def test_import_rejects_malformed_connections_and_bad_indexes(self):
        cases = {
            "kinds-not-object": {"Classify It": [_link("Enrich Beta")]},
            "outputs-not-list": {"Classify It": {"main": "Enrich Beta"}},
            "outputs-empty": {"Classify It": {"main": []}},
            "group-not-list": {"Classify It": {"main": [_link("Enrich Beta")]}},
            "link-not-object": {"Classify It": {"main": [["Enrich Beta"]]}},
            "link-type-not-main": {"Classify It": {"main": [
                [_link("Enrich Beta", type_="ai_tool")]]}},
            "source-output-index-too-high": {"Classify It": {"main": [
                [], [], [_link("Enrich Beta")]]}},
            "target-input-index-not-zero": {"Classify It": {"main": [
                [_link("Enrich Beta", index=1)]]}},
            "unknown-target": {"Classify It": {"main": [[_link("Nobody")]]}},
        }
        for label, connections in cases.items():
            export = self._doc_export()
            export["connections"] = connections
            with self.assertRaisesRegex(pipelines.PipelineError, ".", msg=label):
                pipelines.import_n8n(_export(self.root, export))

    def test_import_rejects_relative_and_malformed_folders(self):
        # relative BEFORE expansion: would silently resolve against cwd
        relative = self._doc_export(folders="Transcripts")
        with self.assertRaisesRegex(pipelines.PipelineError, "must be a list"):
            pipelines.import_n8n(_export(self.root, relative))
        relative = self._doc_export(folders=["Inbox/Wax"])
        with self.assertRaisesRegex(pipelines.PipelineError, "absolute"):
            pipelines.import_n8n(_export(self.root, relative))
        # the editor's fixedCollection export shape IS accepted
        fixed = self._doc_export(folders={"folderValues": [
            {"folder": str(self.docs)}]})
        out = pipelines.import_n8n(_export(self.root, fixed))
        definition = pipelines.compiled()["wax-documents"]["definition"]
        self.assertEqual(definition["folders"], [str(self.docs)])
        self.assertEqual(out["edges"], 1)

    def test_skip_when_validates_as_the_dict_the_runner_evaluates(self):
        good = self._doc_export()
        good["nodes"][2]["parameters"]["skipWhen"] = '{"field": "title", "equals": "X"}'
        pipelines.import_n8n(_export(self.root, good))
        conditions = next(n for n in pipelines.compiled()["wax-documents"]
                          ["definition"]["passes"] if n["slug"] == "beta")
        self.assertEqual(conditions["registry"]["skip_when"],
                         {"field": "title", "equals": "X"})
        # the runner consumes a DICT; a list (the `when` shape) is invalid here
        bad = self._doc_export()
        bad["nodes"][2]["parameters"]["skipWhen"] = '[{"field": "title", "exists": true}]'
        with self.assertRaisesRegex(pipelines.PipelineError, "skip_when"):
            pipelines.import_n8n(_export(self.root, bad))
        bad2 = self._doc_export()
        bad2["nodes"][2]["parameters"]["skipWhen"] = '{"field": "title"}'
        with self.assertRaisesRegex(pipelines.PipelineError, "skip_when"):
            pipelines.import_n8n(_export(self.root, bad2))

    def test_imported_conditions_run_and_skip(self):
        export = self._doc_export()
        export["nodes"][2]["parameters"]["skipWhen"] = '{"field": "title", "equals": "Skip Me"}'
        pipelines.import_n8n(_export(self.root, export))
        doc = self.docs / "skipped.md"
        doc.write_text("---\ntitle: Skip Me\n---\nBody.\n")
        run = pipelines.run_document("wax-documents", doc)
        beta_result = next(r for r in run["results"] if r["ep_slug"] == "beta")
        self.assertEqual(beta_result["state"], "skipped")
        self.assertEqual(beta_result["reason_code"], "condition_matched")
        # and the compiled graph surfaces the condition to discovery surfaces
        snap = catalog.snapshot()
        entry = next(p for p in snap["pipelines"]["pipelines"]
                     if p["name"] == "wax-documents")
        node = next(n for n in entry["nodes"] if n["slug"] == "beta")
        self.assertEqual(node["conditions"]["skip_when"],
                         {"field": "title", "equals": "Skip Me"})

    def test_import_rejects_missing_required_dependency_loudly(self):
        # author the requirement on beta's node parameter directly: a graph
        # whose requires edge names a pass that is not a node fails loudly
        doc = {"name": "dangling", "trigger": "manual", "enabled": True,
               "failure_policy": "independent",
               "passes": [{"slug": "beta", "requires": ["alpha"]}]}
        (self.pipes / "dangling.yaml").write_text(json.dumps(doc))
        out = pipelines.compile(["dangling"])
        self.assertIn("requires ['alpha'], which is not in this graph",
                      out["failed"]["dangling"])

    def test_transcription_import_requires_automatic_passes(self):
        # transcription pipelines become the audio plan authority: only
        # automatic, enabled passes may join them
        self._ensure_auto_pass("auto1")
        export = {
            "name": "Wax Transcription",
            "nodes": [
                _wrapper(trigger="transcription", enabled=True),
                _typed_pass_node("Auto One", "auto1"),
                _typed_pass_node("Manual Alpha", "alpha"),  # auto: false
            ],
            "connections": {},
        }
        with self.assertRaisesRegex(pipelines.PipelineError, "automatic, enabled"):
            pipelines.import_n8n(_export(self.root, export))
        # dropping the manual pass makes it a valid compiled view
        export["nodes"] = [n for n in export["nodes"]
                           if n.get("name") != "Manual Alpha"]
        out = pipelines.import_n8n(_export(self.root, export))
        self.assertEqual(out["trigger"], "transcription")

    def test_imported_edges_appear_in_the_catalog_graph(self):
        export = self._doc_export()
        export["connections"] = {
            "Classify It": {
                "main": [[_link("Enrich Beta")], [_link("Enrich Beta")]],
            },
        }
        pipelines.import_n8n(_export(self.root, export))
        snap = catalog.snapshot()
        entry = next(p for p in snap["pipelines"]["pipelines"]
                     if p["name"] == "wax-documents")
        kinds = {(e["from"], e["to"], e["kind"]) for e in entry["edges"]}
        self.assertIn(("alpha", "beta", "requires"), kinds)
        self.assertIn(("alpha", "beta", "after"), kinds)
        # authored node names are visible to discovery surfaces
        names = {n["slug"]: n.get("name") for n in entry["nodes"]}
        self.assertEqual(names.get("alpha"), "Classify It")

    # ------------------------------------------------- the shipped examples ----

    def test_both_shipped_example_workflows_import_against_a_real_registry(self):
        exports = sorted(EXAMPLES.glob("*.json"))
        self.assertGreaterEqual(len(exports), 2,
                                "the adapter ships example workflows")
        all_kinds: set[str] = set()
        for export in exports:
            with self.subTest(workflow=export.name):
                doc = json.loads(export.read_text())
                # synthesize registry entries for every pass the example names
                # (the fixture registry does not carry the production passes)
                for node in doc.get("nodes", []):
                    ntype = str(node.get("type") or "")
                    slug = None
                    if ntype.startswith("n8n-nodes-heyma.wax-pass-"):
                        slug = ntype[len("n8n-nodes-heyma.wax-pass-"):]
                    elif ntype == "n8n-nodes-heyma.waxEp":
                        slug = (node.get("parameters") or {}).get("pass")
                    if slug and not (self.registry / f"{slug}.yaml").exists():
                        self._ensure_auto_pass(slug)
                out = pipelines.import_n8n(export, name=f"example-{export.stem}")
                self.assertGreaterEqual(out["nodes"], 2)
                definition = pipelines.compiled()[f"example-{export.stem}"]["definition"]
                kinds = {e["kind"] for e in definition["edges"]}
                self.assertIn("requires", kinds,
                              "each example demonstrates a gated edge")
                all_kinds.update(kinds)
        self.assertIn("after", all_kinds,
                      "the examples together demonstrate continue edges")


if __name__ == "__main__":
    unittest.main()
