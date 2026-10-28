"""deploy/provision-transcriptions (design B.1, D.9, F step 1): the parts that can be checked without
touching the cluster or the vault. Secrets travel on stdin only, never in argv."""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from wax.transcriptions import config, db

COMPONENT = Path(__file__).resolve().parents[1]
SCRIPT = COMPONENT / "deploy" / "provision-transcriptions"


def _load():
    loader = SourceFileLoader("provision_transcriptions", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ProvisionScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_psql_literal_escapes_quotes_and_backslashes(self):
        self.assertEqual(self.mod.psql_literal("a'b\\c"), "'a\\'b\\\\c'")

    def test_bootstrap_sql_keeps_passwords_in_psql_variables_and_out_of_the_log(self):
        sql = self.mod.bootstrap_sql({"owner": "create", "app": "alter", "ro": "ok"},
                                     {"owner": "own-pw", "app": "app-pw", "ro": "ro-pw"}, create_database=True)
        lines = sql.splitlines()
        self.assertEqual(lines[0], "SET log_statement = 'none';")
        self.assertIn("SET log_min_error_statement = 'panic';", lines[:3])
        self.assertIn("\\set pw_owner 'own-pw'", lines)
        self.assertIn("CREATE ROLE transcriptions_owner LOGIN PASSWORD :'pw_owner';", lines)
        self.assertIn("ALTER ROLE transcriptions_app WITH LOGIN PASSWORD :'pw_app';", lines)
        self.assertNotIn("ro-pw", sql)                     # a role that already logs in is left alone
        self.assertEqual([l for l in lines if "own-pw" in l or "app-pw" in l],
                         ["\\set pw_owner 'own-pw'", "\\set pw_app 'app-pw'"])
        self.assertIn("CREATE DATABASE transcriptions OWNER transcriptions_owner TEMPLATE template0 ENCODING 'UTF8';",
                      lines)
        self.assertIn("\\connect transcriptions", lines)
        self.assertIn("CREATE EXTENSION IF NOT EXISTS vector;", lines)
        self.assertIn("CREATE EXTENSION IF NOT EXISTS pg_trgm;", lines)
        again = self.mod.bootstrap_sql({"owner": "ok", "app": "ok", "ro": "ok"}, {}, create_database=False)
        self.assertNotIn("CREATE DATABASE", again)
        self.assertNotIn("PASSWORD", again)

    def test_item_creation_pipes_the_template_on_stdin(self):
        calls = []

        def fake_run(argv, *, stdin=None, check=True):
            calls.append((argv, stdin))
            return type("P", (), {"stdout": json.dumps({"id": "abc123"}), "stderr": "", "returncode": 0})()

        with patch.object(self.mod, "run", side_effect=fake_run):
            item_id, password = self.mod.create_item("transcriptions_app", "Transcriptions Postgres App", "waxd",
                                                     dry_run=False)
        self.assertEqual(item_id, "abc123")
        argv, stdin = calls[0]
        self.assertEqual(argv[:5], ["op", "item", "create", "--vault", "DeLoSecrets"])
        self.assertEqual(argv[-1], "-")
        self.assertNotIn(password, " ".join(argv))
        template = json.loads(stdin)
        fields = {f["label"]: f["value"] for f in template["fields"]}
        self.assertEqual(template["category"], "DATABASE")
        self.assertEqual((fields["server"], fields["port"], fields["database"], fields["username"]),
                         ("localhost", "5432", "transcriptions", "transcriptions_app"))
        self.assertEqual(fields["password"], password)
        self.assertEqual(fields["dsn"], f"postgresql://transcriptions_app:{password}@localhost:5432/transcriptions")

    def test_the_committed_unit_carries_placeholders_until_provisioning_fills_them(self):
        text = (COMPONENT / "deploy" / "systemd" / "user" / "waxd.service").read_text()
        self.assertIn("Environment=WAX_TRANSCRIPTS_DSN=postgresql://transcriptions_app@localhost:5432/transcriptions",
                      text)
        for line in text.splitlines():
            if "TRANSCRIPTS" in line and line.startswith("Environment="):
                self.assertNotRegex(line, r"://[^/@\s]+:[^/@\s]+@", "a DSN must never carry a password")
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / "waxd.service"
            shutil.copy(COMPONENT / "deploy" / "systemd" / "user" / "waxd.service", unit)
            with patch.object(self.mod, "UNIT_TEMPLATE", unit):
                refs = self.mod.unit_references()
                if refs["app"] is None:                      # still the committed placeholders
                    changed = self.mod.fill_unit({"app": "appuuid", "owner": "owneruuid"}, dry_run=False)
                    self.assertEqual(len(changed), 2)
                    self.assertEqual(self.mod.unit_references(), {"app": "appuuid", "owner": "owneruuid"})
                    self.assertEqual(self.mod.fill_unit({"app": "x", "owner": "y"}, dry_run=False), [])


class PlaceholderPasswordTest(unittest.TestCase):
    def test_a_placeholder_reference_is_refused_without_running_op(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("WAX_TRANSCRIPTS")}
        env.update({"WAX_TRANSCRIPTS_DSN": "postgresql://transcriptions_app@localhost:5432/transcriptions",
                    "WAX_TRANSCRIPTS_PASSWORD_OP": "op://DeLoSecrets/REPLACE_WITH_APP_ITEM_UUID/password"})
        with patch.dict(os.environ, env, clear=True), \
                patch.object(config, "UNIT_PATH", Path("/nonexistent/unit.service")), \
                patch("wax.provider.subprocess.run") as op:
            db.forget_passwords()
            with self.assertRaises(db.StoreUnavailable) as caught:
                db._password("app")
        op.assert_not_called()
        self.assertIn("provision-transcriptions", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
