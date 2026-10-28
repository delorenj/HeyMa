"""The `transcriptions` capture hooks in passes.run and transcribe_adapter (design D.3, E.1).

A real shell pass in a temp `passes.d`, a temp ledger and a temp vault. These
prove the one property the hooks exist under: whatever the store does - raise,
be unreachable, be unconfigured - the pass result, its ledger row and the note
are exactly what they would have been without it.
"""

import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wax import events, frontmatter, ledger, passes, paths, sentinel, transcribe_adapter
from wax import transcriptions
from wax.transcriptions import config, health, outbox, store

COMPONENT = Path(__file__).resolve().parents[1]
CID = "55640a03-d3bb-5137-af52-f72f2a38f5de"
NOTE = ("---\nwax-item-id: {item}\ntranscribed-at: 2026-10-27T22:19:20Z\nclassification: monolog\n---\n"
        "# Transcription: 20261027-221920-rec.ogg\n\n**Speaker 1 (00:00):** hello there world\n")


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


class CaptureHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.runtime = root / "runtime"
        self.vault = root / "vault"
        self.registry = root / "passes.d"
        for d in (self.runtime, self.vault, self.registry):
            d.mkdir()
        _close_test_connection()
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("WAX_TRANSCRIPTS", "WAX_EMBED"))}
        self.patches = [
            patch.object(paths, "DB", self.runtime / "wax.db"), patch.object(paths, "VAR", self.runtime),
            patch.object(paths, "VAULT", self.vault), patch.object(passes, "REGISTRY_DIR", self.registry),
            patch.object(passes, "_run_frontmatters", side_effect=_fake_frontmatters),
            # One command id per attempt, as events.emit_ep_command mints them (uuid5 over item/slug/attempt).
            patch.object(events, "emit_ep_command",
                         side_effect=lambda item, slug, argv, attempt: str(
                             uuid.uuid5(uuid.NAMESPACE_URL, f"ep:{item}:{slug}:{attempt}"))),
            patch.object(events, "emit"),
            patch.object(config, "UNIT_PATH", root / "no-such-unit.service"),
            patch.dict(os.environ, clean, clear=True),
        ]
        for p in self.patches:
            p.start()
        audio = self.runtime / "20261027-221920-rec.ogg"
        audio.write_bytes(b"audio bytes")
        self.item = ledger.upsert_item(audio)
        self.assertRegex(self.item, r"^[0-9a-f]{16}$")
        self.md = self.vault / "20261027-221920-rec.md"
        self.md.write_text(NOTE.format(item=self.item))
        ledger.connect().execute("INSERT INTO transcripts(item_id,md_path,created_at) VALUES(?,?,?)",
                                 (self.item, str(self.md), sentinel.utcnow()))

    def tearDown(self):
        _close_test_connection()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    # -- registry helpers
    def shell_pass(self, slug: str, *, stdout: dict = None, exit_code: int = 0, extra: str = "") -> None:
        command = self.registry / f"{slug}-bin"
        body = "#!/bin/sh\n"
        if stdout is not None:
            body += "printf '%s\\n' '" + json.dumps(stdout) + "'\n"
        if exit_code:
            body += f"echo 'boom' >&2\nexit {exit_code}\n"
        command.write_text(body)
        command.chmod(0o755)
        (self.registry / f"{slug}.yaml").write_text(
            f"slug: {slug}\nversion: 1\nenabled: true\nauto: true\n"
            f"command: [{json.dumps(str(command))}, \"{{md_path}}\"]\n" + extra)

    def snapshots(self):
        try:
            rows = ledger.connect().execute("SELECT * FROM transcript_snapshots ORDER BY seq").fetchall()
        except Exception:  # noqa: BLE001 - table not created: nothing was captured
            return []
        return [dict(r) for r in rows]

    def pass_row(self, slug: str):
        row = ledger.connect().execute("SELECT state, reason_code, attempt, command_id FROM passes "
                                       "WHERE item_id=? AND ep_slug=?", (self.item, slug)).fetchone()
        return dict(row) if row else None


