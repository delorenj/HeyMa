"""`wax split` against a waxd that predates the fence (HEYMA-18 validation, gap 4).

Production waxd keeps running the pre-fence worker until it is restarted. That worker never reads
`item_fences`, does not track its transcriber or pass children and publishes whatever returns, so the
new CLI cannot halt an item under it. The new waxd advertises `daemon.capabilities` in its snapshot
(socket and state.json); the CLI refuses the halting path unless the running daemon does.

The "daemon" here is a real process whose argv0 is `waxd` plus a real unix socket serving a snapshot,
so the probe runs through its real /proc and socket code. Live captures, `--dry-run`, finished items and
a host with no daemon at all are not affected.
"""

import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import split_live_test
import split_support as sup
from wax import fence, ledger, paths, split

PARAMS = split.Params(**sup.SCALED)


class FakeDaemon:
    """A process named waxd, its singleton lock file, its status socket and its state.json mirror."""

    def __init__(self, snapshot: dict, *, answers_socket: bool = True):
        self.snapshot = snapshot
        self.answers_socket = answers_socket
        # argv carries "waxd" the way `python3 .../bin/waxd` does; the probe reads /proc/<pid>/cmdline
        self.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", "waxd"])
        paths.VAR.mkdir(parents=True, exist_ok=True)
        (paths.VAR / "waxd.singleton.lock").write_text(f"{self.proc.pid}\n")
        daemon = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.sendall(json.dumps(daemon.snapshot).encode() + b"\n")

        self.server = None
        if answers_socket:
            paths.SOCK.unlink(missing_ok=True)
            self.server = socketserver.ThreadingUnixStreamServer(str(paths.SOCK), Handler)
            threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def mirror(self, snapshot: dict) -> None:
        paths.STATE_JSON.write_text(json.dumps(snapshot))

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.proc.kill()
        self.proc.wait(timeout=10)


class GuardBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        ctx = sup.runtime(self.root)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.daemon = None
        self.addCleanup(self._stop)

    def _stop(self):
        if self.daemon:
            self.daemon.stop()

    def start_daemon(self, snapshot, **kwargs) -> FakeDaemon:
        self.daemon = FakeDaemon(snapshot, **kwargs)
        return self.daemon

    def queued_item(self, state: str = "pending"):
        import shutil
        dest = paths.INBOX / "meeting.ogg"
        shutil.copy2(sup.fixture_ogg("meeting_overrun_quiet"), dest)
        item_id = ledger.upsert_item(dest, origin="inbox")
        if state != "pending":
            ledger.set_item_state(item_id, state, cause="test")
        return item_id, dest

    def assert_untouched(self, item_id: str, audio: Path, sha: str, state: str) -> None:
        self.assertTrue(audio.is_file(), "the original is where it was")
        self.assertEqual(ledger.sha256_file(audio), sha)
        self.assertIsNone(ledger.fence_of(item_id), "no fence was raised")
        self.assertEqual(ledger.connect().execute("SELECT state FROM items WHERE item_id=?",
                                                  (item_id,)).fetchone()["state"], state)
        self.assertFalse(split.split_root().exists(), "nothing was cut or held")
        self.assertEqual(ledger.connect().execute("SELECT COUNT(*) c FROM item_fences").fetchone()["c"], 0)


