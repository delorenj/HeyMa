"""`wax doctor` "transcript notes intact": a published note that lost its body or its frontmatter.

Two real transcripts had their bodies destroyed outside Wax (a symlink written through, a manual paste) and
nothing noticed for weeks. The rule is prune's body-lost rule (one definition, `prune.body_lost_claim`) plus
"the frontmatter block is gone"; the scan is one read-only pass over the ledger's transcript rows.
"""

import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wax import ledger, paths, prune

COMPONENT = Path(__file__).resolve().parents[1]

HEALTHY = ("---\nwax-item-id: {item}\nword-count: 80\ntitle: Drum Practice Planning\n---\n\n"
           "**Speaker 1 (00:00):** " + "we should loop the chorus section and slow the tempo " * 10 + "\n")
# word-count: 853 in the frontmatter, nothing but a URL left in the body (a real note, 2026-11).
BODY_LOST = ("---\nwax-item-id: {item}\nword-count: 853\ntitle: Drum Practice Software Feature Priorities\n---\n\n"
             "https://claude.com/cai/oauth/authorize?code=true\n")
PASTED_OVER = "./BRAINDUMP.md -\n"          # the manual paste: no frontmatter, no transcript


class DamageTest(unittest.TestCase):
    """prune.damage: pure."""

    def test_a_healthy_note_is_not_damaged(self):
        self.assertIsNone(prune.damage(HEALTHY.format(item="a" * 16), recorded_words=80))

    def test_a_body_far_smaller_than_the_recorded_word_count_is_lost(self):
        found = prune.damage(BODY_LOST.format(item="a" * 16))
        self.assertEqual((found.kinds, found.claimed, found.words), (("body-lost",), 853, 0))
        self.assertIn("0 spoken words but 853 were recorded", found.detail)

    def test_the_ledger_word_count_alone_is_evidence(self):
        text = "---\nwax-item-id: aaaa\n---\n\nhello there\n"
        self.assertIsNone(prune.damage(text))
        found = prune.damage(text, recorded_words=3040)
        self.assertEqual((found.kinds, found.claimed, found.words), (("body-lost",), 3040, 2))

    def test_a_note_without_a_frontmatter_block_lost_its_frontmatter(self):
        found = prune.damage("# Transcription: a.ogg\n\n" + "the whole transcript is still here " * 40,
                             recorded_words=240)
        self.assertEqual(found.kinds, ("frontmatter-lost",))
        self.assertEqual(found.detail, "no frontmatter")

    def test_frontmatter_that_does_not_parse_is_lost_too(self):
        found = prune.damage("---\ntitle: [unclosed\n---\n\nsome words " * 3)
        self.assertEqual(found.kinds, ("frontmatter-lost",))
        self.assertEqual(found.detail, "frontmatter does not parse")

    def test_frontmatter_the_yaml_loader_raises_on_is_unparseable_not_a_crash(self):
        """yaml's timestamp constructor raises ValueError (not YAMLError) on an impossible date, which
        frontmatter.split lets through; a pure, total `damage` treats that block as unreadable frontmatter."""
        for stamp in ("2026-02-30T10:00:00Z", "2026-13-45", "0000-00-00"):
            with self.subTest(stamp):
                found = prune.damage(f"---\ncaptured: {stamp}\nword-count: 80\n---\n\n" + "some words here " * 30)
                self.assertEqual(found.kinds, ("frontmatter-lost",))
                self.assertEqual(found.detail, "frontmatter does not parse")

    def test_a_paste_over_a_note_loses_both(self):
        found = prune.damage(PASTED_OVER, recorded_words=3040)
        self.assertEqual(found.kinds, ("body-lost", "frontmatter-lost"))
        self.assertEqual((found.claimed, found.words), (3040, 1))
        self.assertEqual(found.detail, "body has 1 spoken words but 3040 were recorded; no frontmatter")

    def test_a_short_note_is_not_body_lost(self):
        """The same fence prune uses: a recorded count must exceed SHORT_MAX_WORDS and 4x what is left."""
        text = "---\nwax-item-id: aaaa\n---\n\nthree words here\n"
        self.assertIsNone(prune.damage(text, recorded_words=prune.SHORT_MAX_WORDS))
        self.assertIsNone(prune.damage(text, recorded_words=prune.BODY_LOST_RATIO * 3))
        self.assertIsNotNone(prune.damage(text, recorded_words=prune.SHORT_MAX_WORDS + 1))

    def test_there_is_one_body_lost_rule_shared_with_prune(self):
        """Same thresholds, no copy: damage() says body-lost exactly when prune.classify does."""
        for words in (0, 1, 5, 15, 16, 17, 40, 100):
            for claimed in (0, 10, 60, 61, 64, 65, 240, 241, 3040):
                body = "word " * words
                text = f"---\nword-count: {claimed}\n---\n\n{body}\n"
                lost_by_prune = prune.classify({"word-count": claimed}, body, "x.md").reason == "body-lost"
                found = prune.damage(text)
                self.assertEqual(found is not None and "body-lost" in found.kinds, lost_by_prune,
                                 (words, claimed))
                lost_by_ledger = prune.classify({}, body, "x.md", recorded_words=claimed).reason == "body-lost"
                found = prune.damage("---\ntitle: t\n---\n\n" + body + "\n", recorded_words=claimed)
                self.assertEqual(found is not None and "body-lost" in found.kinds, lost_by_ledger,
                                 (words, claimed))


