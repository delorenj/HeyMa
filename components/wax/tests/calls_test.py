"""The call log (wax.calls): identity, derivation and the two idempotent writers.

Hermetic: a temporary ledger, Telnyx fixtures shaped like the live API
(recon 2026-11-03), no network. The voicemail download is patched.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import calls, ledger, paths

COMPONENT = Path(__file__).resolve().parents[1]

OUT_SESSION, OUT_LEG = "830f689e-d71e-11f1-b19a-4674b796d526", "830f6f6a-d71e-11f1-8b40-4674b796d526"
IN_SESSION, IN_LEG = "434630aa-d7c1-11f1-ab24-02420a0dda20", "434c5958-d7c1-11f1-8622-02420a0dda20"
VM_SESSION = "921d12aa-d4a3-11f1-8a51-02420a0dda20"
DIVERSION = "<sip:+12152083549;npdi@64.16.250.9;user=phone>;privacy=off;reason=no-answer;counter=1"


def cdr(leg, session, **over):
    row = {"id": leg, "telnyx_session_id": session, "direction": "outbound", "cli": "+12152083549",
           "cld": "+16099203094", "dest_number": "16099203094", "caller_name": "",
           "started_at": "2026-11-03T00:35:47Z", "answered_at": "2026-11-03T00:36:14Z",
           "finished_at": "2026-11-03T00:38:17Z", "call_sec": 123, "hangup_cause": "NORMAL_CLEARING",
           "hangup_details": "send_bye", "completed": 1, "telnyx_voicemail": False,
           "connection_name": "DeLoNET SIP"}
    return row | over


def event(event_id, event_type, session, **payload):
    return {"data": {"id": event_id, "event_type": event_type, "occurred_at": "2026-11-03T20:00:49.6Z",
                     "record_type": "event", "payload": {"call_session_id": session} | payload}}


class _Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [patch.object(paths, "VAR", root / "var"), patch.object(paths, "DB", root / "var/wax.db"),
                        patch.object(paths, "INBOX", root / "inbox")]
        for p in self.patches:
            p.start()
        self._close()
        self.conn = calls.db()

    def _close(self):
        conn = getattr(ledger._local, "conn", None)
        if conn is not None:
            conn.close()
            del ledger._local.conn

    def tearDown(self):
        self._close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class IdentityTest(unittest.TestCase):
    def test_telnyx_trunk_name(self):
        ident = calls.audio_identity(f"telnyx-2026-11-03-{OUT_LEG}-1793666147-{'a' * 64}.mp3")
        self.assertEqual((ident["source"], ident["call_leg_id"], ident["epoch"], ident["record_verb"]),
                         ("telnyx", OUT_LEG, 1793666147, False))

    def test_record_verb_duplicate_is_flagged(self):
        ident = calls.audio_identity(f"telnyx-2026-10-18-{OUT_LEG}-1792102909440788-{'b' * 64}.mp3")
        self.assertTrue(ident["record_verb"])
        self.assertEqual(ident["epoch"], 1792102909)

    def test_voicemail_and_groundwire_and_other(self):
        self.assertEqual(calls.audio_identity(f"telnyx-vm-2026-10-30-{VM_SESSION}-1793393480.wav")
                         ["call_session_id"], VM_SESSION)
        gw = calls.audio_identity("rec_DeLoNET_Damian Miller_16099203094@261030_084214.wav")
        self.assertEqual((gw["source"], gw["remote_digits"], gw["contact"]),
                         ("groundwire", "16099203094", "Damian Miller"))
        self.assertIsNone(calls.audio_identity("20261103-meeting.ogg"))

    def test_person_name(self):
        self.assertEqual(calls.person_name("DAMIAN MILLER"), "Damian Miller")
        self.assertIsNone(calls.person_name("NEW YORK     NY"))
        self.assertIsNone(calls.person_name("FLOURTOWN PA"))
        self.assertIsNone(calls.person_name("Spam Likely"))
        self.assertIsNone(calls.person_name("+1 215 555 0100"))


class DeriveTest(_Ledger):
    def test_answered_outbound_call(self):
        calls.upsert_leg(self.conn, cdr(OUT_LEG, OUT_SESSION))
        row = calls.derive(self.conn, OUT_SESSION)
        self.assertEqual((row["direction"], row["outcome"], row["remote_number"], row["talk_seconds"]),
                         ("outbound", "answered", "+16099203094", 123))
        self.assertEqual(row["line"], calls.LINE)

    def test_missed_forwarded_inbound_from_events_alone(self):
        calls.ingest_event(event("e1", "call.initiated", IN_SESSION, direction="incoming", call_leg_id="w1",
                                 **{"from": "+19297480614", "to": "+12154498437",
                                    "caller_id_name": "NEW YORK     NY", "start_time": "2026-11-03T20:00:48Z",
                                    "sip_headers": [{"name": "Diversion", "value": DIVERSION}]}))
        result = calls.ingest_event(event("e2", "call.hangup", IN_SESSION, call_leg_id="w1",
                                          end_time="2026-11-03T20:00:49Z", hangup_cause="originator_cancel"))
        call = result["call"]
        self.assertEqual((call["direction"], call["outcome"], call["remote_number"]),
                         ("inbound", "missed", "+19297480614"))
        self.assertEqual((call["forwarded_from"], call["forward_reason"]), ("+12152083549", "no-answer"))
        self.assertIsNone(call["remote_name"])  # a CNAM city is not a person

    def test_declined_and_voicemail_outcomes(self):
        calls.upsert_leg(self.conn, cdr(IN_LEG, IN_SESSION, direction="inbound", cli="+16099203094",
                                        cld="+12154498437", caller_name="DAMIAN MILLER", answered_at=None,
                                        call_sec=0, completed=0, hangup_details="recv_refuse"))
        self.assertEqual(calls.derive(self.conn, IN_SESSION)["outcome"], "declined")
        calls.upsert_leg(self.conn, cdr("vm-leg", IN_SESSION, direction="inbound", dest_number="happy_voicemail_deposit",
                                        telnyx_voicemail=True, answered_at="2026-11-03T20:01:00Z", call_sec=20))
        row = calls.derive(self.conn, IN_SESSION)
        self.assertEqual((row["outcome"], row["remote_name"]), ("voicemail", "Damian Miller"))

    def test_outbound_borrows_the_name_an_inbound_call_carried(self):
        calls.upsert_leg(self.conn, cdr(IN_LEG, IN_SESSION, direction="inbound", cli="+16099203094",
                                        cld="+12154498437", caller_name="DAMIAN MILLER",
                                        started_at="2026-11-01T00:00:00Z"))
        calls.derive(self.conn, IN_SESSION)
        calls.upsert_leg(self.conn, cdr(OUT_LEG, OUT_SESSION))
        self.assertEqual(calls.derive(self.conn, OUT_SESSION)["remote_name"], "Damian Miller")

    def test_events_are_idempotent(self):
        first = calls.ingest_event(event("e1", "call.hangup", IN_SESSION))
        again = calls.ingest_event(event("e1", "call.hangup", IN_SESSION))
        self.assertTrue(first["new"])
        self.assertFalse(again["new"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM call_events").fetchone()[0], 1)

    def test_envelope_shapes(self):
        for shape in (event("a", "call.cost", IN_SESSION), event("b", "call.cost", IN_SESSION)["data"],
                      {"webhook": event("c", "call.cost", IN_SESSION)["data"]},
                      {"body": event("d", "call.cost", IN_SESSION)}):
            self.assertIsNotNone(calls.record_event(self.conn, shape))
        with self.assertRaises(calls.CallsError):
            calls.ingest_event({"hello": "world"})


class AudioJoinTest(_Ledger):
    def _seed(self):
        calls.upsert_leg(self.conn, cdr(OUT_LEG, OUT_SESSION))
        calls.upsert_recording(self.conn, {
            "id": "323239fb", "call_leg_id": OUT_LEG, "call_session_id": OUT_SESSION, "channels": "dual",
            "initiated_by": "Trunking", "recording_started_at": "2026-11-03T00:36:14Z",
            "download_urls": {"mp3": f"s3://recordings/x/2026-11-03/{OUT_LEG}-1793666147.mp3"}})
        calls.derive(self.conn, OUT_SESSION)

    def test_trunk_recording_joins_through_its_leg(self):
        self._seed()
        call = calls.call_for_audio(f"telnyx-2026-11-03-{OUT_LEG}-1793666147-{'a' * 64}.mp3")
        self.assertEqual(call["call_session_id"], OUT_SESSION)
        fm = calls.frontmatter_for(call)
        self.assertEqual(fm["call-direction"], "outbound")
        self.assertEqual(fm["call-leg-id"], OUT_LEG)
        self.assertNotIn("call-remote-name", fm)  # unknown is absent, never null
        self.assertTrue(fm["call-started"].startswith("2026-11-0"))

    def test_groundwire_prefers_the_answered_attempt_nearest_in_time(self):
        self._seed()
        calls.upsert_leg(self.conn, cdr("redial", "redial-session", started_at="2026-11-03T00:35:45Z",
                                        answered_at=None, call_sec=0, completed=0))
        with patch.object(calls, "_ts", wraps=calls._ts):
            local = calls._ts("2026-11-03T00:35:46Z").astimezone()
            name = f"rec_DeLoNET_Damian Miller_16099203094@{local:%y%m%d_%H%M%S}.ogg"
            self.assertEqual(calls.call_for_audio(name)["call_session_id"], OUT_SESSION)

    def test_channel_parties_follow_direction(self):
        self._seed()
        name = f"telnyx-2026-11-03-{OUT_LEG}-1793666147-{'a' * 64}.mp3"
        self.assertEqual(calls.channel_parties(name, fetch=False), ("Me", "+16099203094"))
        self.assertIsNone(calls.channel_parties("meeting.ogg", fetch=False))


class VoicemailTest(_Ledger):
    def test_voicemail_is_fetched_once_into_the_inbox(self):
        paths.INBOX.mkdir(parents=True)
        fetched = []

        def fake_download(url, destination):
            fetched.append(url)
            destination.write_bytes(b"RIFF" + b"\0" * 100)
            return destination

        envelope = event("vm1", "calls.voicemail.completed", VM_SESSION,
                         recording_url="https://voice-mail-prod.example/x.wav?sig",
                         **{"from": "+12157748272", "to": "+12154498437"})
        with patch.object(calls, "_download", side_effect=fake_download):
            first = calls.ingest_event(envelope)
            again = calls.ingest_event(envelope)
        self.assertTrue(first["voicemail"]["fetched"])
        self.assertFalse(again["voicemail"]["fetched"])
        self.assertEqual(len(fetched), 1)
        name = first["voicemail"]["file_name"]
        self.assertTrue((paths.INBOX / name).is_file())
        self.assertEqual(calls.audio_identity(name)["call_session_id"], VM_SESSION)
        self.assertEqual(first["call"]["outcome"], "voicemail")

    def test_expired_link_is_reported_not_raised(self):
        envelope = event("vm2", "calls.voicemail.completed", VM_SESSION, recording_url="https://x/y.wav")
        with patch.object(calls, "_download", side_effect=calls.CallsError("HTTP 403", "voicemail_expired")):
            result = calls.ingest_event(envelope)
        self.assertEqual(result["voicemail"]["error"], "voicemail_expired")


class SyncTest(_Ledger):
    def test_sync_reconciles_all_three_sources(self):
        pages = {
            "/detail_records": [cdr(OUT_LEG, OUT_SESSION)],
            "/recordings": [{"id": "r1", "call_leg_id": OUT_LEG, "call_session_id": OUT_SESSION,
                             "initiated_by": "Trunking", "download_urls": {"mp3": "s3://b/k/leg.mp3"}}],
            "/webhook_deliveries": [{"webhook": event("e9", "call.initiated", OUT_SESSION,
                                                      direction="outgoing", to="+16099203094")["data"]}],
        }
        with patch.object(calls, "_pages", side_effect=lambda path, params, key, size=250: iter(pages[path])):
            counts = calls.sync(calls._ts("2026-11-01T00:00:00Z"), key="k")
        self.assertEqual((counts["legs"], counts["recordings"], counts["events"], counts["calls"]), (1, 1, 1, 1))
        self.assertEqual(calls.recent()[0]["recordings"], ["leg.mp3"])
        self.assertEqual(calls.last_sync()["calls"], 1)


class PassCommandTest(_Ledger):
    def test_a_non_call_recording_is_skipped(self):
        proc = subprocess.run([sys.executable, str(COMPONENT / "config/passes.d/bin/call-metadata"), "x.md", "nope"],
                              capture_output=True, text=True, env={"WAX_ROOT": self.tmp.name, "PATH": "/usr/bin:/bin"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.strip().splitlines()[-1])["reason_code"], "not_a_call")


if __name__ == "__main__":
    unittest.main()