class PassCaptureTest(CaptureHarness):
    def test_completed_pass_captures_the_note_as_the_pass_left_it_including_the_rename(self):
        self.shell_pass("title-slug", stdout={
            "wax_ep_version": 1,
            "frontmatter": {"title": "Hello World", "summary": "s.", "title-slug": "hello-world"},
            "transcript": {"slug": "hello-world"},
            "evidence": {"model": "automaticai/auto/fast", "body_replace": {"text": "never stored"}},
        })
        result = passes.run(self.item, "title-slug")
        self.assertEqual(result["state"], "completed")
        renamed = self.vault / "20261027-221920-hello-world.md"
        self.assertTrue(renamed.is_file())
        snaps = self.snapshots()
        self.assertEqual(len(snaps), 1)
        snap = snaps[0]
        self.assertEqual(snap["kind"], "version")
        self.assertEqual(snap["idempotency_key"], f"pass:{result['command_id']}")
        self.assertEqual(snap["md_path"], str(renamed))
        self.assertEqual(snap["note_text"], renamed.read_text())          # after _write_pass_note
        fm, _ = frontmatter.read(renamed)
        self.assertEqual(fm["wax"]["passes"]["title-slug"]["state"], "completed")
        payload = json.loads(snap["payload"])
        self.assertEqual(payload["pass"]["pre_rename_path"], str(self.md))
        self.assertEqual(payload["pass"]["state"], "completed")
        self.assertEqual(payload["relpath"], renamed.name)
        self.assertNotIn("body_replace", json.dumps(payload["pass"]["evidence"]))
        self.assertIsNone(snap["projected_at"])                              # no DSN: capture only

    def test_failed_pass_enqueues_a_pass_run_only(self):
        self.shell_pass("title-slug", exit_code=3)
        result = passes.run(self.item, "title-slug")
        self.assertEqual(result["state"], "failed")
        snaps = self.snapshots()
        self.assertEqual([(s["kind"], s["idempotency_key"]) for s in snaps],
                         [("pass_run", f"pass:{result['command_id']}")])
        self.assertIsNone(snaps[0]["note_text"])
        self.assertEqual(json.loads(snaps[0]["payload"])["pass"]["reason_code"], "nonzero_exit")

    def test_when_skip_enqueues_a_version_keyed_skip(self):
        self.shell_pass("diarization", stdout={"wax_ep_version": 1},
                        extra="when:\n  - field: classification\n    not_equals: monolog\n")
        result = passes.run(self.item, "diarization")
        self.assertEqual((result["state"], result["reason_code"]), ("skipped", "condition_unmet"))
        snaps = self.snapshots()
        self.assertEqual([(s["kind"], s["idempotency_key"]) for s in snaps],
                         [("version", f"skip:{self.item}:diarization:v1:a1")])
        self.assertEqual(snaps[0]["note_text"], self.md.read_text())
        self.assertIn("condition_unmet", snaps[0]["note_text"])

    def test_dependency_failed_enqueues_a_depfail_pass_run(self):
        self.shell_pass("classification", stdout={"wax_ep_version": 1})
        self.shell_pass("diarization", stdout={"wax_ep_version": 1}, extra="requires: [classification]\n")
        result = passes.run(self.item, "diarization")
        self.assertEqual(result["reason_code"], "dependency_failed")
        snaps = self.snapshots()
        self.assertEqual([(s["kind"], s["idempotency_key"]) for s in snaps],
                         [("pass_run", f"depfail:{self.item}:diarization:v1:a1")])

    def test_a_document_key_enqueues_nothing(self):
        with patch.object(transcriptions, "record_pass") as record:
            passes._capture("doc-0123456789abcdef@inbox", "summarize", kind="version", state="completed",
                            md_path=self.md, pass_version=1, attempt=1, command_id=CID, reason_code=None,
                            detail="", definition=None, pass_result=None, changed_fields=[], duration_s=0.1)
        record.assert_not_called()
        self.assertEqual(self.snapshots(), [])

    def test_wax_transcripts_0_captures_nothing(self):
        self.shell_pass("title-slug", stdout={"wax_ep_version": 1, "frontmatter": {"title": "T"}})
        with patch.dict(os.environ, {"WAX_TRANSCRIPTS": "0"}):
            self.assertEqual(passes.run(self.item, "title-slug")["state"], "completed")
        self.assertEqual(self.snapshots(), [])


