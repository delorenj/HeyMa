"""Pass-declared events (`events` in a wax.ep.v1 result) and the dependency gate.

Every test runs against a temp WAX_ROOT ledger with stub pass executables that
print canned JSON; nothing here reaches the live ledger, the vault, NATS or a
provider. The outbox is real (it is just a table), so ordering assertions are
about what would actually drain.
"""

import copy
import hashlib
import importlib.machinery
import importlib.util
import json
import shlex
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import yaml

from wax import events, finalize, frontmatter, ledger, passes, paths, sentinel

COMPONENT_ROOT = next(parent for parent in Path(__file__).resolve().parents
                      if (parent / "pyproject.toml").is_file())
SCHEMA_ROOT = Path("/home/delorenj/code/33GOD/bloodbank/schemas")
COMPLETED = "bloodbank.evt.audio.transcription.completed"
INTAKE = "bloodbank.evt.audio.intake.detected"


def _fake_frontmatters(args):
    if args[0] != "set":
        return
    updates = {}
    for pair in args[2:]:
        key, raw = pair.split("=", 1)
        updates[key] = json.loads(raw)
    frontmatter.merge(Path(args[1]), updates)


def _intake_validator():
    import jsonschema
    from referencing import Registry, Resource
    schema = json.loads((SCHEMA_ROOT / "bloodbank/audio/intake.detected.json").read_text())
    documents = [json.loads(path.read_text()) for path in (SCHEMA_ROOT / "_common").glob("*.json")]
    registry = Registry().with_resources((doc["$id"], Resource.from_contents(doc))
                                         for doc in documents if "$id" in doc)
    return jsonschema.Draft202012Validator(schema, registry=registry,
                                           format_checker=jsonschema.FormatChecker())


class PassEventsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        for name, value in {"VAR": self.root / "var", "DB": self.root / "var/wax.db",
                            "ARCHIVE": self.root / "archive", "VAULT": self.root / "vault"}.items():
            self.stack.enter_context(patch.object(paths, name, value))
        self.registry_dir = self.root / "passes.d"
        for directory in (paths.VAR, paths.ARCHIVE, paths.VAULT, self.registry_dir):
            directory.mkdir(parents=True)
        self.stack.enter_context(patch.object(passes, "REGISTRY_DIR", self.registry_dir))
        self.stack.enter_context(patch.object(passes, "_run_frontmatters", side_effect=_fake_frontmatters))
        self.stack.enter_context(patch.object(finalize, "audio_url", return_value="https://s3.example/audio"))
        self.close()
        audio = paths.ARCHIVE / "source.ogg"
        audio.write_bytes(b"preserved source audio")
        self.item = ledger.upsert_item(audio)
        self.md = paths.VAULT / "20261008-101411-rec.md"
        self.body = "\n# Transcript\nCall the pjangler registry people, then fix the café.\n"
        self.md.write_text(frontmatter.render({"wax-item-id": self.item, "title": "Human title"}, self.body))
        ledger.connect().execute("INSERT INTO transcripts(item_id,md_path,audio_duration,created_at) VALUES(?,?,?,?)",
                                 (self.item, str(self.md), 125, sentinel.utcnow()))
        ledger.connect().execute("INSERT INTO backups VALUES(?,?,?,?,?,?)",
                                 (self.item, "day/hash source.ogg", "recordings", audio.stat().st_size,
                                  sentinel.utcnow(), "size+md5"))

    def close(self):
        conn = getattr(ledger._local, "conn", None)
        if conn:
            conn.close()
            del ledger._local.conn

    def tearDown(self):
        self.close()
        self.stack.close()
        self.temp.cleanup()

    # ---- stub passes ------------------------------------------------------

    def add_pass(self, slug, result=None, *, emits=None, requires=None, version=1, auto=True, **extra):
        out = self.registry_dir / f"{slug}.out"
        flag = self.registry_dir / f"{slug}.fail"
        script = self.registry_dir / f"{slug}.sh"
        script.write_text(f"#!/bin/sh\nif [ -f {shlex.quote(str(flag))} ]; then\n"
                          f"  echo reason_code=provider_down >&2\n  echo 'upstream said no' >&2\n  exit 1\nfi\n"
                          f"cat {shlex.quote(str(out))}\n")
        script.chmod(0o755)
        self.set_result(slug, result)
        definition = {"slug": slug, "version": version, "enabled": True, "auto": auto,
                      "requires": requires or [], "command": [str(script), "{md_path}"], **extra}
        if emits is not None:
            definition["emits"] = emits
        (self.registry_dir / f"{slug}.yaml").write_text(yaml.safe_dump(definition))

    def set_result(self, slug, result):
        payload = {"wax_ep_version": 1, **(result or {})}
        (self.registry_dir / f"{slug}.out").write_text(json.dumps(payload) + "\n")

    def set_failing(self, slug, failing):
        flag = self.registry_dir / f"{slug}.fail"
        flag.touch() if failing else flag.unlink(missing_ok=True)

    def add_renamer(self, **kwargs):
        self.add_pass("renamer", {"frontmatter": {"title-slug": "final-title"},
                                  "transcript": {"slug": "final-title"}}, **kwargs)

    def add_emitter(self, events_list, *, requires=("renamer",), emits=("intake.detected",), **kwargs):
        self.add_pass("emitter", {"frontmatter": {"ticketable": [{"n": len(events_list)}]},
                                  "events": events_list},
                      emits=list(emits), requires=list(requires), **kwargs)

    def ticket(self, description, index=1, count=1, **data):
        digest = hashlib.sha256(description.encode()).hexdigest()[:12]
        intake_id = f"{self.item}:{digest}"
        return {"type": "intake.detected", "key": intake_id,
                "data": {"project_id": "transcription-queue", "project_name": "HeyMa",
                         "description": description, "intake_id": intake_id,
                         "index": index, "count": count, **data}}

    # ---- observation -------------------------------------------------------

    def outbox(self, *subjects):
        rows = ledger.connect().execute("SELECT id, subject, envelope FROM outbox ORDER BY id").fetchall()
        return [(row["id"], row["subject"], json.loads(row["envelope"])) for row in rows
                if not subjects or row["subject"] in subjects]

    def table(self, name):
        return [dict(row) for row in ledger.connect().execute(f"SELECT * FROM {name} ORDER BY rowid")]

    def pass_row(self, slug):
        row = ledger.connect().execute("SELECT * FROM passes WHERE item_id=? AND ep_slug=?",
                                       (self.item, slug)).fetchone()
        return dict(row) if row else None

    def note(self):
        return frontmatter.read(self.current_md())

    def current_md(self):
        return Path(ledger.connect().execute("SELECT md_path FROM transcripts WHERE item_id=?",
                                             (self.item,)).fetchone()["md_path"])


