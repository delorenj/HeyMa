"""Deterministic transcript chunking (design C.1). PURE: no I/O, no psycopg.

A chunk is a contiguous slice of the stored body (`body[char_start:char_end]
== text`) plus `search_text`, the normalised rendering that is both the FTS
source and the EXACT embedding input. Title and date are deliberately not
prefixed, so frontmatter-only passes never change a chunk hash and never
trigger re-embedding; title/summary context goes into the doc vector instead.

Pipeline:
  1. pre-strip the `# Transcription: <file>` H1 and the legacy
     `- **Key**: value` bullet block (up to its closing `---`);
  2. a `## Summary` section becomes its own `kind='summary'` stream;
  3. units are speaker-turn paragraphs (`**Label (ts):** words`) or plain
     blank-line paragraphs;
  4. a unit over HARD_CAP is split at sentence boundaries, or into word
     windows when Whisper left it unpunctuated; every piece keeps its label;
  5. pieces are packed greedily to TARGET; a note of HARD_CAP tokens or fewer
     is exactly one chunk;
  6. each next chunk starts up to OVERLAP tokens back, on a word boundary,
     after the previous chunk's start.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Optional

CHUNKER_VERSION = 1
TARGET = 400
HARD_CAP = 512
OVERLAP = 60
BYTES_PER_TOKEN = 3.7
# A "word" longer than this many bytes (a pasted blob, a URL run) is cut into
# character runs so that no single word can ever break HARD_CAP.
MAX_WORD_BYTES = 400

TURN_RE = re.compile(
    r"^\*\*(?P<label>Speaker \d+|[A-Z][a-z]+(?: [A-Z][a-z]+){0,2})"
    r"(?: \((?P<ts>\d{1,3}:\d{2}(?::\d{2})?)\))?:\*\*\s*")
STOPLIST = frozenset({"Meeting Date", "Date", "Duration", "Source", "Language", "Model",
                      "Diarization", "Title", "Summary"})
H1_RE = re.compile(r"^# Transcription: *(?P<name>.*?)[ \t]*$", re.M)
LEGACY_BULLET_RE = re.compile(r"^- \*\*(?P<key>[A-Za-z][A-Za-z ]*)\*\*: ?(?P<value>.*?)[ \t]*$")
SUMMARY_RE = re.compile(r"^##[ \t]+Summary[ \t]*$", re.M)
HEADING_RE = re.compile(r"^#{1,2}[ \t]+\S", re.M)
RULE_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
PARA_SPLIT_RE = re.compile(r"\n[ \t]*\n")
WORD_RE = re.compile(r"\S+")


def token_estimate(text: str) -> int:
    """max(1, ceil(utf8 bytes / 3.7)). Deterministic, tokenizer-free."""
    return max(1, math.ceil(len(text.encode("utf-8")) / BYTES_PER_TOKEN))


def _bytes_tokens(n_bytes: int) -> int:
    return max(1, math.ceil(n_bytes / BYTES_PER_TOKEN))


def parse_timestamp(ts: Optional[str]) -> Optional[float]:
    """'01:09' -> 69.0, '172:44' -> 10364.0 (minutes may exceed 59), '1:02:03' -> 3723.0."""
    if not ts:
        return None
    parts = ts.split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    if len(numbers) == 2:
        minutes, seconds = numbers
        return float(minutes * 60 + seconds)
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        return float(hours * 3600 + minutes * 60 + seconds)
    return None


@dataclass
class Word:
    text: str           # normalised: bold markup removed
    start: int          # offsets into the body
    end: int


@dataclass
class Segment:
    """One unit: a speaker turn or a plain paragraph, in body coordinates."""
    start: int
    end: int
    kind: str                       # 'transcript' | 'summary'
    words: list[Word]
    label: Optional[str] = None
    ts: Optional[float] = None

    def render(self, i: int = 0, j: Optional[int] = None) -> str:
        words = self.words[i:j]
        text = " ".join(w.text for w in words)
        return f"{self.label}: {text}" if self.label else text


@dataclass
class Chunk:
    ordinal: int
    kind: str
    char_start: int
    char_end: int
    text: str
    search_text: str
    search_sha256: str
    token_estimate: int
    start_s: Optional[float] = None
    end_s: Optional[float] = None
    speakers: list[str] = field(default_factory=list)
    turn_count: int = 0


@dataclass
class Parsed:
    """Everything the extractor needs to know about a body's structure."""
    h1_name: Optional[str]
    h1_span: Optional[tuple[int, int]]
    legacy: dict[str, str]
    legacy_span: Optional[tuple[int, int]]
    summary_span: Optional[tuple[int, int]]
    segments: list[Segment]

    @property
    def word_count(self) -> int:
        return sum(len(s.words) for s in self.segments)

    @property
    def turns(self) -> list[Segment]:
        return [s for s in self.segments if s.label]


