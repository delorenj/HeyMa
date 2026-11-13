"""Bounded, waveform-only split requests, executed by waxd, never by the CLI.

Holds fence *new* source operations before waiting on the shared item flock.
An operation already in its critical section settles, including its children and
publication, before any split effects. There is no cancellation/global lock.
Only an atomic complete manifest is a successful split; attempts are retained.
"""

import array
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import capture, ledger, operations, paths, rename, sentinel

THRESHOLDS = {"quiet_dbfs": -45.0, "terminal_quiet_seconds": 120.0,
              "minimum_active_seconds": 30.0, "prefix_padding_seconds": 2.0,
              "window_seconds": 0.1, "channel_rule": "maximum per-channel RMS"}
MEDIA_TIMEOUT = 300
MAX_WAIT = 3600
RID = re.compile(r"^\d{8}-\d{6}-[a-zA-Z0-9]+$")
RECOVERY = ("Source hold persists, including on timeout/no_boundary/failure. Repeat wax split "
            "with the same target to retry; --submit admits only the derived segments. "
            "No automatic source resume. To abandon a split, retain its receipts/audio and "
            "have an operator release the named hold under the same item lock after review; "
            "do not use retry/drain to bypass it. Existing pre-split transcripts are historical.")


def socket_path():
    return paths.VAR / "waxd-split.sock"


def _read(path):
    return json.loads(path.read_text())


def _media(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=MEDIA_TIMEOUT)
    if result.returncode:
        raise RuntimeError(f"media command failed ({result.returncode}): {result.stderr[-1000:]}")
    return result.stdout


def _owned(path):
    path = path.expanduser().resolve()
    roots = (paths.INBOX, paths.ARCHIVE, paths.SKIPPED, paths.RECOVERED, paths.QUARANTINE)
    if not any(path.is_relative_to(root.resolve()) for root in roots):
        raise RuntimeError("target must be owned audio in inbox/archive/skipped/recovered/quarantine, "
                           "or name the capture RID (not dropoff, stream segments or a vault path)")
    return path


@contextmanager
def _capture_lock(deadline):
    paths.ensure_dirs()
    with paths.LOCK.open("a") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("capture lifecycle has not settled")
                threading.Event().wait(min(0.025, remaining))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _target(target, deadline, context):
    """Resolve a capture under its lifecycle flock; never publish it to inbox."""
    conn = ledger.connect()
    if RID.fullmatch(target):
        directory = paths.RECOVERED / "split-captures" / target
        receipt = directory / "capture.json"
        with _capture_lock(deadline):
            if receipt.exists():
                captured = _read(receipt)
                # Complete evidence retirement after an interrupted finalization.
                if paths.rec_path(target).exists():
                    captured["evidence_path"] = str(capture._preserve_orphan_evidence(target))
                    operations.atomic_json(receipt, captured)
            elif paths.rec_path(target).exists():
                directory.mkdir(parents=True, exist_ok=True)
                intent = directory / "split-intent.json"
                operations.atomic_json(intent, {"rid": target, "state": "held_for_split"})
                context.update(capture={"rid": target, "capture_state": "stopping",
                                        "auto_restart": False, "evidence_path": str(paths.STREAM)},
                               capture_hold=str(intent))
                captured = capture.stop(target, for_split=True)
            else:
                raise RuntimeError(f"unknown capture: {target}")
        context["capture"] = captured
        source = _owned(Path(captured["path"]))
        item_id = ledger.upsert_item(source, origin="split-capture")
        return item_id, source
    row = conn.execute("SELECT item_id,path FROM items WHERE item_id=?", (target,)).fetchone()
    if row:
        return row["item_id"], _owned(Path(row["path"]))
    source = _owned(Path(target))
    # A worker may already have parked a path that the caller still names.
    seen = conn.execute("SELECT item_id FROM files_seen WHERE path=?", (str(source),)).fetchone()
    if not source.is_file():
        if seen:
            row = conn.execute("SELECT path FROM items WHERE item_id=?", (seen["item_id"],)).fetchone()
            if row:
                return seen["item_id"], _owned(Path(row["path"]))
        raise RuntimeError(f"source missing: {source}")
    item_id = ledger.upsert_item(source, origin="split")
    return item_id, source


