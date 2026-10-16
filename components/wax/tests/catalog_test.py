"""Safe discovery: the catalog exposes purposes and availability, never secrets."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import catalog, documents, events, ledger, passes, paths, pipelines, sentinel


def _close_test_connection() -> None:
    connection = getattr(ledger._local, "conn", None)
    if connection is not None:
        connection.close()
        del ledger._local.conn


def _write_registry(directory: Path) -> None:
    (directory / "alpha.yaml").write_text(
        "slug: alpha\nversion: 1\ndescription: Enriches the title.\n"
        "enabled: true\nauto: true\ncapabilities: [document]\n"
        "requires: []\nafter: []\nemits: [intake.detected]\n"
        "when: [{field: classification, exists: true}]\n"
        "command: [\"/bin/true\", \"{md_path}\"]\n"
        "env:\n  SECRET_TOKEN: op://DeLoSecrets/item/credential\n"
    )
    (directory / "beta.yaml").write_text(
        "slug: beta\nversion: 2\ndescription: TODO — placeholder registered by Wax.\n"
        "enabled: false\ncapabilities: [audio]\n"
        "requires: [alpha]\n"
        "command: [\"/bin/true\", \"{md_path}\"]\n"
    )
    (directory / "broken.yaml").write_text(
        "slug: broken\nversion: 1\ndescription: Depends on nothing that exists.\n"
        "enabled: true\nrequires: [missing-pass]\n"
        "command: [\"/bin/true\", \"{md_path}\"]\n"
    )


class CatalogSafetyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = self.root / "runtime"
        self.registry = self.root / "passes.d"
        self.pipes = self.root / "pipelines.d"
        self.runtime.mkdir()
        self.registry.mkdir()
        self.pipes.mkdir()
        _write_registry(self.registry)

    def tearDown(self):
        _close_test_connection()
        self._tmp.cleanup()

    def _catalog(self):
        with patch.object(paths, "DB", self.runtime / "wax.db"), \
                patch.object(paths, "VAR", self.runtime), \
                patch.object(passes, "REGISTRY_DIR", self.registry), \
                patch.object(pipelines, "PIPELINES_DIR", self.pipes):
            _close_test_connection()
            return catalog.snapshot()

    def test_every_registered_pass_is_visible_with_purpose_and_availability(self):
        snap = self._catalog()
        rows = {row["slug"]: row for row in snap["passes"]["passes"]}
        self.assertEqual(set(rows), {"alpha", "beta", "broken"})
        self.assertEqual(rows["alpha"]["description"], "Enriches the title.")
        self.assertEqual(rows["alpha"]["availability"]["state"], "available")
        self.assertEqual(rows["beta"]["availability"]["state"], "placeholder")
        self.assertIn("not implemented", rows["beta"]["availability"]["reason"])
        # broken requires a pass that does not exist -> quarantined, visible
        self.assertEqual(rows["broken"]["availability"]["state"], "quarantined")
        self.assertIn("missing-pass", rows["broken"]["quarantined"])

    def test_conditions_edges_and_capabilities_are_explicit(self):
        snap = self._catalog()
        rows = {row["slug"]: row for row in snap["passes"]["passes"]}
        self.assertEqual(rows["alpha"]["capabilities"], ["document"])
        self.assertEqual(rows["beta"]["capabilities"], ["audio"])
        self.assertEqual(rows["alpha"]["conditions"],
                         {"when": [{"field": "classification", "exists": True}]})
        self.assertEqual(rows["beta"]["requires"], ["alpha"])
        self.assertEqual(rows["alpha"]["after"], [])
        self.assertEqual(rows["alpha"]["emits"], ["intake.detected"])

    def test_discovery_leaks_no_env_command_bodies_or_stderr(self):
        snap = self._catalog()
        encoded = json.dumps(snap)
        for banned in ("SECRET_TOKEN", "op://DeLoSecrets", "/bin/true", "command",
                       "env", "stderr", "detail"):
            self.assertNotIn(banned, encoded, f"catalog must never contain {banned!r}")

    def test_recent_run_receipts_carry_reason_codes_never_error_prose(self):
        folder = self.root / "inbox"
        folder.mkdir()
        (self.pipes / "dgraph.yaml").write_text(
            "name: dgraph\nversion: 1\ndescription: documents graph\n"
            "trigger: documents\nenabled: true\n"
            f"folders: [{json.dumps(str(folder))}]\n"
            "failure_policy: independent\npasses: [alpha]\n"
        )
        with patch.object(paths, "DB", self.runtime / "wax.db"), \
                patch.object(paths, "VAR", self.runtime), \
                patch.object(passes, "REGISTRY_DIR", self.registry), \
                patch.object(pipelines, "PIPELINES_DIR", self.pipes):
            _close_test_connection()
            compiled = pipelines.compile(["dgraph"])
            definition_id = pipelines.compiled()["dgraph"]["definition_id"]
        self.assertFalse(compiled["failed"], compiled["failed"])
        # A failed, exhausted receipt whose stored detail embeds a secret (as
        # real exception text and stderr tails do). The catalog may carry the
        # reason CODE; the prose must stay in the operator ledger.
        now = sentinel.utcnow()
        conn = ledger.connect()
        conn.execute(
            "INSERT INTO documents(binding_id,doc_id,path,sha256,current_sha256,bytes,"
            "origin,state,attempts,first_seen,updated_at) VALUES"
            "('b','doc-0123456789abcdef',?, 'x','x',1,'test','failed',3,?,?)",
            (str(folder / "note.md"), now, now))
        conn.execute(
            "INSERT INTO document_receipts(binding_id,doc_id,pipeline,definition_id,"
            "state,attempts,last_attempt_at,exhausted_at,last_error,reason_code) "
            "VALUES('b','doc-0123456789abcdef','dgraph',?,'failed',3,?,?,?,?)",
            (definition_id, now, now,
             "provider said: Authorization Bearer sk-live-LEAKME-123", "provider_down"))
        snap = self._catalog()
        entry = next(p for p in snap["pipelines"]["pipelines"] if p["name"] == "dgraph")
        run = entry["recent_runs"][0]
        self.assertEqual(run["state"], "failed")
        self.assertEqual(run["reason_code"], "provider_down")
        self.assertNotIn("error", run, "recent_runs must not carry raw error text")
        encoded = json.dumps(snap)
        self.assertNotIn("sk-live-LEAKME-123", encoded)
        self.assertNotIn("last_error", encoded)

    def test_pipeline_drift_and_outcomes_are_truthful(self):
        (self.pipes / "graph.yaml").write_text(
            "name: graph\nversion: 1\ndescription: one graph\ntrigger: manual\n"
            "enabled: true\nfailure_policy: independent\npasses: [alpha, beta]\n"
        )
        snap = self._catalog()
        graph = next(p for p in snap["pipelines"]["pipelines"] if p["name"] == "graph")
        self.assertEqual(graph["drift"]["state"], "uncompiled")
        self.assertIsNone(graph.get("nodes"))  # nothing compiled, nothing claimed active
        with patch.object(paths, "DB", self.runtime / "wax.db"), \
                patch.object(paths, "VAR", self.runtime), \
                patch.object(passes, "REGISTRY_DIR", self.registry), \
                patch.object(pipelines, "PIPELINES_DIR", self.pipes):
            _close_test_connection()
            result = pipelines.compile(["graph"])
            self.assertFalse(result["failed"], result["failed"])
        snap = self._catalog()
        graph = next(p for p in snap["pipelines"]["pipelines"] if p["name"] == "graph")
        self.assertEqual(graph["drift"]["state"], "in_sync")
        self.assertEqual({n["slug"] for n in graph["nodes"]}, {"alpha", "beta"})
        # beta is a placeholder in the registry; the node says so.
        beta = next(n for n in graph["nodes"] if n["slug"] == "beta")
        self.assertEqual(beta["availability"]["state"], "placeholder")
        # manual/documents scenario marks audio-only passes inapplicable
        self.assertFalse(beta["applicable"])
        # the requires edge alpha->beta is part of the graph
        self.assertEqual(graph["edges"],
                         [{"from": "alpha", "to": "beta", "kind": "requires"}])
        # drift appears the moment the authored file changes
        (self.pipes / "graph.yaml").write_text(
            "name: graph\nversion: 2\ndescription: one graph\ntrigger: manual\n"
            "enabled: true\nfailure_policy: independent\npasses: [alpha, beta]\n"
        )
        snap = self._catalog()
        graph = next(p for p in snap["pipelines"]["pipelines"] if p["name"] == "graph")
        self.assertEqual(graph["drift"]["state"], "drifted")
        self.assertIn("compile", graph["drift"]["detail"])

    def test_pipeline_outcomes_are_scoped_and_survive_registry_bumps(self):
        # two pipelines sharing the SAME pass slug, over different folders:
        # one pipeline's runs must never inflate the other's outcome counts
        for name, folder in (("doc-a", "a"), ("doc-b", "b")):
            (self.pipes / f"{name}.yaml").write_text(
                f"name: {name}\nversion: 1\ndescription: {name}\ntrigger: documents\n"
                f"enabled: true\nfolders: [{json.dumps(str(self.root / folder))}]\n"
                "failure_policy: independent\npasses: [alpha]\n"
            )
        with patch.object(paths, "DB", self.runtime / "wax.db"), \
                patch.object(paths, "VAR", self.runtime), \
                patch.object(passes, "REGISTRY_DIR", self.registry), \
                patch.object(pipelines, "PIPELINES_DIR", self.pipes):
            _close_test_connection()
            result = pipelines.compile(["doc-a", "doc-b"])
            self.assertFalse(result["failed"], result["failed"])
            conn = ledger.connect()
            now = sentinel.utcnow()
            # doc-a ran TWO documents to completion; doc-b failed one — one
            # pass row per (item, slug), like the real ledger
            runs = (("doc-a", "a", ("aaaaaaaaaaaaaaaa", "cccccccccccccccc"), "completed"),
                    ("doc-b", "b", ("bbbbbbbbbbbbbbbb",), "failed"))
            for name, folder, docs, state in runs:
                definition_id = pipelines.compiled()[name]["definition_id"]
                binding = f"{name}-{folder}-000000000000"
                for doc in docs:
                    # the receipt is what ties a document run to ITS pipeline
                    conn.execute(
                        "INSERT INTO document_receipts(binding_id,doc_id,pipeline,definition_id,"
                        "state,attempts,last_attempt_at) VALUES(?,?,?,?,'completed',1,?)",
                        (binding, f"doc-{doc}", name, definition_id, now))
                    # and the pass ledger row under the binding-scoped item key
                    key = documents.doc_key(binding, f"doc-{doc}")
                    conn.execute(
                        "INSERT INTO passes(item_id,ep_slug,version,state,attempt,updated_at) "
                        "VALUES(?,?,1,?,1,?)", (key, "alpha", state, now))
        snap = self._catalog()
        by_name = {p["name"]: p for p in snap["pipelines"]["pipelines"]}
        self.assertEqual(by_name["doc-a"]["outcomes"]["alpha"]["completed"], 2)
        self.assertEqual(by_name["doc-a"]["outcomes"]["alpha"]["failed"], 0)
        self.assertEqual(by_name["doc-b"]["outcomes"]["alpha"]["failed"], 1)
        self.assertEqual(by_name["doc-b"]["outcomes"]["alpha"]["completed"], 0,
                         "docB must not claim docA's runs through the shared slug")
        self.assertEqual(by_name["doc-a"]["outcomes_scope"], "pipeline")

        # the LIVE registry bumps alpha to v2 with no recompile: the compiled
        # v1 counts are this pipeline's history and must NOT vanish into stale
        (self.registry / "alpha.yaml").write_text(
            "slug: alpha\nversion: 2\ndescription: Enriches the title.\n"
            "enabled: true\nauto: true\ncapabilities: [document]\n"
            "requires: []\nafter: []\n"
            "when: [{field: classification, exists: true}]\n"
            "command: [\"/bin/true\", \"{md_path}\"]\n"
        )
        snap = self._catalog()
        docA = next(p for p in snap["pipelines"]["pipelines"] if p["name"] == "doc-a")
        self.assertEqual(docA["outcomes"]["alpha"]["completed"], 2,
                         "compiled-version counts survive a live registry bump")
        self.assertEqual(docA["outcomes"]["alpha"]["stale"], 0)

    def test_n8n_sourced_drift_is_an_exported_snapshot_never_in_sync(self):
        export = {
            "name": "Wax Flow",
            "nodes": [
                {"name": "Wax Pipeline", "type": "n8n-nodes-heyma.waxPipeline",
                 "typeVersion": 1, "position": [0, 0],
                 "parameters": {"trigger": "documents",
                                "folders": [str(self.root / "none")],
                                "enabled": True, "failurePolicy": "independent"}},
                {"name": "A", "type": "n8n-nodes-heyma.waxEp", "typeVersion": 1,
                 "position": [0, 0], "parameters": {"pass": "alpha"}},
            ],
            "connections": {},
        }
        path = self.root / "flow.json"
        path.write_text(json.dumps(export))
        with patch.object(paths, "DB", self.runtime / "wax.db"), \
                patch.object(paths, "VAR", self.runtime), \
                patch.object(passes, "REGISTRY_DIR", self.registry), \
                patch.object(pipelines, "PIPELINES_DIR", self.pipes):
            _close_test_connection()
            pipelines.import_n8n(path)
        snap = self._catalog()
        flow = next(p for p in snap["pipelines"]["pipelines"] if p["name"] == "wax-flow")
        # Wax cannot read the authored artifact inside n8n, so it cannot KNOW
        # the graph is still in sync — it must never claim to
        self.assertEqual(flow["drift"]["state"], "exported_snapshot")
        self.assertEqual(flow["drift"]["detail"], "")
        self.assertIsNone(flow["drift"].get("authored_id"))
        self.assertNotIn("in_sync", json.dumps(flow["drift"]))


if __name__ == "__main__":
    unittest.main()
