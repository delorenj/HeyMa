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

import logging
import os
import json
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import archive, config, desktop, fence, frontmatter, ledger, paths, sanity, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

PROGRESS_TIMEOUT_S = 900
PROGRESS_PREFIX = "Transcription-Progress: "
DEGRADED_MARKER = "DIARIZATION-DEGRADED"

# WAX_DIARIZATION is THREE-state, not two. The unit ships WAX_DIARIZATION=1 and
# the old code branched only on falsy values, so the knob that reads as
# "diarization is on" was inert — it granted confidence it could not back, for
# the whole week diarization silently returned []. BMAD story 1-1 keeps the env
# knob until Canonical Config lands, so it has to be honest instead of gone:
#
#   unset/empty -> "default"  : bin/transcribe auto-enables when the venv exists
#   truthy      -> "required" : ask for it explicitly; a degraded run is an ERROR
#   falsy       -> "disabled" : pass --no-diarization and hide the venv
DIARIZATION_TRUTHY = {"1", "true", "yes", "on"}
DIARIZATION_FALSY = {"0", "false", "no", "off"}
DIARIZATION_DEVICES = {"cuda", "cpu", "auto"}


class TranscribeError(RuntimeError):
    pass


class TranscriptionSizeError(TranscribeError):
    pass


class TranscriptionDurationError(TranscribeError):
    pass


class EmptyOutput(TranscribeError):
    """The transcriber returned no words for audio that is NOT silent: a visible failure.

    Silent audio never lands here (it is parked as ``junk_silent``); audible-but-empty is
    an ASR problem, and ``wax retry`` is a sensible next step. ``cause_code`` is what the
    worker records as the item's failure cause.
    """

    cause_code = "asr_empty_output"


