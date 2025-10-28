# TonnyTray Backend Architecture

## Overview

TonnyTray is a Rust-based Tauri application that provides a system tray interface for controlling WhisperLiveKit voice transcription services, n8n home automation integration, and ElevenLabs text-to-speech functionality.

## Technology Stack

- **Framework**: Tauri 1.5 (Rust backend + Web frontend)
- **Language**: Rust 1.75+ (2021 edition)
- **Async Runtime**: Tokio 1.35
- **Audio**: rodio (playback), cpal (recording)
- **WebSocket**: tokio-tungstenite
- **HTTP Client**: reqwest
- **Serialization**: serde + serde_json
- **Process Management**: sysinfo, nix
- **Configuration**: JSON files

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                   Tauri Frontend (React)                 │
│                  (Settings, Dashboard, UI)               │
└────────────────────┬────────────────────────────────────┘
                     │ IPC Commands
┌────────────────────┴────────────────────────────────────┐
│                    main.rs (Entry Point)                 │
│           ┌──────────────────────────────────┐          │
│           │      AppContext (State)           │          │
│           │  - SharedState                    │          │
│           │  - ProcessManager                 │          │
│           │  - AudioManager                   │          │
│           │  - ElevenLabsManager              │          │
│           │  - N8nClient                      │          │
│           └──────────────────────────────────┘          │
└─────────────────────────────────────────────────────────┘
         │          │          │          │          │
         ▼          ▼          ▼          ▼          ▼
┌──────────┐ ┌──────────┐ ┌────────┐ ┌─────────┐ ┌──────┐
│ Process  │ │  System  │ │ Audio  │ │  WebSoc │ │ Eleven│
│ Manager  │ │   Tray   │ │Manager │ │  Client │ │ Labs │
└──────────┘ └──────────┘ └────────┘ └─────────┘ └──────┘
     │             │            │           │          │
     ▼             ▼            ▼           ▼          ▼
