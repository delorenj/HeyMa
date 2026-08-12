import importlib.util
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
SCRIPT = COMPONENT_ROOT / "config" / "passes.d" / "bin" / "title-slug"
LOADER = SourceFileLoader("wax_title_slug_pass", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
TITLE_SLUG = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(TITLE_SLUG)


class TitleSlugPassTest(unittest.TestCase):
    def test_builds_declarative_result_without_editing_note(self):
        with tempfile.TemporaryDirectory() as directory:
            md = Path(directory) / "recording.md"
            original = "---\nlanguage: en\n---\n# Transcript\nDiscussing modular enrichment passes.\n"
            md.write_text(original)
            with patch.object(
                TITLE_SLUG,
                "ollama_enrichment",
                return_value={
                    "title": "Modular Transcript Enrichment",
                    "summary": "The speaker defines a modular pass architecture.",
                },
            ):
                result = TITLE_SLUG.build_result(md)
            self.assertEqual(result["wax_ep_version"], 1)
            self.assertEqual(result["transcript"]["slug"], "modular-transcript-enrichment")
            self.assertEqual(md.read_text(), original)

    def test_existing_grounded_title_and_summary_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            md = Path(directory) / "recording.md"
            md.write_text(
                "---\ntitle: Human Title\nsummary: Human summary.\n"
                "title-slug: human-title\n---\nBody\n"
            )
            with patch.object(TITLE_SLUG, "ollama_enrichment") as model:
                result = TITLE_SLUG.build_result(md)
            model.assert_not_called()
            self.assertEqual(result["frontmatter"]["title"], "Human Title")
            self.assertEqual(result["transcript"]["slug"], "human-title")


if __name__ == "__main__":
    unittest.main()
