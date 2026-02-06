# HeyMa - GOD Document

> **Guaranteed Organizational Document** - Developer-facing reference for HeyMa
>
> **Last Updated**: 2026-02-02
> **Domain**: Dashboards & Voice
> **Status**: Active

---

## Product Overview

**HeyMa** is a voice-controlled AI assistant system that transforms spoken commands into actionable workflows. It combines real-time speech-to-text transcription, AI-powered response generation, and natural text-to-speech output to create a seamless voice interface for the 33GOD ecosystem.

The system captures audio from either a Chrome browser extension or a desktop application, transcribes it using OpenAI's Whisper model, processes the transcription through workflow automation (NODE-Red), generates intelligent responses via the Tonny Agent (Letta), and speaks the response back to the user using ElevenLabs TTS.

**Key Capabilities:**
- Real-time voice transcription using Whisper with speaker diarization
- Workflow automation integration for processing voice commands
- Natural text-to-speech responses via ElevenLabs
- Multi-modal capture: browser tab audio or microphone input
- Persistent conversation context with SQLite storage
- Event-driven architecture with Bloodbank integration
- Desktop system tray application with global hotkey support
- Chrome extension for browser-based voice capture

---

## Architecture Position

```mermaid
graph TB
    subgraph "33GOD Pipeline"
        USER[User Voice Input]
        BB[Bloodbank<br/>Event Bus]
        TONNY[Tonny Agent<br/>Letta]
    end

    subgraph "HeyMa System"
        CHROME[Chrome Extension]
        TRAY[TonnyTray<br/>Desktop App]
        WHISPER[WhisperLiveKit<br/>Transcription Server]
        BACKEND[Integration Backend<br/>Python]
        TTS[ElevenLabs<br/>TTS Service]
    end

    subgraph "External Integrations"
        NODERED[NODE-Red<br/>Workflow Automation]
        RABBITMQ[RabbitMQ<br/>Optional]
    end

    USER -->|speaks| CHROME
    USER -->|speaks| TRAY

    CHROME -->|WebSocket audio| WHISPER
    TRAY -->|WebSocket audio| WHISPER

    WHISPER -->|transcription| BACKEND
    WHISPER -->|bb publish| BB

    BACKEND -->|webhook| NODERED
    BACKEND -->|events| BB

    NODERED -->|response| BACKEND

    BACKEND -->|text| TTS
    TTS -->|audio| TRAY
    TTS -->|audio| CHROME

    BB -->|talkytonny.transcription.completed| TONNY
    TONNY -->|tonny.response.generated| BB
    BB -->|response event| BACKEND
```

**Role in Pipeline**: HeyMa serves as the voice interface layer for the 33GOD ecosystem, enabling hands-free interaction with agents and workflows. It bridges human speech to machine-actionable events and provides spoken feedback, creating a natural conversational interface for controlling the agent pipeline.

---

## Event Contracts

### Bloodbank Events Emitted

| Event Name | Routing Key | Payload Schema | Trigger Condition |
|------------|-------------|----------------|-------------------|
| `transcription.voice.completed` | `transcription.voice.completed` | `TranscriptionPayload` | When Whisper produces final transcription |
| `thread.tonny.prompt` | `thread.tonny.prompt` | `TonnyPromptPayload` | When transcription is sent to Tonny Agent |
| `thread.tonny.response` | `thread.tonny.response` | `TonnyResponsePayload` | When Tonny Agent generates response |
| `thread.tonny.speech_start` | `thread.tonny.speech_start` | `SpeechEventPayload` | When voice activity is detected |
| `thread.tonny.speech_end` | `thread.tonny.speech_end` | `SpeechEventPayload` | When voice activity ends |
| `thread.tonny.conversation.*` | `thread.tonny.conversation.{type}` | `ConversationEntryPayload` | On each conversation entry logged |

### Bloodbank Events Consumed

