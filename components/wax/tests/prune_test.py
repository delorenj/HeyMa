"""`wax transcripts prune`: junk-transcript classification and the trash move.

The classifier believes the title and summary, fenced by word counts; the
cases below are real notes from the vault (2026-11-10), including the three the
operator named when asking for the command.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from wax import failures, frontmatter, ledger, operations, passes, paths, prune

COMPONENT = Path(__file__).resolve().parents[1]
REGISTRY = {"title-slug": {"slug": "title-slug", "enabled": True, "version": 3}}


def _note(fm_yaml: str, body: str) -> tuple[dict, str]:
    return frontmatter.split(f"---\n{fm_yaml.strip()}\n---\n{body}")


UNAVAILABLE = _note("""
duration-seconds: 2.81
word-count: 14
title: Unavailable Recipient Response
summary: One speaker says thank you, and the other states the person being contacted
  has a voice.
""", """
# Transcription: telnyx-2026-11-09-159e8298.mp3

**Me (00:00):** Thank you.

**+19083392545 (00:00):** I'm sorry, the person you are trying to reach has a voice.
""")

CANT_HEAR = _note("""
classification: phone_call
classification-source: rule:telnyx
duration-seconds: 26.42
word-count: 8
title: Unable to Hear Other Party
summary: A participant reports being unable to hear the other party.
""", """
# Transcription: telnyx-2026-11-08-912d2f72.mp3

**Speaker 1 (00:01):** Hey, I can't hear you can't hear you
""")

HEADER_ONLY = _note("""
title: Insufficient Transcript Content
summary: The provided transcript contains only a filename header without any actual
  dialogue or content to summarize.
