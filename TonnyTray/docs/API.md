# TonnyTray API Reference

Complete API documentation for TonnyTray, a WhisperLiveKit system tray application providing voice-to-text transcription with AI integration.

**Version:** 0.1.0
**Last Updated:** 2025-10-16
**API Stability:** Beta

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Tauri IPC Commands](#tauri-ipc-commands)
- [Tauri Events](#tauri-events)
- [REST APIs](#rest-apis)
- [WebSocket Protocols](#websocket-protocols)
- [Database API](#database-api)
- [Integration APIs](#integration-apis)
- [Error Handling](#error-handling)
- [Rate Limiting](#rate-limiting)
- [Authentication](#authentication)
- [Versioning](#versioning)

## Overview

TonnyTray provides multiple API layers for different use cases:

1. **Tauri IPC** - Frontend-to-backend communication (primary interface)
2. **WebSocket** - Real-time communication with WhisperLiveKit and n8n
3. **REST** - External integrations (n8n, ElevenLabs)
4. **Database** - Local SQLite storage
5. **System** - Native OS integrations (audio, keychain, notifications)

### API Design Principles

- **Type Safety** - Full TypeScript/Rust type definitions
- **Async First** - All I/O operations are asynchronous
- **Error Transparency** - Detailed error messages with context
- **Event-Driven** - Real-time updates via Tauri events
- **Offline-First** - Works without internet connectivity

## Architecture

```
┌─────────────────────────────────────────────────┐
│             Frontend (React + TS)               │
├─────────────────────────────────────────────────┤
│           Tauri IPC Layer (Commands)            │
├─────────────────────────────────────────────────┤
│          Rust Backend (src-tauri)               │
│  ┌──────────┬──────────┬──────────┬──────────┐ │
│  │  Audio   │ Process  │ Database │ Keychain │ │
│  │ Manager  │ Manager  │  Layer   │  Layer   │ │
│  └──────────┴──────────┴──────────┴──────────┘ │
├─────────────────────────────────────────────────┤
│         External Services Layer                 │
│  ┌──────────┬──────────┬──────────────────┐   │
│  │ Whisper  │   n8n    │   ElevenLabs     │   │
│  │ LiveKit  │ Webhook  │      TTS         │   │
│  └──────────┴──────────┴──────────────────┘   │
└─────────────────────────────────────────────────┘
```

## Tauri IPC Commands

Tauri IPC commands are the primary way the frontend communicates with the Rust backend. All commands are asynchronous and return Promises.

### Recording Commands

#### `start_recording`

Start voice recording with the active profile.

**Parameters:**
- `profileId` (string, optional) - Profile ID to use. Defaults to active profile.

**Returns:** `Promise<string>`

**Success Response:**
```json
"Recording started with PID: 12345"
```

**Error Responses:**
- `Failed to start recording: No audio device selected`
- `Failed to start recording: Server not running`
- `Failed to start recording: Process already running`

**Example:**
```typescript
import { tauriApi } from '@/services/tauri';

try {
  const result = await tauriApi.recording.start('profile-123');
  console.log(result); // "Recording started with PID: 12345"
} catch (error) {
  console.error('Failed to start recording:', error);
}
```

**Rate Limit:** 1 call per second
**Requires:** Server running, audio device configured

---

#### `stop_recording`

Stop current recording session.

**Parameters:**
- `save` (boolean, optional) - Whether to save the recording. Default: `true`

**Returns:** `Promise<string>`

**Success Response:**
```json
"Recording stopped"
```

**Example:**
```typescript
await tauriApi.recording.stop(true);
```

**Side Effects:**
- Triggers `status_update` event
- Updates transcription history
- Saves recording if `save` is true

---

#### `pause_recording`

Pause current recording without stopping.

**Returns:** `Promise<void>`

**Example:**
```typescript
await tauriApi.recording.pause();
```

**Note:** Not currently implemented in backend. Returns error.

---

#### `resume_recording`

Resume paused recording.

**Returns:** `Promise<void>`

**Example:**
```typescript
await tauriApi.recording.resume();
```

**Note:** Not currently implemented in backend. Returns error.

---

### Settings Commands

#### `get_settings`

Retrieve current application settings.

**Returns:** `Promise<AppSettings>`

**Response Schema:**
```typescript
interface AppSettings {
  voice: {
    model: WhisperModel;
    language: string;
    autoDetectLanguage: boolean;
    microphone: string | null;
    pushToTalk: boolean;
    pushToTalkHotkey: string;
    voiceActivation: boolean;
    voiceActivationThreshold: number;
  };
  integration: {
    n8nWebhookUrl: string;
    elevenLabsApiKey: string;
    elevenLabsVoiceId: string;
    elevenLabsEnabled: boolean;
    responseMode: 'text' | 'voice' | 'both';
    volume: number;
    speed: number;
    pitch: number;
  };
  server: {
    url: string;
    port: number;
    autoStart: boolean;
    autoRestart: boolean;
    pythonPath: string | null;
  };
  advanced: {
    autoTyping: boolean;
    typingSpeed: number;
    targetApplicationFilter: string;
    commandPrefix: string;
    confirmationMode: 'silent' | 'visual' | 'audio';
    logLevel: 'debug' | 'info' | 'warn' | 'error';
    maxLogEntries: number;
  };
  theme: 'light' | 'dark' | 'system';
}
```

**Example:**
```typescript
const settings = await tauriApi.settings.get();
console.log(settings.voice.model); // "base"
```

---

#### `update_settings`

Update application settings (partial or full update).

**Parameters:**
- `settings` (Partial<AppSettings>) - Settings to update

**Returns:** `Promise<void>`

**Example:**
```typescript
await tauriApi.settings.update({
  voice: {
    model: 'large-v3',
    language: 'es'
  }
});
```

**Side Effects:**
- Saves to config file
- Triggers settings validation
- May restart server if server settings changed

---

#### `reset_settings`

Reset all settings to default values.

**Returns:** `Promise<void>`

**Warning:** This action cannot be undone.

**Example:**
```typescript
await tauriApi.settings.reset();
```

---

#### `export_settings`

Export settings to JSON file.

**Parameters:**
- `path` (string) - File path to export to

**Returns:** `Promise<void>`

**Example:**
```typescript
await tauriApi.settings.export('/path/to/settings.json');
```

---

#### `import_settings`

Import settings from JSON file.

**Parameters:**
- `path` (string) - File path to import from

**Returns:** `Promise<void>`

**Example:**
```typescript
await tauriApi.settings.import('/path/to/settings.json');
```

**Validation:** Settings file must match AppSettings schema

---

### Profile Commands

#### `get_profiles`

Get all user profiles.

**Returns:** `Promise<UserProfile[]>`

**Response Schema:**
```typescript
interface UserProfile {
  id: string;
  name: string;
  avatar?: string;
  permissions: 'admin' | 'user' | 'kid' | 'guest';
  settings: Partial<AppSettings>;
  allowedCommands: string[];
  blockedCommands: string[];
  usageStats: {
    totalCommands: number;
    successfulCommands: number;
    lastUsed: string | null;
  };
}
```

**Example:**
```typescript
const profiles = await tauriApi.profile.getAll();
```

---

#### `get_profile`

Get specific profile by ID.

**Parameters:**
- `profileId` (string) - Profile ID

**Returns:** `Promise<UserProfile>`

**Error:** `Profile not found: {profileId}`

---

#### `switch_profile`

Switch to different user profile.

**Parameters:**
- `profileId` (string) - Profile ID to switch to

**Returns:** `Promise<void>`

**Side Effects:**
- Updates active profile
- Applies profile settings
- Triggers `status_update` event

---

#### `create_profile`

Create new user profile.

**Parameters:**
- `profile` (Omit<UserProfile, 'id'>) - Profile data

**Returns:** `Promise<UserProfile>` - Created profile with ID

**Example:**
```typescript
const newProfile = await tauriApi.profile.create({
  name: 'John Doe',
  permissions: 'user',
  settings: {},
  allowedCommands: ['turn on *', 'turn off *'],
  blockedCommands: ['unlock *'],
  usageStats: {
    totalCommands: 0,
    successfulCommands: 0,
    lastUsed: null
  }
});
```

---

#### `update_profile`

Update existing profile.

**Parameters:**
- `profileId` (string) - Profile ID
- `profile` (Partial<UserProfile>) - Fields to update

**Returns:** `Promise<void>`

---

#### `delete_profile`

Delete user profile.

**Parameters:**
- `profileId` (string) - Profile ID

**Returns:** `Promise<void>`

**Warning:** Cannot delete the last remaining profile

---

### Audio Commands

#### `get_audio_devices`

Get list of available audio input devices.

**Returns:** `Promise<AudioDevice[]>`

**Response Schema:**
```typescript
interface AudioDevice {
  id: string;
  name: string;
  isDefault: boolean;
  sampleRate: number;
  channels: number;
}
```

**Example:**
```typescript
const devices = await tauriApi.audio.getDevices();
const defaultDevice = devices.find(d => d.isDefault);
```

---

#### `test_audio_device`

Test audio device functionality.

**Parameters:**
- `deviceId` (string) - Device ID to test

**Returns:** `Promise<boolean>` - True if device works

**Example:**
```typescript
const works = await tauriApi.audio.testDevice('device-123');
if (!works) {
  console.error('Device not working');
}
```

---

#### `get_audio_level`

Get current audio input level.

**Returns:** `Promise<AudioLevel>`

**Response Schema:**
```typescript
interface AudioLevel {
  timestamp: number;
  level: number;    // RMS level (0.0 - 1.0)
  peak: number;     // Peak level (0.0 - 1.0)
}
```

**Example:**
```typescript
const level = await tauriApi.audio.getLevel();
console.log(`Audio level: ${(level.level * 100).toFixed(1)}%`);
```

---

### Server Commands

#### `start_server`

Start WhisperLiveKit server.

**Returns:** `Promise<string>` - Success message with PID

**Example:**
```typescript
const result = await tauriApi.server.start();
console.log(result); // "Server started with PID: 12345"
```

**Timeout:** 30 seconds
**Requirements:** Python environment, WhisperLiveKit installed

---

#### `stop_server`

Stop WhisperLiveKit server.

**Returns:** `Promise<string>`

**Example:**
```typescript
await tauriApi.server.stop();
```

**Graceful Shutdown:** Waits up to 10 seconds for clean shutdown

---

#### `restart_server`

Restart WhisperLiveKit server.

**Returns:** `Promise<string>`

**Example:**
```typescript
await tauriApi.server.restart();
```

**Note:** Equivalent to stop + start

---

#### `get_server_status`

Get current server status.

**Returns:** `Promise<ServerStatus>`

**Response Schema:**
```typescript
enum ServerStatus {
  Stopped = 'stopped',
  Starting = 'starting',
  Running = 'running',
  Stopping = 'stopping',
  Error = 'error'
}
```

**Example:**
```typescript
const status = await tauriApi.server.getStatus();
if (status === 'running') {
  console.log('Server is ready');
}
```

---

#### `test_server_connection`

Test server connectivity.

**Returns:** `Promise<boolean>`

**Example:**
```typescript
const connected = await tauriApi.server.testConnection();
```

**Timeout:** 5 seconds

---

### Integration Commands

#### `test_n8n_webhook`

Test n8n webhook connectivity.

**Parameters:**
- `url` (string) - Webhook URL
- `message` (string, optional) - Test message. Default: "Test message"

**Returns:** `Promise<boolean>`

**Example:**
```typescript
const works = await tauriApi.integration.testWebhook(
  'https://n8n.example.com/webhook/test',
  'Hello from TonnyTray'
);
```

**Timeout:** 5 seconds

---

#### `get_elevenlabs_voices`

Get available ElevenLabs voices.

**Returns:** `Promise<Array<{ id: string; name: string }>>`

**Example:**
```typescript
const voices = await tauriApi.integration.getVoices();
voices.forEach(v => console.log(`${v.name}: ${v.id}`));
```

**Requirements:** ElevenLabs API key configured

---

#### `test_elevenlabs_tts`

Test ElevenLabs text-to-speech.

**Parameters:**
- `text` (string) - Text to speak
- `voiceId` (string) - Voice ID to use

**Returns:** `Promise<void>`

**Example:**
```typescript
await tauriApi.integration.testTTS(
  'Hello, this is a test',
  'voice-id-123'
);
```

**Side Effect:** Plays audio through default output device

---

#### `send_command`

Send command to n8n for processing.

**Parameters:**
- `command` (string) - Command text
- `profileId` (string) - Profile ID

**Returns:** `Promise<string>` - Response from n8n

**Example:**
```typescript
const response = await tauriApi.integration.sendCommand(
  'turn on the lights',
  'profile-123'
);
console.log(response); // n8n response
```

---

### Log Commands

#### `get_logs`

Get application logs with filtering.

**Parameters:**
- `level` (string, optional) - Filter by log level
- `limit` (number, optional) - Max entries. Default: 100
- `offset` (number, optional) - Pagination offset. Default: 0

**Returns:** `Promise<LogEntry[]>`

**Response Schema:**
```typescript
interface LogEntry {
  id: string;
  timestamp: string;
  level: 'debug' | 'info' | 'warn' | 'error';
  component: string;
  message: string;
  metadata?: Record<string, unknown>;
}
```

**Example:**
```typescript
const errorLogs = await tauriApi.logs.get('error', 50);
```

---

#### `clear_logs`

Clear all application logs.

**Returns:** `Promise<void>`

**Warning:** This action cannot be undone

---

#### `export_logs`

Export logs to file.

**Parameters:**
- `path` (string) - File path to export to

**Returns:** `Promise<void>`

**Example:**
```typescript
await tauriApi.logs.export('/path/to/logs.json');
```

---

### History Commands

#### `get_transcriptions`

Get transcription history.

**Parameters:**
- `limit` (number, optional) - Max entries. Default: 50

**Returns:** `Promise<Transcription[]>`

**Response Schema:**
```typescript
interface Transcription {
  id: string;
  timestamp: string;
  text: string;
  confidence: number;
  duration: number;
  profileId: string;
  success: boolean;
  response?: string;
}
```

---

#### `get_statistics`

Get usage statistics.

**Returns:** `Promise<Statistics>`

**Response Schema:**
```typescript
interface Statistics {
  totalCommands: number;
  successfulCommands: number;
  failedCommands: number;
  averageResponseTime: number;
  uptime: number;
  lastError?: string;
}
```

---

#### `clear_history`

Clear transcription history.

**Returns:** `Promise<void>`

---

### System Commands

#### `open_url`

Open external URL in default browser.

**Parameters:**
- `url` (string) - URL to open

**Returns:** `Promise<void>`

**Example:**
```typescript
await tauriApi.system.openUrl('https://github.com/your-repo');
```

---

#### `show_notification`

Show system notification.

**Parameters:**
- `notification` (Notification) - Notification object

**Example:**
```typescript
await tauriApi.system.showNotification({
  id: 'notif-1',
  type: 'success',
  title: 'Recording Started',
  message: 'Voice recording is now active',
  timestamp: new Date().toISOString(),
  duration: 3000
});
```

---

#### `check_for_updates`

Check for application updates.

**Returns:** `Promise<{ available: boolean; version?: string }>`

---

#### `quit_app`

Quit the application.

**Returns:** `Promise<void>`

**Warning:** Stops all running processes

---

## Tauri Events

Events are emitted by the Rust backend and can be subscribed to from the frontend.

### `transcription`

Emitted when new transcription is available.

**Payload:**
```typescript
interface TranscriptionEvent {
  transcription: Transcription;
}
```

**Example:**
```typescript
const unlisten = await tauriApi.events.onTranscription((event) => {
  console.log('New transcription:', event.payload.transcription.text);
});

// Later: cleanup
unlisten();
```

---

### `status_update`

Emitted when server or recording status changes.

**Payload:**
```typescript
interface StatusUpdateEvent {
  status: ServerStatus;
}
```

**Example:**
```typescript
await tauriApi.events.onStatusUpdate((event) => {
  console.log('Status changed:', event.payload.status);
});
```

---

### `audio_level`

Emitted periodically with audio level updates.

**Payload:**
```typescript
interface AudioLevelEvent {
  level: AudioLevel;
}
```

**Frequency:** ~10 times per second when recording

---

### `notification`

Emitted for system notifications.

**Payload:**
```typescript
interface NotificationEvent {
  notification: Notification;
}
```

---

### `error`

Emitted when errors occur.

**Payload:**
```typescript
interface ErrorEvent {
  error: string;
  component: string;
}
```

**Example:**
```typescript
await tauriApi.events.onError((event) => {
  console.error(`Error in ${event.payload.component}:`, event.payload.error);
});
```

---

## REST APIs

TonnyTray integrates with external REST APIs.

See [INTEGRATION_GUIDE.md](./INTEGRATION_GUIDE.md) for details on:
- n8n Webhook API
- ElevenLabs TTS API

---

## WebSocket Protocols

See [INTEGRATION_GUIDE.md](./INTEGRATION_GUIDE.md) for details on:
- WhisperLiveKit WebSocket protocol
- n8n WebSocket communication

---

## Database API

TonnyTray uses SQLite for local storage. See [Database Schema](#database-schema) below.

### Database Schema

#### `settings` table
```sql
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### `logs` table
```sql
CREATE TABLE logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TIMESTAMP NOT NULL,
  level TEXT NOT NULL,
  component TEXT NOT NULL,
  message TEXT NOT NULL,
  metadata TEXT
);
```

#### `profiles` table
```sql
CREATE TABLE profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  permissions TEXT NOT NULL,
  voice_id TEXT,
  allowed_commands TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### `transcriptions` table
```sql
CREATE TABLE transcriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TIMESTAMP NOT NULL,
  profile_id INTEGER,
  text TEXT NOT NULL,
  confidence REAL,
  speaker_id TEXT,
  sent_to_n8n BOOLEAN DEFAULT 0,
  success BOOLEAN DEFAULT 1,
  response TEXT,
  FOREIGN KEY (profile_id) REFERENCES profiles(id)
);
```

#### `activity` table
```sql
CREATE TABLE activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TIMESTAMP NOT NULL,
  profile_id INTEGER,
  action TEXT NOT NULL,
  details TEXT,
  success BOOLEAN DEFAULT 1,
  FOREIGN KEY (profile_id) REFERENCES profiles(id)
);
```

---

## Error Handling

All Tauri commands follow a consistent error handling pattern:

**Error Format:**
```typescript
interface TauriError {
  message: string;
  code?: string;
  details?: any;
}
```

**Common Error Codes:**
- `NOT_INITIALIZED` - Service not initialized
- `INVALID_PARAMS` - Invalid parameters
- `NOT_FOUND` - Resource not found
- `PERMISSION_DENIED` - Insufficient permissions
- `TIMEOUT` - Operation timed out
- `NETWORK_ERROR` - Network connectivity issue
- `INTERNAL_ERROR` - Unexpected error

**Example Error Handling:**
```typescript
try {
  await tauriApi.server.start();
} catch (error) {
  if (error.message.includes('already running')) {
    console.log('Server already started');
  } else {
    console.error('Failed to start server:', error);
  }
}
```

---

## Rate Limiting

### Command Rate Limits

| Command | Rate Limit | Cooldown |
|---------|-----------|----------|
| `start_recording` | 1/second | 1s |
| `stop_recording` | 1/second | 1s |
| `test_n8n_webhook` | 5/minute | 12s |
| `test_elevenlabs_tts` | 10/minute | 6s |
| `get_logs` | 10/second | - |
| Other commands | No limit | - |

**Rate Limit Exceeded Response:**
```
Error: Rate limit exceeded. Please wait 2 seconds.
```

---

## Authentication

### API Key Storage

Sensitive credentials (ElevenLabs API key) are stored securely:

- **macOS/Linux:** System keychain via `keyring` crate
- **Windows:** Windows Credential Manager

**Never** store API keys in plain text configuration files.

---

## Versioning

TonnyTray follows Semantic Versioning 2.0.0:

- **MAJOR:** Breaking API changes
- **MINOR:** New features, backward compatible
- **PATCH:** Bug fixes, backward compatible

Current API version: `0.1.0` (Beta)

### Deprecation Policy

Deprecated APIs will:
1. Be marked with `@deprecated` in documentation
2. Log warnings when used
3. Be removed in next major version
4. Provide migration path in changelog

See [API_CHANGELOG.md](./API_CHANGELOG.md) for version history.

---

## See Also

- [IPC_REFERENCE.md](./IPC_REFERENCE.md) - Detailed IPC reference
- [INTEGRATION_GUIDE.md](./INTEGRATION_GUIDE.md) - External service integration
- [EXAMPLES.md](./EXAMPLES.md) - Code examples and recipes
- [API_CHANGELOG.md](./API_CHANGELOG.md) - API version history
