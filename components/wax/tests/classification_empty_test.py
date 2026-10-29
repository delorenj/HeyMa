"""An empty document has nothing to classify: a skip, never a failure."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
SCRIPT = COMPONENT_ROOT / "config" / "passes.d" / "bin" / "classification"
LOADER = SourceFileLoader("wax_classification_pass", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
CLASSIFICATION = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(CLASSIFICATION)

SKIP = {"wax_ep_version": 1, "state": "skipped", "reason_code": "empty_document"}


class ClassificationEmptyTest(unittest.TestCase):
    def note(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        md = Path(directory.name) / "note.md"
        md.write_text(text)
        return md

    def test_empty_bodies_skip_without_a_model_call(self):
        for text in ("", "\n  \n", "---\ntitle: t\n---\n", "---\ntitle: t\n---\n\n   \n"):
            with self.subTest(text=text), patch.object(CLASSIFICATION.jev, "decide") as decide, \
                    patch.object(CLASSIFICATION.provider, "resolve_key") as key:
                self.assertEqual(CLASSIFICATION.build_result(self.note(text)), SKIP)
            decide.assert_not_called()
            key.assert_not_called()

    def test_existing_classification_still_preserved_on_a_real_body(self):
        result = CLASSIFICATION.build_result(self.note("---\nclassification: meeting\n---\nHello.\n"))
        self.assertEqual(result, {"wax_ep_version": 1, "frontmatter": {"classification": "meeting"}})

    def test_executable_exits_zero_with_the_skip(self):
        done = subprocess.run([str(SCRIPT), str(self.note("---\ntitle: t\n---\n"))],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), SKIP)


if __name__ == "__main__":
    unittest.main()
