"""Voice fingerprints: who is speaking, as a 192-number TitaNet embedding.

The pure half (turn parsing, speech windows, centroids, matching) is numpy
only, so waxd's /usr/bin/python3 and the tests can use it. The embedder needs
torch and NeMo, so it runs in the diarization venv (`.venv-diarization`), the
same place Sortformer runs.

Model: NeMo TitaNet-large (`nvidia/speakerverification_en_titanet_large`) on
CUDA, about 9 s to load and 0.3 s per 10 s window on the 3090. Vinyl's live
captions use TitaNet-small through sherpa-onnx; this is batch work over whole
recordings, so it takes the larger model.

A speaker's fingerprint in one recording is the mean of the embeddings of its
speech windows, after dropping windows that disagree with the rest (overlap,
crosstalk, a diarizer slip). Matching compares that against every exemplar a
registered voice has collected; see `match`.

**Channels.** A fingerprint belongs to a domain: `call` (a phone channel:
narrowband, codec) or `room` (a mono recording on a desk or phone mic). Phone
audio pulls every voice toward the same corner, so raw, two different callers
scored 0.55 and a caller scored 0.74 against the owner. Scoring therefore
subtracts each domain's mean fingerprint first (`config/voiceprint-domains.json`,
measured 2026-11-11) and prefers exemplars from the fingerprint's own domain; a
voice heard only in the other domain is scored against cross-channel thresholds,
because the same person scores lower across channels (0.52 median) than within one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MODEL_NAME = "nvidia/speakerverification_en_titanet_large"
SAMPLE_RATE = 16000
DIM = 192

# A turn line as scripts/transcribe.py renders it: **Speaker 2 (01:51):** or **Me (1:02:03):**
TURN_RE = re.compile(r"^\*\*(?P<label>[^*\n()]+?) \((?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\):\*\*", re.M)

WINDOW_MIN_S = 1.5    # shorter windows embed poorly
WINDOW_MAX_S = 10.0   # longer turns are split; TitaNet saturates well before this
TRIM_S = 0.15         # turn edges carry the previous speaker's tail
OUTLIER_COS = 0.35    # a window this far from its speaker's mean is someone else


def parse_timestamp(text: str) -> float:
    parts = [int(p) for p in text.split(":")]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return float(seconds)


def parse_turns(body: str) -> list[tuple[str, float]]:
    """(label, start seconds) for each speaker turn in a rendered transcript body."""
    return [(m["label"].strip(), parse_timestamp(m["ts"])) for m in TURN_RE.finditer(body)]


def turn_regions(turns: list[tuple[str, float]], duration: float) -> dict[str, list[tuple[float, float]]]:
    """Each turn runs from its start to the next turn's start (the last to the end)."""
    regions: dict[str, list[tuple[float, float]]] = {}
    for index, (label, start) in enumerate(turns):
        end = turns[index + 1][1] if index + 1 < len(turns) else duration
        if end > start:
            regions.setdefault(label, []).append((start, end))
    return regions


def segment_regions(segments: list[dict]) -> dict[int, list[tuple[float, float]]]:
    """Channel index -> (start, end) of each timed ASR segment (channel-separated calls)."""
    regions: dict[int, list[tuple[float, float]]] = {}
    for seg in segments:
        speaker = seg.get("speaker")
        start, end = seg.get("start"), seg.get("end")
        if speaker is None or start is None or end is None or end <= start:
            continue
        regions.setdefault(int(speaker), []).append((float(start), float(end)))
    return regions


def windows(regions: list[tuple[float, float]], *, min_s: float = WINDOW_MIN_S,
            max_s: float = WINDOW_MAX_S, trim: float = TRIM_S) -> list[tuple[float, float]]:
    """Speech windows to embed: regions trimmed at both edges and cut to at most max_s."""
    out = []
    for start, end in regions:
        start, end = start + trim, end - trim
        length = end - start
        if length < min_s:
            continue
        pieces = max(1, int(np.ceil(length / max_s)))
        step = length / pieces
        for i in range(pieces):
            a = start + i * step
            out.append((a, a + step))
    return out


