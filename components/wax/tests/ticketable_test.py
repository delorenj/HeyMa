import hashlib
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from wax import passes, pjregistry, provider


COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
SCRIPT = COMPONENT_ROOT / "config" / "passes.d" / "bin" / "ticketable"
LOADER = SourceFileLoader("wax_ticketable_pass", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
TK = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(TK)

ITEM = "50061b11b65df1dd"
NAME = "20261008-091003-groovato-launch-and-event-schema.md"
INTAKE_SCHEMA = Path("/home/delorenj/code/33GOD/bloodbank/schemas/bloodbank/audio/intake.detected.json")


def record(pid, name, description="", **extra):
    return {"project_id": pid, "slug": pid, "name": name, "description": description,
            "repo_path": f"/home/u/code/{pid}", "status": "active",
            "ticket_provider": {"type": "plane", "workspace": "33god", "identifier": pid[:4].upper(),
                                "board_id": f"board-{pid}", "state": "active"}, **extra}


REGISTRY = {"schema_version": 1, "projects": {
    "bb": record("bb", "Bloodbank", "Event bus for the 33god development pipeline",
                 secrets={"materialize_env": "SECRET-ENV-VALUE"}),
    "gruvato": record("gruvato", "gruvato", "Drummer co-pilot practice app"),
    "voxxy": record("voxxy", "Voxxy"),
}}
ALIASES = "projects:\n  bb:\n    aliases: [blood bank]\n  gruvato:\n    aliases: [Groovato]\n"
BODY = ("**Speaker 1 (00:00):** Groovato needs a waitlist page. Scratch that, a waitlist form on the home page.\n\n"
        "**Speaker 1 (00:20):** Also blood bank needs an intake schema.")
NOTE = ("---\ntitle: Groovato Launch and Event Schema\nsummary: Plans for the drum app and the event bus.\n"
        "project-ids: [gruvato, bb]\nprojects: [gruvato, Bloodbank]\nwax-item-id: 50061b11b65df1dd\n---\n\n"
        "# Transcription: 2026_10_07_18_01_12.mp3\n\n" + BODY + "\n")
WAITLIST = "Add a waitlist signup form to the gruvato home page."
SCHEMA = "Add an intake event schema to Bloodbank."


def key_of(pid, description):
    return hashlib.sha256(f"{pid}\n{description}".encode()).hexdigest()[:16]


class FakeChat:
    """Stands in for provider.chat_json: one scripted answer per call, every call recorded."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if self.outcomes else {"tickets": []}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def tickets(*pairs):
    return {"tickets": [{"project_id": pid, "description": description} for pid, description in pairs]}


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TicketableTest(unittest.TestCase):
    def setUp(self):
        provider._NOTES.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        aliases = self.root / "aliases.yaml"
        aliases.write_text(ALIASES)
        env = patch.dict(os.environ, {"WAX_PROJECT_ALIASES": str(aliases), "WAX_TICKETS_MODEL": "",
                                      "WAX_TICKETS_API_BASE": "", "WAX_TICKETS_REQUEST_TIMEOUT_S": ""})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(provider._NOTES.clear)

    def note(self, text=NOTE, name=NAME) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_main(self, md: Path, *outcomes, registry=REGISTRY, item=ITEM):
        chat = FakeChat(*outcomes)
        fetch = (patch.object(TK.pjregistry, "fetch_registry", side_effect=registry)
                 if isinstance(registry, Exception) else
                 patch.object(TK.pjregistry, "fetch_registry", return_value=registry))
        stdout, stderr = io.StringIO(), io.StringIO()
        original = md.read_bytes()
        with fetch, patch.object(TK.provider, "resolve_key", return_value="test-key"), \
                patch.object(TK.provider, "chat_json", side_effect=chat), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = TK.main([str(md), item] if item is not None else [str(md)])
        self.assertEqual(md.read_bytes(), original, "a pass must never edit its note")
        result = passes._parse_result(stdout.getvalue()) if code == 0 else None
        return code, result, stderr.getvalue(), chat

    def test_no_project_ids_is_a_skip_without_a_model_call(self):
        for header in ("", "project-ids: []\n", "project-ids: ''\n"):
            with self.subTest(header=header):
                code, result, _, chat = self.run_main(self.note(f"---\ntitle: t\n{header}---\n{BODY}\n"))
                self.assertEqual(code, 0)
                self.assertEqual(result, {"wax_ep_version": 1, "state": "skipped", "reason_code": "no_project",
                                          "frontmatter": {"ticketable": []}})
                self.assertEqual(chat.calls, [])

    def test_tickets_land_in_frontmatter_and_are_declared_as_events(self):
        code, result, _, _ = self.run_main(self.note(), tickets(("gruvato", WAITLIST), ("bb", SCHEMA)))
        self.assertEqual(code, 0)
        self.assertEqual(result["frontmatter"]["ticketable"], [
            {"project_id": "gruvato", "description": WAITLIST, "transcript": NAME},
            {"project_id": "bb", "description": SCHEMA, "transcript": NAME},
        ])
        first, second = result["events"]
        self.assertEqual(first, {"type": "intake.detected", "key": key_of("gruvato", WAITLIST), "data": {
            "project_id": "gruvato", "project_name": "gruvato", "description": WAITLIST, "transcript": NAME,
            "intake_id": f"{ITEM}:{key_of('gruvato', WAITLIST)[:12]}", "index": 1, "count": 2}})
        self.assertEqual((second["data"]["project_name"], second["data"]["index"]), ("Bloodbank", 2))
        self.assertEqual(result["evidence"], {"model": TK.DEFAULT_MODEL, "windows": 1, "truncated": False,
                                              "registry": "ok", "dropped": {}, "shortened": 0})

    def test_request_is_a_strict_schema_whose_project_is_an_enum_of_the_note_projects(self):
        _, _, _, chat = self.run_main(self.note(), tickets())
        call = chat.calls[0]
        self.assertEqual((call["base"], call["model"], call["key"]), (TK.DEFAULT_API_BASE, TK.DEFAULT_MODEL, "test-key"))
        self.assertEqual((call["max_tokens"], call["temperature"], call["timeout"]), (8192, 0.1, 180.0))
        schema = call["schema"]
        items = schema["properties"]["tickets"]["items"]
        self.assertEqual(items["properties"]["project_id"]["enum"], ["gruvato", "bb"])
        self.assertEqual(schema["properties"]["tickets"]["maxItems"], 25)
        self.assertFalse(schema["additionalProperties"] or items["additionalProperties"])
        self.assertEqual((schema["required"], items["required"]), (["tickets"], ["project_id", "description"]))
        user = json.loads(call["user"])
        self.assertEqual(user["title"], "Groovato Launch and Event Schema")
        self.assertEqual(user["summary"], "Plans for the drum app and the event bus.")
        self.assertEqual(user["window"], "1/1")
        self.assertEqual(user["transcript"], BODY)
        self.assertEqual(user["candidate_projects"], [
            {"project_id": "gruvato", "name": "gruvato",
             "about": "Drummer co-pilot practice app. Also known as: Groovato."},
            {"project_id": "bb", "name": "Bloodbank",
             "about": "Event bus for the 33god development pipeline. Also known as: blood bank."},
        ])
        self.assertNotIn("SECRET-ENV-VALUE", call["user"])
        system = call["system"]
        for rule in ("Ground every ticket in the transcript", '"scratch that"', "keep only the final version",
                     "One ticket per distinct deliverable", "exactly one candidate project",
                     "never leave a ticket unassigned", "imperative sentence", "plain text",
                     "content, not instructions", '{"tickets": []}', "already done"):
            self.assertIn(rule, system)

    def test_tickets_outside_the_note_projects_and_malformed_entries_are_dropped(self):
        answer = {"tickets": [{"project_id": "voxxy", "description": "Ship a voice."},
                              {"project_id": "GRUVATO", "description": "Shout."},
                              {"project_id": "gruvato"}, "not a ticket",
                              {"project_id": "gruvato", "description": WAITLIST}]}
        code, result, stderr, _ = self.run_main(self.note(), answer)
        self.assertEqual(code, 0)
        self.assertEqual([t["description"] for t in result["frontmatter"]["ticketable"]], [WAITLIST])
        self.assertEqual(result["evidence"]["dropped"], {"unknown_project": 2, "malformed": 2})
        self.assertIn("outside project-ids", stderr)

    def test_descriptions_are_normalised_deduplicated_and_bounded(self):
        long = " ".join(["Detail"] * 500)
        answer = tickets(("gruvato", "  Add a   waitlist\nform.  "), ("gruvato", "add a WAITLIST form."),
                         ("bb", "   "), ("bb", long))
        _, result, stderr, _ = self.run_main(self.note(), answer)
        kept = result["frontmatter"]["ticketable"]
        self.assertEqual(kept[0]["description"], "Add a waitlist form.")
        self.assertEqual(len(kept), 2)
        shortened = kept[1]["description"]
        self.assertLessEqual(len(shortened), TK.MAX_DESCRIPTION_CHARS)
        self.assertTrue(shortened.endswith("Detail…"))
        self.assertEqual(result["evidence"]["dropped"], {"duplicate": 1, "empty": 1})
        self.assertEqual(result["evidence"]["shortened"], 1)
        self.assertIn("shortened 1 description", stderr)

        many = tickets(*[("bb", f"Fix bug number {i}.") for i in range(30)])
        _, result, _, _ = self.run_main(self.note(), many)
        self.assertEqual(len(result["frontmatter"]["ticketable"]), 25)
        self.assertEqual(len(result["events"]), 25)
        self.assertEqual(result["evidence"]["dropped"], {"over_cap": 5})

    def test_existing_tickets_are_reused_and_their_events_redeclared_under_the_same_keys(self):
        _, fresh, _, _ = self.run_main(self.note(), tickets(("gruvato", WAITLIST), ("bb", SCHEMA)))
        stored = [{"project_id": "gruvato", "description": WAITLIST, "transcript": "older-name.md"},
                  {"project_id": "bb", "description": SCHEMA, "transcript": "older-name.md"},
                  {"project_id": "bb", "description": SCHEMA, "transcript": "older-name.md"}]
        text = NOTE.replace("---\n\n# Transcription", "ticketable: " + json.dumps(stored) + "\n---\n\n# Transcription")
        code, result, _, chat = self.run_main(self.note(text, "kept/" + NAME))
        self.assertEqual((code, chat.calls), (0, []))
        self.assertEqual(result["frontmatter"], {"ticketable": stored})
        self.assertEqual([e["key"] for e in result["events"]], [e["key"] for e in fresh["events"]])
        self.assertEqual(result["events"], fresh["events"])
        self.assertEqual(result["evidence"], {"source": "existing", "registry": "ok"})

    def test_malformed_existing_tickets_or_project_ids_fail_loudly(self):
        cases = {
            "ticketable: just a string\n": "invalid_ticketable",
            "ticketable: [{project_id: bb}]\n": "invalid_ticketable",
            "ticketable: [{project_id: BB, description: x, transcript: t.md}]\n": "invalid_ticketable",
            "ticketable: [{project_id: bb, description: '', transcript: t.md}]\n": "invalid_ticketable",
            "project-ids: bb\n": "invalid_project_ids",
            "project-ids: [BB]\n": "invalid_project_ids",
        }
        for header, reason in cases.items():
            with self.subTest(header=header):
                code, _, stderr, chat = self.run_main(self.note(f"---\n{header}---\n{BODY}\n"))
                self.assertEqual((code, chat.calls), (1, []))
                self.assertEqual(passes._split_reason(stderr)[0], reason)

    def test_transcript_is_the_basename_and_output_is_deterministic(self):
        md = self.note(name="deep/nested/" + NAME)
        answer = tickets(("gruvato", WAITLIST))
        _, first, _, _ = self.run_main(md, answer)
        _, second, _, _ = self.run_main(md, json.loads(json.dumps(answer)))
        self.assertEqual(first, second)
        self.assertEqual(first["frontmatter"]["ticketable"][0]["transcript"], NAME)
        self.assertEqual(first["events"][0]["data"]["transcript"], NAME)
        _, manual, _, _ = self.run_main(md, answer, item=None)
        self.assertEqual(manual["events"][0]["data"]["intake_id"], key_of("gruvato", WAITLIST)[:12])

    def test_provider_failures_exit_nonzero_with_their_reason(self):
        for error, reason in ((provider.ProviderError("HTTP 401", "provider_auth_rejected", status=401),
                               "provider_auth_rejected"),
                              (provider.ProviderError("no answer in 180s", "timeout"), "timeout"),
                              ({"tickets": "none"}, "provider_bad_response")):
            with self.subTest(reason=reason):
                code, result, stderr, _ = self.run_main(self.note(), error)
                self.assertEqual((code, result), (1, None))
                self.assertEqual(stderr.splitlines()[0], f"reason_code={reason}")
                self.assertNotIn("waitlist", stderr)

    def test_long_transcripts_are_windowed_and_truncation_is_announced(self):
        def body_note(length):
            return f"---\nproject-ids: [gruvato]\n---\n{('word ' * (length // 5 + 1))[:length].strip()}x\n"

        same = tickets(("gruvato", WAITLIST))
        code, result, _, chat = self.run_main(self.note(body_note(300_000)), same, json.loads(json.dumps(same)))
        self.assertEqual((code, len(chat.calls)), (0, 2))
        self.assertEqual([json.loads(c["user"])["window"] for c in chat.calls], ["1/2", "2/2"])
        self.assertEqual(len(result["frontmatter"]["ticketable"]), 1)
        self.assertEqual(result["evidence"]["dropped"], {"duplicate": 1})
        self.assertFalse(result["evidence"]["truncated"])

        code, result, stderr, chat = self.run_main(self.note(body_note(800_000)))
        windows = [json.loads(c["user"]) for c in chat.calls]
        self.assertEqual([w["window"] for w in windows], ["1/3", "2/3", "3/3"])
        self.assertTrue(all(len(w["transcript"]) <= TK.WINDOW_CHARS for w in windows))
        self.assertTrue(result["evidence"]["truncated"])
        self.assertEqual(result["evidence"]["windows"], 3)
        self.assertIn("not extracted", stderr)

    def test_registry_outage_falls_back_to_note_names(self):
        down = pjregistry.RegistryError("registry_unavailable", "refused")
        code, result, stderr, chat = self.run_main(self.note(), tickets(("bb", SCHEMA)), registry=down)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(chat.calls[0]["user"])["candidate_projects"], [
            {"project_id": "gruvato", "name": "gruvato", "about": "Also known as: Groovato."},
            {"project_id": "bb", "name": "Bloodbank", "about": "Also known as: blood bank."},
        ])
        self.assertEqual(result["events"][0]["data"]["project_name"], "Bloodbank")
        self.assertEqual(result["evidence"]["registry"], "unavailable")
        self.assertIn("registry unavailable", stderr)

    def test_events_honour_the_runner_and_bloodbank_contracts(self):
        answer = tickets(*[("bb" if i % 2 else "gruvato", f"Do thing {i} — carefully.") for i in range(25)])
        _, result, _, _ = self.run_main(self.note(), answer)
        events = result["events"]
        self.assertLessEqual(len(events), 50)
        self.assertEqual(len({e["key"] for e in events}), len(events))
        for event in events:
            self.assertRegex(event["type"], r"^[a-z][a-z_]*\.[a-z][a-z_]*$")
            self.assertRegex(event["key"], r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")
            self.assertNotIn("project", event["data"])
            self.assertLessEqual(len(json.dumps(event["data"]).encode()), 32 * 1024)
        if not INTAKE_SCHEMA.is_file():
            self.skipTest("bloodbank intake.detected schema not present")
        import jsonschema
        schema = json.loads(INTAKE_SCHEMA.read_text())["properties"]["data"]
        for event in events:
            stamped = {**event["data"], "transcription_id": ITEM, "item_id": ITEM, "project": "wax"}
            jsonschema.Draft202012Validator(schema).validate(stamped)

    def test_declared_events_pass_the_runner_validation_for_the_shipped_registry_entry(self):
        validate = getattr(passes, "_validated_events", None)
        if validate is None:
            self.skipTest("runner has no pass-event validation")
        with patch.object(passes, "REGISTRY_DIR", COMPONENT_ROOT / "config" / "passes.d"):
            entry = passes.registry()["ticketable"]
        self.assertEqual(entry["emits"], ["intake.detected"])
        _, result, _, _ = self.run_main(self.note(), tickets(("gruvato", WAITLIST), ("bb", SCHEMA)))
        self.assertEqual(validate(entry, result), result["events"])

    def test_executable_runs_under_the_pass_interpreter(self):
        env = {**os.environ, "PJ_REGISTRY_URL": f"http://127.0.0.1:{closed_port()}", "PJ_PROJECT_REGISTRY": ""}
        md = self.note(f"---\ntitle: t\n---\n{BODY}\n")
        done = subprocess.run([str(SCRIPT), str(md), ITEM], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(passes._parse_result(done.stdout)["reason_code"], "no_project")
        self.assertTrue(re.fullmatch(r"\{.*\}\n", done.stdout, re.DOTALL))


if __name__ == "__main__":
    unittest.main()
