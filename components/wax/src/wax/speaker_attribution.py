"""Turn Sortformer frame activity into speaker turns, and put words under the right speaker.

Pure Python on purpose: scripts/transcribe.py (the GPU venv) and the tests both
import it, and none of it needs torch or numpy.

Why this exists (incident 2026-11-09, transcript 1878):

* The old renderer gave each Whisper *segment* (about 5 s of lowercase,
  unpunctuated text) to the one speaker that overlapped it most. A segment that
  spans a turn change went whole to one speaker, so blocks mixed two people and
  the next block began mid-sentence. Attribution is now per word: real word
  timestamps when the ASR has them, otherwise times interpolated across the
  segment by character length, and a segment is split where the speaker changes.
* The old turns came from argmax over every frame, so silence was always forced
  onto some speaker. A frame is only speech here when a speaker clears
  ``threshold``.
"""

from __future__ import annotations

import bisect
from typing import Iterable, Optional, Sequence

ACTIVITY_THRESHOLD = 0.5
# A run this short, with the same speaker on both sides, is diarizer jitter, not a
# turn. Interpolated word times are rough, so they need a wider margin.
MAX_FLICKER_WORDS_TIMED = 1
MAX_FLICKER_WORDS_INTERPOLATED = 2


def turns_from_frames(frames: Iterable[Sequence[float]], frame_s: float,
                      threshold: float = ACTIVITY_THRESHOLD) -> list[dict]:
    """Per-frame speaker probabilities -> [{start_time, end_time, speaker}] (speech only).

    A frame belongs to its most probable speaker when that probability clears
    ``threshold``; otherwise it is silence and opens a gap between turns.
    """
    turns: list[dict] = []
    open_turn: Optional[dict] = None
    for index, row in enumerate(frames):
        row = list(row)
        best = max(range(len(row)), key=row.__getitem__) if row else None
        speaker = best if best is not None and row[best] >= threshold else None
        if speaker is None:
            open_turn = None
            continue
        start, end = index * frame_s, (index + 1) * frame_s
        if open_turn is not None and open_turn["speaker"] == speaker:
            open_turn["end_time"] = end
        else:
            open_turn = {"start_time": start, "end_time": end, "speaker": speaker}
            turns.append(open_turn)
    return turns


def segment_words(segment: dict) -> tuple[list[dict], bool]:
    """Words of one ASR segment with times, and whether those times are real.

    Real word timestamps (``segment["words"]`` as ``[start, end, word]`` or
    ``{start, end, word}``) are used when present. Older ASR artifacts have none,
    so each word gets a slice of the segment proportional to its length.
    """
    raw = segment.get("words") or []
    words = []
    for item in raw:
        if isinstance(item, dict):
            start, end, text = item.get("start"), item.get("end"), item.get("word")
        else:
            start, end, text = item[0], item[1], item[2]
        text = (text or "").strip()
        if text and start is not None and end is not None:
            words.append({"start": float(start), "end": float(end), "text": text})
    if words:
        return words, True
    tokens = (segment.get("text") or "").split()
    start, end = float(segment["start"]), float(segment["end"])
    weights = [len(t) + 1 for t in tokens]  # +1 for the space: short words still take time
    total = sum(weights) or 1
    out, cursor = [], start
    for token, weight in zip(tokens, weights):
        span = (end - start) * weight / total
        out.append({"start": cursor, "end": cursor + span, "text": token})
        cursor += span
    return out, False


