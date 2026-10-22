"""Conditional chains: `when:` clauses, registry quarantine, exhaustion dead-letters,
schema-enforced emission, and subgraph-gated early publication.

Same fixture discipline as pass_events_test.py: a temp WAX_ROOT ledger, stub pass
executables printing canned JSON, a real outbox table, and nothing reaching the
live ledger, the vault, NATS or a provider.
"""

import hashlib
import json
import shlex
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import yaml

from wax import events, finalize, frontmatter, ledger, passes, paths, pipelines, sentinel

COMPLETED = "bloodbank.evt.audio.transcription.completed"
INTAKE = "bloodbank.evt.audio.intake.detected"
EXHAUSTED = "bloodbank.evt.audio.task.exhausted"


def _fake_frontmatters(args):
    if args[0] != "set":
        return
    updates = {}
    for pair in args[2:]:
        key, raw = pair.split("=", 1)
        updates[key] = json.loads(raw)
    frontmatter.merge(Path(args[1]), updates)


class ChainCase(unittest.TestCase):
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
        self.body = "\n# Transcript\nSome words about a project.\n"
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

    # ---- helpers -----------------------------------------------------------

    def add_pass(self, slug, result=None, *, fails=False, **definition):
        script = self.registry_dir / f"{slug}.sh"
        out = self.registry_dir / f"{slug}.out"
        if fails:
            script.write_text("#!/bin/sh\necho reason_code=provider_down >&2\necho 'upstream said no' >&2\nexit 1\n")
        else:
            out.write_text(json.dumps({"wax_ep_version": 1, **(result or {})}) + "\n")
            script.write_text(f"#!/bin/sh\ncat {shlex.quote(str(out))}\n")
        script.chmod(0o755)
        base = {"slug": slug, "version": 1, "enabled": True, "auto": True,
                "command": [str(script), "{md_path}"]}
        base.update(definition)
        (self.registry_dir / f"{slug}.yaml").write_text(yaml.safe_dump(base))

    def write_yaml(self, slug, text):
        (self.registry_dir / f"{slug}.yaml").write_text(text)

    def outbox(self, *subjects):
        rows = ledger.connect().execute("SELECT id, subject, envelope FROM outbox ORDER BY id").fetchall()
        return [(row["id"], row["subject"], json.loads(row["envelope"])) for row in rows
                if not subjects or row["subject"] in subjects]

    def pass_row(self, slug):
        row = ledger.connect().execute("SELECT * FROM passes WHERE item_id=? AND ep_slug=?",
                                       (self.item, slug)).fetchone()
        return dict(row) if row else None

    def ticket(self, description, index=1, count=1, **data):
        digest = hashlib.sha256(description.encode()).hexdigest()[:12]
        intake_id = f"{self.item}:{digest}"
        return {"type": "intake.detected", "key": intake_id,
                "data": {"project_id": "transcription-queue", "project_name": "HeyMa",
                         "description": description, "intake_id": intake_id,
                         "index": index, "count": count, **data}}


