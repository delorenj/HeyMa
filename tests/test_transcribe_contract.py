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

    def test_diarization_keeps_prediction_history_constant(self):
        class FakeTensor:
            def __init__(self, shape, values=None):
                self.shape = shape
                self.values = values or []

            def unsqueeze(self, _dim):
                return FakeTensor((1, self.shape[0]), self.values)

            def to(self, _device):
                return self

            def cpu(self):
                return self

            def tolist(self):
                return list(self.values)

            def __getitem__(self, _key):
                return self

        class InferenceMode:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        calls = []
        modules = types.SimpleNamespace(
            n_spk=4, chunk_len=10, subsampling_factor=10,
        )
        model = types.SimpleNamespace(
            device="cpu",
            sortformer_modules=modules,
            preprocessor=types.SimpleNamespace(
                _cfg=types.SimpleNamespace(window_stride=0.01),
            ),
        )

        def forward_streaming_step(**kwargs):
            calls.append(kwargs["total_preds"].shape[1])
            return kwargs["streaming_state"], FakeTensor((1, 2, 4), [0, 1])

        model.forward_streaming_step = forward_streaming_step
        model.to = lambda _device: model
        audio2mel = types.SimpleNamespace(
            to=lambda _device: audio2mel,
            get_features=lambda *_args: (FakeTensor((1, 128, 100)), None),
        )
        class FakeDevice:
            def __init__(self, name):
                self.name = str(name)
                self.type = self.name.split(":", 1)[0]

            def __str__(self):
                return self.name

        fake_torch = types.ModuleType("torch")
        fake_torch.device = FakeDevice
        fake_torch.tensor = lambda value, **_kwargs: FakeTensor((len(value),))
        fake_torch.zeros = lambda shape, **_kwargs: FakeTensor(shape)
        fake_torch.concat = lambda _values, dim=0: FakeTensor((1, 128, 199))
        fake_torch.transpose = lambda _value, _a, _b: FakeTensor((1, 199, 128))
        fake_torch.inference_mode = InferenceMode
        fake_torch.argmax = lambda value, dim=0: FakeTensor((len(value.values),), value.values)
        fake_librosa = types.ModuleType("librosa")
        fake_librosa.load = lambda *_args, **_kwargs: ([0.0] * 48_000, 16_000)
        fake_backend = types.ModuleType(
            "wax.diarization_sortformer"
        )
        fake_backend.load_model = lambda device=None: (model, audio2mel)
        fake_backend.init_streaming_state = lambda *_args, **_kwargs: object()

        modules_patch = {
            "librosa": fake_librosa,
            "torch": fake_torch,
            "wax.diarization_sortformer": fake_backend,
        }
        with patch.dict(sys.modules, modules_patch), contextlib.redirect_stderr(io.StringIO()):
            segments, device = TRANSCRIBE.diarize_local("fake.ogg", "cpu")

        self.assertEqual(calls, [0, 0, 0])
        self.assertTrue(segments)
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