def normalize(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32)
    length = float(np.linalg.norm(vector))
    return vector / length if length > 0 else vector


def centroid(embeddings: np.ndarray, weights: np.ndarray | None = None,
             outlier_cos: float = OUTLIER_COS) -> tuple[np.ndarray | None, int]:
    """Weighted mean of unit embeddings after one outlier-rejection round; (vector, windows kept)."""
    embeddings = np.asarray(embeddings, dtype=np.float32)
    if embeddings.size == 0:
        return None, 0
    weights = np.ones(len(embeddings), dtype=np.float32) if weights is None else np.asarray(weights, dtype=np.float32)
    first = normalize((embeddings * weights[:, None]).sum(axis=0))
    keep = embeddings @ first >= outlier_cos
    if not keep.any():
        return first, len(embeddings)
    return normalize((embeddings[keep] * weights[keep][:, None]).sum(axis=0)), int(keep.sum())


DOMAINS = ("call", "room")
DOMAINS_FILE = Path(__file__).resolve().parents[2] / "config" / "voiceprint-domains.json"
_means: dict[str, np.ndarray] | None = None


def domain_means() -> dict[str, np.ndarray]:
    global _means
    if _means is None:
        import json

        try:
            raw = json.loads(DOMAINS_FILE.read_text())["means"]
            _means = {d: np.asarray(v, dtype=np.float32) for d, v in raw.items()}
        except (OSError, ValueError, KeyError):
            _means = {}
    return _means


def center(vec: np.ndarray, domain: str) -> np.ndarray:
    """The fingerprint with its channel's average voice taken out, back on the unit sphere."""
    mean = domain_means().get(domain)
    return normalize(np.asarray(vec, dtype=np.float32) - mean) if mean is not None else normalize(vec)


def voice_score(fingerprint: np.ndarray, exemplars: np.ndarray) -> float:
    """How much a (centred) fingerprint sounds like a voice: mean of its two best exemplar cosines.

    One exemplar is one earlier recording's fingerprint. The best single
    exemplar alone lets one odd recording vouch for a stranger; the mean of the
    top two (or the only one) is steadier.
    """
    exemplars = np.asarray(exemplars, dtype=np.float32)
    if exemplars.ndim == 1:
        exemplars = exemplars[None, :]
    sims = np.sort(exemplars @ fingerprint)[::-1]
    return float(sims[:2].mean())


@dataclass(frozen=True)
class Thresholds:
    """Measured 2026-11-11 on centred fingerprints (references/passes/voice-id.md)."""
    match: float = 0.60        # same channel: the same caller across calls, median 0.67
    new: float = 0.40          # same channel: different callers, 95th percentile 0.56 but mostly < 0.35
    match_cross: float = 0.55  # other channel only: the owner's desk mic against his phone line, median 0.52
    new_cross: float = 0.30    # other channel only: a stranger against the owner, median 0.20
    margin: float = 0.08       # the best voice must beat the next by this much (in headroom)
    learn: float = 0.68        # a match this strong becomes an exemplar of the voice
    learn_cross: float = 0.58  # ... and teaches a voice its other channel
    min_match_s: float = 4.0   # less embedded speech than this is never matched
    min_new_s: float = 20.0    # ... nor registered as a new voice with less than this
    min_consistency: float = 0.5  # a new voice's windows must agree with each other this well
    owner_solo: float = 0.45       # a one-voice recording (a voice memo) is the owner from this score...
    owner_solo_cross: float = 0.40  # ... or this against his other channel (the owner across: 0.52 median)


