# TonnyTray Architecture Review

**Document Version:** 1.0
**Date:** 2025-10-16
**Reviewer:** Senior Software Architect
**PRD Version:** Based on `/home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray/PRD.md`

---

## Executive Summary

**Architectural Impact:** HIGH

The TonnyTray application represents a critical orchestration layer for a distributed voice-to-action system. This review identifies **significant architectural concerns** around process management, state synchronization, and external dependencies that require careful design to prevent cascading failures.

**Key Findings:**
- Stack choice (Tauri + React + Rust) is **appropriate** for cross-platform system tray requirements
- Process management architecture needs **hardening** against failure modes
- Storage split (PostgreSQL + JSON) creates **complexity without clear benefit**
- RabbitMQ integration is **underspecified** and may be unnecessary
- Security model needs **explicit secrets management** strategy
- State synchronization between frontend/backend requires **careful design**

**Recommendation:** Proceed with modifications to storage strategy and process management patterns outlined below.

---

## 1. Technology Stack Assessment

### 1.1 Tauri + React + Rust + PostgreSQL

**Architecture Rating:** ✅ APPROPRIATE with modifications

#### Strengths
- **Tauri:** Excellent choice for system tray applications
  - Native OS integration (hotkeys, tray icons, notifications)
  - Small binary size (~3-5MB vs Electron's 100MB+)
  - Better security boundary (Rust backend, sandboxed frontend)
  - Cross-platform support (Linux/macOS/Windows)

- **React + TypeScript:** Solid for settings UI complexity
  - Large ecosystem for UI components (Material-UI, Radix)
  - Good developer experience for family-friendly interfaces
  - Strong typing reduces runtime errors

- **Rust:** Perfect for process management and system integration
  - Memory safety prevents crashes in long-running tray process
  - Excellent async support (tokio) for WebSocket/HTTP clients
  - Native system integration libraries available

#### Critical Issue: Storage Architecture

**Problem:** PRD specifies "Postgres (native install on host) for logs, settings in JSON"

**Architectural Concern:** This creates **dual-source-of-truth complexity** and operational overhead.

```
Current Proposal:
┌─────────────────────────────────────────┐
│  TonnyTray App                          │
│  ├─ Settings: config.json               │ ← FILE
│  └─ Logs: PostgreSQL                    │ ← DATABASE
└─────────────────────────────────────────┘
```

**Issues:**
1. **Dependency Management:** Requires PostgreSQL installed on every user's machine (non-trivial for family users)
2. **Backup/Migration:** Two separate systems to backup and restore
3. **Consistency Risk:** Settings in JSON could get out of sync with logs in PostgreSQL
4. **Complexity:** Need PostgreSQL connection pooling, migration scripts, error handling
5. **Security:** PostgreSQL credentials need secure storage (where? more JSON?)

**Recommended Alternative: SQLite for Everything**

```rust
// Single source of truth
struct AppStorage {
    db: SqliteConnection,  // ~/.config/tonnytray/app.db
}

// Schema
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,  // JSON-serialized values
    updated_at TIMESTAMP
);

CREATE TABLE logs (
    id INTEGER PRIMARY KEY,
    timestamp TIMESTAMP,
    level TEXT,
    component TEXT,
    message TEXT,
    metadata TEXT  // JSON for structured data
);

CREATE TABLE profiles (
    id INTEGER PRIMARY KEY,
    name TEXT,
    permissions TEXT,
    voice_id TEXT,
    settings TEXT  // JSON
);

CREATE TABLE transcriptions (
    id INTEGER PRIMARY KEY,
    timestamp TIMESTAMP,
    profile_id INTEGER,
    text TEXT,
    confidence REAL,
    speaker_id TEXT,
    sent_to_n8n BOOLEAN
);
```

**Benefits:**
- ✅ **Single file** (~/.config/tonnytray/app.db) - easy backup/restore
- ✅ **No external dependencies** - SQLite embedded in Rust binary
- ✅ **ACID transactions** - settings + logs consistency
- ✅ **Better performance** - no network overhead for localhost PostgreSQL
- ✅ **Simpler deployment** - one less thing to install and configure
- ✅ **SQL benefits** - queryable logs, structured data, migrations

**Implementation Recommendation:**
```rust
// Use diesel or sqlx for type-safe queries
use sqlx::{SqlitePool, sqlite::SqlitePoolOptions};

pub struct AppDatabase {
    pool: SqlitePool,
}

impl AppDatabase {
    pub async fn new(path: &Path) -> Result<Self> {
        let pool = SqlitePoolOptions::new()
            .max_connections(5)
            .connect(&format!("sqlite://{}", path.display()))
            .await?;

        // Run migrations on startup
        sqlx::migrate!("./migrations").run(&pool).await?;

        Ok(Self { pool })
    }

    pub async fn get_setting<T: DeserializeOwned>(
        &self,
        key: &str
    ) -> Result<Option<T>> {
        let row: Option<(String,)> = sqlx::query_as(
            "SELECT value FROM settings WHERE key = ?"
        )
        .bind(key)
        .fetch_optional(&self.pool)
        .await?;

        match row {
            Some((json,)) => Ok(Some(serde_json::from_str(&json)?)),
            None => Ok(None),
        }
    }
}
```

**Decision Required:** Confirm SQLite adoption before proceeding.

---

## 2. Process Management Architecture

### 2.1 Current Challenge

TonnyTray must orchestrate two external Python processes:

1. **WhisperLiveKit Server** (`whisperlivekit-server`)
   - FastAPI + uvicorn WebSocket server
   - Runs Whisper model (CPU/GPU intensive)
   - Port 8888, external clients can connect

2. **Auto-Type Client** (`auto_type_client.py`)
   - Connects to WhisperLiveKit via WebSocket
   - Captures audio, sends to server, types responses
   - Uses `pyaudio`, `ydotool`/`wtype`

### 2.2 Process Lifecycle Architecture

**Architecture Rating:** ⚠️ NEEDS HARDENING

#### Proposed Design Pattern: Supervisor with Health Monitoring

```rust
// whisperlivekit_manager.rs
use tokio::process::{Command, Child};
use std::sync::Arc;
use tokio::sync::RwLock;

pub enum ServiceStatus {
    Stopped,
    Starting,
    Running { pid: u32, uptime: Duration },
    Failing { restarts: u32, last_error: String },
    Failed { reason: String },
}

pub struct WhisperLiveKitManager {
    state: Arc<RwLock<ServiceStatus>>,
    process: Arc<RwLock<Option<Child>>>,
    config: WhisperConfig,
    health_check_url: String,  // http://localhost:8888
}

impl WhisperLiveKitManager {
    pub async fn start(&self) -> Result<()> {
        let mut state = self.state.write().await;
        *state = ServiceStatus::Starting;
        drop(state);

        // Build command from settings
        let mut cmd = Command::new("uv");
        cmd.args(&["run", "whisperlivekit-server"])
            .arg("--model").arg(&self.config.model)
            .arg("--language").arg(&self.config.language)
            .arg("--host").arg(&self.config.host)
            .arg("--port").arg(self.config.port.to_string())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true);  // CRITICAL: cleanup on drop

        let child = cmd.spawn()?;
        let pid = child.id().ok_or("Failed to get PID")?;

        // Store process handle
        let mut process = self.process.write().await;
        *process = Some(child);
        drop(process);

        // Wait for health check (async with timeout)
        let health_check = self.wait_for_health_check(30).await;

        match health_check {
            Ok(_) => {
                let mut state = self.state.write().await;
                *state = ServiceStatus::Running {
                    pid,
                    uptime: Duration::from_secs(0)
                };
                Ok(())
            }
            Err(e) => {
                self.stop().await?;
                Err(e)
            }
        }
    }

    async fn wait_for_health_check(&self, timeout_secs: u64) -> Result<()> {
        let client = reqwest::Client::new();
        let deadline = tokio::time::Instant::now()
            + Duration::from_secs(timeout_secs);

        while tokio::time::Instant::now() < deadline {
            match client.get(&self.health_check_url)
                .timeout(Duration::from_secs(2))
                .send()
                .await
            {
                Ok(resp) if resp.status().is_success() => return Ok(()),
                _ => tokio::time::sleep(Duration::from_millis(500)).await,
            }
        }

        Err(anyhow::anyhow!("Health check timeout"))
    }

    pub async fn stop(&self) -> Result<()> {
        let mut process = self.process.write().await;
        if let Some(mut child) = process.take() {
            // Try graceful shutdown first (SIGTERM)
            #[cfg(unix)]
            {
                use nix::sys::signal::{kill, Signal};
                use nix::unistd::Pid;
                if let Some(pid) = child.id() {
                    let _ = kill(Pid::from_raw(pid as i32), Signal::SIGTERM);

                    // Wait up to 5 seconds for graceful shutdown
                    match tokio::time::timeout(
                        Duration::from_secs(5),
                        child.wait()
                    ).await {
                        Ok(_) => return Ok(()),
                        Err(_) => {
                            // Force kill if graceful shutdown failed
                            child.kill().await?;
                        }
                    }
                }
            }

            #[cfg(not(unix))]
            child.kill().await?;
        }

        let mut state = self.state.write().await;
        *state = ServiceStatus::Stopped;
        Ok(())
    }

    pub async fn restart(&self) -> Result<()> {
        self.stop().await?;
        tokio::time::sleep(Duration::from_secs(1)).await;
        self.start().await
    }

    // Background health monitor
    pub fn spawn_health_monitor(self: Arc<Self>) {
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(10));
            let mut consecutive_failures = 0u32;

            loop {
                interval.tick().await;

                let status = self.state.read().await;
                if !matches!(*status, ServiceStatus::Running { .. }) {
                    consecutive_failures = 0;
                    continue;
                }
                drop(status);

                // Check if process is still alive
                let process = self.process.read().await;
                let is_alive = if let Some(child) = process.as_ref() {
                    // Check PID exists (non-blocking)
                    child.id().is_some()
                } else {
                    false
                };
                drop(process);

                if !is_alive {
                    consecutive_failures += 1;
                    warn!("WhisperLiveKit process died (failures: {})",
                          consecutive_failures);

                    if consecutive_failures < 3 {
                        // Auto-restart
                        warn!("Auto-restarting WhisperLiveKit...");
                        if let Err(e) = self.restart().await {
                            error!("Failed to restart: {}", e);
                        }
                    } else {
                        // Give up after 3 failures
                        let mut state = self.state.write().await;
                        *state = ServiceStatus::Failed {
                            reason: "Too many restart failures".to_string()
                        };
                        break;
                    }
                } else {
                    consecutive_failures = 0;
                }
            }
        });
    }
}
```

#### Key Architectural Decisions

**1. Process Spawning Strategy**

✅ **Recommended:** Direct `tokio::process::Command` spawn
- Simpler than systemd/launchd integration for per-user service
- Works cross-platform without OS-specific service configuration
- `kill_on_drop(true)` ensures cleanup on crash

❌ **Avoid:** Shell script wrappers (`start_server.sh`)
- Loses direct process handle
- PID files can become stale
- Harder to capture stdout/stderr for logs

**2. Health Check Strategy**

```rust
// Multi-layer health check
async fn is_healthy(&self) -> bool {
    // Layer 1: Process exists
    let process_alive = self.check_process_alive().await;
    if !process_alive { return false; }

    // Layer 2: HTTP endpoint responds
    let http_healthy = self.check_http_health().await;
    if !http_healthy { return false; }

    // Layer 3: WebSocket accepts connections
    let ws_healthy = self.check_websocket_health().await;

    ws_healthy
}

async fn check_websocket_health(&self) -> bool {
    use tokio_tungstenite::connect_async;

    match tokio::time::timeout(
        Duration::from_secs(3),
        connect_async("ws://localhost:8888/asr")
    ).await {
        Ok(Ok((ws, _))) => {
            // Successfully connected, immediately disconnect
            drop(ws);
            true
        }
        _ => false
    }
}
```

**3. Restart Policy**

```rust
pub struct RestartPolicy {
    pub max_restarts: u32,         // 3
    pub restart_window: Duration,  // 5 minutes
    pub backoff: Duration,         // 1 second initial, exponential
}

// Track restart history
pub struct RestartTracker {
    restarts: Vec<Instant>,
    policy: RestartPolicy,
}

impl RestartTracker {
    pub fn should_restart(&mut self) -> bool {
        let now = Instant::now();
        let window_start = now - self.policy.restart_window;

        // Remove old restarts outside window
        self.restarts.retain(|&t| t > window_start);

        // Check if under limit
        if self.restarts.len() < self.policy.max_restarts as usize {
            self.restarts.push(now);
            true
        } else {
            false  // Too many restarts
        }
    }

    pub fn backoff_duration(&self) -> Duration {
        // Exponential backoff: 1s, 2s, 4s, 8s...
        let attempts = self.restarts.len() as u32;
        self.policy.backoff * 2u32.pow(attempts.min(5))
    }
}
```

**4. Log Capture**

```rust
// Stream stdout/stderr to database
pub async fn capture_process_logs(
    mut stdout: tokio::process::ChildStdout,
    db: Arc<AppDatabase>,
    component: String,
) {
    use tokio::io::{AsyncBufReadExt, BufReader};

    let reader = BufReader::new(stdout);
    let mut lines = reader.lines();

    while let Ok(Some(line)) = lines.next_line().await {
        let _ = db.insert_log(LogEntry {
            timestamp: Utc::now(),
            level: "INFO",
            component: &component,
            message: &line,
            metadata: None,
        }).await;
    }
}
```

### 2.3 Auto-Type Client Management

**Similar pattern, additional considerations:**

```rust
pub struct AutoTypeManager {
    state: Arc<RwLock<ServiceStatus>>,
    process: Arc<RwLock<Option<Child>>>,
    config: AutoTypeConfig,
}

// Key difference: dependent on WhisperLiveKit
impl AutoTypeManager {
    pub async fn start(&self, whisper_manager: &WhisperLiveKitManager) -> Result<()> {
        // Wait for WhisperLiveKit to be healthy first
        whisper_manager.wait_until_ready(Duration::from_secs(30)).await?;

        let mut cmd = Command::new("python3");
        cmd.arg("auto_type_client.py")
            .arg("--whisper-url").arg(&self.config.whisper_url)
            .arg("--device").arg(self.config.device_index.to_string());

        if self.config.send_to_n8n {
            cmd.arg("--send-to-n8n");
        }

        let child = cmd.spawn()?;
        // ... rest of implementation
    }
}
```

**Startup Sequence:**

```rust
pub struct AppServices {
    whisper: Arc<WhisperLiveKitManager>,
    auto_type: Arc<AutoTypeManager>,
}

impl AppServices {
    pub async fn start_all(&self) -> Result<()> {
        // 1. Start WhisperLiveKit server
        self.whisper.start().await?;

        // 2. Wait for health check (already done in start())

        // 3. Start auto-type client (depends on whisper)
        self.auto_type.start(&self.whisper).await?;

        Ok(())
    }

    pub async fn stop_all(&self) -> Result<()> {
        // Stop in reverse order
        let _ = self.auto_type.stop().await;  // Ignore errors
        tokio::time::sleep(Duration::from_millis(500)).await;
        self.whisper.stop().await?;
        Ok(())
    }
}
```

---

## 3. IPC Design (Tauri Frontend ↔ Rust Backend)

### 3.1 Tauri Command Pattern

**Architecture Rating:** ✅ WELL-DEFINED

Tauri provides type-safe IPC through annotated Rust functions:

```rust
// src-tauri/src/main.rs
use tauri::State;

#[tauri::command]
async fn start_recording(
    state: State<'_, AppState>
) -> Result<(), String> {
    state.services.auto_type.start(&state.services.whisper)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn get_status(
    state: State<'_, AppState>
) -> Result<SystemStatus, String> {
    let whisper_status = state.services.whisper.get_status().await;
    let auto_type_status = state.services.auto_type.get_status().await;

    Ok(SystemStatus {
        whisper: whisper_status,
        auto_type: auto_type_status,
        last_transcription: state.last_transcription.read().await.clone(),
    })
}

#[tauri::command]
async fn update_settings(
    settings: AppSettings,
    state: State<'_, AppState>
) -> Result<(), String> {
    // Validate settings
    validate_settings(&settings)?;

    // Save to database
    state.db.save_settings(&settings).await
        .map_err(|e| e.to_string())?;

    // Apply changes (may require restart)
    if settings_require_restart(&settings, &state.current_settings) {
        state.services.restart_all().await
            .map_err(|e| e.to_string())?;
    }

    Ok(())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            start_recording,
            stop_recording,
            get_status,
            update_settings,
            test_n8n_webhook,
            get_logs,
            get_profiles,
            // ... all commands
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
```

**Frontend (TypeScript):**

```typescript
// src/api/tauri.ts
import { invoke } from '@tauri-apps/api/tauri';

export interface SystemStatus {
  whisper: ServiceStatus;
  autoType: ServiceStatus;
  lastTranscription: string;
}

export const tauriApi = {
  async startRecording(): Promise<void> {
    return invoke('start_recording');
  },

  async getStatus(): Promise<SystemStatus> {
    return invoke('get_status');
  },

  async updateSettings(settings: AppSettings): Promise<void> {
    return invoke('update_settings', { settings });
  },
};
```

### 3.2 Event System (Backend → Frontend Push)

**Critical for real-time UI updates:**

```rust
// Backend: Emit events to frontend
use tauri::Manager;

pub struct EventEmitter {
    app_handle: tauri::AppHandle,
}

impl EventEmitter {
    pub fn emit_status_change(&self, status: ServiceStatus) {
        let _ = self.app_handle.emit_all("status-change", status);
    }

    pub fn emit_transcription(&self, text: String) {
        let _ = self.app_handle.emit_all("transcription",
            TranscriptionEvent {
                text,
                timestamp: Utc::now(),
            });
    }

    pub fn emit_notification(&self, notification: Notification) {
        let _ = self.app_handle.emit_all("notification", notification);
    }
}
```

```typescript
// Frontend: Listen for events
import { listen } from '@tauri-apps/api/event';

// In React component
useEffect(() => {
  const unlisten = listen<TranscriptionEvent>('transcription', (event) => {
    setLastTranscription(event.payload.text);
    // Update UI immediately
  });

  return () => {
    unlisten.then(fn => fn());
  };
}, []);
```

### 3.3 State Synchronization Pattern

**Problem:** Avoid stale state between frontend and backend

**Solution:** Single source of truth (backend) + reactive frontend

```rust
// Backend state
pub struct AppState {
    // Shared state (Arc for clone across threads)
    services: Arc<AppServices>,
    db: Arc<AppDatabase>,
    settings: Arc<RwLock<AppSettings>>,
    last_transcription: Arc<RwLock<String>>,
    event_emitter: Arc<EventEmitter>,
}

// State mutation always emits event
impl AppState {
    pub async fn set_last_transcription(&self, text: String) {
        let mut last = self.last_transcription.write().await;
        *last = text.clone();
        drop(last);

        // Notify frontend immediately
        self.event_emitter.emit_transcription(text);
    }
}
```

```typescript
// Frontend: React Query for server state
import { useQuery, useMutation } from '@tanstack/react-query';

function useSystemStatus() {
  return useQuery({
    queryKey: ['status'],
    queryFn: () => tauriApi.getStatus(),
    refetchInterval: 5000,  // Poll every 5s as fallback
  });
}

// But also listen to events for immediate updates
function useRealtimeStatus() {
  const [status, setStatus] = useState<SystemStatus | null>(null);

  useEffect(() => {
    // Initial fetch
    tauriApi.getStatus().then(setStatus);

    // Real-time updates
    const unlisten = listen<SystemStatus>('status-change', (event) => {
      setStatus(event.payload);
    });

    return () => { unlisten.then(fn => fn()); };
  }, []);

  return status;
}
```

**Recommendation:** Use **hybrid approach**
- Events for immediate updates (transcriptions, status changes)
- Periodic polling as fallback (network resilience)
- React Query for cache management and optimistic updates

---

## 4. Integration Points Architecture

### 4.1 WebSocket (n8n) Integration

**Architecture Rating:** ✅ APPROPRIATE with caveats

```rust
// n8n_client.rs
use reqwest::Client;
use serde::{Deserialize, Serialize};

pub struct N8nClient {
    client: Client,
    webhook_url: String,
    timeout: Duration,
}

#[derive(Serialize)]
pub struct TranscriptionPayload {
    timestamp: DateTime<Utc>,
    text: String,
    source: String,
    speaker: Option<String>,
    profile: Option<String>,
}

impl N8nClient {
    pub async fn send_transcription(
        &self,
        payload: TranscriptionPayload
    ) -> Result<N8nResponse> {
        let response = self.client
            .post(&self.webhook_url)
            .json(&payload)
            .timeout(self.timeout)
            .send()
            .await?;

        if !response.status().is_success() {
            return Err(anyhow::anyhow!(
                "n8n webhook failed: {}",
                response.status()
            ));
        }

        let result: N8nResponse = response.json().await?;
        Ok(result)
    }
}
```

**Critical Issue: Error Handling Strategy**

The PRD mentions "Tonny Always First" pattern but doesn't specify failure modes:

```rust
// Robust error handling with circuit breaker
pub struct N8nClientWithCircuitBreaker {
    client: N8nClient,
    circuit_breaker: Arc<RwLock<CircuitBreaker>>,
}

pub struct CircuitBreaker {
    state: CircuitState,
    failure_count: u32,
    failure_threshold: u32,
    reset_timeout: Duration,
    last_failure: Option<Instant>,
}

pub enum CircuitState {
    Closed,      // Normal operation
    Open,        // Too many failures, reject requests
    HalfOpen,    // Testing if service recovered
}

impl N8nClientWithCircuitBreaker {
    pub async fn send_with_protection(
        &self,
        payload: TranscriptionPayload
    ) -> Result<N8nResponse> {
        let mut cb = self.circuit_breaker.write().await;

        match cb.state {
            CircuitState::Open => {
                // Check if we should try again
                if let Some(last_failure) = cb.last_failure {
                    if last_failure.elapsed() > cb.reset_timeout {
                        cb.state = CircuitState::HalfOpen;
                    } else {
                        return Err(anyhow::anyhow!("Circuit breaker open"));
                    }
                }
            }
            _ => {}
        }
        drop(cb);

        // Attempt request
        match self.client.send_transcription(payload).await {
            Ok(response) => {
                // Success: reset circuit breaker
                let mut cb = self.circuit_breaker.write().await;
                cb.failure_count = 0;
                cb.state = CircuitState::Closed;
                Ok(response)
            }
            Err(e) => {
                // Failure: increment counter
                let mut cb = self.circuit_breaker.write().await;
                cb.failure_count += 1;
                cb.last_failure = Some(Instant::now());

                if cb.failure_count >= cb.failure_threshold {
                    cb.state = CircuitState::Open;
                    warn!("Circuit breaker opened after {} failures",
                          cb.failure_count);
                }

                Err(e)
            }
        }
    }
}
```

**Recommendation:**
- Implement circuit breaker for n8n calls
- Queue failed requests locally (SQLite) for retry
- Display user notification when n8n is unreachable
- Consider offline mode (store + sync later)

### 4.2 REST (ElevenLabs) Integration

**Architecture Rating:** ✅ STRAIGHTFORWARD

```rust
// elevenlabs_client.rs
pub struct ElevenLabsClient {
    client: Client,
    api_key: String,
    base_url: String,
}

impl ElevenLabsClient {
    pub async fn text_to_speech(
        &self,
        text: &str,
        voice_id: &str,
        settings: &VoiceSettings,
    ) -> Result<Vec<u8>> {
        let response = self.client
            .post(&format!("{}/v1/text-to-speech/{}", self.base_url, voice_id))
            .header("xi-api-key", &self.api_key)
            .json(&serde_json::json!({
                "text": text,
                "model_id": "eleven_monolingual_v1",
                "voice_settings": {
                    "stability": settings.stability,
                    "similarity_boost": settings.similarity_boost,
                }
            }))
            .send()
            .await?;

        let bytes = response.bytes().await?;
        Ok(bytes.to_vec())
    }

    pub async fn get_voices(&self) -> Result<Vec<Voice>> {
        let response = self.client
            .get(&format!("{}/v1/voices", self.base_url))
            .header("xi-api-key", &self.api_key)
            .send()
            .await?;

        let voices: VoicesResponse = response.json().await?;
        Ok(voices.voices)
    }
}
```

**Audio Playback (rodio):**

```rust
// audio_player.rs
use rodio::{Decoder, OutputStream, Sink};
use std::io::Cursor;

pub struct AudioPlayer {
    _stream: OutputStream,
    sink: Sink,
}

impl AudioPlayer {
    pub fn new() -> Result<Self> {
        let (stream, stream_handle) = OutputStream::try_default()?;
        let sink = Sink::try_new(&stream_handle)?;

        Ok(Self {
            _stream: stream,
            sink,
        })
    }

    pub fn play_audio(&self, audio_data: Vec<u8>) -> Result<()> {
        let cursor = Cursor::new(audio_data);
        let source = Decoder::new(cursor)?;
        self.sink.append(source);
        Ok(())
    }

    pub fn is_playing(&self) -> bool {
        !self.sink.is_paused() && self.sink.len() > 0
    }

    pub fn stop(&self) {
        self.sink.stop();
    }
}
```

**Response Flow Implementation:**

```rust
// response_handler.rs
pub struct ResponseHandler {
    elevenlabs: Arc<ElevenLabsClient>,
    audio_player: Arc<AudioPlayer>,
    db: Arc<AppDatabase>,
}

impl ResponseHandler {
    pub async fn handle_n8n_response(
        &self,
        response: N8nResponse,
        settings: &AppSettings,
    ) -> Result<()> {
        // Check response mode
        match settings.response_mode {
            ResponseMode::TextOnly => {
                // Type response using auto-type
                self.type_response(&response.text).await?;
            }
            ResponseMode::VoiceOnly => {
                // Speak response
                self.speak_response(&response.text, settings).await?;
            }
            ResponseMode::Both => {
                // Do both concurrently
                tokio::try_join!(
                    self.type_response(&response.text),
                    self.speak_response(&response.text, settings)
                )?;
            }
        }

        // Log response
        self.db.insert_response(response).await?;

        Ok(())
    }

    async fn speak_response(
        &self,
        text: &str,
        settings: &AppSettings,
    ) -> Result<()> {
        // Generate audio from ElevenLabs
        let audio = self.elevenlabs.text_to_speech(
            text,
            &settings.elevenlabs.voice_id,
            &settings.elevenlabs.voice_settings,
        ).await?;

        // Play audio (non-blocking)
        self.audio_player.play_audio(audio)?;

        Ok(())
    }
}
```

### 4.3 RabbitMQ Integration

**Architecture Rating:** ⚠️ UNDERSPECIFIED - Needs Clarification

PRD mentions:
> "Event Publishing: Trigger on Command Received: amq.topic / thread.tonny.prompt"

**Critical Questions:**

1. **Who runs RabbitMQ?** Is this self-hosted or cloud-managed?
2. **What is the topology?**
   ```
   TonnyTray → RabbitMQ → ???
                   ↓
                  n8n workflow?
                   ↓
                  Other agents?
   ```

3. **Why RabbitMQ if we already have n8n webhook?**
   - Webhook is request/response (simple)
   - RabbitMQ is pub/sub (complex but decoupled)

**Recommendation: Clarify Architecture First**

**Option A: n8n Webhook Only (Simpler)**
```
TonnyTray → HTTP POST → n8n Webhook → Tonny Agent
                                         ↓
                                    Delegate to other agents
                                         ↓
                                    Response back to TonnyTray
```

✅ Simpler, fewer dependencies
✅ n8n already handles routing
❌ Tighter coupling

**Option B: RabbitMQ Event Bus (More Scalable)**
```
TonnyTray → Publish → RabbitMQ (amq.topic)
                          ↓
                   ┌──────┴──────┬───────────┐
                   ↓             ↓           ↓
              Tonny Agent   Audit Log   Analytics
                   ↓
            Response Queue → TonnyTray
```

✅ Decoupled, scalable
✅ Multiple consumers possible
❌ More infrastructure (RabbitMQ cluster)
❌ More complexity (connection mgmt, retries)

**If RabbitMQ is required:**

```rust
// rabbitmq_client.rs
use lapin::{
    options::*,
    types::FieldTable,
    Connection,
    ConnectionProperties,
    BasicProperties,
};

pub struct RabbitMQPublisher {
    connection: Connection,
    channel: lapin::Channel,
    exchange: String,
}

impl RabbitMQPublisher {
    pub async fn new(amqp_url: &str, exchange: &str) -> Result<Self> {
        let connection = Connection::connect(
            amqp_url,
            ConnectionProperties::default()
        ).await?;

        let channel = connection.create_channel().await?;

        // Declare topic exchange
        channel.exchange_declare(
            exchange,
            lapin::ExchangeKind::Topic,
            ExchangeDeclareOptions {
                durable: true,
                ..Default::default()
            },
            FieldTable::default(),
        ).await?;

        Ok(Self {
            connection,
            channel,
            exchange: exchange.to_string(),
        })
    }

    pub async fn publish_prompt(
        &self,
        profile: &str,
        text: &str,
    ) -> Result<()> {
        let routing_key = format!("thread.tonny.prompt.{}", profile);

        let payload = serde_json::json!({
            "timestamp": Utc::now(),
            "profile": profile,
            "text": text,
            "source": "tonnytray",
        });

        self.channel.basic_publish(
            &self.exchange,
            &routing_key,
            BasicPublishOptions::default(),
            payload.to_string().as_bytes(),
            BasicProperties::default()
                .with_content_type("application/json".into())
                .with_delivery_mode(2), // persistent
        ).await?;

        Ok(())
    }
}
```

**Decision Required:** Confirm RabbitMQ requirement and architecture before implementing.

---

## 5. Security Architecture

### 5.1 Secrets Management

**Architecture Rating:** ❌ CRITICAL - Not Specified in PRD

**Problem:** PRD shows API keys in JSON config:
```json
{
  "elevenlabs": {
    "api_key": "***",  // ← How is this encrypted?
  }
}
```

**Threat Model:**
- ❌ Plaintext API keys in JSON → readable by any process
- ❌ API keys in SQLite unencrypted → same issue
- ❌ API keys in environment variables → visible in `ps aux`

**Recommended Solution: OS Keychain Integration**

```rust
// secrets_manager.rs
use keyring::Entry;

pub struct SecretsManager {
    service_name: &'static str,
}

impl SecretsManager {
    pub fn new() -> Self {
        Self {
            service_name: "sh.delo.tonnytray",
        }
    }

    pub fn store_elevenlabs_key(&self, api_key: &str) -> Result<()> {
        let entry = Entry::new(self.service_name, "elevenlabs_api_key")?;
        entry.set_password(api_key)?;
        Ok(())
    }

    pub fn get_elevenlabs_key(&self) -> Result<String> {
        let entry = Entry::new(self.service_name, "elevenlabs_api_key")?;
        let password = entry.get_password()?;
        Ok(password)
    }

    pub fn delete_elevenlabs_key(&self) -> Result<()> {
        let entry = Entry::new(self.service_name, "elevenlabs_api_key")?;
        entry.delete_password()?;
        Ok(())
    }
}
```

**Storage Architecture:**
- **Linux:** libsecret (GNOME Keyring, KWallet)
- **macOS:** Keychain
- **Windows:** Credential Manager

**Settings JSON structure (no secrets):**
```json
{
  "elevenlabs": {
    "enabled": true,
    "voice_id": "21m00Tcm4TlvDq8ikWAM",
    "api_key_configured": true  // ← Boolean flag only
  }
}
```

### 5.2 Process Isolation

**Current Risk:** TonnyTray runs with user privileges
- Can access all user files
- Can spawn processes
- Can capture audio from all apps

**Mitigation Strategies:**

1. **Principle of Least Privilege**
```rust
// Request only necessary permissions
#[cfg(target_os = "linux")]
fn setup_seccomp() -> Result<()> {
    // Restrict syscalls (advanced)
    // Only allow: read, write, socket, etc.
}
```

2. **Tauri Security Configuration**
```json
// tauri.conf.json
{
  "tauri": {
    "security": {
      "csp": "default-src 'self'; connect-src 'self' https://api.elevenlabs.io https://n8n.delo.sh",
      "dangerousDisableAssetCspModification": false,
      "freezePrototype": true
    },
    "allowlist": {
      "all": false,  // ← Deny by default
      "fs": {
        "scope": ["$APPCONFIG/*", "$APPDATA/*"]  // Only app directories
      },
      "shell": {
        "execute": true,  // Need to spawn WhisperLiveKit
        "sidecar": false,
        "scope": [
          { "name": "uv", "cmd": "uv", "args": ["run", "whisperlivekit-server"] }
        ]
      },
      "protocol": {
        "asset": true,
        "assetScope": ["$APPCONFIG/*"]
      }
    }
  }
}
```

3. **Audio Permissions**
- Request microphone permission explicitly
- Show indicator when recording (PRD already specifies tray icon states ✅)
- Allow user to revoke permission

### 5.3 Network Security

**Threat: Man-in-the-Middle Attacks**

```rust
// Force HTTPS/WSS for remote endpoints
pub fn validate_url(url: &str) -> Result<()> {
    let parsed = Url::parse(url)?;

    match parsed.scheme() {
        "http" if parsed.host_str() == Some("localhost") => Ok(()),
        "http" if parsed.host_str() == Some("127.0.0.1") => Ok(()),
        "https" => Ok(()),
        "wss" => Ok(()),
        "ws" if parsed.host_str() == Some("localhost") => Ok(()),
        _ => Err(anyhow::anyhow!("Insecure URL: {}", url)),
    }
}
```

**Certificate Pinning (Optional for Production):**
```rust
// For critical endpoints like n8n
use reqwest::Certificate;

let cert = Certificate::from_pem(include_bytes!("n8n-cert.pem"))?;
let client = Client::builder()
    .add_root_certificate(cert)
    .build()?;
```

### 5.4 Security Checklist

- [ ] **Secrets Management**
  - [ ] ElevenLabs API key in OS keychain
  - [ ] n8n webhook URL validation (HTTPS required)
  - [ ] RabbitMQ credentials in keychain (if used)

- [ ] **Process Isolation**
  - [ ] Tauri allowlist configured (minimal permissions)
  - [ ] File system access limited to app directories
  - [ ] Shell execution scope restricted

- [ ] **Network Security**
  - [ ] HTTPS/WSS for all remote endpoints
  - [ ] Certificate validation enabled
  - [ ] Timeout enforcement on all HTTP calls

- [ ] **Data Protection**
  - [ ] Transcriptions in local SQLite (not cloud)
  - [ ] Optional: Encrypt SQLite database with sqlcipher
  - [ ] Clear data option (GDPR compliance)

- [ ] **Code Security**
  - [ ] Input validation on all settings
  - [ ] SQL injection prevention (parameterized queries)
  - [ ] No `unsafe` Rust code (audit carefully if needed)

- [ ] **User Privacy**
  - [ ] Recording indicator always visible
  - [ ] Activity log with clear history option
  - [ ] Opt-in for telemetry (if added later)

---

## 6. Scalability & Performance

### 6.1 Multi-User Profiles

**Architecture Rating:** ✅ WELL-SCOPED

```rust
// Database schema
CREATE TABLE profiles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    permissions TEXT NOT NULL,  -- 'admin', 'user', 'guest', 'child'
    voice_id TEXT,              -- For speaker diarization
    preferred_agent TEXT,       -- 'tonny', 'other_agent'
    settings TEXT,              -- JSON: custom per-profile settings
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE TABLE profile_usage (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER REFERENCES profiles(id),
    timestamp TIMESTAMP,
    command TEXT,
    success BOOLEAN,
    response_time_ms INTEGER
);
```

**Profile Switching Strategy:**

```rust
pub struct ProfileManager {
    db: Arc<AppDatabase>,
    active_profile: Arc<RwLock<Option<Profile>>>,
}

impl ProfileManager {
    // Manual switch (from UI)
    pub async fn switch_profile(&self, profile_id: i64) -> Result<()> {
        let profile = self.db.get_profile(profile_id).await?;
        let mut active = self.active_profile.write().await;
        *active = Some(profile);
        Ok(())
    }

    // Auto-switch based on speaker diarization
    pub async fn detect_and_switch(&self, speaker_id: &str) -> Result<()> {
        // Query database for profile with matching voice_id
        if let Some(profile) = self.db
            .find_profile_by_voice(speaker_id)
            .await?
        {
            let mut active = self.active_profile.write().await;
            *active = Some(profile);
            info!("Auto-switched to profile: {}", profile.name);
        }
        Ok(())
    }
}
```

**Speaker Diarization Integration:**

WhisperLiveKit already supports speaker diarization (see PRD open question). Need to:

1. Enable diarization in WhisperLiveKit config
2. Parse speaker ID from WebSocket messages
3. Train voice profiles (initial setup wizard)

```rust
// Speaker training workflow
pub async fn train_speaker_profile(
    profile: &Profile,
    audio_samples: Vec<Vec<u8>>,
) -> Result<String> {
    // Send samples to WhisperLiveKit for voice embedding
    // Store speaker_id in profile
    // Details depend on WhisperLiveKit speaker diarization API
    todo!("Implement based on WhisperLiveKit capabilities")
}
```

### 6.2 Concurrent Operations

**Potential Issues:**

1. **Multiple transcriptions simultaneously**
   - Current WhisperLiveKit: 1 WebSocket connection per client
   - TonnyTray: Only 1 auto-type client at a time (by design ✅)

2. **UI responsiveness during operations**
   - Audio playback blocking UI? ❌
   - Long database queries blocking UI? ❌

**Solution: Tokio Async + Task Spawning**

```rust
// All blocking operations in separate tasks
pub async fn handle_transcription(text: String, state: AppState) {
    // Spawn each operation independently
    let db_task = tokio::spawn({
        let db = state.db.clone();
        let text = text.clone();
        async move {
            db.insert_transcription(text).await
        }
    });

    let n8n_task = tokio::spawn({
        let client = state.n8n_client.clone();
        let text = text.clone();
        async move {
            client.send_transcription(text).await
        }
    });

    let audio_task = tokio::spawn({
        let handler = state.response_handler.clone();
        async move {
            handler.speak_response(text).await
        }
    });

    // Wait for all, but don't block main thread
    let _ = tokio::try_join!(db_task, n8n_task, audio_task);
}
```

### 6.3 Resource Management

**Memory:**
- Whisper model: ~500MB-2GB RAM (depending on model size)
- TonnyTray app: ~50-100MB RAM
- Total: ~600MB-2GB RAM (acceptable for modern systems ✅)

**CPU:**
- Whisper transcription: 50-100% CPU during active recording
- Idle: <5% CPU
- Recommendation: Offer model size selection in settings

**Disk:**
- SQLite database: ~10MB per 1000 transcriptions
- Audio cache (if implemented): Configurable limit
- Recommendation: Add "Clear old logs" feature (keep last 30 days)

```rust
// Cleanup task (run daily)
pub async fn cleanup_old_data(db: &AppDatabase) -> Result<()> {
    let cutoff = Utc::now() - Duration::days(30);
    db.delete_transcriptions_before(cutoff).await?;
    db.vacuum().await?;  // Reclaim space
    Ok(())
}
```

### 6.4 Performance Benchmarks

**Target Metrics (from PRD):**
- Time to First Command: < 30 seconds ✅ (achievable)
- Command Success Rate: > 95% ✅ (depends on network/Whisper accuracy)
- Response Latency: < 2 seconds ✅ (challenging)

**Latency Breakdown:**
```
User speaks (2s)
  → Whisper transcription (0.5-1s)
  → n8n webhook (0.3-0.5s)
  → ElevenLabs TTS (0.5-1s)
  → Audio playback start (0.1s)
= 3.4-4.6s total
```

⚠️ **PRD target of 2s is unrealistic** unless:
- Using faster Whisper model (tiny/base) ← Already specified ✅
- Caching common responses
- Pre-generating audio for frequent phrases
- Streaming TTS instead of batch

**Recommendation:** Revise target to < 5s or implement streaming optimizations.

---

## 7. Error Handling & Resilience

### 7.1 Failure Scenarios

**Service Crashes:**
```rust
// Health monitor already implemented in Section 2.2
// Key: Auto-restart with exponential backoff
```

**Network Failures:**
```rust
pub async fn send_with_retry<T, F, Fut>(
    operation: F,
    max_retries: u32,
) -> Result<T>
where
    F: Fn() -> Fut,
    Fut: Future<Output = Result<T>>,
{
    let mut attempts = 0;
    loop {
        match operation().await {
            Ok(result) => return Ok(result),
            Err(e) if attempts < max_retries => {
                attempts += 1;
                let backoff = Duration::from_millis(100 * 2u64.pow(attempts));
                warn!("Attempt {} failed: {}, retrying in {:?}",
                      attempts, e, backoff);
                tokio::time::sleep(backoff).await;
            }
            Err(e) => return Err(e),
        }
    }
}
```

**Audio Device Issues:**
```rust
// Detect device removal/change
pub async fn monitor_audio_devices(state: AppState) {
    // Platform-specific: udev on Linux, IOKit on macOS
    #[cfg(target_os = "linux")]
    {
        use tokio_udev::{AsyncMonitorSocket, MonitorBuilder};

        let builder = MonitorBuilder::new()?
            .match_subsystem("sound")?;
        let mut monitor = AsyncMonitorSocket::new(builder.listen()?)?;

        loop {
            if let Some(event) = monitor.next().await {
                if event.event_type() == EventType::Remove {
                    warn!("Audio device removed!");
                    state.event_emitter.emit_notification(
                        Notification::error("Microphone disconnected")
                    );
                    // Auto-restart with different device
                    state.services.auto_type.restart().await?;
                }
            }
        }
    }
}
```

### 7.2 User-Facing Error Messages

**Bad Example:** "Error: ConnectionRefused(std::io::Error)"
**Good Example:** "Could not connect to WhisperLiveKit server. Please check that it's running."

```rust
// error_formatter.rs
pub fn format_user_error(error: &anyhow::Error) -> String {
    // Match specific error types
    if let Some(io_err) = error.downcast_ref::<std::io::Error>() {
        match io_err.kind() {
            std::io::ErrorKind::ConnectionRefused => {
                return "Could not connect to WhisperLiveKit server. \
                        Please check that it's running.".to_string();
            }
            std::io::ErrorKind::PermissionDenied => {
                return "Permission denied. Please check microphone permissions.".to_string();
            }
            _ => {}
        }
    }

    // Default: sanitized error message
    format!("An error occurred: {}", error)
}
```

### 7.3 Logging Strategy

```rust
// Use tracing crate for structured logging
use tracing::{error, warn, info, debug, instrument};

#[instrument(skip(state))]
pub async fn start_recording(state: AppState) -> Result<()> {
    info!("Starting recording");

    match state.services.auto_type.start(&state.services.whisper).await {
        Ok(_) => {
            info!("Recording started successfully");
            Ok(())
        }
        Err(e) => {
            error!("Failed to start recording: {:?}", e);
            Err(e)
        }
    }
}

// Log to file + database
pub fn setup_logging(db: Arc<AppDatabase>) -> Result<()> {
    use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

    let file_layer = tracing_appender::rolling::daily(
        "/var/log/tonnytray",
        "app.log"
    );

    let db_layer = DatabaseLogLayer::new(db);

    tracing_subscriber::registry()
        .with(tracing_subscriber::fmt::layer().with_writer(file_layer))
        .with(db_layer)
        .with(tracing_subscriber::filter::LevelFilter::INFO)
        .init();

    Ok(())
}
```

---

## 8. Global Hotkeys (Ctrl+Shift+V)

**Architecture Rating:** ✅ SUPPORTED by Tauri

```rust
// Use tauri-plugin-global-shortcut
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut};

fn setup_hotkeys(app: &mut tauri::App) -> Result<()> {
    let app_handle = app.handle();

    // Register Ctrl+Shift+V
    let shortcut = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyV)?;

    app.global_shortcut().on_shortcut(shortcut, move || {
        // Toggle recording
        app_handle.emit_all("toggle-recording", ()).unwrap();
    })?;

    // Also register Ctrl+Shift+P for pause (from PRD)
    let pause_shortcut = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyP)?;
    app.global_shortcut().on_shortcut(pause_shortcut, move || {
        app_handle.emit_all("pause-recording", ()).unwrap();
    })?;

    Ok(())
}
```

**User-Configurable Hotkeys:**
```rust
pub struct HotkeyConfig {
    pub start_recording: String,  // "Ctrl+Shift+V"
    pub pause_recording: String,  // "Ctrl+Shift+P"
}

// Parse from settings
pub fn parse_hotkey(config: &str) -> Result<Shortcut> {
    // Format: "Ctrl+Shift+V"
    let parts: Vec<&str> = config.split('+').collect();

    let mut modifiers = Modifiers::empty();
    let mut key_code = None;

    for part in parts {
        match part.to_lowercase().as_str() {
            "ctrl" | "control" => modifiers |= Modifiers::CONTROL,
            "shift" => modifiers |= Modifiers::SHIFT,
            "alt" => modifiers |= Modifiers::ALT,
            "meta" | "cmd" => modifiers |= Modifiers::META,
            key => key_code = Some(parse_key_code(key)?),
        }
    }

    let code = key_code.ok_or_else(|| anyhow::anyhow!("No key specified"))?;
    Shortcut::new(Some(modifiers), code)
}
```

---

## 9. Alternative Approaches

### 9.1 Architecture Alternative: Electron vs Tauri

| Aspect | Tauri (Recommended) | Electron |
|--------|---------------------|----------|
| Binary Size | 3-5 MB | 100-150 MB |
| Memory | 50-100 MB | 200-400 MB |
| Security | Rust backend, sandboxed frontend | Node.js backend, less isolated |
| Performance | Native, faster | Slower startup |
| System Integration | Excellent (Rust + OS APIs) | Good (Node.js + native modules) |
| Learning Curve | Steeper (Rust) | Easier (JavaScript) |

**Verdict:** Stick with Tauri ✅

### 9.2 State Management Alternative: Redux vs Zustand

**Current Recommendation:** Zustand (simpler, smaller)

```typescript
// store.ts
import create from 'zustand';

interface AppStore {
  status: SystemStatus | null;
  lastTranscription: string;
  settings: AppSettings;
  updateStatus: (status: SystemStatus) => void;
  updateSettings: (settings: AppSettings) => void;
}

export const useAppStore = create<AppStore>((set) => ({
  status: null,
  lastTranscription: '',
  settings: DEFAULT_SETTINGS,
  updateStatus: (status) => set({ status }),
  updateSettings: (settings) => set({ settings }),
}));
```

**Alternative:** TanStack Query (if you prefer server state focus)

### 9.3 Audio Recording Alternative

**Current:** Delegate to `auto_type_client.py`
**Alternative:** Record in Tauri app directly

```rust
// Use cpal crate for audio recording in Rust
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};

pub struct AudioRecorder {
    stream: cpal::Stream,
}

impl AudioRecorder {
    pub fn new(device_index: usize) -> Result<Self> {
        let host = cpal::default_host();
        let device = host.input_devices()?
            .nth(device_index)
            .ok_or_else(|| anyhow::anyhow!("Device not found"))?;

        let config = device.default_input_config()?;

        let stream = device.build_input_stream(
            &config.into(),
            move |data: &[f32], _: &cpal::InputCallbackInfo| {
                // Send to WhisperLiveKit WebSocket
            },
            |err| eprintln!("Audio stream error: {}", err),
            None,
        )?;

        Ok(Self { stream })
    }

    pub fn start(&self) -> Result<()> {
        self.stream.play()?;
        Ok(())
    }
}
```

**Pros:**
- One less Python process
- More control over audio pipeline
- Better error handling

**Cons:**
- Need to reimplement audio resampling (currently in auto_type_client.py)
- Need to reimplement typing functionality (ydotool/wtype integration)

**Recommendation:** Keep Python client for MVP, migrate to Rust later if needed.

---

## 10. Risk Assessment

### High-Risk Items

| Risk | Severity | Mitigation |
|------|----------|------------|
| **WhisperLiveKit crash loop** | HIGH | Implement circuit breaker, max restart limit (Section 2.2) |
| **n8n webhook unreachable** | HIGH | Implement circuit breaker, offline queue (Section 4.1) |
| **API key exposure** | HIGH | Use OS keychain, never store plaintext (Section 5.1) |
| **Audio device disconnect** | MEDIUM | Monitor udev events, auto-restart with fallback device (Section 7.1) |
| **Profile switching race condition** | MEDIUM | Use RwLock for profile state, atomic operations (Section 6.1) |
| **Database corruption** | MEDIUM | Regular backups, SQLite integrity checks, use WAL mode |

### Medium-Risk Items

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Slow response times (>2s)** | MEDIUM | Use streaming TTS, cache common responses (Section 6.4) |
| **RabbitMQ connection loss** | MEDIUM | Clarify if RabbitMQ is required; implement reconnection logic (Section 4.3) |
| **Hotkey conflicts** | MEDIUM | Make hotkeys configurable, detect conflicts (Section 8) |
| **Memory leak in long session** | LOW | Use Rust (memory safe), monitor metrics, implement daily restart |

---

## 11. Recommendations Summary

### Critical Changes Required

1. **Storage Architecture**
   - ❌ Remove PostgreSQL requirement
   - ✅ Use SQLite for both settings and logs
   - **Reason:** Simpler deployment, better consistency, no external dependencies

2. **Secrets Management**
   - ❌ Don't store API keys in JSON/database plaintext
   - ✅ Use OS keychain (keyring crate)
   - **Reason:** Security best practice, prevent credential theft

3. **Process Management**
   - ✅ Implement health monitoring with circuit breaker
   - ✅ Add restart policy with exponential backoff
   - ✅ Capture stdout/stderr to database
   - **Reason:** Production-grade reliability

### High-Priority Clarifications Needed

1. **RabbitMQ Architecture**
   - Is RabbitMQ actually required?
   - What is the message topology?
   - Who manages the RabbitMQ infrastructure?
   - **Recommendation:** Start with n8n webhook only, add RabbitMQ later if needed

2. **Response Latency Target**
   - PRD specifies < 2s, but realistic estimate is 3-5s
   - Options: Revise target, implement streaming, or cache responses
   - **Recommendation:** Start with 5s target, optimize later

3. **Speaker Diarization**
   - PRD says "Yes" to auto-profile switching
   - Need WhisperLiveKit diarization API details
   - How are voice profiles trained?
   - **Recommendation:** Manual profile switching for MVP, add diarization in V2

### Architecture Approval Checklist

- [ ] **Confirm SQLite adoption** (remove PostgreSQL)
- [ ] **Confirm OS keychain usage** for secrets
- [ ] **Clarify RabbitMQ requirement** (yes/no/later)
- [ ] **Adjust response latency target** (2s → 5s realistic)
- [ ] **Define speaker diarization scope** (MVP or V2?)
- [ ] **Review security checklist** (Section 5.4)
- [ ] **Approve process management pattern** (Section 2.2)

---

## 12. Implementation Roadmap

Based on PRD phases with architectural considerations:

### Phase 1: MVP (Week 1-2) - Core Infrastructure

**Backend (Rust):**
- [ ] Setup Tauri project structure
- [ ] Implement SQLite database layer (migrations, CRUD)
- [ ] Implement secrets manager (OS keychain)
- [ ] Implement WhisperLiveKit process manager
- [ ] Implement auto-type process manager
- [ ] Setup health monitoring
- [ ] Implement basic Tauri commands (start/stop/status)

**Frontend (React):**
- [ ] Setup React + TypeScript + Tailwind
- [ ] Implement system tray icon with states
- [ ] Implement settings panel (basic)
- [ ] Implement status dashboard
- [ ] Setup event listeners for backend updates

**Integration:**
- [ ] n8n webhook client
- [ ] Basic error handling
- [ ] Logging to database

**Deliverable:** Working tray app that can start/stop services and send to n8n

### Phase 2: Voice Response (Week 3)

- [ ] ElevenLabs client implementation
- [ ] Audio playback (rodio)
- [ ] Response handler (text/voice/both modes)
- [ ] Voice selection UI
- [ ] Queue management for multiple responses

**Deliverable:** Full voice response pipeline working

### Phase 3: Family Features (Week 4)

- [ ] Profile management (database schema + UI)
- [ ] Profile switching (manual)
- [ ] Quick actions (preset commands)
- [ ] Kid-safe mode (permission system)
- [ ] Usage statistics

**Deliverable:** Multi-user profile support

### Phase 4: Polish (Week 5-6)

- [ ] Notifications system
- [ ] Logs viewer UI
- [ ] First-run wizard
- [ ] Settings validation
- [ ] Error recovery flows
- [ ] Documentation
- [ ] Testing (unit + integration)

**Deliverable:** Production-ready V1

### Optional Phase 5: Advanced (Post-MVP)

- [ ] RabbitMQ integration (if confirmed needed)
- [ ] Speaker diarization auto-switching
- [ ] Offline mode with sync
- [ ] Auto-updates
- [ ] Mobile companion app

---

## 13. Conclusion

**Overall Assessment:** ✅ **PROCEED WITH MODIFICATIONS**

The TonnyTray architecture is fundamentally sound with Tauri + React + Rust stack. The main concerns are:

1. **Storage complexity** - SQLite solves this cleanly
2. **Process management resilience** - Needs hardening patterns (provided)
3. **Security** - Must use OS keychain for secrets
4. **Dependency clarity** - RabbitMQ requirement needs confirmation

The architecture as modified in this review provides:
- ✅ Production-grade reliability with health monitoring
- ✅ Security best practices with OS keychain integration
- ✅ Scalability for multi-user households
- ✅ Clear error handling and resilience patterns
- ✅ Cross-platform support (Linux/macOS/Windows)

**Next Steps:**
1. Review and approve architectural changes (SQLite, keychain)
2. Clarify RabbitMQ and diarization requirements
3. Proceed with Phase 1 implementation
4. Regular architecture reviews as system evolves

---

**Document End**