def _decode(source, attempt):
    streams = json.loads(_media(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)]))["streams"]
    audio = [stream for stream in streams if stream["codec_type"] == "audio"]
    if len(audio) != 1:
        raise RuntimeError("split requires exactly one audio stream; refuses to discard other streams")
    rate, channels = int(audio[0]["sample_rate"]), int(audio[0]["channels"])
    decoded = attempt / "decoded.f64le"
    # Float64 PCM represents every decoded int16/int24/int32/float32 sample
    # exactly; WAV outputs retain it without a lossy encode or mono mixdown.
    _media(["ffmpeg", "-v", "error", "-nostdin", "-xerror", "-i", str(source),
            "-map", "0:a:0", "-c:a", "pcm_f64le", "-f", "f64le", str(decoded)])
    with decoded.open("rb") as handle:
        os.fsync(handle.fileno())
    if decoded.stat().st_size % (channels * 8):
        raise RuntimeError("decoded source has an incomplete sample frame")
    return decoded, rate, channels


def _analyze(decoded, rate, channels):
    window = max(1, round(rate * THRESHOLDS["window_seconds"]))
    threshold = 10 ** (THRESHOLDS["quiet_dbfs"] / 20)
    active, total, last_active = 0, 0, 0
    deadline = time.monotonic() + MEDIA_TIMEOUT
    with decoded.open("rb") as handle:
        while block := handle.read(window * channels * 8):
            if time.monotonic() > deadline:
                raise TimeoutError("waveform analysis exceeded its 300 second bound")
            values = array.array("d")
            values.frombytes(block)
            if sys.byteorder != "little":
                values.byteswap()
            count = len(values) // channels
            if any(not math.isfinite(value) for value in values):
                raise RuntimeError("non-finite decoded samples")
            energy = max(sum(v * v for v in values[channel::channels]) / count
                         for channel in range(channels))
            total += count
            if energy >= threshold * threshold:
                active += count
                last_active = total
    quiet = total - last_active
    usable = (active >= math.ceil(rate * THRESHOLDS["minimum_active_seconds"])
              and quiet >= math.ceil(rate * THRESHOLDS["terminal_quiet_seconds"]))
    cut = min(total, last_active + round(rate * THRESHOLDS["prefix_padding_seconds"]))
    reason = ("terminal_quiet" if usable else "insufficient_activity" if active < rate * 30
              else "no_long_terminal_quiet")
    return {"usable": usable, "reason": reason, "total_samples": total,
            "active_samples": active, "terminal_quiet_samples": quiet,
            "quiet_start_sample": last_active, "cut_sample": cut if usable else None,
            "sample_rate": rate, "channels": channels}


def _range_hash(decoded, start, end, channels):
    digest = hashlib.sha256()
    remaining = (end - start) * channels * 8
    with decoded.open("rb") as handle:
        handle.seek(start * channels * 8)
        while remaining:
            block = handle.read(min(1024 * 1024, remaining))
            if not block:
                raise RuntimeError("short decoded range")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def _extract(decoded, attempt, rate, channels, start, end, label):
    output = attempt / (label + ".wav")
    _media(["ffmpeg", "-v", "error", "-nostdin", "-f", "f64le", "-ar", str(rate),
            "-ac", str(channels), "-i", str(decoded), "-af",
            f"atrim=start_sample={start}:end_sample={end},asetpts=PTS-STARTPTS",
            "-c:a", "pcm_f64le", "-rf64", "auto", str(output)])
    expected = _range_hash(decoded, start, end, channels)
    actual = _media(["ffmpeg", "-v", "error", "-nostdin", "-xerror", "-i", str(output),
                     "-map", "0:a:0", "-c:a", "pcm_f64le", "-f", "hash", "-hash", "sha256", "-"]).strip()
    if actual.lower() != "sha256=" + expected:
        raise RuntimeError(f"derived {label} does not losslessly cover its source range")
    with output.open("rb") as handle:
        os.fsync(handle.fileno())
    return {"path": str(output), "sha256": ledger.sha256_file(output),
            "decoded_sha256": expected, "start_sample": start, "end_sample": end,
            "samples": end - start, "start_seconds": start / rate, "end_seconds": end / rate,
            "encoding": "pcm_f64le", "container": "WAV/RF64"}


