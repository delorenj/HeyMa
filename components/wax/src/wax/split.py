"""`wax split`: recover a recording that was left running (HEYMA-18, docs/WAX-SPLIT.md).

Boundaries come from the waveform and nothing else: no ASR, transcript, diarization or
LLM is ever an input. Detection is read-only; only when it finds a boundary does
anything stop, halt, move or get written. The original and every derived part are
kept, and derived parts are HELD until the operator submits them to the inbox, so the
existing verified-S3-backup-before-transcription path applies to them unchanged.

Sections: detection (decode, levels, boundary), cutting (lossless, provenance),
the three targets (live capture, an item in processing, a finished/queued item),
submit and list.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from . import capture, fence, ledger, operations, paths, rename, sentinel, state

SCHEMA = 1
SAMPLE_RATE = 8000          # analysis rate; speech/silence needs nothing more
FRAME_SAMPLES = 800         # 100 ms
BLOCK_FRAMES = 10           # 1 s blocks
DB_FLOOR = -120.0
CUT_TOLERANCE_S = 2.0       # allowed overlap/gap per cut, see docs/WAX-SPLIT.md (codec page edges)

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_NO_BOUNDARY = 3

_RID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


class SplitError(RuntimeError):
    """The split was refused or failed; nothing was cut, or only our own partial outputs were."""


class HaltTimeout(SplitError):
    pass


@dataclass
class Params:
    """Detection thresholds. Every field is a `wax split` flag; defaults are in docs/WAX-SPLIT.md."""
    min_quiet_s: float = 300.0     # a trailing quiet run must last at least this long
    margin_db: float = 10.0        # activity = this far above the adaptive noise floor...
    floor_dbfs: float = -60.0      # ...and never below this absolute level
    min_range_db: float = 12.0     # noise floor to loud-speech spread needed to see any activity
    blip_s: float = 3.0            # a click or cough this short inside quiet is not activity
    tail_slack_s: float = 60.0     # the quiet run may end this far before the end of the audio
    pad_s: float = 5.0             # keep this much of the quiet run with the first part
    min_active_s: float = 30.0     # activity required before the cut


# ---------------------------------------------------------------------------------------------------------
# Detection


class _Levels:
    """Frame RMS levels (dBFS) built from PCM that arrives in arbitrary chunks, from one or many files.

    The remainder of a chunk that is not a whole frame is carried into the next, so
    a capture made of sixty 60 s segments keeps an exact time base (a per-segment
    truncation would drift about 100 s over sixteen hours).
    """

    def __init__(self) -> None:
        self._carry = b""
        self._chunks: list[Any] = []
        self.samples = 0

    def feed(self, data: bytes) -> None:
        import numpy as np

        self.samples += len(data) // 2
        buf = self._carry + data
        usable = (len(buf) // (2 * FRAME_SAMPLES)) * FRAME_SAMPLES * 2
        if usable:
            x = np.frombuffer(buf[:usable], dtype="<i2").astype(np.float32).reshape(-1, FRAME_SAMPLES)
            rms = np.sqrt((x * x).mean(axis=1)) / 32768.0
            self._chunks.append(20.0 * np.log10(np.maximum(rms, 1e-6)))
        self._carry = buf[usable:]

    def result(self) -> tuple[Any, float]:
        import numpy as np

        frames = np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.float32)
        return frames, self.samples / SAMPLE_RATE


def _decode_into(levels: _Levels, path: Path) -> None:
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
           "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        try:
            assert proc.stdout is not None
            for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
                levels.feed(chunk)
        finally:
            proc.stdout.close()
            rc = proc.wait()
        if rc != 0:
            err.seek(0)
            raise SplitError(f"ffmpeg could not decode {path}: {err.read().decode(errors='replace')[-300:]}")


def decode_levels(files: list[Path], *, tolerate_errors: bool = False) -> tuple[Any, float, list[str]]:
    """Frame levels and exact duration over `files` in order. Returns (levels, seconds, skipped files).

    `tolerate_errors` skips an unreadable file instead of failing (live capture: a
    segment can be mid-write), the same rule `capture.concat_segments` applies.
    """
    levels, skipped = _Levels(), []
    for path in files:
        try:
            _decode_into(levels, path)
        except (SplitError, OSError) as exc:
            if not tolerate_errors:
                raise SplitError(str(exc)) from exc
            skipped.append(str(path))
    frames, seconds = levels.result()
    if len(frames) == 0:
        raise SplitError("no decodable audio to analyze")
    return frames, seconds, skipped


def _runs(mask: Any) -> list[tuple[bool, int, int]]:
    """Run-length encode a boolean array as (value, start, length)."""
    import numpy as np

    if len(mask) == 0:
        return []
    edges = np.flatnonzero(np.diff(mask.astype(np.int8))) + 1
    starts = np.concatenate(([0], edges))
    ends = np.concatenate((edges, [len(mask)]))
    return [(bool(mask[s]), int(s), int(e - s)) for s, e in zip(starts, ends)]


def _no_boundary(reason: str, detail: str, base: dict[str, Any]) -> dict[str, Any]:
    return {**base, "found": False, "reason": reason, "detail": detail, "boundary": None, "segments": []}


def detect(frame_db: Any, duration_s: float, params: Params) -> dict[str, Any]:
    """Find the cut from per-frame levels. Pure: array in, verdict out. See docs/WAX-SPLIT.md."""
    import numpy as np

    pad = (-len(frame_db)) % BLOCK_FRAMES
    padded = np.concatenate((frame_db, np.full(pad, DB_FLOOR, dtype=frame_db.dtype))) if pad else frame_db
    block_db = padded.reshape(-1, BLOCK_FRAMES).max(axis=1)        # 1 s blocks, loudest frame wins
    noise = float(np.percentile(block_db, 10))
    threshold = max(noise + params.margin_db, params.floor_dbfs)
    active = block_db > threshold
    # The loud reference is taken over the active blocks only. A percentile of the whole file would
    # call a 4-minute meeting inside a 2-hour overrun "silence" (under 5% of the blocks are loud).
    loud = float(np.percentile(block_db[active], 90)) if active.any() else float(block_db.max())
    base = {
        "duration_s": round(duration_s, 3),
        "params": asdict(params),
        "stats": {"noise_floor_db": round(noise, 1), "loud_db": round(loud, 1),
                  "threshold_db": round(threshold, 1), "active_s": int(active.sum()),
                  "blocks": int(len(block_db))},
        "quiet_runs": [],
    }

    if not active.any():
        if float(block_db.max()) <= params.floor_dbfs:
            return _no_boundary("no_activity", f"the loudest second is {float(block_db.max()):.1f} dBFS, under the "
                                f"{params.floor_dbfs:g} dBFS floor: silent or all-quiet input", base)
        return _no_boundary("no_contrast", f"nothing rises {params.margin_db:g} dB above the noise floor "
                            f"({noise:.1f} dBFS): continuous activity or a flat hum, so there is no quiet to cut at", base)
    if loud - noise < params.min_range_db:
        return _no_boundary("no_contrast", f"quiet and loud differ by only {loud - noise:.1f} dB "
                            f"(floor {noise:.1f} dBFS, loud {loud:.1f} dBFS; --min-range-db {params.min_range_db:g}): "
                            "continuous activity or a flat hum, so there is no quiet to cut at", base)
    if duration_s < params.min_quiet_s + params.min_active_s:
        return _no_boundary("too_short", f"{duration_s:.0f}s is shorter than --min-quiet-s plus --min-active-s", base)

    # A short burst between two quiet stretches is a click, not activity.
    runs = _runs(active)
    closed = active.copy()
    for i, (value, start, length) in enumerate(runs):
        if value and length <= params.blip_s and 0 < i < len(runs) - 1:
            closed[start:start + length] = False
    runs = _runs(closed)
    quiet = [(start, start + length) for value, start, length in runs if not value]
    base["quiet_runs"] = [{"start_s": s, "end_s": e, "length_s": e - s}
                          for s, e in sorted(quiet, key=lambda r: r[1] - r[0], reverse=True)[:5]
                          if e - s >= min(30.0, params.min_quiet_s / 10)]   # report the pauses worth seeing

    saw_long_not_trailing = False
    chosen: Optional[tuple[int, int]] = None
    for start, end in reversed(quiet):
        if duration_s - end > params.tail_slack_s:
            saw_long_not_trailing = saw_long_not_trailing or (end - start) >= params.min_quiet_s
            break           # earlier runs are only farther from the end
        if end - start >= params.min_quiet_s:
            chosen = (start, end)
            break
    if chosen is None:
        if saw_long_not_trailing:
            return _no_boundary("quiet_not_trailing", f"a quiet run of {params.min_quiet_s:g}s or more exists but "
                                f"activity continues for more than {params.tail_slack_s:g}s after it", base)
        trailing = quiet[-1][1] - quiet[-1][0] if quiet and quiet[-1][1] >= len(closed) else 0
        return _no_boundary("no_sustained_quiet", f"no quiet run of {params.min_quiet_s:g}s reaches the end "
                            f"(longest trailing quiet {trailing}s): continuous activity or only ordinary pauses", base)
    start, end = chosen
    active_before = int(closed[:start].sum())
    if active_before < params.min_active_s:
        return _no_boundary("too_little_activity", f"only {active_before}s of activity before the quiet run "
                            f"(--min-active-s {params.min_active_s:g})", base)

    cut = min(start + params.pad_s, float(end))
    cut = round(float(cut), 3)
    total = round(duration_s, 3)
    return {**base, "found": True, "reason": "trailing_quiet", "detail": None,
            "boundary": {"quiet_start_s": start, "quiet_end_s": end, "quiet_length_s": end - start,
                         "active_before_s": active_before, "cut_s": cut},
            "segments": [{"index": 1, "kind": "active", "start_s": 0.0, "end_s": cut},
                         {"index": 2, "kind": "quiet-tail", "start_s": cut, "end_s": total}]}


def analyze_file(path: Path, params: Params) -> dict[str, Any]:
    frames, seconds, _ = decode_levels([path])
    return detect(frames, seconds, params)


# ---------------------------------------------------------------------------------------------------------
# Cutting, provenance


def _probe(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, timeout=600)
    try:
        return float(r.stdout.strip())
    except ValueError as exc:
        raise SplitError(f"ffprobe could not read {path}: {r.stderr[-200:]}") from exc


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def split_root() -> Path:
    return paths.SKIPPED / "split"


def _manifests() -> Iterator[tuple[Path, dict[str, Any]]]:
    for manifest in sorted(split_root().glob("*/split.json")):
        doc = sentinel.read_json(manifest)
        if doc:
            yield manifest, doc


def find_existing(sha256: str) -> Optional[tuple[Path, dict[str, Any]]]:
    for manifest, doc in _manifests():
        if (doc.get("source") or {}).get("sha256") == sha256:
            return manifest, doc
    return None


def cut_parts(source: Path, detection: dict[str, Any], hold: Path, split_id: str) -> list[dict[str, Any]]:
    """Lossless cuts (`-c copy`) into `hold`, verified against the source, never replacing anything.

    Each part's mtime is set to its own end time (source mtime minus what follows it),
    because the vault names a transcript from the audio's mtime.
    """
    hold.mkdir(parents=True, exist_ok=True)
    total = float(detection["duration_s"])
    segments = detection["segments"]
    mtime = source.stat().st_mtime
    stem, suffix = source.stem, source.suffix or ".ogg"
    made: list[Path] = []
    parts: list[dict[str, Any]] = []
    try:
        for seg in segments:
            name = f"{stem}-part{seg['index']}of{len(segments)}{suffix}"
            tmp = hold / f".{name}.cutting{suffix}"
            cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{seg['start_s']:.3f}", "-i", str(source)]
            if seg is not segments[-1]:
                cmd += ["-t", f"{seg['end_s'] - seg['start_s']:.3f}"]
            cmd += ["-map", "0:a:0", "-c", "copy", str(tmp)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            made.append(tmp)
            if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
                raise SplitError(f"cutting part {seg['index']} failed: {r.stderr[-300:]}")
            final = rename.move_noclobber(tmp, hold / name)
            made[-1] = final
            actual = _probe(final)
            end_mtime = mtime - (total - seg["end_s"])
            os.utime(final, (end_mtime, end_mtime))
            digest = ledger.sha256_file(final)
            parts.append({"index": seg["index"], "kind": seg["kind"], "file": final.name,
                          "start_s": seg["start_s"], "end_s": seg["end_s"],
                          "duration_s": round(actual, 3), "bytes": final.stat().st_size,
                          "sha256": digest, "item_id": digest[:16], "mtime": end_mtime,
                          "state": "held", "submitted_as": None, "submitted_at": None})
        covered = sum(p["duration_s"] for p in parts)
        slack = CUT_TOLERANCE_S * (len(parts) - 1) + 0.5
        if abs(covered - total) > slack:
            raise SplitError(f"parts cover {covered:.2f}s of {total:.2f}s (tolerance {slack:.1f}s); refusing to keep them")
    except BaseException:
        for leftover in made:          # our own partial copies only; the source is never touched
            leftover.unlink(missing_ok=True)
        raise
    return parts


def write_manifest(hold: Path, split_id: str, *, source: dict[str, Any], detection: dict[str, Any],
                   parts: list[dict[str, Any]], origin: str, extra: Optional[dict[str, Any]] = None) -> Path:
    doc = {"schema": SCHEMA, "split_id": split_id, "created_at": sentinel.utcnow(), "origin": origin,
           "method": "waveform-rms", "source": source,
           "detection": {k: detection[k] for k in ("duration_s", "params", "stats", "quiet_runs", "boundary")},
           "segments": parts, "original_settled": False, **(extra or {})}
    manifest = hold / "split.json"
    _write_json(manifest, doc)
    conn = ledger.connect()
    for part in parts:
        conn.execute(
            "INSERT OR REPLACE INTO split_segments(split_id,idx,item_id,source_item_id,source_sha256,source_path,"
            "start_s,end_s,path,sha256,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (split_id, part["index"], part["item_id"], source.get("item_id"), source["sha256"], source.get("path"),
             part["start_s"], part["end_s"], str(hold / part["file"]), part["sha256"], "held", doc["created_at"]))
    return manifest


def _update_manifest(manifest: Path, **changes: Any) -> dict[str, Any]:
    doc = sentinel.read_json(manifest) or {}
    doc.update(changes)
    _write_json(manifest, doc)
    return doc


# ---------------------------------------------------------------------------------------------------------
# Targets


@dataclass
class Source:
    kind: str                       # "item" | "capture"
    path: Optional[Path] = None
    item_id: Optional[str] = None
    item_state: Optional[str] = None
    rid: Optional[str] = None


def resolve(target: Optional[str]) -> Source:
    """TARGET -> an active capture or an item. No target: the one thing it can only mean."""
    if target is None:
        cap = state.stream_state(run_preflight=False)
        claim = state._active_claim()
        live = cap["state"] == "recording"
        if live and claim:
            raise SplitError(f"ambiguous: capture {cap['rid']} is recording and item {claim.get('item')} is in "
                             "processing; name one (a capture id or an item id)")
        if live:
            return Source("capture", rid=cap["rid"])
        if claim:
            return _item_source(str(claim["item"]))
        raise SplitError("nothing to split: no capture is recording and no item is in processing; "
                         "name a capture id, an item id or an audio file")
    if _RID.match(target) and target in sentinel.list_captures():
        return Source("capture", rid=target)
    conn = ledger.connect()
    rows = []
    if re.fullmatch(r"[0-9a-f]{6,64}", target):
        rows = conn.execute("SELECT item_id FROM items WHERE item_id LIKE ?", (target + "%",)).fetchall()
    if not rows:
        rows = conn.execute("SELECT item_id FROM items WHERE orig_name=?", (target,)).fetchall()
    if not rows and ("/" in target or Path(target).exists()):
        resolved = str(Path(target).expanduser().resolve())
        rows = conn.execute("SELECT item_id FROM items WHERE path=?", (resolved,)).fetchall()
        if not rows and Path(resolved).is_file() and _under(Path(resolved), paths.INBOX):
            return Source("item", path=Path(resolved))      # not registered yet; registered only if we cut
    if not rows and "/" not in target:
        named = sorted(f for f in paths.INBOX.rglob(target) if f.is_file() and not f.name.startswith("."))
        if len(named) == 1:
            return Source("item", path=named[0])      # a bare name of a not-yet-registered inbox file
        if len(named) > 1:
            raise SplitError(f"{target!r} names {len(named)} files in the inbox; give a path")
    if not rows:
        raise SplitError(f"no capture or item matches {target!r}")
    if len(rows) > 1:
        raise SplitError(f"{target!r} matches {len(rows)} items; give more of the id")
    return _item_source(rows[0]["item_id"])


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _item_source(item_id: str) -> Source:
    row = ledger.connect().execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
    if row is None:
        raise SplitError(f"unknown item {item_id}")
    return Source("item", path=Path(row["path"]), item_id=item_id, item_state=row["state"])


@contextmanager
def _settled(item_id: str, timeout_s: float) -> Iterator[list[dict[str, Any]]]:
    """Hold the item's flock, having stopped its children. Yields what was halted.

    The worker holds this lock for the whole of an item, so getting it proves the
    in-flight work finished or died; the bounded wait is inside this one command.
    """
    deadline = time.monotonic() + timeout_s
    halted: list[dict[str, Any]] = []
    while True:
        halted += fence.halt_children(item_id)
        with operations.try_item_lock(item_id) as got:
            if got:
                yield halted
                return
        if time.monotonic() >= deadline:
            raise HaltTimeout(f"item {item_id} did not settle within {timeout_s:g}s")
        time.sleep(0.2)


def _new_split_id(sha: str) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{sha[:8]}"


def _report(outcome: str, source: Source, detection: Optional[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"outcome": outcome, "target": {"kind": source.kind, "item_id": source.item_id, "rid": source.rid,
                                           "path": str(source.path) if source.path else None},
            "detection": detection, **extra}


def run(target: Optional[str], params: Params, *, dry_run: bool = False, submit_parts: tuple[str, ...] = (),
        halt_timeout_s: float = 180.0, force: bool = False,
        capture_lock: Optional[Callable[[], Any]] = None) -> dict[str, Any]:
    """Split one recording. The report's `outcome` is split | proposed | already_split | no_boundary."""
    source = resolve(target)
    if source.kind == "capture":
        report = _run_live(source, params, dry_run, halt_timeout_s, capture_lock)
    else:
        report = _run_item(source, params, dry_run, halt_timeout_s, force)
    if submit_parts and report["outcome"] in ("split", "already_split"):
        report["submitted"] = submit(report["split_id"], submit_parts)
    return report


