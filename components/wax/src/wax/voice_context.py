"""Who a speaker is when the voice alone cannot say: the transcript's context, weighed by Jev.

The voice match (`voiceprint`) leaves a speaker `uncertain` when the best voice
is between the bars or two voices are too close, and registers a stranger when
nothing reaches the `new` bar. Often the recording itself says who it is: the
others call her by name, the meeting is about a project only one known person
works on, the summary says "Gershon asked". This module asks Jev that question
and combines its answer with the voice evidence:

    posterior(person) ∝ P_context(person) × LR_voice(person)
    posterior(nobody listed) ∝ P_context(someone_else)

`P_context` is Jev's `choice` over the named voices that the voice does not rule
out, plus `someone_else`; Jev never hears audio and is never shown a voice score,
so the two pieces of evidence are independent. `LR_voice` is how much more
likely this voice score is for the same person than for someone else, fitted on
the vault's own known-identity sightings (2026-11-11: owner channels, caller IDs
and hand-typed turn names, 185 probes against 7 named voices, each scored
without the probe's own recording).

A speaker is named from context only when the posterior clears `min_posterior`
(0.85) AND the context alone gives that person at least `min_context` (0.30):
the context must point at someone, the voice only has to not argue. Context
never registers, never teaches the registry an exemplar, and only names voices
that have a name.

Measured 2026-11-11 (label masked, voice scored without the probe's own
recording): Jev's context gives the true person a median 0.29 against 0.04 for
a wrong or unregistered one (AUC 0.86), so it is cautious but discriminating.
At these bars, 34 speakers who are nobody registered got no name, and of 70
known speakers given an uncertain voice (likelihood ratio 7) 19 were named,
with one disagreement (a turn whose hand label the voice also disputed).
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from . import frontmatter, jev, provider, voiceprint

SOMEONE_ELSE = "someone_else"
DEFAULT_KEY_OP = "op://DeLoSecrets/u4dk64drlzi3fd2q3qzpw3ox24/credential"  # alias heyma-wax-voice-id
KEY_ENV = "WAX_VOICE_JEV_API_KEY"

# log LR(score) = slope × (score - even): logistic fits on the 2026-11-11 known-identity probes
# (same channel n=870 pairs, other channel n=795). Capped both ways: the fit is thin at the
# ends, and context must never be outvoted by a voice score's tail alone.
LR_FIT = {False: (11.94, 0.337), True: (18.92, 0.188)}
LR_MIN, LR_MAX = 0.02, 20.0
CANDIDATE_MIN_LR = 0.25   # a named voice the voice clearly rules out is not offered to Jev
MAX_CANDIDATES = 5
TURN_RE = re.compile(r"^\*\*(.+?) \(([0-9:]+)\):\*\* ?(.*)$", re.M)
CONFIDENT = ("matched", "owner", "owner_new", "caller_id", "caller_id_new", "context")


def likelihood_ratio(score: float, cross: bool) -> float:
    slope, even = LR_FIT[bool(cross)]
    return float(min(LR_MAX, max(LR_MIN, math.exp(slope * (score - even)))))


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    min_posterior: float = 0.85
    min_context: float = 0.30

    @classmethod
    def from_env(cls) -> "Settings":
        def num(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except ValueError:
                return default
        return cls(enabled=os.environ.get("WAX_VOICE_CONTEXT", "1").strip().lower() not in ("0", "false", "no", "off"),
                   min_posterior=num("WAX_VOICE_CONTEXT_MIN_P", cls.min_posterior),
                   min_context=num("WAX_VOICE_CONTEXT_MIN_CONTEXT", cls.min_context))


@dataclass
class Candidate:
    voice_id: str
    name: str
    score: float
    cross: bool
    lr: float


def candidates(vec: np.ndarray, domain: str, voices: dict[str, list[tuple[str, np.ndarray]]],
               names: dict[str, str | None], t: voiceprint.Thresholds) -> list[Candidate]:
    """Named voices the voice does not rule out, most likely first."""
    out = []
    for _, score, cross, voice_id, _ in voiceprint.voice_scores(vec, domain, voices, t):
        name = names.get(voice_id)
        if not name:
            continue
        lr = likelihood_ratio(score, cross)
        if lr >= CANDIDATE_MIN_LR:
            out.append(Candidate(voice_id, name, float(score), cross, lr))
    out.sort(key=lambda c: -c.lr)
    return out[:MAX_CANDIDATES]


@dataclass(frozen=True)
class Verdict:
    voice_id: str | None   # the person context names, or None
    posterior: float       # of that person (or of the best candidate when None)
    context: float         # P_context of that person
    probabilities: dict[str, float] = field(default_factory=dict)  # posterior per option


def combine(context: dict[str, float], cands: list[Candidate], s: Settings) -> Verdict:
    """Weigh Jev's context probabilities by each candidate's voice likelihood ratio."""
    weights = {c.voice_id: max(0.0, float(context.get(c.voice_id, 0.0))) * c.lr for c in cands}
    weights[SOMEONE_ELSE] = max(0.0, float(context.get(SOMEONE_ELSE, 0.0)))
    total = sum(weights.values())
    if total <= 0 or not cands:
        return Verdict(None, 0.0, 0.0, {})
    posterior = {k: w / total for k, w in weights.items()}
    best = max(cands, key=lambda c: posterior[c.voice_id])
    p, ctx = posterior[best.voice_id], float(context.get(best.voice_id, 0.0))
    ok = p >= s.min_posterior and ctx >= s.min_context
    return Verdict(best.voice_id if ok else None, round(p, 4), round(ctx, 4),
                   {k: round(v, 4) for k, v in posterior.items()})


# ---- what Jev reads -------------------------------------------------------------------

def turns(body: str) -> list[tuple[str, str, str]]:
    return [(m.group(1), m.group(2), m.group(3).strip()) for m in TURN_RE.finditer(body)]


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def excerpt(body: str, label: str, shown: dict[str, str], hints: list[str], *, limit: int = 2600) -> list[str]:
    """The speaker's turns with the turn before and after each, as `Who: text` lines.

    Turns that mention a candidate's name (or sit next to one) come first, then
    the speaker's first turns; others are shown under `shown` names when known.
    """
    rows = turns(body)
    mine = [i for i, (who, _, _) in enumerate(rows) if who == label]
    if not mine:  # a channel or whole-recording speaker: the body is all theirs
        text = TURN_RE.sub(lambda m: m.group(3), body)
        return [f"{label}: {_clip(text, limit)}"] if text.strip() else []
    lowered = [h.casefold() for h in hints if h]

    def mentions(i: int) -> bool:
        window = " ".join(rows[j][2] for j in range(max(0, i - 1), min(len(rows), i + 2))).casefold()
        return any(re.search(rf"\b{re.escape(h)}\b", window) for h in lowered)

    order = [i for i in mine if mentions(i)] + [i for i in mine if not mentions(i)]
    lines, used, seen = [], 0, set()
    for i in order:
        for j in (i - 1, i, i + 1):
            if j < 0 or j >= len(rows) or j in seen:
                continue
            who, at, text = rows[j]
            name = "THIS SPEAKER" if who == label else shown.get(who, who)
            line = f"[{at}] {name}: {_clip(text, 320)}"
            if used + len(line) > limit:
                return lines
            seen.add(j)
            lines.append(line)
            used += len(line)
    return lines


def profile(conn, voice_id: str, name: str, note_path: Path | None, exclude_item: str,
            limit: int = 8) -> dict[str, Any]:
    """Who a named voice is, from its note and the recordings it was confidently heard in."""
    relationship = None
    if note_path and Path(note_path).exists():
        fm, _ = frontmatter.read(Path(note_path))
        relationship = fm.get("relationship") or None
    rows = conn.execute(
        "SELECT DISTINCT s.item_id, t.md_path FROM voice_sightings s LEFT JOIN transcripts t USING(item_id) "
        f"WHERE s.voice_id=? AND s.item_id != ? AND s.verdict IN ({','.join('?' * len(CONFIDENT))}) "
        "ORDER BY t.md_path DESC", (voice_id, exclude_item, *CONFIDENT)).fetchall()
    titles, projects, kinds = [], {}, {}
    for row in rows:
        md = row["md_path"] if hasattr(row, "keys") else row[1]
        if not md or not Path(md).exists():
            continue
        fm, _ = frontmatter.read(Path(md))
        for p in fm.get("projects") or []:
            projects[str(p)] = projects.get(str(p), 0) + 1
        kind = str(fm.get("classification") or "")
        if kind:
            kinds[kind] = kinds.get(kind, 0) + 1
        if len(titles) < limit and fm.get("title"):
            titles.append(str(fm["title"]))
    out: dict[str, Any] = {"name": name, "heard_in_recordings": len(rows)}
    if relationship:
        out["relationship"] = str(relationship)
    if titles:
        out["recent_recordings"] = titles
    if projects:
        out["projects"] = [p for p, _ in sorted(projects.items(), key=lambda kv: -kv[1])[:6]]
    if kinds:
        out["kinds_of_recording"] = dict(sorted(kinds.items(), key=lambda kv: -kv[1]))
    return out


def state(fm: dict[str, Any], body: str, pending: dict[str, list[Candidate]], shown: dict[str, str],
          people: dict[str, dict[str, Any]], owner: str | None) -> dict[str, Any]:
    recording = {k: fm.get(k) for k in ("title", "summary", "classification", "projects", "captured")
                 if fm.get(k) not in (None, "", [])}
    for k in ("summary", "captured"):
        if k in recording:
            recording[k] = _clip(str(recording[k]), 900)
    hints = sorted({n for cands in pending.values() for c in cands for n in (c.name, c.name.split()[0])})
    out: dict[str, Any] = {
        "recording": recording,
        "already_identified": {label: name for label, name in shown.items() if label not in pending},
        "people": {vid: people[vid] for vid in sorted(people)},
        "unidentified_speakers": {label: excerpt(body, label, shown, hints) for label in pending},
    }
    if owner:
        out["recorded_by"] = owner
    return out


def question(label: str, cands: list[Candidate], people: dict[str, dict[str, Any]]) -> dict[str, Any]:
    options = {}
    for c in cands:
        rel = people.get(c.voice_id, {}).get("relationship")
        options[c.voice_id] = f"{label} is {c.name}" + (f" ({rel})" if rel else "")
    options[SOMEONE_ELSE] = (f"{label} is someone not listed here, or the transcript gives no real sign "
                             "of which listed person it is")
    return jev.choice(
        f"Who is {label} in this recording? Their lines are under unidentified_speakers[{label!r}] "
        "(marked THIS SPEAKER). Judge only from what is said and the context: who the others address "
        "by name, what this speaker says about themselves, their role, and which listed person takes "
        "part in recordings like this one (people[*].recent_recordings and projects). Being plausible "
        "is not enough; pick a person only when the transcript points to them.", options)


def ask(st: dict[str, Any], pending: dict[str, list[Candidate]], people: dict[str, dict[str, Any]], *,
        post: Optional[Callable[..., dict[str, Any]]] = None) -> tuple[dict[str, dict[str, float]], jev.Decision]:
    """One Jev request for every pending speaker; {label: {option: probability}}."""
    qs = {f"speaker_{i}": question(label, cands, people) for i, (label, cands) in enumerate(pending.items())}
    key = provider.resolve_key(env_var=KEY_ENV,
                               op_ref=os.environ.get("WAX_VOICE_JEV_API_KEY_OP", DEFAULT_KEY_OP).strip())
    decision = jev.decide(st, qs, key=key,
                          model=os.environ.get("WAX_JEV_MODEL", "").strip() or jev.DEFAULT_MODEL,
                          url=os.environ.get("WAX_JEV_URL", "").strip() or jev.DEFAULT_URL, post=post)
    out = {}
    for i, label in enumerate(pending):
        probs = decision.answers[f"speaker_{i}"].get("probabilities") or {}
        out[label] = {k: float(v) for k, v in probs.items()}
    return out, decision
