"""Hermetic shadow projection and adapter tests: no provider or live Wax state."""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from wax import calls, ledger, paths, telephony

COMPONENT = Path(__file__).resolve().parents[1]
SESSION = "434630aa-d7c1-11f1-ab24-02420a0dda20"
LEG = "434c5958-d7c1-11f1-8622-02420a0dda20"
OTHER = "830f689e-d71e-11f1-b19a-4674b796d526"


def stamp(seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def status(decisions=(), mode="shadow"):
    return {"schema": "heyma.telephony.status.v1", "mode": mode,
            "ring_enabled": False, "readiness": "shadow only", "decisions": list(decisions)}


class ProjectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [patch.object(paths, "VAR", root / "var"),
                        patch.object(paths, "DB", root / "var" / "wax.db")]
        for p in self.patches:
            p.start()
        self.conn = calls.db()
        self.conn.execute("INSERT INTO calls(call_session_id,direction,line,remote_number,started_at,"
                          "first_seen,updated_at) VALUES(?,?,?,?,?,?,?)",
                          (SESSION, "inbound", "+12025550101", "+12025550102", stamp(-5), stamp(-5), stamp(-5)))
        self.conn.execute("INSERT INTO call_legs(call_leg_id,call_session_id,raw,updated_at) VALUES(?,?,?,?)",
                          (LEG, SESSION, "{}", stamp()))
        self.status_patch = patch.object(telephony, "_core_status", return_value=status())
        self.status_patch.start()

    def tearDown(self):
        self.status_patch.stop()
        conn = getattr(ledger._local, "conn", None)
        if conn is not None:
            conn.close()
            del ledger._local.conn
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def snapshot(self):
        return telephony.snapshot()["calls"][0]

    def event(self, kind, when=None, leg=LEG):
        self.conn.execute("INSERT INTO call_events(event_id,event_type,occurred_at,call_session_id,call_leg_id,"
                          "payload,via,received_at) VALUES(?,?,?,?,?,?,?,?)",
                          (f"{kind}-{self.conn.execute('SELECT COUNT(*) FROM call_events').fetchone()[0]}",
                           kind, when or stamp(), SESSION, leg, "{}", "webhook", stamp()))

    def test_no_initiated_event_never_rings_and_receipt_gap_warns(self):
        card = self.snapshot()
        self.assertEqual(card["call_state"], "unknown")
        self.assertIsNone(card["ring_event_at"])
        self.assertEqual(card["screening"]["decision"], "unassessed")
        self.assertIn("no_shadow_assessment_receipt", card["warnings"])
        self.assertEqual(card["processing"]["state"], "awaiting_audio")

    def test_only_fresh_inbound_initiation_rings_and_terminal_events_win_replays(self):
        self.event("call.initiated", stamp(-8))
        card = self.snapshot()
        self.assertEqual(card["call_state"], "ringing")
        self.assertIsNotNone(card["ring_event_at"])
        self.event("call.hangup", stamp(-1))
        self.event("call.initiated", stamp(-2))
        self.assertEqual(self.snapshot()["call_state"], "ended")
        self.assertIsNone(self.snapshot()["ring_event_at"])

    def test_stale_initiation_and_answer_do_not_ring(self):
        self.event("call.initiated", stamp(-100))
        self.assertEqual(self.snapshot()["call_state"], "unknown")
        self.event("call.answered", stamp(-20))
        self.assertEqual(self.snapshot()["call_state"], "connected")
        self.assertIsNone(self.snapshot()["ring_event_at"])

    def test_sibling_fork_hangup_then_answer_then_final_end(self):
        sibling = OTHER
        self.conn.execute("INSERT INTO call_legs(call_leg_id,call_session_id,raw,updated_at) VALUES(?,?,?,?)",
                          (sibling, SESSION, "{}", stamp()))
        self.event("call.initiated", stamp(-8))
        self.event("call.initiated", stamp(-7), sibling)
        self.event("call.hangup", stamp(-4), sibling)
        self.conn.execute("UPDATE calls SET ended_at=? WHERE call_session_id=?", (stamp(-4), SESSION))
        card = self.snapshot()
        self.assertEqual(card["call_state"], "ringing")
        self.assertIsNone(card["ended_at"])
        self.assertIsNotNone(card["ring_event_at"])
        self.event("call.answered", stamp(-3))
        self.conn.execute("UPDATE calls SET answered_at=?,announced_at=?,outcome=? WHERE call_session_id=?",
                          (stamp(-3), stamp(-2), "answered", SESSION))
        card = self.snapshot()
        self.assertEqual(card["call_state"], "connected")
        self.assertIsNone(card["ended_at"])
        self.event("call.hangup", stamp(-1))
        self.conn.execute("UPDATE calls SET ended_at=? WHERE call_session_id=?", (stamp(-1), SESSION))
        card = self.snapshot()
        self.assertEqual(card["call_state"], "ended")
        self.assertIsNotNone(card["ended_at"])
        self.assertIsNone(card["ring_event_at"])
        self.event("call.initiated", stamp(-6))
        self.assertEqual(self.snapshot()["call_state"], "ended")

    def test_settled_call_not_revived_by_stale_unfinished_leg(self):
        self.event("call.initiated", stamp(-8))
        self.event("call.answered", stamp(-6))
        self.event("call.hangup", stamp(-2))
        self.conn.execute("UPDATE calls SET answered_at=?,ended_at=?,announced_at=?,outcome=? WHERE call_session_id=?",
                          (stamp(-6), stamp(-2), stamp(-1), "answered", SESSION))
        self.assertEqual(self.snapshot()["call_state"], "ended")
        self.event("call.initiated", stamp(-7), OTHER)
        card = self.snapshot()
        self.assertEqual(card["call_state"], "ended")
        self.assertIsNone(card["ring_event_at"])

    def test_two_recordings_same_leg_require_distinct_exact_audio(self):
        first = f"telnyx-2026-11-07-{LEG}-1794000000.mp3"
        second = f"telnyx-2026-11-07-{LEG}-1794000001.mp3"
        for idx, filename in enumerate((first, second)):
            self.conn.execute("INSERT INTO call_recordings(recording_key,kind,call_leg_id,call_session_id,"
                              "file_name,raw,updated_at) VALUES(?,?,?,?,?,?,?)",
                              (f"rec{idx}", "trunk", LEG, SESSION, filename, "{}", stamp()))
        for idx, filename in enumerate((first, first.replace(".mp3", "-" + "a" * 64 + ".mp3"))):
            item_id = f"item{idx}"
            self.conn.execute("INSERT INTO items(item_id,sha256,path,orig_name,state,first_seen,updated_at) "
                              "VALUES(?,?,?,?,?,?,?)", (item_id, f"hash{idx}", str(Path(self.tmp.name) / filename),
                                                     filename, "complete", stamp(), stamp()))
            self.conn.execute("INSERT INTO processing_plans(item_id,plan_id,definitions,created_at) VALUES(?,?,?,?)",
                              (item_id, f"plan{idx}", "{}", stamp()))
            self.conn.execute("INSERT INTO completions(item_id,plan_id,event_id,outbox_id,artifact_sha256,created_at) "
                              "VALUES(?,?,?,?,?,?)", (item_id, f"plan{idx}", f"receipt{idx}", idx, f"hash{idx}", stamp()))
        self.assertNotEqual(self.snapshot()["processing"]["state"], "complete")

    def test_conflicting_event_leg_cannot_claim_another_sessions_audio(self):
        self.conn.execute("UPDATE call_legs SET call_session_id=? WHERE call_leg_id=?", (OTHER, LEG))
        filename = f"telnyx-2026-11-07-{LEG}-1794000000.mp3"
        self.event("call.initiated", stamp(-4))
        self.conn.execute("INSERT INTO items(item_id,sha256,path,orig_name,state,first_seen,updated_at) "
                          "VALUES(?,?,?,?,?,?,?)", ("item", "hash", str(Path(self.tmp.name) / filename),
                                                 filename, "pending", stamp(), stamp()))
        self.assertEqual(self.snapshot()["processing"]["items"], [])

    def test_receipts_join_exact_identity_and_latest_provider_time(self):
        receipts = [
            {"session_id": SESSION, "occurred_at": stamp(-20), "observed_at": stamp(),
             "mode": "shadow", "enforced": False, "decision": "allow", "event_id": "old"},
            {"session_id": OTHER, "call_leg_id": LEG, "occurred_at": stamp(),
             "mode": "shadow", "enforced": False, "decision": "would_block", "event_id": "wrong-session"},
            {"call_leg_id": LEG, "occurred_at": stamp(-10), "observed_at": stamp(-100),
             "mode": "shadow", "enforced": False, "decision": "would_block", "event_id": "new"},
        ]
        with patch.object(telephony, "_core_status", return_value=status(receipts)):
            self.assertEqual(self.snapshot()["screening"]["decision"], "would_block")

    def test_completed_without_recording_inventory_remains_unverified(self):
        filename = f"telnyx-2026-11-07-{LEG}-1794000000.mp3"
        self.conn.execute("INSERT INTO items(item_id,sha256,path,orig_name,state,first_seen,updated_at) "
                          "VALUES(?,?,?,?,?,?,?)",
                          ("one", "hash", str(Path(self.tmp.name) / filename), filename,
                           "complete", stamp(), stamp()))
        self.assertNotEqual(self.snapshot()["processing"]["state"], "complete")
        self.conn.execute("INSERT INTO processing_plans(item_id,plan_id,definitions,created_at) VALUES(?,?,?,?)",
                          ("one", "plan", "{}", stamp()))
        self.assertNotEqual(self.snapshot()["processing"]["state"], "complete")
        self.conn.execute("INSERT INTO completions(item_id,plan_id,event_id,outbox_id,artifact_sha256,created_at) "
                          "VALUES(?,?,?,?,?,?)", ("one", "plan", "receipt", 1, "hash", stamp()))
        self.assertNotEqual(self.snapshot()["processing"]["state"], "complete")

    def test_completed_requires_current_plan_receipt_for_all_audio_items(self):
        filename = f"telnyx-2026-11-07-{LEG}-1794000000.mp3"
        self.conn.execute("INSERT INTO call_recordings(recording_key,kind,call_leg_id,call_session_id,raw,updated_at,file_name) "
                          "VALUES(?,?,?,?,?,?,?)", ("rec", "trunk", LEG, SESSION, "{}", stamp(), filename))
        self.conn.execute("INSERT INTO items(item_id,sha256,path,orig_name,state,first_seen,updated_at) "
                          "VALUES(?,?,?,?,?,?,?)",
                          ("one", "hash", str(Path(self.tmp.name) / filename), filename,
                           "complete", stamp(), stamp()))
        self.assertNotEqual(self.snapshot()["processing"]["state"], "complete")
        self.conn.execute("INSERT INTO processing_plans(item_id,plan_id,definitions,created_at) VALUES(?,?,?,?)",
                          ("one", "plan", "{}", stamp()))
        self.assertNotEqual(self.snapshot()["processing"]["state"], "complete")
        self.conn.execute("INSERT INTO completions(item_id,plan_id,event_id,outbox_id,artifact_sha256,created_at) "
                          "VALUES(?,?,?,?,?,?)", ("one", "old-plan", "old-receipt", 1, "hash", stamp()))
        self.assertNotEqual(self.snapshot()["processing"]["state"], "complete")
        self.conn.execute("INSERT INTO completions(item_id,plan_id,event_id,outbox_id,artifact_sha256,created_at) "
                          "VALUES(?,?,?,?,?,?)", ("one", "plan", "receipt", 2, "hash", stamp()))
        self.assertEqual(self.snapshot()["processing"]["state"], "complete")

    def test_receipt_without_provider_time_remains_unassessed(self):
        receipt = {"session_id": SESSION, "observed_at": stamp(), "decision": "would_block",
                   "mode": "shadow", "enforced": False}
        with patch.object(telephony, "_core_status", return_value=status([receipt])):
            self.assertEqual(self.snapshot()["screening"]["decision"], "unassessed")

    def test_cli_parser_and_limit_are_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            import os
            env = os.environ.copy()
            env["WAX_ROOT"] = root
            for flag, expected in (([], 0), (["--limit", "0"], 2)):
                proc = subprocess.run([sys.executable, str(COMPONENT / "bin" / "wax"), "--json", "calls",
                                       "telephony", *flag], capture_output=True, text=True, env=env)
                self.assertEqual(proc.returncode, expected, proc.stderr)
                if not flag:
                    self.assertEqual(json.loads(proc.stdout)["schema"], "heyma.telephony.snapshot.v1")

    def test_missing_core_is_unavailable_not_healthy_empty(self):
        with patch.object(telephony, "_core_status", side_effect=FileNotFoundError()):
            self.assertEqual(telephony.snapshot()["health"], "unavailable")


    def test_daemon_observer_coalesces_one_in_flight_refresh(self):
        import importlib.machinery
        import importlib.util
        import threading
        spec = importlib.util.spec_from_loader("waxd_telephony_test", importlib.machinery.SourceFileLoader(
            "waxd_telephony_test", str(COMPONENT / "bin" / "waxd")))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        daemon = object.__new__(module.Daemon)
        daemon._telephony_lock = threading.Lock()
        daemon._telephony_refresh_at = 0.0
        daemon._telephony_refreshing = False
        daemon._telephony_snapshot = None
        entered = threading.Event()
        release = threading.Event()
        result = status()

        def slow_snapshot(*, limit):
            entered.set()
            self.assertTrue(release.wait(2), "observer test must release worker")
            return result

        with patch.object(module.telephony_shadow, "snapshot", side_effect=slow_snapshot) as read:
            try:
                self.assertEqual(daemon._schedule_telephony()["health"], "unavailable")
                self.assertTrue(entered.wait(2))
                daemon._telephony_refresh_at = 0.0
                self.assertEqual(daemon._schedule_telephony()["health"], "unavailable")
                self.assertEqual(read.call_count, 1)
            finally:
                release.set()
            for _ in range(100):
                with daemon._telephony_lock:
                    if not daemon._telephony_refreshing:
                        break
                threading.Event().wait(0.01)
            self.assertFalse(daemon._telephony_refreshing)
            self.assertIs(daemon._schedule_telephony(), result)


class AdapterTest(unittest.TestCase):
    def test_new_event_bounded_and_duplicate_noop(self):
        raw = {"data": {"id": "event"}}
        with patch.object(telephony, "CORE_CLI", Path(__file__)), patch.object(
                telephony.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            telephony.shadow_event(raw, is_new=False)
            run.assert_not_called()
            telephony.shadow_event(raw, is_new=True)
            self.assertEqual(run.call_args.args[0], [str(telephony.CORE_CLI), "--json", "event"])
            self.assertLessEqual(run.call_args.kwargs["timeout"], 2)
            telephony.shadow_event({"large": "x" * (telephony.MAX_EVENT + 1)}, is_new=True)
            self.assertEqual(run.call_count, 1)

    def test_unavailable_adapter_cannot_break_ingest(self):
        with patch.object(telephony, "CORE_CLI", Path(__file__)), patch.object(
                telephony.subprocess, "run", side_effect=subprocess.TimeoutExpired("telephony", 1)):
            telephony.shadow_event({"data": {"id": "event"}}, is_new=True)


if __name__ == "__main__":
    unittest.main()
