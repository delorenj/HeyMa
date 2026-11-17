"""HEYMA-25: six diagnosed Telnyx session pairs, isolated from the live ledger."""

import unittest
from pathlib import Path
from unittest.mock import patch

from wax import calls, ledger, worker
from junk_gate_test import JunkGateHarness, words

# Failing leg/session -> sibling leg/session, UTC recording start, duration in ms.
# Read-only snapshot of the six HEYMA-17 diagnostic pairs (2026-11-17).
PAIRS = (
    (("bf4ade48-d87b-11f1-92e4-02420aef2620", "bf4ad5d8-d87b-11f1-ace7-02420aef2620", "2026-11-04T18:15:46Z", 22971),
     ("bf35a6ae-d87b-11f1-849e-02420aef8d20", "bf305cee-d87b-11f1-beba-02420aef8d20", "2026-11-04T18:15:46Z", 23500)),
    (("bf6bf364-d950-11f1-ba69-4aec9a62b814", "bf6bebc6-d950-11f1-844f-4aec9a62b814", "2026-11-05T19:40:30Z", 14283),
     ("bf4c1e2c-d950-11f1-9549-02420a0de81f", "bf491a4c-d950-11f1-937c-02420a0de81f", "2026-11-05T19:40:29Z", 14760)),
    (("19e2add6-d953-11f1-93c7-02420aefde1f", "19e2a52a-d953-11f1-8bc5-02420aefde1f", "2026-11-05T19:57:22Z", 13785),
     ("19c52432-d953-11f1-9075-121d2fd9f087", "19c1646e-d953-11f1-8efe-121d2fd9f087", "2026-11-05T19:57:21Z", 14380)),
    (("26a960e8-de10-11f1-acaf-3e5fb29bdadd", "26a958c8-de10-11f1-998a-3e5fb29bdadd", "2026-11-11T20:41:13Z", 60423),
     ("2686f422-de10-11f1-9715-02420a0de820", "2683e25a-de10-11f1-ae17-02420a0de820", "2026-11-11T20:41:13Z", 60400)),
    (("72058306-de14-11f1-91b0-c6cc9409bc24", "72057910-de14-11f1-898f-c6cc9409bc24", "2026-11-11T21:11:30Z", 59663),
     ("71e75c78-de14-11f1-a90c-32b4181711b8", "71e1f6ca-de14-11f1-82c3-32b4181711b8", "2026-11-11T21:11:30Z", 59780)),
    (("5bb1bc1e-de23-11f1-9eb6-5eb80c52c54c", "5bb1b1ba-de23-11f1-aa1f-5eb80c52c54c", "2026-11-11T22:58:22Z", 6637),
     ("5b9f0c40-de23-11f1-a077-02420aef8fa1", "5b9a8a62-de23-11f1-9452-02420aef8fa1", "2026-11-11T22:58:21Z", 6960)),
)