class WhenConditionsTest(ChainCase):
    def test_all_operators(self):
        fm = {"classification": "meeting", "project-ids": ["a", "b"], "title": "x", "empty-list": []}
        cases = [
            ({"field": "classification", "equals": "meeting"}, True),
            ({"field": "classification", "equals": "monolog"}, False),
            ({"field": "classification", "not_equals": "monolog"}, True),
            ({"field": "classification", "in": ["meeting", "other"]}, True),
            ({"field": "classification", "not_in": ["monolog"]}, True),
            ({"field": "title", "exists": True}, True),
            ({"field": "missing", "exists": True}, False),
            ({"field": "missing", "absent": True}, True),
            ({"field": "empty-list", "empty": True}, True),
            ({"field": "project-ids", "not_empty": True}, True),
            ({"field": "project-ids", "length": 2}, True),
            ({"field": "project-ids", "min_length": 2}, True),
            ({"field": "project-ids", "min_length": 3}, False),
            ({"field": "project-ids", "max_length": 1}, False),
            ({"field": "missing", "length": 0}, True),
        ]
        for clause, holds in cases:
            with self.subTest(clause=clause):
                failure = passes._clause_failure(clause, fm)
                self.assertEqual(failure is None, holds, failure)

    def test_unmet_condition_skips_with_detail_and_satisfies_requires(self):
        self.add_pass("producer", {"frontmatter": {"project-ids": ["only-one"]}})
        self.add_pass("fanout", requires=["producer"],
                      when=[{"field": "project-ids", "min_length": 2}])
        results = {r["ep_slug"]: r for r in passes.run_auto(self.item)}
        self.assertEqual(results["producer"]["state"], "completed")
        self.assertEqual(results["fanout"]["state"], "skipped")
        self.assertEqual(results["fanout"]["reason_code"], "condition_unmet")
        self.assertIn("min_length", results["fanout"]["detail"])
        # A skip satisfies the gate: a dependent of the skipped pass runs.
        self.add_pass("downstream", requires=["fanout"])
        self.assertEqual(passes.run(self.item, "downstream")["state"], "completed")
        # And the skip is on the note where people actually look.
        fm, _ = frontmatter.read(self.md)
        self.assertEqual(fm["wax"]["passes"]["fanout"]["reason_code"], "condition_unmet")

    def test_met_condition_runs(self):
        self.add_pass("producer", {"frontmatter": {"project-ids": ["a", "b"]}})
        self.add_pass("fanout", requires=["producer"],
                      when=[{"field": "project-ids", "min_length": 2}])
        results = {r["ep_slug"]: r for r in passes.run_auto(self.item)}
        self.assertEqual(results["fanout"]["state"], "completed")

    def test_legacy_skip_when_still_works(self):
        self.add_pass("producer", {"frontmatter": {"classification": "monolog"}})
        self.add_pass("diarize-ish", requires=["producer"],
                      skip_when={"field": "classification", "equals": "monolog"})
        results = {r["ep_slug"]: r for r in passes.run_auto(self.item)}
        self.assertEqual(results["diarize-ish"]["state"], "skipped")
        self.assertEqual(results["diarize-ish"]["reason_code"], "condition_matched")

    def test_invalid_when_quarantines_only_that_pass(self):
        self.add_pass("healthy", {"frontmatter": {"ok": True}})
        self.add_pass("broken", when=[{"field": "x", "bogus_op": 1}])
        bad = passes.quarantined(passes.registry())
        self.assertIn("broken", bad)
        self.assertNotIn("healthy", bad)
        with self.assertRaises(passes.PassError) as ctx:
            passes.run(self.item, "broken")
        self.assertIn("quarantined", str(ctx.exception))
        self.assertEqual(passes.run(self.item, "healthy")["state"], "completed")


class QuarantineTest(ChainCase):
    def test_unknown_dependency_quarantines_dependent_not_fleet(self):
        self.add_pass("healthy", {"frontmatter": {"ok": True}})
        self.write_yaml("dangling", "slug: dangling\nenabled: true\nauto: true\n"
                        "requires: [ghost]\ncommand: [/bin/true]\n")
        self.assertEqual(passes.ordered(passes.registry()), ["healthy"])
        results = {r["ep_slug"]: r for r in passes.run_all(self.item)}
        self.assertEqual(results["healthy"]["state"], "completed")
        self.assertEqual(results["dangling"]["state"], "quarantined")
        self.assertIn("ghost", results["dangling"]["error"])
        # No ledger row: nothing ran, so there is nothing for the sweep to retry.
        self.assertIsNone(self.pass_row("dangling"))

    def test_cycle_quarantines_members_only(self):
        self.add_pass("healthy", {"frontmatter": {"ok": True}})
        self.write_yaml("cycle-a", "slug: cycle-a\nenabled: true\nrequires: [cycle-b]\ncommand: [/bin/true]\n")
        self.write_yaml("cycle-b", "slug: cycle-b\nenabled: true\nrequires: [cycle-a]\ncommand: [/bin/true]\n")
        bad = passes.quarantined(passes.registry())
        self.assertEqual({s for s in bad}, {"cycle-a", "cycle-b"})
        self.assertEqual(passes.ordered(passes.registry()), ["healthy"])

    def test_dependent_of_quarantined_is_quarantined_and_fix_lifts_live(self):
        self.write_yaml("dangling", "slug: dangling\nenabled: true\nauto: true\n"
                        "requires: [ghost]\ncommand: [/bin/true]\n")
        self.write_yaml("dependent", "slug: dependent\nenabled: true\nauto: true\n"
                        "requires: [dangling]\ncommand: [/bin/true]\n")
        bad = passes.quarantined(passes.registry())
        self.assertEqual({s for s in bad}, {"dangling", "dependent"})
        # Fixing the YAML lifts the quarantine with no restart: the registry is
        # re-read on every call.
        self.write_yaml("dangling", "slug: dangling\nenabled: true\nauto: true\n"
                        "requires: []\ncommand: [/bin/true]\n")
        self.assertEqual(passes.quarantined(passes.registry()), {})

    def test_disabled_dependency_quarantines_the_dependent(self):
        self.add_pass("off-duty")
        self.write_yaml("off-duty", "slug: off-duty\nenabled: false\ncommand: [/bin/true]\n")
        self.write_yaml("needy", "slug: needy\nenabled: true\nrequires: [off-duty]\ncommand: [/bin/true]\n")
        self.assertEqual(passes.quarantined(passes.registry()),
                         {"needy": "dependency 'off-duty' is disabled"})


