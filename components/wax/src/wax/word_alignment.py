"""Word times for ASR segments by CTC forced alignment (torchaudio MMS_FA).

Speaker attribution needs to know when each word was said. Whisper can emit word
timestamps, but on the shared GPU that needs more VRAM than plain decoding: with
5.2 GB free (above the 5 GB floor) large-v3 decoded fine and OOMed with
``word_timestamps=True`` (2026-11-09). So the ASR stays as it is and the
diarization pass, which runs after Whisper has exited, aligns the known text to
the audio with a small (~1.2 GB) multilingual CTC model. It also works on every
asr.json written before this existed, without repeating Whisper.

Needs torch and torchaudio (the diarization venv). Every failure degrades to
"no words for this segment"; attribution then interpolates.
"""

from __future__ import annotations

import unicodedata
from typing import Callable, Optional, Sequence

from .speaker_attribution import interpolate_missing

SAMPLE_RATE = 16000
PAD_S = 0.5            # Whisper segment bounds are approximate; give the aligner room.
MAX_SEGMENT_S = 30.0
MODEL_FILE = "mms_fa_ctc_alignment_mling_uroman.pt"  # ~/.cache/torch/hub/checkpoints/   # wav2vec2 attention is quadratic; longer segments are mostly silence.


def normalize(word: str, alphabet: set[str]) -> str:
    """Romanize-lite: strip accents, lowercase, keep only characters the aligner knows."""
    plain = unicodedata.normalize("NFKD", word.lower())
    return "".join(ch for ch in plain if ch in alphabet)


class Aligner:
    def __init__(self, device: str = "cuda"):
        import torch
        import torchaudio

        self.torch = torch
        bundle = torchaudio.pipelines.MMS_FA
        self.device = torch.device(device)
        # torch.hub would cache this as a bare "model.pt"; name it so nothing else collides.
        self.model = bundle.get_model(with_star=False, dl_kwargs={"file_name": MODEL_FILE}).to(self.device).eval()
        self.labels = bundle.get_labels(star=None)
        self.alphabet = {ch for ch in self.labels if ch != "-"}
        self.index = {ch: i for i, ch in enumerate(self.labels)}
        self.forced_align = torchaudio.functional.forced_align
        self.merge_tokens = torchaudio.functional.merge_tokens

    def align_segment(self, signal, segment: dict) -> Optional[list[list]]:
        """[[start, end, word], ...] for one segment, or None when it cannot be aligned."""
        torch = self.torch
        start, end = float(segment["start"]), float(segment["end"])
        tokens = (segment.get("text") or "").split()
        if not tokens or end - start > MAX_SEGMENT_S:
            return None
        lo = max(0, int((start - PAD_S) * SAMPLE_RATE))
        hi = min(len(signal), int((end + PAD_S) * SAMPLE_RATE))
        if hi - lo < SAMPLE_RATE // 10:
            return None
        normalized = [normalize(t, self.alphabet) for t in tokens]
        targets = [self.index[ch] for word in normalized for ch in word]
        if not targets:
            return None
        with torch.inference_mode():
            wave = torch.as_tensor(signal[lo:hi], dtype=torch.float32, device=self.device).unsqueeze(0)
            emission, _ = self.model(wave)
            if emission.shape[1] < len(targets):
                return None
            log_probs = torch.log_softmax(emission, dim=-1)
            aligned, scores = self.forced_align(
                log_probs, torch.tensor([targets], dtype=torch.int32, device=self.device), blank=0)
        spans = self.merge_tokens(aligned[0].cpu(), scores[0].exp().cpu())
        frame_s = (hi - lo) / SAMPLE_RATE / emission.shape[1]
        offset = lo / SAMPLE_RATE
        words: list[dict] = []
        cursor = 0
        for token, norm in zip(tokens, normalized):
            if not norm:
                words.append({"start": None, "end": None, "text": token})
                continue
            group = spans[cursor:cursor + len(norm)]
            cursor += len(norm)
            words.append({"start": offset + group[0].start * frame_s,
                          "end": offset + group[-1].end * frame_s, "text": token})
        interpolate_missing(words, start, end)
        return [[round(w["start"], 2), round(w["end"], 2), w["text"]] for w in words]


def align_segments(signal, segments: Sequence[dict], device: str = "cuda",
                   log: Callable[[str], None] = lambda _m: None) -> list[dict]:
    """Copies of ``segments`` with ``words`` filled in where alignment succeeded.

    Segments that already carry words are left alone. Never raises: a missing
    model, an OOM or a bad segment leaves that segment (or all of them) without
    words, and attribution interpolates.
    """
    out = [dict(seg) for seg in segments]
    todo = [seg for seg in out if not seg.get("words")]
    if not todo:
        return out
    try:
        aligner = Aligner(device)
    except Exception as exc:  # torchaudio missing, download failed, no VRAM
        log(f"Warning: word alignment unavailable ({exc}); speaker splits will be interpolated")
        return out
    aligned = failed = 0
    for seg in todo:
        try:
            words = aligner.align_segment(signal, seg)
        except Exception as exc:
            words = None
            if failed == 0:
                log(f"Warning: word alignment failed on a segment ({exc}); interpolating it")
        if words:
            seg["words"] = words
            aligned += 1
        else:
            failed += 1
    log(f"  Word alignment: {aligned} of {len(todo)} segments aligned")
    return out