# ---- recording already in processing (or queued, or finished) ----

def _run_item(source: Source, params: Params, dry_run: bool, halt_timeout_s: float, force: bool) -> dict[str, Any]:
    path = source.path
    if path is None or not path.is_file():
        raise SplitError(f"audio for item {source.item_id} is missing: {path}")
    sha = ledger.sha256_file(path)
    registered = ledger.connect().execute("SELECT sha256 FROM items WHERE item_id=?", (source.item_id,)).fetchone() \
        if source.item_id else None
    if registered and registered["sha256"] and registered["sha256"] != sha:
        raise SplitError(f"{path} no longer matches the ledger sha256 for {source.item_id}; refusing")

    detection = analyze_file(path, params)                      # read-only: nothing is halted yet
    if not detection["found"]:
        return _report("no_boundary", source, detection, source_sha256=sha, changes="none")
    if dry_run:
        return _report("proposed", source, detection, source_sha256=sha, changes="none (--dry-run)")

    existing = None if force else find_existing(sha)
    if existing and existing[1].get("original_settled"):
        return _report("already_split", source, detection, source_sha256=sha, split_id=existing[1]["split_id"],
                       manifest=str(existing[0]), segments=existing[1]["segments"], changes="none")

    item_id = source.item_id or ledger.upsert_item(path, origin="split")
    before = ledger.connect().execute("SELECT state FROM items WHERE item_id=?", (item_id,)).fetchone()["state"]
    split_id = existing[1]["split_id"] if existing else _new_split_id(sha)
    hold = split_root() / split_id
    needs_halt = before not in ("complete", "skipped")
    raised = fence.raise_fence(item_id, split_id) if needs_halt else False
    try:
        with _settled(item_id, halt_timeout_s) as halted:
            # Settled: re-read, the item may have moved on or been parked while we waited.
            row = ledger.connect().execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
            current = Path(row["path"])
            # The worker may have finished the item while we waited for its lock: judge by the state
            # now, not the one read before the fence, so a published transcript's item is never
            # flipped from complete to skipped.
            before = row["state"]
            if not current.is_file() or ledger.sha256_file(current) != sha:
                raise SplitError(f"audio of {item_id} changed or vanished while halting ({current}); nothing was cut")
            src_doc = {"kind": "item", "item_id": item_id, "path": str(current), "orig_name": row["orig_name"],
                       "sha256": sha, "bytes": current.stat().st_size, "duration_s": detection["duration_s"],
                       "item_state_before": before}
            if existing:
                manifest, doc = existing
                parts = doc["segments"]
            else:
                parts = cut_parts(current, detection, hold, split_id)
                manifest = write_manifest(hold, split_id, source=src_doc, detection=detection, parts=parts,
                                          origin="item", extra={"halted": halted})
            settled = _settle_item_original(item_id, current, hold, manifest, before, halted)
            if raised and before == "complete":
                fence.lift_fence(item_id, split_id)       # it finished before the fence bit: nothing to protect
                raised = False
    except BaseException:
        if raised:
            fence.lift_fence(item_id, split_id)      # nothing was cut: the item resumes as it was
        raise
    return _report("split", source, detection, split_id=split_id, manifest=str(manifest), held_dir=str(hold),
                   source_sha256=sha, segments=parts, halted_children=halted,
                   processing={"state_before": before, "state_after": settled["state_after"],
                               "fenced": needs_halt, "original": settled["original"],
                               "disposition": settled["disposition"]})


