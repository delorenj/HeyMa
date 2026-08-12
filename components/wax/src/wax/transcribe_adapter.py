"""Run HeyMa's transcribe CLI under Wax's rules.

Three things this adds over calling `transcribe` directly:

1. A retained, per-item log (TRANSCRIBE_LOG_FILE), so a failure is diagnosable
   instead of vanishing with the old `trap 'rm -f' EXIT`.
2. A dated output name — YYYYMMDD-HHMMSS-<slug>.md — because the new pipeline
   had started emitting clip_NNNN.md with no date anywhere, which made
   transcripts unfindable next to years of date-named ones.
3. The duration gate. Nothing reaches the vault unless the transcript demonstrably
   covers the audio.

Wax does NOT take ~/.cache/heyma-transcribe.lock. bin/transcribe grabs that
itself with a blocking, un-timeouted flock, so a parent holding it would
deadlock its own child on every job. Wax serialises with its own semaphore and
lets the script own its file lock.
"""

import os
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import archive, config, desktop, frontmatter, ledger, paths, sanity, sentinel

PROGRESS_TIMEOUT_S = 900
PROGRESS_PREFIX = "Transcription-Progress: "


class TranscribeError(RuntimeError):
    pass


class TranscriptionSizeError(TranscribeError):
    pass


class TranscriptionDurationError(TranscribeError):
    pass


def transcribe_env(logfile: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["TRANSCRIBE_LOG_FILE"] = str(logfile)
    # Diarization is the default. An explicit false value is the only opt-out.
    if env.get("WAX_DIARIZATION", "").lower() in {"0", "false", "no", "off"}:
        env["DIARIZATION_VENV"] = str(paths.VAR / ".diarization-disabled")
    return env


def parse_metadata(stderr: str) -> dict[str, Any]:
    """Read the in-band metadata record emitted by transcribe.py."""
    prefix = "Transcription-Metadata: "
    for line in reversed((stderr or "").splitlines()):
        if not line.startswith(prefix):
            continue
        try:
            value = json.loads(line[len(prefix):])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def parse_progress(text: str) -> dict[str, Any]:
    """Return the latest structured heartbeat, with legacy-log fallbacks."""
    for line in reversed((text or "").splitlines()):
        if line.startswith(PROGRESS_PREFIX):
            try:
                value = json.loads(line[len(PROGRESS_PREFIX):])
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict) or not value.get("stage"):
                continue
            out = {"stage": str(value["stage"])}
            if value.get("percent") is not None:
                try:
                    out["progress_pct"] = max(0, min(100, int(value["percent"])))
                except (TypeError, ValueError):
                    pass
            detail = value.get("detail") or value.get("position")
            if detail:
                out["progress_detail"] = str(detail)
            return out
        # Existing jobs predate structured heartbeats. Their log still tells
        # us when ASR's 99% was followed by the separate diarization phase.
        if "Running diarization" in line:
            return {"stage": "diarize"}
        if "Loading diarization model" in line:
            return {"stage": "diarize", "progress_pct": 0}
        if line.startswith("Done. Duration:"):
            return {"stage": "finalize"}
        match = re.search(r"\[\s*(\d{1,3})%\]", line)
        if match:
            return {"stage": "transcribe", "progress_pct": min(100, int(match.group(1)))}
    return {}


def progress_for(item_id: str) -> dict[str, Any]:
    """Read progress for the active attempt without touching the ledger."""
    logdir = paths.LOGS / item_id
    try:
        logs = sorted(logdir.glob("transcription.*.log"), key=lambda path: path.stat().st_mtime_ns)
        if not logs:
            return {}
        # Progress records are short; cap the read so a noisy dependency can
        # never make the once-per-second tray poll expensive.
        with logs[-1].open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 256 * 1024))
            text = handle.read().decode("utf-8", errors="replace")
        return parse_progress(text)
    except OSError:
        return {}


def transcribe_command() -> Path:
    """Resolve the configured transcriber and reject missing/non-executable paths."""
    configured = os.environ.get("WAX_TRANSCRIBE", "").strip()
    candidate = Path(configured).expanduser() if configured else None
    if candidate is None:
        discovered = shutil.which("transcribe")
        candidate = Path(discovered) if discovered else None
    if candidate is None:
        raise TranscribeError(
            "transcribe command not found; set WAX_TRANSCRIBE to an executable "
            "or install `transcribe` on PATH"
        )
    candidate = candidate.resolve()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        source = "WAX_TRANSCRIBE" if configured else "PATH"
        raise TranscribeError(f"{source} transcribe command is not executable: {candidate}")
    return candidate


def vault_name(audio: Path, when: Optional[float] = None) -> str:
    """YYYYMMDD-HHMMSS-<slug>.md, derived from the recording's mtime."""
    import time
    t = time.localtime(when if when is not None else audio.stat().st_mtime)
    slug = audio.stem
    # Strip a leading date-time that our own captures already carry, so we do
    # not end up with 20260725-032204-20260725-032204-rec.
    import re
    slug = re.sub(r"^\d{8}-\d{6}-", "", slug)
    return f"{time.strftime('%Y%m%d-%H%M%S', t)}-{slug}.md"


