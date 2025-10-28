# WhisperLiveKit Tray App - Product Requirements Document

## Vision
A family-friendly system tray application that serves as the primary voice interface for home automation, enabling natural voice communication with the house through WhisperLiveKit + n8n integration.

## Core User Stories

### Primary Users
- **Family Members (All Ages)**: Need simple, reliable voice control without technical knowledge
- **Power User (You)**: Need configuration, monitoring, and debugging capabilities
- **Guests**: Should be able to use basic features without setup

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
  - Response preferences
  - Usage statistics

#### 4.2 Kid-Safe Mode
- **Command Filtering**: Whitelist/blacklist
- **Parental Controls**: Require PIN for certain actions
- **Usage Limits**: Time-based restrictions
- **Activity Log**: What was said and when

#### 4.3 Quick Actions
- **Preset Commands**: One-click buttons
  - "Good morning" (lights, coffee, news)
  - "Goodnight" (lights off, lock doors, arm security)
  - "Movie time" (dim lights, close blinds, start TV)
  - "I'm home" (unlock door, lights on, music)

### 5. Monitoring & Debugging

#### 5.1 Status Dashboard
- **Connection Status**: Server, n8n, ElevenLabs
- **Recent Transcriptions**: Last 10 with timestamps
- **Success Rate**: % of successful commands
- **Response Times**: Average latency graph
- **Error Log**: Recent errors with details

#### 5.2 Live Monitoring
- **Audio Level Meter**: Real-time visualization
- **Transcription Preview**: See text as it's processed
- **n8n Response**: Show what came back
- **Network Activity**: WebSocket status

#### 5.3 Logs Viewer
- **Filterable Logs**: By level, component, time
- **Export Logs**: Save to file for debugging
- **Clear Logs**: Reset history

### 6. Notifications

#### 6.1 System Notifications
- **Command Executed**: "Turned on living room lights"
- **Command Failed**: "Could not connect to lights"
- **Service Status**: "WhisperLiveKit started"
- **Updates Available**: "New version available"

#### 6.2 Visual Feedback
- **Toast Notifications**: Brief, non-intrusive
- **Tray Icon Badge**: Unread count
- **Color Coding**: Success (green), Error (red), Info (blue)

### 7. Security & Privacy

#### 7.1 Data Handling
- **Local Processing**: All transcription stays local
- **Encrypted Storage**: API keys in system keychain
- **No Cloud Logging**: Optional local-only logs
- **Clear History**: One-click data wipe

#### 7.2 Access Control
- **PIN Protection**: Lock settings panel
- **Guest Mode**: Limited functionality
- **Audit Trail**: Who did what, when

### 8. Installation & Updates

#### 8.1 First-Run Experience
1. Welcome screen with quick tour
2. Microphone permission request
3. Test recording + playback
4. n8n webhook setup wizard
5. ElevenLabs API key (optional)
6. Quick command examples

#### 8.2 Auto-Updates
- **Background Checks**: Daily update check
- **Notification**: "Update available"
- **One-Click Update**: Download + install + restart
- **Rollback**: Revert to previous version

## Technical Architecture

### Tech Stack
- **Frontend**: Tauri + React + TypeScript
- **Backend**: Rust (process management, system integration)
- **IPC**: Tauri commands for frontend ↔ backend
- **Storage**: SQLite for logs, settings in JSON
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

## Success Metrics

### User Experience
- **Time to First Command**: < 30 seconds from install
- **Command Success Rate**: > 95%
- **Response Latency**: < 2 seconds end-to-end
- **Crash Rate**: < 0.1% of sessions

### Family Adoption
- **Daily Active Users**: All family members
- **Commands per Day**: > 20 per household
- **Feature Discovery**: 80% use quick actions within week 1

## Future Enhancements (V2+)

### Advanced Features
- **Multi-Language Support**: Switch languages per user
- **Voice Biometrics**: Automatic user identification
- **Offline Mode**: Basic commands without internet
- **Mobile Companion**: iOS/Android remote control
- **Voice Training**: Improve accuracy over time
- **Custom Wake Words**: "Hey [Your Name]"
- **Conversation Mode**: Multi-turn dialogues
- **Whisper Mode**: Low-volume detection
- **Room Detection**: Different commands per room
- **Integration Marketplace**: Pre-built n8n workflows

### Smart Home Integrations
- **Home Assistant**: Direct integration
- **Alexa/Google Home**: Fallback routing
- **IFTTT**: Webhook triggers
- **Zigbee/Z-Wave**: Direct device control

## Development Phases

### Phase 1: MVP (Week 1-2)
- Basic tray icon with start/stop
- Settings panel (server, n8n, mic)
- Status monitoring
- Auto-start on boot

### Phase 2: Voice Response (Week 3)
- ElevenLabs integration
- Response playback
- Voice selection

### Phase 3: Family Features (Week 4)
- User profiles
- Quick actions
- Kid-safe mode

### Phase 4: Polish (Week 5-6)
- Notifications
- Logs viewer
- First-run wizard
- Auto-updates

## Open Questions

1. **Multi-User Voice Recognition**: Should we use speaker diarization to auto-switch profiles?
2. **Command Confirmation**: Always confirm destructive actions (e.g., "unlock front door")?
3. **Privacy Mode**: Temporary disable that doesn't log anything?
4. **Offline Fallback**: What commands should work without n8n?
5. **Voice Feedback**: Should the house "talk back" by default or opt-in?

## Appendix

### Example Commands
- "Turn on the living room lights"
- "Set temperature to 72 degrees"
- "What's the weather today?"
- "Start movie mode"
- "Lock all doors"
- "Play jazz music in the kitchen"
- "Remind me to take out trash at 7pm"
- "Show me the front door camera"

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
