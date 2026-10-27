"""Document hygiene: the jobs move the user's notes, so every rule is a test.

Covers the emptiness predicate, the trash move (path, no-clobber, recoverable),
every refusal (young, dot-dirs, symlinks, escapes, unparseable frontmatter,
in-flight lock, vault requirements, re-check under the lock, post-move
verification), the nightly gate, the untitled flow (trash / summarize / rename
/ ledger re-point), vanished-document bookkeeping and its effect on
`passes_failed`, the worker cadence, the doctor probe and the CLI.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from wax import documents, frontmatter, hygiene, ledger, paths, pipelines, rename, state, worker

COMPONENT = Path(__file__).resolve().parents[1]
# Captured before any test patches it: the worker's guard against a scratch ledger.
_REAL_LEDGER_IS_LIVE = hygiene.runtime_ledger_is_live


def _close_test_connection() -> None:
    connection = getattr(ledger._local, "conn", None)
    if connection is not None:
        connection.close()
        del ledger._local.conn


class _Env(unittest.TestCase):
    """A tmp vault (with .obsidian/) holding Inbox/ and Sandbox/, and a tmp ledger."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.vault = self.root / "vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.inbox = self.vault / "Inbox"
        self.sandbox = self.vault / "Sandbox"
        self.inbox.mkdir()
        self.sandbox.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        runtime = self.root / "runtime"
        runtime.mkdir()
        self.folders = f"{self.inbox}{os.pathsep}{self.sandbox}"
        self.stack = [
            patch.object(paths, "DB", runtime / "wax.db"),
            patch.object(paths, "VAR", runtime),
            patch.dict(os.environ, {"WAX_DOC_HYGIENE_FOLDERS": self.folders}),
        ]
        for p in self.stack:
            p.start()
        _close_test_connection()
        self.folder = hygiene.resolve_folders().folders[0]

    def tearDown(self):
        for p in reversed(self.stack):
            p.stop()
        _close_test_connection()
        self.tmp.cleanup()

    def note(self, where: Path, name: str, text: str = "", age: float = 3600.0) -> Path:
        path = where / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        when = time.time() - age
        os.utime(path, (when, when))
        return path

    def trash(self, rel: str) -> Path:
        return self.vault / ".trash" / rel


# ------------------------------------------------------------ predicate ----

