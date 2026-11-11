"""voice-id pass: fingerprint each speaker, match the voice registry, register strangers.

For every speaker in a transcript (the diarizer's `Speaker N`, a call's two
channels, or the whole recording when nobody was diarized) the pass:

1. fingerprints the speaker's speech with TitaNet in the diarization venv
   (`voiceprint.fingerprint_item`); a rerun reuses the fingerprints it stored;
2. matches the fingerprint against every registered voice (`voices`);
3. **matched** a known voice: records the sighting, keeps the recording as a new
   exemplar when the match is strong and adds something, and relabels a generic
   `**Speaker N (mm:ss):**` turn with the voice's name once the voice has one;
   **new** (nothing close, and enough clean speech): registers a voice, writes its
   note and clip in `~/d/Voices/`, and notifies you to name it;
   **uncertain** or **too short**: records the sighting and changes nothing.

What is known for certain comes before any matching (`voiceprint.decide`):
channel A of a call is our own leg in both directions (verified 2026-11-11),
so it is the owner (`WAX_VOICE_OWNER`); a call's far channel labelled with a
contact name is that contact (registered named, no notification); a recording
with a single voice (a voice memo) is the owner when it sounds enough like him.
That is also how the owner's desk-mic voice is learned from his phone voice.

Thresholds were measured on this vault's own recordings (2026-11-11); see
`skills/heyma/references/passes/voice-id.md`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from . import component, frontmatter, ledger, voiceprint, voices

WHOLE = "(whole recording)"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def thresholds() -> voiceprint.Thresholds:
    """The measured defaults, each overridable as WAX_VOICE_<FIELD> (e.g. WAX_VOICE_MATCH=0.65)."""
    base = voiceprint.Thresholds()
    return voiceprint.Thresholds(**{f: _env_float(f"WAX_VOICE_{f.upper()}", getattr(base, f))
                                    for f in base.__dataclass_fields__})


# ---- where each speaker talks ------------------------------------------------

def regions_for(body: str, asr: dict | None, duration: float) -> tuple[str, dict]:
    """(mode, {label: {"channel", "spans"}}) for the speakers in this note."""
    result = (asr or {}).get("result") or {}
    if result.get("channel_separated"):
        labels = list(result.get("channel_labels") or [])
        per_channel = voiceprint.segment_regions(result.get("segments") or [])
        regions = {}
        for channel, spans in sorted(per_channel.items()):
            label = labels[channel] if channel < len(labels) else f"Channel {channel}"
            regions[label] = {"channel": channel, "spans": spans}
        return "channels", regions
    turns = voiceprint.parse_turns(body)
    if turns:
        return "turns", {label: {"channel": None, "spans": spans}
                         for label, spans in voiceprint.turn_regions(turns, duration).items()}
    spans = [(s["start"], s["end"]) for s in result.get("segments") or []
             if s.get("start") is not None and s.get("end") is not None]
    return ("whole", {WHOLE: {"channel": None, "spans": spans}}) if spans else ("none", {})


def quotes_for(body: str, label: str, limit: int = 3) -> list[str]:
    """A few of the speaker's own lines, for the voice note."""
    out = []
    pattern = re.compile(rf"^\*\*{re.escape(label)} \([0-9:]+\):\*\* (.+)$", re.M)
    for m in pattern.finditer(body):
        text = m.group(1).strip()
        if len(text.split()) >= 6:
            out.append(text if len(text) <= 240 else text[:237] + "…")
        if len(out) >= limit:
            break
    return out


def first_at(body: str, label: str) -> str:
    m = re.search(rf"^\*\*{re.escape(label)} \(([0-9:]+)\):\*\*", body, re.M)
    return m.group(1) if m else "00:00"


def relabel(body: str, renames: dict[str, str]) -> str:
    """Rename speaker turn labels; only `**Label (ts):**` turn heads change."""
    for old, new in renames.items():
        if old != new:
            body = re.sub(rf"^\*\*{re.escape(old)} \((?=[0-9:]+\):\*\*)", f"**{new} (", body, flags=re.M)
    return body


