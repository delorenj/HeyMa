# Tauri IPC Reference

Comprehensive reference for TonnyTray's Tauri Inter-Process Communication (IPC) system.

## Table of Contents

- [Overview](#overview)
- [IPC Architecture](#ipc-architecture)
- [Command Registration](#command-registration)
- [Type System](#type-system)
- [Command Reference](#command-reference)
- [Event Reference](#event-reference)
- [Advanced Patterns](#advanced-patterns)
- [Performance Optimization](#performance-optimization)
- [Debugging](#debugging)
- [Security Considerations](#security-considerations)

## Overview

Tauri IPC enables type-safe, bidirectional communication between the React frontend and Rust backend using:

- **Commands** - Frontend calls backend functions
- **Events** - Backend emits events to frontend
- **State Management** - Shared application state

### Key Features

- Full TypeScript type safety
- Async/await support
- Error propagation
- JSON serialization
- Permission-based access control

## IPC Architecture

```
┌─────────────────────────────────────────────┐
│         Frontend (TypeScript)               │
│                                             │
│  ┌──────────────────────────────────────┐  │
│  │   services/tauri.ts                  │  │
│  │   - Type-safe wrappers               │  │
│  │   - Error handling                   │  │
│  │   - Event subscriptions              │  │
│  └──────────────────────────────────────┘  │
│                    ↕                        │
│           Tauri IPC Bridge                  │
│                    ↕                        │
│  ┌──────────────────────────────────────┐  │
│  │   src-tauri/src/main.rs              │  │
│  │   - Command handlers                 │  │
│  │   - State management                 │  │
│  │   - Event emitters                   │  │
│  └──────────────────────────────────────┘  │
│                                             │
└─────────────────────────────────────────────┘
```

## Command Registration

### Backend (Rust)

Commands are registered in `main.rs`:

```rust
use tauri::{command, State};

#[tauri::command]
async fn my_command(
    context: State<'_, AppContext>,
    param: String,
) -> Result<String, String> {
    // Implementation
    Ok(format!("Result: {}", param))
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            my_command,
            // ... other commands
        ])
        .run(tauri::generate_context!())
        .expect("Error running application");
}
```

### Frontend (TypeScript)

Commands are invoked using the `invoke` function:

```typescript
import { invoke } from '@tauri-apps/api/tauri';

const result = await invoke<string>('my_command', {
  param: 'value'
});
```

## Type System

### TypeScript Types

All types are defined in `src/types/index.ts`:

```typescript
// Command payload types
export namespace TauriCommands {
  export interface StartRecordingPayload {
    profileId: string;
  }

  export interface UpdateSettingsPayload {
    settings: Partial<AppSettings>;
  }
}

// Event payload types
export namespace TauriEvents {
  export interface TranscriptionEvent {
    transcription: Transcription;
  }

  export interface StatusUpdateEvent {
    status: ServerStatus;
  }
}
```

### Rust Types

Corresponding Rust types use Serde for serialization:

```rust
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppSettings {
    pub server_url: String,
    pub model: String,
    pub language: String,
    // ...
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TranscriptionEntry {
    pub timestamp: DateTime<Utc>,
    pub text: String,
    pub success: bool,
    pub response: Option<String>,
}
```

### Type Mapping

| TypeScript | Rust | Notes |
|-----------|------|-------|
| `string` | `String` | UTF-8 encoded |
| `number` | `i32`, `i64`, `f32`, `f64` | Use appropriate size |
| `boolean` | `bool` | - |
| `null` | `Option<T>` with `None` | - |
| `undefined` | `Option<T>` with `None` | - |
| `Array<T>` | `Vec<T>` | - |
| `Record<string, T>` | `HashMap<String, T>` | - |
| `Date` | `DateTime<Utc>` | ISO 8601 format |

## Command Reference

### Command Signature Pattern

All commands follow this pattern:

```rust
#[tauri::command]
async fn command_name(
    context: State<'_, AppContext>,  // Application context
    app: AppHandle,                   // Optional: Tauri app handle
    param1: Type1,                    // Command parameters
    param2: Type2,
) -> Result<ReturnType, String> {    // Result with error string
    // Implementation
}
```

### State Management

Commands access shared state via `State<AppContext>`:

```rust
pub struct AppContext {
    state: SharedState,
    process_manager: Arc<TokioMutex<ProcessManager>>,
    audio_manager: Arc<TokioMutex<AudioManager>>,
    elevenlabs_manager: Arc<TokioMutex<ElevenLabsManager>>,
    n8n_client: Arc<TokioMutex<Option<N8nClient>>>,
}
```

### Example Command Implementation

```rust
#[tauri::command]
async fn get_audio_devices(
    context: State<'_, AppContext>,
) -> Result<Vec<String>, String> {
    info!("Command: get_audio_devices");

    let audio = context.audio_manager.lock().await;
    audio.list_input_devices()
        .map_err(|e| e.to_string())
}
```

### Frontend Wrapper

```typescript
export const audioCommands = {
  async getDevices(): Promise<AudioDevice[]> {
    return invokeCommand<AudioDevice[]>('get_audio_devices');
  }
};

async function invokeCommand<T>(
  command: string,
  payload?: Record<string, unknown>
): Promise<T> {
  try {
    return await invoke<T>(command, payload);
  } catch (error) {
    console.error(`Tauri command '${command}' failed:`, error);
    throw error;
  }
}
```

## Event Reference

### Event Emission (Backend)

Events are emitted using `AppHandle`:

```rust
use tauri::Manager;

// Emit event to all windows
app.emit_all("transcription", TranscriptionEvent {
    transcription: entry,
})?;

// Emit to specific window
app.emit_to("main", "status_update", StatusUpdateEvent {
    status: ServerStatus::Running,
})?;
```

### Event Subscription (Frontend)

```typescript
import { listen, Event as TauriEvent } from '@tauri-apps/api/event';

type EventCallback<T> = (event: TauriEvent<T>) => void;
type UnlistenFn = () => void;

export const eventListeners = {
  async onTranscription(
    callback: EventCallback<TauriEvents.TranscriptionEvent>
  ): Promise<UnlistenFn> {
    return listen('transcription', callback);
  }
};

// Usage
const unlisten = await eventListeners.onTranscription((event) => {
  console.log('Transcription:', event.payload.transcription.text);
});

// Cleanup
unlisten();
```

### Event Lifecycle

```
┌──────────────────────────────────────────────┐
│ Backend Event Emission                       │
│ app.emit_all("event_name", payload)          │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────┐
│ Tauri Event System                           │
│ - Serializes payload to JSON                 │
│ - Routes to subscribed listeners             │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────┐
│ Frontend Event Handler                       │
│ - Receives event                             │
│ - Deserializes payload                       │
│ - Executes callback                          │
└──────────────────────────────────────────────┘
```

### Event Best Practices

1. **Unsubscribe on cleanup** - Always call the unlisten function
2. **Handle errors** - Events can fail to deserialize
3. **Throttle high-frequency events** - Use debouncing for UI updates
4. **Type safety** - Always specify payload types

```typescript
// Good: Cleanup on unmount
useEffect(() => {
  const setupListener = async () => {
    const unlisten = await tauriApi.events.onTranscription(handleTranscription);
    return unlisten;
  };

  const unlistenPromise = setupListener();

  return () => {
    unlistenPromise.then(unlisten => unlisten());
  };
}, []);

// Good: Debounce high-frequency events
const debouncedHandler = debounce((event) => {
  updateUI(event.payload);
}, 100);

await tauriApi.events.onAudioLevel(debouncedHandler);
```

## Advanced Patterns

### Async Command with Progress Updates

Backend emits progress events:

```rust
#[tauri::command]
async fn long_running_task(
    app: AppHandle,
    context: State<'_, AppContext>,
) -> Result<String, String> {
    // Emit progress updates
    app.emit_all("task_progress", ProgressEvent {
        percent: 25,
        message: "Processing...".to_string(),
    })?;

    // Do work
    tokio::time::sleep(Duration::from_secs(1)).await;

    app.emit_all("task_progress", ProgressEvent {
        percent: 50,
        message: "Half done...".to_string(),
    })?;

    // More work
    tokio::time::sleep(Duration::from_secs(1)).await;

    app.emit_all("task_progress", ProgressEvent {
        percent: 100,
        message: "Complete!".to_string(),
    })?;

    Ok("Task completed".to_string())
}
```

Frontend handles progress:

```typescript
// Start listening for progress
const unlisten = await listen('task_progress', (event) => {
  setProgress(event.payload.percent);
  setMessage(event.payload.message);
});

// Start task
try {
  const result = await invoke('long_running_task');
  console.log(result);
} finally {
  unlisten();
}
```

### Request-Response Pattern with Correlation

Backend:

```rust
#[derive(Serialize)]
struct ResponseEvent {
    request_id: String,
    data: String,
}

#[tauri::command]
async fn async_request(
    app: AppHandle,
    request_id: String,
    query: String,
) -> Result<(), String> {
    // Process async
    tokio::spawn(async move {
        let result = process_query(&query).await;

        app.emit_all("async_response", ResponseEvent {
            request_id,
            data: result,
        }).ok();
    });

    Ok(())
}
```

Frontend:

```typescript
const requestId = uuid();
const responsePromise = new Promise((resolve) => {
  const unlisten = listen('async_response', (event) => {
    if (event.payload.request_id === requestId) {
      unlisten();
      resolve(event.payload.data);
    }
  });
});

await invoke('async_request', { requestId, query: 'test' });
const result = await responsePromise;
```

### Batch Operations

Batch multiple operations for efficiency:

```rust
#[derive(Deserialize)]
struct BatchCommand {
    operation: String,
    params: serde_json::Value,
}

#[tauri::command]
async fn batch_execute(
    context: State<'_, AppContext>,
    commands: Vec<BatchCommand>,
) -> Result<Vec<serde_json::Value>, String> {
    let mut results = Vec::new();

    for cmd in commands {
        match cmd.operation.as_str() {
            "get_setting" => {
                // Execute operation
                results.push(json!({ "success": true }));
            }
            _ => {
                results.push(json!({ "error": "Unknown operation" }));
            }
        }
    }

    Ok(results)
}
```

## Performance Optimization

### 1. Minimize Serialization Overhead

```typescript
// Bad: Multiple small calls
for (const item of items) {
  await invoke('process_item', { item });
}

// Good: Single batch call
await invoke('process_items', { items });
```

### 2. Use Streaming for Large Data

```rust
#[tauri::command]
async fn stream_large_data(
    app: AppHandle,
) -> Result<(), String> {
    // Send data in chunks via events
    for chunk in data_chunks {
        app.emit_all("data_chunk", chunk)?;
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    Ok(())
}
```

### 3. Cache Expensive Operations

```typescript
// Frontend cache
const deviceCache = new Map<string, AudioDevice[]>();

async function getDevices(refresh = false): Promise<AudioDevice[]> {
  if (!refresh && deviceCache.has('devices')) {
    return deviceCache.get('devices')!;
  }

  const devices = await tauriApi.audio.getDevices();
  deviceCache.set('devices', devices);
  return devices;
}
```

### 4. Debounce Rapid Commands

```typescript
const debouncedUpdateSettings = debounce(async (settings) => {
  await tauriApi.settings.update(settings);
}, 500);

// User changes setting frequently
debouncedUpdateSettings(newSettings);
```

## Debugging

### Enable Tauri Debugging

```rust
// In main.rs
fn main() {
    // Enable logging
    env_logger::init();

    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![...])
        .run(tauri::generate_context!())
        .expect("Error running application");
}
```

### Frontend Debugging

```typescript
// Add logging wrapper
async function invokeCommand<T>(
  command: string,
  payload?: Record<string, unknown>
): Promise<T> {
  console.log(`[IPC] Calling ${command}`, payload);
  const start = performance.now();

  try {
    const result = await invoke<T>(command, payload);
    const duration = performance.now() - start;
    console.log(`[IPC] ${command} completed in ${duration}ms`, result);
    return result;
  } catch (error) {
    console.error(`[IPC] ${command} failed:`, error);
    throw error;
  }
}
```

### Common Issues

#### 1. Serialization Errors

**Error:** `failed to serialize response`

**Cause:** Return type doesn't implement Serialize

**Solution:**
```rust
// Add Serialize derive
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MyType {
    // fields
}
```

#### 2. Type Mismatches

**Error:** `expected string, found number`

**Cause:** Frontend/backend type mismatch

**Solution:** Verify TypeScript types match Rust types exactly

#### 3. State Access Errors

**Error:** `failed to lock state`

**Cause:** State mutex deadlock

**Solution:**
```rust
// Bad: Holding lock across await
let state = context.state.lock().unwrap();
some_async_operation().await; // Deadlock!

// Good: Release lock before await
let data = {
    let state = context.state.lock().unwrap();
    state.data.clone()
};
some_async_operation().await;
```

## Security Considerations

### 1. Input Validation

Always validate command inputs:

```rust
#[tauri::command]
async fn process_user_input(input: String) -> Result<String, String> {
    // Validate input
    if input.len() > 1000 {
        return Err("Input too long".to_string());
    }

    if input.contains("../") {
        return Err("Invalid characters".to_string());
    }

    // Process safely
    Ok(sanitize(input))
}
```

### 2. Permission Checking

Implement permission-based access:

```rust
#[tauri::command]
async fn admin_command(
    context: State<'_, AppContext>,
) -> Result<String, String> {
    let state = context.state.lock().unwrap();

    if state.active_profile.permissions != "admin" {
        return Err("Insufficient permissions".to_string());
    }

    // Execute admin operation
    Ok("Success".to_string())
}
```

### 3. Secure Credential Storage

Never pass sensitive data through IPC unnecessarily:

```typescript
// Bad: Passing API key through IPC
await invoke('set_api_key', { apiKey: 'secret-key-123' });

// Good: Store directly in keychain from frontend
import { saveCredential } from '@/services/keychain';
await saveCredential('elevenlabs_api_key', apiKey);
await invoke('reload_api_credentials');
```

### 4. Rate Limiting

Implement rate limiting for expensive operations:

```rust
use std::sync::RwLock;
use std::collections::HashMap;

struct RateLimiter {
    calls: RwLock<HashMap<String, Vec<Instant>>>,
}

impl RateLimiter {
    fn check(&self, command: &str, max_calls: usize, window: Duration) -> bool {
        let mut calls = self.calls.write().unwrap();
        let now = Instant::now();

        let command_calls = calls.entry(command.to_string()).or_default();
        command_calls.retain(|&t| now.duration_since(t) < window);

        if command_calls.len() >= max_calls {
            return false;
        }

        command_calls.push(now);
        true
    }
}

#[tauri::command]
async fn rate_limited_command(
    limiter: State<'_, RateLimiter>,
) -> Result<String, String> {
    if !limiter.check("rate_limited_command", 5, Duration::from_secs(60)) {
        return Err("Rate limit exceeded".to_string());
    }

    // Execute command
    Ok("Success".to_string())
}
```

## See Also

- [API.md](./API.md) - Complete API reference
- [EXAMPLES.md](./EXAMPLES.md) - Code examples
- [Tauri Documentation](https://tauri.app/v1/guides/) - Official Tauri guides
