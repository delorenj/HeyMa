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
SPEC = importlib.util.spec_from_file_location("heyma_transcribe_channels", SCRIPT)
TRANSCRIBE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(TRANSCRIBE)

from wax import channels  # noqa: E402  (transcribe.py put the component on sys.path)

CALL = "telnyx-2026-10-30-6f1c2d3e-aaaa-bbbb-cccc-0123456789ab-1761800000-" + "a" * 64 + ".mp3"


def seg(start, end, text):
    return types.SimpleNamespace(start=start, end=end, text=f" {text} ")


class FakeModel:
    """One model, asked for each channel in turn; the file name says which channel it is."""

    SPEECH = {
        "ch0.wav": [seg(0.0, 2.0, "Hello, this is Jarad."), seg(6.0, 8.0, "Sure, Tuesday.")],
        "ch1.wav": [seg(2.5, 5.9, "Hi Jarad, got a minute?")],
    }
    loads = 0
    seen: list = []

    def transcribe(self, path, language=None, **kwargs):
        FakeModel.seen.append((Path(path).name, language, kwargs))
        info = types.SimpleNamespace(duration=10.0, language="en", language_probability=0.9)
        return iter(self.SPEECH[Path(path).name]), info


def fake_extract(audio, index, out, sample_rate=16000):
    Path(out).write_bytes(b"wav")
    return Path(out)


class ChannelTranscriptionTest(unittest.TestCase):
    def run_channels(self, labels=("Me", "Damian Miller")):
        FakeModel.loads = 0
        FakeModel.seen = []

        def load(*_a, **_k):
            FakeModel.loads += 1
            return FakeModel()

        plan = channels.Plan(True, "channel_separated", [-30.0, -25.0], labels)
        with patch.object(TRANSCRIBE, "load_whisper", side_effect=load), \
                patch.object(channels, "extract_channel", side_effect=fake_extract), \
                contextlib.redirect_stderr(io.StringIO()):
            return TRANSCRIBE.transcribe_channels("/x/" + CALL, plan, "large-v3", None, "cpu")

    def test_one_model_two_channels_same_settings_merged_by_time(self):
        result = self.run_channels()
        self.assertEqual(FakeModel.loads, 1, "never two Whisper models")
        self.assertEqual([name for name, _l, _k in FakeModel.seen], ["ch0.wav", "ch1.wav"])
        for _name, _lang, kwargs in FakeModel.seen:
            self.assertEqual((kwargs["beam_size"], kwargs["vad_filter"]), (5, True))
        self.assertEqual(FakeModel.seen[1][1], "en", "the second channel is pinned to the confident first language")
        self.assertTrue(result["channel_separated"])
        self.assertEqual(result["duration"], 10.0)
        self.assertEqual([(s["start"], s["speaker_label"], s["text"]) for s in result["segments"]],
                         [(0.0, "Me", "Hello, this is Jarad."), (2.5, "Damian Miller", "Hi Jarad, got a minute?"),
                          (6.0, "Me", "Sure, Tuesday.")])
        self.assertEqual(result["text"], "Hello, this is Jarad. Hi Jarad, got a minute? Sure, Tuesday.")
        for key in ("language", "language_probability", "model", "backend"):
            self.assertIn(key, result)

    def test_markdown_has_party_turns_and_marks_the_note_diarized_and_channel_attributed(self):
        result = self.run_channels()
        text = TRANSCRIBE.to_markdown(result, "/x/" + CALL, False, None, False, None, None)
        self.assertIn("diarized: true", text)
        self.assertIn("channel-attributed: true", text)
        self.assertIn('channel-speakers: ["Me", "Damian Miller"]', text)
        self.assertIn("**Me (00:00):** Hello, this is Jarad.", text)
        self.assertIn("**Damian Miller (00:02):** Hi Jarad, got a minute?", text)
        self.assertLess(text.index("**Me (00:00)"), text.index("**Damian Miller"))
        self.assertLess(text.index("**Damian Miller"), text.index("**Me (00:06)"))
        self.assertNotIn("Speaker 1", text)

    def test_plain_markdown_is_unchanged_by_the_channel_keys(self):
        result = {"text": "Hi.", "segments": [{"start": 0.0, "end": 1.0, "text": "Hi."}], "language": "en",
                  "duration": 1.0, "model": "m", "backend": "faster-whisper"}
        text = TRANSCRIBE.to_markdown(result, "/x/a.ogg", False, None)
        self.assertIn("diarized: false", text)
        self.assertNotIn("channel-attributed", text)

    def main_with(self, audio_name, plan, tmp):
        audio = Path(tmp) / audio_name
        audio.write_bytes(b"audio")
        out = Path(tmp) / "out.md"
        err = io.StringIO()
        plain = {"text": "x", "segments": [{"start": 0.0, "end": 1.0, "text": "x"}], "language": "en",
                 "language_probability": 1.0, "duration": 1.0, "model": "large-v3", "backend": "faster-whisper"}
        chan = {**plain, "channel_separated": True, "channel_labels": ["Me", "Them"],
                "segments": [{"start": 0.0, "end": 1.0, "text": "x", "speaker": 0, "speaker_label": "Me"}]}
        with patch.object(sys, "argv", ["transcribe.py", str(audio), "-o", str(out), "--no-diarization"]), \
                patch.object(channels, "plan", return_value=plan), \
                patch.object(TRANSCRIBE, "transcribe_channels", return_value=chan) as by_channel, \
                patch.object(TRANSCRIBE, "transcribe_local", return_value=plain) as mono, \
                patch.object(TRANSCRIBE, "diarize_local") as diarize, \
                contextlib.redirect_stderr(err):
            TRANSCRIBE.main()
        meta = json.loads(next(line for line in err.getvalue().splitlines()
                               if line.startswith("Transcription-Metadata: "))[len("Transcription-Metadata: "):])
        return meta, by_channel, mono, diarize, out.read_text()

    def test_main_reports_channel_attribution_in_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = channels.Plan(True, "channel_separated", [-30.0, -25.0], ("Me", "Them"))
            meta, by_channel, mono, diarize, note = self.main_with(CALL, plan, tmp)
        by_channel.assert_called_once()
        mono.assert_not_called()
        diarize.assert_not_called()
        self.assertTrue(meta["diarized"])
        self.assertTrue(meta["channel_separated"])
        self.assertEqual(meta["speaker_attribution"], "channel")
        self.assertFalse(meta["diarization_degraded"])
        self.assertEqual(meta["word_count"], 1)
        self.assertIn("channel-attributed: true", note)

    def test_main_falls_back_to_mono_when_the_plan_says_no(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = channels.Plan(False, "channel_silent", [-30.0, -90.0])
            meta, by_channel, mono, _d, note = self.main_with(CALL, plan, tmp)
        mono.assert_called_once()
        by_channel.assert_not_called()
        self.assertFalse(meta["channel_separated"])
        self.assertFalse(meta["diarized"])
        self.assertNotIn("channel-attributed", note)

    def test_voicemail_never_even_probes_the_audio(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(channels, "stream_channels", side_effect=AssertionError("probed")), \
                patch.object(channels, "channel_levels", side_effect=AssertionError("measured")):
            audio = Path(tmp) / "telnyx-vm-2026-11-03-x.wav"
            audio.write_bytes(b"a")
            self.assertEqual(channels.plan(audio).reason, "voicemail")


if __name__ == "__main__":
    unittest.main()