class EmptinessTest(unittest.TestCase):
    CASES = [
        ("", "empty"),
        ("   \n\t\n  ", "empty"),
        ("﻿", "empty"),
        ("﻿\n", "empty"),
        ("---\n---\n", "empty"),
        ("---\n---", "empty"),
        ("---\ntags: []\n---\n", "empty"),
        ("---\ncreated: 2026-01-01\nmodified: 2026-01-02\ntags:\n  - a\ncssclasses: []\n"
         "aliases: [x]\nid: abc\nwax:\n  passes: {a: 1}\nwax-item-id: i\n---\n\n\n", "empty"),
        ("---\ntitle:\n---\n", "empty"),
        ("---\ntitle: ''\n---\n  \n", "empty"),
        ("---\nauthor: [ ]\nmeta: {a: , b: ' '}\n---\n", "empty"),
        ("---\r\ntags: []\r\n---\r\n\r\n", "empty"),
        ("Hello", "content"),
        ("# ", "content"),
        ("​", "content"),
        ("\x00", "content"),
        ("---\ntitle: Foo\n---\n", "content"),
        ("---\ntags: [x]\n---\nBody\n", "content"),
        ("---\nrating: 0\n---\n", "content"),
        ("---\ndone: false\n---\n", "content"),
        ("---\nauthor: [x]\n---\n", "content"),
        ("---\nnot closed\n", "content"),
        ("---garbage\nfoo\n---\n", "content"),
        ("---\n", "content"),
        ("---\ntitle: [unclosed\n---\n", "unparseable"),
        ("---\n- a\n- b\n---\n", "unparseable"),
        ("---\njust a string\n---\n", "unparseable"),
        ("---\ntitle: Foo\n  bad: [\n---\n", "unparseable"),
    ]

    def test_predicate_table(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(hygiene.classify_text(text), expected)

    def test_untitled_regex(self):
        for stem in ("Untitled", "untitled", "UNTITLED 3", "Untitled-12", "untitled_4", "Untitled7"):
            with self.subTest(stem=stem):
                self.assertTrue(hygiene.UNTITLED_RE.match(stem))
        for stem in ("Untitled note", "Untitled 3 copy", "My Untitled", "Untitled-", "Untitled 1 2",
                     "Untitled.1", "", "untitle"):
            with self.subTest(stem=stem):
                self.assertFalse(hygiene.UNTITLED_RE.match(stem))

    def test_safe_title_name(self):
        self.assertEqual(hygiene.safe_title_name("my-great-note"), "my-great-note.md")
        self.assertEqual(hygiene.safe_title_name("Café notes 2"), "Café notes 2.md")
        self.assertEqual(hygiene.safe_title_name("already.md"), "already.md")
        for bad in ("../evil", "a/b", ".hidden", "", "  ", None, 3, "x" * 200, "untitled",
                    "Untitled 2", "trailing.", "a\x00b", "a\nb"):
            with self.subTest(slug=bad):
                self.assertIsNone(hygiene.safe_title_name(bad))


# ---------------------------------------------------------------- ledger ---

class LedgerMetaTest(_Env):
    def test_meta_roundtrip_and_upsert(self):
        self.assertIsNone(ledger.meta_get("k"))
        self.assertEqual(ledger.meta_get("k", "d"), "d")
        ledger.meta_set("k", "1")
        ledger.meta_set("k", "2")
        self.assertEqual(ledger.meta_get("k"), "2")


# ------------------------------------------------------------- folders -----

class FolderResolutionTest(_Env):
    def test_folder_without_vault_is_an_error_and_never_swept(self):
        bare = self.outside / "Inbox"
        bare.mkdir()
        empty = self.note(bare, "Untitled.md")
        out = hygiene.sweep_empty(folders_spec=str(bare))
        self.assertIn("no Obsidian vault", out["last_error"])
        self.assertTrue(empty.exists(), "a folder outside any vault is never swept")

    def test_vault_root_itself_is_refused(self):
        note = self.note(self.vault, "Untitled.md")
        out = hygiene.sweep_empty(folders_spec=str(self.vault))
        self.assertIn("vault root itself", out["last_error"])
        self.assertTrue(note.exists())

    def test_dot_directory_folder_is_refused(self):
        hidden = self.vault / ".trash" / "Inbox"
        note = self.note(hidden, "Untitled.md")
        out = hygiene.sweep_empty(folders_spec=str(hidden))
        self.assertIn("dot-directory", out["last_error"])
        self.assertTrue(note.exists())

    def test_missing_folder_is_reported_not_an_error(self):
        out = hygiene.sweep_empty(folders_spec=str(self.vault / "Ghost"))
        self.assertIsNone(out["last_error"])
        self.assertEqual(out["missing_folders"], [str(self.vault / "Ghost")])

    def test_symlinked_folder_resolves_to_its_real_vault(self):
        link = self.root / "d"
        link.symlink_to(self.vault)
        resolved = hygiene.resolve_folders(str(link / "Inbox"))
        self.assertEqual(resolved.errors, [])
        self.assertEqual(resolved.folders[0].root, self.inbox)
        self.assertEqual(resolved.folders[0].vault, self.vault)

    def test_default_folders_are_inbox_and_sandbox_of_d(self):
        with patch.dict(os.environ, clear=False):
            os.environ.pop("WAX_DOC_HYGIENE_FOLDERS")
            self.assertEqual(hygiene.DEFAULT_FOLDERS, "~/d/Inbox:~/d/Sandbox")
            with patch.object(os.path, "expanduser", side_effect=lambda p: p.replace("~", str(self.root))):
                (self.root / ".obsidian").mkdir()
                (self.root / "d").symlink_to(self.vault)
                resolved = hygiene.resolve_folders()
        self.assertEqual({f.root for f in resolved.folders}, {self.inbox, self.sandbox})


# -------------------------------------------------------- empty sweep ------

class TrashTest(_Env):
    def test_empty_note_moves_to_vault_trash_and_is_recoverable(self):
        note = self.note(self.inbox, "Untitled.md")
        out = hygiene.sweep_empty()
        self.assertEqual(out["counts"]["trashed"], 1)
        self.assertFalse(note.exists())
        trashed = self.trash("Inbox/Untitled.md")
        self.assertTrue(trashed.is_file(), "the vault's own .trash, mirroring the vault path")
        os.rename(trashed, note)  # recovery is a plain move back
        self.assertTrue(note.exists())

    def test_nested_note_keeps_its_relative_path_in_the_trash(self):
        self.note(self.inbox, "Wax/2026/Untitled 1.md")
        hygiene.sweep_empty()
        self.assertTrue(self.trash("Inbox/Wax/2026/Untitled 1.md").is_file())

    def test_sandbox_is_swept_too(self):
        self.note(self.sandbox, "Untitled.md")
        hygiene.sweep_empty()
        self.assertTrue(self.trash("Sandbox/Untitled.md").is_file())

    def test_trash_never_clobbers(self):
        self.trash("Inbox").mkdir(parents=True)
        precious = self.trash("Inbox/Untitled.md")
        precious.write_text("old trash content")
        self.note(self.inbox, "Untitled.md")
        hygiene.sweep_empty()
        self.assertEqual(precious.read_text(), "old trash content")
        self.assertTrue(self.trash("Inbox/Untitled-1.md").is_file())
        self.note(self.inbox, "Untitled.md")
        hygiene.sweep_empty()
        self.assertTrue(self.trash("Inbox/Untitled-2.md").is_file())
        self.assertEqual(precious.read_text(), "old trash content")

    def test_young_note_is_untouched(self):
        note = self.note(self.inbox, "Untitled.md", age=10)
        out = hygiene.sweep_empty()
        self.assertTrue(note.exists())
        self.assertEqual(out["counts"].get("young"), 1)
        self.assertNotIn("trashed", out["counts"])

    def test_age_boundary_uses_the_knob(self):
        note = self.note(self.inbox, "Untitled.md", age=120)
        hygiene.sweep_empty()
        self.assertTrue(note.exists(), "120s is inside the default 300s gate")
        with patch.dict(os.environ, {"WAX_DOC_HYGIENE_MIN_AGE_S": "60"}):
            hygiene.sweep_empty()
        self.assertFalse(note.exists())

    def test_future_mtime_is_not_old(self):
        note = self.note(self.inbox, "Untitled.md", age=-3600)
        hygiene.sweep_empty()
        self.assertTrue(note.exists())

    def test_notes_with_content_are_untouched(self):
        keep = [
            self.note(self.inbox, "a.md", "text"),
            self.note(self.inbox, "b.md", "---\ntitle: Real\n---\n"),
            self.note(self.inbox, "c.md", "---\ntags: [x]\n---\nbody"),
            self.note(self.inbox, "d.md", "---\ntitle: [unclosed\n---\n"),
            self.note(self.inbox, "e.md", "---\n- a\n---\n"),
        ]
        undecodable = self.inbox / "f.md"
        undecodable.write_bytes(b"\xff\xfe\x00 \x80")
        old = time.time() - 3600
        os.utime(undecodable, (old, old))
        keep.append(undecodable)
        big = self.inbox / "g.md"
        big.write_text(" " * (hygiene.MAX_EMPTY_BYTES + 10))
        os.utime(big, (old, old))
        keep.append(big)
        out = hygiene.sweep_empty()
        for path in keep:
            self.assertTrue(path.exists(), path.name)
        self.assertNotIn("trashed", out["counts"])
        self.assertEqual(out["counts"].get("unparseable"), 2)
        self.assertEqual(out["counts"].get("unreadable"), 1)

    def test_auto_generated_frontmatter_only_is_empty(self):
        note = self.note(self.inbox, "Untitled.md", "---\ncreated: 2026-10-01\ntags: []\n---\n\n")
        hygiene.sweep_empty()
        self.assertFalse(note.exists())

    def test_only_markdown_dotfiles_and_dotdirs(self):
        keep = [
            self.note(self.inbox, "empty.txt"),
            self.note(self.inbox, "empty.markdown"),
            self.note(self.inbox, ".hidden.md"),
            self.note(self.inbox, ".wax-ep-abc.md"),
            self.note(self.inbox, ".obsidian/Untitled.md"),
            self.note(self.inbox, ".trash/Untitled.md"),
            self.note(self.inbox, "sub/.git/Untitled.md"),
        ]
        hygiene.sweep_empty()
        for path in keep:
            self.assertTrue(path.exists(), str(path))

    def test_symlinks_are_never_followed_or_moved(self):
        target = self.note(self.outside, "elsewhere.md")
        link = self.inbox / "link.md"
        link.symlink_to(target)
        outside_dir = self.outside / "dir"
        victim = self.note(outside_dir, "Untitled.md")
        (self.inbox / "linkdir").symlink_to(outside_dir)
        hygiene.sweep_empty()
        self.assertTrue(link.is_symlink() and target.exists())
        self.assertTrue(victim.exists(), "a symlinked directory is not descended into")
        self.assertFalse((self.vault / ".trash").exists())

    def test_choke_point_refuses_unsafe_paths_even_when_called_directly(self):
        target = self.note(self.outside, "elsewhere.md")
        link = self.inbox / "link.md"
        link.symlink_to(target)
        outside = self.note(self.outside, "Untitled.md")
        in_trash = self.note(self.vault / ".trash", "Untitled.md")
        dot = self.note(self.inbox, ".hidden.md")
        in_dotdir = self.note(self.inbox, ".obsidian/x.md")
        txt = self.note(self.inbox, "x.txt")
        escape_dir = self.outside / "esc"
        escape_dir.mkdir()
        self.note(escape_dir, "Untitled.md")
        (self.inbox / "viaDir").symlink_to(escape_dir)
        via_dir = self.inbox / "viaDir" / "Untitled.md"
        for path, verdict in ((link, "symlink"), (outside, "escape"), (in_trash, "escape"),
                              (dot, "excluded"), (in_dotdir, "excluded"), (txt, "excluded"),
                              (via_dir, "escape"), (self.inbox / "gone.md", "vanished")):
            with self.subTest(path=str(path)):
                result = hygiene.trash_note(path, self.folder)
                self.assertEqual(result["outcome"], verdict)
        for path in (target, outside, in_trash, dot, in_dotdir, txt):
            self.assertTrue(path.exists())

    def test_in_flight_run_lock_skips_the_note(self):
        note = self.note(self.inbox, "Untitled.md")
        row, _ = documents.register(note, binding_id="b1")
        lock = documents.document_run_lock("b1", row["doc_id"])
        self.assertTrue(lock.acquire())
        try:
            out = hygiene.sweep_empty()
        finally:
            lock.release()
        self.assertTrue(note.exists())
        self.assertEqual(out["counts"].get("locked"), 1)
        self.assertEqual(documents.get(documents.doc_key("b1", row["doc_id"]))["state"], "pending")
        hygiene.sweep_empty()
        self.assertFalse(note.exists())
        self.assertEqual(documents.get(documents.doc_key("b1", row["doc_id"]))["state"], "trashed")

    def test_every_row_at_the_path_is_locked_and_marked(self):
        note = self.note(self.inbox, "Untitled.md")
        a, _ = documents.register(note, binding_id="")
        documents.register(note, binding_id="b2")
        held = documents.document_run_lock("b2", a["doc_id"])
        self.assertTrue(held.acquire())
        try:
            hygiene.sweep_empty()
            self.assertTrue(note.exists(), "any one live run is enough to skip")
        finally:
            held.release()
        hygiene.sweep_empty()
        states = {r["binding_id"]: r["state"] for r in documents.list_documents()}
        self.assertEqual(states, {"": "trashed", "b2": "trashed"})

    def test_hold_run_locks_is_all_or_nothing(self):
        note = self.note(self.inbox, "x.md", "x")
        r1, _ = documents.register(note, binding_id="b1")
        r2, _ = documents.register(note, binding_id="b2")
        blocker = documents.document_run_lock("b2", r2["doc_id"])
        self.assertTrue(blocker.acquire())
        try:
            with documents.hold_run_locks([r1, r2]) as held:
                self.assertFalse(held)
            self.assertFalse(documents.run_lock_held("b1", r1["doc_id"]),
                             "a partial acquisition must be released")
        finally:
            blocker.release()
        with documents.hold_run_locks([r1, r2]) as held:
            self.assertTrue(held)
            self.assertTrue(documents.run_lock_held("b1", r1["doc_id"]))
        self.assertFalse(documents.run_lock_held("b1", r1["doc_id"]))

    def test_recheck_under_the_lock_catches_a_keystroke(self):
        note = self.note(self.inbox, "Untitled.md")
        real = documents.hold_run_locks

        def typed_meanwhile(rows):
            note.write_text("I just started typing")
            return real(rows)

        with patch.object(documents, "hold_run_locks", side_effect=typed_meanwhile):
            result = hygiene.trash_note(note, self.folder)
        self.assertEqual(result["outcome"], "changed")
        self.assertEqual(note.read_text(), "I just started typing")
        self.assertFalse((self.vault / ".trash").exists())

    def test_recheck_catches_a_touch_that_makes_it_young(self):
        note = self.note(self.inbox, "Untitled.md")
        real = documents.hold_run_locks

        def touched_meanwhile(rows):
            os.utime(note, None)
            return real(rows)

        with patch.object(documents, "hold_run_locks", side_effect=touched_meanwhile):
            result = hygiene.trash_note(note, self.folder)
        self.assertEqual(result["outcome"], "changed")
        self.assertTrue(note.exists())

    def test_a_write_that_races_the_move_puts_the_note_back(self):
        note = self.note(self.inbox, "Untitled.md")
        real_move = rename.move_noclobber
        calls = []

        def move_then_write(src, dst, *a, **kw):
            used = real_move(src, dst, *a, **kw)
            if not calls:
                calls.append(used)
                Path(used).write_text("typed during the move")
            return used

        with patch.object(rename, "move_noclobber", side_effect=move_then_write):
            result = hygiene.trash_note(note, self.folder)
        self.assertEqual(result["outcome"], "restored")
        self.assertEqual(note.read_text(), "typed during the move")
        self.assertFalse(self.trash("Inbox/Untitled.md").exists())

    def test_trash_that_is_a_symlink_is_refused(self):
        (self.vault / ".trash").symlink_to(self.outside)
        note = self.note(self.inbox, "Untitled.md")
        out = hygiene.sweep_empty()
        self.assertTrue(note.exists())
        self.assertIn("symlink", out["last_error"])
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_dry_run_changes_nothing(self):
        note = self.note(self.inbox, "Untitled.md")
        row, _ = documents.register(note, binding_id="b1")
        ghost = self.note(self.inbox, "ghost.md", "x")
        grow, _ = documents.register(ghost, binding_id="b1")
        ghost.unlink()
        hygiene.sweep_empty(min_age=1e9)  # first sighting of the ghost: grace, not gone
        out = hygiene.sweep_empty(dry_run=True, record=True)
        self.assertTrue(note.exists())
        self.assertFalse((self.vault / ".trash").exists())
        self.assertEqual(out["counts"]["would_trash"], 1)
        self.assertEqual(out["counts"]["gone_marked"], 1, "reported, not applied")
        self.assertEqual(documents.get(documents.doc_key("b1", row["doc_id"]))["state"], "pending")
        self.assertEqual(documents.get(documents.doc_key("b1", grow["doc_id"]))["state"], "pending")
        self.assertIsNone(ledger.meta_get(hygiene.META_EMPTY), "a dry run records nothing")

    def test_run_summary_is_recorded_for_the_worker_form_only(self):
        self.note(self.inbox, "Untitled.md")
        hygiene.sweep_empty()
        self.assertIsNone(hygiene.load_summary("empty"), "the CLI form must not look like the worker")
        out = hygiene.sweep_empty(record=True)
        stored = hygiene.load_summary("empty")
        self.assertEqual(stored["counts"], out["counts"])
        self.assertIsNone(stored["last_error"])
        self.assertEqual(stored["interval_s"], hygiene.EMPTY_SWEEP_INTERVAL_S)
        self.assertTrue(stored["last_run_at"].endswith("Z"))

    def test_one_bad_note_does_not_stop_the_sweep(self):
        a = self.note(self.inbox, "Untitled.md")
        b = self.note(self.inbox, "Untitled 1.md")
        real = hygiene.trash_note
        seen = []

        def flaky(path, *args, **kw):
            seen.append(path.name)
            if path.name == "Untitled 1.md":
                raise OSError("boom")
            return real(path, *args, **kw)

        with patch.object(hygiene, "trash_note", side_effect=flaky):
            out = hygiene.sweep_empty(record=True)
        self.assertFalse(a.exists())
        self.assertTrue(b.exists())
        self.assertEqual(out["counts"]["errors"], 1)
        self.assertIn("boom", out["last_error"])
        self.assertIn("boom", hygiene.load_summary("empty")["last_error"])


# --------------------------------------------- vanished + passes_failed -----

class VanishedDocumentsTest(_Env):
    def _failed_pass(self, key: str, slug: str = "alpha") -> None:
        ledger.connect().execute(
            "INSERT OR REPLACE INTO passes(item_id,ep_slug,state,attempt,version,reason_code,updated_at) "
            "VALUES(?,?,'failed',3,1,'empty_document','2026-10-26T00:14:26.796Z')", (key, slug))

    def test_vanished_file_is_marked_gone_and_its_failure_stops_counting(self):
        note = self.note(self.inbox, "Untitled.md")
        row, _ = documents.register(note, binding_id="b1")
        bare, _ = documents.register(self.note(self.inbox, "x.md", "x"), binding_id="")
        keep = self.note(self.inbox, "keep.md", "keep")
        krow, _ = documents.register(keep, binding_id="b1")
        key = documents.doc_key("b1", row["doc_id"])
        bare_key = documents.doc_key("", bare["doc_id"])
        kkey = documents.doc_key("b1", krow["doc_id"])
        for k in (key, bare_key, kkey, "audio-item"):
            self._failed_pass(k)
        with patch.object(ledger, "_enabled_pass_versions", return_value={"alpha": 1}):
            self.assertEqual(ledger.passes_failed()["failed"], 4)
            note.unlink()
            (self.inbox / "x.md").unlink()
            first = hygiene.sweep_empty()
            self.assertEqual(first["counts"]["gone_marked"], 0, "one sweep of grace")
            self.assertEqual(first["counts"]["gone_pending"], 2)
            self.assertEqual(ledger.passes_failed()["failed"], 4)
            out = hygiene.sweep_empty()
            self.assertEqual(out["counts"]["gone_marked"], 2)
            self.assertEqual(documents.get(key)["state"], "gone")
            self.assertEqual(documents.get(bare_key)["state"], "gone")
            self.assertNotEqual(documents.get(kkey)["state"], "gone")
            self.assertEqual(ledger.passes_failed(), {"failed": 2, "slugs": ["alpha"]},
                             "live document and audio failures still count")
            again = hygiene.sweep_empty()
            self.assertEqual(again["counts"]["gone_marked"], 0, "idempotent")

    def test_trashed_documents_also_stop_counting(self):
        note = self.note(self.inbox, "Untitled.md")
        row, _ = documents.register(note, binding_id="b1")
        key = documents.doc_key("b1", row["doc_id"])
        self._failed_pass(key)
        with patch.object(ledger, "_enabled_pass_versions", return_value={"alpha": 1}):
            self.assertEqual(ledger.passes_failed()["failed"], 1)
            hygiene.sweep_empty()
            self.assertEqual(documents.get(key)["state"], "trashed")
            self.assertEqual(ledger.passes_failed()["failed"], 0)

    def test_phantom_doc_standard_failure_is_cleared(self):
        """The 2026-10-26 case: the empty doc's row, its file long gone."""
        binding = "doc-standard-Wax-4a02b5d4cb30"
        ledger.connect().execute(
            "INSERT INTO documents(binding_id,doc_id,path,sha256,state,attempts,first_seen,updated_at) "
            "VALUES(?,?,?,?,'failed',3,'2026-10-16T00:00:00Z','2026-10-26T00:00:00Z')",
            (binding, "doc-e3b0c44298fc1c14", str(self.inbox / "Wax" / "Untitled.md"), "e3b0"))
        self._failed_pass(f"doc-e3b0c44298fc1c14@{binding}", "classification")
        with patch.object(ledger, "_enabled_pass_versions", return_value={"classification": 1}):
            self.assertEqual(ledger.passes_failed()["failed"], 1)
            hygiene.sweep_empty()
            hygiene.sweep_empty()
            self.assertEqual(ledger.passes_failed()["failed"], 0)

    def test_a_live_run_lock_blocks_gone_marking(self):
        note = self.note(self.inbox, "x.md", "x")
        row, _ = documents.register(note, binding_id="b1")
        note.unlink()
        roots = [str(self.folder.root)]
        lock = documents.document_run_lock("b1", row["doc_id"])
        self.assertTrue(lock.acquire())
        try:
            self.assertIsNone(documents.mark_gone("b1", row["doc_id"], roots))
        finally:
            lock.release()
        self.assertEqual(documents.mark_gone("b1", row["doc_id"], roots)["previous_state"], "pending")

    def test_a_missing_vault_writes_nothing_off(self):
        """The vault unmounts/renames between sweeps: every row's file 'vanishes',
        but the folders are refused, so no row may be written off."""
        note = self.note(self.inbox, "a.md", "a")
        row, _ = documents.register(note, binding_id="b1")
        key = documents.doc_key("b1", row["doc_id"])
        gone_vault = self.root / "vault-away"
        self.vault.rename(gone_vault)
        try:
            for _ in range(4):
                out = hygiene.sweep_empty(record=True)
                self.assertEqual(out["counts"].get("gone_marked"), 0)
            self.assertTrue(out["last_error"], "the refusal is reported")
            self.assertEqual(documents.get(key)["state"], "pending")
        finally:
            gone_vault.rename(self.vault)
        note.write_text("edited after the vault came back")
        self.assertEqual(documents._arrivals({"binding_id": "b1", "folder": str(self.inbox)}), [],
                         "a known path stays known")

    def test_a_blink_between_sweeps_resets_the_grace(self):
        note = self.note(self.inbox, "a.md", "a")
        row, _ = documents.register(note, binding_id="b1")
        key = documents.doc_key("b1", row["doc_id"])
        note.rename(self.root / "elsewhere.md")
        hygiene.sweep_empty()
        (self.root / "elsewhere.md").rename(note)
        hygiene.sweep_empty()
        note.rename(self.root / "elsewhere.md")
        hygiene.sweep_empty()
        self.assertNotEqual(documents.get(key)["state"], "gone", "never missing twice in a row")
        hygiene.sweep_empty()
        self.assertEqual(documents.get(key)["state"], "gone")

    def test_rows_outside_the_swept_folders_and_relative_paths_are_left_alone(self):
        roots = [str(self.folder.root)]
        outside = documents.register(self.note(self.outside, "o.md", "o"), binding_id="b1")[0]
        self.assertFalse(documents.file_absent(str(self.outside / "nope.md"), roots))
        self.assertFalse(documents.file_absent("Inbox/Wax/note.md", roots), "relative: unknown")
        self.assertTrue(documents.file_absent(str(self.inbox / "nope.md"), roots))
        self.assertTrue(documents.file_absent(str(self.inbox / "no" / "dir" / "nope.md"), roots))
        (self.outside / "o.md").unlink()
        self.assertEqual(documents.missing_rows(roots), [])
        self.assertIsNone(documents.mark_gone("b1", outside["doc_id"], roots))

    def test_a_stat_failure_other_than_enoent_is_unknown_not_gone(self):
        roots = [str(self.folder.root)]
        target = str(self.inbox / "locked.md")
        for exc in (PermissionError(13, "denied"), OSError(5, "I/O error")):
            with self.subTest(exc=exc), patch.object(documents.os, "lstat", side_effect=exc):
                self.assertFalse(documents.file_absent(target, roots))

    def test_register_stores_absolute_paths(self):
        note = self.note(self.inbox, "rel.md", "relative")
        cwd = os.getcwd()
        os.chdir(self.inbox)
        try:
            row, _ = documents.register(Path("rel.md"), binding_id="")
        finally:
            os.chdir(cwd)
        self.assertEqual(row["path"], str(note))

    def test_mark_gone_judges_the_rows_current_path(self):
        """A re-point (a rename) that lands after the caller's snapshot saves the row."""
        old = self.note(self.inbox, "Untitled.md", "x")
        row, _ = documents.register(old, binding_id="b1")
        roots = [str(self.folder.root)]
        old.unlink()
        snapshot = documents.missing_rows(roots)
        self.assertEqual(len(snapshot), 1)
        new = self.note(self.inbox, "plan.md", "x")
        documents.repoint("b1", row["doc_id"], new, None)
        self.assertIsNone(documents.mark_gone("b1", row["doc_id"], roots))
        self.assertEqual(documents.get(documents.doc_key("b1", row["doc_id"]))["state"], "pending")

    def test_a_gone_note_that_comes_back_unchanged_is_not_an_arrival(self):
        binding = {"binding_id": "b1", "folder": str(self.inbox)}
        cases = (("processed", "completed"), ("failed", "failed"), ("seeded", None))
        for i, (expected, receipt_state) in enumerate(cases):
            with self.subTest(expected=expected):
                note = self.note(self.inbox, f"n{i}.md", f"note {i}")
                row, _ = documents.register(note, binding_id="b1")
                if receipt_state:
                    documents.record_receipt("b1", row["doc_id"], "doc", "def1", receipt_state)
                documents.mark_state("b1", row["doc_id"], "gone")
                self.assertEqual(documents._arrivals(binding), [], "same bytes: revived")
                self.assertEqual(documents.get(documents.doc_key("b1", row["doc_id"]))["state"],
                                 expected)

    def test_a_gone_note_that_comes_back_edited_is_an_arrival(self):
        note = self.note(self.inbox, "n.md", "before")
        row, _ = documents.register(note, binding_id="b1")
        documents.mark_state("b1", row["doc_id"], "gone")
        note.write_text("after")
        self.assertEqual(documents._arrivals({"binding_id": "b1", "folder": str(self.inbox)}), [note])

    def test_a_fresh_note_at_a_written_off_path_is_a_new_arrival(self):
        note = self.note(self.inbox, "Untitled.md", "first life")
        row, _ = documents.register(note, binding_id="b1")
        binding = {"binding_id": "b1", "folder": str(self.inbox)}
        self.assertEqual(documents._arrivals(binding), [])
        documents.mark_state("b1", row["doc_id"], "trashed")
        note.write_text("a different note entirely")
        self.assertEqual(documents._arrivals(binding), [note])

    def test_status_counts_the_new_states(self):
        binding = documents.ensure_binding("p", str(self.inbox))
        note = self.note(self.inbox, "a.md", "a")
        row, _ = documents.register(note, binding_id=binding["binding_id"])
        documents.mark_state(binding["binding_id"], row["doc_id"], "gone")
        shown = documents.status()["bindings"][0]
        self.assertEqual((shown["documents_gone"], shown["documents_trashed"]), (1, 0))
        self.assertEqual(shown["documents"], 1)

    def test_a_vanished_row_can_be_claimed_by_an_explicit_run(self):
        note = self.note(self.inbox, "a.md", "a")
        row, _ = documents.register(note, binding_id="")
        documents.mark_state("", row["doc_id"], "trashed")
        self.assertTrue(documents.claim_run("", row["doc_id"]))

    def test_rows_for_path_matches_through_a_symlinked_prefix_and_escapes_like(self):
        link = self.root / "d"
        link.symlink_to(self.vault)
        note = self.note(self.inbox, "100%_done.md", "x")
        other = self.note(self.inbox / "sub", "1000x_done.md", "y")
        via_link, _ = documents.register(link / "Inbox" / "100%_done.md", binding_id="")
        documents.register(other, binding_id="")
        rows = documents.rows_for_path(note)
        self.assertEqual([r["doc_id"] for r in rows], [via_link["doc_id"]])


# ------------------------------------------------------------ nightly ------

class _FakePipeline:
    """Stands in for `pipelines.run_document` (the real one is covered by the
    pipeline/summarize tests): registers the unbound row like the real run and
    writes the frontmatter a completed untitled-standard run leaves behind."""

    def __init__(self, test: "UntitledSweepTest"):
        self.test = test
        self.calls: list[Path] = []
        self.doc_ids: list = []
        self.slugs: dict[str, object] = {}
        self.mode = "complete"
        self.raise_for: set[str] = set()
        self.hold_locks: list = []

    def __call__(self, name, path, *, binding_id="", doc_id=None, explicit=False):
        assert name == hygiene.UNTITLED_PIPELINE and binding_id == "" and explicit
        path = Path(path)
        self.calls.append(path)
        self.doc_ids.append(doc_id)
        if path.name in self.raise_for:
            raise RuntimeError("pipeline blew up")
        row, _ = documents.register(path, binding_id="", origin=f"pipeline:{name}",
                                    adopt_doc_id=doc_id)
        if self.mode == "fail":
            return {"pipeline": name, "path": str(path), "failed": 1, "completed": 0,
                    "results": [{"state": "failed", "reason_code": "provider_down"}]}
        if self.mode == "exhausted":
            return {"pipeline": name, "path": str(path),
                    "skipped": "exhausted after 3 attempts (re-arm it with `wax documents retry`)"}
        if self.mode == "already":
            return {"pipeline": name, "path": str(path),
                    "skipped": "already processed at this definition (duplicate arrival)"}
        frontmatter.merge(path, {"summary": "A summary.",
                                 "title-slug": self.slugs.get(path.name, "a-great-title")})
        documents.note_own_write("", row["doc_id"], path)
        if self.mode == "locked":
            lock = documents.document_run_lock("", row["doc_id"])
            assert lock.acquire()
            self.hold_locks.append(lock)
        return {"pipeline": name, "path": str(path), "failed": 0, "completed": 2,
                "results": [{"state": "completed"}, {"state": "completed"}]}


class UntitledSweepTest(_Env):
    def setUp(self):
        super().setUp()
        self.fake = _FakePipeline(self)
        for p in (patch.object(pipelines, "run_document", side_effect=self.fake),
                  patch.object(pipelines, "compiled",
                               return_value={hygiene.UNTITLED_PIPELINE: {"definition": {}}})):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: [lock.release() for lock in self.fake.hold_locks])

    def test_empty_untitled_notes_are_trashed_and_other_empties_left_to_the_interval_job(self):
        a = self.note(self.inbox, "Untitled.md")
        b = self.note(self.sandbox, "Untitled 2.md", "---\ntags: []\n---\n")
        c = self.note(self.inbox, "Notes.md")
        out = hygiene.sweep_untitled()
        self.assertFalse(a.exists() or b.exists())
        self.assertTrue(c.exists())
        self.assertEqual(out["counts"]["trashed"], 2)
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(self.trash("Sandbox/Untitled 2.md").is_file())

    def test_untitled_with_content_is_summarized_and_renamed(self):
        note = self.note(self.inbox, "Untitled 3.md", "Some thoughts about the project.\n")
        binding_row, _ = documents.register(note, binding_id="b1", origin="binding-scan")
        out = hygiene.sweep_untitled()
        renamed = self.inbox / "a-great-title.md"
        self.assertEqual(self.fake.calls, [note])
        self.assertFalse(note.exists())
        self.assertTrue(renamed.is_file())
        self.assertEqual(out["counts"]["renamed"], 1)
        rows = documents.list_documents()
        self.assertEqual({r["binding_id"] for r in rows}, {"", "b1"})
        digest = documents.sha256_file(renamed)
        for r in rows:
            self.assertEqual(r["path"], str(renamed), "EVERY row at the old path is re-pointed")
            self.assertEqual(r["current_sha256"], digest)
        self.assertNotEqual(binding_row["sha256"], digest)
        binding = {"binding_id": "b1", "folder": str(self.inbox)}
        self.assertEqual(documents._arrivals(binding), [],
                         "the renamed note must not look like a new arrival to the binding")

    def test_rename_never_clobbers_an_existing_note(self):
        existing = self.note(self.inbox, "a-great-title.md", "I was here first")
        note = self.note(self.inbox, "Untitled.md", "new thoughts")
        hygiene.sweep_untitled()
        self.assertEqual(existing.read_text(), "I was here first")
        self.assertTrue((self.inbox / "a-great-title-1.md").is_file())
        self.assertFalse(note.exists())

    def test_rename_stays_in_the_notes_own_folder(self):
        note = self.note(self.inbox, "Wax/2026/Untitled.md", "deep note")
        hygiene.sweep_untitled()
        self.assertTrue((self.inbox / "Wax/2026/a-great-title.md").is_file())
        self.assertFalse(note.exists())

    def test_unsafe_titles_are_not_used(self):
        for slug in ("../evil", "a/b", ".hidden", "Untitled"):
            with self.subTest(slug=slug):
                note = self.note(self.inbox, "Untitled.md", f"body {slug}")
                self.fake.slugs["Untitled.md"] = slug
                out = hygiene.sweep_untitled()
                self.assertTrue(note.exists())
                self.assertEqual(out["counts"].get("summarized_unsafe_title"), 1)
                note.unlink()
                for row in documents.list_documents():
                    documents.mark_state(row["binding_id"], row["doc_id"], "gone")
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_pipeline_failure_leaves_the_file_alone(self):
        note = self.note(self.inbox, "Untitled.md", "content")
        self.fake.mode = "fail"
        out = hygiene.sweep_untitled(nightly=True)
        self.assertTrue(note.exists())
        self.assertEqual(out["counts"]["failed"], 1)
        self.assertEqual(out["notes"][0]["detail"], "provider_down")
        self.assertTrue(out["complete"], "a failed note is the receipts' business, not a failed night")
        self.assertIsNotNone(ledger.meta_get(hygiene.META_UNTITLED_DATE))

    def test_a_retry_adopts_the_identity_the_note_already_has(self):
        note = self.note(self.inbox, "Untitled.md", "content")
        self.fake.mode = "fail"
        hygiene.sweep_untitled(nightly=True)
        hygiene.sweep_untitled(nightly=True)
        first, second = self.fake.doc_ids
        self.assertIsNone(first, "nothing to adopt the first time")
        rows = documents.rows_for_path(note)
        self.assertEqual([r["doc_id"] for r in rows], [second])

    def test_an_edited_note_earns_a_new_identity_and_trashed_rows_are_not_adopted(self):
        note = self.note(self.inbox, "Untitled.md", "content")
        row, _ = documents.register(note, binding_id="")
        self.assertEqual(hygiene._known_identity(note), row["doc_id"])
        self.assertEqual(hygiene._known_identity(self.note(self.inbox, "Untitled 1.md", "other")), None)
        documents.mark_state("", row["doc_id"], "trashed")
        self.assertIsNone(hygiene._known_identity(note))
        documents.mark_state("", row["doc_id"], "pending")
        note.write_text("edited since")
        self.assertIsNone(hygiene._known_identity(note))

    def test_a_bound_row_is_never_adopted_by_the_unbound_sweep(self):
        note = self.note(self.inbox, "Untitled.md", "content")
        documents.register(note, binding_id="b1")
        self.assertIsNone(hygiene._known_identity(note))

    def test_exhausted_receipt_is_skipped_without_a_rename(self):
        note = self.note(self.inbox, "Untitled.md", "content")
        self.fake.mode = "exhausted"
        out = hygiene.sweep_untitled()
        self.assertTrue(note.exists())
        self.assertEqual(out["counts"]["skipped"], 1)

    def test_already_processed_note_is_still_renamed(self):
        note = self.note(self.inbox, "Untitled.md", "---\ntitle-slug: from-before\n---\nbody")
        self.fake.mode = "already"
        out = hygiene.sweep_untitled()
        self.assertEqual(out["counts"]["renamed"], 1)
        self.assertTrue((self.inbox / "from-before.md").is_file())
        self.assertFalse(note.exists())

    def test_completed_run_without_a_title_is_not_renamed(self):
        note = self.note(self.inbox, "Untitled.md", "content")
        self.fake.slugs["Untitled.md"] = ""
        out = hygiene.sweep_untitled()
        self.assertTrue(note.exists())
        self.assertEqual(out["counts"]["no_title"], 1)

    def test_rename_waits_for_a_live_run_lock(self):
        note = self.note(self.inbox, "Untitled.md", "content")
        self.fake.mode = "locked"
        out = hygiene.sweep_untitled()
        self.assertTrue(note.exists())
        self.assertEqual(out["counts"]["summarized_rename_locked"], 1)

    def test_a_placeholder_title_is_cleared_before_the_pipeline_and_a_real_one_kept(self):
        seen: dict[str, dict] = {}
        fake = self.fake

        def spy(name, path, **kw):
            seen[Path(path).name] = frontmatter.read(Path(path))[0]
            raw[Path(path).name] = Path(path).read_text()
            return fake(name, path, **kw)

        raw: dict[str, str] = {}
        placeholder = self.note(self.inbox, "Untitled 5.md",
                                "---\nmodified: 2026-10-09 01:22:51-04:00\ntitle: Untitled 5\n"
                                "title-slug: untitled-5\nkeep: me\n---\nBody.\n")
        retired = self.note(self.inbox, "Untitled 6.md",
                            "---\ntitle: 'Transcription: 20261009-010221-rec.ogg'\n---\nBody.\n")
        chosen = self.note(self.inbox, "Untitled 7.md", "---\ntitle: My Real Title\n---\nBody.\n")
        self.fake.slugs = {"Untitled 5.md": "five", "Untitled 6.md": "six", "Untitled 7.md": "seven"}
        with patch.object(pipelines, "run_document", side_effect=spy):
            hygiene.sweep_untitled()
        self.assertNotIn("title", seen["Untitled 5.md"])
        self.assertNotIn("title-slug", seen["Untitled 5.md"])
        self.assertEqual(seen["Untitled 5.md"]["keep"], "me")
        self.assertEqual(raw["Untitled 5.md"],
                         "---\nmodified: 2026-10-09 01:22:51-04:00\nkeep: me\n---\nBody.\n",
                         "only the placeholder lines are cut; no YAML round-trip")
        self.assertNotIn("title", seen["Untitled 6.md"])
        self.assertEqual(seen["Untitled 7.md"]["title"], "My Real Title")
        self.assertTrue((self.inbox / "five.md").is_file())
        self.assertFalse(placeholder.exists() or retired.exists() or chosen.exists())
        self.assertEqual([p.name for p in self.inbox.iterdir() if p.name.startswith(".")], [])

    def test_young_untitled_note_is_never_sent_to_the_pipeline(self):
        note = self.note(self.inbox, "Untitled.md", "typing right now", age=5)
        out = hygiene.sweep_untitled()
        self.assertTrue(note.exists())
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(out["counts"].get("young"), 1)

    def test_unparseable_and_dot_and_symlinked_untitled_notes_are_left_alone(self):
        bad = self.note(self.inbox, "Untitled.md", "---\ntitle: [unclosed\n---\nbody")
        hidden = self.note(self.inbox, ".obsidian/Untitled.md", "body")
        target = self.note(self.outside, "Untitled.md", "outside body")
        (self.inbox / "Untitled 9.md").symlink_to(target)
        hygiene.sweep_untitled()
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(bad.exists() and hidden.exists() and target.exists())

    def test_one_bad_note_does_not_stop_the_run(self):
        a = self.note(self.inbox, "Untitled 1.md", "first")
        b = self.note(self.inbox, "Untitled 2.md", "second")
        self.fake.raise_for = {"Untitled 1.md"}
        self.fake.slugs["Untitled 2.md"] = "second-note"
        out = hygiene.sweep_untitled(nightly=True)
        self.assertTrue(a.exists())
        self.assertTrue((self.inbox / "second-note.md").is_file())
        self.assertFalse(b.exists())
        self.assertEqual(out["counts"]["errors"], 1)
        self.assertIn("pipeline blew up", out["last_error"])

    def test_uncompiled_pipeline_touches_nothing_and_records_a_clear_error(self):
        empty = self.note(self.inbox, "Untitled.md")
        content = self.note(self.inbox, "Untitled 1.md", "content")
        with patch.object(pipelines, "compiled", return_value={}):
            out = hygiene.sweep_untitled(nightly=True)
        self.assertTrue(empty.exists() and content.exists())
        self.assertEqual(self.fake.calls, [])
        self.assertIn("is not compiled", out["last_error"])
        self.assertIn("wax pipeline compile untitled-standard", out["last_error"])
        self.assertFalse(out["complete"])
        self.assertIsNone(ledger.meta_get(hygiene.META_UNTITLED_DATE), "tonight is not used up")
        self.assertIn("is not compiled", hygiene.load_summary("untitled")["last_error"])

    def test_budget_defers_the_rest_and_the_night_stays_open(self):
        self.note(self.inbox, "Untitled 1.md", "one")
        self.note(self.inbox, "Untitled 2.md", "two")
        self.fake.slugs.update({"Untitled 1.md": "one-title", "Untitled 2.md": "two-title"})
        out = hygiene.sweep_untitled(nightly=True, max_runs=1)
        self.assertEqual(out["counts"]["renamed"], 1)
        self.assertEqual(out["counts"]["deferred"], 1)
        self.assertFalse(out["complete"])
        self.assertIsNone(ledger.meta_get(hygiene.META_UNTITLED_DATE))
        out = hygiene.sweep_untitled(nightly=True, max_runs=1)
        self.assertTrue(out["complete"])
        self.assertTrue((self.inbox / "two-title.md").is_file())

    def test_skipped_runs_do_not_spend_the_budget(self):
        self.fake.mode = "exhausted"
        for n in range(4):
            self.note(self.inbox, f"Untitled {n}.md", f"body {n}")
        out = hygiene.sweep_untitled(max_runs=1)
        self.assertEqual(out["counts"]["skipped"], 4)
        self.assertNotIn("deferred", out["counts"])

    def test_dry_run_changes_nothing_and_calls_no_pipeline(self):
        empty = self.note(self.inbox, "Untitled.md")
        content = self.note(self.inbox, "Untitled 1.md", "content")
        out = hygiene.sweep_untitled(dry_run=True, nightly=True)
        self.assertTrue(empty.exists() and content.exists())
        self.assertFalse((self.vault / ".trash").exists())
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(out["counts"]["would_trash"], 1)
        self.assertEqual(out["counts"]["would_summarize"], 1)
        self.assertIsNone(ledger.meta_get(hygiene.META_UNTITLED_DATE))
        self.assertIsNone(hygiene.load_summary("untitled"))

    def test_cli_form_does_not_set_the_nightly_date(self):
        self.note(self.inbox, "Untitled.md")
        hygiene.sweep_untitled()
        self.assertIsNone(ledger.meta_get(hygiene.META_UNTITLED_DATE))
        self.assertIsNone(hygiene.load_summary("untitled"))