class TwinPreventionTest(JunkGateHarness):
    def seed(self, pair):
        twin, kept = pair
        conn = calls.db()
        for leg, session, start, duration in pair:
            conn.execute("INSERT INTO call_recordings(recording_key,kind,call_leg_id,call_session_id,started_at,"
                         "duration_millis,object_name,raw,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (leg, "trunk", leg, session, start, duration,
                          f"{leg}-1794431481.mp3", "{}", start))
        conn.execute("INSERT INTO call_legs(call_leg_id,call_session_id,started_at,raw,updated_at) VALUES(?,?,?,?,?)",
                     (kept[0], kept[1], kept[2], "{}", kept[2]))
        conn.execute("INSERT INTO calls(call_session_id,started_at,outcome,first_seen,updated_at) VALUES(?,?,?,?,?)",
                     (kept[1], kept[2], "answered", kept[2], kept[2]))

    def name(self, rec):
        return f"telnyx-{rec[2][:10]}-{rec[0]}-1794431481.mp3"

    def drop_rec(self, rec, index):
        return self.drop(self.name(rec), words(40), payload=(f"unique audio {index} ".encode() * 40))

    def test_same_leg_distinct_recording_hour_later_publishes_own_note(self):
        twin, kept = PAIRS[1]
        self.seed((twin, kept))
        self.drop_rec(kept, 430)
        first = self.run_once()
        note = Path(first["transcribe"]["md_path"])
        before = note.read_bytes()
        later_start = "2026-11-05T20:40:29Z"
        later_twin = "bbbbbbbb-d950-11f1-ba69-4aec9a62b814"
        later_session = "cccccccc-d950-11f1-ba69-4aec9a62b814"
        conn = calls.db()
        conn.execute("INSERT INTO call_recordings(recording_key,kind,call_leg_id,call_session_id,started_at,"
                     "duration_millis,raw,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     ("later-kept-recording", "trunk", kept[0], kept[1], later_start, 15000, "{}", later_start))
        conn.execute("INSERT INTO call_recordings(recording_key,kind,call_leg_id,call_session_id,started_at,"
                     "duration_millis,raw,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (later_twin, "trunk", later_twin, later_session,
                      "2026-11-05T20:40:31Z", 14000, "{}", later_start))
        later_name = self.name(kept).replace("-1794431481.mp3", "-1794435081.mp3")
        self.drop(later_name, words(40), payload=b"unrelated later call " * 50)
        second = self.run_once()
        self.assertIn("transcribe", second)
        self.assertEqual(self.transcript_rows(), 2)
        self.assertEqual(note.read_bytes(), before)
        self.assertTrue(Path(second["transcribe"]["md_path"]).is_file())
        self.assertIsNone(self.run_once())

    def test_late_own_cdr_preserves_published_pair_but_not_unrelated_same_leg(self):
        twin, kept = PAIRS[0]
        self.seed((twin, kept))
        self.drop_rec(twin, 401)
        twin_id = self.item
        first = self.run_once()
        note = Path(first["transcribe"]["md_path"])
        original = note.read_bytes()
        self.assertEqual(ledger.connect().execute(
            "SELECT twin_of_recording_key FROM items WHERE item_id=?", (twin_id,)).fetchone()[0], kept[0])
        own_session = "aaaaaaaa-d87b-11f1-92e4-02420aef2620"
        conn = calls.db()
        conn.execute("INSERT INTO call_legs(call_leg_id,call_session_id,started_at,raw,updated_at) VALUES(?,?,?,?,?)",
                     (twin[0], own_session, twin[2], "{}", twin[2]))
        conn.execute("INSERT INTO calls(call_session_id,started_at,outcome,first_seen,updated_at) VALUES(?,?,?,?,?)",
                     (own_session, twin[2], "answered", twin[2], twin[2]))
        self.assertIsNone(calls.twin_of(self.name(twin)))
        self.drop_rec(kept, 402)
        second = self.run_once()
        self.assertEqual(second["skipped"]["reason"], "duplicate_call")
        self.assertEqual(self.transcript_rows(), 1)
        self.assertEqual(note.read_bytes(), original)

        # A distinct receipt on the primary's same leg an hour later owns its note.
        late = self.name(kept).replace("-1794431481.mp3", "-1794435081.mp3")
        self.drop(late, words(40), payload=b"distinct later conversation " * 40)
        own = self.run_once()
        self.assertIn("transcribe", own)
        self.assertEqual(self.transcript_rows(), 2)
        self.assertTrue(Path(own["transcribe"]["md_path"]).is_file())
        self.assertIsNone(self.run_once())

    def test_redelivered_twin_parks_extra_bytes_without_changing_canonical(self):
        twin, kept = PAIRS[3]
        self.seed((twin, kept))
        self.drop_rec(kept, 410)
        self.run_once()
        self.drop_rec(twin, 411)
        twin_id = self.item
        first = self.run_once()
        canonical = Path(first["skipped"]["path"])
        original = canonical.read_bytes()
        self.drop_rec(twin, 411)
        self.assertEqual(self.item, twin_id)
        second = self.run_once()
        self.assertEqual(second["reason"], "duplicate_call_redropped")
        self.assertEqual(Path(second["parked"]).read_bytes(), original)
        self.assertEqual(canonical.read_bytes(), original)
        self.assertEqual(ledger.connect().execute("SELECT path FROM items WHERE item_id=?",
                                                  (twin_id,)).fetchone()[0], str(canonical))
        self.assertEqual(self.transcript_rows(), 1)
        self.assertIsNone(self.run_once())

    def test_redelivery_after_rename_crash_recovers_then_parks_extra(self):
        twin, kept = PAIRS[4]
        self.seed((twin, kept))
        self.drop_rec(kept, 420)
        self.run_once()
        self.drop_rec(twin, 421)
        twin_id = self.item
        real_move = worker.rename.move_noclobber
        def crash_after_move(*args, **kwargs):
            real_move(*args, **kwargs)
            raise RuntimeError("injected crash after rename")
        with patch.object(worker.rename, "move_noclobber", side_effect=crash_after_move):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.run_once()
        canonical = Path(ledger.connect().execute("SELECT path FROM items WHERE item_id=?",
                                                 (twin_id,)).fetchone()[0])
        original = canonical.read_bytes()
        self.drop_rec(twin, 421)
        self.assertEqual(self.item, twin_id)
        self.assertEqual(self.run_once()["skipped"]["reason"], "duplicate_call")
        extra = self.run_once()
        self.assertEqual(extra["reason"], "duplicate_call_redropped")
        self.assertEqual(Path(extra["parked"]).read_bytes(), original)
        self.assertEqual(canonical.read_bytes(), original)
        self.assertEqual(self.transcript_rows(), 1)
        self.assertIsNone(self.run_once())

    def test_six_pairs_link_mark_and_never_publish_second_notes(self):
        for index, pair in enumerate(PAIRS):
            with self.subTest(pair=pair[0][0][:8]):
                twin, kept = pair
                self.seed(pair)
                primary_audio = self.drop_rec(kept, index * 2)
                primary_bytes = primary_audio.read_bytes()
                primary = self.run_once()
                primary_note = Path(primary["transcribe"]["md_path"])
                original_note = primary_note.read_bytes()
                self.assertTrue(primary_note.is_file())
                self.assertNotIn("duplicate_of_call_session_id", calls.call_for_audio(self.name(kept)))
                self.assertEqual(calls.call_for_audio(self.name(twin))["call_session_id"], kept[1])
                self.assertEqual(calls.call_for_audio(self.name(twin))["duplicate_of_call_session_id"], kept[1])
                twin_audio = self.drop_rec(twin, index * 2 + 1)
                twin_bytes = twin_audio.read_bytes()
                result = self.run_once()
                self.assertEqual(result["skipped"]["reason"], "duplicate_call")
                self.assertEqual(result["skipped"]["call_session_id"], kept[1])
                self.assertEqual(self.state_of(self.item), ("skipped", "duplicate_call"))
                row = ledger.connect().execute("SELECT path,duplicate_of_call_session_id FROM items WHERE item_id=?",
                                               (self.item,)).fetchone()
                self.assertEqual(row["duplicate_of_call_session_id"], kept[1])
                self.assertEqual(Path(row["path"]).read_bytes(), twin_bytes)
                self.assertEqual(Path(primary["parked"]).read_bytes(), primary_bytes)
                self.assertEqual(primary_note.read_bytes(), original_note)
                self.assertEqual(self.transcript_rows(), index + 1)
                self.assertEqual(len(self.transcriber_calls()), index + 1)
                self.assertEqual(len(self.vault_files()), index + 1)
                self.assertIsNone(self.run_once(), "a skipped twin must not run again")

    def test_unmatched_and_ambiguous_recordings_remain_ordinary(self):
        twin, kept = PAIRS[0]
        self.seed((twin, kept))
        conn = calls.db()
        conn.execute("UPDATE call_recordings SET duration_millis=99999 WHERE call_leg_id=?", (kept[0],))
        self.assertIsNone(calls.twin_of(self.name(twin)))
        self.assertIsNone(calls.call_for_audio(self.name(twin)))
        audio = self.drop_rec(twin, 101)
        result = self.run_once()
        self.assertIn("transcribe", result)
        self.assertIsNone(ledger.connect().execute("SELECT duplicate_of_call_session_id FROM items WHERE item_id=?",
                                                  (self.item,)).fetchone()[0])
        self.assertTrue(Path(result["transcribe"]["md_path"]).is_file())

        second_twin, second_kept = PAIRS[1]
        self.seed((second_twin, second_kept))
        another_leg = "ffffffff-d950-11f1-ba69-4aec9a62b814"
        another_session = "eeeeeeee-d950-11f1-ba69-4aec9a62b814"
        conn.execute("INSERT INTO call_recordings(recording_key,kind,call_leg_id,call_session_id,started_at,"
                     "duration_millis,raw,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (another_leg, "trunk", another_leg, another_session, second_twin[2], second_twin[3], "{}", second_twin[2]))
        conn.execute("INSERT INTO call_legs(call_leg_id,call_session_id,started_at,raw,updated_at) VALUES(?,?,?,?,?)",
                     (another_leg, another_session, second_twin[2], "{}", second_twin[2]))
        self.assertIsNone(calls.twin_of(self.name(second_twin)))
        self.assertIsNone(calls.call_for_audio(self.name(second_twin)))
        audio2 = self.drop_rec(second_twin, 102)
        result2 = self.run_once()
        self.assertIn("transcribe", result2)
        self.assertIsNone(ledger.connect().execute("SELECT duplicate_of_call_session_id FROM items WHERE item_id=?",
                                                  (self.item,)).fetchone()[0])
        self.assertEqual(self.transcript_rows(), 2)

    def test_own_cdr_leg_wins_over_recording_session_and_is_never_skipped(self):
        twin, kept = PAIRS[0]
        self.seed((twin, kept))
        conn = calls.db()
        own_session = "aaaaaaaa-d87b-11f1-92e4-02420aef2620"
        conn.execute("INSERT INTO call_legs(call_leg_id,call_session_id,started_at,raw,updated_at) VALUES(?,?,?,?,?)",
                     (twin[0], own_session, twin[2], "{}", twin[2]))
        conn.execute("INSERT INTO calls(call_session_id,started_at,outcome,first_seen,updated_at) VALUES(?,?,?,?,?)",
                     (own_session, twin[2], "answered", twin[2], twin[2]))
        self.assertIsNone(calls.twin_of(self.name(twin)))
        own = calls.call_for_audio(self.name(twin))
        self.assertEqual(own["call_session_id"], own_session)
        self.assertNotIn("duplicate_of_call_session_id", own)
        self.drop_rec(twin, 201)
        result = self.run_once()
        self.assertIn("transcribe", result)
        self.assertEqual(self.transcript_rows(), 1)
        self.assertIsNone(conn.execute("SELECT duplicate_of_call_session_id FROM items WHERE item_id=?",
                                       (self.item,)).fetchone()[0])

    def test_both_delivery_orders_keep_one_note(self):
        for first_is_twin in (False, True):
            with self.subTest(first_is_twin=first_is_twin):
                pair = PAIRS[1 if first_is_twin else 0]
                self.seed(pair)
                first, second = pair if first_is_twin else pair[::-1]
                before_rows = self.transcript_rows()
                before_files = len(self.vault_files())
                before_calls = len(self.transcriber_calls())
                self.drop_rec(first, 211 if first_is_twin else 210)
                first_id = self.item
                first_result = self.run_once()
                first_note = Path(first_result["transcribe"]["md_path"])
                self.drop_rec(second, 221 if first_is_twin else 220)
                result = self.run_once()
                self.assertEqual(result["skipped"]["reason"], "duplicate_call")
                self.assertEqual(self.transcript_rows(), before_rows + 1)
                self.assertEqual(len(self.vault_files()), before_files + 1)
                self.assertEqual(len(self.transcriber_calls()), before_calls + 1)
                self.assertTrue(first_note.is_file())
                self.assertTrue(Path(result["skipped"]["path"]).is_file())
                self.assertIsNone(self.run_once())
                self.assertEqual(self.state_of(first_id)[0], "transcribed")

    def test_primary_not_delivered_or_unpublished_does_not_discard_only_note(self):
        for index, situation in enumerate(("absent", "pending", "failed", "pruned", "missing")):
            with self.subTest(situation=situation):
                pair = PAIRS[index]
                self.seed(pair)
                kept_audio = None
                if situation != "absent":
                    kept_audio = self.drop_rec(pair[1], 230 + index)
                    kept_id = self.item
                    if situation == "failed":
                        ledger.set_item_state(kept_id, "failed", cause="fixture")
                    if situation in ("pruned", "missing"):
                        stale = self.vault / f"stale-{index}.md"
                        conn = ledger.connect()
                        conn.execute("INSERT INTO transcripts(item_id,md_path,created_at,pruned_at) VALUES(?,?,?,?)",
                                     (kept_id, str(stale), "2026-11-17T00:00:00Z",
                                      "2026-11-17T01:00:00Z" if situation == "pruned" else None))
                self.drop_rec(pair[0], 240 + index)
                twin_id = self.item
                # In the pending case the older primary must remain unpublished while
                # the twin is selected. Failed/pruned/missing cases are likewise real.
                if situation != "absent":
                    with patch.object(worker.state, "inbox_items", return_value=[self.root / "inbox" / self.name(pair[0]), kept_audio]):
                        result = self.run_once()
                else:
                    result = self.run_once()
                self.assertIn("transcribe", result)
                self.assertEqual(ledger.connect().execute("SELECT duplicate_of_call_session_id FROM items WHERE item_id=?",
                                                          (twin_id,)).fetchone()[0], pair[1][1], situation)
                self.assertTrue(Path(result["transcribe"]["md_path"]).is_file())
                if situation in ("pending", "pruned", "missing"):
                    # Previously published but pruned/missing audio must not create a
                    # second note when the primary later becomes processable.
                    if situation in ("pruned", "missing"):
                        ledger.set_item_state(kept_id, "pending", cause="fixture_retry")
                    later = self.run_once()
                    self.assertEqual(later["skipped"]["reason"], "duplicate_call")
                self.assertEqual(len(self.vault_files()), index + 1)
                if kept_audio is not None:
                    kept_audio.unlink(missing_ok=True)
                ledger.connect().execute("DELETE FROM call_recordings")
                ledger.connect().execute("DELETE FROM call_legs")
                ledger.connect().execute("DELETE FROM calls")

    def test_interrupted_move_is_completed_by_worker_retry(self):
        self.seed(PAIRS[5])
        self.drop_rec(PAIRS[5][1], 270)
        self.run_once()
        self.drop_rec(PAIRS[5][0], 271)
        twin_id = self.item
        real_move = worker.rename.move_noclobber
        def crash_after_move(*args, **kwargs):
            real_move(*args, **kwargs)
            raise RuntimeError("injected crash after rename")
        with patch.object(worker.rename, "move_noclobber", side_effect=crash_after_move):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.run_once()
        row = ledger.connect().execute("SELECT state,path FROM items WHERE item_id=?", (twin_id,)).fetchone()
        self.assertEqual(row["state"], "archived")
        self.assertTrue(Path(row["path"]).is_file())
        recovered = self.run_once()
        self.assertEqual(recovered["skipped"]["reason"], "duplicate_call")
        self.assertEqual(self.state_of(twin_id), ("skipped", "duplicate_call"))
        self.assertIsNone(self.run_once())
        self.assertEqual(self.transcript_rows(), 1)

    def test_failed_move_keeps_inbox_path_and_retry_succeeds(self):
        self.seed(PAIRS[4])
        self.drop_rec(PAIRS[4][1], 280)
        self.run_once()
        source = self.drop_rec(PAIRS[4][0], 281)
        twin_id = self.item
        with patch.object(worker.rename, "renameat2_noreplace", side_effect=OSError("injected pre-move failure")):
            with self.assertRaisesRegex(OSError, "injected pre-move failure"):
                self.run_once()
        row = ledger.connect().execute("SELECT state,path FROM items WHERE item_id=?", (twin_id,)).fetchone()
        self.assertEqual(row["state"], "archived")
        self.assertEqual(row["path"], str(source))
        self.assertTrue(source.is_file())
        result = self.run_once()
        self.assertEqual(result["skipped"]["reason"], "duplicate_call")
        self.assertEqual(self.state_of(twin_id), ("skipped", "duplicate_call"))
        self.assertIsNone(self.run_once())

    def test_twin_without_primary_note_does_not_duplicate_on_redelivery(self):
        self.seed(PAIRS[3])
        twin = PAIRS[3][0]
        self.drop_rec(twin, 290)
        original_id = self.item
        first = self.run_once()
        note_path = Path(first["transcribe"]["md_path"])
        original = note_path.read_bytes()
        self.assertIsNone(self.run_once())
        self.drop_rec(PAIRS[3][1], 291)
        second = self.run_once()
        self.assertEqual(second["skipped"]["reason"], "duplicate_call")
        self.assertTrue(note_path.is_file())
        self.assertEqual(note_path.read_bytes(), original)
        self.assertEqual(self.transcript_rows(), 1)
        self.assertEqual(self.state_of(original_id)[0], "transcribed")
        self.assertIsNone(self.run_once())
    def test_ingested_fractional_start_does_not_suppress_outside_window(self):
        twin, kept = PAIRS[0]
        twin = (twin[0], twin[1], "2026-11-04T18:15:46.100Z", 22971)
        kept = (kept[0], kept[1], "2026-11-04T18:15:48.800Z", 22000)
        conn = calls.db()
        names = {}
        for rec, epoch in ((twin, 1793816169), (kept, 1793816170)):
            obj = f"{rec[0]}-{epoch}.mp3"
            names[rec[0]] = f"telnyx-{rec[2][:10]}-{obj}"
            calls.upsert_recording(conn, {
                "id": rec[0], "call_leg_id": rec[0], "call_session_id": rec[1],
                "initiated_by": "Trunking", "recording_started_at": rec[2],
                "duration_millis": rec[3], "download_urls": {"mp3": f"s3://recordings/x/{obj}"},
            })
        calls.upsert_leg(conn, {"id": kept[0], "telnyx_session_id": kept[1],
                                "started_at": kept[2], "answered_at": kept[2],
                                "finished_at": "2026-11-04T18:16:10.800Z",
                                "completed": True, "call_sec": 22})
        calls.derive(conn, kept[1])
        self.assertEqual([r[0] for r in conn.execute("SELECT started_at FROM call_recordings ORDER BY started_at")],
                         ["2026-11-04T18:15:46.100000Z", "2026-11-04T18:15:48.800000Z"])
        self.drop(names[kept[0]], words(40), payload=b"primary fractional audio " * 40)
        first = self.run_once()
        note = Path(first["transcribe"]["md_path"])
        before = note.read_bytes()
        self.drop(names[twin[0]], words(40), payload=b"twin fractional audio " * 40)
        self.assertIsNone(calls.twin_of(names[twin[0]]))
        self.assertIn("transcribe", self.run_once())
        self.assertEqual(self.transcript_rows(), 2)
        self.assertEqual(note.read_bytes(), before)

    def test_object_epochs_are_not_recording_starts_in_either_order(self):
        for index, first_is_twin in enumerate((False, True)):
            with self.subTest(first_is_twin=first_is_twin):
                twin, kept = PAIRS[index]
                twin = (twin[0], twin[1], "2026-11-04T18:15:48Z", 24000)
                kept = (kept[0], kept[1], "2026-11-04T18:15:46Z", 23000)
                names = {}
                conn = calls.db()
                for rec, epoch in ((twin, 1793816172), (kept, 1793816169)):
                    obj = f"{rec[0]}-{epoch}.mp3"
                    names[rec[0]] = f"telnyx-{rec[2][:10]}-{obj[:-4]}-" + ("a" if rec == twin else "b") * 64 + ".mp3"
                    calls.upsert_recording(conn, {
                        "id": rec[0], "call_leg_id": rec[0], "call_session_id": rec[1],
                        "initiated_by": "Trunking", "recording_started_at": rec[2],
                        "duration_millis": rec[3], "download_urls": {"mp3": f"s3://recordings/x/{obj}"},
                    })
                calls.upsert_leg(conn, {"id": kept[0], "telnyx_session_id": kept[1],
                                        "started_at": kept[2], "completed": True, "call_sec": 23})
                calls.derive(conn, kept[1])
                self.assertEqual(calls.twin_of(names[twin[0]])["start_delta_s"], 2)
                first, later = (twin, kept) if first_is_twin else (kept, twin)
                self.drop(names[first[0]], words(40), payload=f"first conversation {index} ".encode() * 40)
                result = self.run_once()
                note = Path(result["transcribe"]["md_path"])
                before = note.read_bytes()
                self.drop(names[later[0]], words(40), payload=f"second conversation {index} ".encode() * 40)
                self.assertEqual(self.run_once()["skipped"]["reason"], "duplicate_call")
                self.assertEqual(note.read_bytes(), before)
                self.assertEqual(self.transcript_rows(), index + 1)
                conn.execute("DELETE FROM call_recordings")
                conn.execute("DELETE FROM call_legs")
                conn.execute("DELETE FROM calls")

    def test_distinct_primary_note_same_leg_one_second_away_is_not_pair_note(self):
        twin, kept = PAIRS[0]
        self.seed((twin, kept))
        self.drop_rec(kept, 801)
        self.run_once()
        conn = calls.db()
        conn.execute("UPDATE call_recordings SET duration_millis=50000 WHERE recording_key=?", (twin[0],))
        conn.execute("INSERT INTO call_recordings(recording_key,kind,call_leg_id,call_session_id,started_at,"
                     "duration_millis,object_name,raw,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     ("second-kept-recording", "trunk", kept[0], kept[1], "2026-11-04T18:15:47Z",
                      50000, f"{kept[0]}-1794431482.mp3", "{}", kept[2]))
        twin_name = self.name(twin).replace("-1794431481.mp3", "-1794431482.mp3")
        # The newer twin is a separate object; the original leg row is no longer its identity.
        conn.execute("UPDATE call_recordings SET object_name=? WHERE recording_key=?",
                     (f"{twin[0]}-1794431482.mp3", twin[0]))
        self.assertEqual(calls.twin_of(twin_name)["twin_of_recording_key"], "second-kept-recording")
        self.drop(twin_name, words(40), payload=b"separate conversation requiring a note " * 45)
        self.assertIn("transcribe", self.run_once())
        self.assertEqual(self.transcript_rows(), 2)

    def test_distinct_primary_recording_one_second_away_is_not_paired_twin(self):
        twin, kept = PAIRS[0]
        self.seed((twin, kept))
        self.drop_rec(twin, 900)
        self.run_once()
        conn = calls.db()
        conn.execute("INSERT INTO call_recordings(recording_key,kind,call_leg_id,call_session_id,started_at,"
                     "duration_millis,object_name,raw,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     ("second-kept-recording", "trunk", kept[0], kept[1], "2026-11-04T18:15:47Z",
                      50000, f"{kept[0]}-1794431482.mp3", "{}", kept[2]))
        later = self.name(kept).replace("-1794431481.mp3", "-1794431482.mp3")
        self.drop(later, words(40), payload=b"independent primary call data " * 55)
        self.assertIn("transcribe", self.run_once())
        self.assertEqual(self.transcript_rows(), 2)

    def test_ambiguous_and_missing_object_identity_never_suppress(self):
        twin, kept = PAIRS[0]
        self.seed((twin, kept))
        self.drop_rec(kept, 901)
        self.run_once()
        conn = calls.db()
        conn.execute("INSERT INTO call_recordings(recording_key,kind,call_leg_id,call_session_id,started_at,"
                     "duration_millis,object_name,raw,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     ("conflicting-object", "trunk", kept[0], kept[1], kept[2],
                      kept[3], f"{kept[0]}-1794431481.mp3", "{}", kept[2]))
        self.drop_rec(twin, 902)
        self.assertIn("transcribe", self.run_once())
        self.assertEqual(self.transcript_rows(), 2)
        # An unlabelled legacy primary is likewise not evidence for suppression.
        twin2, kept2 = PAIRS[1]
        self.seed((twin2, kept2))
        self.drop_rec(kept2, 903)
        self.run_once()
        conn.execute("UPDATE call_recordings SET object_name=NULL WHERE recording_key=?", (kept2[0],))
        self.drop_rec(twin2, 904)
        self.assertIn("transcribe", self.run_once())
        self.assertEqual(self.transcript_rows(), 4)

    def test_fractional_start_bounds_and_ambiguity(self):
        for index, (a, b, expected) in enumerate((("46.100", "48.800", False),
                                                    ("46.900", "44.950", True),
                                                    ("46.000", "48.000", True),
                                                    ("46.000", "48.001", False),
                                                    ("46.000", "44.000", True))):
            with self.subTest(start=(a, b)):
                pair = PAIRS[index]
                self.seed(pair)
                conn = calls.db()
                for rec, frac in zip(pair, (a, b)):
                    conn.execute("UPDATE call_recordings SET started_at=? WHERE call_leg_id=?",
                                 (f"2026-11-04T18:15:{frac}Z", rec[0]))
                self.assertEqual(calls.twin_of(self.name(pair[0])) is not None, expected)
                conn.execute("DELETE FROM call_recordings")
                conn.execute("DELETE FROM call_legs")
                conn.execute("DELETE FROM calls")
        pair = PAIRS[5]
        self.seed(pair)
        third = "ffffffff-de23-11f1-ba69-4aec9a62b814"
        session = "eeeeeeee-de23-11f1-ba69-4aec9a62b814"
        conn = calls.db()
        conn.execute("INSERT INTO call_recordings(recording_key,kind,call_leg_id,call_session_id,started_at,duration_millis,raw,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (third, "trunk", third, session, "2026-11-11T22:58:23.900Z", pair[0][3], "{}", pair[0][2]))
        conn.execute("INSERT INTO call_legs(call_leg_id,call_session_id,started_at,raw,updated_at) VALUES(?,?,?,?,?)",
                     (third, session, pair[0][2], "{}", pair[0][2]))
        self.assertIsNone(calls.twin_of(self.name(pair[0])))
    def test_calls_ledger_failure_does_not_block_publication(self):
        audio = self.drop_rec(PAIRS[0][0], 104)
        with patch.object(calls, "twin_of", side_effect=RuntimeError("ledger unavailable")):
            result = self.run_once()
        self.assertIn("transcribe", result)
        self.assertTrue(Path(result["transcribe"]["md_path"]).is_file())
        self.assertIsNone(ledger.connect().execute("SELECT duplicate_of_call_session_id FROM items WHERE item_id=?",
                                                  (self.item,)).fetchone()[0])

    def test_existing_transcribed_twin_is_not_cleaned_up(self):
        pair = PAIRS[2]
        self.seed(pair)
        audio = self.drop_rec(pair[0], 103)
        ledger.set_item_state(self.item, "transcribed", cause="fixture")
        existing = self.vault / "existing-twin.md"
        existing.write_text("operator-owned note\n")
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.assertEqual(existing.read_text(), "operator-owned note\n")
        self.assertIsNone(ledger.connect().execute("SELECT duplicate_of_call_session_id FROM items WHERE item_id=?",
                                                  (self.item,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
