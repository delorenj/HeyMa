import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import paths, state


class StreamStateTest(unittest.TestCase):
    def _write_capture(self, stream: Path, rid: str, *, fin=None) -> None:
        (stream / f"{rid}.rec.json").write_text(json.dumps({
            "rid": rid,
            "pid": 0,
            "starttime": 0,
            "boot_id": "dead-boot",
            "target_name": "test.ogg",
        }))
        (stream / f"{rid}.stop").write_text(json.dumps({
            "rid": rid,
            "owner_pid": 0,
            "owner_starttime": 0,
            "owner_boot_id": "dead-boot",
            "deadline_epoch": time.time() + 60,
        }))
        if fin is not None:
            (stream / f"{rid}.fin.json").write_text(json.dumps({"rid": rid, **fin}))

    def test_failed_fin_uses_recorded_reason_not_finalizer_died(self):
        with tempfile.TemporaryDirectory() as directory:
            stream = Path(directory)
            rid = "20260824-120000-failed"
            self._write_capture(stream, rid, fin={
                "ok": False,
                "reason": "no_valid_segments",
                "segments": 0,
            })

            with patch.object(paths, "STREAM", stream):
                result = state.stream_state(run_preflight=False)

        self.assertEqual(result["state"], "error-partial")
        self.assertEqual(result["cause_code"], "no_valid_segments")
        self.assertEqual(result["fin"]["reason"], "no_valid_segments")
        self.assertIn("fin.json", result["evidence"])

    def test_successful_fin_does_not_keep_stream_in_error(self):
        with tempfile.TemporaryDirectory() as directory:
            stream = Path(directory)
            rid = "20260824-120000-success"
            self._write_capture(stream, rid, fin={"ok": True, "duration_s": 12.0})

            with patch.object(paths, "STREAM", stream):
                result = state.stream_state(run_preflight=False)

        self.assertEqual(result["state"], "ready")
        self.assertIsNone(result["cause_code"])

    def test_dead_finalizer_without_fin_is_still_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            stream = Path(directory)
            rid = "20260824-120000-dead"
            self._write_capture(stream, rid)

            with patch.object(paths, "STREAM", stream):
                result = state.stream_state(run_preflight=False)

        self.assertEqual(result["state"], "error-partial")
        self.assertEqual(result["cause_code"], "finalizer_died")


if __name__ == "__main__":
    unittest.main()