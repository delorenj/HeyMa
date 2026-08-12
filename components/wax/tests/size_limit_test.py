import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wax import config, transcribe_adapter, worker


class TranscriptionSizeLimitTest(unittest.TestCase):
    def test_default_is_decimal_300mb_and_exclusive(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.max_audio_file_size_for_transcription(), 300_000_000)
            self.assertTrue(config.transcription_size_allowed(299_999_999))
            self.assertFalse(config.transcription_size_allowed(300_000_000))

    def test_environment_override_accepts_binary_units(self):
        with patch.dict(os.environ, {"MAX_AUDIO_FILE_SIZE_FOR_TRANSCRIPTION": "2MiB"}):
            self.assertEqual(config.max_audio_file_size_for_transcription(), 2 * 1024**2)

    def test_adapter_blocks_before_resolving_or_launching_transcriber(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "large.ogg"
            audio.write_bytes(b"x" * 10)
            with patch.dict(os.environ, {"MAX_AUDIO_FILE_SIZE_FOR_TRANSCRIPTION": "10B"}), \
                    patch.object(transcribe_adapter, "transcribe_command") as command:
                with self.assertRaises(transcribe_adapter.TranscriptionSizeError):
                    transcribe_adapter.transcribe(audio)
                command.assert_not_called()

    def test_default_duration_ceiling_is_three_hours_and_exclusive(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.max_audio_duration_for_transcription(), 3 * 3600)
            self.assertTrue(config.transcription_duration_allowed(3 * 3600 - 0.001))
            self.assertFalse(config.transcription_duration_allowed(3 * 3600))

    def test_duration_override_accepts_minutes(self):
        with patch.dict(os.environ, {"MAX_AUDIO_DURATION_FOR_TRANSCRIPTION": "90m"}):
            self.assertEqual(config.max_audio_duration_for_transcription(), 5400)

    def test_adapter_blocks_overduration_before_resolving_transcriber(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "long-but-small.ogg"
            audio.write_bytes(b"tiny")
            with patch.dict(os.environ, {"MAX_AUDIO_DURATION_FOR_TRANSCRIPTION": "3h"}), \
                    patch.object(transcribe_adapter.sanity, "probe_duration", return_value=50_044.3), \
                    patch.object(transcribe_adapter, "transcribe_command") as command:
                with self.assertRaises(transcribe_adapter.TranscriptionDurationError):
                    transcribe_adapter.transcribe(audio)
                command.assert_not_called()

    def test_worker_archives_then_preserves_overduration_audio_without_whisper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "inbox" / "overnight.ogg"
            audio.parent.mkdir()
            audio.write_bytes(b"tiny compressed audio")
            moved = root / "skipped" / "overduration" / audio.name
            connection = MagicMock()
            connection.execute.return_value.fetchone.return_value = {"state": "pending"}
            with patch.dict(os.environ, {"MAX_AUDIO_DURATION_FOR_TRANSCRIPTION": "3h"}), \
                    patch.object(worker, "_write_claim"), \
                    patch.object(worker.archive, "archive", return_value={
                        "s3_key": "2026-08-12/hash-overnight.ogg", "bytes": audio.stat().st_size,
                    }), \
                    patch.object(worker.config, "transcription_size_allowed", return_value=True), \
                    patch.object(worker.sanity, "probe_duration", return_value=50_044.3), \
                    patch.object(worker.paths, "SKIPPED", root / "skipped"), \
                    patch.object(worker.rename, "move_noclobber", return_value=moved), \
                    patch.object(worker.ledger, "connect", return_value=connection), \
                    patch.object(worker.ledger, "set_item_state") as set_state, \
                    patch.object(worker.transcribe_adapter, "transcribe") as transcribe:
                result = worker.process("item", audio)

            transcribe.assert_not_called()
            self.assertEqual(result["skipped"]["reason"], "audio_too_long_for_transcription")
            self.assertEqual(set_state.call_args.args[:2], ("item", "skipped"))


if __name__ == "__main__":
    unittest.main()
