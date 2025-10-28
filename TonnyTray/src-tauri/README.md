# TonnyTray Backend - Rust Implementation

Rust-based Tauri backend for the TonnyTray system tray application.

## Quick Start

```bash
# Build
cargo build

# Run tests
cargo test

# Development (with Tauri)
cargo tauri dev

# Production build
cargo tauri build
```

## Architecture

See [../ARCHITECTURE.md](../ARCHITECTURE.md) for complete documentation.

## Module Overview

### Core Modules

- **main.rs**: Application entry point, IPC command handlers
- **state.rs**: Thread-safe application state management
- **process_manager.rs**: WhisperLiveKit and auto-type process control
- **tray.rs**: System tray menu and icon management
- **config.rs**: JSON configuration loading and persistence
- **audio.rs**: Audio recording (cpal) and playback (rodio)
- **websocket.rs**: n8n WebSocket and HTTP client
- **elevenlabs.rs**: ElevenLabs TTS API client

## Dependencies

Key dependencies (see Cargo.toml for complete list):

- `tauri`: Desktop application framework
- `tokio`: Async runtime
- `serde`: Serialization
- `reqwest`: HTTP client
- `tokio-tungstenite`: WebSocket client
- `rodio`: Audio playback
- `cpal`: Audio recording
- `sysinfo`: Process monitoring
- `nix`: Unix signals
- `anyhow`: Error handling

## IPC Commands

Commands exposed to the frontend via Tauri:

### Server Control
- `start_server()` → `Result<String, String>`
- `stop_server()` → `Result<String, String>`
- `restart_server()` → `Result<String, String>`
- `get_server_status()` → `Result<ServerStatus, String>`

### Recording Control
- `start_recording()` → `Result<String, String>`
- `stop_recording()` → `Result<String, String>`

### State Management
- `get_state()` → `Result<AppState, String>`
- `get_settings()` → `Result<AppSettings, String>`
- `update_settings(settings: AppSettings)` → `Result<String, String>`
- `get_transcription_history()` → `Result<Vec<TranscriptionEntry>, String>`

### Audio
- `list_audio_devices()` → `Result<Vec<String>, String>`

### n8n Integration
- `test_n8n_connection()` → `Result<bool, String>`

### ElevenLabs
- `list_elevenlabs_voices()` → `Result<Vec<Voice>, String>`
- `test_elevenlabs_connection()` → `Result<bool, String>`
- `speak_text(text: String)` → `Result<String, String>`

## Configuration

Configuration file: `~/.config/tonnytray/config.json`

Example:

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
    "enabled": true
  },
  "elevenlabs": {
    "api_key": "your_key_here",
    "voice_id": "21m00Tcm4TlvDq8ikWAM",
    "enabled": true,
    "response_mode": "both"
  }
}
```

## Testing

```bash
# Run all tests
cargo test

# Run specific test
cargo test test_process_manager

# Run with logging
RUST_LOG=debug cargo test -- --nocapture

# Run tests in specific module
cargo test process_manager::tests
```

## Logging

Set log level via environment variable:

```bash
RUST_LOG=debug cargo run
RUST_LOG=info cargo run
RUST_LOG=warn cargo run
```

## Building

### Debug Build
```bash
cargo build
```

### Release Build (Optimized)
```bash
cargo build --release
```

### Tauri Bundle
```bash
cargo tauri build
```

Output: `target/release/bundle/`

## Code Organization

```
src-tauri/
├── src/
│   ├── main.rs              # Entry point, IPC handlers
│   ├── lib.rs               # Module re-exports
│   ├── state.rs             # State management
│   ├── process_manager.rs   # Process control
│   ├── tray.rs              # System tray
│   ├── config.rs            # Configuration
│   ├── audio.rs             # Audio I/O
│   ├── websocket.rs         # WebSocket client
│   └── elevenlabs.rs        # TTS client
├── Cargo.toml               # Dependencies
├── tauri.conf.json          # Tauri configuration
└── build.rs                 # Build script
```

## Performance Characteristics

- Startup time: ~1-2 seconds
- Memory (idle): ~10-20 MB
- CPU (idle): <1%
- Audio latency: <50ms
- IPC command latency: <10ms

## Error Handling

- Internal errors use `anyhow::Result<T>`
- IPC commands return `Result<T, String>` (JSON serializable)
- All errors logged with `log` crate
- State updated with `ServerStatus::Error` on failures

## Thread Safety

All state protected by:
- `Arc<Mutex<AppState>>` for shared state
- `Arc<TokioMutex<T>>` for async managers
- No shared mutable state without locks

## Platform Support

- **Linux**: Fully supported (Wayland + X11)
- **macOS**: Planned
- **Windows**: Planned

## Troubleshooting

### Build Fails

1. Install system dependencies:
```bash
sudo apt install libwebkit2gtk-4.0-dev build-essential curl wget libssl-dev libgtk-3-dev
```

2. Update Rust:
```bash
rustup update
```

### Server Won't Start

Check WhisperLiveKit installation:
```bash
cd ../../
uv run whisperlivekit-server --help
```

### Process Manager Issues

View process list:
```bash
ps aux | grep whisper
```

Kill stuck processes:
```bash
pkill -f whisperlivekit
```

## Development Tips

1. **Use RUST_LOG for debugging**:
   ```bash
   RUST_LOG=tonnytray=debug cargo run
   ```

2. **Run tests in watch mode**:
   ```bash
   cargo watch -x test
   ```

3. **Check for common mistakes**:
   ```bash
   cargo clippy
   ```

4. **Format code**:
   ```bash
   cargo fmt
   ```

5. **Check dependencies**:
   ```bash
   cargo tree
   cargo outdated
   ```

## Security Considerations

- API keys stored in plaintext config (TODO: keychain integration)
- Subprocess isolation for external processes
- No network access without user configuration
- Local-only operation by default

## License

MIT - See [LICENSE](../../LICENSE)