@dataclass(frozen=True)
class Match:
    voice_id: str | None     # the best voice, or None with an empty registry
    score: float | None      # its score
    runner_up: float | None  # the next voice's score
    cross: bool              # the best voice was only ever heard on the other channel
    clear: bool              # the best voice beat the next by the margin
    stranger: bool           # no voice came near (below `new` on every one)

    def learnable(self, t: Thresholds) -> bool:
        return self.score is not None and self.score >= (t.learn_cross if self.cross else t.learn)


NO_MATCH = Match(None, None, None, False, True, True)


def match(fingerprint: np.ndarray, domain: str, voices: dict[str, list[tuple[str, np.ndarray]]],
          t: Thresholds = Thresholds()) -> Match:
    """The registered voice this raw fingerprint sounds most like.

    `voices` maps a voice id to its exemplars as (domain, raw vector). Each
    voice is scored on its same-channel exemplars when it has any, otherwise on
    the others with the cross-channel thresholds; voices are ranked by
    headroom over their own match threshold.
    """
    if not voices:
        return NO_MATCH
    probe = center(fingerprint, domain)
    ranked, stranger = [], True
    for voice_id, exemplars in voices.items():
        same = [center(v, d) for d, v in exemplars if d == domain]
        cross = not same
        pool = same or [center(v, d) for d, v in exemplars]
        if not pool:
            continue
        score = voice_score(probe, np.stack(pool))
        if score >= (t.new_cross if cross else t.new):
            stranger = False
        ranked.append((score - (t.match_cross if cross else t.match), score, cross, voice_id))
    if not ranked:
        return NO_MATCH
    ranked.sort(reverse=True)
    headroom, score, cross, voice_id = ranked[0]
    clear = len(ranked) == 1 or headroom - ranked[1][0] >= t.margin
    return Match(voice_id, score, ranked[1][1] if len(ranked) > 1 else None, cross, clear, stranger)


def classify(m: Match, seconds: float, consistency: float | None, t: Thresholds = Thresholds()) -> str:
    """matched | new | uncertain | too_short."""
    if seconds < t.min_match_s:
        return "too_short"
    if m.score is not None and m.score >= (t.match_cross if m.cross else t.match):
        return "matched" if m.clear else "uncertain"
    if m.stranger:
        if seconds >= t.min_new_s and (consistency is None or consistency >= t.min_consistency):
            return "new"
        return "too_short"
    return "uncertain"


PHONE_LABEL = re.compile(r"^\+?[\d\s().-]{7,}$")
NOT_A_NAME = re.compile(r"^(channel [a-z0-9]+|unassigned|unknown.*|anonymous|private.*|restricted|"
                        r"wireless caller|me|speaker \d+)$", re.I)


def caller_name(label: str) -> str | None:
    """The contact name a call's far channel was labelled with, or None (a number, `Channel B`, ...)."""
    label = (label or "").strip()
    if not label or PHONE_LABEL.match(label) or NOT_A_NAME.match(label):
        return None
    return label


@dataclass(frozen=True)
class Decision:
    """What the pass does with one speaker."""
    verdict: str              # matched | new | uncertain | too_short | owner | owner_new | caller_id | caller_id_new
    voice_id: str | None      # the voice heard (None: nobody, or a voice still to register)
    register_as: str | None   # register a new voice: "" unnamed (notify), or the name it already has
    learn: bool               # keep this fingerprint as an exemplar of voice_id
    match: Match              # the open match against every voice, for the record


