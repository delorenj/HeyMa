"""The authored pipelines.d YAML must name the same passes as the release exports the live pipelines were
imported from. `wax pipeline compile` (no arguments) recompiles every authored YAML; one that had
drifted from the export would silently drop an enabled pass (project-extraction, enabled 2026-10-29) from
the plan authority. Found in the adversarial review of the Holocene Transcripts stage."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from wax import events, passes, paths, pipelines
from documents_test import _close_test_connection

COMPONENT_ROOT = next(parent for parent in Path(__file__).resolve().parents
                      if (parent / "pyproject.toml").is_file())
PASSES_DIR = COMPONENT_ROOT / "config" / "passes.d"
PIPELINES_DIR = COMPONENT_ROOT / "config" / "pipelines.d"
EXAMPLES = COMPONENT_ROOT.parents[1] / "components" / "n8n-nodes-heyma" / "examples"


def slug(entry) -> str:
    return entry if isinstance(entry, str) else entry["slug"]


class AuthoredPipelineParityTest(unittest.TestCase):
    def test_authored_yaml_names_the_passes_the_release_exports_run(self):
        for name in ("transcript-standard", "doc-standard"):
            with self.subTest(pipeline=name), tempfile.TemporaryDirectory() as directory:
                runtime = Path(directory)
                stack = [patch.object(paths, "DB", runtime / "wax.db"), patch.object(paths, "VAR", runtime),
                         patch.object(passes, "REGISTRY_DIR", PASSES_DIR),
                         patch.object(pipelines, "PIPELINES_DIR", PIPELINES_DIR), patch.object(events, "emit")]
                for item in stack:
                    item.start()
                _close_test_connection()
                try:
                    pipelines.import_n8n(EXAMPLES / f"{name}-release.json", name=name)
                    compiled = {n["slug"] for n in pipelines.compiled()[name]["definition"]["passes"]}
                finally:
                    _close_test_connection()
                    for item in reversed(stack):
                        item.stop()
                authored = {slug(e) for e in yaml.safe_load((PIPELINES_DIR / f"{name}.yaml").read_text())["passes"]}
                self.assertEqual(authored, compiled)
                self.assertIn("project-extraction", authored)


if __name__ == "__main__":
    unittest.main()
