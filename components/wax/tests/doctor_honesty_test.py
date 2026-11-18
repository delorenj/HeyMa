"""`wax doctor` probes the OUTPUT (recordings -> transcripts), not just dependencies.

Born from three Voice Memos that failed while `wax status` exited 2 and `wax doctor` reported
nothing wrong: no probe looked at item state. Every test here keys on item state failed|suspect and
on measured level, never on a cause or an id, so the next failure from any source is covered too.

Also pins the waxd descriptor leak: one SQLite connection per thread (threading.local) was never
closed when a short-lived thread exited, so waxd reached ~15k descriptors on wax.db and wax.db-wal.
"""
import json
import textwrap
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cli_honesty_test import COMPONENT_ROOT, Root, load_cli

PROBE_DRIVER = """
import json, sys
from importlib.machinery import SourceFileLoader
import importlib.util
loader = SourceFileLoader("wax_cli_probe", {wax!r})
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec); loader.exec_module(cli)
rows = []
{body}
print(json.dumps(rows, default=str))
"""


class Doctor(Root):
    def probes(self, body: str) -> list[dict]:
        code = PROBE_DRIVER.format(wax=str(COMPONENT_ROOT / "bin" / "wax"), body=textwrap.dedent(body))
        return json.loads(self.py(code))

    def failed_items(self, now_offset_s: float = 0.0) -> dict:
        (row,) = self.probes(f"import time; cli.probe_failed_items(rows.append, now=time.time() + {now_offset_s})")
        return row

    def age(self, item_id: str, minutes: float) -> None:
        """Backdate the failure so it reads as having sat for `minutes`."""
        self.py(f"""
            from wax import ledger
            ts = '2026-01-01T00:00:00Z'
            c = ledger.connect()
            c.execute("UPDATE items SET updated_at=?, first_seen=? WHERE item_id=?", (ts, ts, {item_id!r}))
            c.execute("UPDATE transitions SET at=? WHERE subject=?", (ts, {item_id!r}))
        """)


class FailedItemsProbeTest(unittest.TestCase):
    def setUp(self):
        self.r = Doctor()
        self.addCleanup(self.r.close)

    def test_clean_inbox_passes(self):
        self.r.item("done.m4a", "complete")
        row = self.r.failed_items()
        self.assertEqual(row["status"], "pass")

    def test_any_failed_item_warns_with_next_command_grouped_by_cause(self):
        a = self.r.item("one.m4a", "failed", "transcribe_failed", "EMPTY TRANSCRIPT from 100s\ndetail")
        self.r.item("two.m4a", "failed", "transcribe_failed", "EMPTY TRANSCRIPT from 200s")
        b = self.r.item("odd.m4a", "suspect", "a_brand_new_cause", "whatever")
        row = self.r.failed_items()
        self.assertEqual(row["status"], "warn")
        self.assertIn("3 failed: 2x transcribe_failed, 1x a_brand_new_cause", row["detail"])
        self.assertEqual(len(row["lines"]), 2)          # grouped by cause, not one per item
        self.assertTrue(row["lines"][0].startswith("2x transcribe_failed: EMPTY TRANSCRIPT"))
        self.assertIn("wax retry", row["lines"][0])
        self.assertIn("wax skip", row["lines"][0])
        self.assertIn(f"{b[:12]}", row["lines"][1])      # any cause is covered, not just this one
        self.assertIn("wax queue", row["fix"])

    def test_a_failed_item_older_than_an_hour_fails(self):
        a = self.r.item("old.m4a", "failed", "transcribe_failed", "x")
        self.r.age(a, 600)
        row = self.r.failed_items()
        self.assertEqual(row["status"], "fail")
        self.assertIn("with nothing to retry it", row["detail"])

    def test_lost_backup_causes_fail_immediately(self):
        self.r.item("nobackup.m4a", "failed", "s3_reverify_failed", "etag mismatch")
        row = self.r.failed_items()
        self.assertEqual(row["status"], "fail")
        self.assertIn("no verified backup", row["detail"])
        self.assertIn("wax archive", row["lines"][0])

    def test_failed_row_whose_audio_is_gone_is_history_not_an_inbox_item(self):
        gone = self.r.item("gone.m4a", "failed", "transcribe_failed", "x")
        (self.r.root / "inbox" / "gone.m4a").unlink()
        row = self.r.failed_items()
        self.assertEqual(row["status"], "pass")
        self.assertIn("1 older failed row(s) have no audio in the inbox", row["detail"])

    def test_nested_inbox_items_count(self):
        self.r.item("deep.m4a", "failed", "decode_failed", "x", sub="2026/11")
        self.assertEqual(self.r.failed_items()["status"], "warn")


