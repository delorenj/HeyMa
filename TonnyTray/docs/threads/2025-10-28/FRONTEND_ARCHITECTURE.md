# TonnyTray Frontend Architecture

## Executive Summary

A production-ready React + TypeScript frontend for the TonnyTray system tray application. Built with Material-UI, Zustand state management, and comprehensive Tauri IPC integration for voice-controlled home automation.

## Technology Decisions

| Technology | Version | Rationale |
|------------|---------|-----------|
| React | 18.2 | Modern hooks API, concurrent features, mature ecosystem |
| TypeScript | 5.4 | Strict type safety, excellent IDE support, compile-time error detection |
| Material-UI | 5.15 | Complete component library, Material Design 3, built-in theming |
| Zustand | 4.5 | Lightweight state (~1KB), minimal boilerplate, TypeScript-first |
| React Router | 6.22 | Industry standard routing, type-safe navigation |
| Vite | 5.2 | Fast HMR, optimized builds, modern ESM-first approach |
| Tauri API | 1.5 | Native desktop integration, secure IPC, cross-platform |

## Architecture Principles

### 1. Strict Type Safety
- All code uses TypeScript strict mode
- No `any` types (ESLint enforced)
- Comprehensive type definitions for all data structures
- Type-safe Tauri IPC communication

### 2. Separation of Concerns
```
Components (UI) ← Hooks (State Logic) ← Services (Backend) ← Tauri (Native)
```

### 3. Single Source of Truth
- Zustand store as central state repository
- Backend events update state automatically
- Components subscribe to state changes

### 4. Event-Driven Updates
- Backend emits events → Frontend listeners → State updates → UI re-renders

## Project Structure

```
src/
├── components/
│   ├── Common/              # Shared components
│   │   ├── ErrorScreen.tsx
│   │   ├── LoadingScreen.tsx
│   │   └── NotificationContainer.tsx
│   ├── Dashboard/           # Main dashboard
│   │   ├── Dashboard.tsx
│   │   ├── Header.tsx
│   │   ├── RecordingControls.tsx
│   │   ├── StatusPanel.tsx
│   │   ├── TranscriptionPanel.tsx
│   │   └── QuickActions.tsx
│   ├── Profile/
│   │   └── ProfileSelector.tsx
│   └── Settings/            # Configuration panels
│       ├── Settings.tsx
│       ├── VoiceConfigTab.tsx
│       ├── IntegrationTab.tsx
│       ├── ServerConfigTab.tsx
│       └── AdvancedTab.tsx
├── hooks/
│   └── useTauriState.ts     # State management + custom hooks
├── services/
│   └── tauri.ts             # Tauri IPC service layer
├── theme/
│   └── index.ts             # MUI theme configuration
├── types/
│   └── index.ts             # TypeScript type definitions
├── App.tsx                  # Root component
└── main.tsx                 # Entry point
```

## State Management

### Zustand Store Structure

```typescript
AppState {
  // Core state
  recording: RecordingState           // idle | listening | processing | error
  audioLevel: AudioLevel | null       // Real-time audio meter
  serverStatus: ServerStatus          // Server connection state
  activeProfile: UserProfile          // Current user
  settings: AppSettings               // Configuration

  // Collections
  transcriptions: Transcription[]     // Recent history (last 50)
  notifications: Notification[]       // Toast notifications
  logs: LogEntry[]                    // Application logs
  profiles: UserProfile[]             // Available profiles

  // Metadata
  connectionStatus: ConnectionStatus  // n8n, ElevenLabs status
  statistics: Statistics              // Usage metrics
  availableDevices: AudioDevice[]     // Microphones
  availableVoices: Voice[]            // ElevenLabs voices
}
```

### Custom Hooks

**useTauriState()** - Main initialization hook
- Loads initial state from backend
- Sets up event listeners
- Manages connection lifecycle

**useRecordingControls()** - Recording operations
- startRecording(), stopRecording(), pauseRecording(), resumeRecording()
- Exposes isRecording computed state

**useSettings()** - Settings management
- updateSettings() with partial updates
- resetSettings() to defaults

