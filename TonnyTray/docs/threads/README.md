# TonnyTray API Documentation

Welcome to the TonnyTray API documentation. This directory contains comprehensive documentation for all APIs and integration points.

## Documentation Structure

### Core API Documentation

1. **[API.md](./API.md)** - Complete API Reference
   - Overview of all API layers
   - Tauri IPC commands (Frontend ↔ Backend)
   - Tauri events (Backend → Frontend)
   - REST API endpoints
   - WebSocket protocols
   - Database schema
   - Error handling and rate limiting
   - Authentication and security

2. **[IPC_REFERENCE.md](./IPC_REFERENCE.md)** - Tauri IPC Detailed Reference
   - IPC architecture and design
   - Command registration patterns
   - Type system mapping (TypeScript ↔ Rust)
   - Event lifecycle and subscription
   - Advanced IPC patterns
   - Performance optimization
   - Debugging and troubleshooting
   - Security considerations

3. **[INTEGRATION_GUIDE.md](./INTEGRATION_GUIDE.md)** - External Service Integration
   - WhisperLiveKit WebSocket protocol
   - n8n webhook integration
   - ElevenLabs TTS API
   - RabbitMQ messaging (optional)
   - SQLite database API
   - Security best practices
   - Troubleshooting guides

4. **[EXAMPLES.md](./EXAMPLES.md)** - Code Examples and Recipes
   - Basic usage examples
   - React hooks and components
   - Rust backend patterns
   - Integration scenarios
   - Advanced recipes
   - Testing examples
   - Production patterns

5. **[API_CHANGELOG.md](./API_CHANGELOG.md)** - API Versioning and Changes
   - Version history
   - Migration guides
   - Breaking changes
   - Deprecation notices
   - Planned features

## Quick Start

### For Frontend Developers

Start here to integrate with TonnyTray from your React/TypeScript application:

