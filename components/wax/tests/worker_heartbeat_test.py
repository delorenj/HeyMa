"""Worker liveness: the heartbeat is written by a live thread every beat.

The review finding this guards against: the enabled branch of the old loop
`continue`d past the heartbeat write, so the busiest worker (mid-item) was
exactly the one whose heartbeat went stale — and a tick counter is not
liveness. These tests drive the Worker tick disabled AND enabled, and prove
the heartbeat keeps advancing DURING a long run.
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import documents, ledger, passes, paths, pipelines, state, worker


def _close_test_connection() -> None:
    connection = getattr(ledger._local, "conn", None)
    if connection is not None:
        connection.close()
        del ledger._local.conn


def _beat() -> dict:
    payload = documents.read_worker_heartbeat()
    try:
        raw = json.loads(documents.worker_heartbeat_path().read_text())
    except (OSError, ValueError):
        raw = {}
    return {"view": payload, "raw": raw}


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    result = None
    while time.monotonic() < deadline:
        result = _beat()
        if predicate(result):
            return result
        time.sleep(0.02)
    return result


class WorkerHeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = self.root / "passes.d"
        self.pipes = self.root / "pipelines.d"
        for d in (self.root / "var", self.registry, self.pipes):
            d.mkdir(parents=True)
        self.stack = [
            patch.object(paths, "VAR", self.root / "var"),
            patch.object(paths, "DB", self.root / "var/wax.db"),
            patch.object(passes, "REGISTRY_DIR", self.registry),
            patch.object(pipelines, "PIPELINES_DIR", self.pipes),
            patch.object(worker.Worker, "HEARTBEAT_INTERVAL_S", 0.05),
            patch.object(worker, "sweep_failed_passes", return_value={"candidates": 0}),
            patch.object(documents, "scan", return_value={}),
        ]
        for p in self.stack:
            p.start()
        _close_test_connection()

    def tearDown(self):
        for p in self.stack:
            p.stop()
        _close_test_connection()
        self.tmp.cleanup()

    def _start(self, *, enabled: bool, run_once=lambda: None):
        stack = [
            patch.object(state, "pipeline_enabled", return_value=enabled),
            patch.object(worker, "run_once", side_effect=run_once),
        ]
        for p in stack:
            p.start()
            self.addCleanup(p.stop)
        self._runtime = stack
        w = worker.Worker()
        w.start()
        # Cleanups run LIFO: stop, THEN join. The other way round the join
        # timed out against a live thread, which then ticked into the next
        # test and called its run_once mock.
        self.addCleanup(w.join, 3.0)
        self.addCleanup(w.stop)
        return w

    def test_disabled_pipeline_still_beats(self):
        self._start(enabled=False)
        beat = _wait_for(lambda b: b["view"]["state"] == "live")
        self.assertIsNotNone(beat)
        self.assertFalse(beat["view"]["pipeline_enabled"])
        self.assertIsNone(beat["view"]["busy"])

    def test_enabled_pipeline_beats_through_the_continue_path(self):
        # the old bug: run_once() returning None hit `continue` and skipped
        # the heartbeat write entirely
        self._start(enabled=True)
        beat = _wait_for(lambda b: b["view"]["state"] == "live"
                         and b["view"]["pipeline_enabled"])
        self.assertIsNotNone(beat)

    def test_heartbeat_advances_during_a_long_run(self):
        started = threading.Event()
        release = threading.Event()

        def long_run():
            started.set()
            release.wait(5.0)
            return {"item_id": "x", "worked": True}

        self._start(enabled=True, run_once=long_run)
        first = _wait_for(lambda b: b["view"].get("busy") == "inbox"
                          and b["view"]["state"] == "live")
        self.assertIsNotNone(first, "busy heartbeat while an item runs")
        self.assertTrue(started.wait(2.0))
        # mid-run: the beat keeps moving (a live thread writes it), the tick
        # legitimately does not — liveness is the thread, not the counter
        second = _wait_for(lambda b: b["view"].get("busy") == "inbox"
                           and b["raw"].get("at") != first["raw"].get("at"))
        self.assertIsNotNone(second, "the heartbeat must advance mid-run")
        self.assertEqual(second["view"]["tick"], first["view"]["tick"],
                         "one tick, many beats: the beat is the liveness proof")
        self.assertTrue(second["view"]["documents"]["interval_s"] > 0)
        release.set()

    def test_tick_once_is_driven_and_idle_without_pipeline(self):
        # deterministic, threadless: a disabled tick is idle and not busy
        with patch.object(state, "pipeline_enabled", return_value=False), \
                patch.object(worker, "run_once", return_value=None) as run:
            w = worker.Worker()
            self.assertFalse(w._tick_once())
            run.assert_not_called()
            self.assertIsNone(w._busy)
        with patch.object(state, "pipeline_enabled", return_value=True), \
                patch.object(worker, "run_once", return_value=None) as run:
            self.assertFalse(w._tick_once())  # idle: no work happened
            run.assert_called_once()
            self.assertIsNone(w._busy, "busy is cleared after the run")


if __name__ == "__main__":
    unittest.main()
