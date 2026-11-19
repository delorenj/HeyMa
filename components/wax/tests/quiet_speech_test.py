"""Quiet speech under a "silent" level is lifted and transcribed; true silence still parks.

HEYMA-38 (2026-11-19): ten Mac Voice Memos measured mean -70..-82 dB and were parked as
junk_silent, but a +40 dB copy of each transcribed to 72..4830 coherent words. The input level
was ~35-50 dB too low, and faster-whisper's VAD discarded the speech. The worker now profiles a
level-silent RECORDING (never a Telnyx call), transcribes a gained private copy when the
profile moves like speech, and still parks flat silence without a GPU run.
"""

import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import junk_gate_test as jg
from wax import frontmatter, ledger, sanity

HAVE_FFMPEG = shutil.which("ffmpeg") is not None

# ~-73 dB RMS bursts (one second on, one off) over digital zero: speech-shaped, level-silent.
GATED = "aevalsrc='0.0003*sin(2*PI*300*t)*lt(mod(t\\,2)\\,1)':s=16000:d=20"
# Flat white noise at about the same level: a live but empty input.
FLAT = "anoisesrc=color=white:amplitude=0.0003:r=16000:d=20"


def render(tmp: Path, name: str, graph: str) -> Path:
    out = tmp / f"{name}.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", graph,
                    str(out)], check=True)
    return out


class JudgeProfileTest(unittest.TestCase):
    """The pure verdict over half-second windows (dB)."""

    def test_flat_input_is_not_speech(self):
        self.assertFalse(sanity.judge_profile([-79.8] * 400)["quiet_speech"])

    def test_digital_silence_is_not_speech(self):
        self.assertFalse(sanity.judge_profile([-120.0] * 400)["quiet_speech"])

    def test_a_few_clicks_in_a_long_silence_are_not_speech(self):
        # 1.5 s of bumps in an hour of room tone (the live Yeti's pattern): under 2% active.
        levels = [-64.5] * 7200
        levels[100:103] = [-50.0] * 3
        self.assertFalse(sanity.judge_profile(levels)["quiet_speech"])

    def test_speech_moving_over_its_floor_is_quiet_speech_and_gets_capped_gain(self):
        levels = ([-94.0] * 60 + [-75.0] * 40) * 4
        verdict = sanity.judge_profile(levels)
        self.assertTrue(verdict["quiet_speech"])
        self.assertEqual(verdict["loud_db"], -75.0)
        self.assertEqual(verdict["gain_db"], 50.0, "capped at GAIN_MAX_DB")
        self.assertEqual(sanity.judge_profile(([-80.0] * 60 + [-60.0] * 40) * 4)["gain_db"], 36.0)

    def test_the_150103_shape_is_nominated(self):
        # The hardest real positive: a -80 dB hiss floor with speech only ~12 dB above it.
        levels = [-79.7] * 120 + [-74.5] * 700 + [-71.0] * 200 + [-68.1] * 102
        self.assertTrue(sanity.judge_profile(levels)["quiet_speech"])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
