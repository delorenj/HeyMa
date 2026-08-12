#!/usr/bin/env python3
"""Batch transcribe an audio file to markdown using faster-whisper (local model).

Usage:
    uv run python scripts/transcribe.py recording.mp3
    uv run python scripts/transcribe.py recording.mp3 -o transcript.md
    uv run python scripts/transcribe.py recording.mp3 --model large-v3
    uv run python scripts/transcribe.py recording.mp3 --groq  # use Groq API instead
"""

import argparse
import ctypes
import json
import sys
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path


_progress_file = None
_cudnn_lib_dirs: list[Path] = []
_PROGRESS_PREFIX = "Transcription-Progress: "


def _configure_cudnn_runtime_paths() -> None:
    """Expose bundled cuDNN wheel libs to the dynamic loader when present."""
    candidate_dirs = []

    # Preferred: nvidia-cudnn-cu12 wheel layout in the active environment.
    try:
        import nvidia.cudnn as cudnn_pkg  # type: ignore

        if getattr(cudnn_pkg, "__file__", None):
            candidate_dirs.append(Path(cudnn_pkg.__file__).resolve().parent / "lib")
    except Exception:
        pass

    # Fallback: infer from this interpreter's site-packages location.
    for root in {Path(sys.prefix), Path(sys.base_prefix)}:
        for pattern in (
            "lib/python*/site-packages/nvidia/cudnn/lib",
            "Lib/site-packages/nvidia/cudnn/lib",
        ):
            for match in root.glob(pattern):
                candidate_dirs.append(match)

    existing = [path for path in candidate_dirs if path.exists()]
    if not existing:
        return

    global _cudnn_lib_dirs
    seen = set()
    _cudnn_lib_dirs = []
    for path in existing:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        _cudnn_lib_dirs.append(path)

    ld_parts = [part for part in os.environ.get("LD_LIBRARY_PATH", "").split(":") if part]
    for directory in reversed([str(path) for path in _cudnn_lib_dirs]):
        if directory not in ld_parts:
            ld_parts.insert(0, directory)
    os.environ["LD_LIBRARY_PATH"] = ":".join(ld_parts)

    # Preload cuDNN shared objects by absolute path so sanitized environments
    # (where LD_LIBRARY_PATH is not honored at process start) still resolve.
    preload_order = [
        "libcudnn_graph.so.9",
        "libcudnn_ops.so.9",
        "libcudnn_cnn.so.9",
        "libcudnn_adv.so.9",
        "libcudnn_engines_precompiled.so.9",
        "libcudnn_engines_runtime_compiled.so.9",
        "libcudnn_heuristic.so.9",
        "libcudnn.so.9",
    ]
    for lib_dir in _cudnn_lib_dirs:
        absolute_candidates = {name: lib_dir / name for name in preload_order}
        for path in sorted(lib_dir.glob("libcudnn*.so.9")):
            absolute_candidates.setdefault(path.name, path)

        for lib_name in preload_order:
            lib_path = absolute_candidates.get(lib_name)
            if lib_path is None or not lib_path.exists():
                continue
            try:
                ctypes.CDLL(str(lib_path), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                continue
        for extra_name, extra_path in absolute_candidates.items():
            if extra_name in preload_order or not extra_path.exists():
                continue
            try:
                ctypes.CDLL(str(extra_path), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                continue


_configure_cudnn_runtime_paths()

def log(msg: str):
    """Write to stderr and optionally to a progress file for remote tailing."""
    print(msg, file=sys.stderr, flush=True)
    if _progress_file:
        with open(_progress_file, "a") as f:
            f.write(msg + "\n")
            f.flush()


def report_progress(stage: str, percent: int | None = None, **detail) -> None:
    """Emit one machine-readable heartbeat without coupling to a host runner."""
    record = {"stage": stage}
    if percent is not None:
        record["percent"] = max(0, min(100, int(percent)))
    record.update({key: value for key, value in detail.items() if value is not None})
    log(_PROGRESS_PREFIX + json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def cuda_runtime_available() -> tuple[bool, str]:
    """Check whether the host can safely use CUDA + cuDNN ops at runtime."""
    if shutil.which("nvidia-smi") is None:
        return False, "nvidia-smi not found"

    absolute_candidates = []
    for lib_dir in _cudnn_lib_dirs:
        absolute_candidates.extend(sorted(lib_dir.glob("libcudnn_ops.so.9*")))

    for lib_path in absolute_candidates:
        try:
            handle = ctypes.CDLL(str(lib_path), mode=ctypes.RTLD_GLOBAL)
        except OSError:
            continue

        if hasattr(handle, "cudnnCreateTensorDescriptor"):
            return True, str(lib_path)

    candidates = (
        "libcudnn_ops.so.9.1.0",
        "libcudnn_ops.so.9.1",
        "libcudnn_ops.so.9",
        "libcudnn_ops.so",
    )

    for lib_name in candidates:
        try:
            handle = ctypes.CDLL(lib_name)
        except OSError:
            continue

        if hasattr(handle, "cudnnCreateTensorDescriptor"):
            return True, lib_name

    return False, "missing libcudnn_ops runtime/symbol"


# large-v3 in float16 needs ~4.5-5 GB of VRAM to load + decode. This host's GPU
# is shared (ComfyUI has historically spiked to ~18 GB; vexa runs live-meeting
# whisper workers), so a cold cuda load can OOM mid-job or starve a live meeting.
# Guard the batch path: require a free-VRAM margin, waiting briefly for a
# transient spike (e.g. ComfyUI) to clear before falling back to CPU int8.
# Tunable via env for CI / low-VRAM hosts.
_MIN_FREE_VRAM_MB = int(os.getenv("TRANSCRIBE_MIN_FREE_VRAM_MB", "5000"))
_VRAM_WAIT_SECS = int(os.getenv("TRANSCRIBE_VRAM_WAIT_SECS", "30"))


def free_vram_mb() -> int | None:
    """Return free VRAM (MB) on the first GPU, or None if it can't be read."""
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    lines = out.stdout.strip().splitlines()
    if not lines:
        return None
    try:
        return int(lines[0].strip())
    except ValueError:
        return None


def gpu_vram_headroom(min_mb: int = _MIN_FREE_VRAM_MB,
                      wait_secs: int = _VRAM_WAIT_SECS) -> tuple[bool, str]:
    """Poll free VRAM, waiting up to wait_secs for a transient spike to clear.

    Returns (headroom_ok, reason). If free VRAM can't be read we optimistically
    return True and let the cuda init path's own try/except handle failure.
    """
    poll_every = 5
    polls = max(1, wait_secs // poll_every + 1)
    last = None
    for attempt in range(polls):
        free = free_vram_mb()
        if free is None:
            return True, "free VRAM unreadable; deferring to cuda init"
        last = free
        if free >= min_mb:
            return True, f"{free} MB free (need {min_mb})"
        if attempt + 1 < polls:
            log(f"  [vram] only {free} MB free (need {min_mb}); "
                f"waiting {poll_every}s for GPU to free up...")
            time.sleep(poll_every)
    return False, f"{last} MB free (< {min_mb} after {wait_secs}s)"


def missing_diarization_dependencies() -> list[str]:
    """Return a list of missing modules required for Sortformer diarization."""
    missing = []
    try:
        import librosa  # noqa: F401
    except ImportError:
        missing.append("librosa")

    try:
        import nemo  # noqa: F401
    except ImportError:
        missing.append("nemo_toolkit[asr]")

    return missing


def transcribe_local(audio_path: str, model_size: str, language: str | None, device: str | None = None) -> dict:
    """Transcribe using faster-whisper locally."""
    from faster_whisper import WhisperModel

    cuda_ok, cuda_reason = cuda_runtime_available()

    if device == "cpu":
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        log("  Device: cpu (int8)")
    elif device == "cuda":
        if not cuda_ok:
            raise RuntimeError(f"CUDA requested but unavailable: {cuda_reason}")
        # Honor the explicit request, but wait out a transient spike first. Never
        # silently downgrade a forced --device cuda; just warn if headroom is low.
        vram_ok, vram_reason = gpu_vram_headroom()
        if not vram_ok:
            log(f"  [vram] WARNING: proceeding on cuda with low headroom ({vram_reason})")
        model = WhisperModel(model_size, device="cuda", compute_type="float16")
        log("  Device: cuda (float16)")
    else:
        # Auto: use CUDA only when the runtime preflight AND VRAM headroom pass.
        vram_ok, vram_reason = gpu_vram_headroom() if cuda_ok else (False, cuda_reason)
        if cuda_ok and vram_ok:
            try:
                model = WhisperModel(model_size, device="cuda", compute_type="float16")
                log(f"  Device: cuda (float16) — {vram_reason}")
            except Exception as exc:
                log(f"  CUDA init failed ({exc}); using cpu (int8)")
                model = WhisperModel(model_size, device="cpu", compute_type="int8")
                log("  Device: cpu (int8 fallback)")
        else:
            reason = cuda_reason if not cuda_ok else vram_reason
            log(f"  Skipping cuda ({reason}); using cpu (int8)")
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            log("  Device: cpu (int8)")
    segments, info = model.transcribe(
        audio_path,
        language=language,
        beam_size=5,
        vad_filter=True,  # skip silence
    )

    duration = info.duration or 0
    transcript_segments = []
    full_text_parts = []
    last_pct = -1
    report_progress("transcribe", 0, duration_s=round(duration, 3))
    for segment in segments:
        transcript_segments.append({
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip(),
        })
        full_text_parts.append(segment.text.strip())
        # Progress reporting every 1%
        if duration > 0:
            pct = int(segment.end / duration * 100)
            if pct > last_pct:
                last_pct = pct
                ts = format_timestamp(segment.end)
                report_progress(
                    "transcribe", pct, position=ts,
                    duration=format_duration(duration),
                )

    report_progress("transcribe", 100, duration_s=round(duration, 3))

    return {
        "text": " ".join(full_text_parts),
        "segments": transcript_segments,
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
        "model": model_size,
        "backend": "faster-whisper",
    }


def diarize_local(audio_path: str) -> list:
    """Identify speakers using Sortformer."""
    try:
        import librosa
        import torch
        from whisperlivekit.diarization.sortformer_backend_offline import (
            load_model,
            init_streaming_state,
        )
    except ImportError as e:
        log(f"Error: Missing dependency for diarization: {e}")
        log("Please install librosa and nemo_toolkit[asr].")
        return []

    log("  Loading diarization model (Sortformer)...")
    report_progress("diarize", 0, detail="loading Sortformer")
    try:
        diar_model, audio2mel = load_model()
    except Exception as e:
        log(f"Error: Failed to load diarization model: {e}")
        return []

    # Force diarization to CPU for stability on hosts where CUDA runtime is partial.
    cpu_device = torch.device("cpu")
    try:
        diar_model = diar_model.to(cpu_device)
        audio2mel = audio2mel.to(cpu_device)
    except Exception:
        pass

    log("  Running diarization (cpu)...")
    report_progress("diarize", 0, detail="running on CPU")
    try:
        signal, sr = librosa.load(audio_path, sr=16000)

        # Process in one-second chunks, but never retain the full feature or
        # prediction history. The old implementation accumulated every mel
        # tensor and passed a growing `total_preds` back into every step, then
        # copied that entire history to NumPy. That made long recordings
        # quadratic and left the UI apparently frozen at ASR 99% for hours.
        chunk_size = 16000
        total_chunks = max(1, (len(signal) + chunk_size - 1) // chunk_size)

        previous_chunk = None
        batch_size = 1
        streaming_state = init_streaming_state(
            diar_model.sortformer_modules,
            batch_size=batch_size,
            async_streaming=True,
            device=diar_model.device
        )
        empty_preds = torch.zeros(
            (batch_size, 0, diar_model.sortformer_modules.n_spk),
            device=diar_model.device,
        )

        chunk_duration_seconds = diar_model.sortformer_modules.chunk_len * diar_model.sortformer_modules.subsampling_factor * diar_model.preprocessor._cfg.window_stride

        l_speakers = []
        left_offset = 0
        right_offset = 8
        last_pct = -1
        last_heartbeat = time.monotonic()

        for i, start in enumerate(range(0, len(signal), chunk_size)):
            chunk = signal[start:start + chunk_size]
            audio_signal_chunk = torch.tensor(chunk).unsqueeze(0).to(diar_model.device)
            audio_signal_length_chunk = torch.tensor(
                [audio_signal_chunk.shape[1]], device=diar_model.device,
            )
            processed_signal_chunk, _ = audio2mel.get_features(
                audio_signal_chunk, audio_signal_length_chunk,
            )
            if previous_chunk is not None:
                features = torch.concat(
                    [previous_chunk[:, :, -99:], processed_signal_chunk], dim=2,
                )
            else:
                features = processed_signal_chunk
            previous_chunk = processed_signal_chunk
            chunk_feat_seq_t = torch.transpose(features, 1, 2)

            with torch.inference_mode():
                # `forward_streaming_step` only uses total_preds as an output
                # accumulator; speaker alignment lives in streaming_state. Give
                # it an empty accumulator each time so work stays O(chunks).
                streaming_state, chunk_preds = diar_model.forward_streaming_step(
                    processed_signal=chunk_feat_seq_t,
                    processed_signal_length=torch.tensor(
                        [chunk_feat_seq_t.shape[1]], device=diar_model.device,
                    ),
                    streaming_state=streaming_state,
                    total_preds=empty_preds,
                    left_offset=left_offset,
                    right_offset=right_offset,
                )
                left_offset = 8
                active_speakers = torch.argmax(chunk_preds[0], dim=1).cpu().tolist()
                if not active_speakers:
                    continue
                frame_duration = chunk_duration_seconds / len(active_speakers)
                for idx, spk in enumerate(active_speakers):
                    curr_start = i * chunk_duration_seconds + idx * frame_duration
                    curr_end = i * chunk_duration_seconds + (idx + 1) * frame_duration
                    if not l_speakers or spk != l_speakers[-1]['speaker']:
                        l_speakers.append({
                            'start_time': curr_start,
                            'end_time': curr_end,
                            'speaker': spk
                        })
                    else:
                        l_speakers[-1]['end_time'] = curr_end

            pct = int((i + 1) / total_chunks * 100)
            now = time.monotonic()
            if pct > last_pct or now - last_heartbeat >= 30:
                last_pct = pct
                last_heartbeat = now
                report_progress(
                    "diarize", pct, chunks_done=i + 1,
                    chunks_total=total_chunks,
                )

        report_progress("diarize", 100, chunks_total=total_chunks)
        return l_speakers
    except Exception as e:
        log(f"Error: Diarization failed: {e}")
        log("Continuing without speaker labels.")
        return []


def transcribe_groq(audio_path: str, model: str, language: str | None) -> dict:
    """Transcribe using Groq's Whisper API."""
    import httpx

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("Error: GROQ_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

    groq_model = {
        "large-v3": "whisper-large-v3",
        "large-v3-turbo": "whisper-large-v3-turbo",
        "distil": "distil-whisper-large-v3-en",
    }.get(model, "whisper-large-v3-turbo")

    with open(audio_path, "rb") as f:
        files = {"file": (Path(audio_path).name, f, "audio/mpeg")}
        data = {"model": groq_model, "response_format": "verbose_json"}
        if language:
            data["language"] = language

        response = httpx.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            data=data,
            timeout=300,
        )

    if response.status_code != 200:
        print(f"Groq API error ({response.status_code}): {response.text}", file=sys.stderr)
        sys.exit(1)

    result = response.json()
    segments = []
    for seg in result.get("segments", []):
        segments.append({
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"].strip(),
        })

    return {
        "text": result.get("text", "").strip(),
        "segments": segments,
        "language": result.get("language", "unknown"),
        "language_probability": None,
        "duration": result.get("duration"),
        "model": groq_model,
        "backend": "groq",
    }


def format_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_duration(seconds: float) -> str:
    """Human-readable duration."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}m {s}s"


def to_markdown(result: dict, audio_path: str, include_timestamps: bool, diarization: list = None) -> str:
    """Format transcription result as markdown."""
    # Machine-readable enrichment belongs in frontmatter. Keeping it out of the
    # prose means the transcript begins with the transcript, while Obsidian and
    # downstream automation retain the full provenance.
    metadata = {
        "source-audio": str(Path(audio_path).resolve()),
        "transcribed-at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "audio-duration-seconds": round(float(result.get("duration") or 0), 3),
        "language": result.get("language"),
        "engine-model": result.get("model"),
        "transcription-backend": result.get("backend"),
        "diarized": bool(diarization),
    }
    lines = ["---"]
    for key, value in metadata.items():
        # JSON scalars are valid YAML scalars and safely quote paths/strings.
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.extend(["---", ""])
    lines.append(f"# Transcription: {Path(audio_path).name}")
    lines.append("")

    if result["segments"]:
        current_speaker = None
        for seg in result["segments"]:
            ts = format_timestamp(seg["start"])

            speaker_label = ""
            if diarization:
                # Find the speaker that overlaps most with this segment
                seg_start = seg["start"]
                seg_end = seg["end"]
                overlaps = []
                for dseg in diarization:
                    overlap_start = max(seg_start, dseg["start_time"])
                    overlap_end = min(seg_end, dseg["end_time"])
                    if overlap_end > overlap_start:
                        overlaps.append((overlap_end - overlap_start, dseg["speaker"]))

                if overlaps:
                    speaker = max(overlaps, key=lambda x: x[0])[1]
                    if speaker != current_speaker:
                        current_speaker = speaker
                        speaker_label = f"**Speaker {speaker+1} ({ts}):** "
                    else:
                        # Same speaker, but maybe we want to show timestamp if requested
                        if include_timestamps:
                            speaker_label = f"**({ts})** "
                else:
                    if include_timestamps:
                        speaker_label = f"**({ts})** "
            elif include_timestamps:
                speaker_label = f"**[{ts}]** "

            if speaker_label:
                # If we have a speaker change or forced timestamp, start a new paragraph
                if lines and lines[-1] != "":
                    lines.append("")
                lines.append(f"{speaker_label}{seg['text']}")
            else:
                # Append to current paragraph
                if lines and lines[-1] != "" and not lines[-1].startswith("#") and not lines[-1].startswith("-") and not lines[-1].startswith("---"):
                    lines[-1] += f" {seg['text']}"
                else:
                    lines.append(seg['text'])
    else:
        lines.append(result["text"])

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Transcribe audio to markdown")
    parser.add_argument("audio", help="Path to audio file (MP3, WAV, etc.)")
    parser.add_argument("-o", "--output", help="Output markdown file (default: same name as input with .md)")
    parser.add_argument("-m", "--model", default="large-v3", help="Whisper model size: tiny, base, small, medium, large-v3 (default: large-v3)")
    parser.add_argument("-l", "--language", default=None, help="Language code (e.g., en). Auto-detected if omitted.")
    parser.add_argument("--groq", action="store_true", help="Use Groq API instead of local model")
    parser.add_argument("--timestamps", action="store_true", help="Include segment timestamps in output")
    parser.add_argument(
        "--diarization",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include speaker diarization (default: enabled; use --no-diarization to opt out)",
    )
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of markdown")
    parser.add_argument("--stdout", action="store_true", help="Print to stdout instead of writing file")
    parser.add_argument("--device", choices=["cpu", "cuda", "auto"], default="auto", help="Device for inference (default: auto)")
    parser.add_argument("--progress-file", default=None, help="Write progress to this file (for remote monitoring)")
    args = parser.parse_args()

    audio_path = args.audio
    if not Path(audio_path).exists():
        print(f"Error: File not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    if args.diarization:
        missing = missing_diarization_dependencies()
        if missing:
            log(f"Warning: --diarization requested but missing dependencies: {', '.join(missing)}")
            log('Install with: uv pip install "nemo_toolkit[asr]"')
            log("Continuing without diarization.")
            args.diarization = False

    global _progress_file
    _progress_file = args.progress_file
    if _progress_file:
        Path(_progress_file).parent.mkdir(parents=True, exist_ok=True)
        Path(_progress_file).write_text("")  # truncate

    log(f"Transcribing: {audio_path}")
    log(f"Backend: {'groq' if args.groq else 'faster-whisper'} | Model: {args.model}")
    report_progress("preparing", 0)

    if args.groq:
        result = transcribe_groq(audio_path, args.model, args.language)
        diarization = None
    else:
        result = transcribe_local(audio_path, args.model, args.language, args.device)
        if args.diarization:
            diarization = diarize_local(audio_path)
        else:
            diarization = None

    report_progress("finalize", 0)
    log(f"Done. Duration: {format_duration(result['duration'])} | Language: {result['language']}")

    # Determine output path
    if args.output:
        output_path = args.output
    else:
        transcript_dir = Path.home() / "d" / "Transcripts"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        # Name the transcript after the SOURCE file stem so distinct recordings
        # never collide. The old "<date>-<duration>" scheme silently overwrote
        # short same-day clips (18 voice memos collapsed onto 2 files). The stem
        # is unique per recording; a numeric guard covers any residual clash and
        # guarantees we NEVER overwrite an existing transcript.
        stem = Path(audio_path).stem
        suffix = ".json" if args.json else ".md"
        candidate = transcript_dir / f"{stem}{suffix}"
        n = 1
        while candidate.exists():
            candidate = transcript_dir / f"{stem}-{n}{suffix}"
            n += 1
        output_path = str(candidate)

    if args.json:
        # Include diarization in JSON if present
        if diarization:
            result["diarization"] = diarization
        output = json.dumps(result, indent=2, ensure_ascii=False)
    else:
        output = to_markdown(result, audio_path, args.timestamps, diarization)

    if args.stdout:
        print(output)
    else:
        Path(output_path).write_text(output, encoding="utf-8")
        meta = {
            "mdPath": output_path,
            "model": result.get("model"),
            "backend": result.get("backend"),
            "duration_seconds": result.get("duration"),
            "language": result.get("language"),
            "word_count": len((result.get("text") or "").split()),
            "segment_count": len(result.get("segments") or []),
            "diarized": bool(diarization),
        }
        # Metadata is carried in-band for the parent process. It is deliberately
        # not persisted as a sibling .meta.json artifact.
        log("Transcription-Metadata: " + json.dumps(meta, ensure_ascii=False))
        log(f"Written: {output_path}")
        report_progress("finalize", 100, output=str(output_path))


if __name__ == "__main__":
    main()
