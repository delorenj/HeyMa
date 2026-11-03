"""The HeyMa window's two contracts that live outside GTK.

1. The operator pass switch (var/settings.json) is a run-time overlay: a pass
   switched off is recorded `skipped` / `operator_disabled` under a FROZEN
   compiled pipeline, its dependents still run, and an explicit `wax ep run`
   (operator_toggle=False) still runs it.
2. The window's pure helpers shape `wax` CLI output and journal lines; they are
   importable without gi (mise's Python has none).
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import heyma_window, ledger, passes, paths, settings
from pipelines_test import _Graph


class SettingsFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.var = Path(self.tmp.name)
        self.patch = patch.object(paths, "VAR", self.var)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_default_is_everything_on(self):
        self.assertEqual(settings.disabled_passes(), set())
        self.assertTrue(settings.pass_enabled("title-slug"))

    def test_switch_off_and_back_on_is_idempotent_and_durable(self):
        first = settings.set_pass_enabled("title-slug", False)
        self.assertTrue(first["changed"])
        again = settings.set_pass_enabled("title-slug", False)
        self.assertFalse(again["changed"])
        self.assertEqual(json.loads((self.var / "settings.json").read_text())["passes"]["disabled"],
                         ["title-slug"])
        self.assertFalse(settings.pass_enabled("title-slug"))
        settings.set_pass_enabled("title-slug", True)
        self.assertEqual(settings.disabled_passes(), set())

    def test_a_corrupt_file_means_nothing_is_switched_off(self):
        (self.var / "settings.json").write_text("{not json")
        self.assertEqual(settings.disabled_passes(), set())
        # and the next write repairs it rather than raising
        settings.set_pass_enabled("summarize", False)
        self.assertEqual(settings.disabled_passes(), {"summarize"})


class OperatorDisabledRunTest(unittest.TestCase):
    def test_switched_off_pass_skips_and_its_dependent_still_runs(self):
        from wax import pipelines
        with _Graph() as g:
            g.write_pipeline(["a", "b", "c"])  # b requires a
            pipelines.compile(["graph"])
            settings.set_pass_enabled("a", False)
            run = pipelines.run_document("graph", g.doc())
            states = {r["ep_slug"]: r for r in run["results"]}
            self.assertEqual(states["a"]["state"], "skipped")
            self.assertEqual(states["a"]["reason_code"], "operator_disabled")
            self.assertEqual(states["b"]["state"], "completed")
            self.assertEqual(states["c"]["state"], "completed")
            row = ledger.connect().execute(
                "SELECT state, reason_code FROM passes WHERE ep_slug='a'").fetchone()
            self.assertEqual((row["state"], row["reason_code"]), ("skipped", "operator_disabled"))

    def test_switching_back_on_runs_the_pass_on_the_next_item(self):
        from wax import pipelines
        with _Graph() as g:
            g.write_pipeline(["a"])
            pipelines.compile(["graph"])
            settings.set_pass_enabled("a", False)
            pipelines.run_document("graph", g.doc("one.md"))
            settings.set_pass_enabled("a", True)
            run = pipelines.run_document("graph", g.doc("two.md"))
            self.assertEqual(run["results"][0]["state"], "completed")

    def test_explicit_run_ignores_the_switch(self):
        from wax import pipelines
        with _Graph() as g:
            g.write_pipeline(["a"])
            pipelines.compile(["graph"])
            run = pipelines.run_document("graph", g.doc())
            settings.set_pass_enabled("a", False)
            result = passes.run(run["doc_id"], "a", operator_toggle=False)
            self.assertEqual(result["state"], "completed")


class WindowHelpersTest(unittest.TestCase):
    def test_log_levels(self):
        self.assertEqual(heyma_window.log_level("... ERROR wax.worker: boom"), "error")
        self.assertEqual(heyma_window.log_level("Traceback (most recent call last):"), "error")
        self.assertEqual(heyma_window.log_level("... WARNING wax.passes: x"), "warn")
        self.assertEqual(heyma_window.log_level("... INFO wax.passes: ok"), "info")

    def test_journal_lines_lose_host_pid_and_year(self):
        line = ("2026-11-02T14:39:55-05:00 big-chungus waxd[1750258]: INFO wax.passes: "
                "title-slug completed")
        self.assertEqual(heyma_window.split_journal_line(line),
                         ("11-02 14:39:55", "waxd", "INFO wax.passes: title-slug completed"))
        self.assertEqual(heyma_window.split_journal_line("-- No entries --"),
                         ("", "", "-- No entries --"))

    def test_journal_argv_follows_every_unit(self):
        argv = heyma_window.journal_argv(("waxd.service", "wax-alert.service"), backlog=10)
        self.assertEqual(argv[:3], ["journalctl", "--user", "--follow"])
        self.assertIn("--lines=10", argv)
        self.assertEqual(argv.count("--unit"), 2)

    def test_queue_counts(self):
        rows = [{"state": "complete"}, {"state": "complete"}, {"state": "failed"},
                {"state": "suspect"}, {"state": "archived"}]
        self.assertEqual(heyma_window.finished_count(rows), 2)
        self.assertEqual(heyma_window.failed_count(rows), 2)

    def test_pass_rows_split_runnable_from_dormant(self):
        ep_rows = [
            {"slug": "title-slug", "enabled": True, "auto": True, "version": 3,
             "description": "Grounded title.\nmore", "operator_enabled": False, "failed": 2},
            {"slug": "summarize", "enabled": True, "auto": False, "version": 1,
             "description": "Summary."},
            {"slug": "wikification", "enabled": False, "version": 1,
             "path": "/x/passes.d/wikification.yaml", "description": "Wiki."},
        ]
        catalog = {"pipelines": {"pipelines": [
            {"name": "transcript-standard", "enabled": True, "nodes": [{"slug": "title-slug"}]},
            {"name": "doc-standard", "enabled": True, "nodes": [{"slug": "title-slug"}]},
            {"name": "old", "enabled": False, "nodes": [{"slug": "summarize"}]},
        ]}}
        shaped = heyma_window.pass_rows(ep_rows, catalog)
        active = {r["slug"]: r for r in shaped["active"]}
        self.assertEqual(set(active), {"title-slug", "summarize"})
        self.assertFalse(active["title-slug"]["operator_enabled"])
        self.assertEqual(active["title-slug"]["pipelines"], ["doc-standard", "transcript-standard"])
        self.assertIn("2 failing", active["title-slug"]["subtitle"])
        self.assertIn("manual", active["summarize"]["subtitle"])
        self.assertEqual([r["slug"] for r in shaped["inactive"]], ["wikification"])
        self.assertIn("wikification.yaml", shaped["inactive"][0]["subtitle"])


if __name__ == "__main__":
    unittest.main()