def _settle_item_original(item_id: str, current: Path, hold: Path, manifest: Path, before: str,
                          halted: list[dict[str, Any]]) -> dict[str, Any]:
    """Put the original where it is preserved and the worker will not touch it. Never deletes."""
    conn = ledger.connect()
    original = current
    if _under(current, paths.INBOX):
        original = rename.move_noclobber(current, hold / current.name)
        conn.execute("UPDATE items SET path=?,updated_at=? WHERE item_id=?",
                     (str(original), sentinel.utcnow(), item_id))
    if before in ("complete", "skipped"):
        after, disposition = before, "item untouched (already finished or skipped); parts held"
    else:
        ledger.set_item_state(item_id, "skipped", cause="split", evidence=f"{manifest}")
        what = (f"in-flight work halted ({', '.join(h['argv0'] or '?' for h in halted)})" if halted
                else "no child process was running for it")
        after, disposition = "skipped", f"{what}; original preserved; parts held until submitted"
    _update_manifest(manifest, original_settled=True, original_path=str(original))
    return {"state_after": after, "original": str(original), "disposition": disposition}


# ---- live capture ----

def _run_live(source: Source, params: Params, dry_run: bool, halt_timeout_s: float,
              capture_lock: Optional[Callable[[], Any]]) -> dict[str, Any]:
    rid = source.rid
    assert rid is not None
    lock = capture_lock or _nullcontext
    before = state.stream_state(run_preflight=False)
    if before["state"] != "recording" or before.get("rid") != rid:
        raise SplitError(f"capture {rid} is not recording (stream is {before['state']}); "
                         "use `wax split <item>` once it has been finalized")
    rec = sentinel.read_json(paths.rec_path(rid)) or {}
    segs = sentinel.segments(rid)
    if sentinel.encoder_alive(rec) and segs:
        segs = segs[:-1]                      # the newest segment is still being written
    if not segs:
        raise SplitError(f"capture {rid} has no completed segment yet; nothing to analyze")
    frames, seconds, skipped = decode_levels(segs, tolerate_errors=True)
    snapshot = detect(frames, seconds, params)
    capture_info = {"id": rid, "state_before": before["state"], "segments_analyzed": len(segs) - len(skipped),
                    "segments_unreadable": len(skipped), "snapshot_duration_s": snapshot["duration_s"]}
    if not snapshot["found"]:
        capture_info["state_after"] = state.stream_state(run_preflight=False)["state"]
        return _report("no_boundary", source, snapshot, capture=capture_info, changes="none; capture still recording")
    if dry_run:
        capture_info["state_after"] = state.stream_state(run_preflight=False)["state"]
        return _report("proposed", source, snapshot, capture=capture_info,
                       changes="none (--dry-run); capture still recording")

    hold = split_root() / rid
    with lock():
        now = state.stream_state(run_preflight=False)
        if now["state"] != "recording" or now.get("rid") != rid:
            raise SplitError(f"capture {rid} changed state to {now['state']} before it could be stopped")
        stopped = capture.stop(rid, dest_dir=hold)          # orderly stop + finalize, held away from inbox/
    final = Path(stopped["path"])
    capture_info.update(state_after=state.stream_state(run_preflight=False)["state"],
                        finalized_duration_s=stopped["duration_s"], segments_used=stopped["segments_used"],
                        segments_skipped=stopped["segments_skipped"])
    # The capture is stopped and finalized into the hold by now. If anything below fails, the recording
    # must not be left orphaned there (no manifest, nothing queued): release it to the inbox, unsplit.
    try:
        detection = analyze_file(final, params)             # authoritative: the finalized file
        if not detection["found"]:
            released = rename.move_noclobber(final, paths.INBOX / final.name)
            return _report("no_boundary", source, detection, capture=capture_info,
                           changes=f"capture stopped; unsplit recording released to inbox as {released}")
        sha = ledger.sha256_file(final)
        parts = cut_parts(final, detection, hold, rid)
        src_doc = {"kind": "capture", "rid": rid, "path": str(final), "orig_name": final.name, "sha256": sha,
                   "bytes": final.stat().st_size, "duration_s": detection["duration_s"],
                   "device_source": rec.get("device_source"), "started_at": rec.get("started_at")}
        manifest = write_manifest(hold, rid, source=src_doc, detection=detection, parts=parts, origin="live-capture")
        _update_manifest(manifest, original_settled=True, original_path=str(final))
    except Exception as exc:
        where = _release_unsplit(final)
        raise SplitError(f"capture {rid} was stopped and finalized but splitting it failed ({exc}); "
                         f"the unsplit recording is at {where}") from exc
    return _report("split", source, detection, split_id=rid, manifest=str(manifest), held_dir=str(hold),
                   source_sha256=sha, segments=parts, capture=capture_info,
                   processing={"state_before": None, "state_after": None, "fenced": False, "original": str(final),
                               "disposition": "capture stopped and finalized into the hold, not the inbox; "
                                              "original preserved; parts held until submitted; recording not restarted"})


