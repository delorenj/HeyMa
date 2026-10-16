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
          requires: list | None = None, after: list | None = None) -> None:
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
        f"slug: {slug}\nversion: 1\ndescription: {slug} pass\n"
        "enabled: true\nauto: false\ncapabilities: [document]\n"
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

    def write_pipeline(self, passes: str, **extra) -> None:
        doc = {
            "name": "graph",
            "version": 1,
            "description": "test graph",
            "trigger": "manual",
            "enabled": True,
            "failure_policy": "independent",
            "passes": passes,
        }
        doc.update(extra)
        (self.pipes / "graph.yaml").write_text(json.dumps(doc))

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
            # the failed run withheld the processed receipt
            row = documents.get(run["doc_id"])
            self.assertIsNone(row["processed_at"])

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


if __name__ == "__main__":
    unittest.main()
