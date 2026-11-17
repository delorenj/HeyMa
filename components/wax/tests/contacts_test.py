"""The contact directory (wax.contacts) and the call labels it feeds.

Hermetic: a temporary ledger and fake 1Password items; `op` is never run.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import calls, contacts, ledger, paths

WIFE = "+19085550142"
SESSION, LEG = "928fa950-e28b-11f1-bfc5-02420a0ddb1f", "928fa951-e28b-11f1-bfc5-02420a0ddb1f"
AUDIO = f"telnyx-2026-11-17-{LEG}-1794922451-{'c' * 64}.mp3"


def item(item_id, title, *, first=None, phones=(), extra=()):
    fields = [{"id": "firstname", "label": "first name", "value": first or ""}]
    fields += [{"id": fid, "type": "PHONE", "label": "default phone", "value": v} for fid, v in phones]
    fields += [dict(f) for f in extra]
    return {"id": item_id, "title": title, "category": "IDENTITY", "fields": fields}


ITEMS = [
    item("wife", "Carrie D", first="Carrie", phones=[("defphone", "(908) 555-0142")]),
    item("owner", "Jarad D", first="Jarad", phones=[("defphone", calls.CELL)]),
    item("owner-old-phone", "Jarad iPhone", first="Jarad", phones=[("defphone", "609-555-0100")]),
    item("nick", "Robert Smith", first="Robert", phones=[("cellphone", "+1 215 555 0177")],
         extra=[{"label": "call name", "value": "Bobby"}]),
    item("titled", "Plumber", phones=[("defphone", "2155550188")]),
    item("dup-a", "A", first="Alex", phones=[("defphone", "2155550199")]),
    item("dup-b", "B", first="Sam", phones=[("defphone", "2155550199")]),
    item("no-phone", "Nobody", first="Nobody"),
]


class NormalizeTest(unittest.TestCase):
    def test_written_forms_become_e164(self):
        for raw in ("(908) 555-0142", "908-555-0142", "9085550142", "19085550142", "+1 908 555 0142",
                    "sip:+19085550142@sip.telnyx.com"):
            with self.subTest(raw=raw):
                self.assertEqual(contacts.normalize(raw), WIFE)
        self.assertEqual(contacts.normalize("+442071838750"), "+442071838750")
        for raw in (None, "", "Channel B", "Me", "12345", "Carrie"):
            self.assertIsNone(contacts.normalize(raw))

    def test_mask_shows_last_four_only(self):
        self.assertEqual(contacts.mask(WIFE), "…0142")


class DirectoryTest(unittest.TestCase):
    def test_names_owner_and_ambiguity(self):
        found, stats = contacts.directory(ITEMS, own=(calls.LINE, calls.CELL))
        self.assertEqual(found[WIFE][0], "Carrie")
        self.assertEqual(found["+12155550177"][0], "Bobby", "a `call name` field wins over the first name")
        self.assertEqual(found["+12155550188"][0], "Plumber", "no first name: the item title")
        self.assertNotIn(contacts.normalize(calls.CELL), found, "the operator's own number is Me, not a contact")
        self.assertNotIn("+16095550100", found, "an item named like the operator's is the operator")
        self.assertNotIn("+12155550199", found, "two names on one number: neither")
        self.assertEqual((stats["owner_items"], stats["ambiguous"], stats["numbers"]), (2, 1, 3))


class _Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.patches = [patch.object(paths, "VAR", root / "var"), patch.object(paths, "DB", root / "var/wax.db"),
                        patch.object(paths, "INBOX", root / "inbox"),
                        patch.dict(os.environ, {"WAX_CONTACTS": "1password"}),
                        # Only an explicit fake fetch ever reads a directory: the real `op` is never run.
                        patch.object(contacts, "fetch_items", side_effect=AssertionError("a test ran the real op"))]
        for p in self.patches:
            p.start()
        self._close()
        self.conn = calls.db()
        self.items = list(ITEMS)

    def fetch(self):
        return self.items

    def sync(self):
        return contacts.refresh(force=True, fetch=self.fetch)

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


class RefreshTest(_Ledger):
    def test_refresh_then_lookup_without_op(self):
        result = self.sync()
        self.assertEqual((result["state"], result["numbers"]), ("refreshed", 3))
        self.assertIn(WIFE, result["changed"])
        self.assertEqual(contacts.name_for("908.555.0142"), "Carrie")  # fresh: fetch_items would raise
        self.assertIsNone(contacts.name_for("+12125550000"))
        self.assertEqual(self.sync()["changed"], [], "an unchanged directory changes nothing")

    def test_a_1password_outage_keeps_the_last_good_directory(self):
        self.sync()
        broken = contacts.refresh(force=True, fetch=lambda: (_ for _ in ()).throw(RuntimeError("op exited 1")))
        self.assertEqual(broken["state"], "error")
        self.assertEqual(contacts.name_for(WIFE, refresh_stale=False), "Carrie")
        self.assertIn("op exited 1", contacts.status()["error"])
        self.sync()
        self.assertIsNone(contacts.status()["error"], "a good read clears the error")

    def test_a_failed_read_backs_off_instead_of_retrying_every_lookup(self):
        calls_made = []

        def broken():
            calls_made.append(1)
            raise RuntimeError("op timed out")
        contacts.refresh(fetch=broken)
        self.assertEqual(contacts.refresh(fetch=broken)["state"], "backoff")
        self.assertEqual(len(calls_made), 1)
        self.assertEqual(contacts.refresh(force=True, fetch=self.fetch)["state"], "refreshed")

    def test_refresh_never_raises_and_never_commits_a_callers_transaction(self):
        self.sync()
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("INSERT INTO meta(k,v) VALUES('probe','1')")
        self.assertEqual(contacts.refresh(force=True, fetch=self.fetch)["state"], "deferred")
        self.assertEqual(contacts.name_for(WIFE), "Carrie")
        self.assertTrue(self.conn.in_transaction, "the caller's transaction is still open")
        self.conn.execute("ROLLBACK")
        self.assertIsNone(ledger.meta_get("probe"))
        with patch.object(contacts, "directory", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(contacts.refresh(force=True, fetch=self.fetch)["state"], "error")

    def test_off_never_reads_and_never_names(self):
        self.sync()
        with patch.dict(os.environ, {"WAX_CONTACTS": "off"}):
            self.assertEqual(contacts.refresh(force=True, fetch=self.fetch)["state"], "off")
            self.assertIsNone(contacts.name_for(WIFE))

    def test_listing_masks_numbers(self):
        self.sync()
        listed = contacts.listing()
        self.assertTrue(listed and all(row["number"].startswith("…") for row in listed))
        self.assertNotIn("9085550142", json.dumps(listed))


def inbound(session=SESSION, leg=LEG, cnam=""):
    return {"id": leg, "telnyx_session_id": session, "direction": "inbound", "cli": WIFE, "cld": calls.LINE,
            "dest_number": calls.LINE, "caller_name": cnam, "started_at": "2026-11-17T13:41:58Z",
            "answered_at": "2026-11-17T13:42:00Z", "finished_at": "2026-11-17T13:44:00Z", "call_sec": 120,
            "hangup_cause": "NORMAL_CLEARING", "hangup_details": "send_bye", "completed": 1,
            "telnyx_voicemail": False, "connection_name": "DeLoNET SIP"}


class CallLabelTest(_Ledger):
    def test_contact_names_the_call_over_cnam(self):
        self.sync()
        calls.upsert_leg(self.conn, inbound(cnam="WIRELESS CALLER"))
        row = calls.derive(self.conn, SESSION)
        self.assertIsNone(row["remote_name"], "the stored row keeps CNAM only (announcements carry no contact)")
        self.assertEqual(calls.remote_name(row), "Carrie")
        self.assertEqual(calls.channel_parties(AUDIO, fetch=False), ("Me", "Carrie"))
        self.assertEqual(calls.frontmatter_for(calls.call_for_audio(AUDIO))["call-remote-name"], "Carrie")

    def test_the_webhook_result_names_the_contact_for_the_ntfy_line_only(self):
        self.sync()
        calls.ingest_event({"data": {"id": "e1", "event_type": "call.initiated", "occurred_at": "2026-11-17T13:41:58Z",
                                     "record_type": "event", "payload": {
                                         "call_session_id": SESSION, "call_leg_id": LEG, "direction": "incoming",
                                         "from": WIFE, "to": calls.LINE, "start_time": "2026-11-17T13:41:58Z"}}})
        result = calls.ingest_event({"data": {"id": "e2", "event_type": "call.hangup",
                                              "occurred_at": "2026-11-17T13:44:00Z", "record_type": "event",
                                              "payload": {"call_session_id": SESSION, "call_leg_id": LEG,
                                                          "end_time": "2026-11-17T13:44:00Z"}}})
        self.assertEqual(result["call"]["contact_name"], "Carrie")
        self.assertIsNone(result["call"]["remote_name"], "the bloodbank event's remote_name stays CNAM")
        release = (Path(__file__).resolve().parents[2] / "n8n-workflows/telnyx_calls.py").read_text()
        ntfy = release[release.index("NTFY_MESSAGE"):release.index("def build")]
        ended = release[release.index("ENDED_DATA"):release.index("NTFY_MESSAGE")]
        self.assertIn("$json.call.contact_name ||", ntfy)
        self.assertNotIn("contact_name", ended, "names go to the operator's ntfy line, not the bloodbank event")

    def test_without_a_contact_the_number_stands(self):
        calls.upsert_leg(self.conn, inbound())
        self.assertIsNone(calls.derive(self.conn, SESSION)["remote_name"])
        self.assertEqual(calls.channel_parties(AUDIO, fetch=False), ("Me", WIFE))

    def test_a_contact_added_or_removed_later_reaches_every_call_from_that_number(self):
        self.items = []
        self.sync()
        calls.upsert_leg(self.conn, inbound())
        row = calls.derive(self.conn, SESSION)
        self.assertIsNone(calls.remote_name(row))
        self.items = list(ITEMS)
        self.sync()
        self.assertEqual(calls.remote_name(row), "Carrie")
        self.items = []
        self.sync()
        calls.upsert_leg(self.conn, inbound(session="later", leg="later-leg"))
        self.assertIsNone(calls.remote_name(calls.derive(self.conn, "later")),
                          "a removed contact never lingers through an earlier call's name")

NOTE = """---
title: Financial stress
call-direction: inbound
call-remote-number: '+19085550142'
call-started: '2026-11-17T08:41:58-05:00'
---