class FailOpenTest(CaptureHarness):
    """A failing store never fails a pass, at any layer the failure can come from."""

    def run_completed_pass(self):
        self.shell_pass("title-slug", stdout={"wax_ep_version": 1, "frontmatter": {"title": "Kept"}})
        result = passes.run(self.item, "title-slug")
        self.assertEqual(result["state"], "completed")
        self.assertNotIn("error", result)
        self.assertEqual(self.pass_row("title-slug")["state"], "completed")
        fm, _ = frontmatter.read(self.md)
        self.assertEqual(fm["title"], "Kept")
        self.assertEqual(fm["wax"]["passes"]["title-slug"]["state"], "completed")
        return result

    def test_record_pass_raising_leaves_the_pass_untouched(self):
        with patch.object(transcriptions, "record_pass", side_effect=RuntimeError("store exploded")) as record, \
                self.assertLogs("wax.passes", level="WARNING") as logs:
            self.run_completed_pass()
        record.assert_called_once()
        self.assertTrue(any("transcriptions capture failed" in line for line in logs.output))

    def test_the_queue_itself_raising_leaves_the_pass_untouched(self):
        with patch.object(outbox, "enqueue", side_effect=OSError("disk full")):
            self.run_completed_pass()
        self.assertEqual(self.snapshots(), [])

    def test_no_dsn_never_calls_project_item(self):
        with patch.object(store, "project_item") as project:
            self.run_completed_pass()
        project.assert_not_called()
        self.assertEqual(len(self.snapshots()), 1)

    def test_project_item_raising_trips_the_breaker_until_it_expires(self):
        self.shell_pass("title-slug", stdout={"wax_ep_version": 1, "frontmatter": {"title": "Kept"}})
        with patch.dict(os.environ, {"WAX_TRANSCRIPTS_DSN": "postgresql://transcriptions_app@localhost/x"}), \
                patch.object(store, "project_item", side_effect=RuntimeError("pg down")) as project:
            self.assertEqual(passes.run(self.item, "title-slug")["state"], "completed")
            self.assertEqual(project.call_count, 1)
            self.assertTrue(outbox.breaker_open())
            # The breaker is open: the next pass captures but makes no projection attempt.
            self.assertEqual(passes.run(self.item, "title-slug")["state"], "completed")
            self.assertEqual(project.call_count, 1)
            # Expired: the pass path tries again.
            ledger.meta_set(outbox.BREAKER_KEY, f"{time.time() - 1:.3f}")
            self.assertEqual(passes.run(self.item, "title-slug")["state"], "completed")
            self.assertEqual(project.call_count, 2)
        self.assertEqual(len(self.snapshots()), 3)
        self.assertTrue(all(s["projected_at"] is None for s in self.snapshots()))

    def test_an_unreachable_store_costs_nothing_but_lag(self):
        # A real connection attempt at a closed loopback port (or, under an interpreter without
        # psycopg, StoreUnavailable): the pass completes, the snapshot stays queued, the breaker opens.
        with patch.dict(os.environ, {"WAX_TRANSCRIPTS_DSN": "postgresql://transcriptions_app@127.0.0.1:1/x"}):
            started = time.monotonic()
            self.run_completed_pass()
            self.assertLess(time.monotonic() - started, 10)
            self.assertTrue(outbox.breaker_open())
            snaps = self.snapshots()
            self.assertEqual(len(snaps), 1)
            self.assertIsNone(snaps[0]["projected_at"])
            self.assertEqual(health.snapshot()["queued"], 1)


class TranscribeHookTest(unittest.TestCase):
    """The transcribe_adapter hook: called once the note is published, and never fatal."""

    def run_transcribe(self, record):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "audio.ogg"
            md = root / "derived.md"
            audio.write_bytes(b"audio")
            md.write_text("# Transcription: audio.ogg\n\nhello\n")
            metadata = ('Transcription-Metadata: {"duration_seconds": 1, "word_count": 1, '
                        '"diarized": true, "model": "large-v3"}\n')
            connection = SimpleNamespace(execute=lambda *_args, **_kwargs: None)
            verdict = {"ok": True, "reason_code": None, "audio_duration_s": 1.0,
                       "asr_duration_s": 1.0, "duration_ratio": 1.0}
            with patch.object(transcribe_adapter, "transcribe_command", return_value=Path("/bin/true")), \
                    patch.object(transcribe_adapter.subprocess, "run", return_value=SimpleNamespace(
                        returncode=0, stdout=str(md) + "\n", stderr=metadata)), \
                    patch.object(transcribe_adapter.sanity, "check", return_value=verdict), \
                    patch.object(transcribe_adapter.sanity, "source_unchanged", return_value=True), \
                    patch.object(transcribe_adapter, "vault_name", return_value=md.name), \
                    patch.object(transcribe_adapter.paths, "VAULT", root), \
                    patch.object(transcribe_adapter.paths, "LOGS", root / "logs"), \
                    patch.object(transcribe_adapter.ledger, "connect", return_value=connection), \
                    patch.object(transcribe_adapter.ledger, "set_item_state"), \
                    patch.object(transcribe_adapter.archive, "references", return_value=[]), \
                    patch.object(transcribe_adapter.desktop, "ding"), \
                    patch.object(transcriptions, "record_transcription", side_effect=record) as hook:
                out = transcribe_adapter.transcribe(audio, item_id="0123456789abcdef", attempt=2)
            return out, hook, md

    def test_hook_runs_after_publish_with_the_final_note(self):
        out, hook, md = self.run_transcribe(None)
        hook.assert_called_once()
        kw = hook.call_args.kwargs
        self.assertEqual((kw["item_id"], kw["md_path"], kw["attempt"]), ("0123456789abcdef", md, 2))
        self.assertIsInstance(kw["processing_seconds"], float)
        self.assertEqual(out["md_path"], str(md))

    def test_hook_raising_never_costs_the_transcript(self):
        with self.assertLogs("wax.transcribe_adapter", level="WARNING"):
            out, hook, md = self.run_transcribe(RuntimeError("store exploded"))
        hook.assert_called_once()
        self.assertEqual(out["md_path"], str(md))
        self.assertTrue(out["ok"])