┌──────────────────────────────────────────────────────┐
│              External Services & Processes            │
│  - WhisperLiveKit Server (Python subprocess)         │
│  - Auto-Type Client (Python subprocess)              │
│  - n8n Webhook API                                   │
│  - ElevenLabs TTS API                                │
└──────────────────────────────────────────────────────┘
```

## Core Modules

### 1. state.rs - Application State Management

**Purpose**: Centralized state management using Arc<Mutex<AppState>>

**Key Types**:
- `AppState`: Main application state
  - `recording`: bool - Recording status
  - `server_status`: ServerStatus - WhisperLiveKit server state
  - `autotype_status`: ServerStatus - Auto-type client state
  - `last_transcription`: String - Most recent transcription
  - `active_profile`: UserProfile - Current user profile
  - `settings`: AppSettings - Application settings
  - `tray_state`: TrayState - System tray icon state
  - `transcription_history`: Vec<TranscriptionEntry> - History
  - `server_pid`: Option<u32> - Server process ID
  - `autotype_pid`: Option<u32> - Auto-type process ID

- `ServerStatus`: Enum for process states
  - Stopped, Starting, Running, Stopping, Error(String)

- `TrayState`: Enum for tray icon states
  - Idle (Gray), Listening (Blue), Processing (Yellow), Error (Red), Disabled

- `AppSettings`: Configuration settings
  - Server, n8n, ElevenLabs, Audio, Typing behavior

**Thread Safety**:
- Uses `Arc<Mutex<AppState>>` for thread-safe shared state
- Helper function `with_state()` for safe state access

### 2. process_manager.rs - Process Control

**Purpose**: Manage WhisperLiveKit server and auto-type client processes

**Key Functionality**:
- `start_whisper_server()`: Spawn WhisperLiveKit server subprocess
  - Uses `uv run whisperlivekit-server` command
  - Configures model, language, port from settings
  - Waits for server to be ready (TCP health check)
  - Stores PID for process tracking

- `stop_whisper_server()`: Gracefully stop server
  - Sends SIGTERM, falls back to SIGKILL if needed
  - Cleans up process handle and state

- `start_autotype_client()`: Start auto-type client
  - Runs `python3 auto_type_client.py`
  - Adds `--send-to-n8n` flag if enabled
  - Manages stdin/stdout/stderr

- `stop_autotype_client()`: Stop auto-type client
  - Similar graceful shutdown with SIGTERM/SIGKILL

- `monitor_processes()`: Background task
  - Checks process health every 5 seconds
  - Auto-restarts if `auto_restart` setting enabled
  - Updates state on crashes

**Process Management**:
- Uses `std::process::Command` for spawning
- `nix` crate for Unix signal handling (SIGTERM, SIGKILL)
- `sysinfo` for process status checking

### 3. config.rs - Configuration Management

**Purpose**: Load/save configuration from JSON files

**Configuration Structure**:
```json
{
  "server": {
    "url": "ws://localhost:8888/asr",
    "model": "base",
    "language": "en",
    "auto_start": true,
    "port": 8888,
    "auto_restart": true
  },
  "n8n": {
    "webhook_url": "https://n8n.delo.sh/webhook/ask-tonny",
    "enabled": false
  },
  "elevenlabs": {
    "api_key": "***",
    "voice_id": "21m00Tcm4TlvDq8ikWAM",
    "enabled": false,
    "response_mode": "text_only"
  },
  "profiles": [...],
  "audio": {...},
  "typing": {...},
  "advanced": {...}
}
```

**Location**: `~/.config/tonnytray/config.json` (Linux)

**Key Functions**:
- `load_or_create_config()`: Load existing or create default
- `Config::save()`: Persist to disk
- `to_app_settings()` / `from_app_settings()`: Convert to/from runtime state

### 4. tray.rs - System Tray Integration

**Purpose**: System tray icon, menu, and event handling

**Menu Structure**:
```
🎤 WhisperLiveKit
├── 🔴 Start Recording (Ctrl+Shift+V)
├── ⏸️  Pause Recording
├── ───────────────
├── 📊 Status: Connected
├── 🔊 Last: "Turn on living room lights"
├── ───────────────
├── ⚙️  Settings
├── 📝 View Logs
├── 🔄 Restart Service
├── ───────────────
└── 🚪 Quit
```

**Key Functions**:
- `build_tray_menu()`: Construct system tray menu
- `handle_tray_event()`: Handle clicks and menu selections
- `update_tray_menu()`: Refresh menu based on state
- `update_tray_icon()`: Change icon based on TrayState

**Icon States**:
- Idle: Gray (server running, not recording)
- Listening: Blue pulse (actively recording)
- Processing: Yellow (transcribing audio)
- Error: Red (service error)
- Disabled: Gray strikethrough (stopped)

### 5. audio.rs - Audio Management

**Purpose**: Audio recording (cpal) and playback (rodio)

**Key Functionality**:
- `list_input_devices()`: Enumerate microphones
- `list_output_devices()`: Enumerate speakers
- `set_input_device()`: Select microphone
- `start_recording()`: Begin audio capture
  - Callback-based: `FnMut(&[f32])`
  - Handles sample format conversion (f32, i16, u16)
  - Real-time audio processing

- `stop_recording()`: End capture
- `play_audio()`: Play audio bytes (MP3, WAV, etc.)
  - Uses rodio::Decoder for format handling
  - Non-blocking playback with sink.detach()

- `calculate_audio_level()`: RMS level calculation
- `is_voice_detected()`: Voice activation detection

**Architecture**:
- Uses cpal for low-level audio I/O
- Uses rodio for high-level playback
- Thread-safe with Arc<Mutex<Stream>>

### 6. websocket.rs - n8n Integration

**Purpose**: WebSocket and HTTP communication with n8n

**Components**:

**WebSocketClient**:
- Bidirectional WebSocket connection
- `connect()` / `disconnect()`
- `send_message()` / `receive_message()`
- `listen()`: Continuous message loop
- Parses `N8nResponse` JSON structure

**N8nClient**:
- HTTP webhook client
- `send_transcription()`: POST transcription to n8n
  - Payload: `{ timestamp, text, source }`
  - Returns: `N8nResponse`
- `test_connection()`: Health check

**N8nResponse Structure**:
```rust
{
  success: bool,
  message: Option<String>,
  action: Option<String>,
  data: Option<Value>
}
```

### 7. elevenlabs.rs - Text-to-Speech

**Purpose**: ElevenLabs API integration for TTS

**Key Components**:

**ElevenLabsClient**:
- Low-level API client
- `list_voices()`: Get available voices
- `text_to_speech()`: Convert text to audio bytes
  - Model: "eleven_monolingual_v1"
  - Returns: Vec<u8> (MP3 audio)
- `text_to_speech_stream()`: Streaming TTS
- `get_voice()`: Voice metadata

**ElevenLabsManager**:
- High-level manager
- `initialize()`: Set API key and default voice
- `speak()`: Quick TTS with default voice
- `list_voices()`: Cached voice list
- `test_connection()`: API health check

**VoiceSettings**:
```rust
{
  stability: 0.5,
  similarity_boost: 0.75,
  style: 0.0,
  use_speaker_boost: true
}
```

### 8. main.rs - Application Entry Point

**Purpose**: Tauri app initialization and IPC command definitions

**IPC Commands** (Frontend ↔ Backend):

**Server Control**:
- `start_server()`: Start WhisperLiveKit server
- `stop_server()`: Stop server
- `restart_server()`: Restart server
- `get_server_status()`: Get current status

**Recording Control**:
- `start_recording()`: Start auto-type client
- `stop_recording()`: Stop recording

**State Management**:
- `get_state()`: Get full AppState
- `get_settings()`: Get AppSettings
- `update_settings()`: Update and persist settings
- `get_transcription_history()`: Get history

**Audio**:
- `list_audio_devices()`: Enumerate microphones

**n8n**:
- `test_n8n_connection()`: Test webhook

**ElevenLabs**:
- `list_elevenlabs_voices()`: Get voice list
- `test_elevenlabs_connection()`: Test API
- `speak_text()`: Convert text to speech and play

**Setup Flow**:
1. Load configuration from JSON
2. Initialize state with settings
3. Create managers (ProcessManager, AudioManager, etc.)
4. Initialize ElevenLabs if enabled
5. Build Tauri app with system tray
6. Auto-start server if configured
7. Start process monitor background task

## Data Flow

### Transcription Flow

```
User speaks into mic
      ↓