class ParityTest(unittest.TestCase):
    """doctor must never be greener than status, and must agree with `wax failures` on which items."""

    def setUp(self):
        self.r = Doctor()
        self.addCleanup(self.r.close)

    def run_cli(self, body: str) -> dict:
        (row,) = self.r.probes(body)
        return row

    def test_doctor_exit_is_at_least_status_exit_for_a_failed_item(self):
        self.r.item("bad.m4a", "failed", "transcribe_failed", "EMPTY TRANSCRIPT")
        status = self.r.wax("status")
        self.assertEqual(status.returncode, 2)          # failed items make status exit 2
        # The failed-items probe alone WARNs (exit 1); the status-snapshot probe is what keeps
        # doctor from being greener than the thing it audits.
        rows = self.r.probes("""
            cli.probe_failed_items(rows.append)
            cli.probe_transcription_health(rows.append)
            cli.probe_silent_input(rows.append)
            cli.probe_status_snapshot(rows.append)
            rows.append({'exit': cli.doctor_exit_code(rows)})
        """)
        self.assertGreaterEqual(rows[-1]["exit"], status.returncode)
        snap = next(r for r in rows if r.get("probe") == "status snapshot")
        self.assertEqual(snap["status"], "fail")
        self.assertIn("failed items in the inbox", snap["detail"])

    def test_clean_ledger_is_green_in_both(self):
        self.r.item("done.m4a", "complete")
        rows = self.r.probes("""
            cli.probe_failed_items(rows.append)
            cli.probe_transcription_health(rows.append)
            cli.probe_silent_input(rows.append)
            cli.probe_status_snapshot(rows.append)
            rows.append({'exit': cli.doctor_exit_code(rows)})
        """)
        self.assertEqual(rows[-1]["exit"], 0)
        self.assertEqual(self.r.wax("status").returncode, 0)

    def test_doctor_and_failures_name_the_same_items(self):
        ids = {self.r.item(f"f{n}.m4a", "failed" if n % 2 else "suspect", "some_cause", "e") for n in range(4)}
        (self.r.root / "inbox" / "f0.m4a")  # still present; plus one history row below
        ghost = self.r.item("ghost.m4a", "failed", "transcribe_failed", "x")
        (self.r.root / "inbox" / "ghost.m4a").unlink()
        out = json.loads(self.r.py("""
            import json
            from wax import failures, ledger
            doctor_ids = sorted(r["item_id"] for r in ledger.failed_inbox_items())
            failure_ids = sorted(r["subject"] for r in failures.collect() if r["kind"] == "item"
                                 and r["hints"]["in_inbox"])
            print(json.dumps([doctor_ids, failure_ids]))
        """))
        self.assertEqual(out[0], out[1])
        self.assertEqual(set(out[0]), ids)
        self.assertNotIn(ghost, out[0])

    def test_status_terms_name_why_it_is_unhealthy(self):
        cli = load_cli()
        self.assertEqual(cli.status_unhealthy_terms({}), [])
        terms = cli.status_unhealthy_terms({"queue": {"failed": 3}, "transcription": {"degraded": True}})
        self.assertEqual(terms, ["failed items in the inbox (3)", "transcription degraded"])
        self.assertTrue(cli.status_unhealthy({"transcription": {"degraded": True}}))


