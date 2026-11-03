"""The base-taxonomy schema is a doctor row, not only a per-run journal warning.

2026-11-03: every title-slug run logged "frontmatter schema missing, skipping
base stamp: ~/d/_vault/Settings/frontmatter-category-map.json". `_vault/` had
left DeLoDocs for ~/code/obsops a month earlier, and since the stamp is
best-effort nothing failed. Compiled pipelines freeze the path, so the probe
reads them as well as the live registry.
"""

import importlib.util
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from wax import passes, pipelines

COMPONENT = Path(__file__).resolve().parents[1]


def load_cli():
    loader = SourceFileLoader("wax_cli_schema", str(COMPONENT / "bin" / "wax"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class SchemaPathTest(unittest.TestCase):
    def test_none_without_a_schema_and_home_is_expanded(self):
        self.assertIsNone(passes.frontmatter_schema_path({"slug": "x"}))
        path = passes.frontmatter_schema_path({"frontmatter_schema": "{home}/a/b.json"})
        self.assertEqual(path, Path.home() / "a" / "b.json")

    def test_title_slug_points_at_the_vault_ops_schema(self):
        ep = passes.registry()["title-slug"]
        path = passes.frontmatter_schema_path(ep)
        self.assertEqual(path, Path.home() / "code/obsops/Settings/frontmatter-category-map.json")
        self.assertNotIn("_vault", str(path), "the retired DeLoDocs/_vault location")


class DoctorProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = load_cli()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.present = Path(self.tmp.name) / "map.json"
        self.present.write_text("{}")
        self.absent = Path(self.tmp.name) / "gone.json"

    def tearDown(self):
        self.tmp.cleanup()

    def probe(self, registry_schema: Path, frozen_schema: Path):
        registry = {"title-slug": {"slug": "title-slug", "enabled": True,
                                   "frontmatter_schema": str(registry_schema)},
                    "off": {"slug": "off", "enabled": False, "frontmatter_schema": str(self.absent)}}
        compiled = {"transcript-standard": {"definition": {"passes": [
            {"slug": "title-slug", "registry": {"frontmatter_schema": str(frozen_schema)}}]}}}
        with patch.object(passes, "registry", return_value=registry), \
                patch.object(pipelines, "compiled", return_value=compiled):
            return self.cli.probe_frontmatter_schemas()

    def test_present_everywhere_passes(self):
        row = self.probe(self.present, self.present)
        self.assertEqual(row["status"], "pass", row)

    def test_a_stale_frozen_path_warns_even_when_the_yaml_is_fixed(self):
        row = self.probe(self.present, self.absent)
        self.assertEqual(row["status"], "warn", row)
        self.assertIn(str(self.absent), row["detail"])
        self.assertIn("title-slug (transcript-standard)", row["detail"])
        self.assertIn("wax pipeline", row["fix"])

    def test_a_disabled_pass_is_not_reported(self):
        row = self.probe(self.present, self.present)
        self.assertNotIn(str(self.absent), row["detail"])


if __name__ == "__main__":
    unittest.main()