""", """
# Transcription: telnyx-vm-2026-11-09-ef5a6fa2-1794253062.wav
""")


class SpeechWordsTest(unittest.TestCase):
    def test_labels_headings_and_rules_do_not_count(self):
        self.assertEqual(prune.speech_words("# Transcription: a.ogg\n\n---\n\n**Me (00:00):** Hi there.\n"), 2)

    def test_legacy_metadata_header_is_not_speech(self):
        body = ("# Transcription: x.mp3\n\n- **Source**: `/home/x/inbox/x.mp3`\n- **Duration**: 2.8s\n"
                "- **Model**: large-v3 (faster-whisper)\n\n---\n")
        self.assertEqual(prune.speech_words(body), 0)

    def test_urls_and_punctuation_are_not_speech(self):
        self.assertEqual(prune.speech_words("https://example.com/a?b=c\n... -- !!\n"), 0)

    def test_unlabelled_text_counts(self):
        self.assertEqual(prune.speech_words("oh okay can you leave it on the porch"), 9)


class ClassifyTest(unittest.TestCase):
    def reason(self, fm_body, name="note.md", **kw):
        fm, body = fm_body
        return prune.classify(fm, body, name, **kw).reason

    def test_operator_examples(self):
        self.assertEqual(self.reason(UNAVAILABLE), "voicemail")
        self.assertEqual(self.reason(CANT_HEAR), "no-content")
        self.assertEqual(self.reason(HEADER_ONLY), "empty")

    def test_voicemail_by_structure(self):
        self.assertEqual(self.reason(_note("call-outcome: voicemail\ntitle: Smoothie",
                                           "**A (00:00):** " + "word " * 40)), "voicemail")
        self.assertEqual(self.reason(_note("title: Hours", "# Transcription: telnyx-vm-2026.wav\n\n"
                                           "**A (00:00):** " + "word " * 80)), "voicemail")

    def test_voicemail_greeting_title(self):
        fm_body = _note("title: Jarad's Voicemail Greeting", "**Speaker 1 (00:00):** Hi, you've reached Jarad.")
        self.assertEqual(self.reason(fm_body), "voicemail")

    def test_long_note_whose_summary_mentions_voicemail_is_kept(self):
        fm_body = _note("title: Voice-Driven App Workflow\nsummary: Covers closeout and voicemail handling.",
                        "**Speaker 1 (00:00):** " + "word " * 1500)
        self.assertIsNone(self.reason(fm_body))

    def test_no_content_title_only_judges_short_notes(self):
        short = _note("title: Empty Nest Planning", "**Speaker 1 (00:00):** " + "word " * 20)
        long = _note("title: Empty Nest Planning", "**Speaker 1 (00:00):** " + "word " * 200)
        self.assertEqual(self.reason(short), "no-content")
        self.assertIsNone(self.reason(long))

    def test_jev_no_content_on_a_short_note(self):
        fm_body = _note("classification-detail: no_content\ntitle: Pine Nuts",
                        "**Speaker 1 (00:00):** Do you like pine nuts? Boy, boy, boy.")
        self.assertEqual(self.reason(fm_body), "no-content")

    def test_too_short(self):
        self.assertEqual(self.reason(_note("title: Peace", "**Speaker 1 (00:00):** Peace.")), "too-short")
        self.assertIsNone(self.reason(_note("title: Deckard", "Deckard doesn't start on reboot")))
        self.assertEqual(self.reason(_note("title: Deckard", "Deckard doesn't start on reboot"),
                                     min_words=10), "too-short")

    def test_call_noise(self):
        fm_body = _note("classification: phone_call\ntitle: Weird Phone",
                        "**Speaker 1 (00:00):** Hello. Hello. Yeah, your phone's weird.")
        self.assertEqual(self.reason(fm_body), "call-noise")
        dictation = _note("classification: monolog\ntitle: Weird Phone",
                          "**Speaker 1 (00:00):** Hello. Hello. Yeah, your phone's weird.")
        self.assertIsNone(self.reason(dictation))

    def test_body_lost_is_reported_not_default(self):
        fm_body = _note("title: Drum Practice Software Feature Priorities\nword-count: 853",
                        "\nhttps://claude.com/cai/oauth/authorize?code=true\n")
        self.assertEqual(self.reason(fm_body), "body-lost")
        self.assertNotIn("body-lost", prune.DEFAULT_REASONS)

    def test_a_real_short_dictation_is_kept(self):
        fm_body = _note("title: Cost comparison between modal and fal.ai API",
                        "I'd like to compare the estimated costs between the modal and the fal.ai API.")
        self.assertIsNone(self.reason(fm_body))


    def test_long_note_whose_title_mentions_voicemail_is_kept(self):
        fm_body = _note("title: Designing Voicemail Spam Filtering", "**A (00:00):** " + "word " * 3000)
        self.assertIsNone(self.reason(fm_body))

    def test_non_latin_speech_counts(self):
        self.assertEqual(prune.speech_words("**Me (00:00):** Привет, как дела сегодня"), 4)
        self.assertGreater(prune.speech_words("**Me (00:00):** 你好，我想预订明天的会议室"), 0)

    def test_ledger_word_count_reveals_a_clobbered_body(self):
        fm, body = _note("pipeline-status: new", "./BRAINDUMP.md -")
        self.assertEqual(prune.classify(fm, body, "x.md").reason, "too-short")
        self.assertEqual(prune.classify(fm, body, "x.md", recorded_words=3040).reason, "body-lost")
        vm, vbody = _note("title: Jarad Voicemail Greeting", "**A (00:00):** Hi, you've reached Jarad.")
        self.assertEqual(prune.classify(vm, vbody, "v.md", recorded_words=4).reason, "voicemail")

    def test_greeting_opening_a_call_is_a_voicemail(self):
        fm_body = _note("classification: phone_call\ntitle: Personal Loan Pre-Approval Offer",
                        "**Speaker 1 (00:00):** Hi, you've reached Jarad. Leave a message after the tone. "
                        + "word " * 100)
        self.assertEqual(self.reason(fm_body), "voicemail")
        dictation = _note("title: Notes", "**Speaker 1 (00:00):** Hi, you've reached Jarad. "
                          "Leave a message after the tone. " + "word " * 100)
        self.assertIsNone(self.reason(dictation))

    def test_floor_no_content_needs_a_fragment(self):
        floor = "classification-detail: no_content\nclassification-source: rule:floor\ntitle: Pjangler"
        self.assertIsNone(self.reason(_note(floor, "This is great. How can we incorporate this into Pjangler?")))
        self.assertEqual(self.reason(_note(floor, "We're talking to me")), "no-content")

    def test_untitled_test_utterance(self):
        self.assertEqual(self.reason(_note("pipeline-status: new",
                                           "All right. Now let's see if this works. Coconuts.")), "no-content")

    def test_automated_call_menu_title_is_believed_past_the_short_fence(self):
        fm_body = _note("classification: phone_call\ntitle: Automated Wells Fargo Auto Account Identification Menu",
                        "**A (00:00):** " + "press one " * 100)
        self.assertEqual(self.reason(fm_body), "no-content")

    def test_queued_states_mirror_the_worker(self):
        import ast
        tree = ast.parse((COMPONENT / "src/wax/worker.py").read_text())
        value = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                     and any(getattr(t, "id", "") == "WORK_STATES" for t in node.targets))
        self.assertEqual(value, prune.QUEUED_STATES)


JUNK = ("---\nwax-item-id: {item}\ntitle: Jarad's Voicemail Greeting\n---\n\n"
        "# Transcription: telnyx-x.mp3\n\n**Speaker 1 (00:00):** Hi, you've reached Jarad.\n")
REAL = ("---\nwax-item-id: {item}\ntitle: Drum Practice Planning\n---\n\n"
        "**Speaker 1 (00:00):** " + "we should loop the chorus section and slow the tempo " * 10 + "\n")


class _Vault(unittest.TestCase):
    """A tmp Obsidian vault with a Transcripts/ folder, and a tmp ledger."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.vault = self.root / "d"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.folder_path = self.vault / "Transcripts"
        self.folder_path.mkdir()
        self.stack = ExitStack()
        for name, value in {"VAR": self.root / "var", "DB": self.root / "var/wax.db",
                            "VAULT": self.folder_path}.items():
            self.stack.enter_context(patch.object(paths, name, value))
        self.stack.enter_context(patch.object(passes, "registry", side_effect=lambda: dict(REGISTRY)))
        self._close()
        self.conn = ledger.connect()

    def _close(self):
        conn = getattr(ledger._local, "conn", None)
        if conn is not None:
            conn.close()
            del ledger._local.conn

    def tearDown(self):
        self._close()
        self.stack.close()
        self.tmp.cleanup()

    def note(self, name, text, age=7200.0, item=None, state="complete"):
        path = self.folder_path / name
        path.write_text(text.format(item=item or "none"))
        when = time.time() - age
        os.utime(path, (when, when))
        if item:
            now = "2026-11-09T00:00:00.000Z"
            self.conn.execute("INSERT INTO items(item_id,sha256,path,state,first_seen,updated_at) "
                              "VALUES(?,?,?,?,?,?)", (item, item * 4, f"/a/{item}.ogg", state, now, now))
            self.conn.execute("INSERT INTO transcripts(item_id,md_path,created_at) VALUES(?,?,?)",
                              (item, str(path), now))
        return path

    def failed_pass(self, item):
        self.conn.execute(
            "INSERT INTO passes(item_id,ep_slug,version,state,attempt,updated_at,detail,reason_code) "
            "VALUES(?,?,?,?,?,?,?,?)", (item, "title-slug", 3, "failed", 1, "2026-11-09T00:00:00.000Z",
                                        "boom", "provider_bad_response"))

    def trashed(self, name):
        return self.vault / ".trash" / "Transcripts" / name


