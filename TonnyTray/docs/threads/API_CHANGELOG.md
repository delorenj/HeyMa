# API Changelog

Version history and migration guides for TonnyTray API.

## Versioning Strategy

TonnyTray follows [Semantic Versioning 2.0.0](https://semver.org/):

- **MAJOR** version: Incompatible API changes
- **MINOR** version: New functionality, backward compatible
- **PATCH** version: Bug fixes, backward compatible

## Version 0.1.0 (2025-10-16) - Beta Release

**Status:** Beta - API may change before 1.0.0 release

### Added

#### Tauri IPC Commands

**Recording Commands:**
- `start_recording` - Start voice recording with profile support
- `stop_recording` - Stop current recording session
- `pause_recording` - Pause recording (placeholder)
- `resume_recording` - Resume recording (placeholder)

**Settings Commands:**
- `get_settings` - Retrieve application settings
- `update_settings` - Update settings (partial or full)
- `reset_settings` - Reset to default settings
- `export_settings` - Export settings to JSON file
- `import_settings` - Import settings from JSON file

**Profile Commands:**
- `get_profiles` - Get all user profiles
- `get_profile` - Get specific profile by ID
- `switch_profile` - Switch active profile
- `create_profile` - Create new profile
- `update_profile` - Update existing profile
- `delete_profile` - Delete profile

**Audio Commands:**
- `get_audio_devices` - List available audio input devices
- `test_audio_device` - Test audio device functionality
- `get_audio_level` - Get current audio input level

**Server Commands:**
- `start_server` - Start WhisperLiveKit server
- `stop_server` - Stop WhisperLiveKit server
- `restart_server` - Restart server
- `get_server_status` - Get current server status
- `test_server_connection` - Test server connectivity

**Integration Commands:**
- `test_n8n_webhook` - Test n8n webhook connectivity
- `get_elevenlabs_voices` - Get available TTS voices
- `test_elevenlabs_tts` - Test text-to-speech
- `send_command` - Send command to n8n for processing

**Log Commands:**
- `get_logs` - Get application logs with filtering
- `clear_logs` - Clear all logs
- `export_logs` - Export logs to file

**History Commands:**
- `get_transcriptions` - Get transcription history
- `get_statistics` - Get usage statistics
- `clear_history` - Clear transcription history

**System Commands:**
- `open_url` - Open external URL in browser
- `show_notification` - Show system notification
- `check_for_updates` - Check for application updates
- `quit_app` - Quit the application

#### Tauri Events

- `transcription` - New transcription available
- `status_update` - Server/recording status changed
- `audio_level` - Audio level updates (real-time)
- `notification` - System notification
- `error` - Error occurred

#### Integration APIs

**WhisperLiveKit WebSocket:**
- Configuration message protocol
- Audio streaming format (PCM 16-bit, 16kHz)
- Transcription response format
- Error handling

**n8n Webhook:**
- Request format with profile and metadata
- Response format with action and data
- Test endpoint

**ElevenLabs REST API:**
- List voices endpoint
- Text-to-speech endpoint
- Streaming TTS endpoint
- Voice settings configuration

#### Database Schema

**Tables:**
- `settings` - Application settings storage
- `logs` - Application logs
- `profiles` - User profiles
- `transcriptions` - Transcription history
- `activity` - User activity tracking

#### Type System

**TypeScript Types:**
- `AppSettings` - Complete settings interface
- `UserProfile` - User profile definition
- `Transcription` - Transcription entry
- `LogEntry` - Log entry
- `AudioDevice` - Audio device info
- `ServerStatus` - Server status enum
- `RecordingState` - Recording state enum

**Rust Types:**
- Corresponding Serde-serializable types for all TypeScript interfaces
- State management types
- Error types

### Known Issues

1. `pause_recording` and `resume_recording` not implemented
2. Rate limiting not enforced on all commands
3. WebSocket reconnection not fully automatic
4. Database migration system not implemented
5. Settings validation incomplete

### Breaking Changes

None - initial release

### Deprecations

None - initial release

---

## Planned for Version 0.2.0

**Target Date:** 2025-11-30

### Planned Features

#### New Commands

- `pause_recording` - Implement actual pause functionality
- `resume_recording` - Implement resume functionality
- `get_recording_duration` - Get current recording duration
- `set_recording_quality` - Adjust recording quality settings

#### Enhanced Features

- WebSocket auto-reconnect with exponential backoff
- Settings validation with error messages
- Database migration system
- Batch command execution
- Command history and undo/redo

#### Performance Improvements

- Response caching for frequently accessed data
- Lazy loading for large datasets
- Audio processing optimization
- Database query optimization with prepared statements

#### Security Enhancements

- API key encryption in keychain
- Command permission system
- Rate limiting enforcement
- Input sanitization for all commands

### API Changes

**Potentially Breaking:**
- Settings schema may be restructured for better organization
- Profile permissions may use enum instead of string
- Error response format may be standardized

**New Types:**
- `RecordingQuality` enum
- `CommandPermission` interface
- `ValidationError` interface

---

## Planned for Version 1.0.0

**Target Date:** 2026-02-01

### Major Features

- **Plugin System** - Support for custom integrations
- **Multi-language Support** - Localization framework
- **Cloud Sync** - Optional cloud backup for settings/profiles
- **Advanced Analytics** - Usage statistics and insights
- **Voice Training** - Custom voice model training

### API Stabilization

- Finalize all command signatures
- Lock down type definitions
- Complete documentation
- Comprehensive test coverage
- Stability guarantees

---

## Migration Guides

### Migrating to 0.2.0 (Upcoming)

When 0.2.0 is released, the following changes may be required:

#### Settings Structure Changes

**Before (0.1.0):**
```typescript
interface AppSettings {
  voice: VoiceConfig;
  integration: IntegrationSettings;
  server: ServerConfig;
  advanced: AdvancedSettings;
  theme: ThemeMode;
}
```

**After (0.2.0 - Planned):**
```typescript
interface AppSettings {
  voice: VoiceConfig;
  integrations: {
    n8n: N8nConfig;
    elevenlabs: ElevenLabsConfig;
    [key: string]: any; // Plugin integrations
  };
  server: ServerConfig;
  ui: UISettings; // Replaces 'advanced' and 'theme'
}
```

**Migration:**
```typescript
// Load old settings
const oldSettings = await tauriApi.settings.get();

// Transform to new structure
const newSettings = {
  voice: oldSettings.voice,
  integrations: {
    n8n: {
      webhookUrl: oldSettings.integration.n8nWebhookUrl,
      enabled: oldSettings.integration.n8nEnabled
    },
    elevenlabs: {
      apiKey: oldSettings.integration.elevenLabsApiKey,
      voiceId: oldSettings.integration.elevenLabsVoiceId,
      enabled: oldSettings.integration.elevenLabsEnabled,
      responseMode: oldSettings.integration.responseMode
    }
  },
  server: oldSettings.server,
  ui: {
    theme: oldSettings.theme,
    autoTyping: oldSettings.advanced.autoTyping,
    typingSpeed: oldSettings.advanced.typingSpeed,
    // ... other UI settings
  }
};

// Save new settings
await tauriApi.settings.update(newSettings);
```

#### Profile Permissions Changes

**Before (0.1.0):**
```typescript
interface UserProfile {
  permissions: 'admin' | 'user' | 'kid' | 'guest';
}
```

**After (0.2.0 - Planned):**
```typescript
enum PermissionLevel {
  Admin = 'admin',
  User = 'user',
  Kid = 'kid',
  Guest = 'guest'
}

interface UserProfile {
  permissions: PermissionLevel;
  capabilities: string[]; // Fine-grained permissions
}
```

**Migration:**
```typescript
// Automatic migration handled by backend
// Frontend should update type imports:
import { PermissionLevel } from '@/types';

// Update comparisons:
// Before:
if (profile.permissions === 'admin') { }

// After:
if (profile.permissions === PermissionLevel.Admin) { }
```

---

## Deprecation Policy

### Deprecation Process

1. **Announcement** - Feature marked as deprecated in documentation
2. **Warning Period** - Minimum 3 months before removal
3. **Warning Logs** - Usage logs warnings in console
4. **Migration Guide** - Provided in changelog
5. **Removal** - Only in major version updates

### Currently Deprecated

None - initial release

---

## API Stability Guarantees

### Pre-1.0 (Current)

- **No Stability Guarantee** - API may change at any time
- **Best Effort** - Breaking changes minimized but not avoided
- **Communication** - All changes documented in changelog
- **Migration Support** - Migration guides provided for major changes

### Post-1.0 (Future)

- **Stability Guaranteed** - Breaking changes only in major versions
- **Deprecation Period** - Minimum 6 months for deprecated features
- **Semantic Versioning** - Strict adherence to SemVer
- **Long-Term Support** - Security updates for previous major version

---

## Reporting Issues

### API Bugs

If you encounter an API bug:

1. Check this changelog for known issues
2. Search existing GitHub issues
3. Create new issue with:
   - API version
   - Command/event name
   - Expected vs actual behavior
   - Minimal reproduction code
   - Error messages/logs

### Feature Requests

For new API features:

1. Check planned features in this changelog
2. Search existing feature requests
3. Create GitHub issue with:
   - Use case description
   - Proposed API signature
   - Alternative solutions considered
   - Impact on existing code

---

## Version History

| Version | Release Date | Status | Notes |
|---------|-------------|--------|-------|
| 0.1.0 | 2025-10-16 | Beta | Initial release |
| 0.2.0 | 2025-11-30 | Planned | Enhanced features |
| 1.0.0 | 2026-02-01 | Planned | Stable release |

---

## See Also

- [API.md](./API.md) - Complete API reference
- [IPC_REFERENCE.md](./IPC_REFERENCE.md) - Tauri IPC details
- [INTEGRATION_GUIDE.md](./INTEGRATION_GUIDE.md) - External services
- [EXAMPLES.md](./EXAMPLES.md) - Code examples
- [Semantic Versioning](https://semver.org/) - Versioning specification
