"""summarize: a grounded `## Summary` section and `summary` field for a document.

The pass itself is exercised with a scripted chat (the ticketable_test style).
The body-replace contract is proven end to end: a real document run through
the real runner, over the shipped untitled-standard graph compiled into a
SCRATCH ledger, with the pass subprocess's model call faked.
"""

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

import yaml

from documents_test import _close_test_connection, _fake_frontmatters
from wax import events, frontmatter, ledger, passes, paths, pipelines, provider


COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
PASSES_DIR = COMPONENT_ROOT / "config" / "passes.d"
PIPELINES_DIR = COMPONENT_ROOT / "config" / "pipelines.d"
SCRIPT = PASSES_DIR / "bin" / "summarize"
LOADER = SourceFileLoader("wax_summarize_pass", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
SUM = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(SUM)

CHAIN = ["automaticai/auto/fast", "automaticai/personal/glm-5.3-flash", "automaticai/personal/glm-5.3",
         "automaticai/hypercharm/glm-5.3", "automaticai/hypercharm/minimax-m3", "automaticai/free"]
BODY = "Met with the vendor about the new rack. They quoted two weeks.\n\nDecision: order the 42U rack.\n"
NOTE = f"---\ntags: [infra]\n---\n{BODY}"
ANSWER = {"summary": "A vendor quoted two weeks for a rack and the 42U model was chosen.",
          "bullets": ["Vendor quoted two weeks", "Decision: order the 42U rack"]}


class FakeChat:
    """Stands in for provider.chat_json: one scripted answer per call, every call recorded."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if self.outcomes else ANSWER
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def rate_limited():
    return provider.ProviderError("HTTP 429", "provider_rate_limited", status=429)


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        provider._NOTES.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ, {"WAX_SUMMARY_MODEL": "", "WAX_SUMMARY_MODEL_FALLBACKS": "",
                                      "WAX_SUMMARY_API_BASE": "", "WAX_SUMMARY_REQUEST_TIMEOUT_S": "",
                                      "WAX_PASS_TIMEOUT_S": ""})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(provider._NOTES.clear)
        os.environ.pop("WAX_SUMMARY_MODEL_FALLBACKS")

    def note(self, text=NOTE, name="Untitled.md") -> Path:
        path = self.root / name
        path.write_text(text)
        return path

    def run_main(self, md: Path, *outcomes, catalog=None):
        chat = FakeChat(*outcomes)
        stdout, stderr = io.StringIO(), io.StringIO()
        original = md.read_bytes()
        with patch.object(SUM.provider, "resolve_key", return_value="test-key"), \
                patch.object(SUM.provider, "list_model_ids", return_value=catalog), \
                patch.object(SUM.provider, "chat_json", side_effect=chat), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = SUM.main([str(md), "doc-0123456789abcdef"])
        self.assertEqual(md.read_bytes(), original, "a pass must never edit its note")
        result = passes._parse_result(stdout.getvalue()) if code == 0 else None
        return code, result, stderr.getvalue(), chat

    def test_happy_path_writes_the_field_and_a_section_whose_hash_matches_the_body(self):
        md = self.note()
        code, result, _, chat = self.run_main(md)
        self.assertEqual(code, 0)
        self.assertEqual(len(chat.calls), 1)
        self.assertEqual(result["wax_ep_version"], 1)
        self.assertEqual(result["frontmatter"], {"summary": ANSWER["summary"]})
        _, body = frontmatter.read(md)
        replace = result["body_replace"]
        self.assertEqual(set(replace), {"sha256", "text"})
        self.assertEqual(replace["sha256"], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(replace["text"],
                         "## Summary\n\n- Vendor quoted two weeks\n- Decision: order the 42U rack\n\n" + body)
        self.assertEqual(result["evidence"]["model"], CHAIN[0])
        self.assertEqual(result["evidence"]["chain"], CHAIN)

    def test_request_is_a_strict_schema_with_a_bounded_output(self):
        _, _, _, chat = self.run_main(self.note())
        call = chat.calls[0]
        schema = call["schema"]
        self.assertEqual(call["schema_name"], "document_summary")
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(sorted(schema["required"]), sorted(schema["properties"]))
        self.assertEqual(sorted(schema["properties"]), ["bullets", "summary"])
        self.assertLessEqual(call["max_tokens"], 1_024)
        self.assertEqual(json.loads(call["user"]), {"document": BODY.strip()})
        self.assertEqual(call["model"], CHAIN[0])

    def test_a_summary_heading_and_field_make_no_model_call(self):
        text = f"---\nsummary: Mine.\n---\n## Summary\n\n- x\n\n{BODY}"
        code, result, _, chat = self.run_main(self.note(text))
        self.assertEqual(code, 0)
        self.assertEqual(chat.calls, [])
        self.assertNotIn("body_replace", result)
        self.assertEqual(result["frontmatter"], {"summary": "Mine."})
        self.assertEqual(result["evidence"], {"source": "existing"})

    def test_a_heading_without_the_field_adds_only_the_field(self):
        for opening in ("## Summary\n\n- x\n\n", "\n\n## summary\n- x\n\n", "# Notes\n\nIntro.\n\n### Summary:\n- x\n"):
            with self.subTest(opening=opening):
                code, result, _, chat = self.run_main(self.note(f"---\ntags: [a]\n---\n{opening}{BODY}"))
                self.assertEqual(code, 0)
                self.assertEqual(len(chat.calls), 1)
                self.assertNotIn("body_replace", result)
                self.assertEqual(result["frontmatter"], {"summary": ANSWER["summary"]})

    def test_a_deep_summary_heading_is_not_the_notes_opening(self):
        far = "One.\n\nTwo.\n\nThree.\n\nFour.\n\nFive.\n\n## Summary\n\nlater\n"
        code, result, _, chat = self.run_main(self.note(f"---\ntags: [a]\n---\n{far}"))
        self.assertEqual(code, 0)
        self.assertIn("body_replace", result)

    def test_a_field_without_the_heading_keeps_the_field_and_adds_the_section(self):
        code, result, _, chat = self.run_main(self.note(f"---\nsummary: Mine.\n---\n{BODY}"))
        self.assertEqual(code, 0)
        self.assertEqual(len(chat.calls), 1)
        self.assertEqual(result["frontmatter"], {"summary": "Mine."})
        self.assertTrue(result["body_replace"]["text"].startswith("## Summary\n\n- Vendor"))

    def test_rerunning_over_its_own_output_is_a_no_op(self):
        md = self.note()
        _, first, _, _ = self.run_main(md)
        fm, body = frontmatter.read(md)
        md.write_text(frontmatter.render({**fm, **first["frontmatter"]}, first["body_replace"]["text"]))
        code, again, _, chat = self.run_main(md)
        self.assertEqual(code, 0)
        self.assertEqual(chat.calls, [])
        self.assertNotIn("body_replace", again)

    def test_an_empty_body_is_a_skip_not_a_failure(self):
        for text in ("", "   \n\n", "---\ntags: [a]\n---\n\n  \n", "---\ntags: [a]\n---\n"):
            with self.subTest(text=text):
                code, result, stderr, chat = self.run_main(self.note(text))
                self.assertEqual(code, 0, stderr)
                self.assertEqual(result, {"wax_ep_version": 1, "state": "skipped", "reason_code": "empty_document"})
                self.assertEqual(chat.calls, [])

    def test_unparseable_frontmatter_never_gets_a_section_above_it(self):
        text = "---\nkey: [unclosed\n---\n" + BODY
        code, result, _, chat = self.run_main(self.note(text))
        self.assertEqual(code, 0)
        self.assertNotIn("body_replace", result)
        self.assertEqual(result["evidence"]["body"], "unparseable_frontmatter")

    def test_a_note_with_no_frontmatter_still_gets_the_section(self):
        code, result, _, _ = self.run_main(self.note(BODY))
        self.assertEqual(code, 0)
        self.assertEqual(result["body_replace"]["sha256"], hashlib.sha256(BODY.encode()).hexdigest())
        self.assertEqual(result["body_replace"]["text"].split("\n\n", 2)[2], BODY)

    def test_answers_are_cleaned_deduplicated_and_bounded(self):
        messy = {"summary": "  One\n sentence.  ",
                 "bullets": ["- Alpha", "* alpha", "1. Beta  point", "", "   ", "x" * 900,
                             "g", "h", "i", "j"]}
        _, result, _, _ = self.run_main(self.note(), messy)
        self.assertEqual(result["frontmatter"]["summary"], "One sentence.")
        lines = [ln for ln in result["body_replace"]["text"].split("\n") if ln.startswith("- ")]
        self.assertEqual(lines[:2], ["- Alpha", "- Beta point"])
        self.assertLessEqual(len(lines), SUM.MAX_BULLETS)
        self.assertTrue(all(len(ln) <= SUM.MAX_BULLET_CHARS + 2 for ln in lines))

    def test_an_answer_without_bullets_falls_through_to_the_next_member(self):
        code, result, _, chat = self.run_main(self.note(), {"summary": "S.", "bullets": []}, ANSWER)
        self.assertEqual(code, 0)
        self.assertEqual([c["model"] for c in chat.calls], CHAIN[:2])
        self.assertEqual(result["evidence"]["model"], CHAIN[1])
        self.assertEqual([f["reason_code"] for f in result["evidence"]["fallbacks"]], ["provider_bad_response"])

    def test_a_rate_limit_falls_through_and_the_evidence_names_it(self):
        code, result, stderr, chat = self.run_main(self.note(), rate_limited(), ANSWER)
        self.assertEqual(code, 0)
        self.assertEqual([c["model"] for c in chat.calls], CHAIN[:2])
        self.assertEqual(result["evidence"]["model"], CHAIN[1])
        self.assertIn(f"{CHAIN[0]} did not serve (provider_rate_limited)", stderr)

    def test_a_member_the_catalog_lacks_is_skipped_without_a_request(self):
        catalog = set(CHAIN) - {CHAIN[0]}
        code, result, _, chat = self.run_main(self.note(), catalog=catalog)
        self.assertEqual(code, 0)
        self.assertEqual([c["model"] for c in chat.calls], [CHAIN[1]])
        self.assertEqual(result["evidence"]["fallbacks"][0]["reason_code"], "missing_model")

    def test_every_member_failing_exits_nonzero_with_the_reason_first(self):
        code, result, stderr, chat = self.run_main(self.note(), *[rate_limited() for _ in CHAIN])
        self.assertEqual(code, 1)
        self.assertEqual(len(chat.calls), len(CHAIN))
        self.assertEqual(stderr.splitlines()[0], "reason_code=provider_rate_limited")

    def test_a_crlf_replacement_hashes_raw_bytes_and_keeps_the_note_line_endings(self):
        raw = b"---\r\ntags: [infra]\r\n---\r\nMet with the vendor.\r\nThey quoted two weeks.\r\n"
        md = self.note(name="crlf.md")
        md.write_bytes(raw)
        code, result, _, _ = self.run_main(md)
        self.assertEqual(code, 0)
        _, runner_body = frontmatter.split(raw.decode())
        replace = result["body_replace"]
        self.assertEqual(replace["sha256"], hashlib.sha256(runner_body.encode()).hexdigest())
        self.assertTrue(replace["text"].endswith(runner_body))
        self.assertNotIn("\n", replace["text"].replace("\r\n", ""), "no bare LF in a CRLF note")

    def test_no_key_asks_no_model(self):
        md = self.note()
        stderr = io.StringIO()
        with patch.object(SUM.provider, "resolve_key", side_effect=provider.ProviderError("no key", "no_api_key")), \
                patch.object(SUM.provider, "list_model_ids") as listing, \
                patch.object(SUM.provider, "chat_json") as chat, redirect_stderr(stderr):
            code = SUM.main([str(md)])
        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue().splitlines()[0], "reason_code=no_api_key")
        chat.assert_not_called()
        listing.assert_not_called()

    def test_a_missing_file_is_a_classified_failure(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = SUM.main([str(self.root / "nope.md")])
        self.assertEqual((code, stderr.getvalue().splitlines()[0]), (1, "reason_code=run_error"))

    def test_a_long_document_is_sent_as_a_bounded_head_and_tail(self):
        long_body = "H" * 30_000 + "M" * 100_000 + "T" * 30_000
        _, result, stderr, chat = self.run_main(self.note(f"---\ntags: [a]\n---\n{long_body}\n"))
        sent = json.loads(chat.calls[0]["user"])["document"]
        self.assertLessEqual(len(sent), SUM.MAX_CONTEXT_CHARS + 50)
        self.assertTrue(sent.startswith("H" * SUM.HEAD_CHARS))
        self.assertTrue(sent.endswith("T" * SUM.TAIL_CHARS))
        self.assertIn("[... middle omitted ...]", sent)
        self.assertNotIn("M", sent)
        self.assertIs(result["evidence"]["truncated"], True)
        self.assertIn("summarized its first", stderr)
        # The digest still covers the WHOLE body, not the window.
        _, body = frontmatter.read(self.root / "Untitled.md")
        self.assertEqual(result["body_replace"]["sha256"], hashlib.sha256(body.encode()).hexdigest())
        self.assertTrue(result["body_replace"]["text"].endswith(body))

    def test_the_chat_never_sees_the_frontmatter(self):
        _, _, _, chat = self.run_main(self.note("---\nsecret-note: hunter2\n---\n" + BODY))
        self.assertNotIn("hunter2", chat.calls[0]["user"])

    def test_the_executable_runs_under_the_pass_interpreter(self):
        md = self.note("---\ntags: [a]\n---\n \n")
        done = subprocess.run([str(SCRIPT), str(md), "doc-0123456789abcdef"], capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(passes._parse_result(done.stdout)["reason_code"], "empty_document")
        self.assertTrue(os.access(SCRIPT, os.X_OK))


STUB = textwrap.dedent('''\
    #!/usr/bin/python3
    """Runs the real summarize pass with its model call faked, in the runner's subprocess."""
    import sys
    from importlib.machinery import SourceFileLoader
    from unittest.mock import patch

    module = SourceFileLoader("summarize_under_test", {script!r}).load_module()
    mode = {mode!r}

    def chat(**kwargs):
        if mode == "fail":
            raise module.provider.ProviderError("HTTP 503", "provider_http_error", status=503)
        return {answer!r}

    with patch.object(module.provider, "resolve_key", return_value="k"), \\
            patch.object(module.provider, "list_model_ids", return_value=None), \\
            patch.object(module.provider, "chat_json", side_effect=chat):
        raise SystemExit(module.main(sys.argv[1:]))
''')

TITLE_STUB = textwrap.dedent('''\
    #!/usr/bin/python3
    import json
    print(json.dumps({"wax_ep_version": 1, "frontmatter": {"title": "Rack Order", "title-slug": "rack-order"}}))
''')


class _Scenario:
    """The shipped untitled-standard graph compiled into a scratch ledger.

    summarize runs as the real script (model call faked); title-slug is a stub
    that returns a fixed title, since the real one needs the network. The real
    registry entry's fields (capabilities, body_mutation, requires) are kept.
    """

    def __init__(self, mode="ok"):
        self.mode = mode
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.registry = self.root / "passes.d"
        self.pipes = self.root / "pipelines.d"
        self.docs = self.root / "docs"
        for directory in (self.runtime, self.registry, self.pipes, self.docs):
            directory.mkdir()
        stub = self.root / "summarize-stub"
        stub.write_text(STUB.format(script=str(SCRIPT), mode=mode, answer=ANSWER))
        stub.chmod(0o755)
        title = self.root / "title-stub"
        title.write_text(TITLE_STUB)
        title.chmod(0o755)
        summarize = yaml.safe_load((PASSES_DIR / "summarize.yaml").read_text())
        summarize["command"] = [str(stub), "{md_path}", "{item_id}"]
        (self.registry / "summarize.yaml").write_text(yaml.safe_dump(summarize))
        slug = yaml.safe_load((PASSES_DIR / "title-slug.yaml").read_text())
        slug["command"] = [str(title), "{md_path}", "{item_id}"]
        (self.registry / "title-slug.yaml").write_text(yaml.safe_dump(slug))
        (self.pipes / "untitled-standard.yaml").write_text((PIPELINES_DIR / "untitled-standard.yaml").read_text())

    def __enter__(self):
        self.stack = [
            patch.object(paths, "DB", self.runtime / "wax.db"),
            patch.object(paths, "VAR", self.runtime),
            patch.object(passes, "REGISTRY_DIR", self.registry),
            patch.object(pipelines, "PIPELINES_DIR", self.pipes),
            patch.object(passes, "_run_frontmatters", side_effect=_fake_frontmatters),
            patch.object(events, "emit_ep_command", return_value="command-id"),
            patch.object(events, "emit"),
        ]
        for item in self.stack:
            item.start()
        _close_test_connection()
        pipelines.compile(["untitled-standard"])
        return self

    def __exit__(self, *exc):
        for item in self.stack:
            item.stop()
        _close_test_connection()
        self.tmp.cleanup()

    def drop(self, name="Untitled.md", text=NOTE) -> Path:
        path = self.docs / name
        path.write_text(text)
        return path


class UntitledStandardTest(unittest.TestCase):
    def test_the_shipped_graph_compiles_into_a_scratch_ledger(self):
        # The REAL registry and the REAL authored pipeline, in a throwaway ledger.
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            pipes = runtime / "pipelines.d"
            pipes.mkdir()
            (pipes / "untitled-standard.yaml").write_text((PIPELINES_DIR / "untitled-standard.yaml").read_text())
            stack = [patch.object(paths, "DB", runtime / "wax.db"), patch.object(paths, "VAR", runtime),
                     patch.object(passes, "REGISTRY_DIR", PASSES_DIR),
                     patch.object(pipelines, "PIPELINES_DIR", pipes), patch.object(events, "emit")]
            for item in stack:
                item.start()
            _close_test_connection()
            try:
                out = pipelines.compile(["untitled-standard"])
                self.assertEqual(out["failed"], {})
                self.assertEqual([c["name"] for c in out["compiled"]], ["untitled-standard"])
                compiled = pipelines.compiled()["untitled-standard"]
                definition = compiled["definition"]
                self.assertEqual(definition["trigger"], "manual")
                self.assertEqual(definition["failure_policy"], "halt")
                self.assertTrue(definition["enabled"])
                self.assertFalse(definition.get("folders"))
                nodes, order = pipelines._frozen_graph(definition)
                self.assertEqual(order, ["summarize", "title-slug"])
                self.assertEqual(nodes["title-slug"]["requires"], ["summarize"])
                self.assertEqual(nodes["summarize"]["requires"], [])
                frozen = nodes["summarize"]
                self.assertEqual(frozen["body_mutation"], "compare-and-replace")
                self.assertEqual(frozen["capabilities"], ["document"])
                self.assertEqual(frozen["version"], 1)
                self.assertTrue(frozen["enabled"])
                self.assertFalse(frozen["auto"])
                self.assertIn("document", nodes["title-slug"]["capabilities"])
                # A manual pipeline binds no folder: the worker's scan never picks it up.
                self.assertEqual(ledger.connect().execute("SELECT COUNT(*) FROM document_bindings").fetchone()[0], 0)
            finally:
                for item in stack:
                    item.stop()
                _close_test_connection()

    def test_the_registry_entry_matches_the_design(self):
        entry = yaml.safe_load((PASSES_DIR / "summarize.yaml").read_text())
        self.assertEqual((entry["slug"], entry["version"]), ("summarize", 1))
        self.assertEqual(entry["capabilities"], ["document"])
        self.assertIs(entry["enabled"], True)
        self.assertIs(entry["auto"], False)
        self.assertEqual(entry["requires"], [])
        self.assertEqual(entry["body_mutation"], "compare-and-replace")
        self.assertEqual(entry["timeout_s"], 390)
        self.assertEqual(entry.get("clobber") or [], [])

    def test_a_document_run_applies_the_section_title_and_a_valid_hash(self):
        with _Scenario() as scenario:
            note = scenario.drop()
            run = pipelines.run_document("untitled-standard", note, explicit=True)
            self.assertNotIn("skipped", run)
            self.assertEqual([r["state"] for r in run["results"]], ["completed", "completed"], run["results"])
            self.assertEqual(run["failed"], 0)
            fm, body = frontmatter.read(note)
            self.assertEqual(fm["summary"], ANSWER["summary"])
            self.assertEqual(fm["title-slug"], "rack-order")
            self.assertEqual(fm["tags"], ["infra"], "owner values survive")
            self.assertEqual(body, "## Summary\n\n- Vendor quoted two weeks\n- Decision: order the 42U rack\n\n" + BODY)
            self.assertEqual(note.name, "Untitled.md", "a document run never renames; the sweep does")
            self.assertEqual(documents_state(run), "processed")

    def test_a_crlf_note_hashes_the_body_the_runner_hashes(self):
        # Path.read_text turns CRLF into LF; the runner hashes the raw bytes. A
        # digest of the translated body would never match and the pass would
        # fail closed on every Windows-edited or synced note.
        raw = b"---\r\ntags: [infra]\r\n---\r\nMet with the vendor.\r\nThey quoted two weeks.\r\n"
        with _Scenario() as scenario:
            note = scenario.docs / "Untitled.md"
            note.write_bytes(raw)
            run = pipelines.run_document("untitled-standard", note, explicit=True)
            self.assertEqual([r["state"] for r in run["results"]], ["completed", "completed"], run["results"])
            # The runner's later frontmatter merges normalise line endings in
            # the file; what this pass owns is that its intent was accepted.
            self.assertIn("## Summary", note.read_text())
            self.assertIn("They quoted two weeks.", note.read_text())

    def test_a_human_edit_after_the_read_wins(self):
        # The pass hashed the body it read; an edit that lands before the runner
        # applies the intent makes the runner refuse the replacement.
        with _Scenario() as scenario:
            note = scenario.drop()
            original_run = passes._parse_result

            def edit_then_parse(stdout):
                result = original_run(stdout)
                if "body_replace" in result:
                    note.write_text(NOTE + "\nA late human edit.\n")
                return result

            with patch.object(passes, "_parse_result", side_effect=edit_then_parse):
                run = pipelines.run_document("untitled-standard", note, explicit=True)
            self.assertEqual(run["results"][0]["state"], "failed", run["results"])
            self.assertTrue(run["halted"], "halt policy: no title without a summary")
            self.assertEqual(len(run["results"]), 1)
            self.assertIn("A late human edit.", note.read_text())
            self.assertNotIn("## Summary", note.read_text())

    def test_a_failed_summarize_halts_and_title_slug_never_runs(self):
        with _Scenario(mode="fail") as scenario:
            note = scenario.drop()
            run = pipelines.run_document("untitled-standard", note, explicit=True)
            self.assertEqual([(r["ep_slug"], r["state"]) for r in run["results"]], [("summarize", "failed")])
            self.assertTrue(run["halted"])
            self.assertEqual(run["halted_after"], "summarize")
            fm, body = frontmatter.read(note)
            self.assertNotIn("title-slug", fm)
            self.assertEqual(body, BODY)
            self.assertEqual(fm["tags"], ["infra"])

    def test_an_empty_document_skips_summarize_and_title_slug_still_runs(self):
        # The skip satisfies ordering like any other (Track 1 makes title-slug
        # skip an empty document too; the stub here proves only the gating).
        with _Scenario() as scenario:
            note = scenario.drop(text="---\ntags: [infra]\n---\n\n")
            run = pipelines.run_document("untitled-standard", note, explicit=True)
            states = {r["ep_slug"]: r["state"] for r in run["results"]}
            self.assertEqual(states["summarize"], "skipped", run["results"])
            self.assertEqual(states["title-slug"], "completed", run["results"])
            self.assertNotIn("## Summary", note.read_text())


def documents_state(run):
    from wax import documents
    return documents.get(run["item_key"])["state"]


if __name__ == "__main__":
    unittest.main()