class NightlyGateTest(_Env):
    def _at(self, *args):
        return patch.object(hygiene, "_now_local", return_value=datetime(*args))

    def test_due_from_the_configured_hour_once_per_local_date(self):
        with self._at(2026, 10, 27, 2, 59):
            self.assertFalse(hygiene.untitled_due())
        with self._at(2026, 10, 27, 3, 0):
            self.assertTrue(hygiene.untitled_due())
        ledger.meta_set(hygiene.META_UNTITLED_DATE, "2026-10-27")
        with self._at(2026, 10, 27, 23, 59):
            self.assertFalse(hygiene.untitled_due())
        with self._at(2026, 10, 28, 2, 59):
            self.assertFalse(hygiene.untitled_due())
        with self._at(2026, 10, 28, 3, 1):
            self.assertTrue(hygiene.untitled_due())

    def test_hour_knob(self):
        with patch.object(hygiene, "UNTITLED_SWEEP_HOUR", 22), self._at(2026, 10, 27, 21, 59):
            self.assertFalse(hygiene.untitled_due())
        with patch.object(hygiene, "UNTITLED_SWEEP_HOUR", 22), self._at(2026, 10, 27, 22, 0):
            self.assertTrue(hygiene.untitled_due())

    def test_a_completed_run_records_the_local_date_at_its_start(self):
        with self._at(2026, 10, 27, 3, 5), \
                patch.object(pipelines, "compiled", return_value={hygiene.UNTITLED_PIPELINE: {}}):
            hygiene.sweep_untitled(nightly=True)
        self.assertEqual(ledger.meta_get(hygiene.META_UNTITLED_DATE), "2026-10-27")

    def test_malformed_knobs_fall_back(self):
        with patch.dict(os.environ, {"WAX_FOO": "abc"}):
            self.assertEqual(hygiene._env_number("WAX_FOO", 7.0), 7.0)
            self.assertEqual(hygiene._env_number("WAX_UNSET_FOO", 7.0), 7.0)


