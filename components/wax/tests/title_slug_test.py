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
                "hosted_enrichment",
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
            with patch.object(TITLE_SLUG, "hosted_enrichment") as model:
                result = TITLE_SLUG.build_result(md)
            model.assert_not_called()
            self.assertEqual(result["frontmatter"]["title"], "Human Title")
            self.assertEqual(result["transcript"]["slug"], "human-title")

    def test_hosted_enrichment_caps_provider_output_tokens(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"title":"Bounded Provider Output",'
                            '"summary":"The response budget is deliberately bounded."}'
                        )
                    }
                }
            ]
        }
        with (
            patch.object(TITLE_SLUG, "preflight_model"),
            patch.object(TITLE_SLUG, "_api_request", return_value=response) as request,
        ):
            result = TITLE_SLUG.hosted_enrichment(
                "A transcript about keeping provider output budgets finite.",
                model="example/model",
                api_base="https://provider.example/v1",
                api_key="test-key",
                timeout=10,
            )

        self.assertEqual(result["title"], "Bounded Provider Output")
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["max_tokens"], TITLE_SLUG.MAX_OUTPUT_TOKENS)
        self.assertLessEqual(payload["max_tokens"], 2_048)


if __name__ == "__main__":
    unittest.main()
