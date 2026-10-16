"""The document scenario: enrichment in place, with honest provenance.

Covers the approved new-arrivals-only binding semantics (seed, ignore Wax
writes, explicit backfill), the no-fake-transcript-row rule, visible
inapplicability of audio passes, no-clobber on human values, and one effective
run per duplicate arrival.
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


def _write_registry(directory: Path) -> Path:
    """alpha is document-capable (proposes title/summary); diarize is audio-only."""
    alpha = directory / "alpha"
    alpha.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '" + json.dumps({
            "wax_ep_version": 1,
            "frontmatter": {"title": "A Grounded Title", "summary": "A grounded summary."},
        }) + "'\n"
    )
    alpha.chmod(0o755)
    (directory / "alpha.yaml").write_text(
        "slug: alpha\nversion: 1\ndescription: Grounded title for any document.\n"
        "enabled: true\nauto: false\ncapabilities: [document]\nrequires: []\n"
        f"command: [{json.dumps(str(alpha))}, \"{{md_path}}\"]\n"
    )
    (directory / "diarize.yaml").write_text(
        "slug: diarize\nversion: 1\ndescription: Needs the recording.\n"
        "enabled: true\nauto: false\ncapabilities: [audio]\nrequires: []\n"
        "command: [\"/bin/true\", \"{md_path}\"]\n"
    )
    return alpha


class _Scenario:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.registry = self.root / "passes.d"
        self.pipes = self.root / "pipelines.d"
        self.docs = self.root / "docs"
        for d in (self.runtime, self.registry, self.pipes, self.docs):
            d.mkdir()
        _write_registry(self.registry)
        (self.pipes / "doc.yaml").write_text(
            "name: doc\nversion: 1\ndescription: document graph\ntrigger: documents\n"
            f"enabled: true\nfolders: [{json.dumps(str(self.docs))}]\n"
            "failure_policy: independent\npasses: [alpha, diarize]\n"
        )

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
        pipelines.compile(["doc"])
        return self

    def __exit__(self, *a):
        for p in self.stack:
            p.stop()
        _close_test_connection()
        self.tmp.cleanup()

    def drop(self, name: str, text: str = "---\ntitle:\n---\nBody text.\n") -> Path:
        path = self.docs / name
        path.write_text(text)
        return path


class DocumentScenarioTest(unittest.TestCase):
    def test_seed_then_new_arrivals_only_then_explicit_backfill(self):
        with _Scenario() as s:
            existing = s.drop("old.md", "---\ntitle: Old\n---\nHistorical.\n")
            out = documents.scan(pipeline="doc")
            seed = out["bindings"][0]
            self.assertEqual(seed["action"], "seeded")
            self.assertEqual(seed["seeded"], 1)
            self.assertEqual(seed["processed"], 0)
            seeded = documents.list_documents(state="seeded")
            self.assertEqual(len(seeded), 1, "seeding must register without processing")
            self.assertEqual(seeded[0]["state"], "seeded")

            fresh = s.drop("new.md")
            out = documents.scan(pipeline="doc")
            scan = out["bindings"][0]
            self.assertEqual(scan["arrivals"], 1)
            self.assertEqual(scan["processed"], 1)
            fm, _ = frontmatter.read(fresh)
            self.assertEqual(fm["title"], "A Grounded Title")

            backfill = documents.backfill(pipeline="doc")
            self.assertEqual(backfill["candidates"], 1)
            self.assertEqual(backfill["processed"], 1)
            # no-clobber: the human title survives backfill; empty keys fill in
            fm, _ = frontmatter.read(existing)
            self.assertEqual(fm["title"], "Old")
            self.assertEqual(fm["summary"], "A grounded summary.")

    def test_wax_output_writes_are_ignored(self):
        with _Scenario() as s:
            s.drop(".wax-ep-staging.md", "staging output")
            s.drop("note.md.tmp", "atomic-rename temp")
            s.drop("capture.ogg.partial", "capture partial")
            s.drop("real.md")
            # the first scan seeds; the ignored shapes must not even register
            out = documents.scan(pipeline="doc")
            self.assertEqual(out["bindings"][0]["seeded"], 1,
                             "only real.md counts — Wax writes and temps are ignored")
            s.drop("fresh.md", "---\ntitle:\n---\nA genuinely new document.\n")
            scan = documents.scan(pipeline="doc")["bindings"][0]
            self.assertEqual(scan["arrivals"], 1)

    def test_duplicate_arrival_is_one_effective_run(self):
        with _Scenario() as s:
            first = s.drop("twin.md")
            original = first.read_bytes()  # BEFORE enrichment changes the bytes
            documents.scan(pipeline="doc")            # seeds; nothing processed
            documents.backfill(pipeline="doc")        # explicit processing
            # same CONTENT, second path: not an arrival at all — content identity
            # is what the binding tracks, so there is exactly one effective run
            twin = s.docs / "twin-copy.md"
            twin.write_bytes(original)
            out = documents.scan(pipeline="doc")
            scan = out["bindings"][0]
            self.assertEqual(scan["arrivals"], 0)
            self.assertEqual(scan.get("results", []), [])
            # and the duplicate is not re-claimed on the NEXT scan either
            again = documents.scan(pipeline="doc")
            self.assertEqual(again["bindings"][0]["arrivals"], 0)
            # a manual run of the duplicate path is an explicit no-op
            manual = pipelines.run_document("doc", twin)
            self.assertIn("already processed", manual.get("skipped", ""))

    def test_no_transcript_row_and_no_fabricated_provenance(self):
        with _Scenario() as s:
            doc = s.drop("note.md")
            run = pipelines.run_document("doc", doc)
            self.assertEqual(run["completed"], 1)
            doc_id = run["doc_id"]
            self.assertTrue(doc_id.startswith("doc-"))
            conn = ledger.connect()
            self.assertIsNone(
                conn.execute("SELECT 1 FROM transcripts WHERE item_id=?", (doc_id,)).fetchone(),
                "a document must never acquire a transcripts row")
            fm, _ = frontmatter.read(doc)
            self.assertNotIn("asset-kind", fm)
            self.assertNotIn("source", fm)
            self.assertNotIn("source-sha256", fm)
            self.assertNotIn("source-s3-key", fm)
            # human values survive; the file keeps its name
            self.assertEqual(doc.name, "note.md")

    def test_audio_pass_is_visibly_inapplicable_not_silent(self):
        with _Scenario() as s:
            doc = s.drop("note.md")
            run = pipelines.run_document("doc", doc)
            diarize = next(r for r in run["results"] if r["ep_slug"] == "diarize")
            self.assertEqual(diarize["state"], "skipped")
            self.assertEqual(diarize["reason_code"], "inapplicable_capability")
            fm, _ = frontmatter.read(doc)
            self.assertEqual(fm["wax"]["passes"]["diarize"]["reason_code"],
                             "inapplicable_capability")

    def test_human_frontmatter_values_are_never_clobbered(self):
        with _Scenario() as s:
            doc = s.drop("human.md", "---\ntitle: My Own Title\n---\nBody.\n")
            pipelines.run_document("doc", doc)
            fm, _ = frontmatter.read(doc)
            self.assertEqual(fm["title"], "My Own Title")
            self.assertEqual(fm["summary"], "A grounded summary.")

    def test_failed_pass_stays_retryable_and_receipt_withheld(self):
        with _Scenario() as s:
            doc = s.drop("retry.md")
            # break the pass command after compile: run -> fail, run again -> fix
            alpha = s.registry / "alpha"
            alpha.write_text("#!/bin/sh\necho 'reason_code=provider_down' >&2\nexit 1\n")
            alpha.chmod(0o755)
            first = pipelines.run_document("doc", doc)
            self.assertEqual(first["failed"], 1)
            receipt = documents.receipt("", first["doc_id"], "doc", first["definition_id"])
            self.assertEqual(receipt["state"], "failed",
                             "a failed run must not record a completed receipt")
            self.assertEqual(documents.get(first["doc_id"])["state"], "failed")
            alpha.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '" + json.dumps({
                    "wax_ep_version": 1,
                    "frontmatter": {"title": "Recovered Title", "summary": "s"},
                }) + "'\n"
            )
            alpha.chmod(0o755)
            second = pipelines.run_document("doc", doc)
            self.assertEqual(second["failed"], 0)
            completed = next(r for r in second["results"] if r["ep_slug"] == "alpha")
            self.assertEqual(completed["state"], "completed")
            receipt = documents.receipt("", second["doc_id"], "doc", second["definition_id"])
            self.assertEqual(receipt["state"], "completed")
            self.assertEqual(documents.get(second["doc_id"])["state"], "processed")


class DocumentIdentityTest(unittest.TestCase):
    """Durable per-binding identity: own writes, overlapping pipelines, run
    locking, scan-driven recovery, exhaustion, and honest seeding."""

    def test_own_enrichment_write_is_not_a_new_arrival(self):
        with _Scenario() as s:
            s.drop("seed.md", "---\ntitle: Seed\n---\nseed only\n")
            documents.scan(pipeline="doc")     # seed
            fresh = s.drop("fresh.md")         # empty title -> alpha fills it
            original = fresh.read_bytes()
            out = documents.scan(pipeline="doc")
            self.assertEqual(out["bindings"][0]["arrivals"], 1)
            fm, _ = frontmatter.read(fresh)
            self.assertEqual(fm["title"], "A Grounded Title")
            self.assertNotEqual(fresh.read_bytes(), original,
                                "the enrichment write changed the file bytes")
            # the next scan must NOT see Wax's own frontmatter write as new
            # content: identity is the document, not the current hash
            again = documents.scan(pipeline="doc")
            scan = again["bindings"][0]
            self.assertEqual(scan["arrivals"], 0)
            self.assertEqual(scan.get("results", []), [])
            self.assertEqual(len(documents.list_documents()), 2)

    def test_overlapping_pipelines_keep_separate_outcomes(self):
        with _Scenario() as s:
            # a second pipeline over the SAME folder: per-binding identity,
            # not a global content lookup that would suppress the second run
            (s.pipes / "doc2.yaml").write_text(
                "name: doc2\nversion: 1\ndescription: second view\ntrigger: documents\n"
                f"enabled: true\nfolders: [{json.dumps(str(s.docs))}]\n"
                "failure_policy: independent\npasses: [alpha]\n"
            )
            pipelines.compile(["doc2"])
            s.drop("seed.md", "---\ntitle: Seed\n---\nseed\n")
            documents.scan(pipeline="doc")   # seeds doc
            documents.scan(pipeline="doc2")  # seeds doc2 (same folder, its own seed)
            shared = s.drop("shared.md")
            documents.scan(pipeline="doc")
            documents.scan(pipeline="doc2")
            # both pipelines processed the same content, each with its own
            # binding-scoped rows and receipts — no cross-suppression
            rows = documents.list_documents()
            by_state = {}
            for r in rows:
                if r["path"].endswith("shared.md"):
                    by_state.setdefault(r["binding_id"], []).append(r["state"])
            self.assertEqual(
                by_state,
                {b["binding_id"]: ["processed"]
                 for b in documents.bindings()
                 if b["pipeline"] in ("doc", "doc2")},
                "each binding processed the shared file exactly once")

    def test_dependency_on_inapplicable_skip_is_satisfied(self):
        # alpha (document) gated on diarize (audio-only): in a document run
        # diarize is VISIBLY skipped, and a skip satisfies the requirement —
        # the dependent runs; nothing is silently dropped
        with _Scenario() as s:
            (s.registry / "alpha.yaml").write_text(
                (s.registry / "alpha.yaml").read_text()
                .replace("requires: []", "requires: [diarize]")
            )
            (s.pipes / "doc.yaml").write_text(
                "name: doc\nversion: 1\ndescription: document graph\ntrigger: documents\n"
                f"enabled: true\nfolders: [{json.dumps(str(s.docs))}]\n"
                "failure_policy: independent\npasses: [alpha, diarize]\n"
            )
            pipelines.compile(["doc"])
            doc = s.drop("note.md")
            run = pipelines.run_document("doc", doc)
            states = {r["ep_slug"]: r for r in run["results"]}
            self.assertEqual(states["diarize"]["reason_code"], "inapplicable_capability")
            self.assertEqual(states["alpha"]["state"], "completed")

    def test_failed_arrival_recovers_on_scan_until_exhausted(self):
        with _Scenario() as s:
            alpha = s.registry / "alpha"
            s.drop("seed.md", "---\ntitle: Seed\n---\nseed\n")
            documents.scan(pipeline="doc")  # seed
            failing = "#!/bin/sh\necho 'reason_code=provider_down' >&2\nexit 1\n"
            alpha.write_text(failing)
            alpha.chmod(0o755)
            doc = s.drop("broken.md")
            first = documents.scan(pipeline="doc")
            self.assertEqual(first["bindings"][0]["arrivals"], 1)
            self.assertEqual(first["bindings"][0]["results"][0]["failed"], 1)
            failed = documents.list_documents(state="failed")
            self.assertEqual(len(failed), 1)
            failed_doc = failed[0]["doc_id"]
            self.assertEqual(failed[0]["path"], str(doc))

            # recovery: fix the pass; the NEXT scan retries the SAME identity
            # (a failed run's pass-note writes moved the hash — adoption, not
            # a fresh registration)
            alpha.write_text(
                "#!/bin/sh\nprintf '%s\\n' '" + json.dumps({
                    "wax_ep_version": 1,
                    "frontmatter": {"title": "Fixed", "summary": "ok"},
                }) + "'\n")
            alpha.chmod(0o755)
            second = documents.scan(pipeline="doc")
            self.assertEqual(second["bindings"][0]["retried"], 1,
                             "a failed arrival is retried by the scan, not parked")
            self.assertEqual(second["bindings"][0]["results"][0]["failed"], 0)
            processed = documents.list_documents(state="processed")
            self.assertEqual([r["doc_id"] for r in processed], [failed_doc],
                             "recovery continues the failed row's identity")

            # exhaustion: keep failing and the scan stops after the bound
            alpha.write_text(failing)
            alpha.chmod(0o755)
            s.drop("doomed.md", "---\ntitle:\n---\nDoomed, uniquely.\n")
            documents.scan(pipeline="doc")  # arrival fails (attempt 1)
            documents.scan(pipeline="doc")  # retry fails (attempt 2)
            documents.scan(pipeline="doc")  # retry fails (attempt 3 -> exhausted)
            fourth = documents.scan(pipeline="doc")
            self.assertEqual(fourth["bindings"][0].get("retried", 0), 0,
                             "exhausted arrivals are dead-lettered, not retried forever")
            exhausted = [r for r in documents.recent_receipts(pipeline="doc")
                         if r["exhausted_at"]]
            self.assertEqual(len(exhausted), 1)

    def test_concurrent_duplicate_runs_take_the_lock_once(self):
        with _Scenario() as s:
            doc = s.drop("locked.md")
            row, _ = documents.register(doc, binding_id="")
            # the compare-and-set is the whole concurrency story
            self.assertTrue(documents.claim_run("", row["doc_id"]))
            self.assertFalse(documents.claim_run("", row["doc_id"]),
                             "a second claimant must see the lock held")
            run = pipelines.run_document("doc", doc)
            self.assertIn("run lock held", run.get("skipped", ""))
            # a stale claim (crashed run) becomes reclaimable, not wedged
            ledger.connect().execute(
                "UPDATE documents SET run_started_at=? WHERE binding_id='' AND doc_id=?",
                ("2020-01-01T00:00:00.000Z", row["doc_id"]))
            self.assertTrue(documents.claim_run("", row["doc_id"]))

    def test_missing_folder_is_never_claimed_as_seeded(self):
        with _Scenario() as s:
            (s.pipes / "ghost.yaml").write_text(
                "name: ghost\nversion: 1\ndescription: bound to nothing yet\n"
                "trigger: documents\nenabled: true\n"
                "folders: [" + json.dumps(str(s.root / "not-yet")) + "]\n"
                "failure_policy: independent\npasses: [alpha]\n"
            )
            pipelines.compile(["ghost"])
            out = documents.scan(pipeline="ghost")
            self.assertEqual(out["bindings"][0]["action"], "folder_missing")
            binding = documents.bindings(pipeline="ghost")[0]
            self.assertIsNone(binding["seeded_at"],
                              "a missing folder must not claim seeded history")
            # the day the folder exists, it seeds (still without processing)
            (s.root / "not-yet").mkdir()
            (s.root / "not-yet" / "a.md").write_text("---\ntitle: A\n---\nA.\n")
            out = documents.scan(pipeline="ghost")
            self.assertEqual(out["bindings"][0]["action"], "seeded")
            self.assertEqual(out["bindings"][0]["seeded"], 1)

    def test_worker_heartbeat_reports_liveness(self):
        with _Scenario() as s:
            self.assertEqual(documents.read_worker_heartbeat()["state"], "down",
                             "no heartbeat yet: nobody scans")
            documents.write_worker_heartbeat(tick=7)
            beat = documents.read_worker_heartbeat()
            self.assertEqual(beat["state"], "live")
            self.assertEqual(beat["tick"], 7)
            self.assertEqual(beat["documents"]["interval_s"], documents.SCAN_INTERVAL_S)
            # a stale heartbeat is DOWN, not healthy-idle
            documents.worker_heartbeat_path().write_text(json.dumps(
                {"at": "2020-01-01T00:00:00.000Z", "tick": 1}))
            self.assertEqual(documents.read_worker_heartbeat()["state"], "down")


if __name__ == "__main__":
    unittest.main()
