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

    def test_run_still_holds_the_item_lock(self):
        """b40825d's first cut left run() undecorated: a manual run could race waxd's sweep on the same item."""
        seen = []
        real = passes._frozen_plan_entry

        def spy(item_id, slug):
            seen.append(item_id in getattr(operations._local, "held", set()))
            return real(item_id, slug)

        with patch.object(passes, "_frozen_plan_entry", side_effect=spy):
            passes.run(self.item, "diarization")
        self.assertEqual(seen, [True])

    def test_a_malformed_plan_falls_back_to_the_live_registry(self):
        for bad in ("not json", json.dumps(["a list"]), json.dumps({"other": {"version": 1}}),
                    json.dumps({"diarization": {"version": "v-two"}})):
            ledger.connect().execute("UPDATE processing_plans SET definitions=? WHERE item_id=?", (bad, self.item))
            self.assertEqual(passes._frozen_plan_entry(self.item, "diarization"), (None, None), bad)

    def test_items_without_a_plan_keep_the_live_registry(self):
        ledger.connect().execute("DELETE FROM processing_plans WHERE item_id=?", (self.item,))
        self.assertEqual(passes._frozen_plan_entry(self.item, "diarization"), (None, None))
        passes.run(self.item, "diarization")
        row = ledger.connect().execute(
            "SELECT definition_id FROM passes WHERE item_id=? AND ep_slug='diarization'", (self.item,)).fetchone()
        self.assertEqual(row["definition_id"], operations.definition_id(self.registry["diarization"]))

    def test_live_redoes_a_pass_of_a_finished_item_under_the_live_definition(self):
        # `wax ep run --live`: the classification v2 reclassification. A finished item's plan is history
        # (replan refuses it), so the operator runs the LIVE definition; finalize stays unchanged.
        self.assertEqual(passes.run(self.item, "diarization")["state"], "completed")
        self.assertTrue(finalize.finalize(self.item)["finalized"])
        live = {**self.planned, "version": 2}
        self.registry = {"diarization": live}
        result = passes.run(self.item, "diarization", live=True)
        self.assertEqual(result["state"], "completed")
        row = ledger.connect().execute(
            "SELECT definition_id, version FROM passes WHERE item_id=? AND ep_slug='diarization'",
            (self.item,)).fetchone()
        self.assertEqual((row["definition_id"], row["version"]), (operations.definition_id(live), 2))
        self.assertTrue(finalize.finalize(self.item)["unchanged"])

    def test_live_refuses_an_unfinished_item(self):
        with self.assertRaises(passes.PassError) as caught:
            passes.run(self.item, "diarization", live=True)
        self.assertIn("unfinished plan", str(caught.exception))
        self.assertIsNone(ledger.connect().execute(
            "SELECT 1 FROM passes WHERE item_id=? AND ep_slug='diarization'", (self.item,)).fetchone())


if __name__ == "__main__":
    unittest.main()
