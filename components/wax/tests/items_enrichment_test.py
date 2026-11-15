"""`wax --json items --id <item>`: one item's enrichment status, the read the pilot harvest worker polls.

The authority for "all enrichment passes are done" is the `completions` row finalize() writes; items.state
'complete' also counts. Exhausted passes are listed so a waiter can stop waiting on them.
"""
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from wax import ledger, paths

COMPONENT_ROOT = Path(__file__).resolve().parents[1]
WAX = COMPONENT_ROOT / "bin" / "wax"
NOW = "2026-11-15T13:24:00.000Z"


class ItemEnrichmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)  # a failing setUp skips tearDown; the paths patch must never leak
        for name, value in {"VAR": self.root / "var", "DB": self.root / "var/wax.db"}.items():
            self.stack.enter_context(patch.object(paths, name, value))
        self.close()
        self.item = "0123456789abcdef"
        conn = ledger.connect()
        conn.execute("INSERT INTO items(item_id,sha256,path,state,first_seen,updated_at) VALUES(?,?,?,?,?,?)",
                     (self.item, "0" * 64, "/nowhere/a.ogg", "enrichment_pending", NOW, NOW))
        conn.execute("INSERT INTO processing_plans(item_id,plan_id,definitions,created_at) VALUES(?,?,?,?)",
                     (self.item, "plan-1", "{}", NOW))
        for slug, state, exhausted in (("title-slug", "completed", None), ("voice-id", "failed", NOW),
                                       ("diarization", "skipped", NOW)):
            conn.execute("INSERT INTO passes(item_id,ep_slug,version,state,attempt,updated_at,reason_code,exhausted_at)"
                         " VALUES(?,?,?,?,?,?,?,?)", (self.item, slug, 2, state, 3, NOW,
                                                      "provider_error" if state == "failed" else None, exhausted))

    def close(self):
        conn = getattr(ledger._local, "conn", None)
        if conn:
            conn.close()
            del ledger._local.conn

    def tearDown(self):
        self.close()
        self.temp.cleanup()

    def complete(self):
        ledger.connect().execute("INSERT INTO completions VALUES(?,?,?,?,?,?)",
                                 (self.item, "plan-1", "event-1", 7, "f" * 64, NOW))

    def test_pending_item_lists_passes_and_exhausted_without_completion(self):
        status = ledger.item_enrichment(self.item)
        self.assertFalse(status["enrichment_complete"])
        self.assertIsNone(status["completion"])
        self.assertEqual(status["state"], "enrichment_pending")
        self.assertEqual(status["plan_id"], "plan-1")
        self.assertEqual(status["exhausted"], ["voice-id"], "a pass skipped after it was exhausted is not stuck")
        self.assertEqual([p["ep_slug"] for p in status["passes"]], ["diarization", "title-slug", "voice-id"])
        self.assertEqual(status["passes"][1], {"ep_slug": "voice-id", "state": "failed", "version": 2, "attempt": 3,
                                               "updated_at": NOW, "reason_code": "provider_error"})

    def test_completion_row_is_the_authority(self):
        self.complete()
        status = ledger.item_enrichment(self.item)
        self.assertTrue(status["enrichment_complete"])
        self.assertEqual(status["completion"], {"plan_id": "plan-1", "event_id": "event-1", "created_at": NOW})

    def test_item_state_complete_also_counts(self):
        ledger.connect().execute("UPDATE items SET state='complete' WHERE item_id=?", (self.item,))
        self.assertTrue(ledger.item_enrichment(self.item)["enrichment_complete"])

    def test_unknown_item_is_none(self):
        self.assertIsNone(ledger.item_enrichment("ffffffffffffffff"))

    def test_cli_reads_one_item_and_refuses_an_unknown_one(self):
        self.complete()
        self.close()
        env = {**os.environ, "WAX_ROOT": str(self.root)}
        run = lambda *a: subprocess.run([str(WAX), "--json", "items", *a], env=env, capture_output=True,
                                        text=True, timeout=60)
        hit = run("--id", self.item)
        self.assertEqual(hit.returncode, 0, hit.stderr)
        body = json.loads(hit.stdout)
        self.assertTrue(body["enrichment_complete"])
        self.assertEqual(body["exhausted"], ["voice-id"])
        miss = run("--id", "ffffffffffffffff")
        self.assertNotEqual(miss.returncode, 0)
        self.assertEqual(json.loads(miss.stdout)["error"], "unknown_item")
        counts = run()
        self.assertEqual(counts.returncode, 0, counts.stderr)
        self.assertEqual(json.loads(counts.stdout), {"enrichment_pending": 1, "total": 1})


if __name__ == "__main__":
    unittest.main()