class TranscriptionHealthTest(unittest.TestCase):
    def setUp(self):
        self.r = Doctor()
        self.addCleanup(self.r.close)

    def health(self) -> dict:
        return json.loads(self.r.py("import json; from wax import ledger; "
                                    "print(json.dumps(ledger.transcription_health()))"))

    def mint(self, name, state, minutes_ago, cause=""):
        i = self.r.item(name, state, cause, "evidence")
        self.r.py(f"""
            from wax import ledger
            import datetime
            t = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes={minutes_ago})
                 ).strftime('%Y-%m-%dT%H:%M:%SZ')
            ledger.connect().execute("UPDATE items SET first_seen=?, updated_at=? WHERE item_id=?", (t, t, {i!r}))
        """)
        return i

    def test_newest_three_failed_and_old_enough_is_degraded(self):
        for n in range(3):
            self.mint(f"f{n}.m4a", "failed", 60 - n, "transcribe_failed")
        for n in range(3):
            self.mint(f"ok{n}.m4a", "complete", 300 - n)
        h = self.health()
        self.assertTrue(h["degraded"])
        self.assertEqual(h["recent_failed"], 3)
        self.assertEqual({r["cause_code"] for r in h["newest_failed"]}, {"transcribe_failed"})

    def test_three_quick_failures_do_not_flash_degraded_inside_five_minutes(self):
        for n in range(3):
            self.mint(f"f{n}.m4a", "failed", 2, "transcribe_failed")
        h = self.health()
        self.assertFalse(h["degraded"])
        self.assertEqual(h["recent_failed"], 3)

    def test_a_good_newest_recording_clears_it(self):
        for n in range(3):
            self.mint(f"f{n}.m4a", "failed", 60 - n, "transcribe_failed")
        self.mint("fresh.m4a", "complete", 1)
        self.assertFalse(self.health()["degraded"])

    def test_skipped_items_are_decisions_not_outcomes(self):
        for n in range(3):
            self.mint(f"f{n}.m4a", "failed", 60 - n, "transcribe_failed")
        for n in range(4):
            self.mint(f"silent{n}.m4a", "skipped", 1, "junk_silent")   # silence parks, never masks the run
        self.assertTrue(self.health()["degraded"])

    def test_a_short_ledger_is_not_degraded(self):
        self.mint("only.m4a", "failed", 60, "transcribe_failed")
        self.assertFalse(self.health()["degraded"])

    def test_snapshot_and_status_line_carry_it(self):
        for n in range(3):
            self.mint(f"f{n}.m4a", "failed", 60 - n, "transcribe_failed")
        snap = self.r.snap()
        self.assertTrue(snap["transcription"]["degraded"])
        out = self.r.wax("status").stdout
        self.assertIn("transcription DEGRADED the newest 3 recordings produced no transcript", out)
        probes = self.r.probes("cli.probe_transcription_health(rows.append)")
        self.assertEqual(probes[0]["status"], "fail")
        self.assertIn("transcribe_failed", probes[0]["detail"])


class SilentInputProbeTest(unittest.TestCase):
    def setUp(self):
        self.r = Doctor()
        self.addCleanup(self.r.close)

    def silent(self, name, origin="dropoff"):
        i = self.r.item(name, "pending")
        self.r.py(f"""
            from wax import ledger
            c = ledger.connect()
            c.execute("UPDATE items SET origin=? WHERE item_id=?", ({origin!r}, {i!r}))
            ledger.record_audio_levels({i!r}, {{"mean_db": -80.0, "max_db": -45.0}})
            ledger.set_item_state({i!r}, "skipped", cause="junk_silent", evidence="silent audio")
        """)
        return i

    def probe(self):
        return self.r.probes("cli.probe_silent_input(rows.append)")[0]

    def test_nothing_silent_passes(self):
        self.assertEqual(self.probe()["status"], "pass")

    def test_a_silent_recording_warns_per_origin(self):
        self.silent("a.m4a")
        row = self.probe()
        self.assertEqual(row["status"], "warn")
        self.assertIn("1 from dropoff", row["detail"])
        self.assertIn("mean -80.0 dB", row["lines"][0])

    def test_three_in_a_row_from_one_source_fails(self):
        for n in range(3):
            self.silent(f"s{n}.m4a")
        row = self.probe()
        self.assertEqual(row["status"], "fail")
        self.assertIn("dropoff is 3 in a row", row["detail"])