class TranscriptRejected(TranscribeError):
    """The junk gate (wax/junk.py) refused a fresh transcript: nothing was published.

    ``rejection`` is the junk.Rejection; ``kept`` is where the ASR text was set
    aside (out of the vault). The caller owns the item state and the audio.
    """

    def __init__(self, message: str, rejection: Any, kept: Optional[Path],
                 levels: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.rejection = rejection
        self.kept = kept
        self.levels = levels      # sanity.audio_levels, when the rejection was decided by level


def _record_levels(item_id: str, levels: Optional[dict[str, Any]]) -> None:
    try:
        ledger.record_audio_levels(item_id, levels)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not change the verdict
        log.debug("could not record audio levels for %s: %s", item_id, exc)


def _has_output_arg(args: list[str]) -> bool:
    return any(a in ("-o", "--output") or a.startswith("--output=") or (a.startswith("-o") and len(a) > 2)
               for a in args)


def _move_out(src: Path, dst: Path) -> Path:
    """move_noclobber, falling back to a copy-move when renameat2 cannot cross filesystems.

    Never overwrites: on the fallback the first free ``dst``/``dst-N`` name is used.
    """
    from . import rename as rn
    try:
        return rn.move_noclobber(src, dst)
    except rn.RenameCollision:
        raise
    except OSError:
        dst.parent.mkdir(parents=True, exist_ok=True)
        cand, n = dst, 0
        while os.path.lexists(cand):
            n += 1
            cand = dst.with_name(f"{dst.stem}-{n}{dst.suffix}")
        return Path(shutil.move(str(src), str(cand)))


def _move_suspect(md: Path) -> Path:
    """Quarantine a sanity-failed note in the vault as ``<stem>.suspect.md``.

    A repeat failure of the same stem becomes ``<stem>-N.suspect.md``: the
    suffix must stay ``.suspect.md`` because prune and the store reconcile skip
    exactly that, and a generic ``<stem>.suspect-1.md`` would read as a real note.
    """
    dst = paths.VAULT / (md.stem + ".suspect.md")
    n = 0
    while os.path.lexists(dst):
        n += 1
        dst = paths.VAULT / f"{md.stem}-{n}.suspect.md"
    return _move_out(md, dst)


def _in_vault(p: Path) -> bool:
    try:
        p.resolve().relative_to(paths.VAULT.resolve())
        return True
    except (OSError, ValueError):
        return False


def diarization_mode(env: Optional[dict[str, str]] = None) -> str:
    """Resolve WAX_DIARIZATION to "default" | "required" | "disabled".

    An unrecognised value is treated as "default": a typo must not silently
    disable diarization, and it must not silently claim to require it either.
    """
    raw = (env if env is not None else os.environ).get("WAX_DIARIZATION", "").strip().lower()
    if raw in DIARIZATION_FALSY:
        return "disabled"
    if raw in DIARIZATION_TRUTHY:
        return "required"
    return "default"


def diarization_device(env: Optional[dict[str, str]] = None) -> str:
    """Resolve the independent Sortformer device; CUDA is the strict default."""
    raw = (env if env is not None else os.environ).get(
        "WAX_DIARIZATION_DEVICE", "cuda"
    ).strip().lower()
    value = raw or "cuda"
    if value not in DIARIZATION_DEVICES:
        raise TranscribeError(
            f"invalid WAX_DIARIZATION_DEVICE={value!r}; expected cuda, cpu, or auto"
        )
    return value


def transcribe_env(logfile: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["TRANSCRIBE_LOG_FILE"] = str(logfile)
    env["WAX_DIARIZATION_DEVICE"] = diarization_device(env)
    # Diarization is the default. An explicit false value is the only opt-out;
    # pointing DIARIZATION_VENV at a path that cannot exist is how bin/transcribe
    # is told to take its no-diarization branch.
    if diarization_mode(env) == "disabled":
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


@contextmanager
def _staged_output(item_id: Optional[str], logdir: Path, env: dict[str, str]):
    """Own the child's output BEFORE spawning it, independently of stdout/exit.

    Never inspect or sweep the vault. Even a partial note written before the
    child is killed belongs to this unique private directory and is preserved.
    """
    stage = Path(tempfile.mkdtemp(prefix="output-", dir=logdir))
    env.update(TRANSCRIPTS=str(stage), WAX_VAULT=str(stage))
    try:
        yield stage / "transcript.md"
    finally:
        if ledger.fence_of(item_id) is not None:
            from . import rename as rn
            dest = paths.RECOVERED / "split-late"
            dest.mkdir(parents=True, exist_ok=True)
            moved = rn.move_noclobber(stage, dest / stage.name)
            log.warning("item %s was split; private transcription output parked at %s", item_id, moved)


@contextmanager
def _publication(item_id: Optional[str]):
    """Serialize the final fence check + publication against raise_fence's INSERT."""
    conn = ledger.connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        ledger.guard_fence(item_id)
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


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
    # `wax split` fenced this item: do not even start (docs/WAX-SPLIT.md).
    ledger.guard_fence(item_id)

    logdir = paths.LOGS / (item_id or audio.stem)
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / f"transcription.{attempt}.log"

    st = audio.stat()
    before = (st.st_size, st.st_mtime_ns)

    env = transcribe_env(logfile)

    with _staged_output(item_id, logdir, env) as output:
        mode = diarization_mode(env)
        asr_path = logdir / "asr.json"
        requested = list(extra or [])
        if _has_output_arg(requested):
            raise TranscribeError("caller-supplied output is not allowed; transcriber output must stay private")
        requested = [arg for arg in requested if arg not in ("--diarization", "--no-diarization")]
        requested.extend(["--no-diarization", "--asr-output", str(asr_path), "--output", str(output)])
        started = __import__("time").monotonic()
        cmd = ["nice", "-n", "15", str(transcribe_command()), str(audio)] + requested
        desktop.ding("start")
        try:
            # Tracked so `wax split` can stop exactly this item's transcriber.
            r = fence.run_tracked(item_id, cmd, env=env, timeout=86400)
        except subprocess.TimeoutExpired as e:
            raise TranscribeError(f"timeout after 24h; log: {logfile}") from e

        # The late-result fence. If `wax split` halted this item while the child ran,
        # whatever it produced is stale: it must not be published, gated, renamed or
        # recorded. A transcript the child managed to finish is parked, never deleted.
        if ledger.fence_of(item_id) is not None:
            ledger.guard_fence(item_id)

        if r.returncode != 0:
            raise TranscribeError(
                f"transcribe exited {r.returncode}; log retained: {logfile}\n"
                f"{(r.stderr or '')[-800:]}"
            )

        # stdout contract: exactly one line, the markdown path.
        md = Path((r.stdout or "").strip().splitlines()[-1]) if r.stdout.strip() else None
        if md is None or not md.is_file() or not md.resolve().is_relative_to(output.parent.resolve()):
            raise TranscribeError(f"no private 'Written:' path produced; log retained: {logfile}")

        meta = parse_metadata(r.stderr)

        # Diarization degrades by returning nothing instead of failing, so the item
        # state stays "transcribed" while the speaker track has in fact been empty
        # since 2026-08-12. Name the condition once, here, and carry it to both the
        # journal and the note so a human surface can finally represent it.
        diar_requested = bool(meta.get("diarization_requested"))
        if "diarization_degraded" in meta:
            diar_degraded = bool(meta["diarization_degraded"])
        else:
            # Older transcribe.py builds emit neither flag; the stderr marker (and,
            # failing that, requested-but-not-diarized) is the fallback signal.
            diar_degraded = DEGRADED_MARKER in (r.stderr or "") or (
                diar_requested and not meta.get("diarized")
            )
        if diar_degraded:
            detail = "diarization requested but produced no speaker turns: %s (mode=%s, log=%s)"
            if mode == "required":
                # WAX_DIARIZATION=1 means require it. The transcript is still
                # published — hours of ASR are not worth discarding over a missing
                # speaker track — but this must never read as a clean run.
                log.error(detail, audio.name, mode, logfile)
            else:
                log.warning(detail, audio.name, mode, logfile)

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

        # Inspect junk before opening the publication transaction: classification
        # may initialize the contacts schema (executescript commits SQLite's
        # active transaction). Output is still private until the final fence gate.
        from . import junk
        rejection = junk.after_asr(md, audio, item_id)
        # An EMPTY transcript is decided by MEASURED level, not by length (junk.empty_transcript_verdict).
        # Measured here, outside the publication transaction: ffmpeg takes seconds and the call lookup
        # touches the ledger.
        empty_levels, empty_what, dur = None, None, verdict.get("audio_duration_s")
        if rejection is not None and rejection.reason == "empty":
            empty_levels = sanity.audio_levels(audio)
            empty_what = junk.empty_transcript_verdict(
                empty_levels, dur, call=bool(junk._call_facts(audio.name)))
            if item_id:
                _record_levels(item_id, empty_levels)
        with _publication(item_id):
            if rejection is not None:
                kept: Optional[Path] = md
                try:
                    kept = _move_out(md, logdir / "rejected.md")
                except OSError as exc:
                    log.warning("junk gate could not set %s aside: %s", md, exc)
                if empty_what == "silent":
                    silent = junk.silent_rejection(empty_levels, dur)
                    if silent is not None:
                        raise TranscriptRejected(
                            f"JUNK GATE REJECTED (silent): {silent.detail}; "
                            f"not published, text kept at {kept}", silent, kept, levels=empty_levels)
                elif empty_what == "audible":
                    heard = (f"mean {empty_levels['mean_db']:.1f} dB, max {empty_levels['max_db']:.1f} dB"
                             if empty_levels else "level could not be measured")
                    raise EmptyOutput(
                        f"EMPTY TRANSCRIPT from {sanity.format_duration(dur)} of audible audio "
                        f"({heard}): the transcriber returned no words, so suspect ASR, "
                        f"not the microphone; not published, empty note kept at {kept}; log: {logfile}")
                raise TranscriptRejected(
                    f"JUNK GATE REJECTED ({rejection.reason}): {rejection.detail}; "
                    f"not published, text kept at {kept}", rejection, kept)

            # Passed. Give it a dated, findable name.
            paths.VAULT.mkdir(parents=True, exist_ok=True)
            desired = paths.VAULT / vault_name(audio)
            final = md
            try:
                final = _move_out(md, desired)
            except OSError as exc:
                raise TranscribeError(f"could not publish private transcript: {exc}") from exc

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
                            # The ledger's transcripts table can only say diarized 0/1.
                            # Until it grows a column, the note is the only durable
                            # record of the difference between "one speaker" and
                            # "the diarizer gave us nothing".
                            "diarization_requested": diar_requested,
                            "diarization_degraded": diar_degraded,
                            "diarization_device_requested": meta.get(
                                "diarization_device_requested"
                            ),
                            "diarization_device": meta.get("diarization_device"),
                            # Dual-channel call: speakers come from the carrier's channel
                            # split (scripts/transcribe.py), and the diarization pass skips.
                            "channel_attributed": bool(meta.get("channel_separated")),
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
                import hashlib
                _, body = frontmatter.read(final)
                processing_s = __import__("time").monotonic() - started
                ledger.connect().execute(
                    "UPDATE transcripts SET asr_path=?,body_sha256=?,processing_seconds=?,diarized=?,engine_model=? WHERE item_id=?",
                    (str(asr_path), hashlib.sha256(body.encode()).hexdigest(),
                     processing_s, int(bool(meta.get("diarized"))),
                     meta.get("model"), item_id),
                )
                ledger.set_item_state(item_id, "transcribed", cause="gate_passed",
                                      evidence=f"ratio={verdict['duration_ratio']} -> {final.name}")
        if item_id:
            try:
                from . import transcriptions
                transcriptions.record_transcription(item_id=item_id, md_path=final, meta=meta, verdict=verdict,
                                                    asr_path=asr_path, attempt=attempt,
                                                    processing_seconds=processing_s)
            except Exception as exc:  # noqa: BLE001 - the store must never cost a transcript
                log.warning("transcriptions capture failed for %s: %s", item_id, exc)

        return {
            "item_id": item_id,
            "md_path": str(final),
            "log": str(logfile),
            "diarization_requested": diar_requested,
            "diarization_degraded": diar_degraded,
            "diarization_device_requested": meta.get("diarization_device_requested"),
            "diarization_device": meta.get("diarization_device"),
            **verdict,
        }
