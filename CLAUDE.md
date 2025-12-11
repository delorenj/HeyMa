# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**TalkyTonny** is a voice-controlled AI assistant system with three main components:

1. **WhisperLiveKit** - Real-time speech-to-text transcription server (Python/FastAPI/WebSocket)
2. **TonnyTray** - System tray desktop application (Tauri/Rust + React/TypeScript)
3. **Chrome Extension** - Browser-based transcription client

The system allows users to speak commands that are transcribed via Whisper, processed through n8n workflows, and responded to via ElevenLabs TTS.

## Architecture

### Multi-Component Structure

```
TalkyTonny/
├── whisperlivekit/        # Python transcription server
├── TonnyTray/             # Desktop app (Tauri + React)
│   ├── src/               # React frontend (TypeScript)
│   └── src-tauri/         # Rust backend
├── chrome-extension/      # Browser extension
├── scripts/               # Python client scripts
└── bin/                   # Convenience wrappers
```

### Component Communication Flow

```
User Speech → WhisperLiveKit (WebSocket) → TonnyTray/Clients
                                          ↓
                                    n8n Webhook
                                          ↓
                                    ElevenLabs TTS → Audio Output
```

## Common Development Commands

### WhisperLiveKit Server (Python)

```bash
# Install dependencies (using uv package manager)
uv sync

# Start server locally
./scripts/start_server.sh
# Or manually:
uv run whisperlivekit-server --port 8888

# Stop server
./scripts/stop_server.sh

# Run auto-type client (types transcriptions into active window)
./bin/auto-type
# Or: uv run python scripts/auto_type_client.py

# Run n8n webhook client
./bin/n8n-webhook --n8n-webhook https://n8n.delo.sh/webhook/transcription
# Or: uv run python scripts/n8n_webhook_client.py

# Test connection
uv run python scripts/test_connection.py

# Debug client (verbose logging)
uv run python scripts/debug_client.py

# Voice to n8n workflow
uv run python scripts/voice_to_n8n.py

# Check audio device sample rates
uv run python scripts/check_device_rates.py

# Complete setup script
./scripts/whisperlivekit_complete_setup.sh

# Add Python dependencies
uv add package-name
```

**Important**: This project uses `uv` as the Python package manager, not pip or poetry. Always use `uv run` or `uv add` for Python operations.

### TonnyTray Desktop App (Tauri)

```bash
cd TonnyTray

# Install Node dependencies
npm install

# Development mode (hot reload)
npm run tauri:dev
# Or frontend only: npm run dev

# Build production app
npm run tauri:build

# Frontend type checking
npm run type-check

# Linting
npm run lint

# Tests
npm run test              # Vitest unit tests
npm run test:ui          # Vitest UI
npm run test:run         # Run tests once (CI mode)
npm run test:coverage    # Coverage report
npm run test:integration # Integration tests
npm run test:e2e         # Playwright E2E tests
npm run test:e2e:ui      # Playwright UI mode
npm run test:e2e:headed  # Playwright with browser visible
npm run test:rust        # Rust unit tests
npm run test:rust:coverage # Rust coverage report
npm run test:all         # All tests (Rust + TS + E2E)

# Benchmarks and Security
npm run bench            # Rust benchmarks
npm run security-audit   # npm + cargo security audit

# Rust development
cd src-tauri
cargo build              # Debug build
cargo build --release    # Release build
cargo test               # Run tests
cargo clippy             # Linting
cargo fmt                # Format code
```

### Chrome Extension

```bash
cd chrome-extension

# Load in Chrome:
# 1. Navigate to chrome://extensions/
# 2. Enable "Developer mode"
# 3. Click "Load unpacked"
# 4. Select the chrome-extension/ directory
```

### Docker Deployment

```bash
# Build and run with Docker Compose
docker-compose up -d

# Server available at http://localhost:8000
# Configured for Traefik reverse proxy at whisper.delo.sh
```

## Key Architecture Patterns

### TonnyTray: Tauri IPC Communication

The React frontend communicates with the Rust backend via Tauri's IPC system:

