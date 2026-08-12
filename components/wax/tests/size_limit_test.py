import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import config, transcribe_adapter


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


if __name__ == "__main__":
    unittest.main()