# ------------------------------------------------------------------ parse --

def _strip_markup(token: str) -> str:
    return token.replace("**", "")


def _words(body: str, start: int, end: int) -> list[Word]:
    out: list[Word] = []
    for match in WORD_RE.finditer(body, start, end):
        raw = match.group(0)
        text = _strip_markup(raw)
        if not text:
            continue
        if len(raw.encode("utf-8")) <= MAX_WORD_BYTES:
            out.append(Word(text, match.start(), match.end()))
            continue
        # Cut an overlong run into character windows of <= MAX_WORD_BYTES bytes.
        pos = match.start()
        while pos < match.end():
            size, stop = 0, pos
            while stop < match.end():
                b = len(body[stop].encode("utf-8"))
                if size + b > MAX_WORD_BYTES:
                    break
                size += b
                stop += 1
            piece = _strip_markup(body[pos:stop])
            if piece:
                out.append(Word(piece, pos, stop))
            pos = stop
    return out


def _legacy_block(body: str, after: int) -> tuple[dict[str, str], Optional[tuple[int, int]]]:
    """The `- **Key**: value` bullets that open a legacy note, up to the closing `---`."""
    lines = body[after:].split("\n")
    pos = after
    i = 0
    # skip blank lines between the H1 and the bullets
    while i < len(lines) and not lines[i].strip():
        pos += len(lines[i]) + 1
        i += 1
    if i >= len(lines) or not LEGACY_BULLET_RE.match(lines[i]):
        return {}, None
    start = pos
    values: dict[str, str] = {}
    end = pos
    while i < len(lines):
        line = lines[i]
        match = LEGACY_BULLET_RE.match(line)
        if not match:
            break
        values.setdefault(match.group("key").strip(), match.group("value").strip())
        pos += len(line) + 1
        end = min(pos, len(body))
        i += 1
    # optional blank lines then a closing rule
    j, probe = i, pos
    while j < len(lines) and not lines[j].strip():
        probe += len(lines[j]) + 1
        j += 1
    if j < len(lines) and lines[j].strip() == "---":
        end = min(probe + len(lines[j]) + 1, len(body))
    return values, (start, end)


def parse(body: str) -> Parsed:
    h1 = None
    h1_span = None
    match = H1_RE.search(body)
    # Only the first H1, and only when it opens the body (nothing but blank lines before it).
    if match and not body[:match.start()].strip():
        h1 = match.group("name").strip() or None
        h1_span = (match.start(), min(match.end() + 1, len(body)))
    legacy, legacy_span = _legacy_block(body, h1_span[1] if h1_span else 0)

    summary_span = None
    summary = SUMMARY_RE.search(body)
    if summary:
        nxt = HEADING_RE.search(body, summary.end())
        summary_span = (summary.start(), nxt.start() if nxt else len(body))

    excluded = sorted(span for span in (h1_span, legacy_span, summary_span) if span)
    regions: list[tuple[int, int, str]] = []
    cursor = 0
    for a, b in excluded:
        if a > cursor:
            regions.append((cursor, a, "transcript"))
        cursor = max(cursor, b)
    if cursor < len(body):
        regions.append((cursor, len(body), "transcript"))
    if summary_span:
        # The heading line itself is structure, not content.
        heading_end = body.find("\n", summary_span[0])
        heading_end = summary_span[1] if heading_end < 0 else min(heading_end + 1, summary_span[1])
        regions.append((heading_end, summary_span[1], "summary"))
    regions.sort()

    segments: list[Segment] = []
    for start, end, kind in regions:
        segments.extend(_paragraphs(body, start, end, kind))
    segments.sort(key=lambda s: s.start)
    return Parsed(h1, h1_span, legacy, legacy_span, summary_span, segments)


