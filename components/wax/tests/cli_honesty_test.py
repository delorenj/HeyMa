"""The inbox-control CLI says what is true about failed items, for ANY cause.

Born from three silent Voice Memos that failed (`transcribe_failed`) while
`wax status` said "queued", `wax queue` dumped raw JSON, `wax drain` said
"nothing pending", `wax skip` refused the item and a bare `wax retry` retried
everything. Everything here keys on item state failed|suspect, never on a cause
or an id, so the next failure from any intake source is covered too.
"""
import importlib.util
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
WAX = COMPONENT_ROOT / "bin" / "wax"


def load_cli():
    loader = SourceFileLoader("wax_cli_honesty", str(WAX))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Root:
    """An isolated WAX_ROOT with helpers to mint ledger fixtures and run the CLI."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "inbox").mkdir()
        self.env = {**os.environ, "WAX_ROOT": str(self.root),
                    "PYTHONPATH": str(COMPONENT_ROOT / "src")}
        self.py("from wax import state; state.ENABLED_FLAG.parent.mkdir(parents=True, exist_ok=True); "
                "state.ENABLED_FLAG.touch()")

    def close(self):
        self._tmp.cleanup()

    def py(self, code: str) -> str:
        return subprocess.run(["python3", "-c", textwrap.dedent(code)], env=self.env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def item(self, name: str, state: str, cause: str = "", evidence: str = "", sub: str = "",
             body: bytes = b"", s3: bool = False) -> str:
        path = self.root / "inbox" / sub / name if sub else self.root / "inbox" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body or name.encode() * 4)
        lines = ["from wax import ledger; from pathlib import Path",
                 f"i = ledger.upsert_item(Path({str(path)!r}))"]
        if state != "pending":
            lines.append(f"ledger.set_item_state(i, {state!r}, cause={cause!r}, evidence={evidence!r})")
        if s3:
            lines.append("ledger.connect().execute('INSERT INTO backups(item_id,s3_key,bucket,bytes,verified_at) "
                         "VALUES(?,?,?,?,?)', (i, 'k', 'recordings', 1, '2026-01-01T00:00:00Z'))")
        lines.append("print(i)")
        return self.py("\n".join(lines))

    def wax(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run([str(WAX), *args], env=self.env, capture_output=True, text=True, check=check)

    def snap(self) -> dict:
        return json.loads(self.py("""
            import json
            from wax import ledger, state
            print(json.dumps(ledger.enrich(state.snapshot(run_preflight=False)), default=str))
        """))


class FailedItemPrimitiveTest(unittest.TestCase):
    def setUp(self):
        self.r = Root()
        self.addCleanup(self.r.close)

    def test_fresh_failed_item_is_failed_not_queued_inside_the_grace_window(self):
        a = self.r.item("one.m4a", "failed", "transcribe_failed", "EMPTY TRANSCRIPT from 4748s\nmore")
        self.r.item("two.m4a", "failed", "transcribe_failed", "EMPTY TRANSCRIPT from 100s")
        snap = self.r.snap()  # files were created a moment ago: inside QUEUE_GRACE_S
        self.assertEqual(snap["inbox"]["cause_code"], "failed_items")
        self.assertEqual(snap["inbox"]["state"], "error")
        self.assertEqual(snap["inbox"]["evidence"], "2 failed: 2x transcribe_failed")
        queue = snap["queue"]
        self.assertEqual((queue["total"], queue["waiting"], queue["failed"], queue["parked"]), (2, 0, 2, 0))
        self.assertEqual(queue["actionable"], queue["waiting"])  # deprecated alias
        self.assertEqual(len(snap["failed"]), 2)
        row = next(r for r in snap["failed"] if r["item_id"] == a)
        self.assertEqual((row["orig_name"], row["cause_code"]), ("one.m4a", "transcribe_failed"))
        self.assertEqual(row["evidence"], "EMPTY TRANSCRIPT from 4748s")  # first line only

    def test_waiting_work_keeps_the_inbox_honest_about_the_worker(self):
        self.r.item("bad.m4a", "failed", "decode_failed")
        self.r.item("new.m4a", "pending")
        snap = self.r.snap()
        self.assertEqual(snap["inbox"]["cause_code"], "queued")
        self.assertEqual((snap["queue"]["waiting"], snap["queue"]["failed"]), (1, 1))
        # ...but the failed one is still listed with its reason.
        self.assertEqual([r["cause_code"] for r in snap["failed"]], ["decode_failed"])

    def test_buckets_sum_to_total_and_unminted_files_count_as_waiting(self):
        self.r.item("bad.m4a", "failed", "decode_failed")
        self.r.item("done.m4a", "complete")
        (self.r.root / "inbox" / "unminted.m4a").write_bytes(b"not in the ledger yet")
        q = self.r.snap()["queue"]
        self.assertEqual((q["total"], q["failed"], q["parked"], q["waiting"]), (3, 1, 1, 1))
        self.assertEqual(q["waiting"] + q["failed"] + q["parked"], q["total"])

    def test_failed_item_in_a_nested_inbox_directory_is_found(self):
        self.r.item("deep.m4a", "failed", "transcribe_failed", "x", sub="2026/11")
        self.assertEqual(self.r.snap()["queue"]["failed"], 1)
        rows = json.loads(self.r.wax("--json", "queue").stdout)
        self.assertEqual([row["state"] for row in rows], ["failed"])  # the tray list sees it too

    def test_snapshot_lists_five_and_counts_the_rest(self):
        for n in range(7):
            self.r.item(f"f{n}.m4a", "suspect", "sanity_gate", "bad")
        snap = self.r.snap()
        self.assertEqual((len(snap["failed"]), snap["failed_more"], snap["queue"]["failed"]), (5, 2, 7))
        self.assertEqual(snap["inbox"]["evidence"], "7 failed: 7x sanity_gate")

    def test_no_failures_no_noise(self):
        snap = self.r.snap()
        self.assertEqual(snap["failed"], [])
        self.assertEqual(snap["failed_more"], 0)
        self.assertEqual(snap["queue"]["failed"], 0)

    def test_failed_item_without_a_transition_still_has_a_cause(self):
        i = self.r.item("odd.m4a", "pending")
        self.r.py(f"from wax import ledger; ledger.connect().execute(\"UPDATE items SET state='failed' WHERE item_id='{i}'\")")
        self.assertEqual(self.r.snap()["failed"][0]["cause_code"], "discovered")

    def test_waxd_runs_enrich_before_recording_the_inbox_transition(self):
        source = (COMPONENT_ROOT / "bin" / "waxd").read_text()
        tick = source[source.index("def _tick"):]
        self.assertLess(tick.index("ledger.enrich(snap)"), tick.index('note_machine("inbox"'))


class StatusSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.cli = load_cli()

    def test_queue_failed_alone_makes_status_unhealthy(self):
        healthy = {"stream": {"state": "ready"}, "inbox": {"state": "ready-and-active", "cause_code": "queued"},
                   "queue": {"failed": 0}}
        self.assertFalse(self.cli.status_unhealthy(healthy))
        self.assertTrue(self.cli.status_unhealthy({**healthy, "queue": {"failed": 1}}))

    def test_render_status_speaks_operator_not_worker(self):
        import contextlib
        import io
        snap = {
            "stream": {"state": "ready"},
            "inbox": {"state": "error", "cause_code": "failed_items", "evidence": "3 failed: 3x transcribe_failed"},
            "queue": {"total": 3, "waiting": 0, "failed": 3, "parked": 0, "actionable": 0},
            "failed": [{"item_id": "89d99381a94d0eb5", "orig_name": "20261118 092137.m4a",
                        "cause_code": "transcribe_failed",
                        "evidence": "EMPTY TRANSCRIPT from 4748s (audio is silent?)"}],
            "failed_more": 2,
        }
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.cli.render_status(snap)
        text = out.getvalue()
        self.assertIn("3 in inbox · 0 waiting for the worker · 3 FAILED (need you)", text)
        self.assertNotIn("actionable", text)
        self.assertIn("89d99381a94d0eb5  20261118 092137.m4a  transcribe_failed: EMPTY TRANSCRIPT from 4748s "
                      "(audio is silent?)  -> wax retry 89d99381a94d0eb5 | wax skip 89d99381a94d0eb5", text)
        self.assertIn("+2 more", text)

    def test_duration_format(self):
        self.assertEqual(self.cli._fmt_dur(4748), "79m 8s")
        self.assertEqual(self.cli._fmt_dur(45), "45s")
        self.assertEqual(self.cli._fmt_dur(3725), "62m 5s")
        self.assertEqual(self.cli._fmt_dur(None), "")


class CliVerbsTest(unittest.TestCase):
    def setUp(self):
        self.r = Root()
        self.addCleanup(self.r.close)

    def test_status_command_prints_the_reason_and_exits_unhealthy(self):
        i = self.r.item("bad.m4a", "failed", "transcribe_failed", "EMPTY TRANSCRIPT from 9s")
        res = self.r.wax("status")
        self.assertEqual(res.returncode, 2)
        self.assertIn("1 FAILED (need you)", res.stdout)
        self.assertIn(f"wax retry {i} | wax skip {i}", res.stdout)
        self.assertIn("failed_items", res.stdout)
        self.assertNotIn("stranded_work", res.stdout)

    # ---- skip --------------------------------------------------------------
    def test_skip_parks_a_failed_item_and_never_deletes(self):
        i = self.r.item("bad.m4a", "failed", "transcribe_failed", "EMPTY", s3=True)
        res = self.r.wax("skip", i)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"skipped {i} (bad.m4a) (was transcribe_failed): audio preserved at "
                      "~/HeyMa/skipped/bad.m4a", res.stdout)
        self.assertTrue((self.r.root / "skipped" / "bad.m4a").is_file())
        self.assertFalse((self.r.root / "inbox" / "bad.m4a").exists())
        self.assertNotIn("WARNING", res.stderr)  # verified backup exists
        last = self.r.py(f"from wax import ledger; d = ledger.describe_failure('{i}'); "
                         "print(d['cause_code'], '|', d['evidence'])")
        self.assertTrue(last.startswith("operator_skip | previous_cause=transcribe_failed"), last)
        self.assertEqual(self.r.snap()["queue"]["failed"], 0)

    def test_skip_accepts_suspect_and_warns_without_a_verified_backup(self):
        i = self.r.item("odd.m4a", "suspect", "archive_failed", "no s3")
        res = self.r.wax("skip", i)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("WARNING: no verified S3 backup", res.stderr)
        self.assertIn("moved, not deleted", res.stderr)
        self.assertTrue((self.r.root / "skipped" / "odd.m4a").is_file())

    def test_skip_still_takes_waiting_items(self):
        i = self.r.item("wait.m4a", "pending")
        res = self.r.wax("--json", "skip", i)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["state"], "skipped")

    def test_skip_refusals_say_what_to_do(self):
        done = self.r.item("done.m4a", "complete")
        res = self.r.wax("skip", done)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("nothing to skip", res.stderr)
        res = self.r.wax("skip", "nosuchitem")
        self.assertIn("no item with id nosuchitem (see wax queue)", res.stderr)
        self.assertTrue((self.r.root / "inbox" / "done.m4a").is_file())

    def test_skip_of_a_failed_item_whose_audio_is_gone_refuses_without_inventing_a_move(self):
        i = self.r.item("gone.m4a", "failed", "x", "y")
        (self.r.root / "inbox" / "gone.m4a").unlink()
        res = self.r.wax("skip", i)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("audio is missing", res.stderr)

    # ---- retry -------------------------------------------------------------
    def test_bare_retry_is_an_error_that_lists_failed_items(self):
        i = self.r.item("bad.m4a", "failed", "transcribe_failed", "EMPTY TRANSCRIPT")
        res = self.r.wax("retry")
        self.assertEqual(res.returncode, 2)
        self.assertIn("needs an item id, or --all", res.stderr)
        self.assertIn(i, res.stderr)
        self.assertIn("transcribe_failed", res.stderr)
        self.assertEqual(self.r.snap()["queue"]["failed"], 1)  # nothing was retried

    def test_retry_by_id_prints_previous_evidence_and_all_still_works(self):
        a = self.r.item("a.m4a", "failed", "transcribe_failed", "EMPTY TRANSCRIPT from 4748s")
        self.r.item("b.m4a", "failed", "archive_failed", "no s3")
        res = self.r.wax("retry", a)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"requeued {a} (a.m4a) (was transcribe_failed)", res.stdout)
        self.assertIn("previously: EMPTY TRANSCRIPT from 4748s", res.stdout)
        self.assertIn("from scratch", res.stdout)
        res = self.r.wax("retry", "--all")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("(was archive_failed)", res.stdout)
        self.assertEqual(self.r.snap()["queue"]["failed"], 0)

    def test_retry_of_an_unknown_or_waiting_item_is_actionable(self):
        w = self.r.item("w.m4a", "pending")
        self.assertIn("already waiting for the worker", self.r.wax("retry", w).stderr)
        self.assertIn("no item with id zzz (see wax queue)", self.r.wax("retry", "zzz").stderr)

    # ---- queue / items / history / drain -----------------------------------
    def test_queue_default_is_human_and_hides_done_rows(self):
        bad = self.r.item("bad.m4a", "failed", "transcribe_failed", "EMPTY TRANSCRIPT", body=b"b" * 9)
        self.r.item("wait.m4a", "pending")
        res = self.r.wax("queue")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(res.stdout.lstrip().startswith(("[", "{")), res.stdout)
        self.assertIn("NEEDS ATTENTION (1)", res.stdout)
        self.assertIn("WAITING (1)", res.stdout)
        self.assertIn(f"-> wax retry {bad} | wax skip {bad}", res.stdout)
        self.assertIn("transcribe_failed: EMPTY TRANSCRIPT", res.stdout)

    def test_queue_json_is_still_the_raw_row_array(self):
        self.r.item("bad.m4a", "failed", "transcribe_failed", "EMPTY")
        rows = json.loads(self.r.wax("queue", "--json").stdout)
        self.assertIsInstance(rows, list)
        self.assertEqual(rows[0]["error"]["cause_code"], "transcribe_failed")

    def test_queue_done_rows_need_all(self):
        i = self.r.item("fin.m4a", "complete")
        self.r.py(f"""
            from wax import ledger
            c = ledger.connect()
            c.execute("INSERT INTO transcripts(item_id,md_path,created_at) VALUES(?,?,?)", ('{i}', '/x/y.md', '2026-11-18T00:00:00Z'))
        """)
        # a `complete` file still in inbox/ is not a queued row; use a parked one for the done list
        self.r.py(f"from wax import ledger; ledger.connect().execute(\"UPDATE items SET path='/gone/fin.m4a', updated_at='2999-01-01T00:00:00Z' WHERE item_id='{i}'\")")
        plain = self.r.wax("queue").stdout
        full = self.r.wax("queue", "--all").stdout
        self.assertIn("1 finished item(s) hidden", plain)
        self.assertNotIn("fin.m4a", plain)
        self.assertIn("DONE (1)", full)
        self.assertIn("fin.m4a", full)

    def test_items_is_live_only_by_default_and_says_so(self):
        self.r.item("live.m4a", "failed", "x", "y")
        ghost = self.r.item("ghost.m4a", "pending")
        (self.r.root / "inbox" / "ghost.m4a").unlink()
        plain = self.r.wax("items")
        self.assertIn("1 (audio still on disk)", plain.stdout)
        self.assertIn("plus 1 historical row(s) whose file is gone (wax items --all)", plain.stdout)
        self.assertEqual(json.loads(self.r.wax("--json", "items").stdout)["total"], 1)
        self.assertEqual(json.loads(self.r.wax("--json", "items", "--all").stdout)["total"], 2)
        self.assertIn("2 (every row ever minted", self.r.wax("items", "--all").stdout)
        self.assertTrue(ghost)

    def test_items_id_explains_a_failed_item(self):
        i = self.r.item("bad.m4a", "failed", "transcribe_failed", "EMPTY TRANSCRIPT from 4748s")
        payload = json.loads(self.r.wax("--json", "items", "--id", i).stdout)
        self.assertEqual(payload["error"]["cause_code"], "transcribe_failed")
        self.assertEqual(payload["error"]["evidence"], "EMPTY TRANSCRIPT from 4748s")
        self.assertIn("at", payload["error"])
        human = self.r.wax("items", "--id", i).stdout
        self.assertIn("transcribe_failed", human)
        self.assertIn(f"wax retry {i} | wax skip {i}", human)
        ok = self.r.item("ok.m4a", "complete")
        self.assertIsNone(json.loads(self.r.wax("--json", "items", "--id", ok).stdout)["error"])

    def test_history_shows_evidence_under_failed_rows(self):
        self.r.item("bad.m4a", "failed", "transcribe_failed", "EMPTY TRANSCRIPT from 4748s\nlater lines")
        out = self.r.wax("history").stdout
        self.assertIn("why: EMPTY TRANSCRIPT from 4748s", out)
        self.assertNotIn("later lines", out)

    def test_drain_idle_names_the_failed_items_it_skips(self):
        i = self.r.item("bad.m4a", "failed", "transcribe_failed", "EMPTY TRANSCRIPT")
        res = self.r.wax("drain")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(res.stdout.lstrip().startswith(("[", "{")), res.stdout)
        self.assertIn("nothing to process", res.stdout)
        self.assertIn("1 failed item(s) in the inbox are skipped by drain", res.stdout)
        self.assertIn(f"wax retry {i}", res.stdout)
        payload = json.loads(self.r.wax("--json", "drain").stdout)
        self.assertEqual(payload["drained"], 0)
        self.assertEqual(payload["failed_skipped"][0]["item_id"], i)

    def test_drain_dry_run_does_not_pull_dropoff(self):
        drop = self.r.root / "dropoff"
        drop.mkdir()
        (drop / "received.m4a").write_bytes(b"from syncthing")
        res = self.r.wax("drain", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("dropoff/ not pulled", res.stdout)
        self.assertEqual([p.name for p in (self.r.root / "inbox").iterdir()], [])
        self.assertTrue((drop / "received.m4a").is_file())  # dropoff keeps what it received
        payload = json.loads(self.r.wax("--json", "drain", "--dry-run").stdout)
        self.assertTrue(payload["dry_run"])
        self.assertNotEqual(payload["note"], "nothing pending")   # a dry run did not look for "pending"

    def test_inbox_control_help_explains_itself(self):
        # `wax <cmd> --help` prints the parser's description, not the one-line help= entry, so the
        # operator who asks the command itself must get the behaviour, not just the flags.
        def helptext(cmd: str) -> str:   # argparse wraps the description; compare words, not lines
            return " ".join(self.r.wax(cmd, "--help").stdout.split())
        drain = helptext("drain")
        self.assertIn("dropoff/", drain)
        self.assertIn("never touches failed", drain)
        self.assertIn("lists the failed items", helptext("retry"))
        self.assertIn("never deleted", helptext("skip"))
        self.assertIn("needs attention", helptext("queue"))

    def test_an_item_without_a_verified_backup_is_never_offered_skip_as_next_step(self):
        # archive_failed / s3_reverify_failed: retry re-archives; skip would only move the one copy.
        i = self.r.item("unbacked.m4a", "failed", "archive_failed", "mc cp: connection refused")
        status = self.r.wax("status").stdout
        queue = self.r.wax("queue").stdout
        for out in (status, queue):
            self.assertIn(f"wax retry {i} (re-archives", out)
            self.assertNotIn(f"wax skip {i}", out)


if __name__ == "__main__":
    unittest.main()
