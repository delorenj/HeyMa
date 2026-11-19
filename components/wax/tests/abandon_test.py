"""A capture abandoned in silence is stopped by waxd (2026-11-18 incident: 16 h on a muted Yeti, then the next
record press became a STOP of the ghost and the interview was never recorded).

The evidence is the encoder's silencedetect output on the encoder's own clock; the verdict is a silent FRACTION
over a long trailing window, so noise blips (every one prints a silence_end) cannot make it flap, a meeting's
5-30 s pauses cannot trip it, and a waxd restarted mid-capture replays the log to the same answer.
"""

import importlib.util
import json
import os
import random
import tempfile
import threading
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from wax import abandon, capture, paths, sentinel, state

COMPONENT_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
PARAMS = abandon.Params(window_s=3600.0, fraction=0.97)


def encoder_log(total_s: float, silences: list[tuple[float, float | None]], *, seg: int = 60) -> list[str]:
    """What ffmpeg -loglevel info prints: a segment opening every `seg` s, silencedetect start/end lines.

    A silence with end None is still open at `total_s` (the log has its silence_start only).
    """
    events: list[tuple[float, str]] = [(n * seg, f"[segment @ 0x1] Opening '/x/stream/r.segs/seg-{n:05d}.ogg' for writing")
                                       for n in range(int(total_s // seg) + 1)]
    for a, b in silences:
        events.append((a + 5, f"[silencedetect @ 0x2] silence_start: {a:.6f}"))
        if b is not None:
            events.append((b, f"[silencedetect @ 0x2] silence_end: {b:.6f} | silence_duration: {b - a:.6f}"))
    events.sort(key=lambda e: e[0])
    return ["[aist#0:0/pcm_s16le @ 0x3] Guessed Channel Layout: stereo"] + [text for _t, text in events]


def flapping_silence(total_s: float, *, blips_per_hour: float = 30, blip_s: float = 1, seed: int = 7):
    """Nearly all silence, with noise blips: the incident's shape (630 starts in 16 h, 98.4% silent)."""
    rng = random.Random(seed)
    out, t = [], 0.0
    gap = 3600 / blips_per_hour
    while t < total_s:
        start = t
        t += max(10.0, rng.gauss(gap, gap / 3))
        end = min(t, total_s)
        out.append((start, end if end < total_s else None))
        t = end + blip_s
    return out


class VerdictTest(unittest.TestCase):
    def test_incident_shape_is_abandoned_after_the_first_hour_not_after_sixteen(self):
        # Flapping silence_start/silence_end all night: the single "silent now" bit warned and cleared.
        silences = flapping_silence(16 * 3600)
        young = abandon.replay(encoder_log(3000, [s for s in silences if s[0] < 3000]), PARAMS)
        self.assertFalse(young["abandoned"], "a capture younger than the window has no full window to judge")
        at_75 = abandon.replay(encoder_log(75 * 60, [(a, b if b and b < 75 * 60 else None) for a, b in silences
                                                     if a < 75 * 60]), PARAMS)
        self.assertTrue(at_75["abandoned"], at_75)
        full = abandon.replay(encoder_log(16 * 3600, silences), PARAMS)
        self.assertTrue(full["abandoned"])
        self.assertGreaterEqual(full["fraction"], 0.97)

    def test_meeting_with_long_pauses_is_never_abandoned(self):
        # Two hours: 30 s pauses every few minutes and a 20 min break, which is far above any real pause.
        silences = [(t, t + 30) for t in range(600, 7200, 240)]
        silences.append((3000, 4200))
        self.assertLess(max(abandon.replay(encoder_log(end, [(a, b if b < end else None) for a, b in silences
                                                              if a < end]), PARAMS)["fraction"]
                            for end in range(3600, 7201, 600)), 0.6)
        for end in range(3600, 7201, 300):
            v = abandon.replay(encoder_log(end, [(a, b if b < end else None) for a, b in silences if a < end]), PARAMS)
            self.assertFalse(v["abandoned"], (end, v))

    def test_a_four_minute_meeting_holds_the_verdict_off_until_it_leaves_the_window(self):
        # Silent, then 4 minutes of talk (3000-3240 s), then silent again: 93% silent while the talk is inside
        # the trailing hour, abandoned only once it has scrolled out of it (the hour ending at 6840 s).
        def at(end):
            return abandon.replay(encoder_log(end, [(0, 3000), (3240, None)]), PARAMS)
        self.assertFalse(at(4000)["abandoned"])
        self.assertAlmostEqual(at(4000)["fraction"], 1 - 240 / 3600, places=2)
        self.assertFalse(at(6700)["abandoned"])
        self.assertTrue(at(6900)["abandoned"])

    def test_speech_in_the_trailing_window_defeats_the_verdict(self):
        # Silent for 3 h, then 10 minutes of someone talking: the capture is alive.
        end = 3 * 3600 + 600
        v = abandon.replay(encoder_log(end, [(0, 3 * 3600)]), PARAMS)
        self.assertFalse(v["abandoned"], v)
        self.assertAlmostEqual(v["fraction"], 1 - 600 / 3600, places=2)

    def test_open_silence_extends_to_now(self):
        v = abandon.replay(encoder_log(4000, [(10, None)]), PARAMS)
        self.assertTrue(v["abandoned"])
        self.assertAlmostEqual(v["fraction"], 1.0, places=3)

    def test_silence_end_without_its_start_uses_the_printed_duration(self):
        lines = encoder_log(4000, [])
        lines.append("[silencedetect @ 0x2] silence_end: 4000.000000 | silence_duration: 3900.000000")
        self.assertTrue(abandon.replay(lines, PARAMS)["abandoned"])

    def test_the_clock_is_the_encoders_not_the_wall_clock(self):
        # Same log, evaluated at different wall-clock moments (a waxd restarted an hour later): same answer.
        lines = encoder_log(5000, flapping_silence(5000))
        first = abandon.replay(lines, PARAMS)
        with patch.object(time, "time", return_value=time.time() + 86400), \
                patch.object(time, "monotonic", return_value=time.monotonic() + 86400):
            self.assertEqual(abandon.replay(lines, PARAMS), first)

    def test_incremental_feed_equals_a_replay_from_the_top(self):
        lines = encoder_log(9000, flapping_silence(9000))
        tl = abandon.Timeline()
        for n, line in enumerate(lines):
            tl.feed(line)
            if n % 97 == 0:
                tl.verdict(PARAMS)  # pruning mid-stream must not change the final answer
        self.assertEqual(tl.verdict(PARAMS), abandon.replay(lines, PARAMS))

    def test_disabled_and_params_from_env(self):
        lines = encoder_log(5000, [(0, None)])
        self.assertFalse(abandon.replay(lines, abandon.Params(window_s=0))["abandoned"])
        self.assertFalse(abandon.replay(lines, abandon.Params(window_s=3600, fraction=0))["abandoned"])
        with patch.dict(os.environ, {"WAX_ABANDON_WINDOW_S": "600", "WAX_ABANDON_SILENT_FRACTION": "0.9"}):
            p = abandon.Params.from_env()
        self.assertEqual((p.window_s, p.fraction), (600.0, 0.9))
        self.assertTrue(abandon.replay(lines, p)["abandoned"])
        with patch.dict(os.environ, {"WAX_ABANDON_WINDOW_S": "nope", "WAX_ABANDON_SILENT_FRACTION": ""}):
            self.assertEqual(abandon.Params.from_env(), abandon.Params())
        with patch.dict(os.environ, {"WAX_ABANDON_WINDOW_S": "0"}):
            self.assertFalse(abandon.Params.from_env().enabled)

    def test_defaults(self):
        self.assertEqual((abandon.DEFAULT_WINDOW_S, abandon.DEFAULT_FRACTION), (3600.0, 0.97))

    def test_unrelated_log_lines_are_ignored(self):
        tl = abandon.Timeline()
        for line in ("", "Press [q] to stop", "  Duration: N/A, start: 1795040318.824807, bitrate: 1536 kb/s",
                     "[silencedetect @ 0x1] silence_wat: nonsense"):
            tl.feed(line)
        self.assertEqual(tl.verdict(PARAMS)["elapsed_s"], 0.0)


class StopTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        lock = Path(self._tmp.name) / "waxd.lock"
        for target, value in ((paths, {"LOCK": lock, "VAR": Path(self._tmp.name)}),):
            for k, v in value.items():
                patcher = patch.object(target, k, v)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.verdict = abandon.replay(encoder_log(5000, [(0, None)]), PARAMS)

    def test_stops_through_the_ordinary_stop_with_the_cause(self):
        rid = "20261118-171838-abcdef"
        calls = []
        with patch.object(state, "stream_state", return_value={"state": "recording", "rid": rid}), \
                patch.object(capture, "stop", side_effect=lambda r, **kw: calls.append((r, kw)) or
                             {"rid": r, "path": "/inbox/x.ogg", "duration_s": 5000.0}):
            out = abandon.stop_abandoned(rid, self.verdict, before_stop=lambda d: calls.append(("before", d)))
        self.assertEqual(out["action"], "stopped")
        self.assertEqual(out["reason"], "abandoned_silent")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], rid)
        self.assertEqual(calls[0][1]["reason"], "abandoned_silent")
        self.assertIn("silent 100.0% of the last 60 min", calls[0][1]["detail"])
        # the ledger note rides on_intent, which capture.stop calls once the sentinel exists
        calls[0][1]["on_intent"]()
        self.assertEqual(calls[1][0], "before")
        self.assertIn("abandoned_silent", calls[1][1])

    def test_a_capture_already_stopped_or_replaced_is_left_alone(self):
        for cur in ({"state": "ready", "rid": None}, {"state": "recording", "rid": "20261119-093000-other0"},
                    {"state": "not-ready", "rid": "20261118-171838-abcdef"}):
            with patch.object(state, "stream_state", return_value=cur), \
                    patch.object(capture, "stop") as stop:
                out = abandon.stop_abandoned("20261118-171838-abcdef", self.verdict)
            self.assertEqual(out["action"], "skipped", cur)
            stop.assert_not_called()

    def test_the_ledger_note_is_written_only_after_the_stop_sentinel_exists(self):
        rid = "20261118-171838-abcdef"
        order = []
        with tempfile.TemporaryDirectory() as d:
            stream = Path(d)
            (stream / f"{rid}.rec.json").write_text(json.dumps({"rid": rid, "pid": 0, "starttime": 0,
                                                                "boot_id": "b", "target_name": "t.ogg"}))
            with patch.object(paths, "STREAM", stream), \
                    patch.object(state, "stream_state", return_value={"state": "recording", "rid": rid}), \
                    patch.object(capture.sentinel, "encoder_alive", return_value=False), \
                    patch.object(capture, "finalize", return_value={"path": "/inbox/x.ogg"}):
                abandon.stop_abandoned(rid, self.verdict,
                                       before_stop=lambda _d: order.append(paths.stop_path(rid).exists()))
        self.assertEqual(order, [True])

    def test_a_stop_that_fails_before_the_sentinel_leaves_no_ledger_note(self):
        rid = "20261118-171838-abcdef"
        notes = []
        with patch.object(state, "stream_state", return_value={"state": "recording", "rid": rid}), \
                patch.object(capture.sentinel, "read_json", return_value=None):
            with self.assertRaises(capture.CaptureError):
                abandon.stop_abandoned(rid, self.verdict, before_stop=notes.append)
        self.assertEqual(notes, [])

    def test_takes_the_capture_lifecycle_lock(self):
        import fcntl
        rid = "20261118-171838-abcdef"
        held = threading.Event()
        seen = []

        def stop(r, **kw):
            with open(paths.LOCK, "w") as other:
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen.append("free")
                except OSError:
                    seen.append("held")
            held.set()
            return {"rid": r, "path": "/inbox/x.ogg"}

        with patch.object(state, "stream_state", return_value={"state": "recording", "rid": rid}), \
                patch.object(capture, "stop", side_effect=stop):
            abandon.stop_abandoned(rid, self.verdict)
        self.assertEqual(seen, ["held"])

    def test_notification_says_what_and_why(self):
        title, body = abandon.notification(self.verdict, {"path": "/home/x/HeyMa/inbox/a.ogg"})
        self.assertIn("abandoned", title)
        self.assertIn("100%", body)
        self.assertIn("60 min", body)
        self.assertIn("a.ogg", body)
        self.assertIn("inbox", body)


class SentinelTest(unittest.TestCase):
    def test_the_stop_sentinel_names_who_stopped_and_the_stream_state_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            stream = Path(d)
            rid = "20261118-171838-abcdef"
            (stream / f"{rid}.rec.json").write_text(json.dumps({"rid": rid, "pid": 0, "starttime": 0,
                                                                "boot_id": "b", "target_name": "t.ogg"}))
            with patch.object(paths, "STREAM", stream):
                sentinel.write_stop(rid, reason="abandoned_silent", detail="abandoned_silent: encoder silent 100%")
                doc = json.loads(paths.stop_path(rid).read_text())
                self.assertEqual((doc["reason"], doc["detail"]), ("abandoned_silent", "abandoned_silent: encoder silent 100%"))
                st = state.stream_state(run_preflight=False)
                self.assertEqual((st["state"], st["clause"], st["cause_code"]), ("not-ready", "a", "finalizing"))
                self.assertIn("abandoned_silent", st["evidence"])
                sentinel.write_stop(rid)
                self.assertNotIn("detail", json.loads(paths.stop_path(rid).read_text()))
                self.assertIn("explicit stop requested", state.stream_state(run_preflight=False)["evidence"])
                sentinel.write_stop(rid, reason="cancel")
                evidence = state.stream_state(run_preflight=False)["evidence"]
                self.assertNotIn("waxd", evidence, "a user's cancel is not waxd's doing")


def load_waxd():
    loader = SourceFileLoader("waxd_under_test", str(COMPONENT_ROOT / "bin" / "waxd"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class DaemonWiringTest(unittest.TestCase):
    """waxd's real _check_silence, on a bare Daemon (no tray, worker or socket), replaying a real log file."""

    RID = "20261118-171838-abcdef"

    def setUp(self):
        self.waxd = load_waxd()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.logs = Path(self._tmp.name) / "logs"
        (self.logs / self.RID).mkdir(parents=True)
        for k, v in (("LOGS", self.logs), ("STREAM", Path(self._tmp.name) / "stream")):
            patcher = patch.object(paths, k, v)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stopped: list[tuple] = []
        self.notified: list[tuple] = []
        for target, name, repl in (
            (self.waxd.abandon, "stop_abandoned", self.fake_stop),
            (self.waxd.desktop, "notify", lambda *a, **kw: self.notified.append((a, kw)) or True),
            (self.waxd.ledger, "note_machine", lambda *a, **kw: True),
            (self.waxd.ledger, "close", lambda: None),
        ):
            patcher = patch.object(target, name, repl)
            patcher.start()
            self.addCleanup(patcher.stop)
        d = object.__new__(self.waxd.Daemon)
        d._conditions = {}
        d._silence_monitor_rid = None
        d._silence_monitor_file = None
        d._is_silent = False
        d._silent_since = 0.0
        d._abandon_params = abandon.Params(window_s=3600.0, fraction=0.97)
        d._timeline = None
        d._abandon_rid = None
        d._abandon_retry_at = 0.0
        d._abandon_thread = None
        self.d = d

    def fake_stop(self, rid, verdict, *, before_stop=None):
        self.stopped.append((rid, verdict))
        if before_stop:
            before_stop("abandoned_silent: test")
        return {"action": "stopped", "path": "/inbox/ghost.ogg", "detail": "abandoned_silent: test"}

    def write_log(self, lines):
        (self.logs / self.RID / "encoder.log").write_text("\n".join(lines) + "\n")

    def tick(self):
        snap = {"stream": {"state": "recording", "rid": self.RID}}
        self.d._check_silence(snap)
        thread = self.d._abandon_thread
        if thread is not None:
            thread.join(5)
        return snap["stream"]

    def test_an_abandoned_capture_is_stopped_once_and_the_operator_is_told(self):
        self.write_log(encoder_log(5000, flapping_silence(5000)))
        stream = self.tick()
        self.assertGreaterEqual(stream["silent_fraction"], 0.97)
        self.assertEqual([r for r, _v in self.stopped], [self.RID])
        self.assertEqual(len(self.notified), 1)
        self.assertIn("abandoned", self.notified[0][0][0])
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.stopped), 1, "one stop per capture, never one per tick")

    def test_a_restarted_daemon_replays_the_log_and_reaches_the_same_verdict(self):
        self.write_log(encoder_log(20000, flapping_silence(20000)))
        self.tick()
        self.assertEqual(len(self.stopped), 1)
        self.d._silence_monitor_file.close()
        self.d._silence_monitor_file = None
        self.d._silence_monitor_rid = None  # what a fresh waxd looks like
        self.d._timeline = None
        self.d._abandon_retry_at = 0.0
        self.tick()
        self.assertEqual(len(self.stopped), 2)

    def test_an_encoder_log_that_appears_late_is_still_judged(self):
        (self.logs / self.RID / "encoder.log").unlink(missing_ok=True)
        self.tick()
        self.assertEqual(self.stopped, [])
        self.write_log(encoder_log(5000, [(0, None)]))
        self.tick()
        self.assertEqual(len(self.stopped), 1)

    def test_quit_waits_for_an_in_flight_abandon_stop(self):
        done = threading.Event()
        t = threading.Thread(target=lambda: (time.sleep(0.3), done.set()))
        t.start()
        d = self.d
        d._abandon_thread = t
        d.worker = type("W", (), {"stop": lambda s: None})()
        d.server = type("S", (), {"shutdown": lambda s: None})()
        d.stopped = threading.Event()
        d.tray = type("T", (), {"stop": lambda s: None})()
        with patch.object(self.waxd.paths.SOCK.__class__, "unlink", lambda *a, **k: None):
            d.quit()
        self.assertTrue(done.is_set(), "quit must not return while the stop thread is still finalizing")

    def test_a_live_meeting_is_not_stopped(self):
        silences = [(t, t + 30) for t in range(600, 7200, 240)]
        self.write_log(encoder_log(7200, silences))
        stream = self.tick()
        self.assertLess(stream["silent_fraction"], 0.5)
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.notified, [])

    def test_a_failed_stop_is_loud_and_retried_only_after_the_backoff(self):
        def boom(rid, verdict, *, before_stop=None):
            self.stopped.append(rid)
            raise RuntimeError("finalizer fell over")
        self.write_log(encoder_log(5000, [(0, None)]))
        with patch.object(self.waxd.abandon, "stop_abandoned", boom):
            self.tick()
            self.tick()
        self.assertEqual(len(self.stopped), 1)
        self.assertEqual(self.notified[0][1]["urgency"], "critical")
        self.assertIn("could not stop", self.notified[0][0][0])
        self.d._abandon_retry_at = 0.0
        with patch.object(self.waxd.abandon, "stop_abandoned", boom):
            self.tick()
        self.assertEqual(len(self.stopped), 2)

    def test_disabled_by_the_knob(self):
        self.d._abandon_params = abandon.Params(window_s=0)
        self.write_log(encoder_log(5000, [(0, None)]))
        stream = self.tick()
        self.assertNotIn("silent_fraction", stream)
        self.assertEqual(self.stopped, [])

    def test_leaving_the_recording_state_resets_the_monitor(self):
        self.write_log(encoder_log(5000, [(0, None)]))
        self.tick()
        self.d._check_silence({"stream": {"state": "ready"}})
        self.assertIsNone(self.d._timeline)
        self.assertNotIn("capture_abandoned", self.d._conditions)


if __name__ == "__main__":
    unittest.main()