class OldDaemonRefusalTest(GuardBase):
    OLD_SNAPSHOT = {"stream": {"state": "ready"}, "inbox": {"state": "ready"}}     # what a pre-fence waxd serves
    OLD_MIRROR = {**OLD_SNAPSHOT, "daemon": {"pid": 0}}                             # and mirrors: pid only

    def test_refuses_an_item_in_processing_and_changes_nothing(self):
        item_id, audio = self.queued_item("archived")
        sha = ledger.sha256_file(audio)
        self.start_daemon(self.OLD_SNAPSHOT)
        with self.assertRaises(split.SplitError) as ctx:
            split.run(item_id[:10], PARAMS, halt_timeout_s=5)
        message = str(ctx.exception)
        self.assertIn("restart waxd first", message.lower())
        self.assertIn(f"pid {self.daemon.proc.pid}", message)
        self.assertIn("Nothing was fenced, halted, cut or moved", message)
        self.assert_untouched(item_id, audio, sha, "archived")

    def test_refuses_a_queued_item_too_because_the_old_worker_would_pick_it_up(self):
        item_id, audio = self.queued_item("pending")
        sha = ledger.sha256_file(audio)
        self.start_daemon(self.OLD_SNAPSHOT)
        with self.assertRaises(split.SplitError):
            split.run(item_id[:10], PARAMS, halt_timeout_s=5)
        self.assert_untouched(item_id, audio, sha, "pending")

    def test_the_cli_exits_2_with_the_message(self):
        item_id, audio = self.queued_item("archived")
        self.start_daemon(self.OLD_SNAPSHOT)
        done = sup.cli(self.root, "split", item_id[:10], "--min-quiet-s", "30", "--tail-slack-s", "10",
                       "--min-active-s", "20", "--pad-s", "2")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("restart waxd first", (done.stdout + done.stderr).lower())

    def test_a_mirror_from_the_old_daemon_is_not_a_capability(self):
        item_id, audio = self.queued_item("archived")
        self.start_daemon(self.OLD_SNAPSHOT, answers_socket=False)
        self.daemon.mirror({**self.OLD_SNAPSHOT, "daemon": {"pid": self.daemon.proc.pid}})
        with self.assertRaises(split.SplitError):
            split.run(item_id[:10], PARAMS, halt_timeout_s=5)

    def test_a_stale_mirror_from_another_pid_cannot_vouch_for_the_running_daemon(self):
        item_id, audio = self.queued_item("archived")
        self.start_daemon(self.OLD_SNAPSHOT, answers_socket=False)
        self.daemon.mirror({"daemon": {"pid": self.daemon.proc.pid + 7, "capabilities": [fence.CAPABILITY]}})
        with self.assertRaises(split.SplitError):
            split.run(item_id[:10], PARAMS, halt_timeout_s=5)

    def test_a_live_old_socket_beats_a_mirror_that_claims_the_capability(self):
        item_id, audio = self.queued_item("archived")
        self.start_daemon(self.OLD_SNAPSHOT)
        self.daemon.mirror({"daemon": {"pid": self.daemon.proc.pid, "capabilities": [fence.CAPABILITY]}})
        with self.assertRaises(split.SplitError):
            split.run(item_id[:10], PARAMS, halt_timeout_s=5)