Auto-Type Client (Python)
      ↓ (captures audio)
WhisperLiveKit Server
      ↓ (transcribes)
Auto-Type Client receives text
      ├─→ Types into active window (ydotool/wtype/xdotool)
      └─→ Sends to n8n webhook (if enabled)
            ↓
      n8n processes command
            ↓
      n8n returns response
            ↓
      TonnyTray receives response
            ├─→ Updates transcription history
            ├─→ Sends to ElevenLabs (if voice enabled)
            └─→ Plays audio response
```

### State Update Flow

```
IPC Command from Frontend
      ↓
Tauri Command Handler
      ↓
Lock SharedState (Mutex)
      ↓
Modify AppState
      ↓
Update System Tray Menu
      ↓
Update Tray Icon
      ↓
Emit Event to Frontend (optional)
      ↓
Frontend React UI updates
```

## Thread Safety

**Synchronization Primitives**:
- `Arc<Mutex<T>>`: Shared state across threads
- `Arc<TokioMutex<T>>`: Async-safe managers
- All state mutations protected by locks

**Lock Hierarchy** (to prevent deadlocks):
1. AppState (SharedState)
2. Individual managers (ProcessManager, AudioManager, etc.)
3. Never hold multiple locks simultaneously

**Async/Sync Boundaries**:
- Main thread: Tauri event loop (sync)
- Background tasks: Tokio runtime (async)
- IPC commands: Async handlers
- Process spawning: Sync with async wrappers

## Error Handling

**Strategy**:
- Use `anyhow::Result<T>` for internal errors
- Return `Result<T, String>` for IPC commands (JSON serializable)
- Log errors with `log` crate (info, warn, error, debug)
- Update state with ServerStatus::Error on failures

**Recovery**:
- Auto-restart processes if configured
- Graceful degradation (e.g., clipboard fallback if typing fails)
- User notifications via system tray

## Configuration Management

**Persistence**:
- Settings saved to `~/.config/tonnytray/config.json`
- Auto-save on settings changes
- Reload on app restart

**Defaults**:
- Sensible defaults in `Config::default()`
- Creates config on first run

## Security Considerations

**API Keys**:
- ElevenLabs API key stored in config file
- Should be migrated to system keychain (future enhancement)

**Process Isolation**:
- WhisperLiveKit and auto-type run as separate processes
- Limited IPC surface area

**Input Validation**:
- Validate all IPC command inputs
- Sanitize file paths and URLs

## Performance Characteristics

**Memory**:
- Minimal state: ~1-2 MB
- Audio buffers: Streaming, not stored
- Transcription history: Limited to 100 entries

**CPU**:
- Background monitor: 5-second poll interval
- Audio recording: Real-time callback
- No heavy processing in main thread

**Latency**:
- IPC commands: <10ms
- Server startup: 2-15 seconds
- Voice activation: <50ms
- TTS + playback: 1-3 seconds

## Testing Strategy

**Unit Tests**:
- State management tests
- Config serialization tests
- Audio level calculations
- Voice detection logic

**Integration Tests**:
- Process spawning (mocked)
- API clients (mocked)
- State updates + tray menu sync

**Manual Testing**:
- Full end-to-end flows
- System tray interactions
- Multi-user scenarios

## Future Enhancements

1. **Keychain Integration**: Store API keys securely
2. **Hotkey Support**: Global shortcuts for start/stop
3. **Multi-language Support**: i18n for UI strings
4. **Voice Biometrics**: Automatic user identification
5. **Offline Mode**: Basic commands without internet
6. **Metrics Dashboard**: Real-time stats in UI
7. **Plugin System**: Extensible command handlers
8. **Docker Support**: Containerized deployment

## Build & Development

**Prerequisites**:
- Rust 1.75+
- Node.js 18+ (for frontend)
- Tauri CLI: `cargo install tauri-cli`
- System dependencies: libwebkit2gtk, libgtk3 (Linux)

**Build Commands**:
```bash
# Development
cargo tauri dev

