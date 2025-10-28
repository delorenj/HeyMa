# Integration Guide

Complete guide for integrating TonnyTray with external services: WhisperLiveKit, n8n, ElevenLabs, and RabbitMQ.

## Table of Contents

- [Overview](#overview)
- [WhisperLiveKit Integration](#whisperlivekit-integration)
- [n8n Integration](#n8n-integration)
- [ElevenLabs TTS Integration](#elevenlabs-tts-integration)
- [RabbitMQ Integration](#rabbitmq-integration)
- [Database Integration](#database-integration)
- [Security Best Practices](#security-best-practices)
- [Troubleshooting](#troubleshooting)

## Overview

TonnyTray integrates with multiple external services to provide a complete voice assistant system:

```
┌──────────────────────────────────────────────────────┐
│                    TonnyTray                         │
│                                                      │
│  ┌────────────┐  ┌────────────┐  ┌──────────────┐  │
│  │  Frontend  │  │  Backend   │  │   Database   │  │
│  │   (React)  │  │   (Rust)   │  │   (SQLite)   │  │
│  └────────────┘  └────────────┘  └──────────────┘  │
│         │              │                 │          │
└─────────┼──────────────┼─────────────────┼──────────┘
          │              │                 │
          ▼              ▼                 ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  WhisperKit  │  │     n8n      │  │  ElevenLabs  │
│ (WebSocket)  │  │  (Webhook)   │  │  (REST API)  │
└──────────────┘  └──────────────┘  └──────────────┘
```

## WhisperLiveKit Integration

WhisperLiveKit provides real-time speech-to-text transcription via WebSocket.

### WebSocket Protocol

**Connection URL:** `ws://localhost:8888/asr`

**Connection Flow:**
1. Client establishes WebSocket connection
2. Client sends configuration message
3. Client streams audio data
4. Server responds with transcriptions
5. Connection remains open for continuous transcription

### Message Formats

#### 1. Configuration Message (Client → Server)

Sent immediately after connection:

```json
{
  "type": "config",
  "data": {
    "language": "en",
    "model": "base",
    "task": "transcribe",
    "use_vad": true,
    "vad_threshold": 0.5
  }
}
```

**Parameters:**
- `language` (string) - Language code (e.g., "en", "es", "fr")
- `model` (string) - Whisper model size: "tiny", "base", "small", "medium", "large-v3"
- `task` (string) - "transcribe" or "translate"
- `use_vad` (boolean) - Enable voice activity detection
- `vad_threshold` (number) - VAD sensitivity (0.0-1.0)

#### 2. Audio Data Message (Client → Server)

Stream audio data continuously:

```javascript
// Audio data format: PCM 16-bit, 16kHz, mono
const audioData = new Int16Array(samples);
websocket.send(audioData.buffer);
```

**Audio Requirements:**
- Format: PCM (uncompressed)
- Sample Rate: 16000 Hz
- Bit Depth: 16-bit
- Channels: 1 (mono)
- Chunk Size: 1024-4096 samples recommended

#### 3. Transcription Response (Server → Client)

Server sends transcription results:

```json
{
  "type": "transcription",
  "data": {
    "text": "Hello, how are you?",
    "is_final": true,
    "confidence": 0.95,
    "start_time": 0.0,
    "end_time": 2.5,
    "language": "en"
  }
}
```

**Response Fields:**
- `text` (string) - Transcribed text
- `is_final` (boolean) - Whether transcription is complete
- `confidence` (number) - Confidence score (0.0-1.0)
- `start_time` (number) - Start time in seconds
- `end_time` (number) - End time in seconds
- `language` (string) - Detected language

#### 4. Error Response (Server → Client)

```json
{
  "type": "error",
  "data": {
    "code": "MODEL_LOAD_FAILED",
    "message": "Failed to load Whisper model",
    "details": "Model file not found"
  }
}
```

### Implementation Example

#### Rust Backend (WebSocket Client)

```rust
use tokio_tungstenite::{connect_async, tungstenite::Message};
use futures_util::{SinkExt, StreamExt};

pub struct WhisperLiveKitClient {
    url: String,
}

impl WhisperLiveKitClient {
    pub fn new(url: String) -> Self {
        Self { url }
    }

    pub async fn connect(&self) -> Result<(), Box<dyn std::error::Error>> {
        let (ws_stream, _) = connect_async(&self.url).await?;
        let (mut write, mut read) = ws_stream.split();

        // Send configuration
        let config = serde_json::json!({
            "type": "config",
            "data": {
                "language": "en",
                "model": "base",
                "task": "transcribe",
                "use_vad": true
            }
        });
        write.send(Message::Text(config.to_string())).await?;

        // Listen for responses
        while let Some(message) = read.next().await {
            match message? {
                Message::Text(text) => {
                    let response: serde_json::Value = serde_json::from_str(&text)?;
                    if response["type"] == "transcription" {
                        println!("Transcription: {}", response["data"]["text"]);
                    }
                }
                _ => {}
            }
        }

        Ok(())
    }

    pub async fn send_audio(&mut self, audio_data: Vec<i16>) -> Result<(), Box<dyn std::error::Error>> {
        // Convert to bytes and send
        let bytes: Vec<u8> = audio_data
            .iter()
            .flat_map(|&sample| sample.to_le_bytes())
            .collect();

        // Send via WebSocket (implementation depends on connection state)
        Ok(())
    }
}
```

#### TypeScript Frontend

```typescript
class WhisperLiveKitClient {
  private ws: WebSocket | null = null;

  async connect(url: string): Promise<void> {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(url);

      this.ws.onopen = () => {
        // Send configuration
        this.ws?.send(JSON.stringify({
          type: 'config',
          data: {
            language: 'en',
            model: 'base',
            task: 'transcribe',
            use_vad: true
          }
        }));
        resolve();
      };

      this.ws.onmessage = (event) => {
        const response = JSON.parse(event.data);
        if (response.type === 'transcription') {
          console.log('Transcription:', response.data.text);
        }
      };

      this.ws.onerror = (error) => reject(error);
    });
  }

  sendAudio(audioBuffer: Int16Array): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(audioBuffer.buffer);
    }
  }

  disconnect(): void {
    this.ws?.close();
    this.ws = null;
  }
}
```

### Audio Processing

Convert browser audio to required format:

```typescript
class AudioProcessor {
  private audioContext: AudioContext;
  private processor: ScriptProcessorNode | null = null;

  constructor() {
    this.audioContext = new AudioContext({ sampleRate: 16000 });
  }

  async startRecording(callback: (audioData: Int16Array) => void): Promise<void> {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        sampleRate: 16000,
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true
      }
    });

    const source = this.audioContext.createMediaStreamSource(stream);
    this.processor = this.audioContext.createScriptProcessor(4096, 1, 1);

    this.processor.onaudioprocess = (e) => {
      const float32Data = e.inputBuffer.getChannelData(0);

      // Convert Float32 to Int16
      const int16Data = new Int16Array(float32Data.length);
      for (let i = 0; i < float32Data.length; i++) {
        const s = Math.max(-1, Math.min(1, float32Data[i]));
        int16Data[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }

      callback(int16Data);
    };

    source.connect(this.processor);
    this.processor.connect(this.audioContext.destination);
  }

  stopRecording(): void {
    this.processor?.disconnect();
    this.processor = null;
  }
}
```

### Error Handling

Common errors and solutions:

| Error Code | Description | Solution |
|-----------|-------------|----------|
| `CONNECTION_FAILED` | Cannot connect to server | Check server is running on correct port |
| `MODEL_LOAD_FAILED` | Whisper model not found | Verify model files exist |
| `INVALID_AUDIO_FORMAT` | Wrong audio format | Check sample rate (16kHz) and format (PCM) |
| `VAD_TIMEOUT` | No voice detected | Adjust VAD threshold or check microphone |
| `TRANSCRIPTION_FAILED` | Transcription error | Check audio quality and language setting |

## n8n Integration

n8n provides workflow automation via webhooks.

### Webhook Configuration

**Webhook URL Format:** `https://your-n8n-instance.com/webhook/{workflow-id}`

### Request Format

TonnyTray sends transcriptions to n8n:

```json
{
  "timestamp": "2025-10-16T12:34:56.789Z",
  "text": "turn on the living room lights",
  "source": "tonnytray",
  "profile": {
    "id": "profile-123",
    "name": "John Doe",
    "permissions": "admin"
  },
  "metadata": {
    "confidence": 0.95,
    "language": "en",
    "duration": 2.5
  }
}
```

### Response Format

n8n should respond with:

```json
{
  "success": true,
  "message": "Lights turned on",
  "action": "lights_on",
  "data": {
    "room": "living_room",
    "devices": ["light_1", "light_2"]
  }
}
```

**Response Fields:**
- `success` (boolean) - Whether action succeeded
- `message` (string, optional) - Human-readable response
- `action` (string, optional) - Action identifier
- `data` (object, optional) - Additional data

### Implementation

#### Rust Backend

```rust
use reqwest::Client;
use serde::{Deserialize, Serialize};

#[derive(Serialize)]
pub struct N8nRequest {
    timestamp: String,
    text: String,
    source: String,
    profile: ProfileInfo,
    metadata: TranscriptionMetadata,
}

#[derive(Deserialize)]
pub struct N8nResponse {
    success: bool,
    message: Option<String>,
    action: Option<String>,
    data: Option<serde_json::Value>,
}

pub struct N8nClient {
    webhook_url: String,
    client: Client,
}

impl N8nClient {
    pub fn new(webhook_url: String) -> Self {
        Self {
            webhook_url,
            client: Client::new(),
        }
    }

    pub async fn send_transcription(
        &self,
        text: &str,
        profile: &ProfileInfo,
    ) -> Result<N8nResponse, Box<dyn std::error::Error>> {
        let payload = N8nRequest {
            timestamp: chrono::Utc::now().to_rfc3339(),
            text: text.to_string(),
            source: "tonnytray".to_string(),
            profile: profile.clone(),
            metadata: TranscriptionMetadata {
                confidence: 0.95,
                language: "en".to_string(),
                duration: 2.5,
            },
        };

        let response = self.client
            .post(&self.webhook_url)
            .json(&payload)
            .timeout(std::time::Duration::from_secs(10))
            .send()
            .await?;

        let status = response.status();
        let body = response.text().await?;

        if !status.is_success() {
            return Err(format!("n8n error: {} - {}", status, body).into());
        }

        let n8n_response: N8nResponse = serde_json::from_str(&body)?;
        Ok(n8n_response)
    }

    pub async fn test_connection(&self) -> Result<bool, Box<dyn std::error::Error>> {
        let test_payload = serde_json::json!({
            "test": true,
            "timestamp": chrono::Utc::now().to_rfc3339(),
        });

        let response = self.client
            .post(&self.webhook_url)
            .json(&test_payload)
            .timeout(std::time::Duration::from_secs(5))
            .send()
            .await?;

        Ok(response.status().is_success())
    }
}
```

### n8n Workflow Example

Example n8n workflow for handling TonnyTray requests:

```json
{
  "nodes": [
    {
      "name": "Webhook",
      "type": "n8n-nodes-base.webhook",
      "parameters": {
        "path": "tonnytray-webhook",
        "responseMode": "responseNode",
        "options": {}
      },
      "position": [250, 300]
    },
    {
      "name": "Parse Command",
      "type": "n8n-nodes-base.code",
      "parameters": {
        "jsCode": "const text = $json.text.toLowerCase();\nlet action = null;\nlet data = {};\n\nif (text.includes('turn on')) {\n  action = 'lights_on';\n  if (text.includes('living room')) {\n    data.room = 'living_room';\n  }\n} else if (text.includes('turn off')) {\n  action = 'lights_off';\n}\n\nreturn { action, data, originalText: text };"
      },
      "position": [450, 300]
    },
    {
      "name": "Execute Action",
      "type": "n8n-nodes-base.httpRequest",
      "parameters": {
        "url": "http://your-home-automation/api/{{$json.action}}",
        "method": "POST",
        "jsonParameters": true,
        "bodyParametersJson": "={{$json.data}}"
      },
      "position": [650, 300]
    },
    {
      "name": "Respond",
      "type": "n8n-nodes-base.respondToWebhook",
      "parameters": {
        "respondWith": "json",
        "responseBody": "{\n  \"success\": true,\n  \"message\": \"Action executed\",\n  \"action\": \"{{$json.action}}\"\n}"
      },
      "position": [850, 300]
    }
  ],
  "connections": {
    "Webhook": { "main": [[{ "node": "Parse Command" }]] },
    "Parse Command": { "main": [[{ "node": "Execute Action" }]] },
    "Execute Action": { "main": [[{ "node": "Respond" }]] }
  }
}
```

### Best Practices

1. **Timeout Handling** - Set reasonable timeouts (5-10 seconds)
2. **Retry Logic** - Implement exponential backoff for failed requests
3. **Error Logging** - Log all webhook failures for debugging
4. **Rate Limiting** - Limit requests to prevent overwhelming n8n
5. **Security** - Use HTTPS and authentication headers

## ElevenLabs TTS Integration

ElevenLabs provides high-quality text-to-speech via REST API.

### API Endpoint

**Base URL:** `https://api.elevenlabs.io/v1`

### Authentication

Include API key in all requests:

```
xi-api-key: your-api-key-here
```

### API Methods

#### 1. List Voices

**Endpoint:** `GET /voices`

**Request:**
```bash
curl -X GET 'https://api.elevenlabs.io/v1/voices' \
  -H 'xi-api-key: YOUR_API_KEY'
```

**Response:**
```json
{
  "voices": [
    {
      "voice_id": "21m00Tcm4TlvDq8ikWAM",
      "name": "Rachel",
      "category": "premade",
      "description": "Calm, clear American female voice"
    },
    {
      "voice_id": "ErXwobaYiN019PkySvjV",
      "name": "Antoni",
      "category": "premade",
      "description": "Well-rounded American male voice"
    }
  ]
}
```

#### 2. Text-to-Speech

**Endpoint:** `POST /text-to-speech/{voice_id}`

**Request:**
```bash
curl -X POST 'https://api.elevenlabs.io/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM' \
  -H 'xi-api-key: YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "Hello, this is a test of text to speech.",
    "model_id": "eleven_monolingual_v1",
    "voice_settings": {
      "stability": 0.5,
      "similarity_boost": 0.75,
      "style": 0.0,
      "use_speaker_boost": true
    }
  }'
```

**Response:** Binary audio data (MP3 format)

#### 3. Streaming TTS

**Endpoint:** `POST /text-to-speech/{voice_id}/stream`

Same as above, but streams audio chunks for lower latency.

### Implementation

#### Rust Backend

```rust
use reqwest::Client;
use serde::{Deserialize, Serialize};

#[derive(Serialize)]
pub struct TtsRequest {
    text: String,
    model_id: String,
    voice_settings: VoiceSettings,
}

#[derive(Serialize)]
pub struct VoiceSettings {
    stability: f32,
    similarity_boost: f32,
    style: Option<f32>,
    use_speaker_boost: Option<bool>,
}

impl Default for VoiceSettings {
    fn default() -> Self {
        Self {
            stability: 0.5,
            similarity_boost: 0.75,
            style: Some(0.0),
            use_speaker_boost: Some(true),
        }
    }
}

#[derive(Deserialize)]
pub struct Voice {
    pub voice_id: String,
    pub name: String,
    pub category: Option<String>,
    pub description: Option<String>,
}

pub struct ElevenLabsClient {
    api_key: String,
    client: Client,
    base_url: String,
}

impl ElevenLabsClient {
    pub fn new(api_key: String) -> Self {
        Self {
            api_key,
            client: Client::new(),
            base_url: "https://api.elevenlabs.io/v1".to_string(),
        }
    }

    pub async fn list_voices(&self) -> Result<Vec<Voice>, Box<dyn std::error::Error>> {
        let url = format!("{}/voices", self.base_url);

        let response = self.client
            .get(&url)
            .header("xi-api-key", &self.api_key)
            .send()
            .await?;

        #[derive(Deserialize)]
        struct VoicesResponse {
            voices: Vec<Voice>,
        }

        let voices_response: VoicesResponse = response.json().await?;
        Ok(voices_response.voices)
    }

    pub async fn text_to_speech(
        &self,
        text: &str,
        voice_id: &str,
        settings: Option<VoiceSettings>,
    ) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
        let url = format!("{}/text-to-speech/{}", self.base_url, voice_id);

        let request = TtsRequest {
            text: text.to_string(),
            model_id: "eleven_monolingual_v1".to_string(),
            voice_settings: settings.unwrap_or_default(),
        };

        let response = self.client
            .post(&url)
            .header("xi-api-key", &self.api_key)
            .header("Content-Type", "application/json")
            .json(&request)
            .send()
            .await?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await?;
            return Err(format!("ElevenLabs error: {} - {}", status, body).into());
        }

        let audio_bytes = response.bytes().await?.to_vec();
        Ok(audio_bytes)
    }
}
```

### Voice Settings Parameters

| Parameter | Range | Description |
|-----------|-------|-------------|
| `stability` | 0.0-1.0 | Higher = more consistent, lower = more expressive |
| `similarity_boost` | 0.0-1.0 | How closely to match original voice |
| `style` | 0.0-1.0 | Style exaggeration (optional) |
| `use_speaker_boost` | boolean | Boost speaker similarity (recommended: true) |

### Rate Limits

Free tier limits:
- 10,000 characters/month
- 3 voices
- Standard latency

Paid tier:
- Higher character limits
- More voices
- Lower latency streaming

### Error Codes

| Status | Error | Solution |
|--------|-------|----------|
| 401 | Invalid API key | Check API key is correct |
| 402 | Insufficient credits | Upgrade plan or wait for reset |
| 422 | Invalid voice_id | Use a valid voice ID from list |
| 429 | Rate limit exceeded | Implement backoff and retry |
| 500 | Server error | Retry with exponential backoff |

## RabbitMQ Integration

RabbitMQ can be used for asynchronous message queuing between TonnyTray and other services.

### Connection Configuration

```rust
use lapin::{
    options::*, types::FieldTable, BasicProperties, Connection,
    ConnectionProperties,
};

pub async fn connect_rabbitmq(
    url: &str
) -> Result<Connection, Box<dyn std::error::Error>> {
    let conn = Connection::connect(
        url,
        ConnectionProperties::default(),
    ).await?;

    Ok(conn)
}
```

### Queue Setup

```rust
pub async fn setup_queue(
    conn: &Connection,
    queue_name: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let channel = conn.create_channel().await?;

    channel.queue_declare(
        queue_name,
        QueueDeclareOptions::default(),
        FieldTable::default(),
    ).await?;

    Ok(())
}
```

### Publishing Messages

```rust
pub async fn publish_transcription(
    conn: &Connection,
    queue_name: &str,
    transcription: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let channel = conn.create_channel().await?;

    let payload = serde_json::json!({
        "timestamp": chrono::Utc::now().to_rfc3339(),
        "text": transcription,
        "source": "tonnytray"
    }).to_string();

    channel.basic_publish(
        "",
        queue_name,
        BasicPublishOptions::default(),
        payload.as_bytes(),
        BasicProperties::default(),
    ).await?;

    Ok(())
}
```

## Database Integration

TonnyTray uses SQLite for local data persistence.

### Database Location

**Default Path:** `~/.config/tonnytray/tonnytray.db`

### Schema

See [API.md - Database Schema](./API.md#database-schema) for complete schema.

### Query Examples

#### Insert Transcription

```rust
use rusqlite::{Connection, params};

pub fn insert_transcription(
    conn: &Connection,
    text: &str,
    success: bool,
) -> Result<i64, rusqlite::Error> {
    conn.execute(
        "INSERT INTO transcriptions (timestamp, text, success)
         VALUES (?1, ?2, ?3)",
        params![chrono::Utc::now(), text, success],
    )?;

    Ok(conn.last_insert_rowid())
}
```

#### Query Recent Transcriptions

```rust
use rusqlite::Row;

pub fn get_recent_transcriptions(
    conn: &Connection,
    limit: usize,
) -> Result<Vec<Transcription>, rusqlite::Error> {
    let mut stmt = conn.prepare(
        "SELECT id, timestamp, text, success, response
         FROM transcriptions
         ORDER BY timestamp DESC
         LIMIT ?1"
    )?;

    let rows = stmt.query_map(params![limit], |row| {
        Ok(Transcription {
            timestamp: row.get(1)?,
            text: row.get(2)?,
            success: row.get(3)?,
            response: row.get(4)?,
        })
    })?;

    rows.collect()
}
```

### Backup and Restore

```rust
pub fn backup_database(
    source_path: &str,
    backup_path: &str,
) -> Result<(), std::io::Error> {
    std::fs::copy(source_path, backup_path)?;
    Ok(())
}
```

## Security Best Practices

### 1. API Key Storage

Use system keychain for sensitive credentials:

```rust
use keyring::Entry;

pub fn store_api_key(
    service: &str,
    key: &str,
) -> Result<(), keyring::Error> {
    let entry = Entry::new(service, "api_key")?;
    entry.set_password(key)?;
    Ok(())
}

pub fn retrieve_api_key(
    service: &str,
) -> Result<String, keyring::Error> {
    let entry = Entry::new(service, "api_key")?;
    entry.get_password()
}
```

### 2. HTTPS Only

Always use HTTPS for external API calls:

```rust
let client = reqwest::Client::builder()
    .https_only(true)
    .build()?;
```

### 3. Input Validation

Validate all external input:

```rust
pub fn validate_webhook_url(url: &str) -> Result<(), String> {
    if !url.starts_with("https://") {
        return Err("Webhook URL must use HTTPS".to_string());
    }

    url::Url::parse(url)
        .map_err(|e| format!("Invalid URL: {}", e))?;

    Ok(())
}
```

### 4. Rate Limiting

Implement client-side rate limiting:

```rust
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

pub struct RateLimiter {
    last_call: Mutex<Option<Instant>>,
    min_interval: Duration,
}

impl RateLimiter {
    pub async fn check(&self) -> Result<(), String> {
        let mut last = self.last_call.lock().await;

        if let Some(last_time) = *last {
            let elapsed = last_time.elapsed();
            if elapsed < self.min_interval {
                let wait = self.min_interval - elapsed;
                return Err(format!("Rate limited. Wait {}ms", wait.as_millis()));
            }
        }

        *last = Some(Instant::now());
        Ok(())
    }
}
```

## Troubleshooting

### WhisperLiveKit Issues

**Problem:** Cannot connect to WebSocket

**Solutions:**
1. Check server is running: `ps aux | grep whisperlivekit`
2. Verify port is correct: `netstat -an | grep 8888`
3. Check firewall settings
4. Verify URL format: `ws://localhost:8888/asr`

**Problem:** Audio not transcribing

**Solutions:**
1. Check audio format (16kHz, PCM, mono)
2. Verify VAD threshold (try 0.3-0.5)
3. Check microphone permissions
4. Test with different Whisper model

### n8n Issues

**Problem:** Webhook not responding

**Solutions:**
1. Test with curl: `curl -X POST https://n8n.example.com/webhook/test -d '{"test":true}'`
2. Check n8n workflow is active
3. Verify webhook URL is correct
4. Check n8n logs for errors

**Problem:** Timeout errors

**Solutions:**
1. Increase timeout setting (default: 10s)
2. Optimize n8n workflow
3. Use async webhook response mode

### ElevenLabs Issues

**Problem:** API key rejected

**Solutions:**
1. Verify API key is correct
2. Check account is active
3. Ensure no extra whitespace in key

**Problem:** Rate limit exceeded

**Solutions:**
1. Check usage at elevenlabs.io dashboard
2. Implement request queuing
3. Upgrade plan if needed

**Problem:** Poor audio quality

**Solutions:**
1. Adjust voice settings (stability, similarity_boost)
2. Try different voice model
3. Check text formatting (punctuation helps)

### Database Issues

**Problem:** Database locked

**Solutions:**
1. Close other connections
2. Enable WAL mode (already enabled)
3. Check file permissions

**Problem:** Slow queries

**Solutions:**
1. Add indexes to frequently queried columns
2. Use LIMIT on large result sets
3. Run VACUUM periodically

## See Also

- [API.md](./API.md) - Complete API reference
- [EXAMPLES.md](./EXAMPLES.md) - Integration examples
- [WhisperLiveKit Documentation](https://github.com/collabora/whisperlive)
- [n8n Documentation](https://docs.n8n.io/)
- [ElevenLabs API Docs](https://docs.elevenlabs.io/)