class PruneSweepTest(_Vault):
    def test_dry_run_moves_nothing(self):
        junk = self.note("vm.md", JUNK, item="aaaa1111")
        out = prune.prune()
        self.assertTrue(out["dry_run"])
        self.assertEqual([n["name"] for n in out["notes"]], ["vm.md"])
        self.assertTrue(junk.is_file())
        self.assertFalse(prune._manifest_path().exists())

    def test_apply_moves_to_vault_trash_and_stamps_the_ledger(self):
        junk = self.note("vm.md", JUNK, item="aaaa1111")
        keep = self.note("real.md", REAL, item="bbbb2222")
        out = prune.prune(apply=True, sync_store=False)
        self.assertEqual(out["counts"]["pruned"], 1)
        self.assertFalse(junk.exists())
        self.assertTrue(self.trashed("vm.md").is_file())
        self.assertTrue(keep.is_file())
        row = self.conn.execute("SELECT pruned_at FROM transcripts WHERE item_id='aaaa1111'").fetchone()
        self.assertIsNotNone(row["pruned_at"])
        entry = prune.read_manifest()[-1]
        self.assertEqual((entry["from"], entry["to"], entry["reason"], entry["run"]),
                         (str(junk), str(self.trashed("vm.md")), "voicemail", out["run"]))

    def test_trash_never_clobbers(self):
        self.trashed("vm.md").parent.mkdir(parents=True)
        self.trashed("vm.md").write_text("older")
        self.note("vm.md", JUNK)
        prune.prune(apply=True, sync_store=False)
        self.assertEqual(self.trashed("vm.md").read_text(), "older")
        self.assertTrue(self.trashed("vm-1.md").is_file())

    def test_young_and_queued_notes_are_held_back(self):
        self.note("young.md", JUNK, age=10)
        self.note("queued.md", JUNK, item="cccc3333", state="transcribed")
        out = prune.prune(apply=True, sync_store=False)
        self.assertEqual(out["counts"].get("young"), 1)
        self.assertEqual(out["counts"].get("in_flight"), 1)
        self.assertTrue((self.folder_path / "young.md").is_file())
        self.assertTrue((self.folder_path / "queued.md").is_file())

    def test_a_note_whose_item_lock_is_held_is_locked(self):
        self.note("vm.md", JUNK, item="aaaa1111")
        import threading
        ready, release = threading.Event(), threading.Event()

        def hold():
            with operations.item_lock("aaaa1111"):
                ready.set()
                release.wait(5)

        t = threading.Thread(target=hold)
        t.start()
        ready.wait(5)
        try:
            out = prune.prune(apply=True, sync_store=False)
        finally:
            release.set()
            t.join()
        self.assertEqual(out["counts"].get("locked"), 1)
        self.assertTrue((self.folder_path / "vm.md").is_file())

    def test_symlinks_and_dotfiles_are_never_touched(self):
        target = self.root / "outside.md"
        target.write_text(JUNK.format(item="x"))
        (self.folder_path / "link.md").symlink_to(target)
        self.note(".hidden.md", JUNK)
        out = prune.prune(apply=True, sync_store=False)
        self.assertTrue(target.is_file() and (self.folder_path / "link.md").is_symlink())
        self.assertTrue((self.folder_path / ".hidden.md").is_file())
        self.assertNotIn("pruned", out["counts"])

    def test_reasons_and_body_lost(self):
        self.note("vm.md", JUNK)
        self.note("lost.md", "---\ntitle: Drum Practice\nword-count: 853\n---\n\nhttps://x.example/a\n")
        out = prune.prune(reasons=[r for r in prune.DEFAULT_REASONS if r != "voicemail"])
        self.assertEqual(out["notes"], [])
        self.assertEqual([h["reason"] for h in out["held_back"]], ["body-lost", "voicemail"])
        out = prune.prune(reasons=["body-lost"])
        self.assertEqual([n["name"] for n in out["notes"]], ["lost.md"])
        with self.assertRaises(prune.PruneError):
            prune.prune(reasons=["nope"])

    def test_suspect_quarantine_is_never_touched(self):
        self.note("x.suspect.md", JUNK)
        out = prune.prune(apply=True, sync_store=False)
        self.assertEqual(out["counts"].get("scanned"), None)
        self.assertTrue((self.folder_path / "x.suspect.md").is_file())

    def test_ledger_word_count_holds_back_a_clobbered_note(self):
        path = self.note("lost.md", "---\nwax-item-id: {item}\n---\n\n./BRAINDUMP.md -\n", item="dddd4444")
        self.conn.execute("UPDATE transcripts SET word_count=3040 WHERE item_id='dddd4444'")
        out = prune.prune(apply=True, sync_store=False)
        self.assertNotIn("pruned", out["counts"])
        self.assertEqual(out["held_back"][0]["reason"], "body-lost")
        self.assertTrue(path.is_file())

    def test_manifest_survives_a_failed_ledger_update(self):
        junk = self.note("vm.md", JUNK, item="aaaa1111")
        with patch.object(prune.ledger, "record_transition", side_effect=RuntimeError("database is locked")):
            out = prune.prune(apply=True, sync_store=False)
        self.assertEqual(out["counts"].get("errors"), 1)
        self.assertIn(str(self.trashed("vm.md")), out["errors"][0])
        prune.restore(run=out["run"])
        self.assertTrue(junk.is_file())

    def test_store_sync_reports_vanished_marks(self):
        self.note("vm.md", JUNK)
        from wax.transcriptions import reconcile as store_reconcile
        with patch.object(store_reconcile, "sweep", return_value={"vanished_marked": 1, "errors": []}):
            out = prune.prune(apply=True)
        self.assertEqual(out["store"], {"ok": True, "vanished_marked": 1, "errors": []})
        with patch.object(store_reconcile, "sweep", side_effect=RuntimeError("store down")):
            self.note("vm2.md", JUNK)
            out = prune.prune(apply=True)
        self.assertFalse(out["store"]["ok"])
        self.assertEqual(out["errors"], [])

    def test_no_obsidian_vault_refuses(self):
        (self.vault / ".obsidian").rmdir()
        with self.assertRaises(prune.PruneError):
            prune.prune()