| Event Name | Routing Key | Handler | Purpose |
|------------|-------------|---------|---------|
| `tonny.response.generated` | `tonny.response.#` | `_handle_tonny_response()` | Receive AI response for TTS |
| `talkytonny.voice.captured` | `talkytonny.voice.#` | `_handle_audio_data()` | Process incoming audio data |

### Event Payload Schemas

**TranscriptionPayload** (emitted by WhisperLiveKit):
```json
{
  "text": "string - transcribed text",
  "timestamp": "ISO8601 datetime",
  "source": "whisperlivekit",
  "session_id": "UUID string",
  "target": "optional target service",
  "audio_metadata": {
    "duration": "float seconds",
    "sample_rate": "int Hz"
  },
  "context": "optional additional context"
}
```

**TonnyPromptPayload**:
```json
{
  "session_id": "UUID string",
  "text": "transcribed text",
  "timestamp": "float epoch",
  "context": [
    {
      "type": "transcription | response",
      "content": "string",
      "timestamp": "float epoch"
    }
  ]
}
```

---

## Non-Event Interfaces

### CLI Interface

**WhisperLiveKit Server:**
```bash
# Start transcription server with default settings
./scripts/start_server.sh

# Manual start with custom options
uv run whisperlivekit-server --port 8888 --model base --language en

# Stop the server
./scripts/stop_server.sh
```

**Client Utilities:**
```bash
# Auto-type transcription to active window
./bin/auto-type

# Connect to remote server
./bin/auto-type --remote whisper.delo.sh

# Send transcriptions to NODE-Red webhook
./bin/n8n-webhook --n8n-webhook https://nodered.delo.sh/webhook/transcription

# List available audio devices
./bin/auto-type --list-devices

# Test server connection
uv run python scripts/test_connection.py
```

**TonnyTray Integration CLI:**
```bash
# Start integration service
cd TonnyTray/backend
python main.py start

# Test all integrations
python main.py test

# Check health status
python main.py health

# Test audio pipeline
python main.py audio --mode voice_activation

# Test TTS
python main.py tts "Hello world" --voice "Antoni"

# Publish test event to RabbitMQ
python main.py publish "thread.tonny.test" '{"message": "test"}'
```

**Commands:**

| Command | Description | Arguments |
|---------|-------------|-----------|
| `whisperlivekit-server` | Start transcription server | `--port`, `--model`, `--language`, `--host`, `--ssl-certfile`, `--ssl-keyfile` |
| `./bin/auto-type` | Type transcriptions to active window | `--remote`, `--device`, `--list-devices` |
| `./bin/n8n-webhook` | Send to webhook | `--n8n-webhook`, `--remote` |
| `python main.py start` | Start integration orchestrator | `-c CONFIG`, `-l LOG_LEVEL` |
| `python main.py test` | Test all integrations | `-c CONFIG` |
| `python main.py health` | Check service health | `-c CONFIG` |
| `python main.py audio` | Test audio pipeline | `--mode`, `-i INPUT`, `-o OUTPUT` |
| `python main.py tts` | Test text-to-speech | `TEXT`, `--voice` |
| `python main.py publish` | Publish RabbitMQ event | `ROUTING_KEY`, `MESSAGE` |

### API Interface

**WhisperLiveKit Server:**

**Base URL**: `ws://localhost:8888` (WebSocket) / `http://localhost:8888` (HTTP)

| Endpoint | Method | Description | Request | Response |
|----------|--------|-------------|---------|----------|
| `/` | GET | Web UI for transcription | - | HTML page |
| `/asr` | WebSocket | Audio streaming endpoint | Binary audio chunks (16kHz PCM) | JSON transcription events |

**WebSocket Protocol (`/asr`):**

1. **Connection**: Connect to `ws://localhost:8888/asr`
2. **Handshake**: Server sends `session_info` with `session_id`
3. **Audio Streaming**: Client sends binary WebM audio chunks
4. **Transcription Events**: Server sends JSON responses

