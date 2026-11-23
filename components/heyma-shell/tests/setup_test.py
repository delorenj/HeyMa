import json
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from heyma_shell import setup
from heyma_shell.adapters import Context, Run

VOXXY = "/h/.local/bin/voxxy"
REPORT = {"exit": 0, "url": "https://vox.delo.sh", "key": "ok",
          "speaker": {"active": True, "key": "big-chungus"},
          "steps": [{"name": "key", "state": "ok", "detail": "accepted (31 voices)"}]}
NOW = lambda: datetime(2026, 11, 23, 19, 0, tzinfo=timezone.utc)  # noqa: E731


class SetupTest(unittest.TestCase):
    def ctx(self, home, answer, environ=None):
        self.calls = []

        def run(argv, timeout=10):
            self.calls.append(argv)
            return answer
        return Context(home=home, environ=dict(environ or {}), run=run,
                       exists=lambda p: str(p) == VOXXY.replace("/h", str(home)))

    def test_voxxy_step_passes_the_choices_and_records_the_host(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            ctx = self.ctx(home, Run(0, json.dumps(REPORT)), {"HEYMA_VOXXY_KEY_REF": "op://V/i/credential"})
            doc = setup.run(["voxxy"], setup.Options(speak_here=True), ctx, now=NOW)
            self.assertEqual(self.calls[0][1:], ["setup", "--json", "--key-ref", "op://V/i/credential",
                                                 "--speak-here"])
            self.assertEqual(doc["exit"], 0)
            host = tomllib.loads(setup.host_toml_path(home).read_text())
            self.assertEqual(host["voxxy"], {"role": "client", "url": "https://vox.delo.sh", "key": "ok",
                                             "speak_here": True, "sink": "big-chungus",
                                             "configured_at": "2026-11-23T19:00:00Z"})

    def test_other_tables_in_host_toml_survive(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = setup.host_toml_path(home)
            path.parent.mkdir(parents=True)
            path.write_text('[wax]\nrole = "server"\n')
            setup.run(["voxxy"], setup.Options(), self.ctx(home, Run(0, json.dumps(REPORT))), now=NOW)
            self.assertEqual(tomllib.loads(path.read_text())["wax"], {"role": "server"})

    def test_a_missing_cli_is_a_failure_that_names_the_installer(self):
        with TemporaryDirectory() as tmp:
            ctx = Context(home=Path(tmp), environ={}, run=lambda *a, **k: Run(127), exists=lambda p: False)
            doc = setup.run(["voxxy"], setup.Options(), ctx, now=NOW)
            self.assertEqual(doc["exit"], 3)
            self.assertIn("install.sh", doc["results"][0]["detail"])
            self.assertFalse(setup.host_toml_path(Path(tmp)).exists())

    def test_degraded_exit_passes_through(self):
        with TemporaryDirectory() as tmp:
            doc = setup.run(["voxxy"], setup.Options(),
                            self.ctx(Path(tmp), Run(2, json.dumps({**REPORT, "exit": 2, "key": "missing"}))), now=NOW)
            self.assertEqual(doc["exit"], 2)

    def test_unknown_product_is_refused(self):
        with self.assertRaises(ValueError):
            setup.run(["nope"], setup.Options(), Context(home=Path("/nonexistent")))
