import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wax import worker


class WorkerEnrichmentTest(unittest.TestCase):
    def test_auto_pass_failure_is_visible_but_does_not_block_safe_parking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "inbox" / "recording.ogg"
            audio.parent.mkdir()
            audio.write_bytes(b"audio")
            parked = root / "archive" / "2026" / "08" / audio.name
            connection = MagicMock()
            connection.execute.return_value.fetchone.return_value = {"state": "pending"}
            claims = []
            enrichment = [{"ep_slug": "title-slug", "state": "failed", "error": "model offline"}]
            with patch.object(worker, "_write_claim", side_effect=lambda _item, _path, stage: claims.append(stage)), \
                    patch.object(worker.archive, "archive", return_value={
                        "s3_key": "2026-08-10/hash-recording.ogg",
                        "bytes": audio.stat().st_size,
                    }), \
                    patch.object(worker.archive, "remote_size", return_value=audio.stat().st_size), \
                    patch.object(worker.config, "transcription_size_allowed", return_value=True), \
                    patch.object(worker.transcribe_adapter, "transcribe", return_value={"md_path": "note.md"}), \
                    patch.object(worker.passes, "run_auto", return_value=enrichment) as run_auto, \
                    patch.object(worker.rename, "move_noclobber", return_value=parked), \
                    patch.object(worker.ledger, "connect", return_value=connection), \
                    patch.object(worker.ledger, "set_item_state") as set_state:
                result = worker.process("item-id", audio)

            run_auto.assert_called_once_with("item-id")
            self.assertIn("enrich", claims)
            self.assertEqual(result["enrichment"], enrichment)
            self.assertEqual(result["parked"], str(parked))
            self.assertEqual(set_state.call_args_list[-1].args[:2], ("item-id", "complete"))

    def test_retry_failed_passes_runs_only_the_requested_failed_passes(self):
        connection = MagicMock()
        item_result = MagicMock()
        item_result.fetchone.return_value = {"orig_name": "recording.ogg"}
        passes_result = MagicMock()
        passes_result.fetchall.return_value = [
            {"ep_slug": "title-slug"},
            {"ep_slug": "wikification"},
        ]
        connection.execute.side_effect = [item_result, passes_result]
        results = [
            {"item_id": "item-id", "ep_slug": "title-slug", "state": "completed"},
            {"item_id": "item-id", "ep_slug": "wikification", "state": "completed"},
        ]

        with patch.object(worker.ledger, "connect", return_value=connection), \
                patch.object(worker.passes, "run", side_effect=results) as run, \
                patch.object(worker, "_log_enrichment", return_value=[]), \
                patch.object(worker, "_announce_done"), \
                patch.object(worker, "_link_archive", return_value=None):
            result = worker.retry_failed_passes(
                "item-id", ("title-slug", "title-slug", "wikification")
            )

        self.assertEqual(
            [call.args for call in run.call_args_list],
            [("item-id", "title-slug"), ("item-id", "wikification")],
        )
        self.assertEqual(result["retried"], 2)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["failed"], 0)


if __name__ == "__main__":
    unittest.main()