class UnaffectedPathsTest(GuardBase):
    OLD_SNAPSHOT = OldDaemonRefusalTest.OLD_SNAPSHOT

    def test_dry_run_still_works_under_an_old_daemon(self):
        item_id, audio = self.queued_item("archived")
        self.start_daemon(self.OLD_SNAPSHOT)
        report = split.run(item_id[:10], PARAMS, dry_run=True)
        self.assertEqual(report["outcome"], "proposed")

    def test_a_finished_item_is_split_under_an_old_daemon(self):
        # complete/skipped items need no halt: there is no in-flight work to stop or publish
        item_id, audio = self.queued_item("complete")
        self.start_daemon(self.OLD_SNAPSHOT)
        report = split.run(item_id[:10], PARAMS, halt_timeout_s=10)
        self.assertEqual(report["outcome"], "split")
        self.assertFalse(report["processing"]["fenced"])

    def test_no_daemon_at_all_is_not_a_refusal(self):
        item_id, audio = self.queued_item("archived")
        self.assertEqual(fence.daemon_probe()["running"], False)
        report = split.run(item_id[:10], PARAMS, halt_timeout_s=10)
        self.assertEqual(report["outcome"], "split")

    def test_a_lock_file_naming_a_dead_pid_is_no_daemon(self):
        item_id, audio = self.queued_item("archived")
        paths.VAR.mkdir(parents=True, exist_ok=True)
        (paths.VAR / "waxd.singleton.lock").write_text("999999999\n")
        self.assertFalse(fence.daemon_probe()["running"])
        self.assertEqual(split.run(item_id[:10], PARAMS, halt_timeout_s=10)["outcome"], "split")

    def test_a_pid_that_is_not_waxd_is_no_daemon(self):
        item_id, audio = self.queued_item("archived")
        paths.VAR.mkdir(parents=True, exist_ok=True)
        (paths.VAR / "waxd.singleton.lock").write_text(f"{os.getpid()}\n")      # the test runner, not waxd
        self.assertFalse(fence.daemon_probe()["running"])

    def test_no_boundary_is_reported_under_an_old_daemon(self):
        import shutil
        dest = paths.INBOX / "talk.ogg"
        shutil.copy2(sup.fixture_ogg("continuous_activity"), dest)
        item_id = ledger.upsert_item(dest, origin="inbox")
        self.start_daemon(self.OLD_SNAPSHOT)
        report = split.run(item_id[:10], PARAMS)
        self.assertEqual(report["outcome"], "no_boundary")
        self.assertEqual(split.exit_code(report), 3)


class NewDaemonTest(GuardBase):
    def test_a_daemon_that_advertises_the_fence_may_halt_an_item(self):
        item_id, audio = self.queued_item("archived")
        self.start_daemon({"daemon": {"pid": 0, "capabilities": [fence.CAPABILITY]}})
        self.daemon.snapshot["daemon"]["pid"] = self.daemon.proc.pid
        probe = fence.daemon_probe()
        self.assertEqual((probe["running"], probe["source"], probe["capabilities"]),
                         (True, "socket", [fence.CAPABILITY]))
        report = split.run(item_id[:10], PARAMS, halt_timeout_s=10)
        self.assertEqual(report["outcome"], "split")
        self.assertTrue(report["processing"]["fenced"])

    def test_the_mirror_is_the_fallback_when_the_socket_does_not_answer(self):
        item_id, audio = self.queued_item("archived")
        self.start_daemon({}, answers_socket=False)
        self.daemon.mirror({"daemon": {"pid": self.daemon.proc.pid, "capabilities": [fence.CAPABILITY]}})
        self.assertEqual(fence.daemon_probe()["source"], "state.json")
        self.assertEqual(split.run(item_id[:10], PARAMS, halt_timeout_s=10)["outcome"], "split")

    def test_waxd_advertises_exactly_what_the_cli_checks_for(self):
        self.assertEqual(fence.daemon_advert(123), {"pid": 123, "capabilities": [fence.CAPABILITY]})
        source = (Path(__file__).resolve().parents[1] / "bin" / "waxd").read_text()
        self.assertIn('snap["daemon"] = fence.daemon_advert()', source, "the socket snapshot carries it")
        self.assertIn('payload["daemon"] = fence.daemon_advert()', source, "and so does the state.json mirror")


class LiveCaptureUnderAnOldDaemon(split_live_test.LiveCaptureTest):
    """The live-capture path never halts an item, so it must work under any daemon. Real ffmpeg encoder."""

    def setUp(self):
        super().setUp()
        self.old = FakeDaemon(OldDaemonRefusalTest.OLD_SNAPSHOT)
        self.addCleanup(self.old.stop)

    # keep the one end-to-end live split and the no-boundary case; the rest of the live suite is already run
    test_live_dry_run_and_no_boundary_leave_the_capture_recording = None
    test_a_failure_after_the_stop_releases_the_recording_to_the_inbox = None
    test_no_target_means_the_active_capture = None
    test_an_unknown_capture_is_an_error_and_touches_nothing = None
    test_capture_lock_is_taken_around_the_stop = None


if __name__ == "__main__":
    unittest.main()
