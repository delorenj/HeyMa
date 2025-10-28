# TonnyTray Frontend Architecture

A modern React + TypeScript frontend for the TonnyTray system tray application, built with Material-UI and Tauri integration.

## Architecture Overview

### Tech Stack

- **Framework**: React 18.2
- **Language**: TypeScript 5.4 (Strict mode)
- **UI Library**: Material-UI (MUI) 5.15
- **State Management**: Zustand 4.5
- **Routing**: React Router DOM 6.22
- **Build Tool**: Vite 5.2
- **Desktop Integration**: Tauri 1.5

### Project Structure

```
TonnyTray/
├── src/
│   ├── components/
│   │   ├── Common/              # Shared UI components
│   │   │   ├── ErrorScreen.tsx
│   │   │   ├── LoadingScreen.tsx
│   │   │   └── NotificationContainer.tsx
│   │   ├── Dashboard/           # Main dashboard components
│   │   │   ├── Dashboard.tsx    # Main layout
│   │   │   ├── Header.tsx
│   │   │   ├── RecordingControls.tsx
│   │   │   ├── StatusPanel.tsx
│   │   │   ├── TranscriptionPanel.tsx
│   │   │   └── QuickActions.tsx
│   │   ├── Profile/             # User profile components
│   │   │   └── ProfileSelector.tsx
│   │   └── Settings/            # Settings panel components
│   │       ├── Settings.tsx     # Tabbed settings container
│   │       ├── VoiceConfigTab.tsx
│   │       ├── IntegrationTab.tsx
│   │       ├── ServerConfigTab.tsx
│   │       └── AdvancedTab.tsx
│   ├── hooks/                   # Custom React hooks
│   │   └── useTauriState.ts    # State management & Tauri integration
│   ├── services/                # External service integrations
│   │   └── tauri.ts            # Tauri IPC command wrappers
│   ├── theme/                   # Theme configuration
│   │   └── index.ts            # MUI theme setup & utilities
│   ├── types/                   # TypeScript type definitions
│   │   └── index.ts            # Core type definitions
│   ├── App.tsx                 # Root application component
│   ├── main.tsx                # Application entry point
│   └── index.css               # Global styles
├── package.json
├── tsconfig.json               # TypeScript configuration
├── vite.config.ts              # Vite build configuration
└── README.md
```

## Core Architecture Decisions

### 1. State Management with Zustand

**Why Zustand?**
- Lightweight (~1KB) vs Context API boilerplate
- No provider wrapping required
- Built-in devtools integration
- TypeScript-first API
- Minimal re-renders with selector-based subscriptions

**Store Structure:**
```typescript
useAppStore = {
  // Core state
  recording: RecordingState
  serverStatus: ServerStatus
  activeProfile: UserProfile
  settings: AppSettings

  // Collections
  transcriptions: Transcription[]
  notifications: Notification[]
  logs: LogEntry[]

  // Actions (setters)
  setRecording()
  addTranscription()
  updateSettings()
  // ... etc
}
```

### 2. Tauri IPC Integration

**Service Layer Pattern:**
- All Tauri commands abstracted in `services/tauri.ts`
- Type-safe wrappers for invoke/listen operations
- Organized by domain (recording, settings, audio, etc.)
- Consistent error handling

**Event-Driven Updates:**
```typescript
// Backend emits events → Frontend listens
onTranscription() → adds to transcriptions[]
onStatusUpdate() → updates serverStatus
onAudioLevel() → updates live audio meter
onNotification() → shows toast notification
```

### 3. Component Organization

**Feature-Based Structure:**
- Components grouped by feature (Dashboard, Settings, Profile)
- Common/shared components in `Common/`
- Each component is self-contained with its own logic

**Composition Pattern:**
```
Dashboard (layout)
  ├─ Header (navigation)
  ├─ ProfileSelector (user management)
  ├─ RecordingControls (primary action)
  ├─ StatusPanel (service status)
  ├─ TranscriptionPanel (recent history)
  └─ QuickActions (preset commands)
```

### 4. Type Safety

**Strict TypeScript Configuration:**
- `strict: true`
- `noUnusedLocals: true`
- `noImplicitReturns: true`
- `exactOptionalPropertyTypes: true`

**Comprehensive Type Definitions:**
- All state shapes defined in `types/index.ts`
- Tauri IPC payloads typed via namespaces
- No `any` types (ESLint enforced)
- Union types for enums (e.g., `ServerStatus`, `RecordingState`)

### 5. Theme System

**Material-UI Theme Provider:**
- System-aware dark/light mode
- Custom color palette for recording states
- Consistent component styling overrides
- Responsive design patterns

**Theme Hook:**
```typescript
useAppTheme() → {
  theme: Theme,
  mode: 'light' | 'dark'
}
```

### 6. Custom Hooks

**useTauriState()**: Main initialization hook
- Loads initial state from backend
- Sets up event listeners
- Manages connection lifecycle

**useRecordingControls()**: Recording operations
- `startRecording()`, `stopRecording()`, `pauseRecording()`
- Exposes `isRecording` computed state

**useSettings()**: Settings management
- `updateSettings()` with partial updates
- `resetSettings()` to defaults

**useProfiles()**: Profile switching
- `switchProfile()`, `createProfile()`, `updateProfile()`

**useServerControls()**: Server management
- `startServer()`, `stopServer()`, `restartServer()`

## Key Features

### 1. Dashboard View

**Real-Time Status Monitoring:**
- Connection status for WhisperLiveKit, n8n, ElevenLabs
- Live audio level visualization
- Command success rate statistics

