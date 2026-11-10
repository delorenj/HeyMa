"""Speaker turns come from thresholded frame activity, and words go to the speaker who said them.

Pins the fix for transcript 1878 (2026-11-09): segment-level attribution put two
people in one block and began the next block mid-sentence; argmax turns forced
silence onto a speaker. Pure Python: no GPU, no model.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

from wax import speaker_attribution as sa

REPO = Path(__file__).resolve().parents[3]


def _frames(spec, n_spk=4):
    """'AAA..BB' -> frame probabilities: letter = speaker index, '.' = silence."""
    rows = []
    for ch in spec:
        row = [0.05] * n_spk
        if ch != ".":
            row[ord(ch) - ord("A")] = 0.9
        rows.append(row)
    return rows


class TurnsFromFramesTest(unittest.TestCase):
    def test_silence_is_not_given_to_any_speaker(self):
        turns = sa.turns_from_frames(_frames("AA..BB"), 0.5)
        self.assertEqual(turns, [
            {"start_time": 0.0, "end_time": 1.0, "speaker": 0},
            {"start_time": 2.0, "end_time": 3.0, "speaker": 1},
        ])

    def test_a_pause_splits_one_speaker_into_two_turns(self):
        turns = sa.turns_from_frames(_frames("A.A"), 1.0)
        self.assertEqual([(t["start_time"], t["end_time"]) for t in turns], [(0.0, 1.0), (2.0, 3.0)])

    def test_overlap_goes_to_the_more_probable_speaker(self):
        turns = sa.turns_from_frames([[0.6, 0.8, 0.0, 0.0]], 0.08)
        self.assertEqual(turns[0]["speaker"], 1)


class SegmentWordsTest(unittest.TestCase):
    def test_real_word_times_win(self):
        words, timed = sa.segment_words({"start": 0, "end": 2, "text": "hi there",
                                         "words": [[0.1, 0.4, " hi"], [0.5, 1.9, "there"]]})
        self.assertTrue(timed)
        self.assertEqual([(w["start"], w["text"]) for w in words], [(0.1, "hi"), (0.5, "there")])

    def test_an_untimed_word_among_timed_ones_is_placed_not_dropped(self):
        words, timed = sa.segment_words({"start": 0, "end": 3, "text": "hello 123 world",
                                         "words": [[0, 0.5, "hello"], [None, None, "123"], [1.0, 1.5, "world"]]})
        self.assertTrue(timed)
        self.assertEqual([(w["text"], w["start"], w["end"]) for w in words],
                         [("hello", 0.0, 0.5), ("123", 0.5, 1.0), ("world", 1.0, 1.5)])

    def test_old_asr_without_words_is_interpolated_by_length(self):
        words, timed = sa.segment_words({"start": 10.0, "end": 14.0, "text": "a bbbbbb"})
        self.assertFalse(timed)
        self.assertEqual(words[0]["start"], 10.0)
        self.assertAlmostEqual(words[-1]["end"], 14.0)
        self.assertLess(words[0]["end"] - words[0]["start"], words[1]["end"] - words[1]["start"])


class AttributeSegmentsTest(unittest.TestCase):
    TURNS = [{"start_time": 0.0, "end_time": 3.0, "speaker": 2},
             {"start_time": 3.0, "end_time": 6.0, "speaker": 0}]

    def test_a_segment_spanning_a_turn_change_is_split_there(self):
        seg = {"start": 0.0, "end": 6.0, "text": "two versions of the admin page let's say your booking",
               "words": [[0.0, 0.5, "two"], [0.5, 1.0, "versions"], [1.0, 1.4, "of"], [1.4, 1.8, "the"],
                         [1.8, 2.4, "admin"], [2.4, 2.9, "page"], [3.1, 3.5, "let's"], [3.5, 3.9, "say"],
                         [3.9, 4.5, "your"], [4.5, 5.5, "booking"]]}
        pieces = sa.attribute_segments([seg], self.TURNS)
        self.assertEqual([(p["speaker"], p["text"]) for p in pieces], [
            (0, "two versions of the admin page"),
            (1, "let's say your booking"),
        ])
        self.assertEqual(pieces[1]["start"], 3.1)

    def test_speakers_are_numbered_by_first_word_with_no_gaps(self):
        # Diarizer slot 2 speaks first and slot 0 second: they render as Speaker 1 and 2.
        seg = {"start": 0.0, "end": 6.0, "text": "hello there general kenobi"}
        speakers = [p["speaker"] for p in sa.attribute_segments([seg], self.TURNS)]
        self.assertEqual(speakers, [0, 1])

    def test_a_one_word_flicker_inside_a_turn_is_absorbed(self):
        turns = [{"start_time": 0.0, "end_time": 1.0, "speaker": 0},
                 {"start_time": 1.0, "end_time": 1.2, "speaker": 1},
                 {"start_time": 1.2, "end_time": 3.0, "speaker": 0}]
        seg = {"start": 0.0, "end": 3.0, "text": "one two three four",
               "words": [[0.0, 0.9, "one"], [1.0, 1.2, "two"], [1.3, 2.0, "three"], [2.1, 3.0, "four"]]}
        pieces = sa.attribute_segments([seg], turns)
        self.assertEqual([(p["speaker"], p["text"]) for p in pieces], [(0, "one two three four")])

    def test_a_word_in_a_diarizer_gap_goes_to_the_nearest_turn(self):
        turns = [{"start_time": 0.0, "end_time": 1.0, "speaker": 0},
                 {"start_time": 10.0, "end_time": 11.0, "speaker": 1}]
        seg = {"start": 0.0, "end": 11.0, "text": "a b c",
               "words": [[0.2, 0.8, "a"], [1.2, 1.5, "b"], [10.2, 10.8, "c"]]}
        pieces = sa.attribute_segments([seg], turns)
        self.assertEqual([(p["speaker"], p["text"]) for p in pieces], [(0, "a b"), (1, "c")])

    def test_the_nearest_turn_can_be_a_long_one_that_started_long_before(self):
        # Review finding: three short turns between a long turn's start and the word
        # hid the long turn (gap 10 s) behind a short one 39 s away.
        turns = [{"start_time": 0.0, "end_time": 100.0, "speaker": 0},
                 {"start_time": 50.0, "end_time": 51.0, "speaker": 1},
                 {"start_time": 60.0, "end_time": 61.0, "speaker": 2},
                 {"start_time": 70.0, "end_time": 71.0, "speaker": 3}]
        self.assertEqual(sa._TurnIndex(turns).speaker_at(108.0, 112.0), 0)
        self.assertEqual(sa._TurnIndex(turns + [{"start_time": 113.0, "end_time": 114.0, "speaker": 3}])
                         .speaker_at(108.0, 112.0), 3)

    def test_no_turns_leaves_text_unattributed(self):
        pieces = sa.attribute_segments([{"start": 0.0, "end": 1.0, "text": "hi"}], [])
        self.assertEqual(pieces, [{"speaker": None, "start": 0.0, "end": 1.0, "text": "hi"}])


class RendererTest(unittest.TestCase):
    """scripts/transcribe.py renders the split pieces as speaker blocks."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("wax_tx_render_test", REPO / "scripts" / "transcribe.py")
        cls.tx = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tx)

    def test_a_mid_segment_turn_change_starts_a_new_block(self):
        result = {"duration": 6.0, "language": "en", "model": "large-v3", "backend": "faster-whisper",
                  "text": "", "segments": [{"start": 0.0, "end": 6.0, "text": "admin page let's say",
                                            "words": [[0.5, 1.5, "admin"], [1.5, 2.5, "page"],
                                                      [3.2, 4.0, "let's"], [4.0, 5.0, "say"]]}]}
        md = self.tx.to_markdown(result, "/x/a.ogg", False, AttributeSegmentsTest.TURNS, True, "cuda", "cuda:0")
        body = md.split("# Transcription: a.ogg", 1)[1]
        self.assertIn("**Speaker 1 (00:00):** admin page\n\n**Speaker 2 (00:03):** let's say", body)

    def test_undiarized_render_is_unchanged(self):
        result = {"duration": 2.0, "language": "en", "model": "m", "backend": "b", "text": "",
                  "segments": [{"start": 0.0, "end": 1.0, "text": "one"}, {"start": 1.0, "end": 2.0, "text": "two"}]}
        md = self.tx.to_markdown(result, "/x/a.ogg", False, None)
        self.assertTrue(md.rstrip().endswith("one two"))


if __name__ == "__main__":
    unittest.main()