class _Ledger(unittest.TestCase):
    """A tmp ledger and a tmp vault; nothing here reaches the real ones."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.stack = ExitStack()
        for name, value in {"VAR": self.root / "var", "LOGS": self.root / "var/logs",
                            "DB": self.root / "var/wax.db", "VAULT": self.vault}.items():
            self.stack.enter_context(patch.object(paths, name, value))
        self._close()
        self.conn = ledger.connect()
        self.n = 0

    def _close(self):
        conn = getattr(ledger._local, "conn", None)
        if conn is not None:
            conn.close()
            del ledger._local.conn

    def tearDown(self):
        self._close()
        self.stack.close()
        self.tmp.cleanup()

    def note(self, name, text, *, word_count=None, item=None, pruned=False, asr_path=None, write=True):
        """A note in the vault and its transcripts row. Later calls are newer."""
        self.n += 1
        item = item or f"{self.n:016x}"
        path = self.vault / name
        if write:
            path.write_text(text.format(item=item))
        self.conn.execute(
            "INSERT INTO transcripts(item_id, md_path, word_count, asr_path, created_at, pruned_at) "
            "VALUES(?,?,?,?,?,?)",
            (item, str(path), word_count, asr_path, f"2026-11-09T00:00:{self.n % 60:02d}.{self.n:06d}Z",
             "2026-11-10T00:00:00.000Z" if pruned else None))
        return path, item


class ScanIntegrityTest(_Ledger):
    def test_a_clean_vault_reports_every_note_checked(self):
        self.note("a.md", HEALTHY, word_count=80)
        self.note("b.md", HEALTHY, word_count=80)
        out = prune.scan_integrity()
        self.assertEqual((out["ledger_rows"], out["checked"], out["damaged_total"], out["complete"]),
                         (2, 2, 0, True))

    def test_damaged_notes_are_listed_newest_first_with_restore_sources(self):
        asr = self.root / "asr.json"
        asr.write_text("{}")
        _, old = self.note("old.md", BODY_LOST, word_count=853)
        self.note("fine.md", HEALTHY, word_count=80)
        path, newest = self.note("pasted.md", PASTED_OVER, word_count=3040, asr_path=str(asr))
        out = prune.scan_integrity()
        self.assertEqual(out["damaged_total"], 2)
        first, second = out["damaged"]
        self.assertEqual((first["item_id"], first["name"], first["path"]), (newest, "pasted.md", str(path)))
        self.assertEqual(first["kinds"], ["body-lost", "frontmatter-lost"])
        self.assertEqual((first["claimed"], first["words"]), (3040, 1))
        self.assertEqual(first["restore"], {
            "store_history": f"wax transcripts history {newest}",
            "store_show": f"wax transcripts show {newest} --version N --raw",
            "asr_json": str(asr), "asr_json_exists": True})
        self.assertEqual((second["item_id"], second["claimed"], second["words"]), (old, 853, 0))
        # No asr_path on the row: the conventional var/logs/<item>/asr.json, which may not exist.
        self.assertEqual(second["restore"]["asr_json"], str(paths.LOGS / old / "asr.json"))
        self.assertFalse(second["restore"]["asr_json_exists"])

    def test_one_note_the_yaml_loader_chokes_on_does_not_hide_the_rest(self):
        """A note with an impossible date once raised out of the whole scan (probe: one WARN naming nothing)."""
        _, lost = self.note("lost.md", BODY_LOST, word_count=853)
        _, odd = self.note("odd.md", "---\nwax-item-id: {item}\ncaptured: 2026-02-30\n---\n\n"
                           + "we should loop the chorus section " * 20, word_count=100)
        self.note("fine.md", HEALTHY, word_count=80)
        out = prune.scan_integrity()
        self.assertEqual((out["checked"], out["damaged_total"], out["complete"]), (3, 2, True))
        self.assertEqual({n["item_id"]: n["kinds"] for n in out["damaged"]},
                         {lost: ["body-lost"], odd: ["frontmatter-lost"]})

    def test_the_list_is_capped_but_the_total_is_not(self):
        for i in range(5):
            self.note(f"lost{i}.md", BODY_LOST, word_count=853)
        out = prune.scan_integrity(limit=2)
        self.assertEqual((out["damaged_total"], len(out["damaged"])), (5, 2))

    def test_pruned_missing_unreadable_and_oversized_notes_are_not_findings(self):
        self.note("pruned.md", BODY_LOST, word_count=853, pruned=True)      # in the trash on purpose
        self.note("gone.md", "", write=False)
        (self.vault / "binary.md").write_bytes(b"\xff\xfe not utf-8")
        self.note("binary.md", "", write=False)
        (self.vault / "big.md").write_bytes(b"x" * (prune.MAX_NOTE_BYTES + 1))
        self.note("big.md", "", write=False)
        out = prune.scan_integrity()
        self.assertEqual((out["ledger_rows"], out["checked"], out["damaged_total"]), (3, 0, 0))
        self.assertEqual((out["missing"], out["unreadable"], out["too_large"]), (1, 1, 1))
        self.assertEqual([n["name"] for n in out["unreadable_notes"]], ["binary.md"])   # counted, and named

    def test_a_fifo_is_not_a_note_and_never_blocks(self):
        os.mkfifo(self.vault / "fifo.md")
        self.note("fifo.md", "", write=False)
        result = {}
        worker = threading.Thread(target=lambda: result.update(prune.scan_integrity()), daemon=True)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive(), "a FIFO at md_path hung the scan")
        self.assertEqual((result["checked"], result["unreadable"]), (0, 1))

    def test_the_time_budget_makes_a_partial_scan_say_so(self):
        for i in range(4):
            self.note(f"n{i}.md", HEALTHY, word_count=80)
        # deadline = 0 + 5: the first two rows are inside it, the third is past it
        clock = iter([0.0, 1.0, 2.0, 99.0])
        with patch.object(prune, "time", SimpleNamespace(monotonic=lambda: next(clock))):
            out = prune.scan_integrity(budget_s=5.0)
        self.assertFalse(out["complete"])
        self.assertEqual((out["ledger_rows"], out["checked"]), (4, 2))

    def test_the_scan_never_writes(self):
        path, _ = self.note("lost.md", BODY_LOST, word_count=853)
        before_bytes, before_mtime = path.read_bytes(), path.stat().st_mtime_ns
        changes = self.conn.total_changes
        before_rows = [tuple(r) for r in self.conn.execute("SELECT * FROM transcripts ORDER BY item_id")]
        prune.scan_integrity()
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(path.stat().st_mtime_ns, before_mtime)
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT * FROM transcripts ORDER BY item_id")],
                         before_rows)
        self.assertEqual(sorted(p.name for p in self.vault.iterdir()), ["lost.md"])

    def test_a_big_vault_is_one_cheap_pass(self):
        import time
        for i in range(1000):
            self.note(f"n{i:04d}.md", HEALTHY, word_count=80)
        started = time.monotonic()
        out = prune.scan_integrity()
        self.assertEqual((out["checked"], out["damaged_total"], out["complete"]), (1000, 0, True))
        self.assertLess(time.monotonic() - started, prune.INTEGRITY_BUDGET_S)


def _load_wax_cli():
    loader = SourceFileLoader("wax_cli_note_integrity", str(COMPONENT / "bin" / "wax"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class DoctorProbeTest(_Ledger):
    """bin/wax: the "transcript notes intact" probe, its --json row and its text rendering."""

    @classmethod
    def setUpClass(cls):
        cls.wax = _load_wax_cli()

    def probe(self):
        rows = []
        self.wax.probe_note_integrity(rows.append)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["probe"], "transcript notes intact")
        json.dumps(rows[0])           # `wax doctor --json` dumps the row as it is
        return rows[0]

    def test_clean_vault_passes(self):
        self.note("a.md", HEALTHY, word_count=80)
        row = self.probe()
        self.assertEqual(row["status"], self.wax.PASS)
        self.assertIn("1 published note(s) read", row["detail"])

    def test_damage_is_a_warning_not_a_failure_with_the_restore_hint(self):
        self.note("fine.md", HEALTHY, word_count=80)
        _, item = self.note("lost.md", BODY_LOST, word_count=853)
        row = self.probe()
        self.assertEqual(row["status"], self.wax.WARN)
        self.assertIn("1 of 2 published note(s) lost their body or frontmatter outside Wax", row["detail"])
        self.assertIn(f"wax transcripts history {item}", row["fix"])
        self.assertIn(f"wax transcripts show {item} --version N --raw", row["fix"])
        self.assertIn(f"{item}/asr.json", row["fix"])
        self.assertEqual(row["lines"], [f"lost.md  item {item}  body-lost: 0 word(s) present, 853 recorded"])
        self.assertEqual([n["item_id"] for n in row["notes"]], [item])

    def test_a_note_with_no_word_count_on_record_says_none(self):
        self.note("pasted.md", PASTED_OVER)
        row = self.probe()
        self.assertTrue(row["lines"][0].endswith("frontmatter-lost: 1 word(s) present, none recorded"),
                        row["lines"])

    def test_only_the_newest_offenders_are_listed(self):
        total = prune.INTEGRITY_LIST_MAX + 3
        for i in range(total):
            self.note(f"lost{i}.md", BODY_LOST, word_count=853)
        row = self.probe()
        self.assertEqual(len(row["notes"]), prune.INTEGRITY_LIST_MAX)
        self.assertIn(f"{total} of {total}", row["detail"])
        self.assertIn(f"newest {prune.INTEGRITY_LIST_MAX} listed", row["detail"])

    def test_a_partial_scan_is_never_green(self):
        with patch.object(prune, "scan_integrity", return_value={
                "ledger_rows": 900, "checked": 400, "missing": 0, "unreadable": 0, "too_large": 0,
                "damaged_total": 0, "damaged": [], "complete": False}):
            row = self.probe()
        self.assertEqual(row["status"], self.wax.WARN)
        self.assertIn("after 400 of 900", row["detail"])

    def test_rows_whose_notes_are_all_missing_do_not_read_as_healthy(self):
        self.note("a.md", "", write=False)
        self.note("b.md", "", write=False)
        row = self.probe()
        self.assertEqual(row["status"], self.wax.WARN)
        self.assertIn("none of the 2 ledger transcript(s) has a readable note", row["detail"])
        self.assertIn(str(self.vault), row["fix"])

    def test_a_note_that_cannot_be_read_is_a_warning_not_a_pass(self):
        """Not judged is not healthy: a binary note beside good ones must not let the row go green."""
        self.note("fine.md", HEALTHY, word_count=80)
        (self.vault / "binary.md").write_bytes(b"\xff\xfe not utf-8")
        self.note("binary.md", "", write=False)
        row = self.probe()
        self.assertEqual(row["status"], self.wax.WARN)
        self.assertIn("1 published note(s) could not be read", row["detail"])
        self.assertIn("binary.md", row["detail"])
        self.assertIn("1 others read", row["detail"])
        self.assertIn("wax transcripts history", row["fix"])

    def test_an_empty_ledger_passes_and_says_why(self):
        row = self.probe()
        self.assertEqual(row["status"], self.wax.PASS)
        self.assertEqual(row["detail"], "no published transcripts in the ledger yet")

    def test_a_broken_scan_is_reported_not_raised(self):
        with patch.object(prune, "scan_integrity", side_effect=RuntimeError("ledger locked")):
            row = self.probe()
        self.assertEqual((row["status"], row["detail"]), (self.wax.WARN, "RuntimeError: ledger locked"))

    def test_probe_all_runs_it(self):
        import inspect
        self.assertIn("probe_note_integrity(add)", inspect.getsource(self.wax.probe_all))

    def _doctor(self, rows, as_json):
        out = io.StringIO()
        config_row = {"name": "WAX_ROOT", "value": "/x", "source": "default", "consumer": "paths", "note": ""}
        with patch.object(self.wax, "probe_all", return_value=rows), \
                patch.object(self.wax, "waxd_pid", return_value=None), \
                patch.object(self.wax, "proc_env", return_value={}), \
                patch.object(self.wax, "unit_env", return_value={}), \
                patch.object(self.wax, "yaml_env", return_value={}), \
                patch.object(self.wax, "config_rows", return_value=[config_row]), \
                redirect_stdout(out):
            code = self.wax.doctor(as_json, False)
        return code, out.getvalue()

    def test_doctor_exits_1_on_the_warning_and_names_each_note_in_text_and_json(self):
        item = "1" * 16
        self.note("lost.md", BODY_LOST, word_count=853, item=item)
        rows = [self.probe()]
        code, text = self._doctor(rows, as_json=False)
        self.assertEqual(code, 1)
        self.assertIn("WARN  transcript notes intact", text)
        self.assertIn(f"- lost.md  item {item}  body-lost: 0 word(s) present, 853 recorded", text)
        self.assertIn(f"fix: restore from the store: wax transcripts history {item}", text)
        code, blob = self._doctor(rows, as_json=True)
        probe = json.loads(blob)["probes"][0]
        self.assertEqual((code, probe["status"]), (1, "warn"))
        self.assertEqual(probe["notes"][0]["restore"]["store_history"], f"wax transcripts history {item}")


if __name__ == "__main__":
    unittest.main()