class SpeechProfileTest(unittest.TestCase):
    """sanity.speech_profile and sanity.apply_gain against real ffmpeg."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="quiet-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_gated_quiet_tone_is_quiet_speech_and_flat_noise_is_not(self):
        self.assertTrue(sanity.audio_levels(render(self.tmp, "g", GATED))["silent"])
        self.assertTrue(sanity.speech_profile(render(self.tmp, "g", GATED))["quiet_speech"])
        self.assertFalse(sanity.speech_profile(render(self.tmp, "f", FLAT))["quiet_speech"])

    def test_apply_gain_lifts_the_level_keeps_the_mtime_and_never_touches_the_source(self):
        src = render(self.tmp, "g", GATED)
        before = src.read_bytes()
        import os
        os.utime(src, (1_700_000_000, 1_700_000_000))
        out = sanity.apply_gain(src, self.tmp / "gain" / "g.flac", 45.0)
        self.assertIsNotNone(out)
        self.assertEqual(src.read_bytes(), before)
        self.assertEqual(int(out.stat().st_mtime), 1_700_000_000)
        self.assertFalse(sanity.audio_levels(out)["silent"])

    def test_the_copy_is_mono_16k_s16_so_a_long_memo_stays_small(self):
        src = render(self.tmp, "st", "aevalsrc='0.0003*sin(2*PI*300*t)|0.0003*sin(2*PI*300*t)':s=48000:d=4")
        out = sanity.apply_gain(src, self.tmp / "o.flac", 40.0)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=channels,sample_rate,sample_fmt",
                                "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
        self.assertEqual(sorted(probe.split(",")), ["1", "16000", "s16"])

    def test_gained_audio_lives_only_inside_the_block(self):
        src = render(self.tmp, "g", GATED)
        work = self.tmp / "logs" / "gain"
        with sanity.gained_audio(src, 40.0, work) as heard:
            self.assertTrue(heard.is_file())
            self.assertEqual(heard.parent, work)
        self.assertFalse(heard.exists())
        self.assertFalse(work.exists())
        with sanity.gained_audio(src, None, work) as heard:
            self.assertIsNone(heard)
        with self.assertRaises(RuntimeError), sanity.gained_audio(src, 40.0, work) as heard:
            raise RuntimeError("boom")
        self.assertFalse(heard.exists(), "removed even when the block raises")

    def test_fails_open(self):
        bogus = self.tmp / "x.ogg"
        bogus.write_bytes(b"not audio")
        self.assertIsNone(sanity.speech_profile(bogus))
        self.assertIsNone(sanity.apply_gain(bogus, self.tmp / "o.flac", 10.0))
        self.assertFalse((self.tmp / "o.flac").exists())


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
class QuietSpeechWorkerTest(jg.JunkGateHarness):
    """End to end through worker.run_once with real ffmpeg audio and the fake transcriber."""

    def payload(self, graph: str) -> bytes:
        return render(self.root, "src", graph).read_bytes()

    def test_quiet_speech_is_transcribed_from_a_gained_copy_and_published(self):
        audio = self.drop(jg.MEMO_NAME, jg.words(30), payload=self.payload(GATED))
        result = self.run_once()
        self.assertNotIn("skipped", result)
        calls = self.transcriber_calls()
        self.assertEqual(len(calls), 1)
        heard = Path(calls[0])
        self.assertEqual(heard.parent, self.root / "var/logs" / self.item / "gain")
        self.assertTrue(heard.name.startswith("20261116-120000-memo.") and heard.suffix == ".flac", heard)
        self.assertFalse(heard.exists(), "the gained copy is private and removed")
        self.assertFalse(heard.parent.exists(), "and so is its folder")
        self.assertEqual(self.transcript_rows(), 1)
        row = ledger.connect().execute("SELECT * FROM items WHERE item_id=?", (self.item,)).fetchone()
        self.assertLessEqual(row["audio_mean_db"], sanity.SILENT_MEAN_DB)
        self.assertGreater(row["audio_gain_db"], 30.0)
        self.assertEqual(result["quiet_speech"]["gain_db"], row["audio_gain_db"])
        note = next(p for p in self.vault.rglob("*.md"))
        fm, _ = frontmatter.read(note)
        self.assertEqual(fm["source-audio"], str(audio.resolve()), "provenance names the real audio")
        self.assertEqual(fm[frontmatter.WAX_KEY]["input_gain_db"], row["audio_gain_db"])
        # Quiet is reported (a device level to fix), never counted as silence.
        health = ledger.silent_health()
        self.assertEqual(health["count"], 0)
        self.assertEqual(health["sources"], [])
        self.assertEqual(health["quiet"]["count"], 1)

    def test_a_failed_transcription_records_no_gain_and_announces_nothing(self):
        self.sanity_verdict = {**jg.SANITY_OK, "ok": False, "reason_code": "duration_ratio"}
        self.drop(jg.MEMO_NAME, jg.words(30), payload=self.payload(GATED))
        from wax import worker
        result = self.run_once()
        self.assertIn("error", result)
        self.assertNotIn("quiet_speech", result)
        row = ledger.connect().execute("SELECT audio_gain_db FROM items WHERE item_id=?", (self.item,)).fetchone()
        self.assertIsNone(row["audio_gain_db"])
        worker.desktop.notify_quiet_recording.assert_not_called()
        self.assertEqual(ledger.silent_health()["quiet"]["count"], 0)
        self.assertFalse((self.root / "var/logs" / self.item / "gain").exists())

    def test_asr_json_is_rebound_to_the_items_own_audio(self):
        import json
        from wax import transcribe_adapter
        audio = self.drop(jg.MEMO_NAME, jg.words(30), payload=self.payload(GATED))
        asr = self.root / "asr.json"
        asr.write_text(json.dumps({"version": 1, "source_sha256": "sha-of-the-gained-copy", "result": {}}))
        transcribe_adapter._rebind_asr_source(asr, audio, self.item, 44.5)
        doc = json.loads(asr.read_text())
        sha = ledger.connect().execute("SELECT sha256 FROM items WHERE item_id=?", (self.item,)).fetchone()["sha256"]
        self.assertEqual(doc["source_sha256"], sha, "the diarization pass checks this against items.sha256")
        self.assertEqual(doc["input_gain_db"], 44.5)

    def test_flat_silence_is_still_parked_without_a_gpu_run(self):
        audio = self.drop(jg.MEMO_NAME, jg.words(30), payload=self.payload(FLAT))
        result = self.run_once()
        self.assertEqual(result["skipped"]["reason"], "junk_silent")
        self.assertEqual(self.transcriber_calls(), [])
        self.assertTrue((self.root / "skipped/silent" / audio.name).is_file())

    def test_a_gained_copy_that_holds_no_words_is_parked_as_silent_and_counted(self):
        audio = self.drop(jg.MEMO_NAME, "", payload=self.payload(GATED))
        result = self.run_once()
        self.assertEqual(result["skipped"]["reason"], "junk_silent")
        self.assertEqual(len(self.transcriber_calls()), 1)
        self.assertTrue((self.root / "skipped/silent" / audio.name).is_file())
        self.assert_nothing_published()
        row = ledger.connect().execute("SELECT audio_gain_db FROM items WHERE item_id=?", (self.item,)).fetchone()
        self.assertIsNone(row["audio_gain_db"])
        self.assertEqual(ledger.silent_health()["count"], 1)

    def test_a_telnyx_call_is_never_relevelled(self):
        audio = self.drop(jg.CALL_NAME, jg.words(60), payload=self.payload(GATED))
        with patch.object(sanity, "speech_profile") as profile:
            result = self.run_once()
        profile.assert_not_called()
        self.assertEqual(result["skipped"]["reason"], "junk_silent")
        self.assertEqual(self.transcriber_calls(), [])
        self.assertTrue((self.root / "skipped/silent" / audio.name).is_file())

    def test_wax_retry_of_a_parked_quiet_memo_transcribes_it_with_gain(self):
        # Today's ten memos: parked before this probe existed, released with `wax retry <id>`.
        audio = self.drop(jg.MEMO_NAME, jg.words(30), payload=self.payload(GATED))
        with patch.object(sanity, "speech_profile", return_value=None):
            self.assertEqual(self.run_once()["skipped"]["reason"], "junk_silent")
        from wax import worker
        worker.retry_item(self.item)
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.assertIn("quiet_speech", result)
        self.assertEqual(self.transcript_rows(), 1)
        self.assertFalse((self.root / "skipped/silent" / audio.name).exists())


if __name__ == "__main__":
    unittest.main()
