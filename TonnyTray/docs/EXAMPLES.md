# Code Examples and Recipes

Practical code examples for common TonnyTray integration scenarios.

## Table of Contents

- [Basic Usage](#basic-usage)
- [Frontend Examples](#frontend-examples)
- [Backend Examples](#backend-examples)
- [Integration Patterns](#integration-patterns)
- [Advanced Recipes](#advanced-recipes)
- [Testing Examples](#testing-examples)
- [Production Patterns](#production-patterns)

## Basic Usage

### Quick Start: Voice Recording

```typescript
import { tauriApi } from '@/services/tauri';

async function startVoiceRecording() {
  try {
    // Check server status
    const status = await tauriApi.server.getStatus();
    if (status !== 'running') {
      await tauriApi.server.start();
      // Wait for server to be ready
      await new Promise(resolve => setTimeout(resolve, 3000));
    }

    // Start recording
    await tauriApi.recording.start();
    console.log('Recording started');

    // Stop after 10 seconds
    setTimeout(async () => {
      await tauriApi.recording.stop();
      console.log('Recording stopped');
    }, 10000);

  } catch (error) {
    console.error('Failed to start recording:', error);
  }
}
```

### Quick Start: Settings Management

```typescript
async function updateMicrophone(deviceId: string) {
  const settings = await tauriApi.settings.get();

  await tauriApi.settings.update({
    voice: {
      ...settings.voice,
      microphone: deviceId
    }
  });

  console.log('Microphone updated');
}
```

## Frontend Examples

### React Hook: Voice Recording

```typescript
import { useState, useEffect } from 'react';
import { tauriApi } from '@/services/tauri';

export function useVoiceRecording() {
  const [isRecording, setIsRecording] = useState(false);
  const [transcription, setTranscription] = useState('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Listen for transcription events
    const setupListener = async () => {
      const unlisten = await tauriApi.events.onTranscription((event) => {
        setTranscription(event.payload.transcription.text);
      });
      return unlisten;
    };

    const unlistenPromise = setupListener();

    return () => {
      unlistenPromise.then(unlisten => unlisten());
    };
  }, []);

  const start = async () => {
    try {
      await tauriApi.recording.start();
      setIsRecording(true);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  };

  const stop = async () => {
    try {
      await tauriApi.recording.stop();
      setIsRecording(false);
    } catch (err) {
      setError(err.message);
    }
  };

  return {
    isRecording,
    transcription,
    error,
    start,
    stop
  };
}

// Usage in component
function VoiceRecorder() {
  const { isRecording, transcription, error, start, stop } = useVoiceRecording();

  return (
    <div>
      <button onClick={isRecording ? stop : start}>
        {isRecording ? 'Stop Recording' : 'Start Recording'}
      </button>
      {transcription && <p>Transcription: {transcription}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
```

### React Hook: Audio Level Meter

```typescript
import { useState, useEffect } from 'react';
import { tauriApi } from '@/services/tauri';

export function useAudioLevel() {
  const [level, setLevel] = useState(0);
  const [peak, setPeak] = useState(0);

  useEffect(() => {
    const setupListener = async () => {
      const unlisten = await tauriApi.events.onAudioLevel((event) => {
        setLevel(event.payload.level.level);
        setPeak(event.payload.level.peak);
      });
      return unlisten;
    };

    const unlistenPromise = setupListener();

    return () => {
      unlistenPromise.then(unlisten => unlisten());
    };
  }, []);

  return { level, peak };
}

// Usage in component
function AudioLevelMeter() {
  const { level, peak } = useAudioLevel();

  return (
    <div className="audio-meter">
      <div className="level-bar">
        <div
          className="level-fill"
          style={{ width: `${level * 100}%` }}
        />
      </div>
      <div className="peak-indicator" style={{ left: `${peak * 100}%` }} />
    </div>
  );
}
```

### React Hook: Server Status

```typescript
import { useState, useEffect } from 'react';
import { tauriApi } from '@/services/tauri';

export function useServerStatus() {
  const [status, setStatus] = useState<string>('unknown');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    // Initial status check
    checkStatus();

    // Listen for status updates
    const setupListener = async () => {
      const unlisten = await tauriApi.events.onStatusUpdate((event) => {
        setStatus(event.payload.status);
      });
      return unlisten;
    };

    const unlistenPromise = setupListener();

    // Poll status every 5 seconds
    const interval = setInterval(checkStatus, 5000);

    return () => {
      clearInterval(interval);
      unlistenPromise.then(unlisten => unlisten());
    };
  }, []);

  const checkStatus = async () => {
    try {
      const currentStatus = await tauriApi.server.getStatus();
      setStatus(currentStatus);
    } catch (error) {
      console.error('Failed to get server status:', error);
      setStatus('error');
    }
  };

  const startServer = async () => {
    setLoading(true);
    try {
      await tauriApi.server.start();
    } catch (error) {
      console.error('Failed to start server:', error);
    } finally {
      setLoading(false);
    }
  };

  const stopServer = async () => {
    setLoading(true);
    try {
      await tauriApi.server.stop();
    } catch (error) {
      console.error('Failed to stop server:', error);
    } finally {
      setLoading(false);
    }
  };

  return {
    status,
    loading,
    isRunning: status === 'running',
    startServer,
    stopServer
  };
}
```

### Settings Form with Auto-Save

```typescript
import { useState, useEffect } from 'react';
import { tauriApi } from '@/services/tauri';
import { debounce } from '@/utils/debounce';

function SettingsForm() {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    loadSettings();
  }, []);

  const loadSettings = async () => {
    try {
      const current = await tauriApi.settings.get();
      setSettings(current);
    } catch (error) {
      console.error('Failed to load settings:', error);
    }
  };

  // Debounced save function
  const saveSettings = debounce(async (newSettings: AppSettings) => {
    setSaving(true);
    try {
      await tauriApi.settings.update(newSettings);
      console.log('Settings saved');
    } catch (error) {
      console.error('Failed to save settings:', error);
    } finally {
      setSaving(false);
    }
  }, 1000);

  const updateSetting = (path: string, value: any) => {
    if (!settings) return;

    const newSettings = { ...settings };
    const keys = path.split('.');
    let current: any = newSettings;

    for (let i = 0; i < keys.length - 1; i++) {
      current = current[keys[i]];
    }

    current[keys[keys.length - 1]] = value;
    setSettings(newSettings);
    saveSettings(newSettings);
  };

  if (!settings) return <div>Loading...</div>;

  return (
    <form>
      <label>
        Whisper Model:
        <select
          value={settings.voice.model}
          onChange={(e) => updateSetting('voice.model', e.target.value)}
        >
          <option value="tiny">Tiny</option>
          <option value="base">Base</option>
          <option value="small">Small</option>
          <option value="medium">Medium</option>
          <option value="large-v3">Large-v3</option>
        </select>
      </label>

      <label>
        Language:
        <input
          type="text"
          value={settings.voice.language}
          onChange={(e) => updateSetting('voice.language', e.target.value)}
        />
      </label>

      <label>
        Voice Activation Threshold:
        <input
          type="range"
          min="0"
          max="1"
          step="0.01"
          value={settings.voice.voiceActivationThreshold}
          onChange={(e) =>
            updateSetting('voice.voiceActivationThreshold', parseFloat(e.target.value))
          }
        />
        <span>{settings.voice.voiceActivationThreshold.toFixed(2)}</span>
      </label>

      {saving && <span className="saving-indicator">Saving...</span>}
    </form>
  );
}
```

### Profile Selector

```typescript
import { useState, useEffect } from 'react';
import { tauriApi } from '@/services/tauri';

function ProfileSelector() {
  const [profiles, setProfiles] = useState<UserProfile[]>([]);
  const [activeProfile, setActiveProfile] = useState<string | null>(null);

  useEffect(() => {
    loadProfiles();
  }, []);

  const loadProfiles = async () => {
    try {
      const allProfiles = await tauriApi.profile.getAll();
      setProfiles(allProfiles);

      // Get active profile from settings
      const settings = await tauriApi.settings.get();
      // Assume we store active profile ID somewhere
      setActiveProfile(allProfiles[0]?.id || null);
    } catch (error) {
      console.error('Failed to load profiles:', error);
    }
  };

  const switchProfile = async (profileId: string) => {
    try {
      await tauriApi.profile.switch(profileId);
      setActiveProfile(profileId);
      console.log(`Switched to profile: ${profileId}`);
    } catch (error) {
      console.error('Failed to switch profile:', error);
    }
  };

  return (
    <div className="profile-selector">
      <h3>Select Profile</h3>
      <div className="profile-list">
        {profiles.map((profile) => (
          <button
            key={profile.id}
            className={profile.id === activeProfile ? 'active' : ''}
            onClick={() => switchProfile(profile.id)}
          >
            <div className="profile-avatar">
              {profile.avatar ? (
                <img src={profile.avatar} alt={profile.name} />
              ) : (
                <div className="avatar-placeholder">
                  {profile.name[0].toUpperCase()}
                </div>
              )}
            </div>
            <div className="profile-info">
              <div className="profile-name">{profile.name}</div>
              <div className="profile-permissions">{profile.permissions}</div>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
```

## Backend Examples

### Custom Tauri Command

```rust
use tauri::{command, State};
use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize)]
pub struct CustomResponse {
    success: bool,
    message: String,
    data: Option<serde_json::Value>,
}

#[tauri::command]
async fn custom_command(
    context: State<'_, AppContext>,
    param1: String,
    param2: i32,
) -> Result<CustomResponse, String> {
    info!("Custom command called with: {} {}", param1, param2);

    // Access application state
    let state = context.state.lock().map_err(|e| e.to_string())?;

    // Perform some operation
    let result = perform_operation(&param1, param2)
        .map_err(|e| e.to_string())?;

    Ok(CustomResponse {
        success: true,
        message: "Operation completed".to_string(),
        data: Some(serde_json::json!({ "result": result })),
    })
}

fn perform_operation(param1: &str, param2: i32) -> Result<String, String> {
    // Implementation
    Ok(format!("Processed: {} - {}", param1, param2))
}

// Register command in main.rs
fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            custom_command,
            // ... other commands
        ])
        .run(tauri::generate_context!())
        .expect("Error running application");
}
```

### Event Emitter

```rust
use tauri::{AppHandle, Manager};
use serde::Serialize;

#[derive(Clone, Serialize)]
pub struct ProgressEvent {
    pub percent: f32,
    pub message: String,
}

pub async fn long_task_with_progress(
    app: AppHandle,
) -> Result<(), Box<dyn std::error::Error>> {
    for i in 0..=100 {
        // Do work
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        // Emit progress
        app.emit_all("task_progress", ProgressEvent {
            percent: i as f32,
            message: format!("Processing... {}%", i),
        })?;
    }

    Ok(())
}

// Use in command
#[tauri::command]
async fn start_long_task(
    app: AppHandle,
) -> Result<String, String> {
    tokio::spawn(async move {
        if let Err(e) = long_task_with_progress(app).await {
            error!("Task failed: {}", e);
        }
    });

    Ok("Task started".to_string())
}
```

### Background Task with Cancellation

```rust
use tokio::sync::mpsc;
use std::sync::Arc;
use tokio::sync::Mutex;

pub struct BackgroundTask {
    cancel_tx: Arc<Mutex<Option<mpsc::Sender<()>>>>,
}

impl BackgroundTask {
    pub fn new() -> Self {
        Self {
            cancel_tx: Arc::new(Mutex::new(None)),
        }
    }

    pub async fn start(&self, app: AppHandle) -> Result<(), String> {
        let (cancel_tx, mut cancel_rx) = mpsc::channel(1);

        // Store cancel sender
        *self.cancel_tx.lock().await = Some(cancel_tx);

        tokio::spawn(async move {
            let mut counter = 0;

            loop {
                tokio::select! {
                    _ = cancel_rx.recv() => {
                        info!("Task cancelled");
                        break;
                    }
                    _ = tokio::time::sleep(tokio::time::Duration::from_secs(1)) => {
                        counter += 1;
                        app.emit_all("task_tick", counter).ok();
                    }
                }
            }
        });

        Ok(())
    }

    pub async fn cancel(&self) -> Result<(), String> {
        let cancel_tx = self.cancel_tx.lock().await.take();
        if let Some(tx) = cancel_tx {
            tx.send(()).await.map_err(|e| e.to_string())?;
        }
        Ok(())
    }
}

// Usage in commands
#[tauri::command]
async fn start_background_task(
    task: State<'_, BackgroundTask>,
    app: AppHandle,
) -> Result<String, String> {
    task.start(app).await?;
    Ok("Background task started".to_string())
}

#[tauri::command]
async fn cancel_background_task(
    task: State<'_, BackgroundTask>,
) -> Result<String, String> {
    task.cancel().await?;
    Ok("Background task cancelled".to_string())
}
```

## Integration Patterns

### Complete Voice Assistant Flow

```typescript
class VoiceAssistant {
  private isListening = false;
  private unlistenTranscription?: () => void;
  private unlistenStatus?: () => void;

  async initialize() {
    // Setup event listeners
    this.unlistenTranscription = await tauriApi.events.onTranscription(
      this.handleTranscription.bind(this)
    );

    this.unlistenStatus = await tauriApi.events.onStatusUpdate(
      this.handleStatusUpdate.bind(this)
    );

    // Ensure server is running
    const status = await tauriApi.server.getStatus();
    if (status !== 'running') {
      await tauriApi.server.start();
    }
  }

  async startListening() {
    if (this.isListening) return;

    try {
      await tauriApi.recording.start();
      this.isListening = true;
      console.log('Now listening...');
    } catch (error) {
      console.error('Failed to start listening:', error);
    }
  }

  async stopListening() {
    if (!this.isListening) return;

    try {
      await tauriApi.recording.stop();
      this.isListening = false;
      console.log('Stopped listening');
    } catch (error) {
      console.error('Failed to stop listening:', error);
    }
  }

  private async handleTranscription(event: any) {
    const { text, confidence } = event.payload.transcription;

    console.log(`Transcription: "${text}" (${(confidence * 100).toFixed(1)}%)`);

    // Send to n8n for processing
    try {
      const response = await tauriApi.integration.sendCommand(
        text,
        'default-profile'
      );

      console.log('n8n response:', response);

      // Speak response if ElevenLabs is enabled
      const settings = await tauriApi.settings.get();
      if (settings.integration.elevenLabsEnabled && response) {
        await this.speakResponse(response);
      }

    } catch (error) {
      console.error('Failed to process command:', error);
    }
  }

  private async speakResponse(text: string) {
    const settings = await tauriApi.settings.get();

    try {
      await tauriApi.integration.testTTS(
        text,
        settings.integration.elevenLabsVoiceId
      );
    } catch (error) {
      console.error('Failed to speak response:', error);
    }
  }

  private handleStatusUpdate(event: any) {
    const { status } = event.payload;
    console.log('Status update:', status);

    if (status === 'error') {
      this.isListening = false;
    }
  }

  async cleanup() {
    await this.stopListening();
    this.unlistenTranscription?.();
    this.unlistenStatus?.();
  }
}

// Usage
const assistant = new VoiceAssistant();
await assistant.initialize();
await assistant.startListening();
```

### Multi-Profile Command Filter

```typescript
class CommandFilter {
  private activeProfile: UserProfile | null = null;

  async initialize() {
    const profiles = await tauriApi.profile.getAll();
    this.activeProfile = profiles.find(p => p.id === 'active') || profiles[0];
  }

  async processCommand(command: string): Promise<boolean> {
    if (!this.activeProfile) {
      console.error('No active profile');
      return false;
    }

    // Check if command is allowed
    const isAllowed = this.isCommandAllowed(command);
    const isBlocked = this.isCommandBlocked(command);

    if (isBlocked) {
      console.warn(`Command blocked for ${this.activeProfile.name}: ${command}`);
      await tauriApi.system.showNotification({
        id: 'blocked-command',
        type: 'warning',
        title: 'Command Blocked',
        message: `You don't have permission to execute: ${command}`,
        timestamp: new Date().toISOString()
      });
      return false;
    }

    if (!isAllowed && this.activeProfile.permissions !== 'admin') {
      console.warn(`Command not allowed for ${this.activeProfile.name}: ${command}`);
      return false;
    }

    // Execute command
    try {
      const response = await tauriApi.integration.sendCommand(
        command,
        this.activeProfile.id
      );
      console.log('Command executed:', response);
      return true;

    } catch (error) {
      console.error('Command execution failed:', error);
      return false;
    }
  }

  private isCommandAllowed(command: string): boolean {
    if (!this.activeProfile) return false;

    return this.activeProfile.allowedCommands.some(pattern =>
      this.matchPattern(command.toLowerCase(), pattern.toLowerCase())
    );
  }

  private isCommandBlocked(command: string): boolean {
    if (!this.activeProfile) return false;

    return this.activeProfile.blockedCommands.some(pattern =>
      this.matchPattern(command.toLowerCase(), pattern.toLowerCase())
    );
  }

  private matchPattern(text: string, pattern: string): boolean {
    // Simple wildcard matching
    const regex = new RegExp(
      '^' + pattern.replace(/\*/g, '.*') + '$'
    );
    return regex.test(text);
  }
}
```

### Transcription History Manager

```typescript
class TranscriptionHistory {
  private history: Transcription[] = [];
  private maxEntries = 100;

  async loadHistory() {
    try {
      this.history = await tauriApi.history.getTranscriptions(this.maxEntries);
    } catch (error) {
      console.error('Failed to load history:', error);
    }
  }

  async exportHistory(format: 'json' | 'csv' | 'txt') {
    const data = await this.formatHistory(format);
    const blob = new Blob([data], { type: this.getMimeType(format) });
    const url = URL.createObjectURL(blob);

    // Download file
    const a = document.createElement('a');
    a.href = url;
    a.download = `transcriptions-${new Date().toISOString()}.${format}`;
    a.click();

    URL.revokeObjectURL(url);
  }

  private async formatHistory(format: string): Promise<string> {
    switch (format) {
      case 'json':
        return JSON.stringify(this.history, null, 2);

      case 'csv':
        const header = 'Timestamp,Text,Success,Response\n';
        const rows = this.history.map(t =>
          `"${t.timestamp}","${t.text}",${t.success},"${t.response || ''}"`
        ).join('\n');
        return header + rows;

      case 'txt':
        return this.history.map(t =>
          `[${t.timestamp}] ${t.text}\n${t.response ? `Response: ${t.response}\n` : ''}\n`
        ).join('\n');

      default:
        return '';
    }
  }

  private getMimeType(format: string): string {
    switch (format) {
      case 'json': return 'application/json';
      case 'csv': return 'text/csv';
      case 'txt': return 'text/plain';
      default: return 'text/plain';
    }
  }

  async searchHistory(query: string): Promise<Transcription[]> {
    const lowerQuery = query.toLowerCase();
    return this.history.filter(t =>
      t.text.toLowerCase().includes(lowerQuery) ||
      (t.response && t.response.toLowerCase().includes(lowerQuery))
    );
  }

  async clearHistory() {
    try {
      await tauriApi.history.clearHistory();
      this.history = [];
    } catch (error) {
      console.error('Failed to clear history:', error);
    }
  }
}
```

## Advanced Recipes

### Retry with Exponential Backoff

```typescript
async function retryWithBackoff<T>(
  fn: () => Promise<T>,
  maxRetries = 3,
  baseDelay = 1000
): Promise<T> {
  let lastError: Error | null = null;

  for (let i = 0; i < maxRetries; i++) {
    try {
      return await fn();
    } catch (error) {
      lastError = error as Error;
      const delay = baseDelay * Math.pow(2, i);

      console.log(`Retry ${i + 1}/${maxRetries} after ${delay}ms`);
      await new Promise(resolve => setTimeout(resolve, delay));
    }
  }

  throw lastError;
}

// Usage
const result = await retryWithBackoff(
  () => tauriApi.integration.testWebhook(url, 'test'),
  3,
  1000
);
```

### Command Queue with Rate Limiting

```typescript
class CommandQueue {
  private queue: Array<() => Promise<any>> = [];
  private processing = false;
  private minInterval = 1000; // 1 second between commands

  async enqueue<T>(fn: () => Promise<T>): Promise<T> {
    return new Promise((resolve, reject) => {
      this.queue.push(async () => {
        try {
          const result = await fn();
          resolve(result);
        } catch (error) {
          reject(error);
        }
      });

      this.process();
    });
  }

  private async process() {
    if (this.processing || this.queue.length === 0) {
      return;
    }

    this.processing = true;

    while (this.queue.length > 0) {
      const fn = this.queue.shift();
      if (fn) {
        await fn();
        await new Promise(resolve => setTimeout(resolve, this.minInterval));
      }
    }

    this.processing = false;
  }
}

// Usage
const queue = new CommandQueue();

await queue.enqueue(() => tauriApi.integration.sendCommand('cmd1', 'profile'));
await queue.enqueue(() => tauriApi.integration.sendCommand('cmd2', 'profile'));
await queue.enqueue(() => tauriApi.integration.sendCommand('cmd3', 'profile'));
```

### Persistent State Manager

```typescript
class PersistentState<T> {
  private key: string;
  private state: T;
  private saveDebounced: () => void;

  constructor(key: string, defaultState: T) {
    this.key = key;
    this.state = this.load() || defaultState;

    // Debounced save function
    this.saveDebounced = debounce(() => {
      this.save();
    }, 500);
  }

  get(): T {
    return this.state;
  }

  set(newState: Partial<T>) {
    this.state = { ...this.state, ...newState };
    this.saveDebounced();
  }

  private load(): T | null {
    try {
      const stored = localStorage.getItem(this.key);
      return stored ? JSON.parse(stored) : null;
    } catch (error) {
      console.error('Failed to load state:', error);
      return null;
    }
  }

  private save() {
    try {
      localStorage.setItem(this.key, JSON.stringify(this.state));
    } catch (error) {
      console.error('Failed to save state:', error);
    }
  }

  async syncWithBackend() {
    try {
      const backendSettings = await tauriApi.settings.get();
      this.state = backendSettings as T;
      this.save();
    } catch (error) {
      console.error('Failed to sync with backend:', error);
    }
  }
}

// Usage
interface AppState {
  theme: 'light' | 'dark';
  language: string;
  lastProfile: string;
}

const appState = new PersistentState<AppState>('app-state', {
  theme: 'dark',
  language: 'en',
  lastProfile: ''
});

appState.set({ theme: 'light' });
console.log(appState.get()); // { theme: 'light', language: 'en', lastProfile: '' }
```

## Testing Examples

### Unit Test: Tauri Command

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_get_audio_devices() {
        let audio_manager = AudioManager::new().unwrap();
        let devices = audio_manager.list_input_devices().unwrap();

        assert!(!devices.is_empty(), "Should have at least one device");
    }

    #[tokio::test]
    async fn test_audio_level_calculation() {
        let samples = vec![0.1, 0.2, 0.3, 0.4, 0.5];
        let level = AudioManager::calculate_audio_level(&samples);

        assert!(level > 0.0);
        assert!(level < 1.0);
    }
}
```

### Integration Test: Frontend

```typescript
import { describe, it, expect, beforeEach } from 'vitest';
import { tauriApi } from '@/services/tauri';

describe('Settings API', () => {
  beforeEach(async () => {
    await tauriApi.settings.reset();
  });

  it('should update settings', async () => {
    await tauriApi.settings.update({
      voice: { model: 'large-v3' }
    });

    const settings = await tauriApi.settings.get();
    expect(settings.voice.model).toBe('large-v3');
  });

  it('should handle invalid settings', async () => {
    await expect(
      tauriApi.settings.update({ invalid: 'value' } as any)
    ).rejects.toThrow();
  });
});
```

### Mock Tauri API for Testing

```typescript
export const mockTauriApi = {
  recording: {
    start: vi.fn(() => Promise.resolve()),
    stop: vi.fn(() => Promise.resolve()),
  },
  settings: {
    get: vi.fn(() => Promise.resolve({
      voice: { model: 'base', language: 'en' },
      // ... other settings
    })),
    update: vi.fn(() => Promise.resolve()),
  },
  events: {
    onTranscription: vi.fn((callback) => {
      // Simulate transcription event after 1 second
      setTimeout(() => {
        callback({
          payload: {
            transcription: {
              text: 'test transcription',
              confidence: 0.95
            }
          }
        });
      }, 1000);

      // Return unlisten function
      return Promise.resolve(() => {});
    }),
  },
};

// Usage in tests
import { mockTauriApi } from '@/mocks/tauri';

vi.mock('@/services/tauri', () => ({
  tauriApi: mockTauriApi
}));
```

## Production Patterns

### Error Boundary with Logging

```typescript
import { Component, ReactNode } from 'react';
import { tauriApi } from '@/services/tauri';

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  async componentDidCatch(error: Error, errorInfo: any) {
    console.error('Error caught by boundary:', error, errorInfo);

    // Log to backend
    try {
      await tauriApi.system.showNotification({
        id: 'error-' + Date.now(),
        type: 'error',
        title: 'Application Error',
        message: error.message,
        timestamp: new Date().toISOString()
      });
    } catch (e) {
      console.error('Failed to send error notification:', e);
    }
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="error-screen">
          <h1>Something went wrong</h1>
          <p>{this.state.error?.message}</p>
          <button onClick={() => window.location.reload()}>
            Reload Application
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}
```

### Graceful Shutdown

```typescript
async function gracefulShutdown() {
  console.log('Initiating graceful shutdown...');

  try {
    // Stop recording
    await tauriApi.recording.stop();

    // Save pending settings
    await savePendingSettings();

    // Export logs
    await tauriApi.logs.export('/tmp/tonnytray-shutdown-logs.json');

    // Stop server
    await tauriApi.server.stop();

    // Quit application
    await tauriApi.system.quit();

  } catch (error) {
    console.error('Error during shutdown:', error);
    // Force quit after 5 seconds
    setTimeout(() => {
      tauriApi.system.quit();
    }, 5000);
  }
}

// Register shutdown handler
window.addEventListener('beforeunload', (e) => {
  e.preventDefault();
  gracefulShutdown();
  return '';
});
```

### Performance Monitoring

```typescript
class PerformanceMonitor {
  private metrics = new Map<string, number[]>();

  measureCommand<T>(name: string, fn: () => Promise<T>): Promise<T> {
    const start = performance.now();

    return fn().finally(() => {
      const duration = performance.now() - start;

      if (!this.metrics.has(name)) {
        this.metrics.set(name, []);
      }

      this.metrics.get(name)!.push(duration);

      // Keep only last 100 measurements
      if (this.metrics.get(name)!.length > 100) {
        this.metrics.get(name)!.shift();
      }
    });
  }

  getStats(name: string) {
    const measurements = this.metrics.get(name) || [];
    if (measurements.length === 0) {
      return null;
    }

    const avg = measurements.reduce((a, b) => a + b, 0) / measurements.length;
    const min = Math.min(...measurements);
    const max = Math.max(...measurements);

    return { avg, min, max, count: measurements.length };
  }

  logStats() {
    console.log('Performance Stats:');
    this.metrics.forEach((_, name) => {
      const stats = this.getStats(name);
      if (stats) {
        console.log(`  ${name}: avg=${stats.avg.toFixed(2)}ms, min=${stats.min.toFixed(2)}ms, max=${stats.max.toFixed(2)}ms`);
      }
    });
  }
}

// Usage
const monitor = new PerformanceMonitor();

await monitor.measureCommand('get_settings', () =>
  tauriApi.settings.get()
);

monitor.logStats();
```

## See Also

- [API.md](./API.md) - Complete API reference
- [IPC_REFERENCE.md](./IPC_REFERENCE.md) - Tauri IPC detailed reference
- [INTEGRATION_GUIDE.md](./INTEGRATION_GUIDE.md) - External service integration
