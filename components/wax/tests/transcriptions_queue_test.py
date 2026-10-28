import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import ledger, operations, paths, sentinel
from wax import transcriptions
from wax.transcriptions import config, health, outbox, store

ITEM = "4c9f2d894fde1cc5"
NOTE = "---\nwax-item-id: 4c9f2d894fde1cc5\ntitle: T\n---\n# Transcription: a.mp3\n\nhello there world\n"


def _close_test_connection() -> None:
    connection = getattr(ledger._local, "conn", None)
    if connection is not None:
        connection.close()
        del ledger._local.conn


class TempLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.var = root / "var"
        self.vault = root / "vault"
        self.var.mkdir()
        self.vault.mkdir()
        _close_test_connection()
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("WAX_TRANSCRIPTS", "WAX_EMBED"))}
        self.patches = [
            patch.object(paths, "VAR", self.var), patch.object(paths, "DB", self.var / "wax.db"),
            patch.object(paths, "VAULT", self.vault),
            patch.object(config, "UNIT_PATH", root / "no-such-unit.service"),
            patch.dict(os.environ, clean, clear=True),
        ]
        for p in self.patches:
            p.start()
        self.md = self.vault / "20261027-221920-a.md"
        self.md.write_text(NOTE)

    def tearDown(self):
        _close_test_connection()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def record(self, **overrides):
        kw = dict(item_id=ITEM, slug="frontmatter-stamp", kind="version", state="completed", md_path=self.md,
                  pass_version=1, attempt=1, command_id="55640a03-d3bb-5137-af52-f72f2a38f5de", reason_code=None,
                  detail="", definition={"slug": "frontmatter-stamp", "version": 1},
                  pass_result={"evidence": {"model": "m", "body_replace": {"text": "x"}}},
                  changed_fields=["frontmatter.word-count"], duration_s=0.4)
        kw.update(overrides)
        transcriptions.record_pass(**kw)


class TryItemLockTest(TempLedger):
    def test_second_holder_gets_false_immediately_and_reentry_is_true(self):
        results = []
        entered = threading.Event()
        release = threading.Event()

        def holder():
            with operations.item_lock(ITEM):
                entered.set()
                release.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(entered.wait(5))
        started = time.monotonic()
        with operations.try_item_lock(ITEM) as held:
            results.append(held)
        self.assertLess(time.monotonic() - started, 1.0)
        release.set()
        t.join(5)
        self.assertEqual(results, [False])
        with operations.try_item_lock(ITEM) as held:
            self.assertTrue(held)
            with operations.try_item_lock(ITEM) as again:
                self.assertTrue(again)
        with operations.item_lock(ITEM):
            with operations.try_item_lock(ITEM) as nested:
                self.assertTrue(nested)


class CaptureQueueTest(TempLedger):
    def test_version_snapshot_carries_exact_note_and_stripped_evidence(self):
        self.record()
        rows = outbox.pending(ITEM)
        self.assertEqual(len(rows), 1)
        snap = rows[0]
        self.assertEqual(snap["kind"], "version")
        self.assertEqual(snap["idempotency_key"], "pass:55640a03-d3bb-5137-af52-f72f2a38f5de")
        self.assertEqual(snap["note_text"], NOTE)
        self.assertEqual(snap["payload"]["pass"]["evidence"], {"model": "m"})
        self.assertEqual(snap["payload"]["relpath"], self.md.name)
        self.assertIsNotNone(snap["payload"]["pass"]["definition_id"])
        self.record()                                   # re-delivery is INSERT OR IGNORE
        self.assertEqual(len(outbox.pending(ITEM)), 1)

    def test_failed_skip_and_depfail_keys(self):
        self.record(kind="pass_run", state="failed", command_id="c0ffee00-0000-5000-8000-000000000001",
                    reason_code="timeout", detail="x" * 900)
        self.record(command_id=None, state="skipped", reason_code="condition_unmet", slug="diarization", attempt=2)
        self.record(kind="pass_run", state="failed", command_id=None, reason_code="dependency_failed",
                    slug="ticketable", pass_version=2, md_path=None)
        rows = {r["idempotency_key"]: r for r in outbox.pending(ITEM)}
        self.assertEqual(set(rows), {"pass:c0ffee00-0000-5000-8000-000000000001",
                                     f"skip:{ITEM}:diarization:v1:a2", f"depfail:{ITEM}:ticketable:v2:a1"})
        failed = rows["pass:c0ffee00-0000-5000-8000-000000000001"]
        self.assertIsNone(failed["note_text"])
        self.assertEqual(len(failed["payload"]["pass"]["detail"]), 300)

    def test_documents_and_disabled_enqueue_nothing(self):
        self.record(item_id="doc-e3b0c44298fc1c14@doc-standard-Wax-4a02b5d4cb30")
        with patch.dict(os.environ, {"WAX_TRANSCRIPTS": "0"}):
            self.record()
        self.assertEqual(outbox.pending(), [])

    def test_no_dsn_never_projects(self):
        with patch.object(store, "project_item") as project:
            self.record()
        project.assert_not_called()
        self.assertEqual(len(outbox.pending(ITEM)), 1)

    def test_projection_failure_trips_breaker_until_expiry(self):
        env = {"WAX_TRANSCRIPTS_DSN": "postgresql://transcriptions_app@localhost:1/transcriptions",
               "WAX_TRANSCRIPTS_SYNC_BREAKER_S": "300"}
        with patch.dict(os.environ, env), patch.object(store, "project_item", side_effect=RuntimeError("down")) as p:
            self.record()
            self.assertEqual(p.call_count, 1)
            self.assertTrue(outbox.breaker_open())
            self.record(command_id="11111111-2222-5333-8444-555555555555")
            self.assertEqual(p.call_count, 1)          # breaker open: no attempt
            with patch.object(outbox.time, "time", return_value=time.time() + 301):
                self.record(command_id="11111111-2222-5333-8444-666666666666")
            self.assertEqual(p.call_count, 2)
        self.assertEqual(len(outbox.pending(ITEM)), 3)

    def test_record_pass_never_raises(self):
        with patch.object(outbox, "enqueue", side_effect=RuntimeError("disk full")):
            self.record()                              # swallowed
        self.record(md_path=self.vault / "missing.md")   # unreadable note: swallowed

    def test_record_transcription(self):
        asr = self.var / "asr.json"
        asr.write_text(json.dumps({"version": 1, "source_sha256": "e" * 64, "result": {
            "language": "en", "language_probability": 0.99, "segments": [{}, {}], "duration": 132.28,
            "model": "large-v3", "backend": "faster-whisper"}}))
        transcriptions.record_transcription(item_id=ITEM, md_path=self.md, meta={"model": "large-v3"},
                                            verdict={"ok": True, "audio_duration_s": 132.29}, asr_path=asr,
                                            attempt=1, processing_seconds=11.4)
        snap = outbox.pending(ITEM)[0]
        self.assertTrue(snap["idempotency_key"].startswith(f"transcription:{ITEM}:a1:"))
        self.assertEqual(snap["payload"]["asr"]["segment_count"], 2)
        self.assertEqual(snap["payload"]["producer_kind"], "transcription")