def decide(*, label: str, mode: str, channel: int | None, vec: np.ndarray | None, seconds: float,
           consistency: float | None, domain: str, voices: dict[str, list[tuple[str, np.ndarray]]],
           names: dict[str, str | None], owner: str | None, t: Thresholds = Thresholds(),
           cached: bool = False) -> Decision:
    """One speaker's verdict, from its fingerprint and the registry (pure: the pass and the replay share it).

    `names` maps voice id to its name (None when unnamed). What is known for
    certain comes first: a call's channel A is the owner (our own leg, both
    directions); a call's far channel labelled with a contact name is that
    contact unless the voice clearly is not; a recording with one voice is the
    owner when it sounds enough like him. Everything else is matched openly.
    """
    m = match(vec, domain, voices, t) if vec is not None else NO_MATCH
    if vec is None or seconds < t.min_match_s:
        return Decision("too_short", None, None, False, m)

    def named(name: str) -> str | None:
        folded = name.casefold()
        return next((vid for vid, n in names.items() if n and n.casefold() == folded), None)

    def against(voice_id: str) -> Match:
        return match(vec, domain, {voice_id: voices[voice_id]}, t)

    def floor(one: Match) -> float:
        return t.new_cross if one.cross else t.new

    known = None
    if mode == "channels" and owner and channel == 0:
        known = ("owner", owner)
    elif mode == "channels" and channel not in (None, 0) and caller_name(label):
        name = caller_name(label)
        # The owner calling his own line from another phone shows up under his own name.
        if owner and name.split()[0].casefold() == owner.split()[0].casefold():
            name = owner
        known = ("owner" if name == owner else "caller_id", name)
    if known:
        kind, name = known
        voice_id = named(name)
        # Someone else on that phone: the voice is clearly another person we know by name.
        other = (kind == "caller_id" and m.voice_id not in (None, voice_id) and names.get(m.voice_id)
                 and classify(m, seconds, consistency, t) == "matched")
        if not other:
            if voice_id is None:
                return Decision(f"{kind}_new", None, name, False, m)
            # Learn every sighting: one person's phone voice varies more by device (car,
            # speaker, handset) than two people differ, so a known line must cover them all.
            return Decision(kind, voice_id, None, not cached, m)
    elif mode == "whole" and owner and (owner_id := named(owner)) is not None:
        one = against(owner_id)
        rival = m.voice_id != owner_id and classify(m, seconds, consistency, t) == "matched"
        if not rival and one.score >= (t.owner_solo_cross if one.cross else t.owner_solo):
            return Decision("owner", owner_id, None, not cached, m)

    verdict = classify(m, seconds, consistency, t)
    if verdict == "matched":
        return Decision("matched", m.voice_id, None, not cached and m.learnable(t), m)
    if verdict == "new":
        if cached:  # a stranger registered on an earlier run whose voice was since merged away
            return Decision("uncertain", None, None, False, m)
        return Decision("new", None, "", False, m)
    return Decision(verdict, None, None, False, m)


MAX_WINDOWS = 60       # per speaker; a long meeting is sampled evenly, not exhaustively
CLIP_SECONDS = 15.0    # the listening sample written for a new voice


def spread(spans: list[tuple[float, float]], limit: int = MAX_WINDOWS) -> list[tuple[float, float]]:
    """At most `limit` spans, evenly spaced through the recording."""
    if len(spans) <= limit:
        return list(spans)
    picks = np.linspace(0, len(spans) - 1, limit).round().astype(int)
    return [spans[i] for i in picks]