class EventValidationTest(PassEventsCase):
    def invalid_cases(self):
        good = self.ticket("Ship it")
        return {
            "undeclared type": [{**good, "type": "intake.flagged"}],
            "type is not entity.action": [{**good, "type": "Intake.Detected"}],
            "key with a space": [{**good, "key": "has space"}],
            "key over 128 chars": [{**good, "key": "a" * 129}],
            "key starting with punctuation": [{**good, "key": ":leading"}],
            "duplicate key": [good, {**self.ticket("Other"), "key": good["key"]}],
            "project in data": [{**good, "data": {**good["data"], "project": "transcription-queue"}}],
            "more than 50": [{**good, "key": f"k{i}"} for i in range(51)],
            "member is not an object": ["not-a-dict"],
            "extra field": [{**good, "extra": 1}],
            "missing field": [{"type": "intake.detected", "key": "k"}],
            "data is not an object": [{**good, "data": [1, 2]}],
            "data over 32 KiB": [{**good, "data": {"blob": "x" * (32 * 1024)}}],
            "data with NaN": [{**good, "data": {"x": float("nan")}}],
        }

    def test_malformed_events_fail_the_pass_before_the_note_is_touched(self):
        for name, declared in self.invalid_cases().items():
            with self.subTest(name):
                self.add_pass("emitter", {"frontmatter": {"leak-check": "must not land"},
                                          "transcript": {"slug": "must-not-rename"},
                                          "events": declared},
                              emits=["intake.detected"])
                before = self.md.read_bytes()
                ep = passes.registry()["emitter"]
                result = {"wax_ep_version": 1, "frontmatter": {"leak-check": "must not land"},
                          "transcript": {"slug": "must-not-rename"}, "events": copy.deepcopy(declared)}
                with self.assertRaises(passes.PassError):
                    passes._apply_result(self.item, self.md, ep, result)
                self.assertEqual(self.md.read_bytes(), before)
                self.assertEqual(sorted(p.name for p in paths.VAULT.iterdir()), [self.md.name])

                outcome = passes.run(self.item, "emitter")
                self.assertEqual(outcome["state"], "failed")
                self.assertEqual(outcome["reason_code"], "result_apply_failed")
                self.assertEqual(self.pass_row("emitter")["reason_code"], "result_apply_failed")
                fm, body = frontmatter.read(self.md)
                self.assertNotIn("leak-check", fm)
                self.assertEqual(body, self.body)
                self.assertEqual(sorted(p.name for p in paths.VAULT.iterdir()), [self.md.name])
                fm.pop("wax")
                self.assertEqual(fm, {"wax-item-id": self.item, "title": "Human title"})

    def test_error_detail_never_echoes_event_content(self):
        secret = "the-description-text-from-the-transcript"
        self.add_pass("emitter", {"events": [{**self.ticket(secret), "key": "bad key"}]},
                      emits=["intake.detected"])
        outcome = passes.run(self.item, "emitter")
        self.assertEqual(outcome["reason_code"], "result_apply_failed")
        self.assertNotIn(secret, outcome["error"])
        self.assertNotIn(secret, self.pass_row("emitter")["detail"])

    def test_events_from_a_pass_that_declares_no_emits_fail_but_an_empty_list_is_fine(self):
        self.add_pass("emitter", {"events": [self.ticket("Ship it")]})
        self.assertEqual(passes.run(self.item, "emitter")["reason_code"], "result_apply_failed")
        self.add_pass("emitter", {"events": []})
        self.assertEqual(passes.run(self.item, "emitter")["state"], "completed")

    def test_malformed_emits_fails_only_that_pass_and_never_breaks_ordering(self):
        self.add_pass("sibling", {"frontmatter": {"sibling-field": "ok"}})
        for emits in ("intake.detected", ["IntakeDetected"], [3], {"intake": "detected"}):
            with self.subTest(emits=emits):
                self.add_pass("emitter", {"events": [self.ticket("Ship it")]}, emits=emits)
                self.assertEqual(sorted(passes.ordered(passes.registry())), ["emitter", "sibling"])
                self.assertEqual(passes.run(self.item, "emitter")["reason_code"], "result_apply_failed")
                self.assertEqual(passes.run(self.item, "sibling")["state"], "completed")

    def test_valid_events_are_recorded_verbatim_with_the_result(self):
        declared = [self.ticket("First", 1, 2), self.ticket("Second", 2, 2)]
        self.add_emitter(declared, requires=[])
        outcome = passes.run(self.item, "emitter")
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(json.loads(self.pass_row("emitter")["result"])["events"], declared)
        self.assertEqual(self.table("pass_events"), [])

    def test_events_on_a_reported_skip_are_recorded_but_never_published(self):
        self.add_pass("emitter", {"state": "skipped", "reason_code": "no_project",
                                  "events": [self.ticket("Ship it")]}, emits=["intake.detected"])
        self.assertEqual(passes.run(self.item, "emitter")["state"], "skipped")
        self.assertEqual(finalize.pending_pass_events(ledger.connect(), self.item, {}), [])


