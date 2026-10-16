"""Pipelines: explicit compile, drift identity, and graph-failure semantics.

The I/O matrix, as tests:
- A fails, B consumes A  -> B blocked (dependency_failed), independent C runs.
- B only ordered after A -> both outcomes tracked regardless of A's success.
- a halt policy stops the walk at the first failure.
- authoring changes expose drift; nothing runs until recompiled.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import documents, events, frontmatter, ledger, passes, paths, pipelines, sentinel


def _close_test_connection() -> None:
    connection = getattr(ledger._local, "conn", None)
    if connection is not None:
        connection.close()
        del ledger._local.conn


def _fake_frontmatters(args):
    if args[0] != "set":
        return
    note = Path(args[1])
    updates = {}
    for pair in args[2:]:
        key, raw = pair.split("=", 1)
        updates[key] = json.loads(raw)
    frontmatter.merge(note, updates)


def _pass(directory: Path, slug: str, *, result: dict | None, fail: bool = False,
          requires: list | None = None, after: list | None = None,
          auto: bool = False, version: int = 1,
          capabilities: str = "[document]") -> None:
    script = directory / slug
    if fail:
        script.write_text("#!/bin/sh\necho 'reason_code=provider_down' >&2\nexit 1\n")
    else:
        payload = {"wax_ep_version": 1, "frontmatter": {"summary": f"{slug} summary"}}
        if result is not None:
            payload.update(result)
        script.write_text("#!/bin/sh\nprintf '%s\\n' '" + json.dumps(payload) + "'\n")
    script.chmod(0o755)
    definition = (
        f"slug: {slug}\nversion: {version}\ndescription: {slug} pass\n"
        f"enabled: true\nauto: {str(auto).lower()}\ncapabilities: {capabilities}\n"
        f"requires: {json.dumps(requires or [])}\nafter: {json.dumps(after or [])}\n"
        f"command: [{json.dumps(str(script))}, \"{{md_path}}\"]\n"
    )
    (directory / f"{slug}.yaml").write_text(definition)


class _Graph:
    """A/B/C document graph: B requires A, C independent, D after A."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.registry = self.root / "passes.d"
        self.pipes = self.root / "pipelines.d"
        self.docs = self.root / "docs"
        for d in (self.runtime, self.registry, self.pipes, self.docs):
            d.mkdir()
        for slug in ("a", "b", "c", "d"):
            requires = ["a"] if slug == "b" else None
            after = ["a"] if slug == "d" else None
            _pass(self.registry, slug, result=None, requires=requires, after=after)

    def write_pipeline(self, passes, name: str = "graph", **extra) -> None:
        doc = {
            "name": name,
            "version": 1,
            "description": "test graph",
            "trigger": "manual",
            "enabled": True,
            "failure_policy": "independent",
            "passes": passes,
        }
        doc.update(extra)
        (self.pipes / f"{name}.yaml").write_text(json.dumps(doc))

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
        for p in self.stack:
            p.start()
        _close_test_connection()
        return self

    def __exit__(self, *a):
        for p in self.stack:
            p.stop()
        _close_test_connection()
        self.tmp.cleanup()

    def doc(self, name: str = "note.md") -> Path:
        path = self.docs / name
        path.write_text(f"---\ntitle: {name}\n---\nBody {name}.\n")
        return path

    def break_pass(self, slug: str) -> None:
        _pass(self.registry, slug, result=None, fail=True,
              requires=["a"] if slug == "b" else None,
              after=["a"] if slug == "d" else None)