class TickAndHealthTest(CaptureHarness):
    def test_tick_without_dsn_is_disabled_not_an_error(self):
        stats = transcriptions.tick(gpu_busy=lambda: False)
        self.assertEqual(stats["state"], "disabled")
        self.assertEqual(health.snapshot()["state"], "disabled")

    def test_an_unreachable_store_at_tick_reads_lagging_never_green(self):
        with patch.dict(os.environ, {"WAX_TRANSCRIPTS_DSN": "postgresql://transcriptions_app@127.0.0.1:1/x"}):
            with self.assertRaises(transcriptions.StoreUnavailable):
                transcriptions.tick(gpu_busy=lambda: False)
            self.assertEqual(health.last_tick()["state"], "unreachable")
            snap = health.snapshot()
            self.assertEqual(snap["state"], "lagging")
            self.assertIn("unreachable", snap["reason"])

    def test_status_carries_the_block_and_error_makes_status_unhealthy(self):
        snap = ledger.enrich({})
        self.assertIn("transcriptions", snap)
        self.assertEqual(snap["transcriptions"]["state"], "disabled")
        wax = _load_script(COMPONENT / "bin" / "wax", "wax_cli_status")
        self.assertFalse(wax.status_unhealthy({"transcriptions": {"state": "lagging"}}))
        self.assertTrue(wax.status_unhealthy({"transcriptions": {"state": "error"}}))


class WaxdThreadTest(unittest.TestCase):
    def test_the_transcriptions_loop_survives_a_failing_tick_and_gates_on_the_gpu(self):
        waxd = _load_script(COMPONENT / "bin" / "waxd", "waxd_transcriptions_loop")
        daemon = waxd.Daemon.__new__(waxd.Daemon)
        daemon._conditions = {}
        daemon.stopped = threading.Event()
        daemon.snapshot = {"inbox": {"active_item": "0123456789abcdef"}}
        seen = []

        def tick(gpu_busy):
            seen.append(gpu_busy())
            if len(seen) == 1:
                raise RuntimeError("store down")
            daemon.stopped.set()
            return {"state": "ok"}

        with patch.object(waxd.transcriptions, "tick", side_effect=tick), \
                patch.object(waxd.transcriptions, "yield_gpu", return_value=True) as yield_gpu, \
                patch.object(waxd.transcriptions.config, "tick_s", return_value=0.01):
            thread = threading.Thread(target=daemon._transcriptions_loop)
            thread.start()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(seen, [True, True])
        self.assertNotIn("transcriptions_error", daemon._conditions)    # cleared by the good tick
        # One busy period, one unload: the embedder's VRAM goes back to whisper (design G.7).
        self.assertEqual(yield_gpu.call_count, 1)

    def test_the_loop_unloads_the_embedder_within_a_second_of_an_item_starting(self):
        waxd = _load_script(COMPONENT / "bin" / "waxd", "waxd_transcriptions_yield")
        daemon = waxd.Daemon.__new__(waxd.Daemon)
        daemon._conditions = {}
        daemon.stopped = threading.Event()
        daemon.snapshot = {"inbox": {"active_item": None}}
        ticks = []
        yielded = threading.Event()

        def tick(gpu_busy):
            ticks.append(gpu_busy())
            return {"state": "ok"}

        def on_yield():
            yielded.set()
            daemon.stopped.set()
            return True

        with patch.object(waxd.transcriptions, "tick", side_effect=tick), \
                patch.object(waxd.transcriptions, "yield_gpu", side_effect=on_yield) as yield_gpu, \
                patch.object(waxd.transcriptions.config, "tick_s", return_value=60.0):
            thread = threading.Thread(target=daemon._transcriptions_loop)
            thread.start()
            time.sleep(0.3)
            started = time.monotonic()
            daemon.snapshot = {"inbox": {"active_item": "0123456789abcdef"}}    # whisper is about to load
            self.assertTrue(yielded.wait(5))
            waited = time.monotonic() - started
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(ticks, [False])            # mid-wait, not at the next 60 s tick
        self.assertLess(waited, 2.5)
        self.assertEqual(yield_gpu.call_count, 1)


def _load_script(path: Path, name: str):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
