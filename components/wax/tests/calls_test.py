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


def minimal_env(root) -> dict:
    """A bare subprocess env (no inherited os.environ), so it must carry its OWN vault: without WAX_VAULT and
    TRANSCRIPTS a spawned `wax` falls back to the real ~/d/Transcripts, which tests/conftest.py cannot pin."""
    return {"WAX_ROOT": str(root), "WAX_VAULT": f"{root}/vault", "TRANSCRIPTS": f"{root}/vault",
            "PATH": "/usr/bin:/bin"}

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

    def test_a_millisecond_trunk_object_is_not_a_duplicate(self):
        ident = calls.audio_identity(f"telnyx-2026-10-08-{OUT_LEG}-1791489580786.mp3")
        self.assertEqual((ident["record_verb"], ident["epoch"]), (False, 1791489580))

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
        self.assertFalse(result["ended"], "a missed call is a log row, never announced")
        self.assertEqual((call["direction"], call["outcome"], call["remote_number"]),
                         ("inbound", "missed", "+19297480614"))
        self.assertEqual((call["forwarded_from"], call["forward_reason"]), ("+12152083549", "no-answer"))
        self.assertIsNone(call["remote_name"])  # a CNAM city is not a person

    def test_answered_inbound_announces_once_at_the_real_hangup(self):
        calls.ingest_event(event("a1", "call.initiated", IN_SESSION, direction="incoming", call_leg_id="w1",
                                 **{"from": "+16099203094", "to": "+12154498437",
                                    "start_time": "2026-11-03T20:00:40Z"}))
        early = calls.ingest_event(event("a2", "call.hangup", IN_SESSION, call_leg_id="fork",
                                         end_time="2026-11-03T20:00:45Z"))
        self.assertFalse(early["ended"], "a forked leg that stopped ringing does not end the call")
        answered = calls.ingest_event(
            {"data": event("a3", "call.answered", IN_SESSION, call_leg_id="w1")["data"]
             | {"occurred_at": "2026-11-03T20:00:50Z"}})
        self.assertEqual(answered["call"]["outcome"], "answered")
        self.assertFalse(answered["ended"], "answered after the fork hung up: still live")
        hung = calls.ingest_event(event("a4", "call.hangup", IN_SESSION, call_leg_id="w1",
                                        end_time="2026-11-03T20:03:00Z"))
        self.assertTrue(hung["ended"])
        self.assertEqual(hung["call"]["outcome"], "answered")
        later = calls.ingest_event(event("a5", "call.recording.saved", IN_SESSION, call_leg_id="w1"))
        replay = calls.ingest_event(event("a4", "call.hangup", IN_SESSION, call_leg_id="w1",
                                          end_time="2026-11-03T20:03:00Z"))
        self.assertFalse(later["ended"] or replay["ended"], "announced exactly once")

    def test_voicemail_is_announced_when_the_message_lands_not_at_hangup(self):
        calls.ingest_event(event("v1", "call.initiated", VM_SESSION, direction="incoming", call_leg_id="w1",
                                 **{"from": "+12157748272", "to": "+12154498437",
                                    "start_time": "2026-11-03T20:00:40Z"}))
        # Live shape (2026-10-30): initiated, hangup, cost, then the message ~28 s later. No call.answered.
        hung = calls.ingest_event(event("v3", "call.hangup", VM_SESSION, call_leg_id="w1",
                                        end_time="2026-11-03T20:01:30Z"))
        self.assertFalse(hung["ended"], "an inbound hangup may still become a voicemail")
        with patch.object(calls, "_download", side_effect=calls.CallsError("HTTP 403", "voicemail_expired")):
            vm = calls.ingest_event(event("v4", "calls.voicemail.completed", VM_SESSION,
                                          recording_url="https://voice-mail-prod.s3.us-east-1.amazonaws.com/x.wav"))
        self.assertTrue(vm["ended"])
        self.assertEqual(vm["call"]["outcome"], "voicemail")

    def test_answered_outbound_announces_at_hangup(self):
        calls.ingest_event(event("o1", "call.initiated", OUT_SESSION, direction="outgoing", to="+16099203094"))
        calls.ingest_event(event("o2", "call.answered", OUT_SESSION))
        self.assertTrue(calls.ingest_event(event("o3", "call.hangup", OUT_SESSION))["ended"])
        self.assertFalse(calls.ingest_event(event("o4", "call.cost", OUT_SESSION))["ended"])

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

    def test_cdr_session_wins_when_the_recording_session_has_no_call(self):
        self._seed()
        # HEYMA-12: Telnyx recordings and CDRs report different session ids
        # for the exact same leg. Only the CDR session has a derived call.
        self.conn.execute("UPDATE call_recordings SET call_session_id=?", (IN_SESSION,))
        name = f"telnyx-2026-11-03-{OUT_LEG}-1793666147-{'a' * 64}.mp3"
        call = calls.call_for_audio(name)
        self.assertIsNotNone(call)
        self.assertEqual(call["call_session_id"], OUT_SESSION)
        with patch.object(calls, "_fetch_for", side_effect=AssertionError("complete CDR is already local")):
            call = calls.call_for_audio(name, fetch=True)
        fm = calls.frontmatter_for(call)
        self.assertEqual((fm["call-session-id"], fm["call-leg-id"]), (OUT_SESSION, OUT_LEG))
        self.assertEqual(fm["call-outcome"], "answered")
        # Resolving identity must not rewrite the provider's recording facts.
        self.assertEqual(self.conn.execute(
            "SELECT call_session_id FROM call_recordings").fetchone()[0], IN_SESSION)

    def test_cdr_session_wins_even_if_the_other_session_has_a_call(self):
        self._seed()
        calls.upsert_leg(self.conn, cdr(IN_LEG, IN_SESSION, direction="inbound"))
        calls.derive(self.conn, IN_SESSION)
        self.conn.execute("UPDATE call_recordings SET call_session_id=?", (IN_SESSION,))
        name = f"telnyx-2026-11-03-{OUT_LEG}-1793666147.mp3"
        self.assertEqual(calls.call_for_audio(name)["call_session_id"], OUT_SESSION)
        self.assertEqual(calls.show(OUT_LEG)["call"]["call_session_id"], OUT_SESSION)

    def test_recording_and_event_sessions_remain_fallbacks_without_a_cdr(self):
        self._seed()
        self.conn.execute("DELETE FROM call_legs")
        name = f"telnyx-2026-11-03-{OUT_LEG}-1793666147.mp3"
        self.assertEqual(calls.call_for_audio(name)["call_session_id"], OUT_SESSION)
        self.conn.execute("DELETE FROM call_recordings")
        calls.record_event(self.conn, event("join", "call.hangup", OUT_SESSION, call_leg_id=OUT_LEG))
        self.assertEqual(calls.call_for_audio(name)["call_session_id"], OUT_SESSION)

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

    def test_channel_a_is_our_side_whichever_way_the_call_went(self):
        """Inbound too (verified 2026-11-11): the caller is on channel B, not A."""
        for direction in ("outbound", "inbound"):
            with self.subTest(direction=direction), patch.object(
                    calls, "call_for_audio", return_value={"direction": direction, "remote_name": "GoodLeap"}):
                self.assertEqual(calls.channel_parties("telnyx-x.mp3", fetch=False), ("Me", "GoodLeap"))


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

    def test_a_forged_url_is_never_fetched(self):
        envelope = event("vm3", "calls.voicemail.completed", VM_SESSION, recording_url="https://evil.example/x.wav")
        with patch.object(calls.urllib.request, "urlopen", side_effect=AssertionError("fetched")):
            result = calls.ingest_event(envelope)
        self.assertEqual(result["voicemail"]["error"], "voicemail_untrusted_url")
        self.assertTrue(calls._voicemail_url_ok(
            "https://voice-mail-prod.s3.us-east-1.amazonaws.com/%2B12154498437/x.wav?X-Amz-Signature=1"))
        self.assertFalse(calls._voicemail_url_ok("http://voice-mail-prod.s3.us-east-1.amazonaws.com/x.wav"))


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

    def test_a_lookup_sync_is_bounded_and_never_poses_as_the_reconcile(self):
        asked = []

        def fake_pages(path, params, key, size=250):
            asked.append((path, params))
            return iter([])

        with patch.object(calls, "_pages", side_effect=fake_pages):
            calls.sync(calls._ts("2026-10-09T11:50:00Z"), until=calls._ts("2026-10-09T16:00:00Z"),
                       key="k", deliveries=False, record=False)
        self.assertIsNone(calls.last_sync())
        self.assertEqual([p for p, _ in asked], ["/detail_records", "/recordings"])
        self.assertEqual(asked[0][1]["filter[started_at][lte]"], "2026-10-09T16:00:00Z")
        self.assertEqual(asked[1][1]["filter[created_at][lte]"], "2026-10-09T16:00:00Z")

    def test_a_webhook_only_row_is_refreshed_before_it_is_stamped(self):
        calls.ingest_event(event("w1", "call.initiated", OUT_SESSION, direction="outgoing", call_leg_id=OUT_LEG,
                                 to="+16099203094"))
        calls.ingest_event(event("w2", "call.hangup", OUT_SESSION, call_leg_id=OUT_LEG))
        name = f"telnyx-2026-11-03-{OUT_LEG}-1793666147-{'a' * 64}.mp3"
        self.assertIsNotNone(calls.call_for_audio(name))  # no fetch: the provisional row is fine
        pages = {"/recordings": [], "/detail_records": [cdr(OUT_LEG, OUT_SESSION)]}
        with patch.object(calls, "api_key", return_value="k"), \
                patch.object(calls, "_pages", side_effect=lambda path, params, key, size=250: iter(pages[path])):
            call = calls.call_for_audio(name, fetch=True)
        self.assertEqual((call["outcome"], call["talk_seconds"]), ("answered", 123))

    def test_health_names_a_voicemail_left_behind(self):
        with patch.object(calls, "_download", side_effect=calls.CallsError("HTTP 403", "voicemail_expired")):
            calls.ingest_event(event("vm9", "calls.voicemail.completed", VM_SESSION,
                                     recording_url="https://voice-mail-prod.s3.us-east-1.amazonaws.com/x.wav"))
        # occurred_at in the fixture is fixed; widen the window so it counts.
        self.assertEqual(calls.health(days=3650)["voicemail_unfetched"], ["vm9"])
        self.assertIsNotNone(calls.health()["last_webhook_at"])
        # An event with nothing to fetch (no session, no URL) can never be fetched; not a finding.
        calls.ingest_event(event("vm10", "calls.voicemail.completed", VM_SESSION))
        self.assertEqual(calls.health(days=3650)["voicemail_unfetched"], ["vm9"])

    def test_an_empty_voicemail_is_final_not_left_behind(self):
        url = "https://voice-mail-prod.s3.us-east-1.amazonaws.com/x.wav"
        empty = calls.CallsError("voicemail download too small", "voicemail_empty")
        with patch.object(calls, "_download", side_effect=empty) as download:
            first = calls.ingest_event(event("vm11", "calls.voicemail.completed", VM_SESSION, recording_url=url))
            self.assertEqual(first["voicemail"]["error"], "voicemail_empty")
            again = calls.fetch_voicemail(self.conn, "vm11")
        self.assertEqual(download.call_count, 1, "an empty message is never downloaded again")
        self.assertEqual((again["file_name"], again["empty"]), (None, True))
        self.assertEqual(calls.health(days=3650)["voicemail_unfetched"], [])

    def _recording(self, rid, leg, epoch, *, ended="2026-11-03T00:38:17Z", millis=123000, initiated="Trunking",
                   s3=True):
        urls = {"mp3": f"s3://recordings/x/2026-11-03/{leg}-{epoch}.mp3"} if s3 else \
            {"mp3": f"https://telnyx.example/{leg}-{epoch}.mp3"}
        calls.upsert_recording(self.conn, {
            "id": rid, "call_leg_id": leg, "call_session_id": OUT_SESSION, "channels": "dual",
            "initiated_by": initiated, "recording_started_at": "2026-11-03T00:36:14Z",
            "recording_ended_at": ended, "duration_millis": millis, "download_urls": urls})

    def _item(self, name):
        self.conn.execute("INSERT INTO items(item_id,sha256,path,orig_name,state,first_seen,updated_at) "
                          "VALUES(?,?,?,?,?,?,?)", (name[-24:], "0" * 64, f"/x/{name}", name, "complete",
                                                    "2026-11-03T00:40:00Z", "2026-11-03T00:40:00Z"))

    def _undelivered(self, days=3650):
        # The fixtures are dated; a wide window keeps the test independent of the wall clock.
        return calls.recordings_undelivered(self.conn, days=days)

    def test_names_a_call_recording_that_never_reached_the_inbox(self):
        # 2026-11-07..09: telnyx-s3-intake paused in Candybar; calls synced, audio never arrived.
        self._recording("r1", OUT_LEG, 1793666147)
        self._recording("r2", IN_LEG, 1793666200)
        self._recording("stub", "11111111-d71e-11f1-8b40-4674b796d526", 1793666300, millis=2900)
        self._recording("twin", "22222222-d71e-11f1-8b40-4674b796d526", 1793666400123456, initiated="RecordVerb")
        self.assertEqual(self._undelivered(), [f"{OUT_LEG}-1793666147.mp3", f"{IN_LEG}-1793666200.mp3"])
        self._item(f"telnyx-2026-11-03-{OUT_LEG}-1793666147-{'a' * 64}.mp3")  # intake name
        self._item(f"telnyx-2026-11-03-{IN_LEG}-1793666200.mp3")              # backfill-shaped name
        self.assertEqual(self._undelivered(), [])
        self.assertIn("recordings_undelivered", calls.health())

    def test_a_record_verb_twin_item_does_not_mask_the_trunk_recording(self):
        self._recording("r1", OUT_LEG, 1793666147)
        # Same leg, same second: the twin's microsecond epoch starts with the trunk epoch.
        self._item(f"telnyx-2026-11-03-{OUT_LEG}-1793666147440788-{'b' * 64}.mp3")
        self.assertEqual(self._undelivered(), [f"{OUT_LEG}-1793666147.mp3"])

    def test_a_recording_with_no_s3_object_is_a_finding_until_backfilled(self):
        # 2026-11-04/05: four answered calls never got an object in our bucket; only backfill fetches them.
        self._recording("r-none", OUT_LEG, 1793666147, s3=False)
        self.assertEqual(self._undelivered(), [f"{OUT_LEG} (no S3 object)"])
        self.conn.execute("UPDATE call_recordings SET file_name=? WHERE recording_key='r-none'",
                          (f"telnyx-2026-11-03-{OUT_LEG}-1793666147123.mp3",))
        self.assertEqual(self._undelivered(), [])

    def test_the_grace_and_the_window(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._recording("fresh", OUT_LEG, 1793666147, ended=now)
        self.assertEqual(self._undelivered(), [], "intake gets 15 minutes before it counts")
        self._recording("old", IN_LEG, 1793666200)
        self.assertEqual(self._undelivered(days=1), [])
        self.assertEqual(self._undelivered(), [f"{IN_LEG}-1793666200.mp3"])


def _load_wax_cli():
    import importlib.util
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("wax_cli_calls", str(COMPONENT / "bin" / "wax"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class RecordingsProbeWiringTest(unittest.TestCase):
    """bin/wax: the doctor row and the `wax status` calls line read recordings_undelivered."""

    @classmethod
    def setUpClass(cls):
        cls.wax = _load_wax_cli()

    def _health(self, lost):
        return {"last_sync": {"at": "2026-11-09T23:50:00Z"}, "last_webhook_at": None,
                "voicemail_unfetched": [], "recordings_undelivered": lost, "recordings_days": 30,
                "outcomes": {}, "days": 7}

    def _probes(self, lost):
        rows = []
        with patch.object(self.wax.telephony, "health", return_value=self._health(lost)), \
                patch.object(self.wax.telephony, "_get", return_value={}), \
                patch.object(self.wax.telephony, "api_key", return_value="k"):
            self.wax.probe_calls(rows.append)
        return {r["probe"]: r for r in rows}

    def test_doctor_fails_and_status_errors_on_a_stranded_recording(self):
        probes = self._probes([f"{OUT_LEG}-1793666147.mp3"])
        row = probes["call recordings delivered"]
        self.assertEqual(row["status"], self.wax.FAIL)
        self.assertIn("candybar resume telnyx-s3-intake", row["fix"])
        with patch.object(self.wax.telephony, "health", return_value=self._health(["x.mp3"])), \
                patch.object(self.wax, "_age_seconds", return_value=60):
            snap = self.wax.calls_snapshot()
        self.assertEqual((snap["state"], snap["recordings_undelivered"]), ("error", 1))

    def test_doctor_passes_when_every_recording_is_an_item(self):
        row = self._probes([])["call recordings delivered"]
        self.assertEqual(row["status"], self.wax.PASS)


class BackfillTest(_Ledger):
    def test_backfill_fetches_trunk_recordings_once_and_leaves_twins_and_stubs(self):
        paths.INBOX.mkdir(parents=True)
        for rid, by, ms in (("r-trunk", "Trunking", 60000), ("r-verb", "RecordVerb", 55000), ("r-stub", "Trunking", 900)):
            calls.upsert_recording(self.conn, {"id": rid, "call_leg_id": OUT_LEG, "call_session_id": OUT_SESSION,
                                               "initiated_by": by, "duration_millis": ms,
                                               "recording_started_at": "2026-10-09T12:00:00Z",
                                               "download_urls": {"mp3": "https://telnyx.example/x.mp3"}})
        url = f"https://s3.amazonaws.com/telephony-recorder-prod/a/2026-10-09/{OUT_LEG}-1791547200.mp3?X-Amz-Expires=600"
        asked = []

        def fake_get(path, params, key):
            asked.append(path)
            return {"data": {"download_urls": {"mp3": url}}}

        def fake_download(u, destination, trusted=False):
            self.assertTrue(trusted)
            destination.write_bytes(b"ID3" + b"\0" * 100)
            return destination

        with patch.object(calls, "_get", side_effect=fake_get), patch.object(calls, "_download", side_effect=fake_download):
            first = calls.backfill_recordings(key="k")
            again = calls.backfill_recordings(key="k")
        self.assertEqual(asked, ["/recordings/r-trunk"])
        self.assertEqual((first["candidates"], first["skipped_stub"], again["candidates"]), (2, 1, 1))
        name = first["fetched"][0]["file_name"]
        self.assertEqual(name, f"telnyx-2026-10-09-{OUT_LEG}-1791547200.mp3")
        self.assertEqual(calls.audio_identity(name)["call_leg_id"], OUT_LEG)


class ShadowAdapterCLITest(_Ledger):
    def test_event_retains_ingestion_result_with_disabled_local_core(self):
        import os
        body = event("shadow-cli-event", "call.initiated", IN_SESSION,
                     direction="incoming", call_leg_id=IN_LEG,
                     **{"from": "+12152083549", "to": "+12154498437"})
        env = os.environ.copy()
        env["WAX_ROOT"] = self.tmp.name
        proc = subprocess.run([sys.executable, str(COMPONENT / "bin/wax"), "--json", "calls", "event",
                               "--no-fetch"], input=json.dumps(body), capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["new"])
        self.assertEqual(result["call_session_id"], IN_SESSION)
        self.assertFalse(result["ended"])
        self.assertFalse((Path(self.tmp.name) / "var/telephony").exists(),
                         "disabled core must not write decision receipts")


class PassCommandTest(_Ledger):
    def test_a_non_call_recording_is_skipped(self):
        proc = subprocess.run([sys.executable, str(COMPONENT / "config/passes.d/bin/call-metadata"), "x.md", "nope"],
                              capture_output=True, text=True, env=minimal_env(self.tmp.name))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.strip().splitlines()[-1])["reason_code"], "not_a_call")


class TelephonyCommandTest(unittest.TestCase):
    def test_read_only_snapshot_missing_ledger_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            proc = subprocess.run([sys.executable, str(COMPONENT / "bin/wax"), "--json", "calls", "telephony"],
                                  capture_output=True, text=True,
                                  env=minimal_env(root))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["schema"], "heyma.telephony.snapshot.v1")
            self.assertEqual(result["health"], "unavailable")
            self.assertFalse((Path(root) / "var/wax.db").exists())

    def test_limit_validated_and_tx_parser_still_present(self):
        with tempfile.TemporaryDirectory() as root:
            env = minimal_env(root)
            proc = subprocess.run([sys.executable, str(COMPONENT / "bin/wax"), "--json", "calls",
                                   "telephony", "--limit", "0"], capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("limit", proc.stderr)
            help_proc = subprocess.run([sys.executable, str(COMPONENT / "bin/wax"), "tx", "--help"],
                                       capture_output=True, text=True, env=env)
            self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
            self.assertIn("--project", help_proc.stdout)


if __name__ == "__main__":
    unittest.main()