class InferenceChainRowTest(unittest.TestCase):
    """The four permanent per-member WARNs are one row, and the verdict prints first."""

    SOURCE = (COMPONENT_ROOT / "bin" / "wax").read_text()

    def test_no_per_member_fallback_rows(self):
        self.assertNotIn('f"inference {role}"', self.SOURCE)
        self.assertIn("effective chain = ", self.SOURCE)
        self.assertEqual(self.SOURCE.count('_probe("inference chain"'), 3)   # banned / ok / degraded

    def test_verdict_is_printed_before_the_probe_tables(self):
        body = self.SOURCE[self.SOURCE.index("def doctor(as_json"):]
        self.assertLess(body.index("verdict    "), body.index('print("PROBES")'))
        self.assertLess(body.index("verdict    "), body.index('print("CONFIGURATION")'))


class WaxdFdProbeTest(unittest.TestCase):
    def setUp(self):
        self.cli = load_cli()

    def run_probe(self, pid, count=None):
        rows = []
        with patch.object(self.cli.ledger, "open_fd_count", return_value=count):
            self.cli.probe_waxd_fds(rows.append, pid)
        return rows[0]

    def test_leaking_daemon_warns(self):
        row = self.run_probe(123, 15000)
        self.assertEqual(row["status"], "warn")
        self.assertIn("15000 descriptors", row["detail"])
        self.assertIn("ledger.close()", row["fix"])

    def test_normal_daemon_passes(self):
        self.assertEqual(self.run_probe(123, 12)["status"], "pass")

    def test_unreadable_proc_warns_and_no_daemon_passes(self):
        self.assertEqual(self.run_probe(123, -1)["status"], "warn")
        self.assertEqual(self.run_probe(None, 0)["status"], "pass")


class ConnectionLeakTest(unittest.TestCase):
    """N short-lived threads that use the ledger must not grow the descriptor count."""

    def setUp(self):
        from wax import ledger
        self.ledger = ledger
        ledger.connect().execute("SELECT 1").fetchall()      # the main thread's own pair
        self.base = ledger.open_fd_count()

    def spawn(self, n, target):
        for _ in range(n):
            t = threading.Thread(target=target)
            t.start()
            t.join()

    def use(self):
        self.ledger.connect().execute("SELECT 1").fetchall()

    def test_threads_that_just_exit_do_not_leak(self):
        self.spawn(60, self.use)
        self.assertLessEqual(self.ledger.open_fd_count(), self.base + 2)

    def test_close_releases_the_threads_connection_now(self):
        seen = []

        def work():
            self.use()
            seen.append(self.ledger.open_fd_count())
            self.ledger.close()
            seen.append(self.ledger.open_fd_count())
        self.spawn(1, work)
        self.assertGreater(seen[0], seen[1])
        self.assertLessEqual(seen[1], self.base + 2)

    def test_connect_after_close_reopens(self):
        self.ledger.close()
        self.assertEqual(self.ledger.connect().execute("SELECT 41+1").fetchone()[0], 42)

    def test_telephony_snapshot_closes_its_read_connection(self):
        from wax import calls, telephony
        calls.db()
        with patch.object(telephony, "_core_status", return_value={"mode": "shadow"}):
            before = self.ledger.open_fd_count()
            for _ in range(30):
                telephony.snapshot(limit=5)
        self.assertLessEqual(self.ledger.open_fd_count(), before + 2)

    def test_waxd_short_lived_threads_close_the_ledger(self):
        source = (COMPONENT_ROOT / "bin" / "waxd").read_text()
        self.assertEqual(source.count("ledger.close()"), 4)       # telephony refresh + skip/retry/retry_passes


if __name__ == "__main__":
    unittest.main()
