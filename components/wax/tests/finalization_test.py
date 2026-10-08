import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from wax import events, finalize, frontmatter, ledger, passes, paths, sentinel


class FinalizationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        for name, value in {"VAR": self.root / "var", "DB": self.root / "var/wax.db",
                            "ARCHIVE": self.root / "archive", "VAULT": self.root / "vault"}.items():
            self.stack.enter_context(patch.object(paths, name, value))
        for directory in (paths.VAR, paths.ARCHIVE, paths.VAULT):
            directory.mkdir(parents=True)
        self.close()
        audio = paths.ARCHIVE / "source.ogg"
        audio.write_bytes(b"preserved source audio")
        self.item = ledger.upsert_item(audio)
        self.md = paths.VAULT / "20261002-120000-final-title.md"
        self.body = "\n# Transcript\nAn unchanged café body.\n"
        self.md.write_text(frontmatter.render({"wax-item-id": self.item, "title": "Human title",
                                               "summary": "A grounded summary.", "classification": "meeting",
                                               "title-slug": "final-title", "custom-human": ["keep"]}, self.body))
        ledger.connect().execute("INSERT INTO transcripts(item_id,md_path,audio_duration,created_at) VALUES(?,?,?,?)",
                                 (self.item, str(self.md), 125, sentinel.utcnow()))
        ledger.connect().execute("INSERT INTO backups VALUES(?,?,?,?,?,?)",
                                 (self.item, "day/hash source.ogg", "recordings", audio.stat().st_size,
                                  sentinel.utcnow(), "size+md5"))
        self.definitions = {
            "title": {"slug": "title", "enabled": True, "auto": True, "version": 1},
            "unknown-future": {"slug": "unknown-future", "enabled": True, "auto": True, "version": 8},
        }
        self.stack.enter_context(patch.object(passes, "registry", return_value=self.definitions))
        self.stack.enter_context(patch.object(finalize, "audio_url", return_value="https://s3.example/recordings/audio"))
        passes.ensure_plan(self.item)

    def close(self):
        conn = getattr(ledger._local, "conn", None)
        if conn:
            conn.close()
            del ledger._local.conn

    def tearDown(self):
        self.close()
        self.stack.close()
        self.temp.cleanup()

    def complete(self, state="completed"):
        for slug, ep in self.definitions.items():
            passes._record(self.item, slug, state, version=ep["version"], definition=ep,
                           result={"effective_metadata": {"future-field": "future value"}})

    def test_final_path_metadata_future_pass_and_exact_body_are_receipted_once(self):
        self.complete()
        receipt = finalize.finalize(self.item)
        self.assertTrue(receipt["finalized"])
        row = ledger.connect().execute("SELECT * FROM outbox WHERE id=?", (receipt["outbox_id"],)).fetchone()
        envelope = json.loads(row["envelope"])
        data = envelope["data"]
        self.assertEqual(data["md_path"], str(self.md))
        self.assertEqual(data["metadata"]["custom-human"], ["keep"])
        self.assertEqual(data["pass_results"]["unknown-future"]["result"]["effective_metadata"]["future-field"], "future value")
        self.assertEqual(data["transcript_text"], self.body)
        self.assertEqual(data["s3_uri"], "s3://recordings/day/hash source.ogg")
        self.assertEqual(finalize.finalize(self.item)["event_id"], receipt["event_id"])
        self.assertEqual(ledger.connect().execute("SELECT COUNT(*) FROM completions").fetchone()[0], 1)
        import jsonschema
        from referencing import Registry, Resource
        schema_root = Path("/home/delorenj/code/33GOD/bloodbank/schemas")
        schema = json.loads((schema_root / "bloodbank/audio/transcription.completed.json").read_text())
        registry = Registry().with_resources((doc["$id"], Resource.from_contents(doc)) for doc in
            [json.loads(path.read_text()) for path in (schema_root / "_common").glob("*.json") if "$id" in json.loads(path.read_text())])
        jsonschema.Draft202012Validator(schema, registry=registry, format_checker=jsonschema.FormatChecker()).validate(envelope)

    def test_failed_running_missing_and_stale_passes_withhold_then_repair_once(self):
        for state in ("failed", "running"):
            self.complete(state)
            self.assertFalse(finalize.finalize(self.item)["finalized"])
        ledger.connect().execute("DELETE FROM passes WHERE ep_slug='unknown-future'")
        self.assertFalse(finalize.finalize(self.item)["finalized"])
        self.complete()
        ledger.connect().execute("UPDATE passes SET version=0 WHERE ep_slug='unknown-future'")
        self.assertFalse(finalize.finalize(self.item)["finalized"])
        self.complete("skipped")
        self.assertTrue(finalize.finalize(self.item)["finalized"])

    def test_unparked_or_unverified_audio_never_completes(self):
        self.complete()
        ledger.connect().execute("UPDATE items SET path=? WHERE item_id=?", (str(self.root / "inbox/source.ogg"), self.item))
        self.assertEqual(finalize.finalize(self.item)["reason_code"], "audio_not_parked")
        ledger.connect().execute("UPDATE items SET path=? WHERE item_id=?", (str(paths.ARCHIVE / "source.ogg"), self.item))
        ledger.connect().execute("UPDATE backups SET verified_at=NULL")
        self.assertEqual(finalize.finalize(self.item)["reason_code"], "unverified_archive")

    def test_large_transcript_is_uri_only_never_truncated_or_lost(self):
        self.complete()
        body = "𝄞" * 300000
        fm, _ = frontmatter.read(self.md)
        self.md.write_text(frontmatter.render(fm, body))
        receipt = finalize.finalize(self.item)
        data = json.loads(ledger.connect().execute("SELECT envelope FROM outbox WHERE id=?", (receipt["outbox_id"],)).fetchone()[0])["data"]
        self.assertFalse(data["transcript_inline"])
        self.assertEqual(data["transcript_text"], "")
        self.assertEqual(data["transcript_sha256"], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(frontmatter.read(self.md)[1], body)

    def test_legacy_unpublished_completion_is_suppressed_without_erasing_history(self):
        legacy = events.emit("transcription", "completed", {"item_id": self.item})
        events._ensure()
        row = ledger.connect().execute("SELECT * FROM outbox WHERE id=?", (legacy,)).fetchone()
        self.assertEqual(row["suppressed_reason"], "legacy_premature_completion")
        self.assertIsNone(row["published_at"])
        self.assertEqual(json.loads(row["envelope"])["data"]["item_id"], self.item)

    def test_transaction_rolls_back_receipt_and_event_on_failure(self):
        self.complete()
        before = ledger.connect().execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        with patch.object(ledger, "set_item_state", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(RuntimeError):
                finalize.finalize(self.item)
        self.assertEqual(ledger.connect().execute("SELECT COUNT(*) FROM completions").fetchone()[0], 0)
        self.assertEqual(ledger.connect().execute("SELECT COUNT(*) FROM outbox").fetchone()[0], before)

    def test_invalid_body_intent_leaves_metadata_and_body_unchanged(self):
        original = self.md.read_bytes()
        result = {"wax_ep_version": 1, "frontmatter": {"new-field": "must not leak"},
                  "body_replace": {"sha256": "wrong", "text": "replacement"}}
        with self.assertRaises(passes.PassError):
            passes._apply_result(self.item, self.md, {"body_mutation": "compare-and-replace"}, result)
        self.assertEqual(self.md.read_bytes(), original)

    def test_dependency_order_validates_cycles_missing_and_independent_failure(self):
        reg = copy.deepcopy(self.definitions)
        reg["title"]["after"] = ["unknown-future"]
        self.assertEqual(passes.ordered(reg), ["unknown-future", "title"])
        reg["unknown-future"]["requires"] = ["title"]
        with self.assertRaisesRegex(passes.PassError, "cyclic"):
            passes.ordered(reg)
        reg["unknown-future"]["requires"] = ["missing"]
        with self.assertRaisesRegex(passes.PassError, "missing"):
            passes.ordered(reg)


if __name__ == "__main__":
    unittest.main()
