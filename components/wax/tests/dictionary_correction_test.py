"""dictionary-correction: the HeyMa custom dictionary, the candidate scan, and the pass.

The scan (wax.dictionary) is deterministic and tested directly. The pass is
tested with Jev replaced (FakeJev), against both an ordinary document and a
transcript, and once inside the real runner, where its body edit must land
through compare-and-replace and must not mark the transcript diarized.
"""

import hashlib
import importlib.util
import io
import json
import os
import shlex
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

import yaml

from passes_chain_test import ChainCase
from wax import dictionary, frontmatter, jev, ledger, passes, provider


COMPONENT_ROOT = next(parent for parent in Path(__file__).resolve().parents
                      if (parent / "pyproject.toml").is_file())
SCRIPT = COMPONENT_ROOT / "config" / "passes.d" / "bin" / "dictionary-correction"
REGISTRY = COMPONENT_ROOT / "config" / "passes.d" / "dictionary-correction.yaml"
SHIPPED = COMPONENT_ROOT.parents[1] / "Dictionary.yaml"
LOADER = SourceFileLoader("wax_dictionary_correction_pass", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
DC = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(DC)

BOOK = """\
terms:
  Skillex: {heard: [skillix, skill x], about: "the agent-skill registry"}
  DeLoNET: {heard: [d low net], about: "the home lab"}
  delo.sh: "the home lab domain"
  pjangler: [pee jangler, p jangler]
  Plane: "the ticket-board tool"
  Jarad: {heard: [jared], about: "the speaker's own first name"}
  GorillaDesk: "field-service software"
  Hindsight:
  AutomaticAI: {heard: [automatic a i], about: "the consulting company"}
  IntelliForia: [telephoria]
  Damian: [damien]
  GoHighLevel: "a CRM"
  VoxCPM: [vox cpm]
"""

TRANSCRIPT = ("---\nwax-item-id: 5eea3d5cd205bb84\ntitle: Skill X notes\n---\n\n"
              "# Transcription: skill-x-2026_11_04.mp3\n\n"
              "**Skill X (00:00):** So skill X needs a release, and skillix syncs to D low net.\n\n"
              "**Speaker 2 (00:12):** Then open delo dot sh. Delo net is up; the plane landed.\n")
DOCUMENT = ("---\ntitle: Release plan\ntags: [planning]\n---\n# Release plan\n\n"
            "Ship skill X first, then the p jangler board on D low net.\n")


def book(text=BOOK):
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    handle.write(text)
    handle.close()
    return Path(handle.name)


def scan(body, text=BOOK, **kw):
    path = book(text)
    try:
        return dictionary.candidates(body, dictionary.load(path), **kw)
    finally:
        path.unlink()


def pairs(found):
    return [(c.said, c.replacement, c.source) for c in found]


class DictionaryFileTest(unittest.TestCase):
    def test_every_entry_shape_parses(self):
        path = book()
        try:
            loaded = dictionary.load(path)
        finally:
            path.unlink()
        terms = {t.term: t for t in loaded.terms}
        self.assertEqual(terms["Skillex"].heard, ("skillix", "skill x"))
        self.assertEqual(terms["Skillex"].about, "the agent-skill registry")
        self.assertEqual(terms["delo.sh"].about, "the home lab domain")
        self.assertEqual(terms["pjangler"].heard, ("pee jangler",))  # "p jangler" is the term respaced
        self.assertEqual(terms["Hindsight"], dictionary.Term("Hindsight"))
        self.assertEqual(len(loaded.sha256), 64)

    def test_missing_is_an_error_not_an_empty_dictionary(self):
        with self.assertRaises(dictionary.DictionaryError) as caught:
            dictionary.load(Path("/nonexistent/Dictionary.yaml"))
        self.assertEqual(caught.exception.reason_code, "dictionary_missing")

    def test_malformed_entries_are_refused_loudly(self):
        for text in ("terms: [Skillex]\n", "terms:\n  Skillex: {heard: skillix}\n",
                     "terms:\n  Skillex: {heard: [skillix], about: a, b}\n",   # an unquoted comma
                     "terms:\n  Skillex:\n  skillex:\n",
                     "terms:\n  Skillex: [jangler]\n  pjangler: [jangler]\n",
                     "terms:\n  Skillex: [pjangler]\n  pjangler:\n",
                     "terms: {Skillex: [x\n"):
            with self.subTest(text=text):
                path = book(text)
                try:
                    with self.assertRaises(dictionary.DictionaryError) as caught:
                        dictionary.load(path)
                finally:
                    path.unlink()
                self.assertEqual(caught.exception.reason_code, "dictionary_invalid")

    def test_the_shipped_dictionary_parses(self):
        loaded = dictionary.load(SHIPPED)
        self.assertGreater(len(loaded.terms), 80)
        terms = {t.term for t in loaded.terms}
        self.assertTrue({"Skillex", "DeLoNET", "delo.sh", "HeyMa"} <= terms)

    def test_default_location_is_the_heyma_root_unless_overridden(self):
        with patch.dict(os.environ, {"WAX_DICTIONARY": ""}):
            self.assertEqual(dictionary.default_path().name, "Dictionary.yaml")
        with patch.dict(os.environ, {"WAX_DICTIONARY": "/tmp/elsewhere.yaml"}):
            self.assertEqual(dictionary.default_path(), Path("/tmp/elsewhere.yaml"))


class CandidateScanTest(unittest.TestCase):
    def test_listed_mishearings_respellings_and_sound_alikes(self):
        found = pairs(scan("Push skill X, then skillix, then D low net. Delo net and Skilex too. "
                           "Gorilla desk and Gorila Desk."))
        self.assertIn(("skill X", "Skillex", "heard"), found)
        self.assertIn(("skillix", "Skillex", "heard"), found)
        self.assertIn(("D low net", "DeLoNET", "heard"), found)
        self.assertIn(("Delo net", "DeLoNET", "exact"), found)
        self.assertIn(("Skilex", "Skillex", "fuzzy"), found)
        self.assertIn(("Gorilla desk", "GorillaDesk", "exact"), found)
        self.assertIn(("Gorila Desk", "GorillaDesk", "fuzzy"), found)

    def test_correct_spellings_sentence_case_and_spelling_aloud_are_left_alone(self):
        self.assertEqual(scan("Skillex is fine. DeLoNET too. Pjangler ships. Spell it J-A-R-A-D."), [])

    def test_a_case_only_change_to_an_english_word_is_its_own_source(self):
        self.assertIn(("plane", "Plane", "case"), pairs(scan("the plane landed")))
        self.assertIn(("hindsight", "Hindsight", "case"), pairs(scan("in hindsight")))

    def test_what_was_not_said_is_never_a_candidate(self):
        body = ("# Transcription: skill-x.mp3\n\n**Skill X (00:00):** hi\n\n"
                "`skill x` and ```\nskillix\n``` and [[skill x]] and [a](https://skill.x/skillix) "
                "and https://example.com/skill-x\n")
        self.assertEqual(scan(body), [])

    def test_edge_words_never_ride_along(self):
        # "Skillex is" sounds like Skillex; the exact term inside wins, so "is" survives.
        self.assertEqual(scan("Skillex is up"), [])
        found = scan("skill X is up")
        self.assertEqual(pairs(found), [("skill X", "Skillex", "heard")])

    def test_runs_stop_at_punctuation_and_lines(self):
        self.assertEqual(pairs(scan("a skill. X marks it")), [])
        self.assertEqual(pairs(scan("skill\nX")), [])

    def test_possessives_and_plurals_keep_their_ending(self):
        body = "skill X's registry and two gorilla desks"
        chosen = dictionary.resolve((c, 0.9) for c in scan(body))
        self.assertEqual(dictionary.apply(body, chosen), "Skillex's registry and two GorillaDesks")

    def test_spoken_dot_becomes_a_domain_or_file_name(self):
        body = "open delo dot sh, then drive dot dlo dot sh. Edit settings dot json and a dot env file."
        chosen = dictionary.resolve((c, 0.9) for c in scan(body))
        self.assertEqual(dictionary.apply(body, chosen),
                         "open delo.sh, then drive.delo.sh. Edit settings.json and a .env file.")

    def test_spoken_dot_is_off_when_switched_off_and_a_real_dot_is_a_word(self):
        self.assertEqual(scan("delo dot sh", spoken_dot=False), [])
        self.assertEqual(scan("move the dot to the left"), [])

    def test_a_domain_term_needs_its_suffix_heard(self):
        self.assertEqual([c.replacement for c in scan("de los and delos")], [])
        self.assertIn(("dlo sh", "delo.sh", "fuzzy"), pairs(scan("go to dlo sh now")))

    def test_ordinary_english_does_not_sound_like_a_term(self):
        self.assertEqual(scan("skills and skill set and him a hey"), [])

    def test_a_run_never_swallows_a_function_word_it_was_not_listed_with(self):
        # Labelled-sample failures (2026-11-04): each ate a real word beside the name.
        self.assertEqual(pairs(scan("we'll see Dami and Ava")), [])
        self.assertEqual(pairs(scan("now we go to automatic and i've got it")), [])
        self.assertEqual(pairs(scan("reset today in Telephoria")), [("Telephoria", "IntelliForia", "heard")])
        # A listed form keeps its edge word: "automatic a i" is how it was heard.
        self.assertEqual(pairs(scan("the automatic a I org")), [("automatic a I", "AutomaticAI", "exact")])

    def test_a_trailing_number_is_another_word_not_the_term(self):
        self.assertEqual(pairs(scan("selling agents through GoHighLevel2 plumbers, fed to Vox CPM2")), [])

    def test_a_fragment_inside_a_listed_reading_gives_way_to_it(self):
        found = pairs(scan("the automatic a I neighborhood"))
        self.assertEqual(found, [("automatic a I", "AutomaticAI", "exact")])

    def test_an_address_spelled_letter_by_letter_is_left_alone(self):
        self.assertEqual(scan("a site at C-A-L dot D-E-L-O dot SH"), [])
        self.assertEqual([c.source for c in scan("use D E L o dot sh")], [])

    def test_a_sound_alike_never_reads_across_a_spoken_dot(self):
        self.assertEqual(pairs(scan("open delo dot sh")), [("delo dot sh", "delo.sh", "dot")])

    def test_resolve_keeps_the_most_probable_of_overlapping_readings(self):
        a = dictionary.Candidate(0, 9, "skill X's", "Skillex's", None, "fuzzy", 0.9)
        b = dictionary.Candidate(0, 7, "skill X", "Skillex", None, "heard", 1.0)
        c = dictionary.Candidate(10, 12, "is", "IS", None, "fuzzy", 0.9)
        self.assertEqual(dictionary.resolve([(a, 0.7), (b, 0.8), (c, 0.65)]), [b, c])
        with self.assertRaises(ValueError):
            dictionary.apply("skill X's is", [a, b])

    def test_excerpt_blanks_what_was_not_said(self):
        body = "# Transcription: x.mp3\n\n**Speaker 1 (00:00):** we ship skill X today\n"
        found = scan(body)
        self.assertEqual(dictionary.excerpt(body, found[0].start, found[0].end), "we ship skill X today")


class FakeJev:
    """Stands in for jev.decide: answers each question by (heard text -> p), records every call."""

    def __init__(self, answers=None, default=0.9, error=None):
        self.answers = answers or {}
        self.default = default
        self.error = error
        self.calls = []

    def __call__(self, state, questions, *, key, model, url, **_kw):
        self.calls.append({"state": state, "questions": questions, "key": key, "model": model, "url": url})
        if self.error:
            raise self.error
        out = {}
        for gid, q in questions.items():
            said = q["instructions"].split('Does "', 1)[1].split('"', 1)[0]
            out[gid] = {"type": "noul", "noul": self.answers.get(said, self.default)}
        return jev.Decision(answers=out, model="jev-1.13.0", cost=None, request_id=f"req-{len(self.calls)}")


class PassCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dictionary = self.root / "Dictionary.yaml"
        self.dictionary.write_text(BOOK)
        self.env = patch.dict(os.environ, {"WAX_DICTIONARY": str(self.dictionary), "WAX_JEV_API_KEY": "vk-test"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def note(self, text, name="note.md"):
        path = self.root / name
        path.write_bytes(text.encode())
        return path

    def run_pass(self, md, fake):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(DC.jev, "decide", fake), redirect_stdout(out), redirect_stderr(err):
            code = DC.main([str(md), "item"])
        return code, out.getvalue(), err.getvalue()


class PassTest(PassCase):
    def test_transcript_is_corrected_around_labels_and_header(self):
        md = self.note(TRANSCRIPT)
        before = md.read_bytes()
        fake = FakeJev({"plane": 0.35})  # rejected, but likely enough to be shown in evidence
        code, out, err = self.run_pass(md, fake)
        self.assertEqual(code, 0, err)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(md.read_bytes(), before, "the pass must never write the note")
        self.assertEqual(result["frontmatter"], {})
        _, body = frontmatter.split(TRANSCRIPT)
        self.assertEqual(result["body_replace"]["sha256"], hashlib.sha256(body.encode()).hexdigest())
        text = result["body_replace"]["text"]
        self.assertIn("# Transcription: skill-x-2026_11_04.mp3", text)
        self.assertIn("**Skill X (00:00):** So Skillex needs a release, and Skillex syncs to DeLoNET.", text)
        self.assertIn("Then open delo.sh. DeLoNET is up; the plane landed.", text)
        self.assertEqual(text.replace("Skillex", "skill X", 1).count("\n"), body.count("\n"))
        rows = {(r["from"], r["to"]): r for r in result["evidence"]["corrections"]}
        self.assertFalse(rows[("plane", "Plane")]["accepted"])
        self.assertEqual(rows[("skill X", "Skillex")]["applied"], 1)
        self.assertEqual(result["evidence"]["model"], "jev-1.13.0")
        self.assertEqual(result["evidence"]["request_ids"], ["req-1"])
        # Jev sees the vocabulary it is asked about and an excerpt per question, never labels or the header.
        state = fake.calls[0]["state"]
        self.assertEqual(state["speaker_vocabulary"]["Skillex"], "the agent-skill registry")
        self.assertTrue(all("Transcription:" not in e and "**" not in e for e in state["excerpts"].values()))
        self.assertEqual(fake.calls[0]["key"], "vk-test")

    def test_ordinary_document_uses_the_same_core(self):
        md = self.note(DOCUMENT)
        code, out, err = self.run_pass(md, FakeJev())
        self.assertEqual(code, 0, err)
        text = json.loads(out.strip().splitlines()[-1])["body_replace"]["text"]
        self.assertEqual(text, "# Release plan\n\nShip Skillex first, then the pjangler board on DeLoNET.\n")

    def test_thresholds_depend_on_how_the_candidate_was_found(self):
        md = self.note("---\na: 1\n---\nskill X and Skilex and the plane\n")
        # heard (0.4) accepts 0.45; fuzzy (0.5) rejects 0.45; case (0.65) rejects 0.6.
        code, out, err = self.run_pass(md, FakeJev({"skill X": 0.45, "Skilex": 0.45, "plane": 0.6}))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["body_replace"]["text"],
                         "Skillex and Skilex and the plane\n")
        with patch.dict(os.environ, {"WAX_DICTIONARY_MIN_P": "0.4", "WAX_DICTIONARY_MIN_P_CASE": "0.55"}):
            code, out, err = self.run_pass(md, FakeJev({"skill X": 0.45, "Skilex": 0.45, "plane": 0.6}))
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["body_replace"]["text"],
                         "Skillex and Skillex and the Plane\n")

    def test_nothing_to_correct_asks_jev_nothing(self):
        md = self.note("---\na: 1\n---\nSkillex runs on DeLoNET.\n")
        fake = FakeJev()
        code, out, err = self.run_pass(md, fake)
        self.assertEqual(code, 0, err)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertNotIn("body_replace", result)
        self.assertEqual(result["evidence"]["corrections"], [])
        self.assertEqual(fake.calls, [])

    def test_everything_rejected_means_no_body_change(self):
        md = self.note("skill X\n")
        code, out, _ = self.run_pass(md, FakeJev(default=0.1))
        self.assertEqual(code, 0)
        self.assertNotIn("body_replace", json.loads(out.strip().splitlines()[-1]))

    def test_an_empty_note_is_skipped_not_failed(self):
        for text in ("---\na: 1\n---\n", "---\na: 1\n---\n# Transcription: x.mp3\n\n"):
            with self.subTest(text=text):
                code, out, _ = self.run_pass(self.note(text), FakeJev())
                self.assertEqual(code, 0)
                result = json.loads(out.strip().splitlines()[-1])
                self.assertEqual((result["state"], result["reason_code"]), ("skipped", "empty_document"))

    def test_a_missing_or_broken_dictionary_fails_with_a_reason(self):
        for text, reason in ((None, "dictionary_missing"), ("terms: [x]\n", "dictionary_invalid")):
            with self.subTest(reason=reason):
                if text is None:
                    self.dictionary.unlink(missing_ok=True)
                else:
                    self.dictionary.write_text(text)
                code, out, err = self.run_pass(self.note("skill X\n"), FakeJev())
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(err.splitlines()[0], f"reason_code={reason}")

    def test_jev_failure_fails_the_pass_with_its_reason(self):
        error = provider.ProviderError("HTTP 503", "provider_http_error", status=503)
        code, out, err = self.run_pass(self.note("skill X\n"), FakeJev(error=error))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err.splitlines()[0], "reason_code=provider_http_error")

    def test_non_utf8_input_fails_cleanly(self):
        md = self.root / "bad.md"
        md.write_bytes(b"\xff\xfe skill X")
        code, out, err = self.run_pass(md, FakeJev())
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines()[0], "reason_code=run_error")

    def test_rerun_on_corrected_text_is_a_no_op(self):
        md = self.note(TRANSCRIPT)
        _, out, _ = self.run_pass(md, FakeJev({"plane": 0.1}))
        first = json.loads(out.strip().splitlines()[-1])
        fm, _ = frontmatter.split(TRANSCRIPT)
        md.write_text(frontmatter.render(fm, first["body_replace"]["text"]))
        fake = FakeJev({"plane": 0.1})
        _, out, _ = self.run_pass(md, fake)
        again = json.loads(out.strip().splitlines()[-1])
        self.assertNotIn("body_replace", again)

    def test_batches_stay_within_the_question_limit(self):
        words = " ".join(f"skillix{i}" for i in range(3))  # not candidates; padding only
        lines = "\n".join(f"line {i}: Skilex{chr(97 + i % 26)} {words}" for i in range(DC.BATCH + 5))
        md = self.note("skill X\n" + "\n".join(f"Gorila Desk{n}" for n in range(1)) + "\n" + lines)
        fake = FakeJev()
        with patch.object(DC, "BATCH", 2):
            code, _, err = self.run_pass(md, fake)
        self.assertEqual(code, 0, err)
        self.assertTrue(all(len(call["questions"]) <= 2 for call in fake.calls))
        self.assertGreater(len(fake.calls), 1)


