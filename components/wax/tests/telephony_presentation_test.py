"""GI-free Telephony projection and opt-in tray-ringer safety tests."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from wax import heyma_window, telephony_presentation as phone


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def snapshot(calls=(), **updates):
    snap = {"schema": phone.SCHEMA, "generated_at": NOW.isoformat(), "mode": "shadow",
            "health": "ok", "ring_enabled": True, "calls": list(calls)}
    snap.update(updates)
    return snap


def call(sid="new", **updates):
    row = {"session_id": sid, "direction": "inbound", "call_state": "ringing",
           "started_at": NOW.isoformat(), "ring_event_at": NOW.isoformat(),
           "screening": {"decision": "would_block", "enforced": False},
           "processing": {"state": "awaiting_audio"}}
    row.update(updates)
    return row


class PresentationTest(unittest.TestCase):
    def test_shadow_is_not_blocked_and_processing_is_separate(self):
        view = phone.presentation(snapshot([call()]), NOW)
        self.assertIn("not enforced", view["banner"])
        self.assertEqual(view["calls"][0]["phone"], "Ringing")
        self.assertEqual(view["calls"][0]["screening"], "Would block — not enforced")
        self.assertIn("Awaiting audio", view["calls"][0]["processing"])
        self.assertNotIn("blocked", view["calls"][0]["phone"].lower())

    def test_unknown_receipt_warns_and_old_snapshot_is_unavailable(self):
        view = phone.presentation(snapshot([call(screening={"decision": "unassessed"})]), NOW)
        self.assertIn("No matched shadow", view["calls"][0]["warnings"][-1])
        stale = phone.presentation(snapshot([call()], generated_at=(NOW-timedelta(minutes=1)).isoformat()), NOW)
        self.assertFalse(stale["available"])
        self.assertEqual(stale["calls"], [])

    def test_disabled_never_exposes_ring_on(self):
        view = phone.presentation(snapshot(mode="disabled"), NOW)
        self.assertFalse(view["ring_enabled"])
        self.assertIn("Disabled", view["banner"])

    def test_page_navigation_and_absolute_local_cli(self):
        self.assertEqual(heyma_window.requested_page(["--page", "telephony"]), "telephony")
        with patch.object(heyma_window.subprocess, "run", return_value=Mock(returncode=0, stdout='{}')) as run:
            self.assertTrue(heyma_window.telephony_config("--ring", "on")[0])
            self.assertEqual(run.call_args.args[0],
                             [str(phone.TELEPHONY_CLI), "--json", "config", "--ring", "on"])
            self.assertTrue(phone.TELEPHONY_CLI.is_absolute())
    def test_page_refreshes_visible_call_and_processing_transitions(self):
        states = [call(call_state="ringing"),
                  call(call_state="connected", answered_at=NOW.isoformat(),
                       processing={"state": "queued"}),
                  call(call_state="ended", ended_at=NOW.isoformat(),
                       processing={"state": "transcribing"})]
        page = heyma_window.TelephonyPage.__new__(heyma_window.TelephonyPage)
        page._visible = page._loading = False
        page._generation = 0
        page._refresh_source = None
        page._rows = []
        page.status = Mock()
        page.ring = Mock()
        page.calls = Mock()
        seen = []
        timers = []
        removed = []
        class Row:
            def __init__(self, **kwargs):
                self.values = kwargs
                self.children = []
                seen.append(self)
            def add_row(self, row):
                self.children.append(row)
        def work(*args, **kwargs):
            return True, snapshot([states.pop(0)]), ""
        with (patch.object(heyma_window, "wax", side_effect=work),
              patch.object(heyma_window, "_background", side_effect=lambda task, done: done(task())),
              patch.object(heyma_window, "GLib", Mock(
                  timeout_add=lambda ms, callback: (timers.append((ms, callback)), len(timers))[1],
                  source_remove=lambda source: removed.append(source))),
              patch.object(heyma_window, "Adw", Mock(ExpanderRow=Row, ActionRow=Row)),
              patch.object(heyma_window, "_markup", side_effect=lambda text: text),
              patch.object(heyma_window, "datetime", Mock(now=lambda tz: NOW))):
            page.start()
            self.assertEqual(timers[0][0], heyma_window.TELEPHONY_REFRESH_MS)
            self.assertEqual(seen[-4].children[0].values["subtitle"], "Ringing")
            self.assertTrue(timers[0][1]())
            self.assertEqual(seen[-4].children[0].values["subtitle"], "Connected")
            self.assertIn("Queued", seen[-4].children[2].values["subtitle"])
            self.assertTrue(timers[0][1]())
            self.assertEqual(seen[-4].children[0].values["subtitle"], "Ended")
            self.assertIn("Transcribing", seen[-4].children[2].values["subtitle"])
            page.stop()
            self.assertEqual(removed, [1])
            self.assertFalse(timers[0][1]())

    def test_hidden_page_discards_in_flight_result_and_coalesces_ticks(self):
        page = heyma_window.TelephonyPage.__new__(heyma_window.TelephonyPage)
        page._visible = page._loading = False
        page._generation = 0
        page._refresh_source = None
        pending = []
        timers = []
        with (patch.object(heyma_window, "_background", side_effect=lambda work, done: pending.append(done)),
              patch.object(heyma_window, "GLib", Mock(
                  timeout_add=lambda ms, callback: (timers.append(callback), 1)[1],
                  source_remove=lambda source: None)),
              patch.object(page, "_apply") as apply):
            page.start()
            self.assertEqual(len(pending), 1)
            timers[0]()  # one in-flight read, no overlapping CLI launch
            self.assertEqual(len(pending), 1)
            page.stop()
            page.start()
            pending[0]((True, snapshot([call()]), ""))
            apply.assert_not_called()  # old read must not overwrite reopened page
            timers[1]()
            self.assertEqual(len(pending), 2)
            pending[1]((True, snapshot([call()]), ""))
            apply.assert_called_once()
            page.stop()

    def test_missing_playback_backend_is_explicit(self):
        with TemporaryDirectory() as directory:
            sound = Path(directory) / "ring.oga"
            sound.touch()
            self.assertIn("backend missing", phone.playback_unavailable(sound, Path(directory) / "absent"))


class FakeChild:
    def __init__(self):
        self.terminated = 0
        self.killed = 0
    def poll(self):
        return None if not self.terminated else 0
    def terminate(self):
        self.terminated += 1
    def wait(self, timeout):
        return 0
    def kill(self):
        self.killed += 1


class RingerTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        sound = Path(self.temp.name) / "ring.oga"
        sound.touch()
        self.children = []
        def popen(*args, **kwargs):
            child = FakeChild()
            self.children.append(child)
            return child
        self.player = phone.RingPlayer(sound=sound, popen=popen)
        self.ringer = phone.Ringer(self.player, NOW - timedelta(seconds=1))
        self.ringer.tick(snapshot(), NOW)

    def tick(self, calls=(), now=NOW, **updates):
        return self.ringer.tick(snapshot(calls, generated_at=now.isoformat(), **updates), now)

    def test_fresh_inbound_initiated_would_block_still_rings(self):
        self.assertTrue(self.tick([call()]))
        self.assertEqual(len(self.children), 1)
        self.tick([call()], now=NOW + timedelta(seconds=1))
        self.assertEqual(len(self.children), 1)

    def test_generic_in_progress_and_outbound_never_ring(self):
        self.assertFalse(self.tick([call("generic", ring_event_at=None)]))
        self.assertFalse(self.tick([call("out", direction="outbound")]))
        self.assertEqual(self.children, [])

    def test_historical_and_startup_calls_do_not_ring(self):
        r = phone.Ringer(self.player, NOW)
        r.tick(snapshot([call("startup")]), NOW)
        self.assertFalse(r.tick(snapshot([call("startup")]), NOW + timedelta(seconds=1)))
        self.assertFalse(self.tick([call("old", ring_event_at=(NOW-timedelta(minutes=1)).isoformat())]))
        self.assertEqual(self.children, [])

    def test_answer_end_silence_stale_and_error_stop_playback(self):
        for change in ({"answered_at": NOW.isoformat()}, {"ended_at": NOW.isoformat()},
                       {"call_state": "ended"}):
            with self.subTest(change=change):
                sid = str(len(self.children))
                self.tick([call(sid)])
                child = self.children[-1]
                self.tick([call(sid, **change)], now=NOW + timedelta(seconds=1))
                self.assertEqual(child.terminated, 1)
        self.tick([call("silence")])
        child = self.children[-1]
        self.ringer.silence()
        self.assertEqual(child.terminated, 1)
        self.assertFalse(self.tick([call("silence")]))
        self.tick([call("stale")])
        child = self.children[-1]
        self.ringer.tick(snapshot([call("stale")], generated_at=(NOW-timedelta(minutes=1)).isoformat()), NOW)
        self.assertEqual(child.terminated, 1)
        self.tick([call("error")])
        child = self.children[-1]
        self.tick([call("error")], health="unavailable")
        self.assertEqual(child.terminated, 1)

    def test_disabled_and_ring_off_never_play(self):
        self.assertFalse(self.tick([call("disabled")], mode="disabled"))
        self.assertFalse(self.tick([call("off")], ring_enabled=False))
        self.assertEqual(self.children, [])


if __name__ == "__main__":
    unittest.main()