# ---- the GPU half ----------------------------------------------------------------

def fingerprint(audio: str, regions: dict, clip_dir: str | None) -> dict:
    """Run voiceprint.fingerprint_item in the diarization venv."""
    python = Path(os.environ.get("DIARIZATION_PYTHON") or (component.ROOT.parents[1] / ".venv-diarization/bin/python"))
    spec = {"audio": audio, "regions": regions, "clip_dir": clip_dir}
    code = ("import sys,json; sys.path.insert(0,sys.argv[1]); from wax.voiceprint import fingerprint_item; "
            "print('VOICEPRINT ' + json.dumps(fingerprint_item(json.loads(sys.stdin.read()))))")
    result = subprocess.run([str(python), "-c", code, str(component.ROOT / "src")], input=json.dumps(spec),
                            text=True, capture_output=True, timeout=3600)
    if result.returncode:
        raise RuntimeError((result.stderr or "voiceprint child failed")[-800:])
    lines = [line for line in result.stdout.splitlines() if line.startswith("VOICEPRINT ")]
    if not lines:
        raise RuntimeError("voiceprint child printed no result")
    return json.loads(lines[-1][len("VOICEPRINT "):])


# ---- the pass ----------------------------------------------------------------------

class AudioMissing(RuntimeError):
    pass


