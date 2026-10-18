"""A manual retry (`wax ep run`, the tray) runs under the item's frozen plan, so finalize accepts it.

Regression: three recordings stayed enrichment_pending after a successful manual diarization retry because the run
recorded the LIVE registry definition, which the plan had never frozen (finalize: passes_incomplete).
"""
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from wax import events, finalize, frontmatter, ledger, operations, passes, paths, sentinel


class PlanFrozenManualRetryTest(unittest.TestCase):
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
        self.md = paths.VAULT / "20261018-094742-rec.md"
        self.md.write_text(frontmatter.render({"wax-item-id": self.item}, "\n# Transcript\nBody\n"))
        ledger.connect().execute("INSERT INTO transcripts(item_id,md_path,audio_duration,created_at) VALUES(?,?,?,?)",
                                 (self.item, str(self.md), 60, sentinel.utcnow()))
        ledger.connect().execute("INSERT INTO backups VALUES(?,?,?,?,?,?)",
                                 (self.item, "day/source.ogg", "recordings", audio.stat().st_size,
                                  sentinel.utcnow(), "size+md5"))
        command = self.root / "ok-pass"
        command.write_text("#!/bin/sh\nprintf '%s\\n' '{\"wax_ep_version\": 1}'\n")
        command.chmod(0o755)
        self.planned = {"slug": "diarization", "enabled": True, "auto": True, "version": 1,
                        "command": [str(command), "{md_path}"]}
        self.registry = {"diarization": dict(self.planned)}
        self.stack.enter_context(patch.object(passes, "registry", side_effect=lambda: self.registry))
        self.stack.enter_context(patch.object(passes, "_run_frontmatters"))
        self.stack.enter_context(patch.object(events, "emit_ep_command", return_value="command-id"))
        self.stack.enter_context(patch.object(events, "emit"))
        self.stack.enter_context(patch.object(finalize, "audio_url", return_value="https://s3.example/a"))
        passes.ensure_plan(self.item)
        # The live registry moves on after the plan was minted (here: a capability the plan never froze).
        self.registry = {"diarization": {**self.planned, "capabilities": ["audio"]}}

    def close(self):
        conn = getattr(ledger._local, "conn", None)
        if conn:
            conn.close()
            del ledger._local.conn

    def tearDown(self):
        self.close()
        self.stack.close()
        self.temp.cleanup()

    def test_manual_retry_records_the_frozen_definition_and_finalizes(self):
        self.assertNotEqual(operations.definition_id(self.planned), operations.definition_id(self.registry["diarization"]))
        result = passes.run(self.item, "diarization")
        self.assertEqual(result["state"], "completed")
        row = ledger.connect().execute(
            "SELECT definition_id FROM passes WHERE item_id=? AND ep_slug='diarization'", (self.item,)).fetchone()
        self.assertEqual(row["definition_id"], operations.definition_id(self.planned))
        self.assertTrue(finalize.finalize(self.item)["finalized"])

    def test_items_without_a_plan_keep_the_live_registry(self):
        ledger.connect().execute("DELETE FROM processing_plans WHERE item_id=?", (self.item,))
        self.assertEqual(passes._frozen_plan_entry(self.item, "diarization"), (None, None))
        passes.run(self.item, "diarization")
        row = ledger.connect().execute(
            "SELECT definition_id FROM passes WHERE item_id=? AND ep_slug='diarization'", (self.item,)).fetchone()
        self.assertEqual(row["definition_id"], operations.definition_id(self.registry["diarization"]))


if __name__ == "__main__":
    unittest.main()