class ExhaustionTest(ChainCase):
    def test_final_attempt_fires_dead_letter_exactly_once(self):
        self.add_pass("doomed", fails=True)
        for expected_attempt in (1, 2, 3):
            outcome = passes.run(self.item, "doomed")
            self.assertEqual(outcome["state"], "failed")
            self.assertEqual(self.pass_row("doomed")["attempt"], expected_attempt)
        fired = self.outbox(EXHAUSTED)
        self.assertEqual(len(fired), 1)
        data = fired[0][2]["data"]
        self.assertEqual((data["ep_slug"], data["attempt"], data["reason_code"]),
                         ("doomed", 3, "provider_down"))
        self.assertEqual(fired[0][2]["type"], "bloodbank.audio.task.exhausted")
        # Attempt 4 (a manual re-run past the bound) does not re-fire.
        passes.run(self.item, "doomed")
        self.assertEqual(len(self.outbox(EXHAUSTED)), 1)

    def test_recovery_clears_the_exhausted_receipt_but_a_rerun_in_flight_keeps_it(self):
        self.add_pass("doomed", fails=True)
        for _ in range(3):
            passes.run(self.item, "doomed")
        self.assertIsNotNone(self.pass_row("doomed")["exhausted_at"])
        # running and failed keep the receipt: a re-run in flight has not
        # recovered, and a repeat failure must not re-announce.
        passes._record(self.item, "doomed", "running", attempt=4)
        self.assertIsNotNone(self.pass_row("doomed")["exhausted_at"])
        passes.run(self.item, "doomed")
        self.assertIsNotNone(self.pass_row("doomed")["exhausted_at"])
        self.assertEqual(len(self.outbox(EXHAUSTED)), 1)
        # Completing clears it, so catalog stops counting the row as exhausted.
        self.add_pass("doomed")
        self.assertEqual(passes.run(self.item, "doomed")["state"], "completed")
        self.assertIsNone(self.pass_row("doomed")["exhausted_at"])
        # A skip is also a non-failed terminal state.
        ledger.connect().execute("UPDATE passes SET exhausted_at='x' WHERE ep_slug='doomed'")
        passes._record(self.item, "doomed", "skipped", attempt=7, reason_code="condition_unmet")
        self.assertIsNone(self.pass_row("doomed")["exhausted_at"])

    def test_sweep_exhaustions_covers_prehook_rows_and_version_bump_rearms(self):
        self.add_pass("doomed", fails=True)
        for _ in range(3):
            passes.run(self.item, "doomed")
        # Simulate a row written before the run()-time hook: clear the receipt.
        ledger.connect().execute("UPDATE passes SET exhausted_at=NULL WHERE ep_slug='doomed'")
        ledger.connect().execute("DELETE FROM outbox WHERE subject=?", (EXHAUSTED,))
        fired = passes.sweep_exhaustions(3)
        self.assertEqual(fired, [{"item_id": self.item, "ep_slug": "doomed", "attempt": 3}])
        self.assertEqual(len(self.outbox(EXHAUSTED)), 1)
        self.assertEqual(passes.sweep_exhaustions(3), [])
        # A version bump is a new definition: _record clears the receipt when the
        # version flips, so the new definition's next exhaustion announces afresh.
        # (Attempt itself is lifetime-per-pair — it is the command_id entropy, so
        # it can never reset.)
        self.add_pass("doomed", fails=True, version=2)
        passes.run(self.item, "doomed")
        self.assertEqual(self.pass_row("doomed")["version"], 2)
        self.assertEqual(len(self.outbox(EXHAUSTED)), 2)