**WebSocket Message Types:**
```typescript
// Session info (sent on connect)
{ type: "session_info", session_id: "uuid" }

// Transcription update
{
  lines: Array<{ speaker: number, text: string, beg: number, end: number }>,
  buffer_transcription: string,
  buffer_diarization: string,
  remaining_time_transcription: number,
  remaining_time_diarization: number,
  status: "active_transcription" | "no_audio_detected"
}

// Ready to stop signal
{ type: "ready_to_stop" }
```

**TonnyTray Tauri IPC Commands:**

| Command | Description | Parameters | Returns |
|---------|-------------|------------|---------|
| `start_server` | Start WhisperLiveKit server | - | `Result<String, String>` |
| `stop_server` | Stop WhisperLiveKit server | - | `Result<String, String>` |
| `restart_server` | Restart WhisperLiveKit server | - | `Result<String, String>` |
| `start_recording` | Start audio capture client | - | `Result<String, String>` |
| `stop_recording` | Stop audio capture | - | `Result<String, String>` |
| `pause_recording` | Pause audio capture | - | `Result<String, String>` |
| `resume_recording` | Resume audio capture | - | `Result<String, String>` |
| `get_state` | Get application state | - | `Result<AppState, String>` |
| `get_settings` | Get current settings | - | `Result<AppSettings, String>` |
| `update_settings` | Update settings | `AppSettings` | `Result<String, String>` |
| `get_profiles` | List user profiles | - | `Result<Vec<UserProfile>, String>` |
| `switch_profile` | Change active profile | `id: String` | `Result<String, String>` |
| `list_audio_devices` | Get audio input devices | - | `Result<Vec<String>, String>` |
| `test_audio_device` | Test audio device | `device_id: String` | `Result<bool, String>` |
| `list_elevenlabs_voices` | Get available TTS voices | - | `Result<Vec<Voice>, String>` |
| `speak_text` | Generate and play TTS | `text: String` | `Result<String, String>` |
| `test_n8n_connection` | Test webhook connectivity | - | `Result<bool, String>` |
| `send_command` | Send command via webhook | `command, profile_id` | `Result<String, String>` |
| `get_logs` | Get log entries | `level, limit` | `Result<Vec<LogEntry>, String>` |
| `get_statistics` | Get database statistics | - | `Result<DatabaseStatistics, String>` |

---

## Technical Deep-Dive

### Technology Stack

- **WhisperLiveKit Server**:
  - Language: Python 3.10+
  - Framework: FastAPI with Uvicorn
  - Transcription: faster-whisper, torchaudio
  - WebSocket: websockets, fastapi WebSocket support
  - Audio Processing: librosa, soundfile, scipy
  - Event Publishing: aio-pika (RabbitMQ), custom bb CLI wrapper

- **TonnyTray Desktop App**:
  - Frontend: React 18, TypeScript 5.4, Vite 5.2
  - UI: Material-UI (MUI) 5.15
  - State: Zustand 4.5
  - Desktop: Tauri 2.x (Rust backend)
  - Audio: cpal + rodio (Rust)
  - Database: SQLite via rusqlite

- **Chrome Extension**:
  - Manifest V3
  - WebSocket client for WhisperLiveKit
  - Tab capture API for browser audio
  - MediaRecorder for audio encoding

- **Integration Backend**:
  - Language: Python 3.10+
  - HTTP Client: aiohttp, aio-pika
  - Audio: pyaudio, sounddevice
  - TTS: ElevenLabs API
  - Agent: letta-client (Tonny Agent)

### Architecture Pattern

HeyMa follows a **multi-component event-driven architecture** with three primary interfaces (Chrome Extension, Desktop App, CLI) all converging on a central transcription server:

```
                    ┌─────────────────┐
                    │  User Interface │
                    └────────┬────────┘
                             │
         ┌───────────────────┼───────────────────┐
         │                   │                   │
    ┌────▼────┐        ┌─────▼─────┐       ┌─────▼─────┐
    │ Chrome  │        │ TonnyTray │       │   CLI     │
    │Extension│        │  (Tauri)  │       │  Scripts  │
    └────┬────┘        └─────┬─────┘       └─────┬─────┘
         │                   │                   │
         └───────────────────┼───────────────────┘
                             │ WebSocket (audio)
                    ┌────────▼────────┐
                    │ WhisperLiveKit  │
                    │     Server      │
                    └────────┬────────┘
                             │
         ┌───────────────────┼───────────────────┐
         │                   │                   │
    ┌────▼────┐        ┌─────▼─────┐       ┌─────▼─────┐
    │  bb CLI │        │ NODE-Red  │       │  SQLite   │
    │Bloodbank│        │  Webhook  │       │ WAL/Store │
    └─────────┘        └─────┬─────┘       └───────────┘
                             │
                    ┌────────▼────────┐
                    │   Tonny Agent   │
                    │    (Letta)      │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │   ElevenLabs    │
                    │      TTS        │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │   Audio Output  │
                    └─────────────────┘
```

**Key Patterns:**

1. **WebSocket Streaming**: Real-time bidirectional audio/transcription flow
2. **Event Sourcing**: All conversation entries logged to SQLite with WAL
3. **Circuit Breaker**: Resilient external service calls (RabbitMQ, ElevenLabs)
4. **Offline Queue**: Events persisted locally when Bloodbank unavailable
5. **State Machine**: Explicit system states (INITIALIZING, READY, LISTENING, PROCESSING, SPEAKING, ERROR)

### Key Implementation Details

**WhisperLiveKit Transcription Flow:**

1. Client connects via WebSocket to `/asr` endpoint
2. Server generates unique `session_id` and sends to client
3. Client streams binary audio chunks (WebM format)
4. Server processes audio through Whisper model
5. Partial and final transcriptions sent back as JSON
6. Final transcriptions published to Bloodbank via `bb` CLI

**Bloodbank Integration (via bb CLI):**

The `BloodbankPublisher` class handles event publishing with durability:

```python
# Publish flow:
1. Write event to WAL (raw_voice_ingest.jsonl) for durability
2. Check if `bb` CLI is available
3. Publish via: bb publish transcription.voice.completed --json -
4. On failure: retry with exponential backoff (max 3 attempts)
5. Failed events remain in WAL for later replay
```

**TonnyTray Rust Backend Modules:**

| Module | Purpose |
|--------|---------|
| `lib.rs` | Main entry, IPC command handlers, AppContext |
| `state.rs` | Application state (AppState, ServerStatus, TrayState) |
| `process_manager.rs` | WhisperLiveKit server lifecycle management |
| `audio.rs` | Audio device enumeration and playback |
| `websocket.rs` | N8n/NODE-Red webhook client |
| `elevenlabs.rs` | ElevenLabs TTS integration |
| `config.rs` | Configuration file management |
| `database.rs` | SQLite persistence (profiles, logs, transcriptions) |
| `keychain.rs` | Secure credential storage |
| `events.rs` | Event payload definitions |
| `tray.rs` | System tray menu and events |

**Voice Activity Detection:**

```rust
// TrayState transitions
Disabled  → Idle       // Server starts
Idle      → Listening  // Recording starts + voice detected
Listening → Processing // Transcribing
Processing → Idle      // Transcription complete
Any       → Error      // Service failure
```

### Data Models

**AppState (Rust/TypeScript):**
```typescript
interface AppState {
  recording: boolean;
  server_status: ServerStatus;           // "stopped" | "starting" | "running" | "stopping" | { error: string }
  autotype_status: ServerStatus;
  last_transcription: string;
  active_profile: UserProfile;
  settings: AppSettings;
  tray_state: TrayState;                 // "idle" | "listening" | "processing" | "error" | "disabled"
  transcription_history: TranscriptionEntry[];
  server_pid: number | null;
  autotype_pid: number | null;
}
```