def _submit(manifest, directory):
    """Journal intent before no-clobber admission; hash IDs make retry safe.

    Worker may move a queued copy before we can record the receipt. On retry,
    its existing item row is the receipt: never reset or resubmit that identity.
    Held outputs are never moved. Files only enter inbox fully fsynced.
    """
    receipt_path = directory / "submission.json"
    receipt = _read(receipt_path) if receipt_path.exists() else {"segments": {}}
    for index, segment in enumerate(manifest["segments"]):
        key = str(index)
        if receipt["segments"].get(key, {}).get("admitted"):
            continue
        source = Path(segment["path"])
        if ledger.sha256_file(source) != segment["sha256"]:
            raise RuntimeError("held segment hash changed; refusing submission")
        item_id = segment["sha256"][:16]
        with operations.item_lock(item_id):
            row = ledger.connect().execute("SELECT path,state FROM items WHERE item_id=?", (item_id,)).fetchone()
            if row:
                receipt["segments"][key] = {"item_id": item_id, "admitted": True,
                                             "disposition": "existing_identity", "path": row["path"],
                                             "existing_state": row["state"]}
                operations.atomic_json(receipt_path, receipt)
                continue
            if operations.is_held(item_id):
                raise RuntimeError(f"derived identity {item_id} has a split hold; not requeued")
            destination = paths.INBOX / f"split-{manifest['source']['item_id']}-{index + 1}.wav"
            entry = receipt["segments"].setdefault(key, {"item_id": item_id, "admitted": False,
                                                         "path": str(destination)})
            destination = Path(entry["path"])
            operations.atomic_json(receipt_path, receipt)
            if destination.exists():
                if ledger.sha256_file(destination) != segment["sha256"]:
                    raise RuntimeError(f"admission collision: {destination}; no audio overwritten")
            else:
                # Stage OUTSIDE inbox, so a worker cannot see a partial copy.
                staged = directory / ("admit-" + str(index) + "-" + str(time.time_ns()) + ".wav")
                shutil.copyfile(source, staged)
                with staged.open("rb") as handle:
                    os.fsync(handle.fileno())
                rename.renameat2_noreplace(staged, destination)
                operations.fsync_dir(paths.INBOX)
            ledger.upsert_item(destination, origin="split:" + manifest["source"]["item_id"])
            entry.update(admitted=True, disposition="inbox_verified_backup_required")
            operations.atomic_json(receipt_path, receipt)
    return receipt