def _paragraphs(body: str, start: int, end: int, kind: str) -> list[Segment]:
    out: list[Segment] = []
    bounds = list(PARA_SPLIT_RE.finditer(body, start, end))
    spans = []
    pos = start
    for m in bounds:
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, end))
    for a, b in spans:
        raw = body[a:b]
        lead = len(raw) - len(raw.lstrip())
        trail = len(raw.rstrip())
        a2, b2 = a + lead, a + trail
        if a2 >= b2:
            continue
        text = body[a2:b2]
        if RULE_RE.match(text.strip()):
            continue
        label = None
        ts = None
        content_start = a2
        if kind == "transcript":
            turn = TURN_RE.match(text)
            if turn and turn.group("label") not in STOPLIST:
                label = turn.group("label")
                ts = parse_timestamp(turn.group("ts"))
                content_start = a2 + turn.end()
        words = _words(body, content_start, b2)
        if not words and not label:
            continue
        out.append(Segment(a2, b2, kind, words, label, ts))
    return out


# ------------------------------------------------------------------ chunk --

@dataclass
class _Piece:
    seg: int        # index into the stream's segments
    i: int          # word range [i, j)
    j: int

    def render(self, segs: list[Segment]) -> str:
        return segs[self.seg].render(self.i, self.j)


def _split_segment(segs: list[Segment], index: int) -> list[_Piece]:
    """A segment over HARD_CAP: sentence groups up to TARGET, else word windows."""
    seg = segs[index]
    n = len(seg.words)
    label_bytes = len(f"{seg.label}: ".encode("utf-8")) if seg.label else 0

    def size(i: int, j: int) -> int:
        return _bytes_tokens(label_bytes + sum(len(w.text.encode("utf-8")) for w in seg.words[i:j]) + (j - i - 1))

    # sentences: a word ending in . ! ? closes one
    sentences: list[tuple[int, int]] = []
    s = 0
    for k, word in enumerate(seg.words):
        if word.text[-1:] in ".!?":
            sentences.append((s, k + 1))
            s = k + 1
    if s < n:
        sentences.append((s, n))

    def windows(i: int, j: int) -> list[tuple[int, int]]:
        out, a = [], i
        while a < j:
            b = a + 1
            while b < j and size(a, b + 1) <= TARGET:
                b += 1
            out.append((a, b))
            a = b
        return out

    units: list[tuple[int, int]] = []
    for a, b in sentences:
        if size(a, b) > TARGET:
            units.extend(windows(a, b))
        else:
            units.append((a, b))
    pieces: list[_Piece] = []
    cur: Optional[tuple[int, int]] = None
    for a, b in units:
        if cur is None:
            cur = (a, b)
        elif size(cur[0], b) <= TARGET:
            cur = (cur[0], b)
        else:
            pieces.append(_Piece(index, *cur))
            cur = (a, b)
    if cur is not None:
        pieces.append(_Piece(index, *cur))
    return pieces


def _piece_start(segs: list[Segment], p: _Piece) -> int:
    seg = segs[p.seg]
    return seg.start if p.i == 0 else seg.words[p.i].start


def _piece_end(segs: list[Segment], p: _Piece) -> int:
    seg = segs[p.seg]
    return seg.end if p.j >= len(seg.words) else seg.words[p.j - 1].end


def _merge(pieces: list[_Piece]) -> list[_Piece]:
    """Fuse contiguous pieces of one segment (an overlap tail + its continuation)."""
    out: list[_Piece] = []
    for p in pieces:
        if out and out[-1].seg == p.seg and out[-1].j == p.i:
            out[-1] = _Piece(p.seg, out[-1].i, p.j)
        else:
            out.append(_Piece(p.seg, p.i, p.j))
    return out


def _build(body: str, segs: list[Segment], pieces: list[_Piece], kind: str) -> dict:
    pieces = _merge(pieces)
    rendered = [p.render(segs) for p in pieces]
    search_text = "\n".join(rendered)
    start = _piece_start(segs, pieces[0])
    end = _piece_end(segs, pieces[-1])
    labels: list[str] = []
    turns = 0
    ts = None
    seen_turns = set()
    for p in pieces:
        seg = segs[p.seg]
        if seg.label:
            if p.seg not in seen_turns:
                turns += 1
                seen_turns.add(p.seg)
            if seg.label not in labels:
                labels.append(seg.label)
            if ts is None and seg.ts is not None:
                ts = seg.ts
    return {"kind": kind, "char_start": start, "char_end": end, "text": body[start:end],
            "search_text": search_text, "token_estimate": token_estimate(search_text),
            "start_s": ts, "speakers": labels, "turn_count": turns}


