"""Re-dropping an already-transcribed recording must be loud and non-destructive.

Regression: park_duplicate overwrote items.path with the duplicate copy's path,
wrote no transitions row (complete->complete is swallowed by set_item_state) and
sent no notification, so the user watched a file vanish with no explanation.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import desktop, ledger, paths, worker


def _close_test_connection() -> None:
    connection = getattr(ledger._local, "conn", None)
    if connection is not None:
        connection.close()
        del ledger._local.conn


class ParkDuplicateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for d in ("var", "inbox", "archive/2026/10"):
            (self.root / d).mkdir(parents=True)
        self.stack = [
            patch.object(paths, "AUDIO", self.root),
            patch.object(paths, "VAR", self.root / "var"),
            patch.object(paths, "DB", self.root / "var/wax.db"),
            patch.object(paths, "ARCHIVE", self.root / "archive"),
        ]
        for p in self.stack:
            p.start()
        _close_test_connection()
        self.canonical = self.root / "archive/2026/10/rec.ogg"
        self.canonical.write_bytes(b"audio")
        self.note = self.root / "20261022-ai-design.md"
        conn = ledger.connect()
        conn.execute(
            "INSERT INTO items(item_id,sha256,path,orig_name,bytes,origin,state,first_seen,updated_at) "
            "VALUES('abc','abc',?,'rec.ogg',5,'inbox','complete','t','t')", (str(self.canonical),))
        conn.execute("INSERT INTO transcripts(item_id,md_path,created_at) VALUES('abc',?,'t')",
                     (str(self.note),))
        self.dup = self.root / "inbox/rec.ogg"
        self.dup.write_bytes(b"audio")

    def tearDown(self):
        for p in self.stack:
            p.stop()
        _close_test_connection()
        self.tmp.cleanup()

    def test_keeps_canonical_path_records_transition_and_notifies(self):
        with patch.object(desktop, "notify") as notify:
            result = worker.park_duplicate("abc", self.dup)

        self.assertFalse(self.dup.exists())
        self.assertTrue(Path(result["parked_duplicate"]).exists())
        self.assertEqual(result["transcript"], str(self.note))
        row = ledger.connect().execute("SELECT path,state FROM items WHERE item_id='abc'").fetchone()
        self.assertEqual(row["path"], str(self.canonical))
        self.assertEqual(row["state"], "complete")
        t = ledger.connect().execute(
            "SELECT * FROM transitions WHERE subject='abc' AND cause_code='already_processed'").fetchall()
        self.assertEqual(len(t), 1)
        self.assertEqual((t[0]["machine"], t[0]["from_state"], t[0]["to_state"]),
                         ("item", "complete", "complete"))
        self.assertIn(str(self.note), t[0]["evidence"])
        self.assertIn("duplicate=", t[0]["evidence"])
        notify.assert_called_once()
        self.assertIn("Already transcribed", notify.call_args.args[1])
        self.assertIn(self.note.name, notify.call_args.args[1])

    def test_notification_failure_is_non_fatal(self):
        with patch.object(desktop, "notify", side_effect=RuntimeError("no dbus")):
            result = worker.park_duplicate("abc", self.dup)
        self.assertTrue(Path(result["parked_duplicate"]).exists())
        n = ledger.connect().execute(
            "SELECT COUNT(*) c FROM transitions WHERE cause_code='already_processed'").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_missing_transcript_still_reports(self):
        ledger.connect().execute("DELETE FROM transcripts WHERE item_id='abc'")
        with patch.object(desktop, "notify") as notify:
            result = worker.park_duplicate("abc", self.dup)
        self.assertIsNone(result["transcript"])
        notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