def main(md: Path, item_id: str) -> dict:
    fm, body = frontmatter.read(md)
    digest = hashlib.sha256(body.encode()).hexdigest()
    row = ledger.connect().execute(
        "SELECT i.path, t.asr_path, t.audio_duration FROM items i LEFT JOIN transcripts t USING(item_id) "
        "WHERE item_id=?", (item_id,)).fetchone()
    if not row or not row["path"] or not Path(row["path"]).exists():
        raise AudioMissing(f"no local audio for {item_id}")
    asr = None
    if row["asr_path"] and Path(row["asr_path"]).exists():
        asr = json.loads(Path(row["asr_path"]).read_text())
    duration = float(row["audio_duration"] or fm.get("audio-duration-seconds") or 0) or 1e9
    mode, regions = regions_for(body, asr, duration)
    if not regions:
        return {"wax_ep_version": 1, "frontmatter": {}, "state": "skipped", "reason_code": "no_speech"}

    t = thresholds()
    domain = "call" if mode == "channels" else "room"
    owner = os.environ.get("WAX_VOICE_OWNER", "").strip() or None
    conn = voices.connect()
    cached = voices.sightings(conn, item_id)
    refresh = os.environ.get("WAX_VOICE_REFINGERPRINT", "0") in ("1", "true", "yes")
    prints: dict[str, dict] = {}
    original: dict[str, str] = {}
    for label in regions:
        hit = cached.get(label)
        if hit and hit["vec"] is not None and not refresh:
            prints[label] = {"vec": hit["vec"], "seconds": hit["seconds"], "consistency": None, "clip": None,
                             "cached": True}
            original[label] = hit["label"]
    missing = {label: regions[label] for label in regions if label not in prints}
    with tempfile.TemporaryDirectory(prefix="wax-voice-") as clip_dir:
        if missing:
            for label, fp in fingerprint(row["path"], missing, clip_dir).items():
                fp["vec"] = np.asarray(fp["vec"], dtype=np.float32) if fp.get("vec") is not None else None
                prints[label] = fp
        evidence, found, renames, heard = {}, [], {}, []
        title = fm.get("title") or md.stem
        voices.merge_namesakes(conn)  # a voice you named the same as another is that person
        with voices.locked():
            registry = voices.load(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for label in sorted(prints, key=lambda lb: -prints[lb]["seconds"]):
                    fp = prints[label]
                    vec, seconds = fp["vec"], float(fp["seconds"])
                    first = original.get(label, label)
                    d = voiceprint.decide(
                        label=first, mode=mode, channel=regions[label]["channel"], vec=vec, seconds=seconds,
                        consistency=fp.get("consistency"), domain=domain,
                        voices={vid: v.exemplars for vid, v in registry.items()},
                        names={vid: v.name for vid, v in registry.items()}, owner=owner, t=t,
                        cached=bool(fp.get("cached")))
                    m, verdict = d.match, d.verdict
                    voice = registry.get(d.voice_id) if d.voice_id else None
                    if d.register_as is not None:
                        voice_id = voices.new_voice_id(conn)
                        name = d.register_as or None
                        clip = voices.write_clip(Path(fp["clip"]), voice_id) if fp.get("clip") else None
                        voice = voices.register(conn, item_id=item_id, label=first, domain=domain, seconds=seconds,
                                                vec=vec, name=name, voice_id=voice_id,
                                                note=_note(voice_id, md, title, label, body, clip, name=name,
                                                           how=verdict))
                        registry[voice.voice_id] = voice
                        if not name:
                            found.append({"voice_id": voice.voice_id, "label": label, "title": title})
                    elif d.learn and voice is not None:
                        voices.add_exemplar(conn, voice, item_id, first, domain, seconds, vec)
                    rendered = label
                    if (voice is not None and voice.name and voice.status != voices.STATUS_IGNORE
                            and mode == "turns" and _ours(label, cached)):
                        rendered = voice.name
                        renames[label] = rendered
                    if voice is not None and voice.status != voices.STATUS_IGNORE:
                        heard.append(voice)
                    voices.record_sighting(conn, item_id=item_id, label=first, rendered=rendered,
                                           voice_id=voice.voice_id if voice else None, verdict=verdict,
                                           score=m.score, runner_up=m.runner_up, domain=domain, seconds=seconds,
                                           vec=vec)
                    evidence[label] = {"verdict": verdict, "voice": voice.voice_id if voice else None,
                                       "name": voice.name if voice else None,
                                       "score": round(m.score, 3) if m.score is not None else None,
                                       "runner_up": round(m.runner_up, 3) if m.runner_up is not None else None,
                                       "cross_channel": m.cross, "seconds": round(seconds, 1),
                                       "consistency": (round(fp["consistency"], 3)
                                                       if fp.get("consistency") is not None else None),
                                       "cached": bool(fp.get("cached"))}
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    voices.notify_new(found)
    seen, links = set(), []
    for voice in heard:
        if voice.voice_id not in seen:
            seen.add(voice.voice_id)
            links.append(voice.link)
    result = {"wax_ep_version": 1, "frontmatter": {"voices": links},
              "evidence": {"mode": mode, "speakers": evidence, "registered": [f["voice_id"] for f in found],
                           "domain": domain, "thresholds": dataclasses.asdict(t),
                           "model": voiceprint.MODEL_NAME}}
    new_body = relabel(body, renames)
    if new_body != body:
        result["body_replace"] = {"sha256": digest, "text": new_body}
    return result


def _ours(label: str, cached: dict) -> bool:
    """May the pass rename this label? Generic diarizer labels, and names it put there itself."""
    if voices.GENERIC_LABEL.match(label):
        return True
    hit = cached.get(label)
    return bool(hit and voices.GENERIC_LABEL.match(hit["label"]) and hit["rendered"] == label)


def _note(voice_id: str, md: Path, title: str, label: str, body: str, clip: str | None,
          name: str | None = None, how: str = "new") -> str:
    text = voices.note_text(voice_id, transcript=md, title=title, label=label, at=first_at(body, label),
                            quotes=quotes_for(body, label), clip=clip, registered=voices.now())
    if name:
        source = "the owner's side of a call" if how.startswith("owner") else "the caller ID of a call"
        text = text.replace("name: \n", f"name: {_yaml_name(name)}\n", 1).replace("status: unnamed", "status: named", 1)
        text = text.replace(f"# Unnamed voice `{voice_id}`", f"# {name} (`{voice_id}`)", 1)
        text = text.replace("Wax heard a voice it did not know. Fill in `name` above (and",
                            f"Named from {source}. Correct `name` above if it is wrong (and", 1)
    return text


def _yaml_name(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", name) else voices._yaml_str(name)