class PipelineCompileTest(unittest.TestCase):
    def test_compile_rejects_unknown_passes_triggers_and_cycles(self):
        with _Graph() as g:
            g.write_pipeline(["a", "nope"])
            out = pipelines.compile(["graph"])
            self.assertIn("unknown pass", out["failed"]["graph"])

            g.write_pipeline(["a"], trigger="portal")
            out = pipelines.compile(["graph"])
            self.assertIn("trigger must be one of", out["failed"]["graph"])

            g.write_pipeline(["a"], trigger="documents")  # no folders
            out = pipelines.compile(["graph"])
            self.assertIn("requires at least one folder", out["failed"]["graph"])

            # a -> b (b requires a) plus a fake a-after-b cycle
            _pass(g.registry, "a", result=None, after=["b"])
            g.write_pipeline(["a", "b"])
            out = pipelines.compile(["graph"])
            self.assertIn("cyclic", out["failed"]["graph"])

    def test_compile_is_explicit_and_drift_is_visible(self):
        with _Graph() as g:
            g.write_pipeline(["a", "b"])
            first = pipelines.compile(["graph"])
            self.assertEqual(len(first["compiled"]), 1)
            definition_id = first["compiled"][0]["definition_id"]
            # recompile without changes is an explicit no-op
            again = pipelines.compile(["graph"])
            self.assertEqual(again["unchanged"][0]["definition_id"], definition_id)
            # authoring a change does NOT silently replace the authority
            g.write_pipeline(["a", "b", "c"], version=2)
            self.assertEqual(pipelines.drift("graph")["state"], "drifted")
            current = pipelines.compiled()["graph"]["definition_id"]
            self.assertEqual(current, definition_id)
            # the edited graph still runs the COMPILED authority until recompiled
            run = pipelines.run_document("graph", g.doc())
            self.assertEqual({r["ep_slug"] for r in run["results"]}, {"a", "b"})
            # explicit recompile adopts it
            pipelines.compile(["graph"])
            self.assertEqual(pipelines.drift("graph")["state"], "in_sync")
            run = pipelines.run_document("graph", g.doc("second.md"))
            self.assertEqual({r["ep_slug"] for r in run["results"]}, {"a", "b", "c"})

    def test_disabled_pipeline_never_runs(self):
        with _Graph() as g:
            g.write_pipeline(["a"], enabled=False)
            pipelines.compile(["graph"])
            run = pipelines.run_document("graph", g.doc())
            self.assertIn("not enabled", run["skipped"])
            self.assertNotIn("results", run)


class PipelineFailurePolicyTest(unittest.TestCase):
    def test_failed_dependency_blocks_dependent_but_not_independents(self):
        with _Graph() as g:
            g.write_pipeline(["a", "b", "c"])
            pipelines.compile(["graph"])
            g.break_pass("a")
            run = pipelines.run_document("graph", g.doc())
            states = {r["ep_slug"]: r for r in run["results"]}
            self.assertEqual(states["a"]["state"], "failed")
            self.assertEqual(states["b"]["reason_code"], "dependency_failed")
            self.assertEqual(states["c"]["state"], "completed")
            # the failed run withheld the completed receipt: the durable
            # receipt is 'failed' (retryable) and the document is not processed
            receipt = documents.receipt("", run["doc_id"], "graph", run["definition_id"])
            self.assertEqual(receipt["state"], "failed")
            self.assertEqual(documents.get(run["doc_id"])["state"], "failed")

    def test_after_only_orders_and_survives_failure(self):
        with _Graph() as g:
            g.write_pipeline(["a", "d"])
            pipelines.compile(["graph"])
            g.break_pass("a")
            run = pipelines.run_document("graph", g.doc())
            states = {r["ep_slug"]: r for r in run["results"]}
            self.assertEqual(states["a"]["state"], "failed")
            # `after` never gates: D ran anyway and its outcome is tracked
            self.assertEqual(states["d"]["state"], "completed")

    def test_halt_policy_stops_the_walk_at_first_failure(self):
        with _Graph() as g:
            g.write_pipeline(["a", "c"], failure_policy="halt")
            pipelines.compile(["graph"])
            g.break_pass("a")
            run = pipelines.run_document("graph", g.doc())
            self.assertTrue(run["halted"])
            # c never ran at all: alphabetical order puts a first, halt stops c
            self.assertEqual([r["ep_slug"] for r in run["results"]], ["a"])

    def test_per_node_condition_overrides_for_this_graph(self):
        with _Graph() as g:
            g.write_pipeline([{"slug": "a", "when": [{"field": "title", "equals": "never"}]}])
            pipelines.compile(["graph"])
            run = pipelines.run_document("graph", g.doc())
            a = next(r for r in run["results"] if r["ep_slug"] == "a")
            self.assertEqual(a["state"], "skipped")
            self.assertEqual(a["reason_code"], "condition_unmet")
            # registry condition untouched: the plain graph still runs a
            g.write_pipeline(["a"])
            pipelines.compile(["graph"])
            run = pipelines.run_document("graph", g.doc("again.md"))
            a = next(r for r in run["results"] if r["ep_slug"] == "a")
            self.assertEqual(a["state"], "completed")


