import unittest
import subprocess
from unittest.mock import patch

from wax import desktop


class DesktopNotificationTest(unittest.TestCase):
    @patch("wax.desktop.subprocess.Popen")
    @patch("wax.desktop.shutil.which", return_value="/usr/bin/canberra-gtk-play")
    def test_start_and_complete_are_audible_desktop_events(self, _which, popen):
        self.assertTrue(desktop.ding("start"))
        self.assertTrue(desktop.ding("complete"))
        self.assertEqual(popen.call_args_list[0].args[0][2], "message")
        self.assertEqual(popen.call_args_list[1].args[0][2], "complete")


class TrayActionFeedbackTest(unittest.TestCase):
    """Tray actions used to be fire-and-forget: a refused `wax skip` left a greyed row and nothing else."""

    def run_action(self, runner):
        notes, failures = [], []
        desktop.run_reporting(["/x/bin/wax", "skip", "abc"], what="Skip abc", runner=runner,
                              notifier=lambda title, body, **_: notes.append((title, body)),
                              on_failure=failures.append, wait=True)
        return notes, failures

    def test_success_is_silent(self):
        ok = lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", "")
        self.assertEqual(self.run_action(ok), ([], []))

    def test_refusal_names_the_action_and_the_cli_error(self):
        refused = lambda *a, **k: subprocess.CompletedProcess(
            a[0], 2, '{"ok": false}', "Traceback (most recent call last):\nno item with id abc (see wax queue)\n")
        notes, failures = self.run_action(refused)
        self.assertEqual(notes[0][0], "Wax: Skip abc failed")
        self.assertIn("no item with id abc (see wax queue)", notes[0][1])
        self.assertIn("skip abc", notes[0][1])
        self.assertEqual(failures, ["no item with id abc (see wax queue)"])

    def test_timeout_and_missing_binary_are_reported(self):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired(a[0], 1)

        def missing(*a, **k):
            raise FileNotFoundError(2, "No such file", a[0][0])

        for runner, expected in ((slow, "no answer after"), (missing, "FileNotFoundError")):
            with self.subTest(expected=expected):
                notes, failures = self.run_action(runner)
                self.assertIn(expected, notes[0][1])
                self.assertEqual(len(failures), 1)

    def test_a_raising_callback_never_escapes(self):
        bad = lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "boom")

        def explode(_message):
            raise RuntimeError("host gone")

        notes = []
        desktop.run_reporting(["wax", "retry", "x"], what="Retry x", runner=bad,
                              notifier=lambda t, b, **_: notes.append(t), on_failure=explode, wait=True)
        self.assertEqual(notes, ["Wax: Retry x failed"])

    def test_error_line_is_the_reason_not_json_punctuation_or_a_frame(self):
        def result(out, err):
            return subprocess.CompletedProcess(["wax"], 2, out, err)

        pretty = '{\n  "ok": false,\n  "action": "refused",\n  "evidence": "stream busy: another capture owns the mic"\n}\n'
        trace = ('Traceback (most recent call last):\n  File "/x/bin/wax", line 9, in <module>\n'
                 '    main()\nsqlite3.OperationalError: database is locked\n')
        cases = [(result(pretty, ""), "stream busy: another capture owns the mic"),
                 (result("", trace), "sqlite3.OperationalError: database is locked"),
                 (result("{\n}\n", ""), "exit 2"),
                 (result("", "error: no item with id x (see wax queue)\n"),
                  "error: no item with id x (see wax queue)")]
        for res, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(desktop._action_error(res), expected)

    def test_rerun_passes_is_never_killed_but_ledger_actions_are(self):
        self.assertIsNone(desktop.action_timeout(["ep", "run-selected", "id", "ticketable"]))
        for args in (["retry", "x"], ["skip", "x"], ["queue", "--clear-completed"], ["rec", "toggle"]):
            self.assertEqual(desktop.action_timeout(args), desktop.ACTION_TIMEOUT_S)

    def test_run_wax_action_labels_times_and_names_the_app(self):
        seen, notes = {}, []

        def runner(argv, **kw):
            seen.update(argv=argv, timeout=kw["timeout"])
            return subprocess.CompletedProcess(argv, 1, "", "boom")

        desktop.run_wax_action("/x/bin/wax", ("ep", "run-selected", "id9", "summarize"),
                               app="HeyMa", runner=runner,
                               notifier=lambda t, b, **k: notes.append((t, k.get("app"))), wait=True)
        self.assertEqual(seen, {"argv": ["/x/bin/wax", "ep", "run-selected", "id9", "summarize"],
                                "timeout": None})
        self.assertEqual(notes, [("HeyMa: Rerun passes for id9 failed", "HeyMa")])

    def test_describe_action_names_verb_and_item(self):
        cases = {("retry", "id1"): "Retry id1", ("skip", "id2"): "Skip id2",
                 ("ep", "run-selected", "id3", "title-slug"): "Rerun passes for id3",
                 ("queue", "--clear-completed"): "Clear completed", ("rec", "toggle"): "Recording toggle"}
        for args, label in cases.items():
            self.assertEqual(desktop.describe_action(args), label)