def _release_unsplit(final: Path) -> Path:
    """Move a finalized, unsplittable recording into the inbox; if even that fails, say where it is."""
    if not final.is_file():
        return final
    try:
        return rename.move_noclobber(final, paths.INBOX / final.name)
    except OSError:
        return final


@contextmanager
def _nullcontext() -> Iterator[None]:
    yield


# ---------------------------------------------------------------------------------------------------------
# Submit, list


def _load(split_id: str) -> tuple[Path, dict[str, Any]]:
    manifest = split_root() / split_id / "split.json"
    doc = sentinel.read_json(manifest)
    if doc is None:
        raise SplitError(f"no split {split_id!r}; see `wax split list`")
    return manifest, doc


def submit(split_id: str, which: tuple[str, ...]) -> dict[str, Any]:
    """Copy held parts into inbox/ so the normal path (S3 backup verified, then transcription) takes over.

    A part is copied under a hidden temp name (the inbox scan ignores dot files), verified by
    sha256, then renamed in atomically without clobbering. Submitting a part twice does nothing.
    """
    manifest, doc = _load(split_id)
    hold = manifest.parent
    chosen: list[int] = []
    for token in which:
        if token == "all":
            chosen += [seg["index"] for seg in doc["segments"]]
        elif token.isdigit() and any(seg["index"] == int(token) for seg in doc["segments"]):
            chosen.append(int(token))
        else:
            raise SplitError(f"split {split_id} has no part {token!r}; parts are "
                             f"{[seg['index'] for seg in doc['segments']]} or `all`")
    results = []
    paths.INBOX.mkdir(parents=True, exist_ok=True)
    for index in dict.fromkeys(chosen):
        seg = next(s for s in doc["segments"] if s["index"] == index)
        if seg["state"] == "submitted":
            results.append({"index": index, "action": "already_submitted", "submitted_as": seg["submitted_as"]})
            continue
        held = hold / seg["file"]
        if not held.is_file() or ledger.sha256_file(held) != seg["sha256"]:
            raise SplitError(f"held part {held} is missing or changed; refusing to submit")
        tmp = paths.INBOX / f".{seg['file']}.submitting"
        shutil.copy2(held, tmp)
        if ledger.sha256_file(tmp) != seg["sha256"]:
            tmp.unlink(missing_ok=True)
            raise SplitError(f"copy of {held} did not verify; nothing submitted")
        final = rename.move_noclobber(tmp, paths.INBOX / seg["file"])
        item_id = ledger.upsert_item(final, origin="split")
        seg.update(state="submitted", submitted_as=str(final), submitted_at=sentinel.utcnow())
        _update_manifest(manifest, segments=doc["segments"])
        ledger.connect().execute("UPDATE split_segments SET state='submitted', submitted_as=? "
                                 "WHERE split_id=? AND idx=?", (str(final), split_id, index))
        results.append({"index": index, "action": "submitted", "submitted_as": str(final), "item_id": item_id})
    return {"split_id": split_id, "parts": results,
            "note": "the worker archives each part to S3 and verifies it before transcription"}


