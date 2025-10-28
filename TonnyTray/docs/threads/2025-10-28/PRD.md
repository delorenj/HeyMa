# WhisperLiveKit Tray App - Product Requirements Document

## Vision

A system tray application that serves as the primary controls and configuration hub for the WhisperLiveKit service, auto-type script, n8n integration, and eleven labs speech and voice customization.

## Functional Requirements

### 1. System Tray Interface

#### 1.1 Tray Icon States

- **Idle** (Gray): Server running, not recording
- **Listening** (Blue pulse): Actively recording
- **Processing** (Yellow): Transcribing audio
- **Error** (Red): Service down or error state
- **Disabled** (Gray strikethrough): Service stopped

#### 1.2 Tray Menu

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
├── 🔄 Keybindings
├── ───────────────
├── 🚪 Quit
```

### 2. Settings Panel

#### 2.1 Voice Configuration

- **Model Selection**: Dropdown (tiny, base, small, medium, large-v3)
- **Language**: Auto-detect or manual selection
- **Microphone**: Device picker with live level meter
- **Push-to-Talk**: Toggle + hotkey configuration
- **Voice Activation**: Toggle + sensitivity slider

#### 2.2 Integration Settings

- **n8n Webhook URL**: Text input with test button
- **ElevenLabs API Key**: Secure input field
- **ElevenLabs Voice**: Dropdown of available voices
- **Response Mode**:
  - Text only (type responses)
  - Voice only (speak responses)
  - Both (type + speak)

#### 2.3 Server Configuration

- **Server URL**: ws://localhost:8888/asr (editable)
- **Auto-start on Boot**: Toggle
- **Auto-restart on Crash**: Toggle
- **Port**: Input field (default 8888)

#### 2.4 Advanced Settings

- **Typing Behavior**:
  - Enable/disable auto-typing
  - Typing speed slider
  - Target application filter
- **Command Prefix**: "Computer," / "Hey House," / Custom
- **Confirmation Mode**:
  - Silent (just do it)
  - Visual (notification)
  - Audio (beep/voice confirmation)

### 3. Voice Response System

#### 3.1 ElevenLabs Integration

- **Text-to-Speech**: Convert n8n responses to voice
- **Voice Selection**: Choose from user's ElevenLabs voices
- **Playback Controls**: Volume, speed, pitch
- **Queue Management**: Handle multiple responses

#### 3.2 Response Flow

```
User speaks → WhisperLiveKit → n8n → Response
                                      ↓
                            ┌─────────┴─────────┐
                            ↓                   ↓
                    ElevenLabs TTS      System Notification
                            ↓                   ↓
                    Audio Playback      Visual Feedback
```

### 4. Family-Friendly Features

#### 4.1 User Profiles

- **Multiple Profiles**: Dad, Mom, Kids, Guest
- **Per-Profile Settings**:
  - Voice recognition training
  - Allowed commands/permissions
  - Preferred agent (list a roster of agents)
  - Usage statistics

#### 4.2 Kid-Safe Mode

- **Command Filtering**: Whitelist/blacklist
- **Parental Controls**: Require PIN for certain actions
- **Usage Limits**: Time-based restrictions
- **Activity Log**: What was said and when

### 6. Notifications and Bloodbank Events (RabbitMQ)

#### 6.1 System Notifications

- **Command Executed**: "Turned on living room lights"
- **Command Failed**: "Could not connect to lights"
- **Service Status**: "WhisperLiveKit started"
- **Updates Available**: "New version available

#### 6.2 Visual Feedback

- **Toast Notifications**: Brief, non-intrusive
- **Tray Icon Badge**: Unread count
- **Color Coding**: Success (green), Error (red), Info (blue)

#### 6.3 Event Publishing

- **Trigger on Command Received**: amq.topic / thread.tonny.prompt

> [!NOTE] Tonny Always First
> If someone sets their default agent to anyone other than Tonny, tonny is still the receiver - but upon recognizing the voice and matching to another agent, tonny will silently delegate to the preferred agent. In other words - nothing gets past Tonny.

## Technical Architecture

### Tech Stack

- **Frontend**: Tauri + React + TypeScript
- **Backend**: Rust (process management, system integration)
- **IPC**: Tauri commands for frontend ↔ backend
- **Storage**: Postgres (native install on host) for logs, settings in JSON
- **Audio**: rodio for playback, cpal for recording

### Key Components

```
┌─────────────────────────────────────┐
│         Tauri Tray App              │
├─────────────────────────────────────┤
│  React UI (Settings, Dashboard)     │
├─────────────────────────────────────┤
│  Rust Backend                       │
│  ├── Process Manager                │
│  │   ├── WhisperLiveKit Server      │
│  │   └── Auto-Type Client           │
│  ├── WebSocket Client (n8n)         │
│  ├── ElevenLabs Client              │
│  ├── Audio Manager                  │
│  └── System Tray Controller         │
└─────────────────────────────────────┘
```

### State Management

```rust
struct AppState {
    recording: bool,
    server_status: ServerStatus,
    last_transcription: String,
    active_profile: UserProfile,
    settings: AppSettings,
}
```

## UI/UX Design Principles

### Design Goals

1. **Invisible When Working**: Minimal interruption
2. **Obvious When Needed**: Clear status, easy access
3. **Family-Friendly**: Simple enough for kids, powerful for power users
4. **Accessible**: Keyboard shortcuts, screen reader support

### Visual Design

- **Modern, Clean**: Material Design 3 / Fluent 2
- **Dark/Light Mode**: Auto-switch with system
- **Animations**: Subtle, purposeful (recording pulse, success checkmark)
- **Typography**: Clear, readable (Inter or SF Pro)

### Interaction Patterns

- **Hotkeys**: Global shortcuts for common actions
- **Drag & Drop**: Reorder quick actions
- **Right-Click**: Context menus everywhere
- **Double-Click Tray**: Open dashboard

## Open Questions

1. **Multi-User Voice Recognition**: Should we use speaker diarization to auto-switch profiles? \_Yes
2. **Command Confirmation**: Always confirm destructive actions (e.g., "unlock front door")? \_This is out of scope! This tray app shouldn’t cross into telling agents how to act, what to say or not say.
3. **Privacy Mode**: Temporary disable that doesn't log anything? \_Nah
4. **Offline Fallback**: What commands should work without n8n? \_Nothing
5. **Voice Feedback**: Should the house "talk back" by default or opt-in? \_Again, thinking too much. Just want to start and stop services, monitor, use as dash, etc

### Configuration File Example

```json
{
  "server": {
    "url": "ws://localhost:8888/asr",
    "model": "base",
    "language": "en",
    "auto_start": true
  },
  "n8n": {
    "webhook_url": "https://n8n.delo.sh/webhook/ask-tonny"
  },
  "elevenlabs": {
    "api_key": "***",
    "voice_id": "21m00Tcm4TlvDq8ikWAM",
    "enabled": true
  },
  "profiles": [
    {
      "name": "Dad",
      "permissions": "admin",
      "voice_id": "custom_voice_1"
    }
  ]
}
```
