"""`wax split` (HEYMA-18): waveform detection, cutting, provenance, submission, exit codes.

Fixtures are synthesized in the test process (tests/split_fixtures.py, 10x shorter than a meeting) and
never committed; thresholds are scaled with them. Boundary measurements are asserted against the
fixture's known truth (`overrun_at_s`), not against whatever the detector happens to say.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import split_support as sup
from wax import ledger, paths, split

PARAMS = split.Params(**sup.SCALED)
SLACK_S = 3.0          # a found cut may sit this far from the true overrun start (pad + 1 s blocks)


def analyze(name: str, params: split.Params = PARAMS) -> dict:
    return split.analyze_file(sup.fixture_ogg(name), params)


class DetectionTest(unittest.TestCase):
    def test_clear_overrun_in_a_quiet_room(self):
        det = analyze("meeting_overrun_quiet")
        truth = sup.fixture_info("meeting_overrun_quiet")["overrun_at_s"]
        self.assertTrue(det["found"], det)
        self.assertAlmostEqual(det["boundary"]["quiet_start_s"], truth, delta=SLACK_S)
        self.assertAlmostEqual(det["boundary"]["cut_s"], truth + PARAMS.pad_s, delta=SLACK_S)
        self.assertAlmostEqual(det["boundary"]["quiet_end_s"], det["duration_s"], delta=1.5)

    def test_clear_overrun_over_an_hvac_bed(self):
        # The room is -46 dBFS here, 16 dB louder than the quiet fixture: only an adaptive floor sees it.
        det = analyze("meeting_overrun_hvac")
        truth = sup.fixture_info("meeting_overrun_hvac")["overrun_at_s"]
        self.assertTrue(det["found"], det)
        self.assertAlmostEqual(det["boundary"]["quiet_start_s"], truth, delta=SLACK_S)
        self.assertGreater(det["stats"]["noise_floor_db"], -55)

    def test_a_click_inside_the_overrun_does_not_hide_the_boundary(self):
        det = analyze("meeting_overrun_with_click")
        truth = sup.fixture_info("meeting_overrun_with_click")["overrun_at_s"]
        self.assertTrue(det["found"], det)
        self.assertAlmostEqual(det["boundary"]["quiet_start_s"], truth, delta=SLACK_S)
        # the click sat inside the quiet run, so the run is one run and it is closed over the blip
        self.assertGreaterEqual(det["boundary"]["quiet_length_s"], PARAMS.min_quiet_s)

    def test_continuous_activity_is_no_boundary(self):
        det = analyze("continuous_activity")
        self.assertFalse(det["found"])
        self.assertEqual(det["reason"], "no_contrast")

    def test_all_quiet_is_no_boundary(self):
        det = analyze("all_quiet")
        self.assertFalse(det["found"])
        self.assertEqual(det["reason"], "no_activity")

    def test_all_quiet_hvac_is_no_boundary_not_a_false_cut(self):
        det = analyze("all_quiet_hvac")
        self.assertFalse(det["found"])
        self.assertIn(det["reason"], ("no_contrast", "no_activity"))

    def test_ordinary_pauses_never_trigger_a_cut(self):
        det = analyze("meeting_with_pauses")
        self.assertFalse(det["found"])
        self.assertEqual(det["reason"], "no_sustained_quiet")
        # the 9 s mid-meeting pause is visible as a quiet run and was correctly not chosen
        self.assertTrue(any(run["length_s"] >= 8 for run in det["quiet_runs"]))

    def test_a_long_break_followed_by_more_meeting_is_not_a_boundary(self):
        det = analyze("meeting_break_then_more")
        self.assertFalse(det["found"])
        self.assertEqual(det["reason"], "quiet_not_trailing")

    def test_thresholds_are_parameters(self):
        # The same audio flips with the flags: a 4 s trailing quiet is only a boundary if asked for.
        tight = split.Params(min_quiet_s=3.0, tail_slack_s=3.0, min_active_s=20.0, pad_s=1.0)
        self.assertTrue(analyze("meeting_with_pauses", tight)["found"])
        self.assertFalse(analyze("meeting_with_pauses")["found"])

    def test_too_short_audio_reports_too_short(self):
        import numpy as np
        frames = np.concatenate((np.full(200, -20.0), np.full(200, -62.0))).astype(np.float32)   # 20 s loud, 20 s quiet
        det = split.detect(frames, 40.0, PARAMS)
        self.assertFalse(det["found"])
        self.assertEqual(det["reason"], "too_short")


class ItemSplitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        self.ctx = sup.runtime(self.root)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)

    def stage(self, name: str, as_name: str = "meeting.ogg") -> Path:
        dest = paths.INBOX / as_name
        shutil.copy2(sup.fixture_ogg(name), dest)
        return dest

    def test_dry_run_changes_nothing(self):
        audio = self.stage("meeting_overrun_quiet")
        before = ledger.sha256_file(audio)
        report = split.run("meeting.ogg", PARAMS, dry_run=True)
        self.assertEqual(report["outcome"], "proposed")
        self.assertEqual(split.exit_code(report), 0)
        self.assertEqual(ledger.sha256_file(audio), before)
        self.assertFalse(split.split_root().exists())
        self.assertIsNone(ledger.connect().execute("SELECT 1 FROM items").fetchone())

    def test_split_cuts_two_parts_and_preserves_the_original(self):
        audio = self.stage("meeting_overrun_quiet")
        sha = ledger.sha256_file(audio)
        report = split.run("meeting.ogg", PARAMS)
        self.assertEqual(report["outcome"], "split")
        self.assertEqual(split.exit_code(report), 0)
        hold = Path(report["held_dir"])
        original = hold / "meeting.ogg"
        self.assertEqual(ledger.sha256_file(original), sha, "the original is preserved byte for byte")
        self.assertFalse(audio.exists(), "the original left the inbox so the worker cannot pick it up")
        segs = report["segments"]
        self.assertEqual([s["kind"] for s in segs], ["active", "quiet-tail"])
        self.assertEqual(segs[0]["start_s"], 0.0)
        self.assertAlmostEqual(segs[0]["end_s"], segs[1]["start_s"], delta=0.01)
        total = sum(s["duration_s"] for s in segs)
        self.assertAlmostEqual(total, report["detection"]["duration_s"], delta=2.5)
        for seg in segs:
            part = hold / seg["file"]
            self.assertEqual(ledger.sha256_file(part), seg["sha256"])
            self.assertEqual(seg["state"], "held")
            self.assertGreater(seg["duration_s"], 1.0)
        # held means held: nothing reached the inbox
        self.assertEqual(sorted(p.name for p in paths.INBOX.iterdir()), [])

    def test_manifest_and_ledger_carry_provenance(self):
        self.stage("meeting_overrun_quiet")
        report = split.run("meeting.ogg", PARAMS)
        doc = json.loads(Path(report["manifest"]).read_text())
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["method"], "waveform-rms")
        self.assertEqual(doc["source"]["sha256"], report["source_sha256"])
        self.assertEqual(doc["source"]["orig_name"], "meeting.ogg")
        self.assertEqual(doc["detection"]["params"]["min_quiet_s"], PARAMS.min_quiet_s)
        self.assertTrue(doc["original_settled"])
        rows = ledger.connect().execute("SELECT * FROM split_segments ORDER BY idx").fetchall()
        self.assertEqual(len(rows), 2)
        for row, seg in zip(rows, doc["segments"]):
            self.assertEqual(row["source_sha256"], report["source_sha256"])
            self.assertEqual(row["sha256"], seg["sha256"])
            self.assertAlmostEqual(row["start_s"], seg["start_s"], delta=0.01)
            self.assertAlmostEqual(row["end_s"], seg["end_s"], delta=0.01)
            self.assertEqual(row["state"], "held")
        item = ledger.connect().execute("SELECT state FROM items WHERE item_id=?",
                                        (report["target"]["item_id"] or doc["source"]["item_id"],)).fetchone()
        self.assertEqual(item["state"], "skipped")

    def test_no_boundary_cuts_nothing_and_exits_3(self):
        for name, reason in (("continuous_activity", "no_contrast"), ("all_quiet", "no_activity"),
                             ("meeting_with_pauses", "no_sustained_quiet"),
                             ("meeting_break_then_more", "quiet_not_trailing")):
            with self.subTest(name):
                audio = self.stage(name, f"{name}.ogg")
                before = (ledger.sha256_file(audio), audio.stat().st_mtime_ns)
                report = split.run(f"{name}.ogg", PARAMS)
                self.assertEqual(report["outcome"], "no_boundary")
                self.assertEqual(report["detection"]["reason"], reason)
                self.assertEqual(split.exit_code(report), split.EXIT_NO_BOUNDARY)
                self.assertEqual(split.EXIT_NO_BOUNDARY, 3)
                self.assertEqual((ledger.sha256_file(audio), audio.stat().st_mtime_ns), before)
                self.assertFalse(split.split_root().exists(), "no hold directory, no partial outputs")
        self.assertIsNone(ledger.connect().execute("SELECT 1 FROM split_segments").fetchone())

    def test_a_second_split_of_the_same_audio_is_idempotent(self):
        audio = self.stage("meeting_overrun_quiet")
        first = split.run("meeting.ogg", PARAMS)
        shutil.copy2(sup.fixture_ogg("meeting_overrun_quiet"), audio)       # the same recording shows up again
        again = split.run("meeting.ogg", PARAMS)
        self.assertEqual(again["outcome"], "already_split")
        self.assertEqual(again["split_id"], first["split_id"])
        self.assertEqual(split.exit_code(again), 0)
        self.assertEqual(len(list(split.split_root().iterdir())), 1, "no second set of parts")

    def test_submit_goes_through_the_inbox_and_is_idempotent(self):
        self.stage("meeting_overrun_quiet")
        report = split.run("meeting.ogg", PARAMS)
        sid = report["split_id"]
        out = split.submit(sid, ("1",))
        self.assertEqual([p["action"] for p in out["parts"]], ["submitted"])
        landed = Path(out["parts"][0]["submitted_as"])
        self.assertEqual(landed.parent, paths.INBOX)
        self.assertEqual(ledger.sha256_file(landed), report["segments"][0]["sha256"])
        # the hidden staging name never lingers where the inbox scan would see it
        self.assertEqual([p.name for p in paths.INBOX.iterdir() if p.name.startswith(".")], [])
        # a held part stays held until asked for
        listed = split.list_splits(sid)[0]["segments"]
        self.assertEqual([s["state"] for s in listed], ["submitted", "held"])
        # the part is a normal ledger item and carries the origin
        row = ledger.connect().execute("SELECT state, origin FROM items WHERE path=?", (str(landed),)).fetchone()
        self.assertEqual((row["state"], row["origin"]), ("pending", "split"))
        again = split.submit(sid, ("1", "2"))
        self.assertEqual([p["action"] for p in again["parts"]], ["already_submitted", "submitted"])
        self.assertEqual(len(list(paths.INBOX.glob("*.ogg"))), 2)
        # the held copies are still there: submit copies, it never moves or deletes
        hold = Path(report["held_dir"])
        self.assertTrue((hold / report["segments"][0]["file"]).is_file())
        self.assertTrue((hold / "meeting.ogg").is_file())

    def test_submit_flag_on_run(self):
        self.stage("meeting_overrun_quiet")
        report = split.run("meeting.ogg", PARAMS, submit_parts=("all",))
        self.assertEqual([p["action"] for p in report["submitted"]["parts"]], ["submitted", "submitted"])

    def test_submit_refuses_a_part_that_changed(self):
        self.stage("meeting_overrun_quiet")
        report = split.run("meeting.ogg", PARAMS)
        part = Path(report["held_dir"]) / report["segments"][0]["file"]
        part.write_bytes(part.read_bytes() + b"x")
        with self.assertRaises(split.SplitError):
            split.submit(report["split_id"], ("1",))
        self.assertEqual(list(paths.INBOX.iterdir()), [])

    def test_unknown_target_and_unknown_part_are_errors(self):
        with self.assertRaises(split.SplitError):
            split.run("nope.ogg", PARAMS)
        self.stage("meeting_overrun_quiet")
        report = split.run("meeting.ogg", PARAMS)
        with self.assertRaises(split.SplitError):
            split.submit(report["split_id"], ("9",))
        with self.assertRaises(split.SplitError):
            split.submit("no-such-split", ("1",))

    def test_a_finished_item_is_split_without_touching_its_transcript(self):
        audio = self.stage("meeting_overrun_quiet")
        item_id = ledger.upsert_item(audio, origin="inbox")
        archived = paths.ARCHIVE / "2026" / "11" / audio.name
        archived.parent.mkdir(parents=True)
        shutil.move(audio, archived)
        conn = ledger.connect()
        conn.execute("UPDATE items SET path=? WHERE item_id=?", (str(archived), item_id))
        ledger.set_item_state(item_id, "complete", cause="test")
        note = paths.VAULT / "note.md"
        note.write_text("existing transcript")
        report = split.run(item_id[:8], PARAMS)
        self.assertEqual(report["outcome"], "split")
        self.assertFalse(report["processing"]["fenced"], "a complete item needs no fence")
        self.assertEqual(report["processing"]["state_after"], "complete")
        self.assertEqual(note.read_text(), "existing transcript")
        self.assertTrue(archived.is_file(), "an archived original stays where it is")
        self.assertIsNone(ledger.connect().execute("SELECT 1 FROM item_fences").fetchone())


class CliTest(unittest.TestCase):
    """The real bin/wax, in an isolated root, for the contract callers depend on: output and exit codes."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "root"
        (cls.root / "inbox").mkdir(parents=True)
        (cls.root / "vault").mkdir()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    FLAGS = ["--min-quiet-s", "30", "--tail-slack-s", "10", "--min-active-s", "20", "--pad-s", "2"]

    def put(self, name: str) -> str:
        shutil.copy2(sup.fixture_ogg(name), self.root / "inbox" / f"{name}.ogg")
        return f"{name}.ogg"

    def test_exit_codes_and_json(self):
        name = self.put("continuous_activity")
        r = sup.cli(self.root, "split", name, *self.FLAGS)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("NO_BOUNDARY", r.stdout)
        self.assertIn("no_contrast", r.stdout)
        r = sup.cli(self.root, "split", "nothing-by-this-name.ogg")
        self.assertEqual(r.returncode, 2)
        self.assertIn("error:", r.stderr)
        name = self.put("meeting_overrun_quiet")
        r = sup.cli(self.root, "split", name, "--dry-run", "--json", *self.FLAGS)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["outcome"], "proposed")
        self.assertTrue(doc["detection"]["found"])
        self.assertTrue((self.root / "inbox" / name).is_file(), "dry run leaves the inbox alone")
        r = sup.cli(self.root, "split", "run", name, *self.FLAGS)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SPLIT", r.stdout)
        self.assertIn("held in", r.stdout)
        sid = json.loads(sup.cli(self.root, "split", "list", "--json").stdout)[0]["split_id"]
        r = sup.cli(self.root, "split", "submit", sid, "all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(list((self.root / "inbox").glob("*part*.ogg"))), 2)
        r = sup.cli(self.root, "split", "submit", sid, "7")
        self.assertEqual(r.returncode, 2)

    def test_no_target_with_nothing_to_split_is_an_error(self):
        r = sup.cli(self.root, "split")
        self.assertEqual(r.returncode, 2)
        self.assertIn("nothing to split", r.stderr)


if __name__ == "__main__":
    unittest.main()