def fingerprint_item(spec: dict, embedder: "Embedder | None" = None) -> dict:
    """Fingerprint every speaker in one recording (the diarization venv's half of the pass).

    spec: {"audio": path, "regions": {label: {"channel": int | None, "spans": [[start, end], ...]}},
           "clip_dir": dir or None}
    Returns {label: {"vec", "seconds", "windows", "kept", "consistency", "clip"}}; a label
    with nothing long enough to embed comes back with "vec": None.
    """
    import librosa

    embedder = embedder or Embedder()
    regions = spec["regions"]
    stereo = any(r.get("channel") is not None for r in regions.values())
    signal, _ = librosa.load(spec["audio"], sr=SAMPLE_RATE, mono=not stereo)
    out = {}
    for label, region in regions.items():
        channel = region.get("channel")
        track = signal if signal.ndim == 1 else (signal[channel] if channel is not None else signal.mean(axis=0))
        limit = track.shape[-1] / SAMPLE_RATE  # a turn's end can be "the end of the recording"
        spans = spread(windows([(a, min(b, limit)) for a, b in region["spans"] if a < limit]))
        emb, seconds, used = embedder.embed(track, spans)
        spans = [spans[i] for i in used]
        vec, kept = centroid(emb, seconds)
        if vec is None:
            out[label] = {"vec": None, "seconds": 0.0, "windows": 0, "kept": 0, "consistency": None, "clip": None}
            continue
        sims = emb @ vec
        good = sims >= OUTLIER_COS
        clip = None
        if spec.get("clip_dir"):
            clip = _write_clip(track, [s for s, ok in zip(spans, good) if ok],
                               sims[good], Path(spec["clip_dir"]) / f"{_slug(label)}.wav")
        out[label] = {"vec": [float(x) for x in vec], "seconds": float(seconds[good].sum()),
                      "windows": int(len(emb)), "kept": int(kept),
                      "consistency": float(sims[good].mean()) if good.any() else None, "clip": clip}
    return out


def _slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-") or "speaker"


def _write_clip(track: np.ndarray, spans: list[tuple[float, float]], sims: np.ndarray, target: Path) -> str | None:
    """The speaker's most typical windows, in time order, joined by short gaps."""
    import soundfile

    if not spans:
        return None
    chosen, total = [], 0.0
    for index in np.argsort(-sims):
        a, b = spans[int(index)]
        if total + (b - a) > CLIP_SECONDS and chosen:
            continue
        chosen.append((a, b))
        total += b - a
        if total >= CLIP_SECONDS:
            break
    gap = np.zeros(int(0.4 * SAMPLE_RATE), dtype=np.float32)
    pieces = []
    for a, b in sorted(chosen):
        pieces += [track[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)].astype(np.float32), gap]
    target.parent.mkdir(parents=True, exist_ok=True)
    soundfile.write(str(target), np.concatenate(pieces[:-1]), SAMPLE_RATE)
    return str(target)


class Embedder:
    """TitaNet-large on CUDA (the diarization venv only)."""

    def __init__(self, device: str = "cuda"):
        import torch
        from nemo.collections.asr.models import EncDecSpeakerLabelModel

        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("voice fingerprints need CUDA; torch.cuda.is_available() is false")
        self._torch = torch
        self.model = EncDecSpeakerLabelModel.from_pretrained(MODEL_NAME, map_location=device)
        self.model.eval()
        self.device = str(self.model.device)

    def embed(self, signal: np.ndarray, spans: list[tuple[float, float]],
              batch: int = 16) -> tuple[np.ndarray, np.ndarray, list[int]]:
        """(unit embeddings, seconds behind each, index of each one's span) for a 16 kHz mono signal.

        A span that runs past the end of the signal is cut there, and dropped
        when what is left is too short to embed.
        """
        torch = self._torch
        rows = []
        clips, used = [], []
        for index, (a, b) in enumerate(spans):
            clip = signal[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)]
            if len(clip) >= int(WINDOW_MIN_S * SAMPLE_RATE * 0.9):
                clips.append(clip)
                used.append(index)
        seconds = np.asarray([len(c) / SAMPLE_RATE for c in clips], dtype=np.float32)
        for i in range(0, len(clips), batch):
            group = clips[i:i + batch]
            longest = max(len(c) for c in group)
            audio = np.zeros((len(group), longest), dtype=np.float32)
            for j, clip in enumerate(group):
                audio[j, :len(clip)] = clip
            with torch.no_grad():
                _, emb = self.model.forward(
                    input_signal=torch.from_numpy(audio).to(self.device),
                    input_signal_length=torch.tensor([len(c) for c in group], device=self.device))
            rows.extend(normalize(e) for e in emb.cpu().numpy())
        return np.asarray(rows, dtype=np.float32).reshape(-1, DIM), seconds, used