# ------------------------------------------- the real pipeline, not a fake ----

class RealUntitledPipelineTest(_Env):
    """`run_document` for real (a compiled `untitled-standard` over a one-pass
    registry): the fake above registers whatever it is told, which is exactly how
    a retry that mints a new identity every night went unnoticed."""

    def _compile(self, script_body: str) -> None:
        from wax import events, passes
        from documents_test import _close_test_connection as close, _fake_frontmatters
        registry = self.root / "passes.d"
        pipes = self.root / "pipelines.d"
        registry.mkdir()
        pipes.mkdir()
        script = registry / "alpha"
        script.write_text("#!/bin/sh\n" + script_body)
        script.chmod(0o755)
        (registry / "alpha.yaml").write_text(
            "slug: alpha\nversion: 1\ndescription: x\nenabled: true\nauto: false\n"
            "capabilities: [document]\nrequires: []\n"
            f"command: [{json.dumps(str(script))}, \"{{md_path}}\"]\n")
        (pipes / "untitled-standard.yaml").write_text(
            "name: untitled-standard\nversion: 1\ndescription: x\ntrigger: manual\n"
            "enabled: true\nfailure_policy: halt\npasses: [alpha]\n")
        for p in (patch.object(passes, "REGISTRY_DIR", registry),
                  patch.object(pipelines, "PIPELINES_DIR", pipes),
                  patch.object(passes, "_run_frontmatters", side_effect=_fake_frontmatters),
                  patch.object(events, "emit_ep_command", return_value="c"),
                  patch.object(events, "emit")):
            p.start()
            self.addCleanup(p.stop)
        close()
        pipelines.compile(["untitled-standard"])

    def _night(self, note: Path):
        old = time.time() - 3600
        if note.exists():
            os.utime(note, (old, old))
        return hygiene.sweep_untitled(nightly=True)

    def _state(self):
        db = ledger.connect()
        return (len(db.execute("SELECT 1 FROM documents").fetchall()),
                [r["attempts"] for r in db.execute("SELECT attempts FROM document_receipts")],
                ledger.passes_failed()["failed"])

    def test_a_note_that_keeps_failing_keeps_one_identity_and_stops_at_the_bound(self):
        self._compile("echo boom >&2\nexit 1\n")
        note = self.note(self.inbox, "Untitled.md", "Some real thoughts about the project.\n")
        seen = []
        for _ in range(documents.MAX_ATTEMPTS + 2):
            out = self._night(note)
            seen.append((self._state(), out["counts"]))
        self.assertEqual({s[0][0] for s in seen}, {1}, "one documents row, however many nights")
        self.assertEqual([s[0][1] for s in seen][:documents.MAX_ATTEMPTS],
                         [[n] for n in range(1, documents.MAX_ATTEMPTS + 1)])
        self.assertEqual(seen[-1][0][1], [documents.MAX_ATTEMPTS], "the bound trips")
        self.assertEqual({s[0][2] for s in seen}, {1}, "ONE failing document, not one per night")
        self.assertEqual(seen[-1][1].get("skipped"), 1, "exhausted: skipped, no new run")
        self.assertTrue(note.exists())

    def test_a_flaky_note_that_finally_succeeds_leaves_no_orphaned_failures(self):
        counter = self.root / "tries"
        counter.write_text("")
        ok = json.dumps({"wax_ep_version": 1, "frontmatter": {"title-slug": "Plan next steps"}})
        self._compile(f"echo x >> {counter}\n"
                      f"if [ $(wc -l < {counter}) -lt 3 ]; then echo boom >&2; exit 1; fi\n"
                      f"printf '%s\\n' '{ok}'\n")
        note = self.note(self.inbox, "Untitled.md", "Some real thoughts about the project.\n")
        self._night(note)
        self._night(note)
        self.assertEqual(self._state()[2], 1)
        out = self._night(note)
        self.assertEqual(out["counts"].get("renamed"), 1, out["counts"])
        self.assertTrue((self.inbox / "Plan next steps.md").is_file())
        self.assertEqual(self._state()[0], 1)
        self.assertEqual(self._state()[2], 0, "the failed pass rows were overwritten, not orphaned")

    def test_a_completed_note_without_a_usable_title_is_not_rerun_every_night(self):
        self._compile("printf '%s\\n' '{\"wax_ep_version\": 1, \"state\": \"skipped\", "
                      "\"reason_code\": \"no_text\"}'\n")
        note = self.note(self.inbox, "Untitled.md", "Some real thoughts about the project.\n")
        for _ in range(3):
            out = self._night(note)
            self.assertEqual(out["counts"].get("no_title"), 1, out["counts"])
        self.assertEqual(self._state()[0], 1)
        self.assertEqual(self._state()[1], [1], "one receipt, run once")