**Service Layer** (`TonnyTray/src/services/tauri.ts`):
- Wraps all Tauri `invoke` commands
- Type-safe wrappers for commands
- Centralized error handling
- Example: `startRecording()`, `stopRecording()`, `updateSettings()`

**State Management** (`TonnyTray/src/hooks/useTauriState.ts`):
- Uses Zustand for global state
- Subscribes to Tauri events (`onTranscription`, `onStatusUpdate`)
- Custom hooks for domain-specific operations

**Rust Backend** (`TonnyTray/src-tauri/src/lib.rs`):
- Main entry point registers all IPC command handlers
- Modules: `state`, `process_manager`, `audio`, `websocket`, `elevenlabs`, `config`, `tray`, `database`, `keychain`, `events`
- Thread-safe state via `Arc<Mutex<T>>`
- Event system (`events.rs`) for frontend notifications

### TonnyTray: Process Management

The Rust backend (`process_manager.rs`) manages WhisperLiveKit server lifecycle:
- Spawns Python subprocess for WhisperLiveKit
- Monitors process health
- Auto-restart on crash (configurable)
- Graceful shutdown via SIGTERM

### WhisperLiveKit: WebSocket Protocol

Clients connect via WebSocket to receive real-time transcriptions:
- Connect to `ws://localhost:8888/asr`
- Send binary audio chunks (16kHz, 16-bit PCM)
- Receive JSON transcription events
- See `whisperlivekit/basic_server.py` for protocol details

### State Synchronization

TonnyTray maintains a SQLite database (`database.rs`) for:
- Transcription history
- User profiles with permissions
- Settings persistence
- Statistics tracking

Configuration stored in `~/.config/tonnytray/config.json`

### Security: Keychain Integration

API keys (ElevenLabs, n8n) can be stored securely via system keychain (`keychain.rs`):
- Linux: Secret Service API
- macOS: Keychain Access (planned)
- Windows: Windows Credential Manager (planned)

### Log Tailer Utility

TonnyTray includes a standalone log tailer (`TonnyTray/src/log-tailer.html`) for real-time log viewing:
- Separate Vite build entry point
- WebSocket-based real-time updates
- Useful for debugging server issues

## Testing Strategy

### TonnyTray Frontend Tests

- **Unit Tests**: Vitest with happy-dom environment
- **Component Tests**: React Testing Library
- **E2E Tests**: Playwright
- Test files: `src/**/*.{test,spec}.{ts,tsx}`
- E2E specs: `e2e/**/*.spec.ts`

### TonnyTray Backend Tests

- **Unit Tests**: Cargo test framework
- **Integration Tests**: Tempfile for config/database
- **Benchmarks**: Criterion (`cargo bench`)
- Run with: `cargo test` in `src-tauri/`

### Running Single Tests

```bash
# Frontend unit test
npm run test -- src/components/Common/ConfirmDialog.test.tsx

# Rust test by name
cd TonnyTray/src-tauri
cargo test test_process_manager

# E2E test by file
npm run test:e2e -- e2e/workflows.spec.ts
```

## Type Safety

### TypeScript Configuration

- Strict mode enabled (`TonnyTray/tsconfig.json`)
- Path aliases configured via Vite:
  - `@/` → `src/`
  - `@components/` → `src/components/`
  - `@hooks/` → `src/hooks/`
  - `@services/` → `src/services/`
  - `@types/` → `src/types/`
  - `@utils/` → `src/utils/`
  - `@theme/` → `src/theme/`
  - `@contexts/` → `src/contexts/`

### Rust Type Safety

- All state shapes defined in `state.rs`
- IPC payloads use serde serialization
- No `unwrap()` in production code - use proper error handling

### Shared Types

Frontend types (`TonnyTray/src/types/index.ts`) must match Rust backend types (`src-tauri/src/state.rs`). When adding new fields:

1. Update Rust struct with `#[derive(Serialize)]`
2. Update TypeScript interface
3. Ensure field names match exactly (snake_case → camelCase handled by serde)

## Python Environment