**AppSettings:**
```typescript
interface AppSettings {
  // Server configuration
  server_url: string;                    // "ws://localhost:8888/asr"
  model: string;                         // "tiny" | "base" | "small" | "medium" | "large-v3"
  language: string;                      // ISO language code
  auto_start: boolean;
  auto_restart: boolean;
  port: number;

  // Webhook integration
  n8n_webhook_url: string;
  n8n_enabled: boolean;

  // ElevenLabs TTS
  elevenlabs_api_key: string;
  elevenlabs_voice_id: string;
  elevenlabs_enabled: boolean;
  response_mode: "text_only" | "voice_only" | "both";

  // Audio settings
  microphone_device: string | null;
  push_to_talk: boolean;
  voice_activation: boolean;
  voice_activation_threshold: number;    // 0.0 - 1.0

  // Typing behavior
  auto_typing_enabled: boolean;
  typing_speed: number;                  // chars per second

  // Advanced
  command_prefix: string;                // e.g., "Computer,"
  confirmation_mode: "silent" | "visual" | "audio";
}
```

**TranscriptionEntry:**
```typescript
interface TranscriptionEntry {
  timestamp: string;                     // ISO8601 datetime
  text: string;
  success: boolean;
  response: string | null;
}
```

**UserProfile:**
```typescript
interface UserProfile {
  name: string;
  permissions: string;                   // "admin" | "user" | "kid" | "guest"
  voice_id: string | null;               // ElevenLabs voice override
  allowed_commands: string[];
}
```

### Configuration

**Configuration File Location:**
- **TonnyTray config**: `~/.config/tonnytray/config.json`
- **TonnyTray database**: `~/.local/share/tonnytray/tonnytray.db`
- **TonnyTray logs**: `~/.config/tonnytray/logs/tonnytray.log`
- **Integration config**: `TonnyTray/backend/config.json` (optional)
- **ElevenLabs cache**: `TonnyTray/backend/elevenlabs_cache/`
- **WAL file**: `raw_voice_ingest.jsonl` (in working directory)

**Environment Variables:**
```bash
# ElevenLabs
ELEVENLABS_API_KEY=sk-...

# NODE-Red webhook
N8N_WEBHOOK_URL=https://nodered.delo.sh/webhook/transcription

# RabbitMQ (optional)
RABBITMQ_URL=amqp://guest:guest@localhost/

# Whisper model
WHISPER_MODEL=base
WHISPER_LANGUAGE=en
```

**Sample config.json:**
```json
{
  "server": {
    "url": "ws://localhost:8888/asr",
    "port": 8888,
    "model": "base",
    "language": "en",
    "autoStart": true,
    "autoRestart": true
  },
  "integration": {
    "n8nWebhookUrl": "https://nodered.delo.sh/webhook/transcription",
    "n8nEnabled": true
  },
  "elevenlabs": {
    "apiKey": "",
    "voiceId": "Antoni",
    "enabled": false
  },
  "audio": {
    "inputDevice": null,
    "voiceActivation": true,
    "voiceActivationThreshold": 0.02,
    "pushToTalk": false
  },
  "behavior": {
    "autoTypingEnabled": true,
    "typingSpeed": 50,
    "commandPrefix": "Computer,",
    "confirmationMode": "visual"
  },
  "profiles": [
    {
      "name": "Default",
      "permissions": "admin",
      "voiceId": null,
      "allowedCommands": []
    }
  ]
}
```

---

## Development

### Setup