def list_splits(split_id: Optional[str] = None) -> list[dict[str, Any]]:
    rows = []
    for manifest, doc in _manifests():
        if split_id and doc.get("split_id") != split_id:
            continue
        rows.append({"split_id": doc["split_id"], "origin": doc.get("origin"), "created_at": doc.get("created_at"),
                     "held_dir": str(manifest.parent), "source": doc.get("source"),
                     "original_settled": doc.get("original_settled"), "segments": doc.get("segments")})
    if split_id and not rows:
        raise SplitError(f"no split {split_id!r}")
    return rows


# ---------------------------------------------------------------------------------------------------------
# Presentation


def _t(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def render(report: dict[str, Any]) -> str:
    out = []
    det = report.get("detection") or {}
    target = report["target"]
    what = f"capture {target['rid']}" if target["kind"] == "capture" else f"item {target['item_id'] or '(unregistered)'}"
    out.append(f"wax split: {report['outcome'].upper()}  {what}  {target['path'] or ''}".rstrip())
    if det:
        stats = det.get("stats", {})
        out.append(f"  duration {_t(det['duration_s'])}  noise floor {stats.get('noise_floor_db')} dBFS  "
                   f"loud {stats.get('loud_db')} dBFS  activity threshold {stats.get('threshold_db')} dBFS")
        if det["found"]:
            b = det["boundary"]
            out.append(f"  quiet from {_t(b['quiet_start_s'])} to {_t(b['quiet_end_s'])} "
                       f"({b['quiet_length_s']}s); cut at {_t(b['cut_s'])}")
        else:
            out.append(f"  no usable boundary [{det['reason']}]: {det['detail']}")
            for run in det.get("quiet_runs", [])[:3]:
                out.append(f"    quiet run {_t(run['start_s'])}-{_t(run['end_s'])} ({run['length_s']}s)")
    for seg in report.get("segments") or det.get("segments") or []:
        held = f"  {seg['file']}  [{seg['state']}]" if "file" in seg else ""
        out.append(f"  part {seg['index']} ({seg['kind']}): {_t(seg['start_s'])} - {_t(seg['end_s'])}{held}")
    cap = report.get("capture")
    if cap:
        out.append(f"  capture: {cap['state_before']} -> {cap.get('state_after')}")
    proc = report.get("processing")
    if proc:
        if proc["state_before"] is None:
            out.append(f"  processing: not queued yet; {proc['disposition']}")
        else:
            out.append(f"  processing: {proc['state_before']} -> {proc['state_after']}; {proc['disposition']}")
    if report.get("halted_children"):
        out.append(f"  halted: {[h['argv0'] for h in report['halted_children']]}")
    if report.get("changes"):
        out.append(f"  changes: {report['changes']}")
    if report.get("held_dir"):
        out.append(f"  held in {report['held_dir']} (submit with `wax split submit {report['split_id']} all`)")
    if report.get("submitted"):
        out.append(f"  submitted: {[(p['index'], p['action']) for p in report['submitted']['parts']]}")
    return "\n".join(out)


def exit_code(report: dict[str, Any]) -> int:
    return EXIT_NO_BOUNDARY if report["outcome"] == "no_boundary" else EXIT_OK
