# HeyMa — Agent Guide

Voice-to-text interface for the 33GOD ecosystem. Enables natural voice interaction with the pipeline.

## Tech Stack

- **Language:** Python 3.9+
- **Core:** WhisperLiveKit (real-time speech-to-text with speaker diarization)
- **STT:** faster-whisper
- **Audio:** PyAudio, librosa, soundfile, torchaudio
- **Messaging:** aio-pika (Bloodbank integration)
- **AI Memory:** letta-client
- **Web:** FastAPI, Uvicorn, WebSockets
- **Hardware:** Raspberry Pi Zero + Wisconsin Protocol (TonnyBox satellite)

## Subcomponents

- **TonnyBox** (`TonnyBox/`): Hardware satellite for household voice capture
- **TonnyTray** (`TonnyTray/`): System tray application

## Key Entry Points

- `whisperlivekit-server` — Main server CLI
- `whisperlivekit.basic_server:main` — Server entry point

## Conventions

- Real-time streaming via WebSockets
- Speaker diarization enabled by default
- Events published to Bloodbank on transcription complete
- GPU-accelerated inference when available (triton on x86_64 Linux)

## Anti-Patterns

- Never block the audio processing pipeline with synchronous calls
- Never skip diarization in multi-speaker scenarios
- Never hardcode audio device indices

## See Also

- `CLAUDE.md` — Detailed architecture diagram, component map, event contracts