def execute(target, *, submit=False, wait_seconds=60):
    """One request. A failed attempt never replaces the complete manifest."""
    context = {"status": "pending", "target": target, "thresholds": THRESHOLDS,
               "capture": {"capture_state": "not_targeted", "auto_restart": False},
               "processing": {"disposition": "not_settled"}, "recovery": RECOVERY}
    directory = None
    try:
        wait_seconds = float(wait_seconds)
        if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= MAX_WAIT:
            raise RuntimeError(f"wait-seconds must be finite and between 0 and {MAX_WAIT}")
        if not isinstance(target, str) or not target or len(target) > 4096:
            raise RuntimeError("invalid split target")
        deadline = time.monotonic() + wait_seconds
        item_id, source = _target(target, deadline, context)
        prior = ledger.connect().execute("SELECT sha256,bytes,state FROM items WHERE item_id=?",
                                         (item_id,)).fetchone()
        context["source"] = {"item_id": item_id, "path": str(source),
                             "sha256": prior["sha256"] if prior else None,
                             "bytes": prior["bytes"] if prior else None}
        context["processing"]["state_before_settlement"] = prior["state"] if prior else None
        context["hold_path"] = str(operations.hold_source(item_id, source))
        context["source_held"] = True
        directory = paths.RECOVERED / "splits" / item_id
        operations.durable_directory(directory)
        # Same item lock as worker.process/passes/direct transcribe/finalize.
        with operations.item_lock(item_id, timeout=max(0, deadline - time.monotonic())):
            row = ledger.connect().execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
            source = _owned(Path(row["path"])) if row else source
            digest = ledger.sha256_file(source)
            if digest[:16] != item_id or (row and row["sha256"] != digest):
                raise RuntimeError("canonical source identity changed; source retained, no cut")
            context["source"] = {"item_id": item_id, "path": str(source), "sha256": digest,
                                 "bytes": source.stat().st_size}
            transcript = ledger.connect().execute("SELECT md_path FROM transcripts WHERE item_id=?", (item_id,)).fetchone()
            context["processing"] = {"disposition": "settled_then_held", "state": row["state"] if row else None,
                                     "historical_transcript": transcript["md_path"] if transcript else None,
                                     "historical_transcript_policy": "preserved; never overwritten by split"}
            complete = directory / "manifest.json"
            if complete.exists():
                manifest = _read(complete)
                if manifest["source"]["sha256"] != digest:
                    raise RuntimeError("manifest source hash mismatch")
                for segment in manifest["segments"]:
                    if ledger.sha256_file(Path(segment["path"])) != segment["sha256"]:
                        raise RuntimeError("manifest output missing/changed; no success advertised")
                context = {**manifest, "reused": True}
            else:
                attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=directory))
                context["attempt_path"] = str(attempt)
                operations.atomic_json(directory / "state.json", context)
                decoded, rate, channels = _decode(source, attempt)
                analysis = _analyze(decoded, rate, channels)
                context["analysis"] = analysis
                if not analysis["usable"]:
                    context["status"] = "no_boundary"
                    operations.atomic_json(directory / "state.json", context)
                    return context
                cut, total = analysis["cut_sample"], analysis["total_samples"]
                segments = [_extract(decoded, attempt, rate, channels, start, end, label)
                            for start, end, label in ((0, cut, "prefix"), (cut, total, "tail"))]
                if ledger.sha256_file(source) != digest:
                    raise RuntimeError("source changed during split; no complete manifest published")
                operations.fsync_dir(attempt)
                context.update(status="split", segments=segments,
                               decoded_source={"path": str(decoded), "encoding": "f64le",
                                               "sha256": ledger.sha256_file(decoded)}, reused=False)
                # Sole commit point. All samples/hashes/files are durable first.
                operations.atomic_json(complete, context)
            context["manifest_path"] = str(complete)
            context["submission"] = _submit(context, directory) if submit else {"disposition": "held_not_submitted"}
            operations.atomic_json(directory / "state.json", context)
            return context
    except Exception as exc:  # request boundary: failures retain original, hold and attempts
        context.update(status="pending" if isinstance(exc, TimeoutError) else "failed",
                       error=f"{type(exc).__name__}: {exc}")
        if context.get("capture", {}).get("capture_state") == "stopping":
            context["capture"]["capture_state"] = "stop_or_finalize_failed_evidence_retained"
        if directory:
            # Do not replace a successful manifest with a partially successful admission.
            operations.atomic_json(directory / ("request-failure-" + str(time.time_ns()) + ".json"), context)
        return context


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            raw = self.rfile.readline(8193)
            if len(raw) > 8192 or not raw.endswith(b"\n"):
                raise ValueError("invalid split request frame")
            request = json.loads(raw)
            if (not isinstance(request, dict) or set(request) - {"target", "submit", "wait_seconds"}
                    or not isinstance(request.get("submit", False), bool)):
                raise ValueError("invalid split request")
            result = execute(request.get("target"), submit=request.get("submit", False),
                             wait_seconds=request.get("wait_seconds", 60))
        except Exception as exc:
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        try:
            self.wfile.write(json.dumps(result, sort_keys=True).encode() + b"\n")
        except OSError:
            pass  # disconnect never cancels a source operation; durable receipt is recoverable


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve():
    """Called only by waxd. Legacy status socket remains send-and-close."""
    socket_path().unlink(missing_ok=True)
    server = Server(str(socket_path()), _Handler)
    os.chmod(socket_path(), 0o600)
    threading.Thread(target=server.serve_forever, daemon=True, name="wax-split-control").start()
    return server


def request(target, *, submit=False, wait_seconds=60):
    """CLI client. No fallback local worker if waxd is absent."""
    if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= MAX_WAIT:
        return {"status": "failed", "error": f"wait-seconds must be between 0 and {MAX_WAIT}"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(wait_seconds + 7 * MEDIA_TIMEOUT + 90)
            client.connect(str(socket_path()))
            client.sendall(json.dumps({"target": target, "submit": submit,
                                       "wait_seconds": wait_seconds}).encode() + b"\n")
            with client.makefile("rb") as response:
                raw = response.readline(1024 * 1024)
            return json.loads(raw)
    except (OSError, ValueError) as exc:
        return {"status": "failed", "error": f"waxd split control unavailable: {exc}",
                "recovery": "No local fallback. If the request reached waxd, repeat it to inspect its durable receipt."}