1. Read **[API.md - Tauri IPC Commands](./API.md#tauri-ipc-commands)** for available commands
2. Check **[EXAMPLES.md - Frontend Examples](./EXAMPLES.md#frontend-examples)** for React hooks
3. Review **[IPC_REFERENCE.md - Type System](./IPC_REFERENCE.md#type-system)** for TypeScript types

**Quick Example:**
```typescript
import { tauriApi } from '@/services/tauri';

// Start recording
await tauriApi.recording.start();

// Listen for transcriptions
const unlisten = await tauriApi.events.onTranscription((event) => {
  console.log(event.payload.transcription.text);
});
```

### For Backend Developers

Start here to extend TonnyTray's Rust backend:

1. Read **[IPC_REFERENCE.md - Command Registration](./IPC_REFERENCE.md#command-registration)**
2. Check **[EXAMPLES.md - Backend Examples](./EXAMPLES.md#backend-examples)** for Rust patterns
3. Review **[API.md - Database Schema](./API.md#database-schema)** for data storage

**Quick Example:**
```rust
#[tauri::command]
async fn custom_command(
    context: State<'_, AppContext>,
    param: String,
) -> Result<String, String> {
    // Implementation
    Ok(format!("Result: {}", param))
}
```

### For Integration Developers

Start here to integrate TonnyTray with external services:

1. Read **[INTEGRATION_GUIDE.md](./INTEGRATION_GUIDE.md)** for your service (n8n, ElevenLabs, etc.)
2. Check **[EXAMPLES.md - Integration Patterns](./EXAMPLES.md#integration-patterns)**
3. Review **[API.md - Integration APIs](./API.md#integration-apis)**

**Quick Example:**
```typescript
// Send command to n8n
const response = await tauriApi.integration.sendCommand(
  'turn on the lights',
  'profile-id'
);
console.log(response);
```

## API Overview

### Architecture Layers

```
┌─────────────────────────────────────────────────┐
│          React Frontend (TypeScript)            │
│                                                 │
│  - UI Components                                │
│  - React Hooks                                  │
│  - State Management                             │
│                                                 │
├─────────────────────────────────────────────────┤
│          Tauri IPC Layer                        │
│                                                 │
│  - Commands (Frontend → Backend)                │
│  - Events (Backend → Frontend)                  │
│  - Type Safety (TS ↔ Rust)                      │
│                                                 │
├─────────────────────────────────────────────────┤
│          Rust Backend (Tauri)                   │
│                                                 │
│  - Audio Manager (CPAL, Rodio)                  │
│  - Process Manager (WhisperLiveKit)             │
│  - Database Layer (SQLite)                      │
│  - Keychain (Secure Storage)                    │
│  - State Management                             │
│                                                 │
├─────────────────────────────────────────────────┤
│          External Services                      │
│                                                 │
│  - WhisperLiveKit (WebSocket)                   │
│  - n8n (HTTP Webhook)                           │
│  - ElevenLabs (REST API)                        │
│  - RabbitMQ (AMQP) [Optional]                   │
│                                                 │
└─────────────────────────────────────────────────┘
```

### Key Features

- **Type-Safe Communication** - Full TypeScript/Rust type definitions
- **Async/Await** - All operations are asynchronous
- **Event-Driven** - Real-time updates via Tauri events
- **Secure Storage** - Credentials in system keychain
- **Offline-First** - Works without internet connectivity
- **Multi-Profile** - Support for multiple users
- **Extensible** - Plugin-ready architecture

## Common Use Cases

### 1. Voice Recording and Transcription

**Documentation:**
- [API.md - Recording Commands](./API.md#recording-commands)
- [EXAMPLES.md - Voice Recording](./EXAMPLES.md#react-hook-voice-recording)

**Key APIs:**
- `start_recording()` - Start voice recording
- `stop_recording()` - Stop recording
- `onTranscription()` - Listen for transcriptions

### 2. Settings Management

**Documentation:**
- [API.md - Settings Commands](./API.md#settings-commands)
- [EXAMPLES.md - Settings Form](./EXAMPLES.md#settings-form-with-auto-save)

**Key APIs:**
- `get_settings()` - Get current settings
- `update_settings()` - Update settings
- `export_settings()` / `import_settings()` - Backup/restore

### 3. Multi-User Profiles

**Documentation:**
- [API.md - Profile Commands](./API.md#profile-commands)
- [EXAMPLES.md - Profile Selector](./EXAMPLES.md#profile-selector)

**Key APIs:**
- `get_profiles()` - List all profiles
- `switch_profile()` - Change active profile
- `create_profile()` / `update_profile()` - Manage profiles

### 4. n8n Integration

**Documentation:**
- [INTEGRATION_GUIDE.md - n8n](./INTEGRATION_GUIDE.md#n8n-integration)
- [EXAMPLES.md - Voice Assistant](./EXAMPLES.md#complete-voice-assistant-flow)

**Key APIs:**
- `send_command()` - Send transcription to n8n
- `test_n8n_webhook()` - Test connectivity

### 5. Text-to-Speech

**Documentation:**
- [INTEGRATION_GUIDE.md - ElevenLabs](./INTEGRATION_GUIDE.md#elevenlabs-tts-integration)
- [API.md - Integration Commands](./API.md#integration-commands)

**Key APIs:**
- `get_elevenlabs_voices()` - List available voices
- `test_elevenlabs_tts()` - Speak text

## API Stability

**Current Version:** 0.1.0 (Beta)

- APIs may change before 1.0.0 release
- Breaking changes will be documented in [API_CHANGELOG.md](./API_CHANGELOG.md)
- Migration guides will be provided for significant changes
- Feedback welcome via GitHub issues

See [API_CHANGELOG.md - API Stability Guarantees](./API_CHANGELOG.md#api-stability-guarantees) for details.

## Error Handling

All APIs follow consistent error handling:

```typescript
try {
  const result = await tauriApi.command.execute();
  // Success
} catch (error) {
  console.error('Command failed:', error.message);
  // error.code - Error code (if available)
  // error.details - Additional details
}
```

See [API.md - Error Handling](./API.md#error-handling) for error codes and patterns.

## Rate Limiting

Some commands have rate limits to prevent abuse:

| Command | Rate Limit |
|---------|-----------|
| `start_recording` | 1/second |
| `stop_recording` | 1/second |
| `test_n8n_webhook` | 5/minute |
| `test_elevenlabs_tts` | 10/minute |

See [API.md - Rate Limiting](./API.md#rate-limiting) for complete list.

## Security

- **API Keys** - Stored in system keychain (not config files)
- **HTTPS Only** - All external API calls use HTTPS
- **Input Validation** - All inputs validated before processing
- **Permissions** - Profile-based permission system

See [INTEGRATION_GUIDE.md - Security Best Practices](./INTEGRATION_GUIDE.md#security-best-practices) for details.

## Contributing

### Documentation Improvements

To improve these docs:

1. Fork the repository
2. Edit documentation files in `TonnyTray/docs/`
3. Follow existing formatting and structure
4. Submit pull request with clear description

### API Feedback

To provide API feedback:

1. Open GitHub issue with `api-feedback` label
2. Describe use case and current limitation
3. Suggest improvement or alternative
4. Include code examples if applicable

## Support

### Getting Help

- **Documentation** - Start with this directory
- **Examples** - Check [EXAMPLES.md](./EXAMPLES.md)
- **Issues** - Search [GitHub Issues](https://github.com/your-repo/issues)
- **Discussions** - Ask in [GitHub Discussions](https://github.com/your-repo/discussions)

### Common Issues

See troubleshooting sections in:
- [INTEGRATION_GUIDE.md - Troubleshooting](./INTEGRATION_GUIDE.md#troubleshooting)
- [IPC_REFERENCE.md - Debugging](./IPC_REFERENCE.md#debugging)

## License

See LICENSE file in repository root.

## Related Documentation

- **README.md** - Project overview and setup
- **CONTRIBUTING.md** - Contribution guidelines
- **ARCHITECTURE.md** - System architecture (if available)

---

**Last Updated:** 2025-10-16
**API Version:** 0.1.0
**Documentation Version:** 1.0.0