class RegistryTest(unittest.TestCase):
    def test_entry_matches_the_design(self):
        entry = yaml.safe_load(REGISTRY.read_text())
        self.assertEqual((entry["slug"], entry["version"]), ("dictionary-correction", 1))
        self.assertEqual(entry["capabilities"], ["audio", "document"])
        self.assertEqual(entry["body_mutation"], "compare-and-replace")
        self.assertEqual(entry["after"], ["diarization"])
        self.assertEqual(entry["requires"], [])
        self.assertEqual(entry["clobber"], [])
        self.assertTrue(entry["env"]["WAX_JEV_API_KEY_OP"].startswith("op://DeLoSecrets/"))
        self.assertEqual(entry["env"]["WAX_JEV_MODEL"], "jev-1.13.0")
        self.assertEqual(entry["env"]["WAX_JEV_API_KEY_OP"], DC.DEFAULT_KEY_OP)
        self.assertNotIn("WAX_DICTIONARY", entry["env"], "a frozen path would pin the dictionary at compile")


STUB = textwrap.dedent('''\
    #!/usr/bin/python3
    """The real dictionary-correction pass, Jev answered locally, in the runner's subprocess."""
    import os, sys
    from importlib.machinery import SourceFileLoader
    from unittest.mock import patch
    module = SourceFileLoader("dc_under_test", {script!r}).load_module()

    def decide(state, questions, **_kw):
        return module.jev.Decision(answers={{g: {{"type": "noul", "noul": 0.9}} for g in questions}},
                                   model="jev-1.13.0", cost=None, request_id="r1")

    os.environ["WAX_DICTIONARY"] = {dictionary!r}
    os.environ["WAX_JEV_API_KEY"] = "vk-test"
    with patch.object(module.jev, "decide", side_effect=decide):
        raise SystemExit(module.main(sys.argv[1:]))
''')