# --------------------------------------------------------------- worker ----

class WorkerCadenceTest(_Env):
    def setUp(self):
        super().setUp()
        self.registered = [
            patch.object(state, "pipeline_enabled", return_value=True),
            patch.object(worker, "run_once", return_value=None),
            patch.object(worker, "sweep_failed_passes", return_value={"candidates": 0}),
            patch.object(documents, "scan", return_value={}),
            patch.object(hygiene, "runtime_ledger_is_live", return_value=True),
            patch.object(pipelines, "compiled",
                         return_value={hygiene.UNTITLED_PIPELINE: {"definition": {}}}),
        ]
        for p in self.registered:
            p.start()
            self.addCleanup(p.stop)

    def _worker(self, *, scanned=False):
        w = worker.Worker()
        now = time.monotonic()
        w._last_sweep = now
        w._last_doc_scan = now
        return w

    def test_empty_sweep_runs_once_per_interval_with_the_hygiene_busy_label(self):
        busy = []
        w = self._worker()
        w._last_untitled_try = time.monotonic()
        with patch.object(hygiene, "sweep_empty",
                          side_effect=lambda **kw: busy.append(w._busy)) as sweep:
            w._tick_once()
            self.assertEqual(sweep.call_count, 1, "first tick runs it")
            self.assertEqual(sweep.call_args.kwargs, {"record": True})
            self.assertEqual(busy, ["hygiene"])
            self.assertIsNone(w._busy)
            w._tick_once()
            self.assertEqual(sweep.call_count, 1, "not due again within the interval")
            w._last_empty_sweep = time.monotonic() - hygiene.EMPTY_SWEEP_INTERVAL_S - 1
            w._tick_once()
            self.assertEqual(sweep.call_count, 2)

    def test_both_jobs_run_on_the_first_tick_even_just_after_boot(self):
        """time.monotonic() counts from boot: a machine up for a minute must not
        hold an owed sweep back until it has been up for the interval."""
        with patch.object(worker.time, "monotonic", return_value=60.0), \
                patch.object(hygiene, "sweep_empty") as empty, \
                patch.object(hygiene, "sweep_untitled") as nightly, \
                patch.object(hygiene, "runtime_ledger_is_live", return_value=True), \
                patch.object(hygiene, "_now_local", return_value=datetime(2026, 10, 27, 8, 0)), \
                patch.object(pipelines, "compiled", return_value={}):
            w = worker.Worker()
            w._last_sweep = w._last_doc_scan = 60.0
            w._tick_once()
        empty.assert_called_once()
        nightly.assert_called_once()

    def test_a_ledger_that_is_not_the_runtime_roots_is_never_swept(self):
        w = self._worker()
        with patch.object(hygiene, "runtime_ledger_is_live", return_value=False), \
                patch.object(hygiene, "sweep_empty") as sweep, \
                patch.object(hygiene, "sweep_untitled") as nightly, \
                patch.object(hygiene, "_now_local", return_value=datetime(2026, 10, 27, 4, 0)):
            w._tick_once()
        sweep.assert_not_called()
        nightly.assert_not_called()

    def test_runtime_ledger_check_matches_the_real_layout(self):
        with patch.object(paths, "AUDIO", self.root / "rt"), \
                patch.object(paths, "DB", self.root / "rt" / "var" / "wax.db"):
            self.assertTrue(_REAL_LEDGER_IS_LIVE())
        with patch.object(paths, "AUDIO", self.root / "rt"), \
                patch.object(paths, "DB", self.root / "elsewhere" / "wax.db"):
            self.assertFalse(_REAL_LEDGER_IS_LIVE())

    def test_disabled_pipeline_runs_no_hygiene(self):
        w = worker.Worker()
        with patch.object(state, "pipeline_enabled", return_value=False), \
                patch.object(hygiene, "sweep_empty") as sweep:
            w._tick_once()
        sweep.assert_not_called()

    def test_hygiene_crash_never_escapes_the_tick_and_is_recorded(self):
        w = self._worker()
        with patch.object(hygiene, "sweep_empty", side_effect=RuntimeError("db on fire")), \
                self.assertLogs("wax.worker", level="ERROR"):
            w._tick_once()
        self.assertIn("db on fire", hygiene.load_summary("empty")["last_error"])
        self.assertIsNone(w._busy)

    def test_nightly_runs_once_per_date_across_a_new_worker(self):
        a, b = self.note(self.inbox, "Untitled.md", "one"), None
        fake = _FakePipeline(self)
        with patch.object(pipelines, "run_document", side_effect=fake):
            real = hygiene.sweep_untitled
            with patch.object(hygiene, "sweep_empty"), \
                    patch.object(hygiene, "sweep_untitled", wraps=real) as nightly:
                with patch.object(hygiene, "_now_local", return_value=datetime(2026, 10, 27, 2, 30)):
                    w = self._worker()
                    w._tick_once()
                    nightly.assert_not_called()
                with patch.object(hygiene, "_now_local", return_value=datetime(2026, 10, 27, 3, 10)):
                    w = self._worker()
                    w._tick_once()
                    self.assertEqual(nightly.call_count, 1)
                    self.assertEqual(nightly.call_args.kwargs, {"nightly": True})
                    self.assertTrue((self.inbox / "a-great-title.md").is_file())
                    self.assertEqual(ledger.meta_get(hygiene.META_UNTITLED_DATE), "2026-10-27")
                with patch.object(hygiene, "_now_local", return_value=datetime(2026, 10, 27, 3, 20)):
                    restarted = self._worker()
                    restarted._tick_once()
                    self.assertEqual(nightly.call_count, 1, "a restarted waxd must not repeat the night")
                with patch.object(hygiene, "_now_local", return_value=datetime(2026, 10, 28, 3, 5)):
                    tomorrow = self._worker()
                    tomorrow._tick_once()
                    self.assertEqual(nightly.call_count, 2)

    def test_an_incomplete_night_retries_but_not_every_tick(self):
        with patch.object(hygiene, "sweep_empty"), \
                patch.object(hygiene, "_now_local", return_value=datetime(2026, 10, 27, 3, 10)), \
                patch.object(pipelines, "compiled", return_value={}):
            w = self._worker()
            w._tick_once()
            self.assertIsNone(ledger.meta_get(hygiene.META_UNTITLED_DATE))
            first = hygiene.load_summary("untitled")
            self.assertIn("is not compiled", first["last_error"])
            with patch.object(hygiene, "sweep_untitled") as again:
                w._tick_once()
                again.assert_not_called()
                w._last_untitled_try = time.monotonic() - hygiene.UNTITLED_RETRY_S - 1
                w._tick_once()
                again.assert_called_once()