class PrunedLedgerTest(_Vault):
    def test_pruned_items_leave_every_failure_surface(self):
        self.note("vm.md", JUNK, item="aaaa1111")
        self.failed_pass("aaaa1111")
        self.assertEqual(ledger.passes_failed()["failed"], 1)
        self.assertEqual(len(ledger.failed_passes_for_sweep(3)), 1)
        prune.prune(apply=True, sync_store=False)
        self.assertEqual(ledger.passes_failed()["failed"], 0)
        self.assertEqual(ledger.failed_passes_for_sweep(3), [])
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with patch.object(failures.operator_settings, "disabled_passes", return_value=set()):
            rows = failures._collect_passes(self.conn, tables, cutoff=0, now=time.time(),
                                            since_s=10 ** 9, grace_s=0)
        self.assertEqual(rows, [])

    def test_unprune_puts_the_note_back_and_failures_count_again(self):
        junk = self.note("vm.md", JUNK, item="aaaa1111")
        self.failed_pass("aaaa1111")
        run = prune.prune(apply=True, sync_store=False)["run"]
        out = prune.restore(run=run)
        self.assertEqual([r["path"] for r in out["restored"]], [str(junk)])
        self.assertTrue(junk.is_file())
        self.assertFalse(self.trashed("vm.md").exists())
        row = self.conn.execute("SELECT pruned_at FROM transcripts WHERE item_id='aaaa1111'").fetchone()
        self.assertIsNone(row["pruned_at"])
        self.assertEqual(ledger.passes_failed()["failed"], 1)
        self.assertEqual(prune.restore(names=["vm.md"])["restored"], [])   # newest entry is the restore

    def test_pruned_items_never_dead_letter(self):
        self.note("vm.md", JUNK, item="aaaa1111")
        self.conn.execute(
            "INSERT INTO passes(item_id,ep_slug,version,state,attempt,updated_at,detail,reason_code) "
            "VALUES('aaaa1111','title-slug',3,'failed',3,'2026-11-09T00:00:00.000Z','boom','x')")
        prune.prune(apply=True, sync_store=False)
        with patch.object(passes, "_emit_exhausted", return_value=True) as emit:
            passes.sweep_exhaustions(3)
        emit.assert_not_called()

    def test_unprune_keeps_the_ledger_spelling_of_the_path(self):
        alias = self.root / "alias"
        alias.symlink_to(self.vault)
        junk = self.note("vm.md", JUNK, item="aaaa1111")
        spelled = str(alias / "Transcripts" / "vm.md")
        self.conn.execute("UPDATE transcripts SET md_path=? WHERE item_id='aaaa1111'", (spelled,))
        run = prune.prune(apply=True, sync_store=False)["run"]
        self.assertFalse(junk.exists())
        prune.restore(run=run)
        row = self.conn.execute("SELECT md_path FROM transcripts WHERE item_id='aaaa1111'").fetchone()
        self.assertEqual(row["md_path"], spelled)

    def test_unprune_by_name_and_requires_a_selector(self):
        junk = self.note("vm.md", JUNK)
        prune.prune(apply=True, sync_store=False)
        with self.assertRaises(prune.PruneError):
            prune.restore()
        self.assertEqual(prune.restore(names=["vm.md"], dry_run=True)["restored"][0]["would"], True)
        self.assertFalse(junk.exists())
        prune.restore(names=["vm.md"])
        self.assertTrue(junk.is_file())


class PruneCliTest(_Vault):
    def run_cli(self, *argv):
        env = {**os.environ, "WAX_VAULT": str(self.folder_path), "WAX_ROOT": str(self.root)}
        return subprocess.run([sys.executable, str(COMPONENT / "bin/wax"), *argv],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_cli_dry_run_json(self):
        self._close()
        self.note("vm.md", JUNK)
        res = self.run_cli("--json", "transcripts", "prune")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertTrue(out["dry_run"])
        self.assertEqual([n["name"] for n in out["notes"]], ["vm.md"])
        self.assertTrue((self.folder_path / "vm.md").is_file())


if __name__ == "__main__":
    unittest.main()