class EventSchemaTest(ChainCase):
    def test_schema_violation_fails_the_pass_before_mutation(self):
        bad_ticket = self.ticket("Ship it")
        bad_ticket["data"]["project_id"] = "NOT_A_SLUG"
        self.add_pass("emitter", {"frontmatter": {"leak-check": "must not land"},
                                  "events": [bad_ticket]},
                      emits=["intake.detected"], requires=[])
        outcome = passes.run(self.item, "emitter")
        self.assertEqual(outcome["state"], "failed")
        self.assertEqual(outcome["reason_code"], "result_apply_failed")
        self.assertIn("schema", outcome["error"])
        fm, body = frontmatter.read(self.md)
        self.assertNotIn("leak-check", fm)
        self.assertEqual(body, self.body)
        self.assertEqual(self.outbox(INTAKE), [])

    def test_missing_schema_fails_closed(self):
        event = {"type": "thing.happened", "key": "k1", "data": {"x": 1}}
        self.add_pass("emitter", {"events": [event]}, emits=["thing.happened"], requires=[])
        outcome = passes.run(self.item, "emitter")
        self.assertEqual(outcome["state"], "failed")
        self.assertEqual(outcome["reason_code"], "result_apply_failed")
        self.assertIn("schema-first", outcome["error"])

    def test_valid_events_still_pass_and_publish(self):
        self.add_pass("emitter", {"events": [self.ticket("Ship it")]},
                      emits=["intake.detected"], requires=[])
        self.assertEqual(passes.run(self.item, "emitter")["state"], "completed")
        self.assertEqual(finalize.publish_pass_events(self.item)["pass_events"], 1)
        self.assertEqual(len(self.outbox(INTAKE)), 1)


class EarlyPublicationTest(ChainCase):
    def test_events_publish_when_the_subgraph_is_done_not_the_plan(self):
        self.add_pass("renamer", {"frontmatter": {"title-slug": "final-title"},
                                  "transcript": {"slug": "final-title"}})
        self.add_pass("emitter", {"events": [self.ticket("Ship it")]},
                      emits=["intake.detected"], requires=["renamer"])
        self.add_pass("slowpoke", fails=True)  # the wedged sibling
        passes.run_auto(self.item)
        # The plan is NOT complete: slowpoke failed, so no completion event.
        self.assertEqual(self.outbox(COMPLETED), [])
        published = finalize.publish_pass_events(self.item)
        self.assertEqual(published["pass_events"], 1)
        intake = self.outbox(INTAKE)
        self.assertEqual(len(intake), 1)
        # No completion exists yet, so correlation falls back to the pass's command.
        command_id = self.pass_row("emitter")["command_id"]
        self.assertEqual(intake[0][2]["correlationid"], command_id)
        self.assertEqual(intake[0][2]["data"]["transcript"], "20261008-101411-final-title.md")
        # finalize stays gated on the plan, and its later enqueue is a dedup no-op.
        self.assertFalse(finalize.finalize(self.item)["finalized"])
        self.set_failing_fixed = (self.registry_dir / "slowpoke.sh")
        self.add_pass("slowpoke", {})  # same slug, now succeeding, same version
        passes.run(self.item, "slowpoke")
        outcome = finalize.finalize(self.item)
        self.assertTrue(outcome["finalized"])
        self.assertEqual(len(self.outbox(INTAKE)), 1)
        self.assertEqual(len(self.outbox(COMPLETED)), 1)

    def test_unmet_requires_withhold_events(self):
        self.add_pass("renamer", fails=True)
        self.add_pass("emitter", {"events": [self.ticket("Ship it")]},
                      emits=["intake.detected"], requires=["renamer"])
        passes.run_auto(self.item)
        self.assertEqual(self.pass_row("emitter")["reason_code"], "dependency_failed")
        published = finalize.publish_pass_events(self.item)
        self.assertEqual(published.get("pass_events", 0), 0)
        self.assertEqual(self.outbox(INTAKE), [])


