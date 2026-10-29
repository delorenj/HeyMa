"""Operator order, 2026-10-27: a transcript classified as a monolog is not diarized.

The gate lives in the diarization registry entry (`when`), ordered after
classification by an `after` edge, and must survive into what actually runs:
the compiled transcript-standard imported from its n8n release export. A
missing classification (failed, skipped, unknown) still diarizes.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import events, passes, paths, pipelines
from documents_test import _close_test_connection

COMPONENT_ROOT = next(parent for parent in Path(__file__).resolve().parents
                      if (parent / "pyproject.toml").is_file())
PASSES_DIR = COMPONENT_ROOT / "config" / "passes.d"
PIPELINES_DIR = COMPONENT_ROOT / "config" / "pipelines.d"
RELEASE = (COMPONENT_ROOT.parents[1] / "components" / "n8n-nodes-heyma" / "examples"
           / "transcript-standard-release.json")


def runs_for(when: list, fm: dict) -> bool:
    return all(passes._clause_failure(clause, fm) is None for clause in when or [])


class DiarizationGateTest(unittest.TestCase):
    def assert_gate(self, when):
        self.assertFalse(runs_for(when, {"classification": "monolog"}), "a monolog was diarized")
        self.assertTrue(runs_for(when, {"classification": "meeting"}))
        self.assertTrue(runs_for(when, {"classification": "other"}))
        self.assertTrue(runs_for(when, {"classification": "phone_call"}), "a call leg is multi-party")
        self.assertTrue(runs_for(when, {"classification-detail": "unsorted"}), "an abstain must still diarize")
        self.assertTrue(runs_for(when, {}), "a failed/absent classification must still diarize")

    def test_registry_skips_monologs_after_classification(self):
        with patch.object(passes, "REGISTRY_DIR", PASSES_DIR):
            ep = passes.registry()["diarization"]
        self.assert_gate(ep.get("when"))
        self.assertIn("classification", ep.get("after") or [])
        self.assertNotIn("classification", ep.get("requires") or [],
                         "requires would block diarization whenever classification fails")

    def test_compiled_transcript_standard_freezes_the_gate_and_the_order(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            stack = [patch.object(paths, "DB", runtime / "wax.db"), patch.object(paths, "VAR", runtime),
                     patch.object(passes, "REGISTRY_DIR", PASSES_DIR),
                     patch.object(pipelines, "PIPELINES_DIR", PIPELINES_DIR),
                     patch.object(events, "emit")]
            for item in stack:
                item.start()
            _close_test_connection()
            try:
                pipelines.import_n8n(RELEASE, name="transcript-standard")
                definition = pipelines.compiled()["transcript-standard"]["definition"]
            finally:
                _close_test_connection()
                for item in reversed(stack):
                    item.stop()
        node = next(n for n in definition["passes"] if n["slug"] == "diarization")
        self.assert_gate(node["registry"].get("when"))
        self.assertIn({"from": "classification", "to": "diarization", "kind": "after"},
                      definition.get("edges") or [])


if __name__ == "__main__":
    unittest.main()