- Python version: 3.10 (managed by mise)
- Package manager: `uv` (NOT pip)
- Dependencies defined in `pyproject.toml`
- Lock file: `uv.lock`
- Virtual environment: `.venv/` (auto-created by uv)

Key dependencies:
- fastapi, uvicorn - Web server
- websockets - Real-time communication
- faster-whisper - Speech recognition
- pyaudio - Audio capture
- scipy, numpy - Audio processing

## Important File Locations

### Configuration
- TonnyTray config: `~/.config/tonnytray/config.json`
- TonnyTray database: `~/.local/share/tonnytray/tonnytray.db`
- Python environment: `.venv/`

### Entry Points
- WhisperLiveKit server: `whisperlivekit/basic_server.py`
- Tauri main: `TonnyTray/src-tauri/src/lib.rs`
- React main: `TonnyTray/src/main.tsx`
- Log tailer utility: `TonnyTray/src/log-tailer.html`
- Chrome extension: `chrome-extension/background.js`

### Build Outputs
- Tauri release: `TonnyTray/target/release/bundle/`
- Frontend build: `TonnyTray/dist/`
- Rust debug: `TonnyTray/src-tauri/target/debug/`

## Development Workflow Recommendations

### Working on Frontend

1. Start WhisperLiveKit server: `./scripts/start_server.sh`
2. Start Tauri in dev mode: `cd TonnyTray && npm run tauri:dev`
3. Frontend hot-reloads automatically
4. Backend requires restart for Rust changes

### Working on Rust Backend

1. Make changes in `TonnyTray/src-tauri/src/`
2. Run tests: `cargo test`
3. Check with clippy: `cargo clippy`
4. Format: `cargo fmt`
5. Restart Tauri: `npm run tauri:dev`

### Working on WhisperLiveKit

1. Make changes in `whisperlivekit/`
2. Restart server: `./scripts/stop_server.sh && ./scripts/start_server.sh`
3. Test with client: `./bin/auto-type` or `uv run python scripts/test_connection.py`

### Adding New Features

1. **Define types**: Update `TonnyTray/src/types/index.ts` and `src-tauri/src/state.rs`
2. **Add Rust IPC command**: In `src-tauri/src/lib.rs`, register new handler
3. **Add service wrapper**: In `TonnyTray/src/services/tauri.ts`
4. **Create hooks if needed**: In `TonnyTray/src/hooks/`
5. **Build UI components**: In `TonnyTray/src/components/`
6. **Write tests**: Unit tests, integration tests, E2E tests
7. **Update documentation**: This file and component READMEs

## Common Issues

### "Command not found: uv"

Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh`

### Tauri Build Fails on Linux

Install system dependencies:
```bash
sudo apt install libwebkit2gtk-4.0-dev build-essential curl wget libssl-dev libgtk-3-dev
```

### WhisperLiveKit Connection Issues

1. Check server is running: `ps aux | grep whisper`
2. Test connection: `uv run python scripts/test_connection.py`
3. Check logs: `tail -f whisper.log`
4. Verify port not in use: `lsof -i :8888`

### Audio Device Issues

List devices: `./bin/auto-type --list-devices`
Manually select: `./bin/auto-type --device 6`

### Frontend State Not Updating

Check Tauri event listeners are registered in `useTauriState.ts`. Events must match backend `emit` calls exactly.

## Performance Considerations

### TonnyTray

- Startup time: ~1-2 seconds
- Memory (idle): ~10-20 MB
- Audio latency: <50ms
- IPC latency: <10ms
- Uses `Arc<Mutex<T>>` for thread-safe state access
- Audio processing in separate thread

### WhisperLiveKit

- Model loading time: 3-10 seconds (depends on model size)
- Transcription latency: 200-500ms
- Memory: 500MB-2GB (depends on model)
- Models: tiny, base, small, medium, large-v3

## Remote Development

Server accessible remotely at `https://whisper.delo.sh` (configured in docker-compose.yml with Traefik labels).

Connect clients to remote:
```bash
./bin/auto-type --remote whisper.delo.sh
```

## Platform Support

- **Linux**: Fully supported (Wayland + X11)
- **macOS**: Planned
- **Windows**: Planned
