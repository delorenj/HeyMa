"""`wax split` on a capture that is still recording (HEYMA-18).

A real ffmpeg is the encoder: it holds a FIFO as stdin, writes closed Ogg segments into stream/<rid>.segs/
and is stopped by `q` exactly as `capture.stop` does it. The segments recorded "earlier" are the fixture
meeting cut into 10 s files; the live encoder then keeps appending (very quiet) segments, so the stream is
a meeting followed by a growing overrun. Capture state is read before and after through `state.stream_state`.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import split_support as sup
from wax import ledger, paths, procutil, sentinel, split, state

PARAMS = split.Params(**sup.SCALED)
RID = "20261113-120000-abc123"
PREFILL_S = 10


class LiveCaptureTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        ctx = sup.runtime(self.root)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.encoder = None
        self.ctl_fd = None
        self.addCleanup(self._reap)

    def _reap(self):
        if self.ctl_fd is not None:
            try:
                os.close(self.ctl_fd)
            except OSError:
                pass
        if self.encoder is not None and self.encoder.poll() is None:
            self.encoder.kill()
        if self.encoder is not None:
            self.encoder.wait(timeout=10)

    def start_capture(self, fixture: str, *, live_segments: int = 3) -> Path:
        """Prefill the segment dir from `fixture`, then start the live encoder and wait for closed segments."""
        segdir = paths.segdir(RID)
        segdir.mkdir(parents=True)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(sup.fixture_ogg(fixture)),
                        "-c", "copy", "-f", "segment", "-segment_time", str(PREFILL_S), "-segment_format", "ogg",
                        "-reset_timestamps", "1", str(segdir / "seg-%05d.ogg")], check=True)
        prefilled = len(list(segdir.glob("seg-*.ogg")))
        ctl = paths.ctl_path(RID)
        os.mkfifo(ctl)
        self.ctl_fd = os.open(ctl, os.O_RDWR)
        self.encoder = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i",
             "sine=frequency=60:sample_rate=48000:duration=300", "-af", "volume=-80dB", "-ac", "1",
             "-c:a", "libopus", "-f", "segment", "-segment_time", "1", "-segment_format", "ogg",
             "-segment_start_number", str(prefilled), "-reset_timestamps", "1", str(segdir / "seg-%05d.ogg")],
            stdin=self.ctl_fd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sentinel._write_json_durable(paths.rec_path(RID), {
            "rid": RID, "pid": self.encoder.pid, "starttime": procutil.proc_starttime(self.encoder.pid),
            "boot_id": procutil.boot_id(), "target_name": "meeting-live.ogg", "device_source": "test",
            "started_at": sentinel.utcnow(), "trigger": "test", "channels": 1, "sample_rate": 48000})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if len(list(segdir.glob("seg-*.ogg"))) >= prefilled + live_segments:
                return segdir
            time.sleep(0.1)
        self.fail("the live encoder did not close its segments")

    def stream(self) -> dict:
        return state.stream_state(run_preflight=False)

    def test_live_split_stops_finalizes_and_holds_without_restarting(self):
        segdir = self.start_capture("meeting_overrun_quiet")
        before = self.stream()
        self.assertEqual((before["state"], before["rid"]), ("recording", RID))
        segments_before = sorted(p.name for p in segdir.glob("seg-*.ogg"))

        report = split.run(RID, PARAMS, halt_timeout_s=30)

        self.assertEqual(report["outcome"], "split")
        self.assertEqual(report["capture"]["state_before"], "recording")
        after = self.stream()
        self.assertNotEqual(after["state"], "recording", "the capture was orderly-stopped")
        self.assertEqual(report["capture"]["state_after"], after["state"])
        self.assertIsNotNone(self.encoder.wait(timeout=15))
        self.assertEqual(self.encoder.returncode, 0, "q on the FIFO: a clean encoder exit, not a signal")
        hold = Path(report["held_dir"])
        final = hold / "meeting-live.ogg"
        self.assertTrue(final.is_file(), "finalized into the hold, not the inbox")
        self.assertEqual(list(paths.INBOX.iterdir()), [], "the worker never sees the unsplit recording")
        self.assertEqual(report["capture"]["segments_skipped"], 0)
        self.assertGreaterEqual(report["capture"]["segments_used"], len(segments_before) - 1)
        self.assertEqual(ledger.sha256_file(final), report["source_sha256"])
        segs = report["segments"]
        self.assertEqual([s["kind"] for s in segs], ["active", "quiet-tail"])
        truth = sup.fixture_info("meeting_overrun_quiet")["overrun_at_s"]
        self.assertAlmostEqual(report["detection"]["boundary"]["quiet_start_s"], truth, delta=3.0)
        self.assertAlmostEqual(sum(s["duration_s"] for s in segs), report["detection"]["duration_s"], delta=2.5)
        # no audio lost: the finalized file covers everything the encoder wrote before it was stopped
        self.assertGreater(report["detection"]["duration_s"], sup.fixture_info("meeting_overrun_quiet")["seconds"])
        manifest = json.loads(Path(report["manifest"]).read_text())
        self.assertEqual(manifest["origin"], "live-capture")
        self.assertEqual(manifest["source"]["kind"], "capture")
        self.assertEqual(manifest["source"]["rid"], RID)
        self.assertEqual(manifest["source"]["sha256"], report["source_sha256"])
        # not restarted: no new rec.json, no encoder, nothing recording
        self.assertNotEqual(self.stream()["state"], "recording")
        self.assertIsNotNone(self.encoder.poll(), "and no encoder is running")

    def test_live_dry_run_and_no_boundary_leave_the_capture_recording(self):
        self.start_capture("meeting_overrun_quiet")
        report = split.run(RID, PARAMS, dry_run=True)
        self.assertEqual(report["outcome"], "proposed")
        self.assertEqual(report["capture"]["state_after"], "recording")
        self.assertEqual(self.stream()["state"], "recording")
        self.assertIsNone(self.encoder.poll(), "the encoder was not stopped")
        self.assertFalse(paths.stop_path(RID).exists(), "no stop was requested")
        self.assertFalse(split.split_root().exists())

    def test_live_no_boundary_leaves_the_capture_recording(self):
        self.start_capture("continuous_activity")
        report = split.run(RID, PARAMS)
        self.assertEqual(report["outcome"], "no_boundary")
        self.assertEqual(split.exit_code(report), 3)
        self.assertEqual(report["capture"]["state_before"], "recording")
        self.assertEqual(report["capture"]["state_after"], "recording")
        self.assertIsNone(self.encoder.poll(), "still encoding")
        self.assertEqual(self.stream()["state"], "recording")
        self.assertFalse(split.split_root().exists())
        self.assertEqual(list(paths.INBOX.iterdir()), [])

    def test_no_target_means_the_active_capture(self):
        self.start_capture("meeting_overrun_quiet")
        self.assertEqual(split.resolve(None).rid, RID)

    def test_an_unknown_capture_is_an_error_and_touches_nothing(self):
        with self.assertRaises(split.SplitError):
            split.run(RID, PARAMS)
        self.assertEqual(list(paths.INBOX.iterdir()), [])

    def test_capture_lock_is_taken_around_the_stop(self):
        self.start_capture("meeting_overrun_quiet")
        events = []

        class Lock:
            def __enter__(self_inner):
                events.append(("lock", self.stream()["state"]))

            def __exit__(self_inner, *exc):
                events.append(("unlock", self.stream()["state"]))

        split.run(RID, PARAMS, capture_lock=Lock)
        self.assertEqual(events[0], ("lock", "recording"), "re-checked and stopped under the CLI capture lock")
        self.assertNotEqual(events[1][1], "recording")


if __name__ == "__main__":
    unittest.main()
