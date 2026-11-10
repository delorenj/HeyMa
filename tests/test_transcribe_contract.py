import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "transcribe.py"
SPEC = importlib.util.spec_from_file_location("heyma_transcribe_contract", SCRIPT)
TRANSCRIBE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(TRANSCRIBE)


class TranscribeArtifactContractTest(unittest.TestCase):
    def test_default_is_diarized_frontmatter_only_and_has_no_meta_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "meeting.ogg"
            output = root / "meeting.md"
            audio.write_bytes(b"audio")
            result = {
                "text": "Hello there.",
                "segments": [{"start": 0.0, "end": 1.0, "text": "Hello there."}],
                "language": "en",
                "language_probability": 1.0,
                "duration": 1.0,
                "model": "large-v3",
                "backend": "faster-whisper",
            }
            diarization = [{"start_time": 0.0, "end_time": 1.0, "speaker": 0}]
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["transcribe.py", str(audio), "-o", str(output)]), \
                    patch.object(TRANSCRIBE, "missing_diarization_dependencies", return_value=[]), \
                    patch.object(TRANSCRIBE, "transcribe_local", return_value=result), \
                    patch.object(
                        TRANSCRIBE, "diarize_local", return_value=(diarization, "cuda:0")
                    ) as diarize, \
                    contextlib.redirect_stderr(stderr):
                TRANSCRIBE.main()

            diarize.assert_called_once_with(str(audio), "cuda")
            text = output.read_text()
            self.assertTrue(text.startswith("---\n"))
            self.assertIn("diarized: true", text)
            self.assertIn('diarization-device-requested: "cuda"', text)
            self.assertIn('diarization-device: "cuda:0"', text)
            self.assertIn("# Transcription: meeting.ogg", text)
            self.assertIn("**Speaker 1", text)
            self.assertNotIn("- **Source**", text)
            self.assertNotIn("- **Model**", text)
            self.assertFalse(output.with_suffix(".meta.json").exists())
            self.assertIn('Transcription-Metadata: {', stderr.getvalue())

    def test_progress_heartbeat_is_structured_and_bounded(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            TRANSCRIBE.report_progress("diarize", 137, chunks_done=10)
        line = stderr.getvalue().strip()
        self.assertTrue(line.startswith(TRANSCRIBE._PROGRESS_PREFIX))
        record = json.loads(line[len(TRANSCRIBE._PROGRESS_PREFIX):])
        self.assertEqual(record, {"stage": "diarize", "percent": 100, "chunks_done": 10})

    def test_diarization_returns_speech_only_turns_from_one_whole_file_call(self):
        """NeMo owns chunking (one call per file), and silence is not a speaker."""
        calls = []

        class FakeDevice:
            def __init__(self, name):
                self.name = str(name)
                self.type = self.name.split(":", 1)[0]

            def __str__(self):
                return self.name

        model = types.SimpleNamespace(device="cpu")
        fake_torch = types.ModuleType("torch")
        fake_torch.device = FakeDevice
        fake_librosa = types.ModuleType("librosa")
        fake_librosa.load = lambda *_args, **_kwargs: ([0.0] * 48_000, 16_000)
        fake_backend = types.ModuleType("wax.diarization_sortformer")
        fake_backend.load_model = lambda device=None: model

        def frame_probabilities(_model, signal):
            calls.append(len(signal))
            silent, a, b = [0.1, 0.1, 0.0, 0.0], [0.9, 0.1, 0.0, 0.0], [0.1, 0.8, 0.0, 0.0]
            return [a, a, silent, silent, b], 0.5

        fake_backend.frame_probabilities = frame_probabilities
        with patch.dict(sys.modules, {"librosa": fake_librosa, "torch": fake_torch,
                                      "wax.diarization_sortformer": fake_backend}), \
                contextlib.redirect_stderr(io.StringIO()):
            turns, device = TRANSCRIBE.diarize_local("fake.ogg", "cpu")

        self.assertEqual(calls, [48_000])
        self.assertEqual(turns, [{"start_time": 0.0, "end_time": 1.0, "speaker": 0},
                                 {"start_time": 2.0, "end_time": 2.5, "speaker": 1}])
        self.assertEqual(device, "cpu")

    def test_cuda_diarization_never_silently_falls_back(self):
        fake_torch = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False),
            device=lambda value: value,
        )
        with self.assertRaisesRegex(RuntimeError, "CUDA diarization requested"):
            TRANSCRIBE.resolve_diarization_device("cuda", fake_torch)

        self.assertEqual(
            TRANSCRIBE.resolve_diarization_device("auto", fake_torch), "cpu"
        )


if __name__ == "__main__":
    unittest.main()


class ModelAwareVramFloorTest(unittest.TestCase):
    def test_small_models_do_not_wait_for_large_v3_headroom(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("TRANSCRIBE_MIN_FREE_VRAM_MB", None)
            self.assertEqual(TRANSCRIBE.min_free_vram_for("base"), 1200)
            self.assertEqual(TRANSCRIBE.min_free_vram_for("base.en"), 1200)
            self.assertEqual(TRANSCRIBE.min_free_vram_for("large-v3"), TRANSCRIBE._MIN_FREE_VRAM_MB)

    def test_explicit_env_floor_wins(self):
        with patch.dict("os.environ", {"TRANSCRIBE_MIN_FREE_VRAM_MB": "7000"}):
            self.assertEqual(TRANSCRIBE.min_free_vram_for("base"), TRANSCRIBE._MIN_FREE_VRAM_MB)


class VramFloorCallSiteTest(unittest.TestCase):
    def test_transcribe_local_asks_for_the_models_floor(self):
        import os
        fake = types.ModuleType("faster_whisper")

        class _Model:
            def __init__(self, *a, **k):
                pass

            def transcribe(self, *a, **k):
                return iter(()), types.SimpleNamespace(duration=0, language="en", language_probability=1.0)

        fake.WhisperModel = _Model
        os.environ.pop("TRANSCRIBE_MIN_FREE_VRAM_MB", None)
        with patch.dict(sys.modules, {"faster_whisper": fake}), \
                patch.object(TRANSCRIBE, "cuda_runtime_available", return_value=(True, "ok")), \
                patch.object(TRANSCRIBE, "gpu_vram_headroom", return_value=(True, "ok")) as headroom, \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                TRANSCRIBE.transcribe_local("x.ogg", "base", "en")
            except Exception:
                pass
        headroom.assert_called_once_with(1200)
