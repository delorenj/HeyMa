import random
import unittest

from wax.transcriptions import chunking
from wax.transcriptions.chunking import HARD_CAP, OVERLAP, token_estimate

WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa "
         "quebec romeo sierra tango uniform victor whiskey xray yankee zulu").split()


def words(n, seed=0, punct=False):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        w = rng.choice(WORDS)
        if punct and i % 12 == 11:
            w += "."
        out.append(w)
    return " ".join(out)


def diarized(turns, per_turn=40, labels=("Speaker 1", "Speaker 2", "Gershon")):
    parts = ["# Transcription: meeting.mp3", ""]
    for t in range(turns):
        minutes, seconds = divmod(t * 37, 60)
        parts.append(f"**{labels[t % len(labels)]} ({minutes:02d}:{seconds:02d}):** {words(per_turn, seed=t)}")
        parts.append("")
    return "\n".join(parts)


class ChunkingTest(unittest.TestCase):
    def assertSlices(self, body, chunks):
        for c in chunks:
            self.assertEqual(body[c.char_start:c.char_end], c.text)
            self.assertLessEqual(c.token_estimate, HARD_CAP)
            self.assertEqual(c.token_estimate, token_estimate(c.search_text))

    def test_short_note_is_one_chunk(self):
        body = diarized(5, per_turn=20)
        self.assertLessEqual(token_estimate(body), HARD_CAP)
        chunks = chunking.chunk(body)
        self.assertEqual(len(chunks), 1)
        self.assertSlices(body, chunks)
        self.assertNotIn("Transcription:", chunks[0].search_text)
        self.assertTrue(chunks[0].search_text.startswith("Speaker 1: "))
        self.assertEqual(chunks[0].speakers, ["Speaker 1", "Speaker 2", "Gershon"])
        self.assertEqual(chunks[0].turn_count, 5)

    def test_diarized_body_breaks_only_at_turn_boundaries(self):
        body = diarized(80, per_turn=45)
        chunks = chunking.chunk(body)
        self.assertGreater(len(chunks), 3)
        self.assertSlices(body, chunks)
        ends = {m.end() for m in __import__("re").finditer(r"[^\n]+(?=\n\n|\n?$)", body)}
        for c in chunks:
            self.assertIn(c.char_end, ends, "a chunk ended mid-turn")

    def test_overlap_is_real_text_between_1_and_60_tokens(self):
        body = diarized(80, per_turn=45)
        chunks = chunking.chunk(body)
        for a, b in zip(chunks, chunks[1:]):
            self.assertLess(b.char_start, a.char_end)
            self.assertGreater(b.char_start, a.char_start)
            overlap = token_estimate(body[b.char_start:a.char_end])
            self.assertGreaterEqual(overlap, 1)
            self.assertLessEqual(overlap, OVERLAP)

    def test_unpunctuated_monologue_turn_splits_into_labelled_word_windows(self):
        body = "**Speaker 1 (00:00):** " + words(2600, seed=7) + "\n"
        self.assertGreater(token_estimate(body), 3000)
        chunks = chunking.chunk(body)
        self.assertGreater(len(chunks), 6)
        self.assertSlices(body, chunks)
        for c in chunks:
            self.assertTrue(c.search_text.startswith("Speaker 1: "), c.search_text[:40])
            self.assertEqual(c.speakers, ["Speaker 1"])

    def test_punctuated_long_turn_splits_at_sentences(self):
        body = "**Speaker 1 (00:00):** " + words(1500, seed=3, punct=True) + "\n"
        chunks = chunking.chunk(body)
        self.assertSlices(body, chunks)
        for c in chunks[:-1]:
            self.assertTrue(c.text.endswith("."), c.text[-20:])

    def test_h1_and_legacy_bullets_are_stripped(self):
        body = ("# Transcription: x.mp3\n\n- **Source**: `/a/x.mp3`\n- **Date**: 2026-07-21 16:45\n"
                "- **Duration**: 172m 44s\n\n---\n\n**Speaker 1 (00:00):** hello there friend\n")
        parsed = chunking.parse(body)
        self.assertEqual(parsed.h1_name, "x.mp3")
        self.assertEqual(parsed.legacy["Duration"], "172m 44s")
        chunks = chunking.chunk(body)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].search_text, "Speaker 1: hello there friend")
        self.assertNotIn("Source", chunks[0].text)

    def test_summary_section_is_its_own_chunk(self):
        body = ("# Transcription: x.mp3\n\n## Summary\n\nThey agreed to ship on Friday.\n\n## Transcript\n\n"
                "**Speaker 1 (00:00):** we should ship friday\n\n**Speaker 2 (00:05):** agreed\n")
        chunks = chunking.chunk(body)
        kinds = [c.kind for c in chunks]
        self.assertEqual(kinds, ["summary", "transcript"])
        self.assertEqual(chunks[0].search_text, "They agreed to ship on Friday.")
        self.assertNotIn("Friday.", chunks[1].search_text)
        self.assertSlices(body, chunks)

    def test_timestamps(self):
        self.assertEqual(chunking.parse_timestamp("172:44"), 10364.0)
        self.assertEqual(chunking.parse_timestamp("1:02:03"), 3723.0)
        body = "**Speaker 1 (172:44):** late words here\n\n**Speaker 2 (2:00:01):** later words\n"
        chunks = chunking.chunk(body)
        self.assertEqual(chunks[0].start_s, 10364.0)
        parsed = chunking.parse(body)
        self.assertEqual([s.ts for s in parsed.turns], [10364.0, 7201.0])

    def test_named_speaker_counts_and_stoplist_does_not(self):
        parsed = chunking.parse("**Gershon:** hi there\n\n**Meeting Date:** today\n\n**Mary Ann Smith (01:00):** hey\n")
        self.assertEqual([s.label for s in parsed.turns], ["Gershon", "Mary Ann Smith"])
        self.assertEqual(chunking.chunk("**Meeting Date:** today\n")[0].search_text, "Meeting Date: today")

    def test_deterministic(self):
        body = diarized(60, per_turn=50)
        first = [c.search_sha256 for c in chunking.chunk(body)]
        second = [c.search_sha256 for c in chunking.chunk(body)]
        self.assertEqual(first, second)

    def test_start_end_seconds(self):
        body = diarized(80, per_turn=45)
        chunks = chunking.chunk(body)
        for a, b in zip(chunks, chunks[1:]):
            self.assertEqual(a.end_s, b.start_s)
        self.assertIsNone(chunks[-1].end_s)
        plain = chunking.chunk(words(30) + "\n\n" + words(30, seed=2) + "\n")
        self.assertIsNone(plain[0].start_s)

    def test_empty_body_has_no_chunks(self):
        self.assertEqual(chunking.chunk("\n# Transcription: x.mp3\n"), [])
        self.assertEqual(chunking.chunk(""), [])

    def test_overlong_word_never_breaks_the_cap(self):
        body = "x" * 5000 + " tail words\n"
        chunks = chunking.chunk(body)
        self.assertSlices(body, chunks)


if __name__ == "__main__":
    unittest.main()
