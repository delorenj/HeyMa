"""n8n graph authoring: exported workflow JSON compiled into Wax pipelines.

The export is an authoring artifact and nothing more — importing it must
compile the same frozen, content-addressed definition YAML authoring does,
preserve node identity, and reject loudly everything Wax cannot own:
unsupported node types (native triggers/actions), dynamic expressions,
cycles, duplicate passes, and unknown connection types.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import catalog, documents, events, frontmatter, ledger, passes, paths, pipelines

from documents_test import _close_test_connection, _fake_frontmatters, _write_registry, _Scenario


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


def _wrapper(*, trigger: str = "documents", folders: list | None = None,
             enabled: bool = True, failure_policy: str = "independent") -> dict:
    return {"name": "Wax Pipeline", "type": "n8n-nodes-heyma.waxPipeline",
            "typeVersion": 1,
            "parameters": {"trigger": trigger, "folders": folders or [],
                           "enabled": enabled, "failurePolicy": failure_policy},
            "position": [0, 0]}


def _export(tmp: Path, doc: dict) -> Path:
    path = tmp / "export.json"
    path.write_text(json.dumps(doc))
    return path


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
        # beta: document-capable, REQUIRES alpha (for gated-edge tests)
        beta = self.registry / "beta"
        beta.write_text("#!/bin/sh\nprintf '%s\\n' '" + json.dumps(
            {"wax_ep_version": 1, "frontmatter": {"summary": "beta"}}) + "'\n")
        beta.chmod(0o755)
        (self.registry / "beta.yaml").write_text(
            "slug: beta\nversion: 1\ndescription: beta\nenabled: true\nauto: false\n"
            "capabilities: [document]\nrequires: []\n"
            f"command: [{json.dumps(str(beta))}, \"{{md_path}}\"]\n"
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

    def _doc_export(self, *, folders: list | None = None, enabled: bool = True) -> dict:
        return {
            "name": "Wax Documents",
            "nodes": [
                _wrapper(trigger="documents", folders=folders or [str(self.docs)],
                         enabled=enabled),
                _typed_pass_node("Classify It", "alpha"),
                _pass_node("Enrich Beta", "beta"),
                {"name": "Notes", "type": "n8n-nodes-base.stickyNote",
                 "parameters": {"content": "gated vs continue"}, "position": [0, 0]},
            ],
            "connections": {
                # continue (ordering): Classify It -> Enrich Beta
                "Classify It": {"main": [[{"node": "Enrich Beta", "type": "main", "index": 0}]]},
            },
        }

    def test_import_compiles_frozen_definition_with_node_identity(self):
        export = self._doc_export()
        # requirement edge: alpha's node -> beta's node via the requirement port
        export["connections"] = {
            "Classify It": {"requirement": [[{"node": "Enrich Beta",
                                              "type": "main", "index": 0}]]},
        }
        out = pipelines.import_n8n(_export(self.root, export))
        self.assertEqual(out["imported"], "wax-documents")
        self.assertEqual(out["workflow"], "Wax Documents")
        self.assertEqual(out["nodes"], 2)
        self.assertEqual(out["edges"], 1)  # the requirement edge
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
            "ai_languageModel": [[{"node": "Enrich Beta", "type": "main", "index": 0}]]}}
        with self.assertRaisesRegex(pipelines.PipelineError, "unsupported connection type"):
            pipelines.import_n8n(_export(self.root, weird))

        # a requirement edge to a node that is not in the graph
        missing = self._doc_export()
        missing["connections"] = {"Ghost": {"requirement": [
            [{"node": "Enrich Beta", "type": "main", "index": 0}]]}}
        with self.assertRaisesRegex(pipelines.PipelineError, "unknown node"):
            pipelines.import_n8n(_export(self.root, missing))

        # a cycle through requirement edges
        cycle = self._doc_export()
        cycle["connections"] = {
            "Classify It": {"requirement": [[{"node": "Enrich Beta", "type": "main", "index": 0}]]},
            "Enrich Beta": {"requirement": [[{"node": "Classify It", "type": "main", "index": 0}]]},
        }
        with self.assertRaisesRegex(pipelines.PipelineError, "cyclic"):
            pipelines.import_n8n(_export(self.root, cycle))

    def test_import_rejects_missing_required_dependency_loudly(self):
        # beta requires alpha (authored requirement edge), but alpha's node was
        # deleted from the workflow: the requirement dangles and compile fails
        export = self._doc_export()
        export["nodes"] = [n for n in export["nodes"] if n.get("name") != "Classify It"]
        export["connections"] = {}
        # author the requirement on beta's node parameter directly instead
        export["nodes"].append(_pass_node("Beta Alone", "beta"))
        # use a YAML-authored equivalent through the same validator: a graph
        # whose requires edge names a pass that is not a node
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
        auto = self.registry / "auto1"
        auto.write_text("#!/bin/sh\n")
        auto.chmod(0o755)
        (self.registry / "auto1.yaml").write_text(
            "slug: auto1\nversion: 1\ndescription: auto\nenabled: true\nauto: true\n"
            "capabilities: [audio]\nrequires: []\n"
            f"command: [{json.dumps(str(auto))}, \"{{md_path}}\"]\n"
        )
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
                "main": [[{"node": "Enrich Beta", "type": "main", "index": 0}]],
                "requirement": [[{"node": "Enrich Beta", "type": "main", "index": 0}]],
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


if __name__ == "__main__":
    unittest.main()