class HealthSnapshotTest(TempLedger):
    def test_states(self):
        self.assertEqual(health.snapshot()["state"], "disabled")
        with patch.dict(os.environ, {"WAX_TRANSCRIPTS_DSN": "postgresql://x@localhost/transcriptions"}):
            # Configured but no waxd tick yet: nothing is draining, so not green.
            self.assertEqual(health.snapshot()["state"], "lagging")
            ledger.meta_set(health.LAST_TICK, json.dumps({"state": "ok", "at": sentinel.utcnow()}))
            self.assertEqual(health.snapshot()["state"], "ok")
            self.record()
            ledger.connect().execute("UPDATE transcript_snapshots SET created_at=?", ("2020-01-01T00:00:00.000Z",))
            snap = health.snapshot()
            self.assertEqual(snap["state"], "error")
            self.assertEqual(snap["queued"], 1)
            recent = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 900))
            ledger.connect().execute("UPDATE transcript_snapshots SET created_at=?", (recent,))
            self.assertEqual(health.snapshot()["state"], "lagging")
            ledger.meta_set(health.LAST_TICK, json.dumps({"embed_error": {"reason_code": "digest_mismatch",
                                                                          "detail": "x"}}))
            self.assertEqual(health.snapshot()["state"], "error")
        with patch.dict(os.environ, {"WAX_TRANSCRIPTS": "0"}):
            self.assertEqual(health.snapshot()["state"], "disabled")

    def test_a_dead_or_stale_waxd_thread_is_not_green(self):
        with patch.dict(os.environ, {"WAX_TRANSCRIPTS_DSN": "postgresql://x@localhost/transcriptions"}):
            old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 3000))
            ledger.meta_set(health.LAST_TICK, json.dumps({"state": "ok", "at": old}))
            snap = health.snapshot()
            self.assertEqual(snap["state"], "lagging")
            self.assertIn("is waxd running", snap["reason"])
            # The unit gained a DSN after waxd started: waxd still ticks `disabled`.
            ledger.meta_set(health.LAST_TICK, json.dumps({"state": "disabled", "reason": "no DSN",
                                                          "at": sentinel.utcnow()}))
            snap = health.snapshot()
            self.assertEqual(snap["state"], "lagging")
            self.assertIn("restart waxd", snap["reason"])

    def test_config_falls_back_to_the_installed_unit(self):
        unit = self.var / "waxd.service"
        unit.write_text('[Service]\nEnvironment="WAX_TRANSCRIPTS_DSN=postgresql://transcriptions_app@localhost:5432/'
                        'transcriptions?application_name=waxd"\nEnvironment=WAX_TRANSCRIPTS_TICK_S=20 '
                        '"WAX_EMBED_URL=http://ollama:11434/"\n')
        with patch.object(config, "UNIT_PATH", unit):
            self.assertEqual(config.dsn(),
                             "postgresql://transcriptions_app@localhost:5432/transcriptions?application_name=waxd")
            self.assertEqual(config.tick_s(), 20.0)
            self.assertEqual(config.embed_url(), "http://ollama:11434")
            with patch.dict(os.environ, {"WAX_TRANSCRIPTS_TICK_S": "30"}):
                self.assertEqual(config.tick_s(), 30.0)


if __name__ == "__main__":
    unittest.main()
