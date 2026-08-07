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

    def test_skip_moves_audio_out_of_queue_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            root = Path(runtime_root)
            inbox = root / "inbox"
            inbox.mkdir()
            audio = inbox / "skip-me.ogg"
            audio.write_bytes(b"preserve these bytes")
            env = os.environ.copy()
            env["WAX_ROOT"] = runtime_root
            identify = subprocess.run(
                [
                    "python3", "-c",
                    "from wax import ledger; from pathlib import Path; "
                    "print(ledger.upsert_item(Path(r'" + str(audio) + "')))"
                ],
                cwd=REPO_ROOT,
                env={**env, "PYTHONPATH": str(COMPONENT_ROOT / "src")},
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            result = subprocess.run(
                [str(REPO_ROOT / "bin" / "wax"), "skip", identify, "--json"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)
            skipped = root / "skipped" / audio.name
            self.assertEqual(payload["state"], "skipped")
            self.assertFalse(audio.exists())
            self.assertEqual(skipped.read_bytes(), b"preserve these bytes")

    def test_failed_item_stays_visible_without_blocking_next_pending_item(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            root = Path(runtime_root)
            inbox = root / "inbox"
            inbox.mkdir()
            failed = inbox / "01-failed.ogg"
            pending = inbox / "02-pending.ogg"
            failed.write_bytes(b"failed audio remains")
            pending.write_bytes(b"healthy audio follows")
            env = {**os.environ, "WAX_ROOT": runtime_root,
                   "PYTHONPATH": str(COMPONENT_ROOT / "src")}
            script = (
                "from wax import ledger,worker; from pathlib import Path; "
                f"bad=ledger.upsert_item(Path(r'{failed}')); "
                f"good=ledger.upsert_item(Path(r'{pending}')); "
                "ledger.set_item_state(bad,'failed',cause='test_failure'); "
                "print(worker.next_item()[0]); print(good)"
            )
            result = subprocess.run(
                ["python3", "-c", script], cwd=REPO_ROOT, env=env,
                capture_output=True, text=True, check=True,
            )
            selected, expected = result.stdout.splitlines()
            self.assertEqual(selected, expected)
            self.assertTrue(failed.exists())


if __name__ == "__main__":
    unittest.main()
