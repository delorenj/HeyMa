"""`wax transcripts …` (design D.7, E.1): parsing, dispatch, exit codes, ENV_SPECS coverage."""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

COMPONENT = Path(__file__).resolve().parents[1]
WAX = COMPONENT / "bin" / "wax"


def _load_wax():
    loader = SourceFileLoader("wax_cli_transcripts", str(WAX))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TranscriptsDispatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wax = _load_wax()

    def main(self, *argv):
        out = io.StringIO()
        with patch.object(sys, "argv", ["wax", *argv]), redirect_stdout(out):
            code = self.wax.main()
        return code, out.getvalue()

    def test_search_dispatches_query_and_filters(self):
        result = {"query": "a b", "mode": "hybrid", "count": 0, "results": []}
        with patch.object(self.wax.transcriptions.search, "run", return_value=result) as run:
            code, out = self.main("transcripts", "search", "a", "b", "--tag", "x", "--tag", "y",
                                  "--since", "2026-10-01", "--classification", "meeting", "--json")
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args, ("a b",))
        kw = run.call_args.kwargs
        self.assertEqual(kw["tags"], ["x", "y"])
        self.assertEqual(kw["since"], "2026-10-01")
        self.assertEqual(kw["classifications"], ["meeting"])
        self.assertEqual(kw["limit"], 10)
        self.assertFalse(kw["lexical"])
        self.assertIn('"mode": "hybrid"', out)

    def test_search_accepts_phone_call_and_refuses_an_unknown_classification(self):
        with patch.object(self.wax.transcriptions.search, "run", return_value={"results": []}) as run:
            code, _ = self.main("transcripts", "search", "x", "--classification", "phone_call", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["classifications"], ["phone_call"])
        with self.assertRaises(SystemExit) as refused, patch("sys.stderr", io.StringIO()):
            self.main("transcripts", "search", "x", "--classification", "voicemail")
        self.assertEqual(refused.exception.code, 2)

    def test_search_human_output_is_one_line_per_hit(self):
        hit = {"score": 0.0331, "effective_at": "2026-02-10T20:36:25+00:00", "classification": "meeting",
               "audio_duration_s": 754.2, "display_title": "Spouses disagreeing", "version_no": 2,
               "produced_by": "frontmatter-stamp", "snippet": "Speaker 2: the «phone»", "note_path": "/v/a.md"}
        with patch.object(self.wax.transcriptions.search, "run",
                          return_value={"mode": "lexical", "results": [hit]}):
            code, out = self.main("transcripts", "search", "phone", "--lexical")
        self.assertEqual(code, 0)
        self.assertIn("0.0331  2026-02-10  meeting   12:34  Spouses disagreeing  v2(frontmatter-stamp)", out)
        self.assertIn("Speaker 2: the «phone»", out)
        self.assertIn("/v/a.md", out)

    def test_show_dispatches_version_and_raw(self):
        shown = {"transcript": {"transcript_id": 7}, "version": {"version_no": 2}, "raw": "---\na: 1\n---\nbody\n",
                 "latest_version_no": 3}
        with patch.object(self.wax.transcriptions, "show", return_value=shown) as show:
            code, out = self.main("transcripts", "show", "4c9f2d894fde1cc5", "--version", "2", "--raw")
        self.assertEqual(code, 0)
        show.assert_called_once_with("4c9f2d894fde1cc5", version=2, raw=True)
        self.assertEqual(out, "---\na: 1\n---\nbody\n")             # exact bytes, nothing added

    def test_unknown_ref_is_exit_2(self):
        with patch.object(self.wax.transcriptions, "history", side_effect=LookupError("no transcript matches 'zz'")):
            code, _ = self.main("transcripts", "history", "zz")
        self.assertEqual(code, 2)

    def test_sync_flags_and_drift_exit(self):
        with patch.object(self.wax.transcriptions, "sync", return_value={"drift": True}) as sync:
            code, _ = self.main("transcripts", "sync", "--check", "--json")
        sync.assert_called_once_with(dry_run=False, check=True, embed=False, limit=0)
        self.assertEqual(code, 2)
        with patch.object(self.wax.transcriptions, "sync", return_value={"errors": [], "dry_run": True}) as sync:
            code, _ = self.main("transcripts", "sync", "--dry-run", "--embed", "--limit", "5")
        sync.assert_called_once_with(dry_run=True, check=False, embed=True, limit=5)
        self.assertEqual(code, 0)

    def test_migrate_and_embed(self):
        with patch.object(self.wax.transcriptions.migrate, "apply", return_value={"applied": []}) as apply:
            self.assertEqual(self.main("transcripts", "migrate", "--dry-run")[0], 0)
        apply.assert_called_once_with(dry_run=True)
        with patch.object(self.wax.transcriptions.embed, "sweep", return_value={"embedded": 0}) as sweep:
            self.assertEqual(self.main("transcripts", "embed", "--limit", "16")[0], 0)
        sweep.assert_called_once_with(until_empty=True, limit=16)

    def test_browse_dispatches_list_and_detail(self):
        listing = {"schema": 1, "counts": {"monolog": 1}, "rows": [], "projects": []}
        with patch.object(self.wax.transcriptions.browse, "run", return_value=listing) as run:
            code, out = self.main("--json", "transcripts", "browse", "--view", "phone_call", "--include-hermes",
                                  "--limit", "50", "--since", "2026-10-01", "--project", "pilot")
        self.assertEqual(code, 0)
        run.assert_called_once_with(view="phone_call", include_hermes=True, limit=50, since="2026-10-01",
                                    until=None, project="pilot")
        self.assertEqual(json.loads(out)["schema"], 1)
        with patch.object(self.wax.transcriptions.browse, "run", return_value=listing) as run:
            self.assertEqual(self.main("transcripts", "browse")[0], 0)
        run.assert_called_once_with(view="all", include_hermes=False, limit=1000, since=None, until=None,
                                    project=None)
        shown = {"schema": 1, "row": {"transcript_id": 7, "title": "T", "harvest": {"state": "none"}},
                 "tickets": [], "pass_history": [{"slug": "classification", "pass_version": 1,
                                                  "state": "completed", "finished_at": "2026-10-29T10:00:00Z"}]}
        with patch.object(self.wax.transcriptions.browse, "detail", return_value=shown) as detail:
            code, out = self.main("transcripts", "browse", "--id", "7")
        self.assertEqual(code, 0)
        detail.assert_called_once_with(7)
        self.assertIn("#7", out)
        self.assertIn("classification v1  completed", out)

    def test_browse_refuses_bad_views_unknown_ids_and_reports_an_unreachable_store(self):
        with self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
            self.main("transcripts", "browse", "--view", "voicemail")
        with patch.object(self.wax.transcriptions.browse, "detail", side_effect=LookupError("no transcript #9")), \
                patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(self.main("transcripts", "browse", "--id", "9")[0], 2)
        self.assertIn("error: no transcript #9", err.getvalue())
        down = self.wax.transcriptions.StoreUnavailable("$WAX_TRANSCRIPTS_RO_DSN is not configured")
        with patch.object(self.wax.transcriptions.browse, "run", side_effect=down), \
                patch("sys.stderr", io.StringIO()) as err:
            code, out = self.main("--json", "transcripts", "browse")
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual(payload["store"]["state"], "unreachable")
        self.assertEqual((payload["rows"], payload["counts"]), ([], {}))
        self.assertIn("error:", err.getvalue())

    def test_link_harvest_dispatch_and_exit_codes(self):
        ok = {"files": 1, "inserted": 1, "invalid": [], "errors": [], "warnings": []}
        with patch.object(self.wax.transcriptions.harvest, "link", return_value=ok) as link:
            self.assertEqual(self.main("transcripts", "link-harvest", "--report")[0], 0)
        link.assert_called_once_with(None, dry_run=False, report=True)
        bad = {**ok, "invalid": [{"path": "/x.json", "reason": "artifact version 1"}]}
        with patch.object(self.wax.transcriptions.harvest, "link", return_value=bad) as link:
            self.assertEqual(self.main("transcripts", "link-harvest", "/x.json", "--dry-run")[0], 2)
        link.assert_called_once_with(["/x.json"], dry_run=True, report=False)
        with patch.object(self.wax.transcriptions.harvest, "link", return_value=bad):
            self.assertEqual(self.main("transcripts", "link-harvest")[0], 0)   # a scan reports, never fails

    def test_render_status_prints_a_transcripts_line(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.wax.render_status({"transcriptions": {"state": "lagging", "queued": 4, "embed_pending": 12,
                                                       "reason": "oldest queued snapshot is 900s old"}})
        self.assertIn("transcripts  lagging (queued 4, embed pending 12)", out.getvalue())

    def test_every_store_knob_is_in_env_specs(self):
        names = {name for name, _consumer, _default in self.wax.ENV_SPECS}
        source = "".join(p.read_text() for p in (COMPONENT / "src" / "wax" / "transcriptions").glob("*.py"))
        knobs = set(re.findall(r'"((?:WAX_TRANSCRIPTS|WAX_EMBED|WAX_S3|WAX_HARVEST)[A-Z_]*)"', source))
        self.assertIn("WAX_TRANSCRIPTS_RO_DSN", knobs)
        self.assertIn("WAX_HARVEST_DIR", knobs)
        self.assertTrue(knobs)
        self.assertEqual(sorted(knobs - names), [])
        # The direct passwords are redacted; the op:// references and the password-less DSN are not.
        self.assertEqual(self.wax._redact("WAX_TRANSCRIPTS_PASSWORD", "hunter2"), "<redacted: 7 chars>")
        self.assertEqual(self.wax._redact("WAX_TRANSCRIPTS_OWNER_PASSWORD", "hunter2"), "<redacted: 7 chars>")
        self.assertEqual(self.wax._redact("WAX_TRANSCRIPTS_RO_PASSWORD", "hunter2"), "<redacted: 7 chars>")
        self.assertEqual(self.wax._redact("WAX_TRANSCRIPTS_PASSWORD_OP", "op://DeLoSecrets/x/password"),
                         "op://DeLoSecrets/x/password")


class TranscriptsProcessTest(unittest.TestCase):
    """Through the real `__main__` handler, as an operator runs it."""

    def run_wax(self, *argv):
        env = {k: v for k, v in os.environ.items() if not k.startswith("WAX_TRANSCRIPTS_") or k.endswith("UNIT_FILE")}
        env["WAX_TRANSCRIPTS_UNIT_FILE"] = "/nonexistent/wax-test-suite/waxd.service"
        return subprocess.run([sys.executable, str(WAX), *argv], capture_output=True, text=True, timeout=60, env=env)

    def test_help_lists_every_subcommand(self):
        r = self.run_wax("transcripts", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for sub in ("sync", "search", "show", "history", "migrate", "embed", "link-harvest", "browse"):
            self.assertIn(sub, r.stdout)

    def test_browse_without_the_ro_dsn_is_error_exit_2_with_an_unreachable_payload(self):
        r = self.run_wax("--json", "transcripts", "browse")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("WAX_TRANSCRIPTS_RO_DSN", r.stderr)
        self.assertEqual(json.loads(r.stdout)["store"]["state"], "unreachable")
        self.assertNotIn("Traceback", r.stderr)

    def test_store_unavailable_is_error_exit_2(self):
        r = self.run_wax("transcripts", "search", "anything")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("error:", r.stderr)
        self.assertIn("WAX_TRANSCRIPTS_DSN", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
