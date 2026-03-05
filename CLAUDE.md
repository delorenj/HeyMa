# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**HeyMa** is a voice-controlled AI assistant in the 33GOD ecosystem (Domain: Dashboards & Voice). Users speak commands that are transcribed via Whisper, published to Bloodbank (RabbitMQ event bus), processed through NODE-Red workflows and the Tonny Agent (Letta), then responded to via ElevenLabs TTS.

> **n8n has been deprecated and replaced by NODE-Red** (https://nodered.delo.sh). All webhook references should use NODE-Red URLs.

## Architecture

```
User Speech --> Chrome Extension / TonnyTray / CLI
                        |
                   WebSocket (audio)
                        |
                WhisperLiveKit Server (Python/FastAPI, port 8888)
                        |
         +--------------+--------------+
         |              |              |
    Bloodbank      NODE-Red       SQLite WAL
    (RabbitMQ)     Webhook      (raw_voice_ingest.jsonl)
         |              |
    Tonny Agent    Workflow
    (Letta)        Processing
         |              |
         +--------------+
                |
          ElevenLabs TTS --> Audio Output
```

### Components

- **WhisperLiveKit** (`whisperlivekit/`) - Python transcription server. Entry point: `whisperlivekit/basic_server.py`. Publishes events to Bloodbank via `BloodbankPublisher` with WAL durability.
- **TonnyTray** (`TonnyTray/`) - Tauri 2.x desktop app. React/TypeScript frontend + Rust backend. System tray with global hotkey.
- **Chrome Extension** (`chrome-extension/`) - Manifest V3 browser extension for tab audio capture.
- **Integration Backend** (`TonnyTray/backend/`) - Python orchestrator for ElevenLabs TTS, Letta agent, and RabbitMQ consumers.
- **CLI Scripts** (`scripts/`, `bin/`) - Convenience wrappers for server management and client utilities.

### Event Contracts (Bloodbank)

Events follow the 33GOD pattern: `{domain}.{entity}.{action}`

| Emitted Event | Trigger |
|---|---|
| `transcription.voice.completed` | Whisper produces final transcription |
| `thread.tonny.prompt` | Transcription sent to Tonny Agent |
| `thread.tonny.response` | Tonny Agent generates response |
| `thread.tonny.speech_start` / `speech_end` | Voice activity detected/ended |

| Consumed Event | Purpose |
|---|---|
| `tonny.response.generated` | Receive AI response for TTS |

See `GOD.md` for full event payload schemas and the component's architectural position.

## Development Commands

### WhisperLiveKit Server (Python)

```bash
uv sync                                    # Install dependencies
./scripts/start_server.sh                  # Start server
./scripts/stop_server.sh                   # Stop server
uv run whisperlivekit-server --port 8888   # Manual start with options

# Client utilities
./bin/auto-type                            # Type transcriptions into active window
./bin/auto-type --remote whisper.delo.sh   # Connect to remote server
./bin/auto-type --list-devices             # List audio devices
./bin/n8n-webhook --n8n-webhook https://nodered.delo.sh/webhook/transcription

# Testing/debugging
uv run python scripts/test_connection.py
uv run python scripts/debug_client.py
uv run python scripts/check_device_rates.py
```

### TonnyTray Desktop App (Tauri)

```bash
cd TonnyTray
npm install                    # Install Node dependencies

# Development
npm run tauri:dev              # Full Tauri dev mode (hot reload for frontend)
npm run dev                    # Frontend only

# Build
npm run tauri:build            # Production build

# Quality
npm run lint                   # ESLint
npm run type-check             # TypeScript type checking

# Tests
npm run test                   # Vitest watch mode
npm run test:run               # Vitest single run (CI)
npm run test:coverage          # Coverage report
npm run test:integration       # Integration tests
npm run test:e2e               # Playwright E2E
npm run test:e2e:headed        # E2E with visible browser
npm run test:all               # Rust + TypeScript + E2E

# Single test
npm run test -- src/components/Common/ConfirmDialog.test.tsx
npm run test:e2e -- e2e/workflows.spec.ts

# Rust backend
cd src-tauri
cargo test                     # Run tests
cargo test test_process_manager  # Single test by name
cargo clippy                   # Lint
cargo fmt                      # Format
cargo bench                    # Benchmarks
```

### Integration Backend

```bash
cd TonnyTray/backend
python main.py start           # Start integration orchestrator
python main.py test            # Test all integrations
python main.py health          # Check service health
python main.py tts "Hello" --voice "Antoni"  # Test TTS
python main.py publish "thread.tonny.test" '{"message": "test"}'  # Publish test event
```

### Docker

```bash
docker-compose up -d           # WhisperLiveKit with GPU, exposed at whisper.delo.sh via Traefik
```

## Key Architecture Patterns

### TonnyTray IPC (Frontend <-> Rust)

React frontend communicates with Rust backend via Tauri IPC:

1. **Service Layer** (`TonnyTray/src/services/tauri.ts`) - Type-safe `invoke` wrappers
2. **State Management** (`TonnyTray/src/hooks/useTauriState.ts`) - Zustand store, subscribes to Tauri events
3. **Rust Handlers** (`TonnyTray/src-tauri/src/lib.rs`) - Registers all IPC command handlers

Rust backend modules: `state`, `process_manager`, `audio`, `websocket`, `elevenlabs`, `config`, `tray`, `database`, `keychain`, `events`

### Shared Types

Frontend types (`TonnyTray/src/types/index.ts`) must match Rust types (`src-tauri/src/state.rs`). When adding fields:
1. Update Rust struct with `#[derive(Serialize)]`
2. Update TypeScript interface
3. snake_case to camelCase handled by serde

### TypeScript Path Aliases

`@/` -> `src/`, `@components/` -> `src/components/`, `@hooks/`, `@services/`, `@types/`, `@utils/`, `@theme/`, `@contexts/`

### WhisperLiveKit WebSocket Protocol

- Connect: `ws://localhost:8888/asr`
- Send: binary audio chunks (WebM format)
- Receive: JSON with `lines[]`, `buffer_transcription`, `status`
- Session lifecycle: `session_info` -> transcription updates -> `ready_to_stop`

### Bloodbank Publisher (`whisperlivekit/bloodbank_publisher.py`)

Events are durably published:
1. Write to WAL (`raw_voice_ingest.jsonl`) first
2. Publish via `bb` CLI: `bb publish transcription.voice.completed --json -`
3. Retry with exponential backoff (max 3 attempts)
4. Failed events stay in WAL for later replay

### System State Machine

```
Disabled -> Idle -> Listening -> Processing -> Idle
Any state -> Error
```

## Environment

- **Python**: 3.10 (managed by mise, see `.mise.toml`)
- **Package manager**: `uv` (NOT pip). Always use `uv run` or `uv add`.
- **Node**: npm for TonnyTray
- **Rust**: Tauri 2.x backend
- **Dependencies**: `pyproject.toml` (Python), `TonnyTray/package.json` (Node), `TonnyTray/src-tauri/Cargo.toml` (Rust)

### Configuration Locations

- TonnyTray config: `~/.config/tonnytray/config.json`
- TonnyTray database: `~/.local/share/tonnytray/tonnytray.db`
- WAL file: `raw_voice_ingest.jsonl` (working directory)
- Integration config: `TonnyTray/backend/config.json`

### Environment Variables

```bash
ELEVENLABS_API_KEY=sk-...
N8N_WEBHOOK_URL=https://nodered.delo.sh/webhook/transcription
RABBITMQ_URL=amqp://guest:guest@localhost/
WHISPER_MODEL=base
WHISPER_LANGUAGE=en
```

## Key References

- `GOD.md` - Full component GOD document with event payload schemas, data models, IPC command reference
- `docs/bloodbank-integration.md` - Bloodbank event publishing details
- `TonnyTray/docs/threads/IPC_REFERENCE.md` - Complete IPC command documentation