# Transcription

**{b} (00:02):** Hello?

**{a} (00:04):** Yeah, I know.

**{b} (00:07):** So I need gas. Call {b} back.
"""


class RelabelTest(_Ledger):
    def note(self, a="Me", b=WIFE):
        vault = self.root / "vault"
        vault.mkdir(exist_ok=True)
        md, asr = vault / "20261117-084158-financial-stress.md", self.root / "var/asr.json"
        md.write_text(NOTE.format(a=a, b=b))
        asr.write_text(json.dumps({"result": {"channel_separated": True, "channel_labels": [a, b], "segments": [
            {"start": 2.6, "end": 3.2, "text": "Hello?", "speaker": 1, "speaker_label": b},
            {"start": 4.0, "end": 6.0, "text": "Yeah", "speaker": 0, "speaker_label": a}]}}))
        now = "2026-11-17T13:45:00Z"
        self.conn.execute("INSERT INTO items(item_id,sha256,path,orig_name,state,first_seen,updated_at) "
                          "VALUES(?,?,?,?,?,?,?)", ("item1", "h", str(self.root / AUDIO), AUDIO, "complete", now, now))
        self.conn.execute("INSERT INTO transcripts(item_id,md_path,asr_path,created_at) VALUES(?,?,?,?)",
                          ("item1", str(md), str(asr), now))
        calls.upsert_leg(self.conn, inbound())
        calls.derive(self.conn, SESSION)
        self.sync()
        return md, asr

    def test_dry_run_changes_nothing(self):
        md, asr = self.note()
        before = md.read_text(), asr.read_text()
        result = calls.relabel_notes()
        self.assertEqual((result["count"], result["relabelled"][0]["turns"]), (1, 2))
        self.assertEqual((md.read_text(), asr.read_text()), before)

    def test_apply_renames_turn_heads_frontmatter_and_asr_only(self):
        md, asr = self.note()
        self.assertEqual(calls.relabel_notes(apply=True)["count"], 1)
        text = md.read_text()
        self.assertIn("**Carrie (00:02):** Hello?", text)
        self.assertIn("**Me (00:04):**", text)
        self.assertIn(f"Call {WIFE} back.", text, "only turn heads change, never the words")
        self.assertIn("call-remote-name: Carrie", text)
        self.assertIn("call-started: '2026-11-17T08:41:58-05:00'", text, "no YAML round-trip of other keys")
        result = json.loads(asr.read_text())["result"]
        self.assertEqual(result["channel_labels"], ["Me", "Carrie"])
        self.assertEqual([s["speaker_label"] for s in result["segments"]], ["Carrie", "Me"])
        self.assertEqual(calls.relabel_notes(apply=True)["count"], 0, "idempotent")

    def test_generic_channel_labels_become_me_and_the_contact(self):
        md, asr = self.note(a="Channel A", b="Channel B")
        calls.relabel_notes(apply=True)
        self.assertIn("**Me (00:04):**", md.read_text())
        self.assertIn("**Carrie (00:02):**", md.read_text())
        self.assertEqual(json.loads(asr.read_text())["result"]["channel_labels"], ["Me", "Carrie"])

    def test_backslash_names_and_folded_frontmatter_values_are_safe(self):
        self.items = [item("odd", "Odd", first="A\\1 Smith", phones=[("defphone", WIFE)])]
        md, _ = self.note()
        md.write_text(md.read_text().replace("call-started:", "call-remote-name: Some Very Long Business\n  Hotline Name\ncall-started:"))
        calls.relabel_notes(apply=True)
        text = md.read_text()
        self.assertIn("**A\\1 Smith (00:02):**", text)
        from wax import frontmatter
        self.assertEqual(frontmatter.split(text)[0]["call-remote-name"], "A\\1 Smith")
        self.assertNotIn("Hotline Name", text)

    def test_a_hand_relabelled_note_is_left_alone(self):
        md, _ = self.note()
        md.write_text(md.read_text().replace(f"**{WIFE} (", "**Wife ("))
        result = calls.relabel_notes(apply=True)
        self.assertEqual((result["count"], result["skipped"].get("label_not_in_note")), (0, 1))


if __name__ == "__main__":
    unittest.main()