def transcribe(audio: Path, *, item_id: Optional[str] = None,
               attempt: int = 1, extra: Optional[list[str]] = None) -> dict[str, Any]:
    """Transcribe one item. Publishes only if the sanity gate passes."""
    if not audio.is_file():
        raise TranscribeError(f"not a file: {audio}")
    size = audio.stat().st_size
    limit = config.max_audio_file_size_for_transcription()
    if not config.transcription_size_allowed(size):
        raise TranscriptionSizeError(
            f"audio is {size} bytes; MAX_AUDIO_FILE_SIZE_FOR_TRANSCRIPTION={limit} bytes"
        )
    duration_s = sanity.probe_duration(audio)
    if duration_s is not None and not config.transcription_duration_allowed(duration_s):
        limit_s = config.max_audio_duration_for_transcription()
        raise TranscriptionDurationError(
            f"audio is {duration_s:.3f}s; MAX_AUDIO_DURATION_FOR_TRANSCRIPTION={limit_s:.3f}s"
        )
    item_id = item_id or ledger.identify(audio)

    logdir = paths.LOGS / (item_id or audio.stem)
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / f"transcription.{attempt}.log"

    st = audio.stat()
    before = (st.st_size, st.st_mtime_ns)

    env = transcribe_env(logfile)

    requested = list(extra or [])
    if env.get("WAX_DIARIZATION", "").lower() in {"0", "false", "no", "off"}:
        if "--diarization" not in requested and "--no-diarization" not in requested:
            requested.append("--no-diarization")
    cmd = ["nice", "-n", "15", str(transcribe_command()), str(audio)] + requested
    desktop.ding("start")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=86400)
    except subprocess.TimeoutExpired as e:
        raise TranscribeError(f"timeout after 24h; log: {logfile}") from e

    if r.returncode != 0:
        raise TranscribeError(
            f"transcribe exited {r.returncode}; log retained: {logfile}\n"
            f"{(r.stderr or '')[-800:]}"
        )

    # stdout contract: exactly one line, the markdown path.
    md = Path((r.stdout or "").strip().splitlines()[-1]) if r.stdout.strip() else None
    if md is None or not md.is_file():
        raise TranscribeError(f"no 'Written:' path produced; log retained: {logfile}")

    meta = parse_metadata(r.stderr)

    verdict = sanity.check(audio, meta)

    # If the source changed under us the transcript describes a prefix of a file
    # that no longer exists — precisely the record_0016 failure.
    if not sanity.source_unchanged(audio, before):
        verdict = {**verdict, "ok": False, "reason_code": "source_changed"}

    if not verdict["ok"]:
        suspect = md.with_name(md.stem + ".suspect.md")
        shutil.move(str(md), str(suspect))
        if item_id:
            ledger.set_item_state(item_id, "suspect", cause=verdict["reason_code"] or "gate_failed",
                                  evidence=str(verdict))
        raise TranscribeError(
            f"SANITY GATE FAILED ({verdict['reason_code']}): "
            f"audio={verdict['audio_duration_s']}s asr={verdict['asr_duration_s']}s "
            f"ratio={verdict['duration_ratio']} -> quarantined as {suspect.name}; log: {logfile}"
        )

    # Passed. Give it a dated, findable name.
    desired = paths.VAULT / vault_name(audio)
    final = md
    if md.name != desired.name:
        from . import rename as rn
        try:
            final = rn.move_noclobber(md, desired)
        except OSError:
            final = md

    # Stamp identity onto the note itself, at publish time — NOT only when an
    # enrichment pass happens to run. This is what makes "a .md with no
    # wax-item-id has not been processed" a true statement, and what lets the
    # ledger be rebuilt from the vault alone.
    if item_id:
        try:
            try:
                refs = archive.references(item_id)
            except Exception:  # noqa: BLE001 - provenance backfill must not discard a transcript
                refs = []
            provenance: dict[str, Any] = {
                "schema-version": 1,
                "asset-kind": "transcript",
                "specialist": "transcripts",
                "source": "audio-recording",
                "captured": datetime.fromtimestamp(audio.stat().st_mtime).astimezone().isoformat(
                    timespec="seconds"
                ),
                frontmatter.ITEM_KEY: item_id,
                frontmatter.WAX_KEY: {
                    "transcribed_at": sentinel.utcnow(),
                    "audio_duration_s": verdict["audio_duration_s"],
                    "duration_ratio": verdict["duration_ratio"],
                    "source_audio": audio.name,
                },
            }
            if refs:
                provenance.update({
                    "source-sha256": refs[0]["sha256"],
                    "source-s3-key": refs[0]["s3_key"],
                    "source-s3-uri": f"s3://{refs[0]['bucket']}/{refs[0]['s3_key']}",
                })
            frontmatter.merge(final, provenance)
        except OSError:
            pass

    if item_id:
        ledger.connect().execute(
            "INSERT INTO transcripts(item_id,md_path,audio_duration,asr_duration,duration_ratio,"
            "word_count,diarized,engine_model,created_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET md_path=excluded.md_path, "
            "audio_duration=excluded.audio_duration, asr_duration=excluded.asr_duration, "
            "duration_ratio=excluded.duration_ratio, word_count=excluded.word_count",
            (item_id, str(final), verdict["audio_duration_s"], verdict["asr_duration_s"],
             verdict["duration_ratio"], meta.get("word_count"), int(bool(meta.get("diarized"))),
             meta.get("model"), sentinel.utcnow()),
        )
        ledger.set_item_state(item_id, "transcribed", cause="gate_passed",
                              evidence=f"ratio={verdict['duration_ratio']} -> {final.name}")

    desktop.ding("complete")
    return {"item_id": item_id, "md_path": str(final), "log": str(logfile), **verdict}
