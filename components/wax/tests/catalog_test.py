"""Safe discovery: the catalog exposes purposes and availability, never secrets."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import catalog, events, ledger, passes, paths, pipelines, sentinel


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


if __name__ == "__main__":
    unittest.main()
