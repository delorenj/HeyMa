"""Classification v2: telnyx and floor rules, then one Jev request with two questions.

jev.decide is mocked everywhere; nothing here reaches the ingress.
"""
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

import yaml

from wax import frontmatter, jev, passes, provider

COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
SCRIPT = COMPONENT_ROOT / "config" / "passes.d" / "bin" / "classification"
LOADER = SourceFileLoader("wax_classification_pass_v2", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
C = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(C)

MODEL = "jev-1.13.0"
BODY = "# Transcription: a.mp3\n\n**Speaker 1 (00:00):** Let us plan the release.\n\n**Speaker 2 (00:09):** Sure.\n"


def note(fm: str, body: str = BODY) -> str:
    return f"---\n{fm}---\n{body}"


class FakeJev:
    """Stands in for jev.decide: one scripted answer, every call recorded."""

    def __init__(self, kind="monolog", p=0.95, is_phone=0.02, others=None):
        probabilities = {k: 0.0 for k in C.KINDS}
        probabilities.update(others or {})
        probabilities[kind] = p
        self.answer = {"kind": {"type": "choice", "choice": kind, "probabilities": probabilities},
                       "is_phone": {"type": "noul", "noul": is_phone}}
        self.calls = []

    def __call__(self, state, questions, *, key, model, url, **_kwargs):
        self.calls.append({"state": state, "questions": questions, "key": key, "model": model, "url": url})
        return jev.Decision(answers=self.answer, model=MODEL, cost=None, request_id="req-1", input_tokens=120)


class ClassificationV2Test(unittest.TestCase):
    def setUp(self):
        provider._NOTES.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ, {"WAX_JEV_MODEL": "", "WAX_JEV_URL": "", "WAX_CLASSIFICATION_MIN_P": "0.6"})
        env.start()
        self.addCleanup(env.stop)

    def md(self, text: str, name: str = "n.md") -> Path:
        path = self.root / name
        path.write_text(text)
        return path

    def run_build(self, md: Path, fake: FakeJev, **kwargs):
        with patch.object(C.provider, "resolve_key", return_value="test-key"), \
                patch.object(C.jev, "decide", side_effect=fake):
            return C.build_result(md, **kwargs)

    # ---- rules that never call Jev ------------------------------------------------------------
    def test_telnyx_is_a_phone_call_with_no_model_call(self):
        for name in ("telnyx-2026-10-16-6eb089ba.mp3", "/home/u/HeyMa/inbox/telnyx-x.mp3"):
            with self.subTest(name=name):
                fake = FakeJev()
                result = self.run_build(self.md(note(f"source-audio: {name}\naudio-duration-seconds: 3.1\n",
                                                     "# Transcription: x\n\nHello there.\n")), fake)
                self.assertEqual(result["frontmatter"], {
                    "classification": "phone_call", "classification-confidence": 1.0,
                    "classification-source": "rule:telnyx"})
                self.assertEqual(fake.calls, [])

    def test_telnyx_is_read_from_the_wax_block_when_the_top_level_key_is_gone(self):
        fake = FakeJev()
        result = self.run_build(self.md(note("wax:\n  source_audio: telnyx-abc.mp3\n")), fake)
        self.assertEqual(result["frontmatter"]["classification-source"], "rule:telnyx")
        self.assertEqual(fake.calls, [])

    def test_floor_rule_is_other_no_content_and_the_telnyx_rule_outranks_it(self):
        fake = FakeJev()
        short = "# Transcription: a.mp3\n\nuh huh yeah okay.\n"
        result = self.run_build(self.md(note("source-audio: /i/a.mp3\naudio-duration-seconds: 4.2\n", short)), fake)
        self.assertEqual(result["frontmatter"], {
            "classification": "other", "classification-detail": "no_content",
            "classification-confidence": 1.0, "classification-source": "rule:floor"})
        self.assertEqual(fake.calls, [])
        # Not under the floor: 15 words, or 10 seconds, or no duration at all -> Jev decides.
        for fm in ("source-audio: /i/a.mp3\naudio-duration-seconds: 10.0\n",
                   "source-audio: /i/a.mp3\n"):
            with self.subTest(fm=fm):
                fake = FakeJev("monolog")
                self.run_build(self.md(note(fm, short)), fake)
                self.assertEqual(len(fake.calls), 1)
        fifteen = "# Transcription: a.mp3\n\n" + " ".join(["word"] * 15) + "\n"
        fake = FakeJev("monolog")
        self.run_build(self.md(note("audio-duration-seconds: 3\n", fifteen)), fake)
        self.assertEqual(len(fake.calls), 1)

    def test_empty_body_is_a_skip_before_any_rule(self):
        fake = FakeJev()
        result = self.run_build(self.md(note("source-audio: telnyx-x.mp3\n", "\n  \n")), fake)
        self.assertEqual(result, {"wax_ep_version": 1, "state": "skipped", "reason_code": "empty_document"})
        self.assertEqual(fake.calls, [])

    # ---- the Jev decision ---------------------------------------------------------------------
    def test_accepted_kinds_map_to_the_four_stored_classes(self):
        cases = {
            "monolog": ("monolog", None), "meeting": ("meeting", None), "phone_call": ("phone_call", None),
            "household": ("other", "household"), "no_content": ("other", "no_content"),
        }
        for kind, (stored, detail) in cases.items():
            with self.subTest(kind=kind):
                fake = FakeJev(kind, p=0.83, is_phone=0.95 if kind == "phone_call" else 0.04)
                result = self.run_build(self.md(note("audio-duration-seconds: 120\n")), fake)
                expected = {"classification": stored, "classification-confidence": 0.83,
                            "classification-source": MODEL}
                if detail:
                    expected["classification-detail"] = detail
                self.assertEqual(result["frontmatter"], expected)
                self.assertIn(stored, frontmatter.CLASSIFICATIONS)
                self.assertEqual(result["evidence"]["outcome"], "accepted")
                self.assertEqual(result["evidence"]["request_ids"], ["req-1"])

    def test_one_request_two_questions_and_a_state_with_recording_metadata(self):
        fake = FakeJev("meeting")
        self.run_build(self.md(note("source-audio: /i/2025_06_17.mp3\naudio-duration-seconds: 371.409\n")), fake)
        call = fake.calls[0]
        self.assertEqual(list(call["questions"]), ["kind", "is_phone"])
        self.assertEqual(call["questions"]["kind"]["type"], "choice")
        self.assertEqual(set(call["questions"]["kind"]["criteria"]), set(C.KINDS))
        self.assertEqual(call["questions"]["is_phone"]["type"], "noul")
        self.assertEqual((call["model"], call["url"], call["key"]), (jev.DEFAULT_MODEL, jev.DEFAULT_URL, "test-key"))
        self.assertEqual(call["state"]["recording"], {"source": "2025_06_17.mp3", "speaker_count": 2,
                                                      "duration_s": 371.4, "word_count": 12})
        self.assertNotIn("# Transcription:", call["state"]["transcript"])
        self.assertTrue(call["state"]["transcript"].startswith("**Speaker 1"))

    def test_long_transcripts_are_windowed_head_and_tail(self):
        body = "# Transcription: a.mp3\n\n" + ("x" * 6000) + ("m" * 20_000) + ("z" * 2000)
        fake = FakeJev("meeting")
        self.run_build(self.md(note("", body)), fake)
        sent = fake.calls[0]["state"]["transcript"]
        self.assertEqual(len(sent), C.HEAD_CHARS + 1 + C.TAIL_CHARS)
        self.assertTrue(sent.startswith("x" * 100) and sent.endswith("z" * 100) and "…" in sent)

    def test_threshold_boundary_and_env_override(self):
        accepted = self.run_build(self.md(note("")), FakeJev("meeting", p=0.6))
        self.assertEqual(accepted["frontmatter"]["classification"], "meeting")
        abstained = self.run_build(self.md(note("")), FakeJev("meeting", p=0.59, others={"monolog": 0.4}))
        self.assertNotIn("classification", abstained["frontmatter"])
        with patch.dict(os.environ, {"WAX_CLASSIFICATION_MIN_P": "0.9"}):
            result = self.run_build(self.md(note("")), FakeJev("meeting", p=0.85))
        self.assertNotIn("classification", result["frontmatter"])

    def test_abstain_writes_no_class_and_records_confidence_and_candidates(self):
        fake = FakeJev("meeting", p=0.45, is_phone=0.1, others={"monolog": 0.4, "household": 0.15})
        result = self.run_build(self.md(note("")), fake)
        self.assertEqual(result["frontmatter"], {"classification-detail": "unsorted",
                                                 "classification-confidence": 0.45,
                                                 "classification-source": MODEL})
        self.assertEqual(result["evidence"]["candidates"], ["meeting", "monolog"])
        self.assertEqual(result["evidence"]["outcome"], "abstain:low_confidence")

    def test_a_document_abstain_is_a_stray_other_so_doc_standard_still_titles_it(self):
        fake = FakeJev("meeting", p=0.4, others={"monolog": 0.35})
        with patch.object(C.provider, "resolve_key", return_value="k"), patch.object(C.jev, "decide", side_effect=fake):
            result = C.build_result(self.md(note("")), document=True)
        self.assertEqual(result["frontmatter"], {"classification": "other", "classification-detail": "unsorted",
                                                 "classification-confidence": 0.4,
                                                 "classification-source": MODEL})

    def test_questions_that_disagree_abstain(self):
        # A confident non-phone kind while is_phone says phone, and the reverse.
        for kind, is_phone in (("meeting", 0.91), ("monolog", 0.99), ("phone_call", 0.49), ("phone_call", 0.0)):
            with self.subTest(kind=kind, is_phone=is_phone):
                result = self.run_build(self.md(note("")), FakeJev(kind, p=0.97, is_phone=is_phone))
                self.assertNotIn("classification", result["frontmatter"])
                self.assertEqual(result["frontmatter"]["classification-detail"], "unsorted")
                self.assertEqual(result["evidence"]["outcome"], "abstain:kind_disagrees_with_is_phone")
        # The edges of the band agree.
        for kind, is_phone in (("meeting", 0.9), ("phone_call", 0.5)):
            with self.subTest(kind=kind, is_phone=is_phone):
                result = self.run_build(self.md(note("")), FakeJev(kind, p=0.97, is_phone=is_phone))
                self.assertEqual(result["frontmatter"]["classification"], kind)

    def test_a_stale_detail_is_cleared_when_the_new_verdict_has_none(self):
        md = self.md(note("classification: other\nclassification-detail: household\n"))
        result = self.run_build(md, FakeJev("meeting", p=0.9), reclassify=True)
        self.assertEqual(result["frontmatter"]["classification"], "meeting")
        self.assertEqual(result["frontmatter"]["classification-detail"], "")

    # ---- existing values and --reclassify ----------------------------------------------------------
    def test_an_existing_value_is_kept_without_a_call(self):
        fake = FakeJev("meeting")
        md = self.md(note("classification: monolog\n"))
        self.assertEqual(self.run_build(md, fake),
                         {"wax_ep_version": 1, "frontmatter": {"classification": "monolog"}})
        self.assertEqual(fake.calls, [])

    def test_an_unsupported_existing_value_fails_and_is_preserved(self):
        md = self.md(note("classification: voicemail\n"))
        with self.assertRaises(provider.ProviderError) as caught:
            self.run_build(md, FakeJev(), reclassify=True)
        self.assertEqual(caught.exception.reason_code, "invalid_classification")

    def test_reclassify_overwrites_unmarked_v1_and_own_markers_but_keeps_human_ones(self):
        for fm in ("classification: monolog\n",
                   "classification: monolog\nclassification-source: jev-1.12.0\n",
                   "classification: monolog\nclassification-source: rule:floor\n"):
            with self.subTest(fm=fm):
                fake = FakeJev("meeting")
                result = self.run_build(self.md(note(fm)), fake, reclassify=True)
                self.assertEqual(result["frontmatter"]["classification"], "meeting")
                self.assertEqual(len(fake.calls), 1)
        human = self.md(note("classification: monolog\nclassification-source: human\n"))
        fake = FakeJev("meeting")
        self.assertEqual(self.run_build(human, fake, reclassify=True)["frontmatter"],
                         {"classification": "monolog"})
        self.assertEqual(fake.calls, [])
        forced = self.run_build(human, FakeJev("meeting"), reclassify=True, force_human=True)
        self.assertEqual(forced["frontmatter"]["classification"], "meeting")

    def test_reclassify_abstain_keeps_the_old_value_and_says_so(self):
        md = self.md(note("classification: monolog\n"))
        result = self.run_build(md, FakeJev("meeting", p=0.4, others={"monolog": 0.35}), reclassify=True)
        self.assertEqual(result["frontmatter"], {"classification": "monolog"})
        self.assertEqual(result["evidence"]["kept"], "monolog")
        self.assertTrue(result["evidence"]["outcome"].startswith("abstain"))

    def test_reclassify_applies_the_telnyx_rule_over_a_v1_value(self):
        md = self.md(note("classification: meeting\nsource-audio: telnyx-x.mp3\n"))
        fake = FakeJev()
        result = self.run_build(md, fake, reclassify=True)
        self.assertEqual(result["frontmatter"]["classification"], "phone_call")
        self.assertEqual(fake.calls, [])

    # ---- failures and the executable -----------------------------------------------------------------
    def run_main(self, md: Path, argv=(), outcome=None, env=None):
        def decide(*_a, **_k):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(C.provider, "resolve_key", return_value="k"), \
                patch.object(C.jev, "decide", side_effect=decide), \
                patch.dict(os.environ, env or {}), redirect_stdout(stdout), redirect_stderr(stderr):
            code = C.main([*argv, str(md), "item-1"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_provider_failures_fail_the_pass_with_their_reason_code(self):
        for reason in ("provider_http_error", "provider_bad_response", "timeout", "provider_auth_rejected"):
            with self.subTest(reason=reason):
                code, stdout, stderr = self.run_main(self.md(note("")),
                                                     outcome=provider.ProviderError("upstream said no", reason))
                self.assertEqual((code, stdout), (1, ""))
                self.assertEqual(stderr.splitlines()[0], f"reason_code={reason}")
                self.assertNotIn("plan the release", stderr)

    def test_a_missing_key_is_no_api_key(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(C.provider, "resolve_key",
                          side_effect=provider.ProviderError("op did not resolve", "no_api_key")), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(C.main([str(self.md(note(""))), "item-1"]), 1)
        self.assertEqual(stderr.getvalue().splitlines()[0], "reason_code=no_api_key")

    def test_the_cli_flag_and_the_env_switch_both_reclassify(self):
        answer = FakeJev("meeting").answer
        decision = jev.Decision(answers=answer, model=MODEL, cost=None, request_id="r", input_tokens=1)
        md = self.md(note("classification: monolog\n"))
        code, stdout, _ = self.run_main(md, outcome=decision)
        self.assertEqual(passes._parse_result(stdout)["frontmatter"], {"classification": "monolog"})
        code, stdout, _ = self.run_main(md, argv=("--reclassify",), outcome=decision)
        self.assertEqual(passes._parse_result(stdout)["frontmatter"]["classification"], "meeting")
        code, stdout, _ = self.run_main(md, outcome=decision, env={"WAX_CLASSIFICATION_RECLASSIFY": "1"})
        self.assertEqual(passes._parse_result(stdout)["frontmatter"]["classification"], "meeting")
        self.assertEqual(md.read_text(), note("classification: monolog\n"), "the script never writes a note")

    def test_executable_runs_under_the_pass_interpreter_without_a_key(self):
        env = {**os.environ, "WAX_JEV_API_KEY": ""}
        skip = subprocess.run([str(SCRIPT), str(self.md(note("title: t\n", "\n"), "e.md"))],
                              capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(skip.returncode, 0, skip.stderr)
        self.assertEqual(json.loads(skip.stdout), {"wax_ep_version": 1, "state": "skipped",
                                                   "reason_code": "empty_document"})
        telnyx = subprocess.run([str(SCRIPT), str(self.md(note("source-audio: telnyx-q.mp3\n"), "t.md"))],
                                capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(telnyx.returncode, 0, telnyx.stderr)
        self.assertEqual(json.loads(telnyx.stdout)["frontmatter"]["classification"], "phone_call")

    def test_usage_without_a_document(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(C.main([]), 2)
        self.assertIn("usage:", stderr.getvalue())


class ClassificationRegistryTest(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load((COMPONENT_ROOT / "config" / "passes.d" / "classification.yaml").read_text())

    def test_v2_contract(self):
        self.assertEqual(self.doc["version"], 2)
        self.assertEqual(self.doc["timeout_s"], 60)
        self.assertEqual(set(self.doc["clobber"]), {
            "classification", "classification-detail", "classification-confidence", "classification-source"})
        env = self.doc["env"]
        self.assertEqual(env["WAX_JEV_URL"], jev.DEFAULT_URL)
        self.assertEqual(env["WAX_JEV_MODEL"], jev.DEFAULT_MODEL)
        self.assertEqual(env["WAX_JEV_API_KEY_OP"], "op://DeLoSecrets/lmtarsmlr35gqyydjsw245mvra/credential")
        self.assertEqual(C.DEFAULT_KEY_OP, env["WAX_JEV_API_KEY_OP"])
        self.assertFalse([k for k in env if k.startswith("WAX_TITLE_")], "classification no longer uses the chat chain")

    def test_it_has_its_own_key_not_project_extractions(self):
        other = yaml.safe_load((COMPONENT_ROOT / "config" / "passes.d" / "project-extraction.yaml").read_text())
        self.assertNotEqual(self.doc["env"]["WAX_JEV_API_KEY_OP"], other["env"]["WAX_JEV_API_KEY_OP"])

    def test_the_reclassify_clobber_does_not_overwrite_through_the_runner_without_the_flag(self):
        # The script, not the runner, decides what is overwritten: without --reclassify an existing
        # value comes back unchanged, so a clobber-capable key can never regress one on a plain re-run.
        with tempfile.TemporaryDirectory() as directory:
            md = Path(directory) / "n.md"
            md.write_text(note("classification: meeting\nclassification-source: human\n"))
            self.assertEqual(C.build_result(md), {"wax_ep_version": 1, "frontmatter": {"classification": "meeting"}})


if __name__ == "__main__":
    unittest.main()