def _stream_chunks(body: str, segs: list[Segment], kind: str) -> list[dict]:
    if not segs:
        return []
    whole = [_Piece(k, 0, len(s.words)) for k, s in enumerate(segs)]
    full_bytes = sum(len(p.render(segs).encode("utf-8")) for p in whole) + len(whole) - 1
    if _bytes_tokens(full_bytes) <= HARD_CAP:
        return [_build(body, segs, whole, kind)]

    pieces: list[_Piece] = []
    for k, seg in enumerate(segs):
        rendered = seg.render()
        if token_estimate(rendered) > HARD_CAP and len(seg.words) > 1:
            pieces.extend(_split_segment(segs, k))
        else:
            pieces.append(_Piece(k, 0, len(seg.words)))

    def piece_bytes(p: _Piece) -> int:
        return len(p.render(segs).encode("utf-8"))

    chunks: list[list[_Piece]] = []
    k = 0
    carry: list[_Piece] = []
    while k < len(pieces):
        current = list(carry)
        size = sum(piece_bytes(p) for p in current) + max(0, len(current) - 1)
        added = 0
        while k < len(pieces):
            b = piece_bytes(pieces[k])
            grown = size + b + (1 if current else 0)
            if added == 0 or _bytes_tokens(grown) <= TARGET:
                current.append(pieces[k])
                size = grown
                added += 1
                k += 1
            else:
                break
        chunks.append(current)
        carry = _overlap(body, segs, current, pieces[k] if k < len(pieces) else None) if k < len(pieces) else []
    return [_build(body, segs, c, kind) for c in chunks]


def _overlap(body: str, segs: list[Segment], chunk: list[_Piece], following: Optional[_Piece]) -> list[_Piece]:
    """Tail of `chunk` (<= OVERLAP tokens of verbatim text) that the next chunk repeats."""
    if following is None:
        return []
    budget = OVERLAP
    nxt = len(following.render(segs).encode("utf-8"))
    # never let overlap + the next piece break HARD_CAP
    room = HARD_CAP - _bytes_tokens(nxt + 1)
    budget = min(budget, room - 1)
    if budget < 1:
        return []
    chunk_start = _piece_start(segs, chunk[0])
    end = _piece_end(segs, chunk[-1])
    # candidate starts: word starts inside the chunk, walking back from its end
    best: Optional[tuple[int, int]] = None        # (piece index in chunk, word index)
    for ci in range(len(chunk) - 1, -1, -1):
        p = chunk[ci]
        seg = segs[p.seg]
        stop = False
        for wi in range(p.j - 1, p.i - 1, -1):
            # Starting at a turn's first word means starting at its label markup.
            start = seg.start if wi == 0 else seg.words[wi].start
            if start <= chunk_start:
                stop = True
                break
            if _bytes_tokens(len(body[start:end].encode("utf-8"))) > budget:
                stop = True
                break
            best = (ci, wi)
        if stop:
            break
    if best is None:
        return []
    ci, wi = best
    first = chunk[ci]
    out = [_Piece(first.seg, wi, first.j)] + [_Piece(p.seg, p.i, p.j) for p in chunk[ci + 1:]]
    # rendered overlap must also fit beside the next piece
    rendered = sum(len(p.render(segs).encode("utf-8")) for p in out) + len(out)
    if _bytes_tokens(rendered + nxt) > HARD_CAP:
        return []
    return out


def chunk(body: str, parsed: Optional[Parsed] = None) -> list[Chunk]:
    """Chunks of the body, in body order, deterministic for a given input."""
    parsed = parsed or parse(body)
    transcript = [s for s in parsed.segments if s.kind == "transcript"]
    summary = [s for s in parsed.segments if s.kind == "summary"]
    raw = _stream_chunks(body, summary, "summary") + _stream_chunks(body, transcript, "transcript")
    raw.sort(key=lambda c: (c["char_start"], c["kind"]))
    out: list[Chunk] = []
    for ordinal, c in enumerate(raw):
        out.append(Chunk(ordinal=ordinal, kind=c["kind"], char_start=c["char_start"],
                         char_end=c["char_end"], text=c["text"], search_text=c["search_text"],
                         search_sha256=hashlib.sha256(c["search_text"].encode("utf-8")).hexdigest(),
                         token_estimate=c["token_estimate"], start_s=c["start_s"],
                         speakers=c["speakers"], turn_count=c["turn_count"]))
    # end_s: the next transcript chunk's first turn timestamp
    timed = [c for c in out if c.kind == "transcript"]
    for a, b in zip(timed, timed[1:]):
        a.end_s = b.start_s
    return out