class RunnerTest(ChainCase):
    def setUp(self):
        super().setUp()
        self.body = "\n# Transcription: x.mp3\n\nWe ship skill X on D low net today.\n"
        self.md.write_text(frontmatter.render({"wax-item-id": self.item, "title": "Human title"}, self.body))
        dictionary_file = self.root / "Dictionary.yaml"
        dictionary_file.write_text(BOOK)
        stub = self.registry_dir / "dictionary-correction-stub"
        stub.write_text(STUB.format(script=str(SCRIPT), dictionary=str(dictionary_file)))
        stub.chmod(0o755)
        entry = yaml.safe_load(REGISTRY.read_text())
        entry["command"] = [str(stub), "{md_path}", "{item_id}"]
        entry["after"] = []
        (self.registry_dir / "dictionary-correction.yaml").write_text(yaml.safe_dump(entry))

    def test_the_runner_applies_the_correction_and_keeps_the_frontmatter(self):
        outcome = passes.run(self.item, "dictionary-correction")
        self.assertEqual(outcome["state"], "completed", outcome)
        fm, body = frontmatter.read(self.md)
        self.assertEqual(body, "\n# Transcription: x.mp3\n\nWe ship Skillex on DeLoNET today.\n")
        self.assertEqual(fm["title"], "Human title")
        self.assertEqual(fm["wax"]["passes"]["dictionary-correction"]["state"], "completed")
        self.assertEqual(fm["wax"]["passes"]["dictionary-correction"]["model"], "jev-1.13.0")

    def test_a_body_edit_by_a_non_diarization_pass_does_not_mark_the_transcript_diarized(self):
        passes.run(self.item, "dictionary-correction")
        row = ledger.connect().execute("SELECT diarized FROM transcripts WHERE item_id=?", (self.item,)).fetchone()
        self.assertFalse(row["diarized"])

    def test_a_diarization_body_edit_still_does(self):
        self.add_pass("speakers", {"body_replace": {"sha256": hashlib.sha256(self.body.encode()).hexdigest(),
                                                    "text": "\n**Speaker 1 (00:00):** hi\n"}},
                      kind="diarization", body_mutation="compare-and-replace")
        self.assertEqual(passes.run(self.item, "speakers")["state"], "completed")
        row = ledger.connect().execute("SELECT diarized FROM transcripts WHERE item_id=?", (self.item,)).fetchone()
        self.assertEqual(row["diarized"], 1)


if __name__ == "__main__":
    unittest.main()
