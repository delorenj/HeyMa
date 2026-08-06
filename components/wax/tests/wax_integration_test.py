import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
REPO_ROOT = next(
    parent for parent in COMPONENT_ROOT.parents
    if (parent / ".project.json").is_file()
)


class WaxIntegrationTest(unittest.TestCase):
    def test_default_runtime_root_is_heyma(self):
        env = os.environ.copy()
        env.pop("WAX_ROOT", None)
        env.pop("WAX_AUDIO_ROOT", None)
        env["PYTHONPATH"] = str(COMPONENT_ROOT / "src")
        result = subprocess.run(
            [
                "python3",
                "-c",
                "from wax.paths import HEYMA; print(HEYMA)",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), str(Path.home() / "HeyMa"))

    def test_wax_root_override_isolated_from_project_data(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            env = os.environ.copy()
            env["WAX_ROOT"] = runtime_root
            result = subprocess.run(
                [
                    str(REPO_ROOT / "bin" / "wax"),
                    "state",
                    "inbox",
                    "--cold",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertIn(result.returncode, {0, 2})
            self.assertIn('"state"', result.stdout)

    def test_root_shim_works_outside_repository(self):
        with tempfile.TemporaryDirectory() as working_dir, tempfile.TemporaryDirectory() as runtime_root:
            env = os.environ.copy()
            env["WAX_ROOT"] = runtime_root
            result = subprocess.run(
                [str(REPO_ROOT / "bin" / "wax"), "state", "stream", "--cold", "--json"],
                cwd=working_dir,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn(json.loads(result.stdout)["state"], {"ready", "not-ready"})

    def test_disabled_pipeline_with_backlog_is_stopped_not_error(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            inbox = Path(runtime_root) / "inbox"
            inbox.mkdir()
            (inbox / "queued.ogg").write_bytes(b"queued")
            env = os.environ.copy()
            env["WAX_ROOT"] = runtime_root
            result = subprocess.run(
                [
                    str(REPO_ROOT / "bin" / "wax"),
                    "state",
                    "inbox",
                    "--cold",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            state = json.loads(result.stdout)
            self.assertEqual(state["state"], "stopped")
            self.assertEqual(state["pending"], 1)
            self.assertEqual(state["cause_code"], "scheduler_disabled")


if __name__ == "__main__":
    unittest.main()