class CompiledPlanAuthorityTest(ChainCase):
    """The compiled transcription pipeline freezes the audio plan: dependency
    gating replays against the PLAN's versions and definitions, not the live
    registry's, and the compiled halt policy stops the walk on failure."""

    def setUp(self):
        super().setUp()
        self.pipes = self.root / "pipelines.d"
        self.pipes.mkdir(exist_ok=True)
        self.stack.enter_context(patch.object(pipelines, "PIPELINES_DIR", self.pipes))
        self.close()

    def tearDown(self):
        self.close()
        super().tearDown()

    def _compile(self, name: str, *, policy: str = "independent",
                 passes: tuple[str, ...] | None = None) -> None:
        (self.pipes / f"{name}.yaml").write_text(yaml.safe_dump({
            "name": name, "version": 1, "description": name,
            "trigger": "transcription", "enabled": True,
            "failure_policy": policy,
            "passes": list(passes or ("dep", "dependent")),
        }))
        out = pipelines.compile([name])
        self.assertFalse(out["failed"], out["failed"])

    def test_plan_gate_uses_frozen_versions_not_the_live_registry(self):
        self.add_pass("dep", fails=True)
        self.add_pass("dependent", requires=["dep"])
        self._compile("tplan")
        # dep fails: the dependent is dependency-blocked
        results = {r.get("ep_slug"): r for r in passes.run_auto(self.item)}
        self.assertEqual(results["dep"]["state"], "failed")
        self.assertEqual(results["dependent"]["reason_code"], "dependency_failed")
        # the provider recovers, then the dependent fails on its own merits
        self.add_pass("dep", {"frontmatter": {"ok": True}})
        self.add_pass("dependent", fails=True)
        results = {r.get("ep_slug"): r for r in passes.run_auto(self.item)}
        self.assertEqual(results["dep"]["state"], "completed")
        self.assertEqual(results["dependent"]["state"], "failed")
        from wax import worker
        # the live registry moves on (dep v2) while the plan stays frozen at v1
        self.add_pass("dep", {"frontmatter": {"ok": True}}, version=2)
        self.add_pass("dependent", {"frontmatter": {"ok": True}})
        swept = worker.sweep_failed_passes()
        dependent = next(r for r in swept["results"] if r["ep_slug"] == "dependent")
        self.assertNotEqual(dependent.get("reason_code"), "dependency_failed",
                            "the frozen plan gates on frozen versions")
        self.assertEqual(dependent["state"], "completed")
        # and the plan never re-minted from the drifted registry
        provenance = passes._plan_provenance(self.item)
        self.assertEqual(provenance.get("kind"), "pipeline")

    def test_compiled_halt_policy_stops_the_audio_plan(self):
        self.add_pass("first", fails=True)
        self.add_pass("second", {"frontmatter": {"ok": True}})
        self._compile("halter", policy="halt", passes=("first", "second"))
        results = passes.run_auto(self.item)
        by_slug = {r.get("ep_slug"): r for r in results}
        self.assertEqual(by_slug["first"]["state"], "failed")
        halted = next(r for r in results if r.get("reason_code") == "halt_policy")
        self.assertEqual(halted["halted_after"], "first")
        # the tail never ran: no ledger row, no note entry
        self.assertIsNone(self.pass_row("second"))
        # recovery lifts the halt: the tail runs on the next drive
        self.add_pass("first", {"frontmatter": {"ok": True}})
        results = passes.run_auto(self.item)
        by_slug = {r.get("ep_slug"): r for r in results}
        self.assertEqual(by_slug["first"]["state"], "completed")
        self.assertEqual(by_slug["second"]["state"], "completed")

    def test_sweep_replays_the_frozen_definition_not_the_live_registry(self):
        self.add_pass("dep", {"frontmatter": {"ok": True}})
        self.add_pass("dependent", fails=True)
        self._compile("tplan")
        passes.run_auto(self.item)  # dep completed; dependent failed@v1
        # the live registry moves on: v2 with a DIFFERENT command payload
        self.add_pass("dependent", {"frontmatter": {"marker": "live-v2"}}, version=2)
        from wax import worker
        swept = worker.sweep_failed_passes()
        dependent = next(r for r in swept["results"] if r["ep_slug"] == "dependent")
        self.assertEqual(dependent["version"], 1,
                         "the sweep replays the plan's frozen definition")
        fm, _ = frontmatter.read(self.md)
        self.assertNotIn("marker", fm.get("wax", {}).get("passes", {})
                         .get("dependent", {}),
                         "the live v2 payload must not run through the frozen plan")
        self.assertEqual(self.pass_row("dependent")["version"], 1)


