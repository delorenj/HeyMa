import importlib.util
import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from wax import jev, passes, pjregistry, provider


COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
SCRIPT = COMPONENT_ROOT / "config" / "passes.d" / "bin" / "project-extraction"
LOADER = SourceFileLoader("wax_project_extraction_pass", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
PE = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(PE)

DATED_MODEL = "typesafe/jev-1.13-20260917"


def record(pid, name, description="", repo=None, **extra):
    return {"project_id": pid, "slug": pid, "name": name, "description": description,
            "repo_path": repo or f"/home/u/code/{pid}", "status": "active",
            "ticket_provider": {"type": "plane", "workspace": "33god", "identifier": pid[:4].upper(),
                                "board_id": f"board-{pid}", "state": "active"}, **extra}


REGISTRY = {"schema_version": 1, "projects": {
    "bb": record("bb", "Bloodbank", "Event bus for the 33god development pipeline", "/home/u/code/33GOD/bloodbank"),
    "gruvato": record("gruvato", "gruvato", "Drummer co-pilot practice app"),
    "intelliforia": record("intelliforia", "IntelliForia", "EMR compliance guardian",
                           secrets={"materialize_env": "SECRET-ENV-VALUE"}),
    "tower-of-dumb-things": record("tower-of-dumb-things", "Tower Of Dumb Things",
                                   repo="/home/u/code/pile-of-dumb-things"),
    "transcription-queue": record("transcription-queue", "HeyMa", "33GOD TTS/STT Audio Pipeline", "/home/u/HeyMa"),
}}
ALIASES = """projects:
  bb:
    aliases: [blood bank]
  transcription-queue:
    aliases: [Hey Ma]
    about: The audio pipeline.
"""
GENERIC = ("---\ntitle: Release plan\ntags: [planning]\n---\n# Release plan\n\n"
           "We will ship the Bloodbank schema change next week.\n")
TRANSCRIPT = ("---\nwax-item-id: 5eea3d5cd205bb84\nclassification: meeting\n---\n\n"
              "# Transcription: 2026_10_08_10_14_11.mp3\n\n"
              "**Speaker 1 (00:00):** The blood bank events need a new schema.\n\n"
              "**Speaker 2 (00:12):** And Hey Ma should file the tickets.\n")


class FakeJev:
    """Stands in for jev.decide: one scripted outcome per call, every call recorded."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, state, questions, *, key, model, url, **_kwargs):
        self.calls.append({"state": state, "questions": questions, "key": key, "model": model, "url": url})
        outcome = self.outcomes.pop(0) if self.outcomes else {}
        if isinstance(outcome, Exception):
            raise outcome
        return jev.Decision(answers={pid: {"type": "noul", "noul": outcome.get(pid, 0.01)} for pid in questions},
                            model=DATED_MODEL, cost=0.0003, request_id=f"gen-{len(self.calls)}")


def too_large():
    return provider.ProviderError("Jev's token budget was exceeded", "jev_too_large", status=400,
                                  detail_code="max_tokens_exceeded")


def text_of(length: int) -> str:
    return ("word " * (length // 5 + 1))[:length].strip().ljust(length, "x")


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ProjectExtractionTest(unittest.TestCase):
    def setUp(self):
        provider._NOTES.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        aliases = self.root / "aliases.yaml"
        aliases.write_text(ALIASES)
        env = patch.dict(os.environ, {"WAX_PROJECT_ALIASES": str(aliases), "WAX_PROJECT_MIN_P": "0.6",
                                      "WAX_PROJECT_MIN_P_LONG": "0.75", "WAX_JEV_MODEL": "", "WAX_JEV_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(provider._NOTES.clear)

    def note(self, text: str, name: str = "note.md") -> Path:
        path = self.root / name
        path.write_text(text)
        return path

    def patched(self, fake, registry=REGISTRY):
        fetch = (patch.object(PE.pjregistry, "fetch_registry", side_effect=registry)
                 if isinstance(registry, Exception) else
                 patch.object(PE.pjregistry, "fetch_registry", return_value=registry))
        return fetch, patch.object(PE.provider, "resolve_key", return_value="test-key"), \
            patch.object(PE.jev, "decide", side_effect=fake)

    def build(self, md: Path, *outcomes, registry=REGISTRY):
        fake = FakeJev(*outcomes)
        fetch, key, decide = self.patched(fake, registry)
        with fetch as fetched, key, decide:
            result = PE.build_result(md)
        return result, fake, fetched

    def run_main(self, md: Path, *outcomes, registry=REGISTRY):
        fake = FakeJev(*outcomes)
        fetch, key, decide = self.patched(fake, registry)
        stdout, stderr = io.StringIO(), io.StringIO()
        with fetch, key, decide, redirect_stdout(stdout), redirect_stderr(stderr):
            code = PE.main([str(md), "item-1"])
        return code, stdout.getvalue(), stderr.getvalue(), fake

    def test_one_core_serves_a_generic_document_and_a_diarized_transcript(self):
        outcome = {"bb": 0.93, "transcription-queue": 0.71, "gruvato": 0.45, "intelliforia": 0.1}
        for fixture, first_line in ((GENERIC, "# Release plan"), (TRANSCRIPT, "**Speaker 1 (00:00):**")):
            with self.subTest(first_line=first_line):
                result, fake, _ = self.build(self.note(fixture), outcome)
                self.assertEqual(result["wax_ep_version"], 1)
                self.assertEqual(result["frontmatter"], {"project-ids": ["bb", "transcription-queue"],
                                                         "projects": ["Bloodbank", "HeyMa"]})
                evidence = result["evidence"]
                self.assertEqual(evidence["probabilities"], {"bb": 0.93, "transcription-queue": 0.71, "gruvato": 0.45})
                self.assertEqual((evidence["threshold"], evidence["windows"], evidence["candidates"]), (0.6, 1, 5))
                self.assertEqual((evidence["model"], evidence["request_ids"]), (DATED_MODEL, ["gen-1"]))
                sent = fake.calls[0]["state"]
                self.assertEqual(set(sent), {"transcript"})
                self.assertTrue(sent["transcript"].startswith(first_line))
                for absent in ("# Transcription:", "wax-item-id", "classification:", "tags:"):
                    self.assertNotIn(absent, sent["transcript"])
                self.assertEqual((fake.calls[0]["model"], fake.calls[0]["url"]), (jev.DEFAULT_MODEL, jev.DEFAULT_URL))

    def test_one_question_per_candidate_with_hints_and_never_a_whole_record(self):
        _, fake, _ = self.build(self.note(TRANSCRIPT), {})
        questions = fake.calls[0]["questions"]
        self.assertEqual(list(questions), sorted(REGISTRY["projects"]))
        self.assertTrue(all(q["type"] == "noul" and set(q["criteria"]) == {"true", "false"}
                            for q in questions.values()))
        bb = questions["bb"]["instructions"]
        self.assertIn('"Bloodbank" (project id: bb)', bb)
        self.assertIn("Also known as: blood bank.", bb)
        self.assertIn("Description: Event bus for the 33god development pipeline.", bb)
        self.assertIn("pile-of-dumb-things", questions["tower-of-dumb-things"]["instructions"])
        heyma = questions["transcription-queue"]["instructions"]
        self.assertIn("Hey Ma", heyma)
        self.assertIn("The audio pipeline.", heyma)
        encoded = json.dumps(questions)
        self.assertNotIn("SECRET-ENV-VALUE", encoded)
        self.assertNotIn("for example", encoded.lower())

    def test_threshold_is_stricter_above_thirty_thousand_chars(self):
        for length, expected_threshold, expected in ((30_000, 0.6, ["gruvato"]), (30_001, 0.75, [])):
            with self.subTest(length=length):
                result, _, _ = self.build(self.note(f"---\ntitle: t\n---\n{text_of(length)}"), {"gruvato": 0.7})
                self.assertEqual(result["evidence"]["threshold"], expected_threshold)
                self.assertEqual(result["frontmatter"]["project-ids"], expected)
                self.assertEqual(result["evidence"]["probabilities"], {"gruvato": 0.7})

    def test_accepted_projects_are_ranked_then_capped(self):
        registry = {"schema_version": 1, "projects": {
            f"p{i:02d}": record(f"p{i:02d}", f"Project {i}") for i in range(10)}}
        probabilities = {"p00": 0.7, "p01": 0.9, "p02": 0.9, "p03": 0.61, "p04": 0.99, "p05": 0.8,
                         "p06": 0.65, "p07": 0.75, "p08": 0.62, "p09": 0.6}
        result, _, _ = self.build(self.note(GENERIC), probabilities, registry=registry)
        self.assertEqual(result["frontmatter"]["project-ids"],
                         ["p04", "p01", "p02", "p05", "p07", "p00", "p06", "p08"])
        self.assertEqual(result["frontmatter"]["projects"][0], "Project 4")

    def test_long_documents_are_windowed_and_merged_by_maximum(self):
        text = text_of(150_000)
        md = self.note(f"---\ntitle: t\n---\n{text}")
        result, fake, _ = self.build(
            md,
            {"bb": 0.9, "gruvato": 0.2},
            {"bb": 0.3, "gruvato": 0.8},
            {"tower-of-dumb-things": 0.76, "bb": 0.1},
        )
        windows = [call["state"]["transcript"] for call in fake.calls]
        self.assertEqual(len(windows), 3)
        self.assertTrue(all(len(window) <= PE.WINDOW_CHARS for window in windows))
        self.assertTrue(text.startswith(windows[0]) and text.endswith(windows[-1]))
        self.assertEqual(result["frontmatter"]["project-ids"], ["bb", "gruvato", "tower-of-dumb-things"])
        self.assertEqual(result["evidence"]["windows"], 3)
        self.assertEqual(result["evidence"]["cost"], 0.0009)
        self.assertEqual(result["evidence"]["request_ids"], ["gen-1", "gen-2", "gen-3"])

        single, fake, _ = self.build(self.note(f"---\ntitle: t\n---\n{text_of(80_000)}", "single.md"), {})
        self.assertEqual((len(fake.calls), single["evidence"]["windows"]), (1, 1))

    def test_token_budget_overflow_halves_the_window_once(self):
        md = self.note(f"---\ntitle: t\n---\n{text_of(50_000)}")
        result, fake, _ = self.build(md, too_large(), {"bb": 0.9}, {"gruvato": 0.95})
        self.assertEqual(len(fake.calls), 3)
        self.assertTrue(all(len(call["state"]["transcript"]) <= 27_000 for call in fake.calls[1:]))
        self.assertEqual(result["evidence"]["windows"], 2)
        self.assertEqual(result["frontmatter"]["project-ids"], ["gruvato", "bb"])

        code, stdout, stderr, _ = self.run_main(md, too_large(), too_large())
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr.splitlines()[0], "reason_code=jev_too_large")

    def test_existing_project_ids_are_preserved_without_a_decision(self):
        md = self.note("---\nproject-ids: [transcription-queue, retired-project]\n---\nBody about other things.\n")
        result, fake, _ = self.build(md, {"bb": 0.99})
        self.assertEqual(fake.calls, [])
        self.assertEqual(result["frontmatter"], {"project-ids": ["transcription-queue", "retired-project"],
                                                 "projects": ["HeyMa", "retired-project"]})
        self.assertEqual(result["evidence"], {"source": "existing", "registry": "ok",
                                              "unknown": ["retired-project"]})

        named = self.note("---\nproject-ids: [bb]\nprojects: [Blood Bank]\n---\nBody.\n", "named.md")
        result, fake, fetched = self.build(named)
        fetched.assert_not_called()
        self.assertEqual(result["frontmatter"], {"project-ids": ["bb"]})

        down = pjregistry.RegistryError("registry_unavailable", "refused")
        result, fake, _ = self.build(md, registry=down)
        self.assertEqual(result["frontmatter"], {"project-ids": ["transcription-queue", "retired-project"]})
        self.assertEqual(result["evidence"]["registry"], "unavailable")

    def test_invalid_existing_project_ids_fail_and_are_preserved(self):
        for value in ("bb", "[BB]", "[bb, bb]", "[1]", "{a: 1}", "[bb, '']"):
            with self.subTest(value=value):
                md = self.note(f"---\nproject-ids: {value}\n---\nBody.\n")
                original = md.read_bytes()
                code, stdout, stderr, fake = self.run_main(md, {"bb": 0.99})
                self.assertEqual((code, stdout, fake.calls), (1, "", []))
                self.assertEqual(passes._split_reason(stderr)[0], "invalid_project_ids")
                self.assertEqual(md.read_bytes(), original)

    def test_registry_failure_is_a_failure_never_an_empty_list(self):
        md = self.note(TRANSCRIPT)
        down = pjregistry.RegistryError("registry_unavailable", "http://127.0.0.1:8764/v1/registry: refused")
        code, stdout, stderr, fake = self.run_main(md, {"bb": 0.99}, registry=down)
        self.assertEqual((code, stdout, fake.calls), (1, "", []))
        self.assertEqual(stderr.splitlines()[0], "reason_code=registry_unavailable")

        code, stdout, stderr, _ = self.run_main(md, registry={"schema_version": 1, "projects": {}})
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr.splitlines()[0], "reason_code=registry_invalid")

    def test_provider_failures_exit_nonzero_with_their_reason(self):
        md = self.note(TRANSCRIPT)
        denied = provider.ProviderError("HTTP 401 from x: key rejected", "provider_auth_rejected", status=401)
        code, stdout, stderr, _ = self.run_main(md, denied)
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr.splitlines()[0], "reason_code=provider_auth_rejected")
        self.assertNotIn("blood bank events", stderr)

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(PE.pjregistry, "fetch_registry", return_value=REGISTRY), \
                patch.object(PE.provider, "resolve_key",
                             side_effect=provider.ProviderError("op did not resolve", "no_api_key")), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(PE.main([str(md)]), 1)
        self.assertEqual(stderr.getvalue().splitlines()[0], "reason_code=no_api_key")

    def test_empty_document_fails(self):
        code, stdout, stderr, fake = self.run_main(self.note("---\ntitle: t\n---\n\n# Transcription: a.ogg\n\n"))
        self.assertEqual((code, stdout, fake.calls), (1, "", []))
        self.assertEqual(stderr.splitlines()[0], "reason_code=empty_document")

    def test_nothing_accepted_is_a_successful_empty_list_and_the_note_is_untouched(self):
        md = self.note(TRANSCRIPT)
        original = md.read_bytes()
        code, stdout, _, _ = self.run_main(md, {"bb": 0.59, "gruvato": 0.3})
        self.assertEqual(code, 0)
        self.assertEqual(len(stdout.splitlines()), 1)
        result = passes._parse_result(stdout)
        self.assertEqual(result["frontmatter"], {"project-ids": [], "projects": []})
        self.assertEqual(result["evidence"]["probabilities"], {"bb": 0.59, "gruvato": 0.3})
        self.assertEqual(md.read_bytes(), original)

    def test_executable_runs_under_the_pass_interpreter(self):
        env = {**os.environ, "PJ_REGISTRY_URL": f"http://127.0.0.1:{closed_port()}", "PJ_PROJECT_REGISTRY": ""}
        kept = self.note("---\nproject-ids: [bb]\nprojects: [Bloodbank]\n---\nBody.\n", "kept.md")
        done = subprocess.run([str(SCRIPT), str(kept), "item-1"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(passes._parse_result(done.stdout)["frontmatter"], {"project-ids": ["bb"]})

        empty = self.note("---\ntitle: t\n---\n", "empty.md")
        done = subprocess.run([str(SCRIPT), str(empty), "item-1"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(passes._split_reason(done.stderr)[0], "empty_document")


if __name__ == "__main__":
    unittest.main()
