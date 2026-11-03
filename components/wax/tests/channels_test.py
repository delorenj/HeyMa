"""Dual-channel Telnyx calls are transcribed per channel; the diarization pass then stands down.

Hermetic: ffmpeg/ffprobe are replaced by injected probes, except the two tests that synthesise a
tiny stereo file and are skipped when ffmpeg is missing. No GPU, no model, no network.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import channels, diarization_pass, frontmatter, ledger, paths
from documents_test import _close_test_connection

CALL = "telnyx-2026-10-30-6f1c2d3e-aaaa-bbbb-cccc-0123456789ab-1761800000-" + "a" * 64 + ".mp3"
VOICEMAIL = "telnyx-vm-2026-11-03-abc.wav"


def levels(a, b):
    return lambda _p: [a, b]


class DetectionTest(unittest.TestCase):
    def plan(self, name, count=2, lv=(-30.0, -25.0)):
        return channels.plan(Path("/x") / name, channels_fn=lambda _p: count, levels_fn=levels(*lv))

    def test_stereo_call_with_both_channels_audible_is_split(self):
        p = self.plan(CALL)
        self.assertTrue(p.use)
        self.assertEqual(p.reason, "channel_separated")
        self.assertEqual(p.levels, [-30.0, -25.0])

    def test_one_sided_recording_falls_back_to_mono(self):
        for quiet in (-90.0, -120.0, -60.0):
            with self.subTest(quiet=quiet):
                p = self.plan(CALL, lv=(-30.0, quiet))
                self.assertFalse(p.use)
                self.assertEqual(p.reason, "channel_silent")
        self.assertFalse(self.plan(CALL, lv=(-90.0, -30.0)).use, "silence on channel 0 is just as one-sided")

    def test_just_above_the_threshold_still_counts_as_audio(self):
        self.assertTrue(self.plan(CALL, lv=(-30.0, -59.0)).use)

    def test_voicemail_and_other_names_keep_the_normal_path(self):
        for name, reason in ((VOICEMAIL, "voicemail"), ("record_0016.ogg", "not_telnyx_call"),
                             ("meeting-telnyx-x.mp3", "not_telnyx_call")):
            with self.subTest(name=name):
                p = self.plan(name)
                self.assertFalse(p.use)
                self.assertEqual(p.reason, reason)

    def test_mono_or_unprobeable_call_keeps_the_normal_path(self):
        self.assertEqual(self.plan(CALL, count=1).reason, "not_stereo")
        self.assertEqual(self.plan(CALL, count=None).reason, "probe_failed")
        p = channels.plan(Path(CALL), channels_fn=lambda _p: 2, levels_fn=lambda _p: [])
        self.assertEqual((p.use, p.reason), (False, "levels_unavailable"))

    def test_kill_switch(self):
        with patch.dict("os.environ", {"WAX_CHANNEL_SPLIT": "0"}):
            self.assertEqual(self.plan(CALL).reason, "disabled")

    def test_astats_parser_reads_channels_not_the_overall_block(self):
        log = ("[Parsed_astats_0 @ 0x1] Channel: 1\n[Parsed_astats_0 @ 0x1] RMS level dB: -21.5\n"
               "[Parsed_astats_0 @ 0x1] Channel: 2\n[Parsed_astats_0 @ 0x1] RMS level dB: -inf\n"
               "[Parsed_astats_0 @ 0x1] Overall\n[Parsed_astats_0 @ 0x1] RMS level dB: -24.4\n")
        self.assertEqual(channels.parse_astats(log), [-21.5, -120.0])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_real_stereo_file_levels_and_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / CALL
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=1",
                            "-f", "lavfi", "-i", "sine=f=880:d=1,volume=0.1",
                            "-filter_complex", "[0][1]amerge=inputs=2", "-ar", "8000", str(src)], check=True)
            self.assertEqual(channels.stream_channels(src), 2)
            a, b = channels.channel_levels(src)
            self.assertGreater(a, b + 10)
            self.assertTrue(channels.plan(src).use)
            wav = channels.extract_channel(src, 1, Path(d) / "c1.wav")
            self.assertEqual(channels.stream_channels(wav), 1)
            self.assertLess(channels.channel_levels(wav)[0], a - 10, "channel 1 is the quiet one")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_real_one_sided_file_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / CALL
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=1",
                            "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono:d=1",
                            "-filter_complex", "[0][1]amerge=inputs=2", "-ar", "8000", str(src)], check=True)
            p = channels.plan(src)
            self.assertFalse(p.use)
            self.assertEqual(p.reason, "channel_silent")


class LabelTest(unittest.TestCase):
    def test_without_calls_module_uses_channel_a_b(self):
        with patch.object(channels, "channel_parties", None):
            self.assertEqual(channels.party_labels(CALL), ("Channel A", "Channel B"))

    def test_calls_module_names_win_in_channel_order(self):
        with patch.object(channels, "channel_parties", lambda name: ("Me", "Damian Miller")):
            self.assertEqual(channels.party_labels(CALL), ("Me", "Damian Miller"))

    def test_unknown_party_and_broken_resolver_fall_back(self):
        def boom(_n):
            raise RuntimeError("db down")
        for resolver in (lambda n: None, boom, lambda n: ("Me", ""), lambda n: ("Me", "Me")):
            with self.subTest(resolver=resolver), patch.object(channels, "channel_parties", resolver):
                self.assertEqual(channels.party_labels(CALL), ("Channel A", "Channel B"))


class MergeTest(unittest.TestCase):
    A = [{"start": 0.0, "end": 2.0, "text": "Hello, this is Jarad."},
         {"start": 6.0, "end": 8.0, "text": "Sure, Tuesday."}]
    B = [{"start": 2.5, "end": 5.5, "text": "Hi Jarad."}, {"start": 5.6, "end": 5.9, "text": "Got a minute?"},
         {"start": 9.0, "end": 9.5, "text": "  "}]

    def test_segments_interleave_by_start_and_carry_the_party(self):
        merged = channels.merge_segments([self.A, self.B], ("Me", "Damian"))
        self.assertEqual([(s["start"], s["speaker_label"]) for s in merged],
                         [(0.0, "Me"), (2.5, "Damian"), (5.6, "Damian"), (6.0, "Me")])
        self.assertEqual([s["speaker"] for s in merged], [0, 1, 1, 0])

    def test_tie_goes_to_the_first_leg(self):
        merged = channels.merge_segments([[{"start": 1.0, "end": 2.0, "text": "a"}],
                                          [{"start": 1.0, "end": 2.0, "text": "b"}]], ("A", "B"))
        self.assertEqual([s["text"] for s in merged], ["a", "b"])

    def test_turns_coalesce_consecutive_same_speaker(self):
        merged = channels.merge_segments([self.A, self.B], ("Me", "Damian"))
        turns = channels.speaker_turns(merged)
        self.assertEqual([(t["speaker_label"], t["text"]) for t in turns],
                         [("Me", "Hello, this is Jarad."), ("Damian", "Hi Jarad. Got a minute?"),
                          ("Me", "Sure, Tuesday.")])
        self.assertEqual(turns[1]["start"], 2.5)
        self.assertEqual(turns[1]["end"], 5.9)


class DiarizationSkipTest(unittest.TestCase):
    def note(self, d, fm):
        md = Path(d) / "note.md"
        md.write_text(frontmatter.render(fm, "# Transcription\n\n**Me (00:00):** hi\n"))
        return md

    def test_channel_attributed_note_is_skipped_before_any_gpu_work(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(diarization_pass.subprocess, "run", side_effect=AssertionError("spawned the venv")):
            md = self.note(d, {"channel-attributed": True, "diarized": True})
            self.assertEqual(diarization_pass.main(md, "item-1"),
                             {"wax_ep_version": 1, "state": "skipped", "reason_code": "channel_separated"})
            self.assertEqual(diarization_pass.build_result(md, "item-1")["reason_code"], "channel_separated")

    def test_wax_block_marker_also_skips(self):
        with tempfile.TemporaryDirectory() as d:
            md = self.note(d, {"wax": {"channel_attributed": True}})
            self.assertTrue(diarization_pass.channel_attributed(md))

    def test_timed_asr_is_a_second_witness_when_the_frontmatter_was_edited(self):
        with tempfile.TemporaryDirectory() as d:
            runtime = Path(d)
            asr = runtime / "asr.json"
            asr.write_text(json.dumps({"version": 1, "result": {"channel_separated": True}}))
            with patch.object(paths, "DB", runtime / "wax.db"), patch.object(paths, "VAR", runtime):
                _close_test_connection()
                try:
                    conn = ledger.connect()
                    conn.execute("INSERT INTO items(item_id,sha256,path,state,first_seen,updated_at) VALUES('i1','s','/a.mp3','transcribed','t','t')")
                    conn.execute("INSERT INTO transcripts(item_id,md_path,asr_path,created_at) VALUES('i1','/n.md',?,'t')",
                                 (str(asr),))
                    md = self.note(d, {"title": "edited"})
                    self.assertTrue(diarization_pass.channel_attributed(md, "i1"))
                    self.assertFalse(diarization_pass.channel_attributed(md, "nope"))
                finally:
                    _close_test_connection()

    def test_ordinary_transcript_still_diarizes(self):
        with tempfile.TemporaryDirectory() as d:
            md = self.note(d, {"diarized": False})
            self.assertFalse(diarization_pass.channel_attributed(md))
            self.assertFalse(diarization_pass.channel_attributed(md, None))


if __name__ == "__main__":
    unittest.main()