```bash
# Clone repository
git clone --recurse-submodules https://github.com/delorenj/33GOD.git
cd 33GOD/HeyMa

# Install Python dependencies (WhisperLiveKit)
curl -LsSf https://astral.sh/uv/install.sh | sh  # Install uv if needed
uv sync

# Install Node dependencies (TonnyTray frontend)
cd TonnyTray
npm install

# Install Rust dependencies (TonnyTray backend)
cd src-tauri
cargo build

# Install Chrome extension
# 1. Navigate to chrome://extensions/
# 2. Enable "Developer mode"
# 3. Click "Load unpacked"
# 4. Select the chrome-extension/ directory
```

### Running Locally

```bash
# Terminal 1: Start WhisperLiveKit server
cd HeyMa
./scripts/start_server.sh
# Or: uv run whisperlivekit-server --port 8888

# Terminal 2: Start TonnyTray desktop app
cd HeyMa/TonnyTray
npm run tauri:dev

# Alternative: Run CLI client only
cd HeyMa
./bin/auto-type
```

### Testing

```bash
# WhisperLiveKit tests
uv run python scripts/test_connection.py
uv run python scripts/debug_client.py

# TonnyTray frontend tests
cd TonnyTray
npm run test              # Vitest unit tests
npm run test:ui           # Vitest with UI
npm run test:coverage     # Coverage report
npm run test:integration  # Integration tests
npm run test:e2e          # Playwright E2E tests

# TonnyTray Rust backend tests
cd TonnyTray/src-tauri
cargo test
cargo test test_process_manager  # Single test
cargo clippy                      # Linting
cargo fmt                         # Formatting

# Run all tests
cd TonnyTray
npm run test:all          # Rust + TypeScript + E2E

# Security audit
npm run security-audit    # npm + cargo audit

# Benchmarks
npm run bench             # Rust performance benchmarks
```

### Working with Tests

```bash
# Run specific frontend test
npm run test -- src/components/Common/ConfirmDialog.test.tsx

# Run Rust test by name
cargo test test_config_loading

# E2E test by file
npm run test:e2e -- e2e/workflows.spec.ts

# E2E with visible browser
npm run test:e2e:headed
```

---

## Deployment

### Docker Deployment (WhisperLiveKit)

```bash
# Build and start with Docker Compose
cd HeyMa
docker-compose up -d

# GPU support
docker-compose -f docker-compose.yml up -d

# CPU only
docker build -f Dockerfile.cpu -t talkytonny-cpu .
docker run -p 8888:8888 talkytonny-cpu
```

**Docker Compose Configuration:**
```yaml
services:
  whisper:
    build: .
    ports:
      - "8888:8888"
    environment:
      - WHISPER_MODEL=base
      - WHISPER_LANGUAGE=en
    volumes:
      - ./models:/app/models
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.whisper.rule=Host(`whisper.delo.sh`)"
```

### TonnyTray Desktop Build

```bash
cd TonnyTray

# Development build
npm run tauri:dev

# Production build
npm run tauri:build

# Build outputs:
# - Linux: target/release/bundle/deb/*.deb
#          target/release/bundle/appimage/*.AppImage
# - macOS: target/release/bundle/dmg/*.dmg (planned)
# - Windows: target/release/bundle/msi/*.msi (planned)
```

### Production Considerations

1. **SSL/TLS**: Use `--ssl-certfile` and `--ssl-keyfile` for secure WebSocket connections
2. **Reverse Proxy**: Traefik labels configured for `whisper.delo.sh`
3. **Model Selection**: Larger models (medium, large-v3) require more VRAM
4. **Auto-restart**: Enable `auto_restart` in settings for crash recovery
5. **Credential Storage**: Use system keychain for API keys in production
6. **WAL Replay**: Run `BloodbankPublisher.replay_wal()` after downtime

### Platform Support

| Platform | Status | Notes |
|----------|--------|-------|
| Linux (X11/Wayland) | Fully Supported | Primary development platform |
| macOS | Planned | WKWebView, Keychain Access |
| Windows | Planned | WebView2, Windows Credential Manager |

---

## Component Interactions

### Voice Command Flow