# --------------------------------------------------------------- doctor ----

class HealthTest(_Env):
    NOW = 1_800_000_000.0

    def _empty(self, *, age=60.0, error=None, interval=300.0):
        hygiene._record("empty", {
            "kind": "empty", "last_run_at": documents._iso_at(self.NOW - age),
            "interval_s": interval, "counts": {"trashed": 2, "gone_marked": 1},
            "last_error": error})

    def _health(self, **kw):
        kw.setdefault("pipeline_enabled", True)
        kw.setdefault("waxd_running", True)
        return hygiene.health(now=self.NOW, today="2026-10-27", **kw)

    def test_fresh_clean_run_passes(self):
        self._empty()
        ledger.meta_set(hygiene.META_UNTITLED_DATE, "2026-10-27")
        out = self._health()
        self.assertEqual(out["status"], "pass", out)
        self.assertIn("2 trashed", out["detail"])

    def test_last_error_fails(self):
        self._empty(error="vault went away")
        out = self._health()
        self.assertEqual(out["status"], "fail")
        self.assertIn("vault went away", out["detail"])
        self.assertTrue(out["fix"])

    def test_stale_run_fails_only_while_enabled_and_running(self):
        self._empty(age=3 * 300 + 1)
        self.assertEqual(self._health()["status"], "fail")
        self._empty(age=3 * 300 - 1)
        self.assertNotEqual(self._health()["status"], "fail")
        self._empty(age=10_000)
        self.assertNotEqual(self._health(pipeline_enabled=False)["status"], "fail")
        self.assertNotEqual(self._health(waxd_running=False)["status"], "fail")

    def test_stale_run_while_the_worker_is_busy_is_only_a_warning(self):
        self._empty(age=10_000)
        out = self._health(worker={"state": "live", "busy": "inbox"})
        self.assertEqual(out["status"], "warn")
        self.assertIn("busy", out["detail"])
        out = self._health(worker={"state": "down"})
        self.assertEqual(out["status"], "fail")

    def test_uses_the_interval_the_daemon_recorded(self):
        self._empty(age=2_000, interval=900.0)
        self.assertNotEqual(self._health()["status"], "fail")
        self._empty(age=2_800, interval=900.0)
        self.assertEqual(self._health()["status"], "fail")

    def test_never_ran_fails_when_it_should_be_running(self):
        self.assertEqual(self._health()["status"], "fail")
        self.assertEqual(self._health(pipeline_enabled=False)["status"], "pass")

    def test_stale_nightly_date_warns(self):
        self._empty()
        ledger.meta_set(hygiene.META_UNTITLED_DATE, "2026-10-25")
        self.assertEqual(self._health()["status"], "pass", "two days is tolerated")
        ledger.meta_set(hygiene.META_UNTITLED_DATE, "2026-10-24")
        out = self._health()
        self.assertEqual(out["status"], "warn")
        self.assertIn("2026-10-24", out["detail"])

    def test_nightly_error_warns_and_never_run_warns_after_two_days(self):
        self._empty()
        hygiene._record("untitled", {"kind": "untitled", "last_run_at": "x",
                                     "last_error": "pipeline 'untitled-standard' is not compiled"})
        out = self._health()
        self.assertEqual(out["status"], "warn")
        ledger.meta_set(hygiene.META_UNTITLED_DATE, "2026-10-27")
        self.assertEqual(self._health()["status"], "warn", "the error still shows")
        ledger.connect().execute("DELETE FROM meta WHERE k=?", (hygiene.META_UNTITLED_DATE,))
        ledger.meta_set(hygiene.META_UNTITLED, "{}")
        ledger.meta_set(hygiene.META_FIRST_RUN, documents._iso_at(self.NOW - 3 * 86400))
        self.assertEqual(self._health()["status"], "warn")
        self.assertIn("never completed", self._health()["detail"])


