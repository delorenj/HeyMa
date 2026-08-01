import importlib.util
import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[3]


def load_script(name):
    path = REPO_ROOT / ".mise" / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SkillProjectRootTest(unittest.TestCase):
    def from_nested_cwd(self, function):
        previous = Path.cwd()
        try:
            os.chdir(REPO_ROOT / "components" / "wax" / "assets")
            return function()
        finally:
            os.chdir(previous)

    def test_provision_root_is_independent_of_cwd(self):
        module = load_script("provision-bmad-skills.py")
        self.assertEqual(self.from_nested_cwd(module.project_root), REPO_ROOT)

    def test_sync_root_is_independent_of_cwd(self):
        module = load_script("sync-skills.py")
        self.assertEqual(self.from_nested_cwd(module.resolve_project_root), REPO_ROOT)

    def test_explicit_root_must_be_common_project(self):
        module = load_script("sync-skills.py")
        with self.assertRaises(ValueError):
            module.resolve_project_root("/tmp")

    def test_provision_bootstraps_registry_cache_without_source_checkout(self):
        module = load_script("provision-bmad-skills.py")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)

            def clone(command, check):
                self.assertEqual(command[:4], ["git", "clone", "--depth", "1"])
                registry = Path(command[-1])
                pack = registry / "packs" / "bmad" / module.BMAD_PACK_VERSION
                (pack / "bmad-example").mkdir(parents=True)

            with patch.dict(os.environ, {}, clear=True), patch.object(
                module.Path, "home", return_value=home
            ), patch.object(module.subprocess, "run", side_effect=clone) as run:
                root = module.pack_root()
                expected = module.registry_cache_root() / "packs" / "bmad" / module.BMAD_PACK_VERSION
            self.assertEqual(root, expected)
            run.assert_called_once()

    def test_registry_path_rejects_absolute_path(self):
        module = load_script("sync-skills.py")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "must be relative"):
                module.resolve_registry_path(Path(directory), "/etc/passwd")

    def test_registry_path_rejects_parent_traversal(self):
        module = load_script("sync-skills.py")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "escapes registry root"):
                module.resolve_registry_path(Path(directory), "../../etc/passwd")

    def test_provision_rejects_pack_root_symlink_escape(self):
        module = load_script("provision-bmad-skills.py")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            registry = base / "registry"
            outside = base / "outside-pack"
            outside.mkdir()
            pack = registry / "packs/bmad" / module.BMAD_PACK_VERSION
            pack.parent.mkdir(parents=True)
            pack.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "BMAD pack root escapes"):
                module.require_contained(registry, pack, "BMAD pack root")

    def test_provision_rejects_skill_directory_symlink_escape(self):
        module = load_script("provision-bmad-skills.py")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            pack = base / "pack"
            outside = base / "outside-skill"
            pack.mkdir()
            outside.mkdir()
            (pack / "bmad-escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "BMAD skill .* escapes"):
                module.pack_skills(pack)


if __name__ == "__main__":
    unittest.main()