**useProfiles()** - Profile management
- switchProfile(), createProfile(), updateProfile(), deleteProfile()

**useServerControls()** - Server management
- startServer(), stopServer(), restartServer()
- isConnected computed state

## Tauri IPC Integration

### Command Structure

All backend commands organized by domain:

```typescript
tauriApi = {
  recording: { start, stop, pause, resume },
  settings: { get, update, reset, export, import },
  profile: { getAll, get, switch, create, update, delete },
  audio: { getDevices, testDevice, getLevel },
  server: { start, stop, restart, getStatus, testConnection },
  integration: { testWebhook, getVoices, testTTS, sendCommand },
  logs: { get, clear, export },
  history: { getTranscriptions, getStatistics, clearHistory },
  system: { openUrl, showNotification, checkForUpdates, quit },
  events: {
    onTranscription, onStatusUpdate, onAudioLevel,
    onNotification, onError
  }
}
```

### Type Safety

```typescript
// All commands return typed promises
async function invokeCommand<T>(
  command: string,
  payload?: Record<string, unknown>
): Promise<T>

// Event listeners are fully typed
type EventCallback<T> = (event: TauriEvent<T>) => void
```

## Component Architecture

### Dashboard Components

**Dashboard** - Main layout container
- Routes to settings
- Composes all dashboard components

**Header** - App bar with navigation
- App title and icon
- Settings button

**ProfileSelector** - User switching
- Avatar display
- Profile dropdown menu
- Usage statistics

**RecordingControls** - Primary action
- Large start/stop button
- Pause/resume control
- Visual recording state
- Keyboard shortcut hint

**StatusPanel** - Service monitoring
- WhisperLiveKit connection status
- n8n connection status
- ElevenLabs connection status
- Command statistics

**TranscriptionPanel** - Recent history
- Last transcription with confidence
- n8n response display
- Recent history list

**QuickActions** - Preset commands
- Visual command buttons
- One-click execution

### Settings Components

**Settings** - Tabbed container
- Four tabs: Voice, Integration, Server, Advanced
- Back navigation to dashboard

**VoiceConfigTab**
- Model selection (tiny → large-v3)
- Language picker with auto-detect
- Microphone selection
- Audio level meter
- Voice activation settings

**IntegrationTab**
- n8n webhook URL + test
- ElevenLabs API key (secure input)
- Voice selection
- Response mode (text/voice/both)
- Test TTS button

**ServerConfigTab**
- Server URL and port
- Start/stop/restart controls
- Auto-start toggle
- Auto-restart toggle
- Python path override

**AdvancedTab**
- Auto-typing configuration
- Command prefix
- Confirmation mode
- Log level
- Reset settings (danger zone)

## Data Flow

### User Action Flow

```
User clicks button
    ↓
Component handler (onClick)
    ↓
Custom hook method (e.g., startRecording)
    ↓
Optimistic state update (setRecording('listening'))
    ↓
Service layer call (tauriApi.recording.start)
    ↓
Tauri IPC invoke
    ↓
Rust backend execution
    ↓
Backend emits event
    ↓
Event listener catches event
    ↓
Zustand state update
    ↓
Component re-renders with new state
```

### Event-Driven Updates

```typescript
// Backend emits events automatically
emit('transcription', { text, confidence, ... })
emit('status_update', { status: 'connected' })
emit('audio_level', { level: 0.5, peak: 0.8 })
emit('notification', { type: 'success', message: '...' })

// Frontend listeners update state
onTranscription → adds to transcriptions array
onStatusUpdate → updates serverStatus
onAudioLevel → updates audioLevel meter
onNotification → shows toast notification
```

## Theme System

### Material-UI Configuration

```typescript
// System-aware theme
const mode = themeMode === 'system'
  ? (systemPrefersDark ? 'dark' : 'light')
  : themeMode

// Custom color palette
colors = {
  primary: '#2196F3' (Blue),
  secondary: '#FF9800' (Orange),
  success: '#4CAF50' (Green),
  error: '#F44336' (Red)
}

// Recording state colors
idle: '#9E9E9E' (Gray)
listening: '#2196F3' (Blue + pulse animation)
processing: '#FF9800' (Orange)
error: '#F44336' (Red)
```