class PublicationTest(PassEventsCase):
    def setUp(self):
        super().setUp()
        self.tickets = [self.ticket("zulu goes first", 1, 3), self.ticket("alpha goes second", 2, 3),
                        self.ticket("mike goes last", 3, 3)]
        self.add_renamer()
        self.add_emitter(self.tickets)

    def finalize_new(self):
        outcomes = passes.run_auto(self.item)
        self.assertEqual([o["state"] for o in outcomes], ["completed", "completed"], outcomes)
        return finalize.finalize(self.item)

    def test_new_completion_drains_completed_then_each_ticket_in_list_order(self):
        receipt = self.finalize_new()
        self.assertTrue(receipt["finalized"])
        self.assertEqual(receipt["pass_events"], 3)
        self.assertNotIn("pass_events_dropped", receipt)
        rows = self.outbox(COMPLETED, INTAKE)
        self.assertEqual([subject for _, subject, _ in rows], [COMPLETED, INTAKE, INTAKE, INTAKE])
        self.assertEqual([env["data"]["description"] for _, _, env in rows[1:]],
                         ["zulu goes first", "alpha goes second", "mike goes last"])
        completion_id = rows[0][2]["id"]
        self.assertEqual(rows[0][0], receipt["outbox_id"])
        self.assertEqual([row_id for row_id, _, _ in rows], list(range(rows[0][0], rows[0][0] + 4)),
                         "tickets sit immediately after the completion row")
        for _, _, env in rows[1:]:
            self.assertEqual(env["correlationid"], completion_id)
            self.assertEqual(env["causationid"], completion_id)
            self.assertEqual(env["ordering_key"], f"transcription:{self.item}")
            self.assertEqual(env["type"], "bloodbank.audio.intake.detected")
            self.assertEqual(env["data"]["project"], "wax")
        closed = [i for i, subject, _ in self.outbox() if subject.endswith(".file.closed")]
        self.assertTrue(closed and min(closed) > rows[-1][0])

    def test_event_ids_are_content_keyed_and_recorded_in_pass_events(self):
        self.finalize_new()
        rows = self.outbox(INTAKE)
        receipts = self.table("pass_events")
        self.assertEqual(len(receipts), 3)
        for (outbox_id, _, env), receipt, declared in zip(rows, receipts, self.tickets):
            expected = str(uuid.uuid5(events.WAX_NS, f"ep-event:{self.item}:emitter:intake.detected:{declared['key']}"))
            self.assertEqual(env["id"], expected)
            self.assertEqual(receipt["event_id"], expected)
            self.assertEqual(receipt["outbox_id"], outbox_id)
            self.assertEqual((receipt["item_id"], receipt["ep_slug"], receipt["type"]),
                             (self.item, "emitter", "intake.detected"))
            self.assertEqual(receipt["command_id"], self.pass_row("emitter")["command_id"])

    def test_refinalizing_never_duplicates(self):
        receipt = self.finalize_new()
        again = finalize.finalize(self.item)
        self.assertTrue(again["unchanged"])
        self.assertEqual(again["event_id"], receipt["event_id"])
        self.assertEqual(again["pass_events"], 0)
        self.assertEqual(len(self.outbox(INTAKE)), 3)
        self.assertEqual(len(self.table("pass_events")), 3)

    def test_replanning_into_a_second_completion_does_not_duplicate_tickets(self):
        first = self.finalize_new()
        ledger.connect().execute("UPDATE processing_plans SET plan_id='definition-changed' WHERE item_id=?",
                                 (self.item,))
        second = finalize.finalize(self.item)
        self.assertTrue(second["finalized"])
        self.assertNotIn("unchanged", second)
        self.assertNotEqual(second["event_id"], first["event_id"])
        self.assertEqual(second["pass_events"], 0)
        self.assertEqual(len(self.table("completions")), 2)
        self.assertEqual(len(self.outbox(COMPLETED)), 2)
        self.assertEqual(len(self.outbox(INTAKE)), 3)

    def test_rerunning_the_pass_with_the_same_keys_republishes_nothing(self):
        self.finalize_new()
        self.assertEqual(passes.run(self.item, "emitter")["state"], "completed")
        self.assertEqual(finalize.finalize(self.item)["pass_events"], 0)
        self.assertEqual(len(self.outbox(INTAKE)), 3)

    def test_a_new_key_from_a_rerun_publishes_only_the_new_ticket(self):
        self.finalize_new()
        self.set_result("emitter", {"events": [*self.tickets, self.ticket("a fourth, new one", 4, 4)]})
        passes.run(self.item, "emitter")
        receipt = finalize.finalize(self.item)
        self.assertEqual(receipt["pass_events"], 1)
        self.assertEqual([env["data"]["description"] for _, _, env in self.outbox(INTAKE)][-1],
                         "a fourth, new one")
        self.assertEqual(len(self.outbox(INTAKE)), 4)

    def test_backfill_on_a_finalized_item_is_correlated_to_the_prior_completion(self):
        # The item completed with only `renamer` planned; the emitter was added
        # afterwards and run by hand (`wax ep run emitter <item>`).
        self.add_pass("emitter", {"events": self.tickets}, emits=["intake.detected"], auto=False,
                      requires=["renamer"])
        self.assertEqual(passes.run_auto(self.item)[0]["state"], "completed")
        first = finalize.finalize(self.item)
        self.assertTrue(first["finalized"])
        self.assertEqual(first["pass_events"], 0)
        self.assertEqual(self.outbox(INTAKE), [])

        self.assertEqual(passes.run(self.item, "emitter")["state"], "completed")
        receipt = finalize.finalize(self.item)
        self.assertTrue(receipt["unchanged"])
        self.assertEqual(receipt["pass_events"], 3)
        rows = self.outbox(INTAKE)
        self.assertEqual(len(rows), 3)
        for _, _, env in rows:
            self.assertEqual(env["correlationid"], first["event_id"])
            self.assertEqual(env["causationid"], first["event_id"])
        self.assertEqual(finalize.finalize(self.item)["pass_events"], 0)
        self.assertEqual(len(self.outbox(INTAKE)), 3)

    def test_backfill_waits_for_the_transcript_and_says_why(self):
        self.finalize_new()
        ledger.connect().execute("DELETE FROM pass_events")
        ledger.connect().execute("DELETE FROM outbox WHERE subject=?", (INTAKE,))
        hidden = self.current_md().with_suffix(".away")
        self.current_md().rename(hidden)
        receipt = finalize.finalize(self.item)
        self.assertTrue(receipt["finalized"])
        self.assertEqual(receipt["pass_events"], 0)
        self.assertEqual(receipt["pass_events_reason"], "missing_transcript")
        self.assertEqual(self.outbox(INTAKE), [])
        hidden.rename(hidden.with_suffix(".md"))
        self.assertEqual(finalize.finalize(self.item)["pass_events"], 3)

    def test_runner_stamps_win_and_follow_the_title_slug_rename(self):
        stale = self.ticket("Ship it", transcript="20261008-101411-rec.md", item_id="someone-else",
                            transcription_id="someone-else", transcript_uri="file:///stale.md")
        self.add_emitter([stale])
        self.assertTrue(self.finalize_new()["finalized"])
        renamed = paths.VAULT / "20261008-101411-final-title.md"
        self.assertTrue(renamed.is_file())
        self.assertFalse(self.md.exists())
        (_, _, env), = self.outbox(INTAKE)
        data = env["data"]
        self.assertEqual(data["transcript"], "20261008-101411-final-title.md")
        self.assertEqual(data["transcript_uri"], renamed.resolve().as_uri())
        self.assertEqual(data["item_id"], self.item)
        self.assertEqual(data["transcription_id"], self.item)
        self.assertEqual(data["project_id"], "transcription-queue")
        self.assertEqual(data["project"], "wax")

    def test_intake_envelopes_validate_against_the_bloodbank_schema(self):
        self.finalize_new()
        validator = _intake_validator()
        rows = self.outbox(INTAKE)
        self.assertEqual(len(rows), 3)
        for _, _, env in rows:
            validator.validate(env)

    def test_completion_payload_carries_no_inline_events(self):
        receipt = self.finalize_new()
        (_, _, env), = self.outbox(COMPLETED)
        self.assertEqual(env["id"], receipt["event_id"])
        emitter = env["data"]["pass_results"]["emitter"]["result"]
        self.assertNotIn("events", emitter)
        self.assertEqual(emitter["frontmatter"], {"ticketable": [{"n": 3}]})
        self.assertIn("events", json.loads(self.pass_row("emitter")["result"]))

    def test_transaction_rolls_back_completion_and_tickets_together(self):
        for _ in passes.run_auto(self.item):
            pass
        before = len(self.outbox())
        with patch.object(ledger, "set_item_state", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(RuntimeError):
                finalize.finalize(self.item)
        self.assertEqual(len(self.outbox()), before)
        self.assertEqual(self.table("pass_events"), [])
        self.assertEqual(self.table("completions"), [])
        self.assertEqual(finalize.finalize(self.item)["pass_events"], 3)

    def test_unbuildable_and_oversized_events_are_dropped_without_raising(self):
        for _ in passes.run_auto(self.item):
            pass
        huge = {"type": "intake.detected", "key": "huge", "data": {"description": "x" * (1100 * 1024)}}
        nodot = {"type": "nodot", "key": "bad-type", "data": {}}
        ledger.connect().execute(
            "UPDATE passes SET result=? WHERE item_id=? AND ep_slug='emitter'",
            (json.dumps({"wax_ep_version": 1, "events": [huge, self.tickets[0], nodot]}), self.item))
        receipt = finalize.finalize(self.item)
        self.assertTrue(receipt["finalized"])
        self.assertEqual(receipt["pass_events"], 1)
        self.assertEqual(receipt["pass_events_dropped"], 2)
        self.assertEqual([env["data"]["description"] for _, _, env in self.outbox(INTAKE)],
                         ["zulu goes first"])

    def test_a_non_list_events_value_in_the_ledger_never_blocks_a_completion(self):
        for _ in passes.run_auto(self.item):
            pass
        # Damage a hand edit or an older runner could plausibly leave: `events`
        # that is not a list. (Unparseable result JSON already broke the
        # completion payload before this change and is out of scope.)
        ledger.connect().execute("UPDATE passes SET result=? WHERE item_id=? AND ep_slug='emitter'",
                                 (json.dumps({"wax_ep_version": 1, "events": {"oops": 1}}), self.item))
        receipt = finalize.finalize(self.item)
        self.assertTrue(receipt["finalized"])
        self.assertEqual(receipt["pass_events"], 0)

    def test_pass_events_table_migrates_onto_an_existing_ledger(self):
        ledger.connect().execute("DROP TABLE pass_events")
        self.close()
        tables = {row["name"] for row in ledger.connect().execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        self.assertIn("pass_events", tables)
        self.assertIn("pass_events_item", tables)
        self.close()
        ledger.connect()  # idempotent on the next connect too


class OrderingTest(PassEventsCase):
    def test_events_publish_in_plan_order_then_slug_then_list_order(self):
        # Alphabetical would be alpha, beta, zeta; the plan walks alpha's requirement first.
        emits = ["intake.detected"]
        self.add_pass("zeta-emitter", {"events": [self.ticket("zeta one", 1, 2), self.ticket("zeta two", 2, 2)]},
                      emits=emits)
        self.add_pass("alpha-emitter", {"events": [self.ticket("alpha one")]}, emits=emits,
                      requires=["zeta-emitter"])
        self.add_pass("beta-emitter", {"events": [self.ticket("beta one")]}, emits=emits)
        self.assertEqual(passes.ordered(passes.registry()), ["zeta-emitter", "alpha-emitter", "beta-emitter"])
        passes.run_auto(self.item)
        self.assertEqual(finalize.finalize(self.item)["pass_events"], 4)
        self.assertEqual([env["data"]["description"] for _, _, env in self.outbox(INTAKE)],
                         ["zeta one", "zeta two", "alpha one", "beta one"])

    def test_an_unorderable_plan_falls_back_to_alphabetical_instead_of_raising(self):
        plan = {"b": {"enabled": True, "after": ["ghost"]}, "a": {"enabled": True}}
        with self.assertRaises(passes.PassError):
            passes.ordered(plan)
        self.assertEqual(finalize._plan_order(plan), ["a", "b"])


class DependencyGateTest(PassEventsCase):
    def setUp(self):
        super().setUp()
        self.add_renamer()
        self.add_emitter([self.ticket("Ship it")])
        self.set_failing("renamer", True)

    def task_events_for(self, slug):
        return [env for _, subject, env in self.outbox()
                if ".task." in subject and env["data"].get("ep_slug") == slug]

    def test_a_gated_pass_records_a_real_attempt_and_says_why_in_the_note(self):
        first = passes.run_auto(self.item)
        by_slug = {outcome["ep_slug"]: outcome for outcome in first}
        self.assertEqual(by_slug["renamer"]["reason_code"], "provider_down")
        self.assertEqual(by_slug["emitter"]["reason_code"], "dependency_failed")
        row = self.pass_row("emitter")
        self.assertEqual((row["state"], row["attempt"], row["reason_code"]), ("failed", 1, "dependency_failed"))
        self.assertEqual(row["detail"], "waiting on renamer (failed@v1)")
        entry = self.note()[0]["wax"]["passes"]["emitter"]
        self.assertEqual({k: entry[k] for k in ("state", "version", "attempt", "reason_code", "detail")},
                         {"state": "failed", "version": 1, "attempt": 1, "reason_code": "dependency_failed",
                          "detail": "waiting on renamer (failed@v1)"})
        self.assertEqual(self.task_events_for("emitter"), [])
        self.assertNotEqual(self.task_events_for("renamer"), [])

        passes.run_auto(self.item)
        self.assertEqual(self.pass_row("emitter")["attempt"], 2)
        self.assertEqual(passes.run(self.item, "emitter")["attempt"], 3)
        self.assertEqual(self.pass_row("emitter")["attempt"], 3)

    def test_a_missing_dependency_row_reads_as_missing(self):
        outcome = passes.run(self.item, "emitter")
        self.assertEqual(outcome["reason_code"], "dependency_failed")
        self.assertEqual(outcome["error"], "waiting on renamer (missing)")

    def test_a_version_bumped_dependency_gates_its_dependents(self):
        self.set_failing("renamer", False)
        self.assertEqual(passes.run(self.item, "renamer")["state"], "completed")
        self.add_renamer(version=2)
        outcome = passes.run(self.item, "emitter")
        self.assertEqual(outcome["error"], "waiting on renamer (completed@v1, need v2)")

    def test_gate_failure_withholds_completion_and_the_fix_converges_with_one_attempt_trail(self):
        passes.run_auto(self.item)
        self.assertEqual(finalize.finalize(self.item)["reason_code"], "passes_incomplete")
        self.set_failing("renamer", False)
        outcomes = {o["ep_slug"]: o for o in passes.run_auto(self.item)}
        self.assertEqual(outcomes["renamer"]["state"], "completed")
        self.assertEqual(outcomes["emitter"]["state"], "completed")
        self.assertEqual(self.pass_row("emitter")["attempt"], 2, "a real run continues the counter")
        entry = frontmatter.read(self.current_md())[0]["wax"]["passes"]["emitter"]
        self.assertEqual(entry["state"], "completed")
        self.assertNotIn("reason_code", entry)
        self.assertNotIn("detail", entry)
        self.assertEqual(finalize.finalize(self.item)["pass_events"], 1)

    def test_dependency_failed_rows_stay_sweepable_after_their_dependency_is_exhausted(self):
        for _ in range(3):
            passes.run_auto(self.item)
        self.assertEqual((self.pass_row("renamer")["attempt"], self.pass_row("emitter")["attempt"]), (3, 3))
        targets = {(slug, attempt) for _, slug, attempt in ledger.failed_passes_for_sweep(3)}
        self.assertEqual(targets, {("emitter", 4)})

    def test_wax_ep_sweep_converges_once_the_dependency_is_repaired(self):
        cli = _load_cli()
        for _ in range(3):
            passes.run_auto(self.item)
        self.set_failing("renamer", False)
        out = cli.ep_sweep(max_attempts=3, slug=None, dry_run=False, limit=0)
        self.assertEqual(out["candidates"], 1)
        self.assertTrue(out["completions"][self.item]["finalized"])
        self.assertEqual(out["completions"][self.item]["pass_events"], 1)
        self.assertEqual(self.pass_row("renamer")["state"], "completed")
        self.assertEqual(self.pass_row("emitter")["state"], "completed")
        self.assertEqual(len(self.outbox(INTAKE)), 1)


_CLI = None


def _load_cli():
    """bin/wax as a module, so its sweep and doctor code run in-process."""
    global _CLI
    if _CLI is None:
        path = COMPONENT_ROOT / "bin" / "wax"
        loader = importlib.machinery.SourceFileLoader("wax_cli_under_test", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        _CLI = module
    return _CLI


class DoctorProbeTest(PassEventsCase):
    SNAPSHOT = {"projects": {"transcription-queue": {"name": "HeyMa"}, "bb": {"name": "Bloodbank"}},
                "__registry_status": {"transcription-queue": {"status": "ok"}}}

    def setUp(self):
        super().setUp()
        self.cli = _load_cli()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".project.json").write_text(json.dumps({"project_id": "transcription-queue"}))
        self.requested = []
        self.snapshot = copy.deepcopy(self.SNAPSHOT)
        self.registry_up = True
        self.key_payload = ({"data": {"is_provisioning_key": False, "limit_remaining": 0.5,
                                      "label": "sk-or-v1-LABEL-LEAK"}}, "")

    def get_json_direct(self, url, **_):
        self.requested.append(url)
        if not self.registry_up:
            return None, "URLError: connection refused"
        return ({"ok": True}, "") if url.endswith("/health") else (self.snapshot, "")

    def probe(self, *, enabled=True, env=None, key="SECRET-KEY-VALUE"):
        registry = {} if enabled is None else {"project-extraction": {"slug": "project-extraction",
                                                                      "enabled": enabled, "env": env or {}}}
        found = []
        with patch.object(passes, "registry", return_value=registry), \
                patch.object(self.cli, "_get_json_direct", side_effect=self.get_json_direct), \
                patch.object(self.cli, "_op_read", return_value=key) as op_read, \
                patch.object(self.cli, "_get_json", return_value=self.key_payload) as get_json:
            self.cli.probe_project_extraction(found.append, repo=self.repo, effective={}, yenv={})
        self.op_read, self.get_json = op_read, get_json
        return {p["probe"]: p for p in found}

    def test_registry_down_is_a_failure_only_when_the_pass_is_enabled(self):
        self.registry_up = False
        self.assertEqual(self.probe(enabled=True)["pjangler registry"]["status"], "fail")
        found = self.probe(enabled=False)
        self.assertEqual(found["pjangler registry"]["status"], "warn")
        self.assertNotIn("jev api key", found)

    def test_unregistered_heyma_fails_with_the_index_fix_and_never_pj_init(self):
        del self.snapshot["projects"]["transcription-queue"]
        probe = self.probe()["pjangler registry"]
        self.assertEqual(probe["status"], "fail")
        self.assertIn("-X POST", probe["fix"])
        self.assertIn("content-type:application/json", probe["fix"])
        self.assertIn('{"manifest_path":"%s"}' % (self.repo / ".project.json"), probe["fix"])
        self.assertIn("http://127.0.0.1:8764/v1/index", probe["fix"])
        self.assertNotIn("pj init", json.dumps(probe))

    def test_registry_with_heyma_passes_and_honours_pj_registry_url(self):
        found = self.probe(env={"PJ_REGISTRY_URL": "http://registry.test:9/"})
        self.assertEqual(found["pjangler registry"]["status"], "pass")
        self.assertEqual(self.requested, ["http://registry.test:9/health", "http://registry.test:9/v1/registry"])

    def test_pj_project_registry_wins_over_pj_registry_url_as_it_does_in_pj(self):
        self.probe(env={"PJ_PROJECT_REGISTRY": "http://first.test:1", "PJ_REGISTRY_URL": "http://second.test:2"})
        self.assertEqual(self.requested, ["http://first.test:1/health", "http://first.test:1/v1/registry"])

    def test_a_fixture_path_registry_location_is_reported_not_fetched(self):
        found = self.probe(env={"PJ_PROJECT_REGISTRY": "/tmp/fixture.yaml"})
        self.assertEqual(found["pjangler registry"]["status"], "fail")
        self.assertEqual(self.requested, [])

    def test_registered_but_unindexed_heyma_is_not_a_pass(self):
        self.snapshot["__registry_status"]["transcription-queue"]["status"] = "invalid"
        self.assertEqual(self.probe()["pjangler registry"]["status"], "fail")

    def test_jev_key_must_be_an_inference_key_with_budget_and_is_never_printed(self):
        found = self.probe(env={"WAX_JEV_API_KEY_OP": "op://vault/item/field"})
        self.assertEqual(found["jev api key"]["status"], "pass")
        self.op_read.assert_called_once_with("op://vault/item/field")
        self.assertEqual(self.get_json.call_args.args[0], "https://openrouter.ai/api/v1/key")
        self.key_payload = ({"data": {"is_provisioning_key": True, "limit_remaining": None}}, "")
        self.assertEqual(self.probe()["jev api key"]["status"], "fail")
        self.key_payload = ({"data": {"is_provisioning_key": False, "limit_remaining": 0}}, "")
        self.assertEqual(self.probe()["jev api key"]["status"], "fail")
        self.key_payload = ({"data": {"is_provisioning_key": False, "limit_remaining": None}}, "")
        self.assertEqual(self.probe()["jev api key"]["status"], "pass")
        self.key_payload = (None, "HTTP 401: User not found.")
        failed = self.probe()["jev api key"]
        self.assertEqual(failed["status"], "fail")
        for text in (json.dumps(self.probe()), json.dumps(failed)):
            self.assertNotIn("SECRET-KEY-VALUE", text)
            self.assertNotIn("LABEL-LEAK", text)

    def test_a_literal_jev_key_is_probed_in_preference_to_the_reference_and_warned_about(self):
        found = self.probe(env={"WAX_JEV_API_KEY": "LITERAL-KEY-VALUE"})
        self.assertEqual(found["jev api key"]["status"], "warn")
        self.op_read.assert_not_called()
        self.assertEqual(self.get_json.call_args.kwargs["token"], "LITERAL-KEY-VALUE")
        self.assertNotIn("LITERAL-KEY-VALUE", json.dumps(found))

    def test_unresolvable_jev_key_fails_without_calling_openrouter(self):
        found = self.probe(key="")
        self.assertEqual(found["jev api key"]["status"], "fail")
        self.get_json.assert_not_called()

    def test_jev_probe_is_skipped_when_the_pass_is_absent_or_disabled(self):
        for enabled in (None, False):
            self.assertNotIn("jev api key", self.probe(enabled=enabled))
        self.get_json.assert_not_called()

    def test_pending_pass_event_report_tracks_finalized_items_only(self):
        self.add_renamer()
        self.add_emitter([self.ticket("One", 1, 2), self.ticket("Two", 2, 2)])
        passes.run_auto(self.item)
        self.assertEqual(self.cli.pending_pass_event_report(), [], "not finalized: not due yet")
        self.assertEqual(finalize.finalize(self.item)["pass_events"], 2)
        self.assertEqual(self.cli.pending_pass_event_report(), [])
        ledger.connect().execute("DELETE FROM pass_events WHERE rowid=1")
        self.assertEqual(self.cli.pending_pass_event_report(), [(self.item, "emitter", 1)])
        self.assertEqual(finalize.finalize(self.item)["pass_events"], 1)
        self.assertEqual(self.cli.pending_pass_event_report(), [])


if __name__ == "__main__":
    unittest.main()