**Recording Controls:**
- Large start/stop button with visual feedback
- Pause/resume functionality
- Keyboard shortcut support (Ctrl+Shift+V)
- Animated recording state indicator

**Transcription History:**
- Last transcription with confidence score
- Response from n8n displayed inline
- Recent history (last 3 transcriptions)

**Quick Actions:**
- Preset commands for common scenarios
- Visual icon representation
- One-click execution

### 2. Settings Panel

**Tabbed Interface:**
1. **Voice Configuration**
   - Model selection (tiny → large-v3)
   - Language selection with auto-detect
   - Microphone picker with level meter
   - Push-to-talk vs voice activation
   - Sensitivity threshold slider

2. **Integration Settings**
   - n8n webhook URL with test button
   - ElevenLabs API key (secure input)
   - Voice selection dropdown
   - Response mode (text/voice/both)
   - Test TTS functionality

3. **Server Configuration**
   - Server URL and port
   - Python path override
   - Start/stop/restart controls
   - Auto-start on boot toggle
   - Auto-restart on crash toggle

4. **Advanced Settings**
   - Auto-typing behavior
   - Typing speed slider
   - Target application filter
   - Command prefix customization
   - Confirmation mode selection
   - Log level configuration

### 3. Profile Management

**Multi-User Support:**
- Profile selector in dashboard header
- Visual avatars with permission colors
- Per-profile settings overrides
- Usage statistics tracking

**Permission Levels:**
- Admin (red) - full access
- User (blue) - standard access
- Kid (green) - restricted commands
- Guest (gray) - minimal access

### 4. Notification System

**Toast Notifications:**
- Non-intrusive top-right positioning
- Auto-dismiss with configurable duration
- Action buttons for quick responses
- Severity-based coloring (success/error/info/warning)

## Data Flow

```
┌─────────────┐
│   Tauri     │
│   Backend   │ (Rust)
└──────┬──────┘
       │ IPC Commands (invoke)
       │ Events (emit)
       ↓
┌─────────────┐
│  services/  │
│  tauri.ts   │ (Service Layer)
└──────┬──────┘
       │
       ↓
┌─────────────┐
│   hooks/    │
│ useTauriState│ (State Management)
└──────┬──────┘
       │
       ↓
┌─────────────┐
│   Zustand   │
│   Store     │ (Global State)
└──────┬──────┘
       │
       ↓
┌─────────────┐
│ Components  │
│  (UI Layer) │ (React Components)
└─────────────┘
```

## Development Workflow

### Setup
```bash
cd TonnyTray
npm install
```

### Development
```bash
npm run dev              # Start Vite dev server
npm run tauri:dev        # Start Tauri app in dev mode
```

### Build
```bash
npm run build            # Build frontend
npm run tauri:build      # Build complete Tauri app
```

### Type Checking
```bash
npm run type-check       # Run TypeScript compiler
npm run lint             # Run ESLint
```

## Design Patterns

### 1. Separation of Concerns
- **Services**: External API integration
- **Hooks**: Business logic & state management
- **Components**: Pure UI presentation
- **Types**: Shared type definitions

### 2. Single Responsibility
Each component has one clear purpose:
- `RecordingControls` → recording operations
- `StatusPanel` → service status display
- `ProfileSelector` → user switching

### 3. Composition Over Inheritance
Components compose smaller components rather than extending base classes.

### 4. Dependency Injection
Services and hooks injected via imports, not singletons.

### 5. Immutable State Updates
Zustand uses Immer-style updates for state immutability.

## Performance Optimizations

1. **Selective Re-renders**: Zustand selectors prevent unnecessary re-renders
2. **Code Splitting**: React Router lazy loading (future enhancement)
3. **Event Debouncing**: Audio level updates throttled
4. **Memoization**: Computed values cached with `useMemo`
5. **Virtual Lists**: Future enhancement for long transcription history

## Accessibility

- Semantic HTML elements
- ARIA labels on interactive elements
- Keyboard navigation support
- Focus management
- Screen reader compatible

## Browser Compatibility

As a Tauri app, the frontend runs in a WebView:
- **Windows**: WebView2 (Chromium-based)
- **macOS**: WKWebView (Safari-based)
- **Linux**: WebKitGTK

Target: ES2020+ with modern browser features.

## Future Enhancements

1. **First-Run Wizard**: Guided setup flow
2. **Logs Viewer**: Dedicated log browsing UI
3. **Statistics Dashboard**: Detailed analytics
4. **Profile Manager**: Create/edit/delete profiles
5. **Quick Action Editor**: Custom command creation
6. **Theme Customization**: User-defined colors
7. **Hotkey Recorder**: Visual hotkey binding
8. **Audio Waveform**: Real-time visualization
9. **Command History Search**: Filter/search transcriptions
10. **Export/Import**: Settings backup/restore

## Contributing

When adding new features:

1. **Define types first** in `types/index.ts`
2. **Add Tauri commands** in `services/tauri.ts`
3. **Create custom hooks** if state management needed
4. **Build UI components** following existing patterns
5. **Update this README** with new architecture decisions

## Type Safety Guarantees

All IPC communication is fully typed:
- Frontend knows exact shape of backend responses
- Compile-time errors for mismatched types
- No runtime type surprises
- Autocomplete in IDE

## Testing Strategy (Future)

- **Unit Tests**: Hooks and utility functions (Vitest)
- **Component Tests**: React Testing Library
- **Integration Tests**: Tauri mock backend
- **E2E Tests**: Playwright for full workflows

## License

Same as WhisperLiveKit project.
