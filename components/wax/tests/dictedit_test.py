"""`wax dict`: editing Dictionary.yaml by command, the transcript scan, and the fix plan.

The editor must change exactly one entry and leave every comment, section and
neighbour byte-for-byte alone; the linter must refuse the heard forms that went
wrong in calibration; the scan must tell spelled-right, caught and near misses
apart on a real (temporary) vault.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from wax import dictedit, dictionary, ledger, paths

COMPONENT_ROOT = next(parent for parent in Path(__file__).resolve().parents
                      if (parent / "pyproject.toml").is_file())
WAX = COMPONENT_ROOT / "bin" / "wax"

BOOK = textwrap.dedent("""\
    # The HeyMa custom dictionary. A comment that must survive.
    version: 1

    terms:
      # -- Platform -------------------------------------------------------------
      Skillex: {heard: [skill x], about: "the agent-skill registry"}
      Bloodbank: [blood bank]   # trailing comment stays
      Plane: "the ticket board"

      # -- People ---------------------------------------------------------------
      Jarad:
""")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.book = self.root / "Dictionary.yaml"
        self.book.write_text(BOOK)
        self.var = patch.object(paths, "VAR", self.root / "var")
        self.var.start()

    def tearDown(self):
        self.var.stop()
        self.tmp.cleanup()

    def terms(self):
        return yaml.safe_load(self.book.read_text())["terms"]


class EditTest(Base):
    def test_add_lands_in_a_new_trailing_section_and_touches_nothing_else(self):
        change = dictedit.add("Kapture", about="the browser daemon", heard=("capture d",), path=self.book)
        self.assertEqual(change.action, "added")
        text = self.book.read_text()
        self.assertTrue(text.startswith(BOOK.rstrip("\n")), "existing lines must be untouched")
        self.assertIn("# -- Added with wax dict", text)
        self.assertEqual(self.terms()["Kapture"], {"heard": ["capture d"], "about": "the browser daemon"})
        dictionary.load(self.book)
        header = next(ln for ln in text.splitlines() if "Added with wax dict" in ln)
        platform = next(ln for ln in text.splitlines() if "-- Platform" in ln)
        self.assertEqual(len(header), len(platform))

    def test_add_into_a_named_section_goes_after_its_last_entry(self):
        dictedit.add("Hermes", about="the agent runtime", section="platform", path=self.book)
        lines = self.book.read_text().splitlines()
        self.assertEqual(lines.index('  Hermes: "the agent runtime"'), lines.index('  Plane: "the ticket board"') + 1)

    def test_an_unknown_section_is_refused_with_the_known_ones(self):
        with self.assertRaises(dictedit.DictEditError) as caught:
            dictedit.add("Hermes", section="nowhere", path=self.book)
        self.assertIn("Platform", caught.exception.hint)
        self.assertEqual(self.book.read_text(), BOOK)

    def test_awkward_terms_are_quoted_so_they_read_back_exactly(self):
        for term in ("true", "C: drive", "#hash", "yes", "1.0", "it's"):
            dictedit.add(term, about="x: y # z", path=self.book)
            self.assertEqual(self.terms()[term], "x: y # z", term)
        dictionary.load(self.book)

    def test_existing_term_needs_update_and_update_appends_heard_and_replaces_about(self):
        with self.assertRaises(dictedit.DictEditError) as caught:
            dictedit.add("skillex", path=self.book)
        self.assertIn("wax dict heard", caught.exception.hint)
        change = dictedit.add("skillex", heard=("skillix",), about="the registry", update=True, path=self.book)
        self.assertEqual(change.term, "Skillex")
        self.assertEqual(self.terms()["Skillex"], {"heard": ["skill x", "skillix"], "about": "the registry"})
        # The trailing comment on a neighbour survives.
        self.assertIn("Bloodbank: [blood bank]   # trailing comment stays", self.book.read_text())

    def test_nothing_new_is_unchanged_and_writes_nothing(self):
        before = self.book.read_text()
        change = dictedit.add("Skillex", heard=("Skill X",), update=True, path=self.book)
        self.assertEqual(change.action, "unchanged")
        self.assertEqual(self.book.read_text(), before)

    def test_a_form_spelled_like_the_term_is_noted_not_written(self):
        change = dictedit.add("Skillex", heard=("skill ex", "SKILLEX"), update=True, path=self.book)
        self.assertEqual(change.action, "unchanged")
        self.assertEqual(len(change.lint.notes), 2)

    def test_dry_run_writes_nothing(self):
        change = dictedit.add("Kapture", dry_run=True, path=self.book)
        self.assertFalse(change.written)
        self.assertEqual(self.book.read_text(), BOOK)

    def test_remove_a_term_or_some_heard_forms(self):
        dictedit.remove("bloodbank", path=self.book)
        self.assertNotIn("Bloodbank", self.terms())
        dictedit.remove("Skillex", heard=("SKILL X",), path=self.book)
        self.assertEqual(self.terms()["Skillex"], "the agent-skill registry")
        with self.assertRaises(dictedit.DictEditError):
            dictedit.remove("Skillex", heard=("never",), path=self.book)
        with self.assertRaises(dictedit.DictEditError):
            dictedit.remove("Nope", path=self.book)
        self.assertIn("# -- People", self.book.read_text())

    def test_a_multi_line_entry_is_left_to_hand_editing(self):
        self.book.write_text(BOOK + "  Momo:\n    heard: [mo mo]\n    about: the PM\n")
        with self.assertRaises(dictedit.DictEditError) as caught:
            dictedit.add("Momo", heard=("moe moe",), update=True, path=self.book)
        self.assertIn("by hand", caught.exception.hint)
        # ...but adding after it still works, and its lines are kept whole.
        dictedit.add("Hermes", path=self.book)
        self.assertEqual(self.terms()["Momo"], {"heard": ["mo mo"], "about": "the PM"})

    def test_verify_refuses_an_edit_that_changes_a_neighbour(self):
        old = self.book.read_text()
        bad = old.replace("Plane:", "Plain:")
        with self.assertRaises(dictedit.DictEditError):
            dictedit._verify(old, bad, {"Plane": "the ticket board"}, self.book)

    def test_write_refuses_when_the_file_changed_underneath(self):
        old = self.book.read_text()
        self.book.write_text(old + "  Late:\n")
        with self.assertRaises(dictedit.DictEditError):
            dictedit._write(self.book, old, old + "  Mine:\n")
        self.assertIn("Late", self.book.read_text())

    def test_listing_carries_sections(self):
        rows = {r["term"]: r for r in dictedit.listing(self.book)}
        self.assertEqual(rows["Skillex"]["section"], "Platform")
        self.assertEqual(rows["Jarad"]["section"], "People")
        self.assertEqual(rows["Skillex"]["heard"], ["skill x"])


class ReviewRegressionTest(Base):
    """Defects the adversarial review of `wax dict` reproduced (2026-11-05)."""

    def test_a_quoted_key_with_a_colon_can_be_updated_and_removed(self):
        dictedit.add("Foo: bar", about="x", path=self.book)
        self.assertIn("Foo: bar", dictedit.layout(self.book.read_text()).entries)
        dictedit.add("Foo: bar", about="y", update=True, path=self.book)
        self.assertEqual(self.terms()["Foo: bar"], "y")
        dictedit.remove("Foo: bar", path=self.book)
        self.assertNotIn("Foo: bar", self.terms())

    def test_control_characters_are_refused_not_a_traceback(self):
        for kwargs in ({"about": "a\x7fb"}, {"heard": ("a\x80b",)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(dictedit.DictEditError):
                dictedit.add("Zed", path=self.book, **kwargs)
        self.assertEqual(self.book.read_text(), BOOK)

    def test_rewriting_a_line_keeps_its_trailing_comment(self):
        dictedit.add("Bloodbank", heard=("blood bang",), update=True, path=self.book)
        self.assertIn('Bloodbank: [blood bank, blood bang]   # trailing comment stays', self.book.read_text())
        dictedit.add("Hermes", about="has # inside", path=self.book)
        dictedit.add("Hermes", about="new", update=True, path=self.book)
        self.assertEqual(self.terms()["Hermes"], "new")

    def test_section_prefers_exact_then_refuses_ambiguity(self):
        self.book.write_text(BOOK + "\n  # -- Dev tools ----\n  Mise:\n\n  # -- Tools ----\n  Jq:\n")
        dictedit.add("Uv", section="tools", path=self.book)
        lines = self.book.read_text().splitlines()
        self.assertEqual(lines.index("  Uv:"), lines.index("  Jq:") + 1)
        with self.assertRaises(dictedit.DictEditError) as caught:
            dictedit.add("Fd", section="o", path=self.book)
        self.assertIn("several", str(caught.exception))
        with self.assertRaises(dictedit.DictEditError) as caught:
            dictedit.add("Fd", section="zzz", path=self.book)
        self.assertIn("Dev tools", caught.exception.hint)

    def test_a_symlinked_dictionary_is_edited_through_the_link(self):
        real = self.root / "real.yaml"
        self.book.rename(real)
        self.book.symlink_to(real)
        dictedit.add("Zed", path=self.book)
        self.assertTrue(self.book.is_symlink())
        self.assertIn("Zed", yaml.safe_load(real.read_text())["terms"])

    def test_a_missing_dictionary_is_a_refusal(self):
        with self.assertRaises(dictedit.DictEditError):
            dictedit.add("Zed", path=self.root / "missing.yaml")
        with self.assertRaises(dictedit.DictEditError):
            dictedit.remove("Zed", path=self.root / "missing.yaml")

    def test_a_form_the_pass_only_matches_when_listed_is_written(self):
        self.book.write_text(BOOK + "  AutomaticAI: the consultancy\n")
        change = dictedit.add("AutomaticAI", heard=("automatic a i",), update=True, path=self.book)
        self.assertEqual(change.action, "updated")
        self.assertEqual(self.terms()["AutomaticAI"], {"heard": ["automatic a i"], "about": "the consultancy"})

    def test_scan_counts_spoken_dots_like_the_pass(self):
        self.book.write_text(BOOK + "  delo.sh: the homelab domain\n")
        vault = self.root / "vault"
        vault.mkdir()
        (vault / "a.md").write_text("go to delo dot sh now\n")
        report = dictedit.scan("delo.sh", book=dictionary.load(self.book), vault=vault, workers=1)
        self.assertEqual(len(report["notes_to_fix"]), 1)


class LintTest(Base):
    def setUp(self):
        super().setUp()
        self.loaded = dictionary.load(self.book)

    def test_term_collisions_are_errors(self):
        for term in ("skillex", "Skill-ex", "blood bank"):
            with self.subTest(term=term), self.assertRaises(dictedit.DictEditError):
                dictedit.add(term, path=self.book)
        self.assertEqual(self.book.read_text(), BOOK)

    def test_heard_collisions_are_errors_even_with_force(self):
        for form in ("blood bank", "plane"):
            with self.subTest(form=form), self.assertRaises(dictedit.DictEditError):
                dictedit.add("Skillex", heard=(form,), update=True, force=True, path=self.book)

    def test_fragments_and_ordinary_words_are_refused_without_force(self):
        for form in ("skill", "kill", "table"):
            with self.subTest(form=form):
                with self.assertRaises(dictedit.DictEditError) as caught:
                    dictedit.add("Skillex", heard=(form,), update=True, path=self.book)
                self.assertIn("--force", caught.exception.hint)
        change = dictedit.add("Skillex", heard=("table",), update=True, force=True, path=self.book)
        self.assertEqual(change.action, "updated")

    def test_ordinary_phrases_and_function_word_edges_warn(self):
        lint = dictedit.lint_heard("Bloodbank", "blood bang", self.loaded)
        self.assertTrue(lint.warnings)
        self.assertFalse(lint.refusals)
        lint = dictedit.lint_heard("Skillex", "the skillix", self.loaded)
        self.assertTrue(any("'the'" in w for w in lint.warnings))


class ScanTest(Base):
    def test_scan_separates_right_caught_and_near(self):
        vault = self.root / "vault"
        (vault / ".trash").mkdir(parents=True)
        (vault / "a.md").write_text("---\ntitle: a\n---\nI pushed it to Skillex. Then skill x synced it.\n")
        (vault / "b.md").write_text("Then skillix again, and `Skillex` in code does not count.\n")
        (vault / ".trash" / "c.md").write_text("Skillex skill x\n")
        loaded = dictionary.load(self.book)
        report = dictedit.scan("skillex", book=loaded, vault=vault, workers=1)
        self.assertEqual(report["term"], "Skillex")
        self.assertEqual(report["notes_scanned"], 2)
        self.assertEqual(report["spelled_right"], 1)
        said = {c["said"] for c in report["caught"]}
        self.assertIn("skill x", said)
        self.assertIn("skillix", said)
        self.assertEqual(len(report["notes_to_fix"]), 2)
        self.assertTrue(all(c["examples"] for c in report["caught"]))
        for near in report["near"]:
            self.assertLess(near["score"], report["min_score"])

    def test_scan_of_an_unknown_term_is_refused(self):
        with self.assertRaises(dictedit.DictEditError):
            dictedit.scan("Nope", book=dictionary.load(self.book), vault=self.root, workers=1)


class FixPlanTest(Base):
    def test_items_documents_and_strangers(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE transcripts (item_id TEXT, md_path TEXT)")
        own, moved = self.root / "own.md", self.root / "copy.md"
        db.execute("INSERT INTO transcripts VALUES ('it1', ?)", (str(own),))
        own.write_text("---\nwax-item-id: it1\n---\nbody\n")
        moved.write_text("---\nwax-item-id: it1\n---\nbody\n")
        (self.root / "doc.md").write_text("---\nwax-item-id: doc-abc\n---\nbody\n")
        (self.root / "bare.md").write_text("body\n")
        (self.root / "ghost.md").write_text("---\nwax-item-id: gone\n---\nbody\n")
        names = ["own.md", "copy.md", "doc.md", "bare.md", "ghost.md"]
        with patch.object(ledger, "connect", return_value=db):
            plan = dictedit.fix_plan([str(self.root / n) for n in names])
        self.assertEqual([p["how"] for p in plan], ["item", "skip", "document", "document", "skip"])
        self.assertEqual(plan[0]["item_id"], "it1")


class CliTest(Base):
    def run_wax(self, *argv):
        env = dict(os.environ, WAX_DICTIONARY=str(self.book), WAX_ROOT=str(self.root),
                   WAX_VAULT=str(self.root / "vault"), WAX_DICT_COMMIT="0")
        (self.root / "vault").mkdir(exist_ok=True)
        return subprocess.run([sys.executable, str(WAX), *argv], capture_output=True, text=True, env=env,
                              timeout=120)

    def test_add_list_heard_remove_round_trip(self):
        out = self.run_wax("--json", "dict", "add", "Kapture", "--about", "the browser daemon")
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual(data["action"], "added")
        self.assertEqual(data["scan"]["notes_scanned"], 0)
        self.assertEqual(data["git"], {"committed": False, "why": "WAX_DICT_COMMIT=0"})
        refused = self.run_wax("dict", "heard", "Kapture", "capture")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("--force", refused.stderr)
        self.assertEqual(self.run_wax("dict", "heard", "Kapture", "kapchur", "--no-scan").returncode, 0)
        listed = json.loads(self.run_wax("--json", "dict", "list", "kap").stdout)
        self.assertEqual(listed, [{"term": "Kapture", "heard": ["kapchur"], "about": "the browser daemon",
                                   "section": "Added with wax dict"}])
        self.assertEqual(self.run_wax("dict", "remove", "Kapture").returncode, 0)
        self.assertNotIn("Kapture", self.terms())


if __name__ == "__main__":
    unittest.main()