# Release build
cargo tauri build

# Run tests
cargo test

# Check format
cargo fmt --check

# Lint
cargo clippy
```

**Project Structure**:
```
TonnyTray/
├── src-tauri/          # Rust backend
│   ├── src/
│   │   ├── main.rs           # Entry point
│   │   ├── state.rs          # State management
│   │   ├── process_manager.rs # Process control
│   │   ├── tray.rs           # System tray
│   │   ├── config.rs         # Configuration
│   │   ├── audio.rs          # Audio I/O
│   │   ├── websocket.rs      # n8n integration
│   │   └── elevenlabs.rs     # TTS client
│   ├── Cargo.toml      # Rust dependencies
│   ├── tauri.conf.json # Tauri configuration
│   └── build.rs        # Build script
├── src/                # React frontend
└── ARCHITECTURE.md     # This document
```

## Deployment

**Distribution**:
- AppImage (Linux)
- DMG (macOS)
- MSI (Windows)

**Installation**:
- Auto-update support (future)
- First-run wizard for configuration
- System startup integration

**Monitoring**:
- Logs: `~/.config/tonnytray/logs/`
- Crash reports: Local only (no telemetry)

## References

- Tauri: https://tauri.app/
- WhisperLiveKit: ../README.md
- n8n: https://n8n.io/
- ElevenLabs: https://elevenlabs.io/
- PRD: ../TRAY_APP_PRD.md