```mermaid
sequenceDiagram
    participant User
    participant TonnyTray as TonnyTray/Chrome
    participant Whisper as WhisperLiveKit
    participant BB as Bloodbank
    participant NR as NODE-Red
    participant Tonny as Tonny Agent
    participant 11 as ElevenLabs

    User->>TonnyTray: Speaks command
    TonnyTray->>Whisper: Audio stream (WebSocket)
    Whisper->>Whisper: Transcribe with Whisper
    Whisper->>TonnyTray: Transcription JSON
    Whisper->>BB: transcription.voice.completed

    TonnyTray->>NR: POST /webhook/transcription
    NR->>NR: Process workflow
    NR->>TonnyTray: Response JSON

    BB->>Tonny: Event delivery
    Tonny->>Tonny: Process via Letta
    Tonny->>BB: tonny.response.generated
    BB->>TonnyTray: Event delivery

    TonnyTray->>11: POST /text-to-speech
    11->>TonnyTray: Audio stream
    TonnyTray->>User: Plays response audio
```

### Component Dependencies

```
HeyMa
├── WhisperLiveKit (Python)
│   ├── faster-whisper (transcription)
│   ├── torch/torchaudio (audio processing)
│   ├── fastapi/uvicorn (server)
│   └── aio-pika (RabbitMQ, optional)
│
├── TonnyTray (Tauri)
│   ├── Frontend (React/TypeScript)
│   │   ├── @mui/material (UI)
│   │   ├── zustand (state)
│   │   └── @tanstack/react-query (data fetching)
│   │
│   └── Backend (Rust)
│       ├── tauri (desktop framework)
│       ├── tokio (async runtime)
│       ├── rusqlite (database)
│       ├── cpal/rodio (audio)
│       └── reqwest (HTTP client)
│
├── Chrome Extension
│   ├── Manifest V3
│   ├── tabCapture API
│   └── WebSocket client
│
└── Integration Backend (Python)
    ├── aiohttp (HTTP client)
    ├── elevenlabs (TTS)
    ├── letta-client (Tonny Agent)
    └── aio-pika (RabbitMQ)
```

---

## Troubleshooting

### Common Issues

**"Command not found: uv"**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**WhisperLiveKit Connection Failed**
```bash
# Check if server is running
ps aux | grep whisper

# Test connection
uv run python scripts/test_connection.py

# Check port availability
lsof -i :8888

# View server logs
tail -f whisper.log
```

**Audio Device Not Found**
```bash
# List available devices
./bin/auto-type --list-devices

# Select specific device
./bin/auto-type --device 6

# Check audio permissions
pactl info  # PulseAudio
wpctl status  # PipeWire
```

**TTS Not Working**
1. Verify `ELEVENLABS_API_KEY` is set
2. Test connection: `python main.py tts "test" --voice Antoni`
3. Check quota: ElevenLabs dashboard
4. Verify voice ID is valid

**Bloodbank Events Not Publishing**
1. Check `bb` CLI is installed: `which bb`
2. Verify RabbitMQ is running
3. Check WAL file for queued events: `cat raw_voice_ingest.jsonl`
4. Replay WAL: `python -c "from whisperlivekit.bloodbank_publisher import BloodbankPublisher; import asyncio; p = BloodbankPublisher(); asyncio.run(p.replay_wal())"`

---

## References

- **Domain Doc**: `docs/domains/dashboards-voice/GOD.md`
- **System Doc**: `docs/GOD.md`
- **Source**: `HeyMa/`
- **WhisperLiveKit Upstream**: https://github.com/QuentinFuxa/WhisperLiveKit
- **Tauri Documentation**: https://tauri.app/
- **ElevenLabs API**: https://docs.elevenlabs.io/
- **Letta (Tonny Agent)**: https://docs.letta.com/

---

## Changelog

| Date | Version | Changes |
|------|---------|---------|
| 2026-02-02 | 1.0.0 | Initial GOD document created |