# ------------------------------------------------------------------ CLI ----

class CliTest(_Env):
    def _run(self, *args):
        env = dict(os.environ, WAX_ROOT=str(self.root / "wax-root"),
                   WAX_DOC_HYGIENE_FOLDERS=self.folders)
        return subprocess.run([sys.executable, str(COMPONENT / "bin/wax"), *args],
                              capture_output=True, text=True, env=env, timeout=120)

    def test_sweep_empty_dry_run_then_real(self):
        note = self.note(self.inbox, "Untitled.md")
        dry = self._run("documents", "sweep-empty", "--dry-run", "--json")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(json.loads(dry.stdout)["counts"]["would_trash"], 1)
        self.assertTrue(note.exists())
        real = self._run("documents", "sweep-empty", "--json")
        self.assertEqual(real.returncode, 0, real.stderr)
        self.assertEqual(json.loads(real.stdout)["counts"]["trashed"], 1)
        self.assertFalse(note.exists())
        self.assertTrue(self.trash("Inbox/Untitled.md").is_file())

    def test_sweep_untitled_reports_an_uncompiled_pipeline(self):
        note = self.note(self.inbox, "Untitled.md")
        out = self._run("documents", "sweep-untitled", "--json")
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("is not compiled", json.loads(out.stdout)["last_error"])
        self.assertTrue(note.exists())

    def test_doctor_config_lists_every_new_knob(self):
        text = (COMPONENT / "bin/wax").read_text()
        for knob in ("WAX_DOC_HYGIENE_FOLDERS", "WAX_DOC_HYGIENE_MIN_AGE_S",
                     "WAX_EMPTY_DOC_SWEEP_INTERVAL_S", "WAX_UNTITLED_SWEEP_HOUR",
                     "WAX_UNTITLED_SWEEP_MAX", "WAX_SWEEP_INTERVAL_S",
                     "WAX_DOCUMENT_SCAN_INTERVAL_S"):
            self.assertIn(f'("{knob}",', text)


if __name__ == "__main__":
    unittest.main()