class SweepSpendsOneAttemptTest(ChainCase):
    """One sweep (or one non-sweep re-run) spends at most ONE attempt per (item, slug).

    Regression: items 126d791b0a40654e / 9916aa1c94507b0b -- a provider outage
    burned all three attempts of a chat pass within seconds. The sweep's target
    loop retried each failed pass, then its follow-up run_auto() retried every
    failed pass again, and the scheduled sweep fired right after the first
    failure with no spacing at all.
    """

    def _age_rows(self, seconds):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        ledger.connect().execute("UPDATE passes SET updated_at=? WHERE item_id=?", (old, self.item))

    def test_a_sweep_spends_one_attempt_per_failed_pass(self):
        from wax import worker
        self.add_pass("classification", fails=True)
        self.add_pass("title-slug", fails=True)
        passes.run_auto(self.item)
        self.assertEqual((self.pass_row("classification")["attempt"], self.pass_row("title-slug")["attempt"]), (1, 1))
        swept = worker.sweep_failed_passes()
        self.assertEqual(swept["candidates"], 2)
        self.assertEqual((self.pass_row("classification")["attempt"], self.pass_row("title-slug")["attempt"]), (2, 2),
                         "the follow-up run_auto must not retry what the target loop just retried")
        self.assertIsNone(self.pass_row("classification")["exhausted_at"])

    def test_three_ticks_exhaust_in_exactly_three_attempts(self):
        from wax import worker
        self.add_pass("title-slug", fails=True)
        passes.run_auto(self.item)
        for _ in range(2):
            worker.sweep_failed_passes()
        self.assertEqual(self.pass_row("title-slug")["attempt"], 3)
        self.assertEqual(worker.sweep_failed_passes()["candidates"], 0)
        self.assertEqual(self.pass_row("title-slug")["attempt"], 3)

    def test_min_age_spaces_attempts(self):
        from wax import worker
        self.add_pass("title-slug", fails=True)
        passes.run_auto(self.item)
        # Just failed: a scheduled sweep (spacing = its interval) leaves it alone.
        swept = worker.sweep_failed_passes(min_age_s=900)
        self.assertEqual(swept["candidates"], 0)
        self.assertEqual(self.pass_row("title-slug")["attempt"], 1)
        # The operator's on-demand sweep (no spacing) is not held back.
        self.assertEqual(worker.sweep_failed_passes()["candidates"], 1)
        self.assertEqual(self.pass_row("title-slug")["attempt"], 2)
        # Once the last attempt is older than the spacing, it is eligible again.
        self._age_rows(901)
        self.assertEqual(worker.sweep_failed_passes(min_age_s=900)["candidates"], 1)
        self.assertEqual(self.pass_row("title-slug")["attempt"], 3)

    def test_followup_walk_does_not_retry_a_failed_pass_inside_the_spacing(self):
        from wax import worker
        self.add_pass("classification", fails=True)
        self.add_pass("title-slug", fails=True)
        passes.run_auto(self.item)
        # classification is old enough to sweep; title-slug failed a moment ago.
        ledger.connect().execute(
            "UPDATE passes SET updated_at='2000-01-01T00:00:00.000Z' WHERE item_id=? AND ep_slug='classification'",
            (self.item,))
        swept = worker.sweep_failed_passes(min_age_s=900)
        self.assertEqual([t["ep_slug"] for t in swept["targets"]], ["classification"])
        self.assertEqual(self.pass_row("classification")["attempt"], 2)
        self.assertEqual(self.pass_row("title-slug")["attempt"], 1, "title-slug keeps its spacing")


if __name__ == "__main__":
    unittest.main()
