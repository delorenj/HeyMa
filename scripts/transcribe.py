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
from datetime import datetime
from pathlib import Path


_progress_file = None

def log(msg: str):
    """Write to stderr and optionally to a progress file for remote tailing."""
    print(msg, file=sys.stderr, flush=True)
    if _progress_file:
        with open(_progress_file, "a") as f:
            f.write(msg + "\n")
            f.flush()


def cuda_runtime_available() -> tuple[bool, str]:
    """Check whether the host can safely use CUDA + cuDNN ops at runtime."""
    if shutil.which("nvidia-smi") is None:
        return False, "nvidia-smi not found"

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
        model = WhisperModel(model_size, device="cuda", compute_type="float16")
        log("  Device: cuda (float16)")
    else:
        # Auto: use CUDA only when runtime preflight succeeds.
        if cuda_ok:
            try:
                model = WhisperModel(model_size, device="cuda", compute_type="float16")
                log("  Device: cuda (float16)")
            except Exception as exc:
                log(f"  CUDA init failed ({exc}); using cpu (int8)")
                model = WhisperModel(model_size, device="cpu", compute_type="int8")
                log("  Device: cpu (int8 fallback)")
        else:
            log(f"  CUDA unavailable ({cuda_reason}); using cpu (int8)")
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            log("  Device: cpu (int8 fallback)")
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
                log(f"  [{pct:3d}%] {ts} / {format_duration(duration)}")

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
        import numpy as np
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
    try:
        signal, sr = librosa.load(audio_path, sr=16000)
        
        # Process in 1-second chunks
        chunk_size = 16000
        chunks = [signal[i:i+chunk_size] for i in range(0, len(signal), chunk_size)]
        
        previous_chunk = None
        l_chunk_feat_seq_t = []
        for chunk in chunks:
            audio_signal_chunk = torch.tensor(chunk).unsqueeze(0).to(diar_model.device)
            audio_signal_length_chunk = torch.tensor([audio_signal_chunk.shape[1]]).to(diar_model.device)
            processed_signal_chunk, processed_signal_length_chunk = audio2mel.get_features(audio_signal_chunk, audio_signal_length_chunk)
            if previous_chunk is not None:
                to_add = previous_chunk[:, :, -99:]
                total = torch.concat([to_add, processed_signal_chunk], dim=2)
            else:
                total = processed_signal_chunk
            previous_chunk = processed_signal_chunk
            l_chunk_feat_seq_t.append(torch.transpose(total, 1, 2))

        batch_size = 1
        streaming_state = init_streaming_state(
            diar_model.sortformer_modules,
            batch_size=batch_size,
            async_streaming=True,
            device=diar_model.device
        )
        total_preds = torch.zeros((batch_size, 0, diar_model.sortformer_modules.n_spk), device=diar_model.device)

        chunk_duration_seconds = diar_model.sortformer_modules.chunk_len * diar_model.sortformer_modules.subsampling_factor * diar_model.preprocessor._cfg.window_stride

        l_speakers = [{'start_time': 0, 'end_time': 0, 'speaker': 0}]
        len_prediction = None
        left_offset = 0
        right_offset = 8
        
        for i, chunk_feat_seq_t in enumerate(l_chunk_feat_seq_t):
            with torch.inference_mode():
                streaming_state, total_preds = diar_model.forward_streaming_step(
                    processed_signal=chunk_feat_seq_t,
                    processed_signal_length=torch.tensor([chunk_feat_seq_t.shape[1]]),
                    streaming_state=streaming_state,
                    total_preds=total_preds,
                    left_offset=left_offset,
                    right_offset=right_offset,
                )
                left_offset = 8
                preds_np = total_preds[0].cpu().numpy()
                active_speakers = np.argmax(preds_np, axis=1)
                if len_prediction is None:
                    len_prediction = len(active_speakers)
                frame_duration = chunk_duration_seconds / len_prediction
                active_speakers = active_speakers[-len_prediction:]
                for idx, spk in enumerate(active_speakers):
                    curr_start = i * chunk_duration_seconds + idx * frame_duration
                    curr_end = i * chunk_duration_seconds + (idx + 1) * frame_duration
                    if spk != l_speakers[-1]['speaker']:
                        l_speakers.append({
                            'start_time': curr_start,
                            'end_time': curr_end,
                            'speaker': spk
                        })
                    else:
                        l_speakers[-1]['end_time'] = curr_end
        
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
    lines = []
    lines.append(f"# Transcription: {Path(audio_path).name}")
    lines.append("")
    lines.append(f"- **Source**: `{audio_path}`")
    lines.append(f"- **Date**: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"- **Duration**: {format_duration(result['duration'])}")
    lines.append(f"- **Language**: {result['language']}")
    lines.append(f"- **Model**: {result['model']} ({result['backend']})")
    if diarization:
        lines.append("- **Diarization**: enabled")
    lines.append("")
    lines.append("---")
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
    parser.add_argument("-m", "--model", default="base", help="Whisper model size: tiny, base, small, medium, large-v3 (default: base)")
    parser.add_argument("-l", "--language", default=None, help="Language code (e.g., en). Auto-detected if omitted.")
    parser.add_argument("--groq", action="store_true", help="Use Groq API instead of local model")
    parser.add_argument("--timestamps", action="store_true", help="Include segment timestamps in output")
    parser.add_argument("--diarization", action="store_true", help="Include speaker diarization (requires nemo_toolkit)")
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

    if args.groq:
        result = transcribe_groq(audio_path, args.model, args.language)
        diarization = None
    else:
        result = transcribe_local(audio_path, args.model, args.language, args.device)
        if args.diarization:
            diarization = diarize_local(audio_path)
        else:
            diarization = None

    log(f"Done. Duration: {format_duration(result['duration'])} | Language: {result['language']}")

    # Determine output path
    if args.output:
        output_path = args.output
    else:
        transcript_dir = Path.home() / "d" / "Transcripts"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        dur = result["duration"] or 0
        dur_h = int(dur // 3600)
        dur_m = int((dur % 3600) // 60)
        # Use file creation date (birth time if available, else mtime)
        stat = Path(audio_path).stat()
        file_time = getattr(stat, "st_birthtime", None) or stat.st_mtime
        date_str = datetime.fromtimestamp(file_time).strftime("%Y%m%d")
        filename = f"{date_str}-{dur_h}h{dur_m:02d}m"
        suffix = ".json" if args.json else ".md"
        output_path = str(transcript_dir / f"{filename}{suffix}")

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
        log(f"Written: {output_path}")


if __name__ == "__main__":
    main()