class FrozenDefinitionTest(unittest.TestCase):
    """Compiled storage freezes runnable definitions; the registry moves on
    visibly, not behaviorally, until an explicit recompile."""

    def test_registry_change_after_compile_changes_nothing_until_recompile(self):
        with _Graph() as g:
            g.write_pipeline(["a", "b"])
            pipelines.compile(["graph"])
            # the registry moves on: version bump on a
            _pass(g.registry, "a", result=None, version=2)
            # the compiled graph still runs the FROZEN definition
            run = pipelines.run_document("graph", g.doc())
            self.assertEqual(run["failed"], 0)
            a = next(r for r in run["results"] if r["ep_slug"] == "a")
            self.assertEqual(a["version"], 1, "frozen version executes, not live v2")
            # drift is visible per node, not silent
            drift = pipelines.registry_drift(
                pipelines.compiled()["graph"]["definition"])
            self.assertEqual([c["slug"] for c in drift["changed"]], ["a"])
            self.assertEqual(drift["changed"][0]["live_version"], 2)
            # explicit recompile adopts the registry change and clears drift
            pipelines.compile(["graph"])
            drift = pipelines.registry_drift(
                pipelines.compiled()["graph"]["definition"])
            self.assertEqual(drift["count"], 0)

    def test_missing_required_dependency_is_a_loud_compile_error(self):
        with _Graph() as g:
            # b requires a; author a graph that includes b but not a
            g.write_pipeline(["b", "c"])
            out = pipelines.compile(["graph"])
            self.assertIn("requires ['a'], which is not in this graph",
                          out["failed"]["graph"])


class TranscriptionAuthorityTest(unittest.TestCase):
    """New audio plans come from ONE compiled transcription pipeline, or the
    legacy registry plan stands; an ambiguous pair is never fabricated."""

    def test_authority_freezes_falls_back_and_refuses_ambiguity(self):
        with _Graph() as g:
            _pass(g.registry, "t1", result=None, auto=True)
            _pass(g.registry, "t2", result=None, auto=True, after=["t1"])

            # no compiled transcription pipeline: the legacy registry plan
            definitions, provenance = pipelines.transcription_authority()
            self.assertEqual(provenance["kind"], "registry")
            self.assertEqual(set(definitions), {"t1", "t2"})

            # one compiled+enabled pipeline: its frozen graph is the authority
            g.write_pipeline(["t1", "t2"], trigger="transcription", enabled=True)
            pipelines.compile(["graph"])
            definitions, provenance = pipelines.transcription_authority()
            self.assertEqual(provenance["kind"], "pipeline")
            self.assertEqual(provenance["name"], "graph")
            self.assertEqual(definitions["t2"]["after"], ["t1"])

            # ensure_plan mints from the authority and records its provenance
            plan = passes.ensure_plan("item-1")
            self.assertEqual(set(plan), {"t1", "t2"})
            row = ledger.connect().execute(
                "SELECT source FROM processing_plans WHERE item_id='item-1'").fetchone()
            self.assertEqual(json.loads(row["source"])["kind"], "pipeline")

            # a registry edit after compile does not change NEW plans
            _pass(g.registry, "t2", result=None, auto=True, after=["t1"], version=2)
            again = passes.ensure_plan("item-2")
            self.assertEqual(set(again), {"t1", "t2"})
            self.assertEqual(again["t2"]["version"], 1, "frozen version, not live v2")

            # two enabled transcription pipelines: ambiguous, refuse loudly and
            # fall back to the registry plan (never fabricate an active pick)
            g.write_pipeline(["t1"], name="graph2", trigger="transcription",
                             enabled=True)
            pipelines.compile(["graph2"])
            definitions, provenance = pipelines.transcription_authority()
            self.assertEqual(provenance["kind"], "registry")
            self.assertIn("multiple enabled transcription pipelines",
                          provenance.get("fallback_reason", ""))
            transitions = ledger.connect().execute(
                "SELECT cause_code FROM transitions WHERE machine='pipeline' "
                "AND cause_code='ambiguous_transcription_authority'").fetchall()
            self.assertTrue(transitions, "the refusal is recorded, not just logged")

    def test_transcription_pipeline_rejects_manual_passes_at_compile(self):
        with _Graph() as g:
            g.write_pipeline(["a"], trigger="transcription", enabled=True)
            out = pipelines.compile(["graph"])
            self.assertIn("only compile automatic, enabled passes",
                          out["failed"]["graph"])


if __name__ == "__main__":
    unittest.main()