class _TurnIndex:
    """Overlap and nearest-turn lookups over time-sorted turns."""

    def __init__(self, turns: Sequence[dict]):
        self.turns = sorted(turns, key=lambda t: (t["start_time"], t["end_time"]))
        self.starts = [t["start_time"] for t in self.turns]
        self.max_len = max((t["end_time"] - t["start_time"] for t in self.turns), default=0.0)

    def speaker_at(self, start: float, end: float) -> Optional[int]:
        """Speaker with the most overlap of [start, end]; else the nearest turn's speaker."""
        if not self.turns:
            return None
        lo = bisect.bisect_left(self.starts, start - self.max_len)
        hi = bisect.bisect_right(self.starts, end)
        overlap: dict[int, float] = {}
        for turn in self.turns[lo:hi]:
            amount = min(end, turn["end_time"]) - max(start, turn["start_time"])
            if amount > 0:
                overlap[turn["speaker"]] = overlap.get(turn["speaker"], 0.0) + amount
        if overlap:
            return max(overlap, key=lambda spk: (overlap[spk], -spk))
        mid = (start + end) / 2
        pos = bisect.bisect_right(self.starts, mid)
        best, best_gap = None, None
        for turn in self.turns[max(0, pos - 2):pos + 1] + self._ending_near(mid, pos):
            gap = max(turn["start_time"] - mid, mid - turn["end_time"], 0.0)
            if best_gap is None or gap < best_gap:
                best, best_gap = turn["speaker"], gap
        return best

    def _ending_near(self, mid: float, pos: int) -> list[dict]:
        # A long turn that started well before `mid` can still end closest to it.
        lo = bisect.bisect_left(self.starts, mid - self.max_len)
        return [t for t in self.turns[lo:pos] if t["end_time"] <= mid]


def _smooth(labels: list[Optional[int]], max_run: Sequence[int]) -> list[Optional[int]]:
    """Absorb short runs sandwiched between the same speaker (per-word run limits)."""
    out = list(labels)
    i = 0
    while i < len(out):
        j = i
        while j + 1 < len(out) and out[j + 1] == out[i]:
            j += 1
        if 0 < i and j + 1 < len(out) and out[i - 1] == out[j + 1] and (j - i + 1) <= max(max_run[i:j + 1]):
            for k in range(i, j + 1):
                out[k] = out[i - 1]
            # Re-scan from the start of the merged run.
            i = i - 1
            while i > 0 and out[i - 1] == out[i]:
                i -= 1
            continue
        i = j + 1
    return out


def attribute_segments(segments: Sequence[dict], turns: Sequence[dict]) -> list[dict]:
    """Split ASR segments at speaker changes: [{speaker, start, end, text}].

    Speakers are renumbered 0.. in order of first appearance in the text, so the
    labels have no gaps (a diarizer slot that never owns a word is not a speaker).
    """
    index = _TurnIndex(turns)
    words: list[dict] = []
    for seg_no, seg in enumerate(segments):
        seg_words, timed = segment_words(seg)
        for word in seg_words:
            word["seg"] = seg_no
            word["max_run"] = MAX_FLICKER_WORDS_TIMED if timed else MAX_FLICKER_WORDS_INTERPOLATED
            words.append(word)
    labels = [index.speaker_at(w["start"], w["end"]) for w in words]
    # Fill unknowns (no turns at all) from the previous known speaker.
    last = None
    for i, label in enumerate(labels):
        if label is None:
            labels[i] = last
        else:
            last = label
    labels = _smooth(labels, [w["max_run"] for w in words])
    order: dict[int, int] = {}
    pieces: list[dict] = []
    for word, label in zip(words, labels):
        if label is not None and label not in order:
            order[label] = len(order)
        speaker = order.get(label) if label is not None else None
        if pieces and pieces[-1]["seg"] == word["seg"] and pieces[-1]["speaker"] == speaker:
            pieces[-1]["text"] += " " + word["text"]
            pieces[-1]["end"] = word["end"]
        else:
            pieces.append({"seg": word["seg"], "speaker": speaker, "start": word["start"],
                           "end": word["end"], "text": word["text"]})
    seen: set[int] = set()
    for piece in pieces:
        seg_no = piece.pop("seg")
        if seg_no not in seen:
            # The first piece keeps the segment's own start, so timestamps match the ASR.
            seen.add(seg_no)
            piece["start"] = float(segments[seg_no]["start"])
    return pieces
