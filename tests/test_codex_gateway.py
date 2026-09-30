import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("heyma_codex_gateway", ROOT / ".mise/scripts/codex-gateway.py")
GATEWAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATEWAY)
OWNER = GATEWAY.load_owner()
ROUTES = OWNER.load_catalog(OWNER.CATALOG)
DISCOVERY = [{"id": route["id"], "automaticai": {
    "account": route["account"], "upstream_model": route["upstream"],
    "default_effort": route["default_effort"], "billing": route["billing"],
    "effort": ["medium", "high", "xhigh", "max"],
    "capabilities": ["responses", "streaming", "function_tools"],
}} for route in ROUTES]


class CodexLaunchOwnershipTest(unittest.TestCase):
    def setUp(self):
        discovery = patch.object(OWNER, "discover_models", return_value=DISCOVERY)
        discovery.start()
        self.addCleanup(discovery.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_mise_projects_before_shells_and_children_without_removing_isolation(self):
        config = tomllib.loads((ROOT / "mise.toml").read_text())
        self.assertEqual(config["env"]["CODEX_HOME"], "{{config_root}}/.codex")
        self.assertEqual(config["env"]["_"]["source"], ".mise/scripts/codex-env.sh")
        script = (ROOT / ".mise/scripts/codex-env.sh").read_text()
        self.assertIn("${BASH_SOURCE[0]}", script)
        self.assertIn('codex-gateway.py" --quiet || exit 1', script)

    def test_exact_route_effort_siblings_and_auth_survive_idempotent_regeneration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / ".codex"
            home.mkdir()
            settings = 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "xhigh"\n[tui]\nscreen_reader_detection_done = true\n[projects."/workspace"]\ntrust_level = "trusted"\n'
            (home / "config.toml").write_text(settings)
            auth = '{"auth_mode":"chatgpt","tokens":{}}\n'
            (home / "auth.json").write_text(auth)
            first = GATEWAY.synchronize(OWNER, root)
            self.assertEqual(first["provider"], "automaticai")
            self.assertEqual(first["model"], "automaticai/personal/sol-6.1")
            self.assertEqual(first["effort"], "xhigh")
            self.assertEqual(first["models"], len(DISCOVERY))
            self.assertFalse(first["daemon_auto_start"])
            config = OWNER.read_toml(home / "config.toml")
            original = tomllib.loads(settings)
            for key in ("projects", "tui"):
                self.assertEqual(config[key], original[key])
            self.assertEqual(OWNER.token_reference(config["model_providers"]["automaticai"]),
                             "op://DeLoSecrets/yeurk5dpqkaarspvsn3cjtmkki/codex")
            snapshots = {file: file.read_bytes() for file in home.rglob("*") if file.is_file()}
            self.assertEqual(GATEWAY.synchronize(OWNER, root), first)
            GATEWAY.synchronize(OWNER, root, check=True)
            self.assertEqual({file: file.read_bytes() for file in snapshots}, snapshots)
            self.assertEqual((home / "auth.json").read_text(), auth)

    def test_missing_home_is_seeded_from_maintained_intent_not_inherited_home(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CODEX_HOME": ".codex"}):
            root = Path(directory)
            result = GATEWAY.synchronize(OWNER, root)
            self.assertEqual(result["codex_home"], str(root / ".codex"))
            self.assertEqual(result["model"], "automaticai/personal/sol-6.1")
            self.assertEqual(result["effort"], "xhigh")

    def test_explicit_model_menu_selection_and_effort_remain_owned_by_user(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            GATEWAY.synchronize(OWNER, root)
            config_path = root / ".codex/config.toml"
            original = config_path.read_text()
            updated = OWNER.replace_root_settings(original, {
                "model": "automaticai/personal/kimi-k3", "model_reasoning_effort": "high",
            })
            OWNER.atomic_write(config_path, updated, expected=original)
            result = GATEWAY.synchronize(OWNER, root)
            self.assertEqual(result["model"], "automaticai/personal/kimi-k3")
            self.assertEqual(result["effort"], "high")

    def test_catalog_failure_cannot_activate_native_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / ".codex"
            home.mkdir()
            original = 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "xhigh"\n'
            (home / "config.toml").write_text(original)
            with patch.object(OWNER, "discover_models", side_effect=OWNER.MigrationError("catalog unavailable")):
                with self.assertRaises(OWNER.MigrationError):
                    GATEWAY.synchronize(OWNER, root)
            self.assertEqual((home / "config.toml").read_text(), original)

    def test_check_rejects_return_of_native_provider_or_daemon_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            GATEWAY.synchronize(OWNER, root)
            config_path = root / ".codex/config.toml"
            original = config_path.read_text()
            for setting in ({"model_provider": "openai"}, {"daemon_auto_start": True}):
                with self.subTest(setting=setting):
                    OWNER.atomic_write(config_path, OWNER.replace_root_settings(original, setting))
                    with self.assertRaises(OWNER.MigrationError):
                        GATEWAY.synchronize(OWNER, root, check=True)

    def test_env_source_stops_mise_on_install_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "env.sh").write_text((ROOT / ".mise/scripts/codex-env.sh").read_text())
            (root / "codex-gateway.py").write_text("raise SystemExit(1)\n")
            result = subprocess.run(["bash", "-c", 'source "$1"; echo SHOULD_NOT_LAUNCH', "test", str(root / "env.sh")],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("SHOULD_NOT_LAUNCH", result.stdout)


if __name__ == "__main__":
    unittest.main()