### Component Overrides

- Buttons: Rounded corners, no elevation
- Cards: 12px border radius, subtle shadows
- Switches: iOS-style toggle
- Tabs: Text not uppercase
- Tooltips: Theme-aware colors

## Type System

### Type Categories

1. **State Types**: AppState, AppSettings, UserProfile
2. **Entity Types**: Transcription, LogEntry, Notification
3. **Enum Types**: ServerStatus, RecordingState, WhisperModel
4. **IPC Types**: TauriCommands.*, TauriEvents.*
5. **Config Types**: VoiceConfig, IntegrationSettings

### Key Type Features

```typescript
// Union types for exhaustive checking
type ServerStatus =
  | 'disconnected'
  | 'connecting'
  | 'connected'
  | 'error'

// Namespace organization
namespace TauriCommands {
  export interface StartRecordingPayload {
    profileId: string
  }
}

// Default values fully typed
export const DEFAULT_SETTINGS: AppSettings = { ... }
```

## Performance Optimizations

1. **Selective Re-renders**: Zustand selectors prevent unnecessary updates
2. **Event Throttling**: Audio level updates limited to reasonable frequency
3. **Lazy Loading**: Non-critical data loaded on-demand
4. **Memo Usage**: Expensive computations memoized
5. **List Slicing**: History limited to last 50 items

## Error Handling

### Three-Tier Strategy

**Service Layer** - Log and propagate
```typescript
catch (error) {
  console.error('Command failed:', error);
  throw error;
}
```

**Hook Layer** - Update state, show notification
```typescript
catch (err) {
  setRecording('error');
  addNotification({ type: 'error', message: 'Recording failed' });
}
```

**Component Layer** - Update loading state
```typescript
catch (error) {
  setLoading(false);
  // Already handled by hook
}
```

## Accessibility

- Semantic HTML (proper headings, landmarks)
- ARIA labels on interactive elements
- Keyboard navigation (Tab, Enter, Space)
- Focus management
- Color contrast (WCAG AA)
- Screen reader compatible

## Development Workflow

```bash
# Install dependencies
npm install

# Development server
npm run dev

# Tauri development (with hot reload)
npm run tauri:dev

# Type checking
npm run type-check

# Linting
npm run lint

# Build
npm run build
npm run tauri:build
```

## Testing Strategy (Future)

- **Unit Tests**: Hooks, utilities (Vitest)
- **Component Tests**: User interactions (React Testing Library)
- **Integration Tests**: Tauri IPC mocking
- **E2E Tests**: Full workflows (Playwright)

## Migration to Production

Current implementation is **backend-ready**:

1. All Tauri commands defined with type contracts
2. Event listeners configured
3. State management in place

When Rust backend implements commands:
- Frontend works immediately
- No code changes needed
- Types ensure compatibility

## Security Considerations

1. **API Keys**: Stored in backend (Rust), never in frontend
2. **Input Validation**: All user inputs validated
3. **No Eval**: No dynamic code execution
4. **CSP**: Content Security Policy enforced by Tauri

## Future Enhancements

1. Route-based code splitting
2. Virtual scrolling for long lists
3. Offline capability with service worker
4. Logs viewer with filtering
5. Statistics dashboard with charts
6. Profile management UI
7. Quick action editor
8. Theme customization
9. Hotkey recorder
10. Audio waveform visualization

## Key Advantages

**Type Safety**: End-to-end TypeScript with strict checking eliminates runtime type errors

**Maintainability**: Clear separation of concerns makes code easy to understand and modify

**Scalability**: Modular architecture allows easy addition of features

**Performance**: Optimized rendering with minimal re-renders

**Developer Experience**: Excellent IDE autocomplete, clear patterns, minimal boilerplate

**User Experience**: Polished Material Design UI, responsive feedback, accessibility support

## Conclusion

This frontend architecture provides a production-ready foundation for TonnyTray that is:
- Fully type-safe
- Easy to maintain and extend
- Performant and responsive
- Accessible and user-friendly
- Ready for backend integration
