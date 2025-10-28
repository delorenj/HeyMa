# TonnyTray Frontend Developer Guide

Quick reference for working with the TonnyTray React + TypeScript frontend.

## Quick Start

```bash
# Install dependencies
cd TonnyTray
npm install

# Start development server (frontend only)
npm run dev

# Start Tauri app (frontend + backend)
npm run tauri:dev

# Type checking
npm run type-check

# Linting
npm run lint

# Build production
npm run build
```

## Project Structure

```
src/
├── components/     # React UI components
│   ├── Common/    # Reusable: Loading, Error, Notifications
│   ├── Dashboard/ # Main view: Recording, Status, Transcriptions
│   ├── Profile/   # User profile management
│   └── Settings/  # Configuration tabs
├── hooks/         # Custom React hooks
├── services/      # Tauri IPC wrappers
├── theme/         # MUI theme config
├── types/         # TypeScript definitions
└── App.tsx        # Root component
```

## Adding a New Component

1. **Create component file**
```typescript
// src/components/Dashboard/MyComponent.tsx
import { Box, Typography } from '@mui/material';
import { useAppStore } from '@hooks/useTauriState';

export default function MyComponent() {
  const { someState } = useAppStore();

  return (
    <Box>
      <Typography variant="h6">My Component</Typography>
      {/* ... */}
    </Box>
  );
}
```

2. **Import and use**
```typescript
import MyComponent from './MyComponent';
```

## State Management

### Reading State

```typescript
import { useAppStore } from '@hooks/useTauriState';

// Subscribe to specific state
const recording = useAppStore((state) => state.recording);
const settings = useAppStore((state) => state.settings);

// Subscribe to multiple values
const { recording, serverStatus } = useAppStore();
```

### Updating State

```typescript
import { useAppStore } from '@hooks/useTauriState';

const { setRecording, updateSettings } = useAppStore();

// Direct state update
setRecording('listening');

// Update via hook (preferred)
const { updateSettings } = useSettings();
await updateSettings({ voice: { model: 'base' } });
```

## Using Custom Hooks

### Recording Controls

```typescript
import { useRecordingControls } from '@hooks/useTauriState';

const {
  recording,           // Current state
  startRecording,      // Start function
  stopRecording,       // Stop function
  isRecording,         // Boolean helper
} = useRecordingControls();

// Start recording
await startRecording();

// Stop recording
await stopRecording();
```

### Settings Management

```typescript
import { useSettings } from '@hooks/useTauriState';

const {
  settings,          // Current settings
  updateSettings,    // Update function
  resetSettings,     // Reset to defaults
} = useSettings();

// Update settings
await updateSettings({
  voice: { model: 'small' }
});

// Reset all settings
await resetSettings();
```

### Profile Management

```typescript
import { useProfiles } from '@hooks/useTauriState';

const {
  activeProfile,     // Current profile
  profiles,          // All profiles
  switchProfile,     // Switch function
  createProfile,     // Create function
} = useProfiles();

// Switch profile
await switchProfile('profile-id');

// Create new profile
await createProfile({
  name: 'New User',
  permissions: 'user',
  settings: {},
  allowedCommands: [],
  blockedCommands: [],
  usageStats: { ... }
});
```

## Calling Tauri Commands

### Direct Service Calls

```typescript
import { tauriApi } from '@services/tauri';

// Recording
await tauriApi.recording.start('profile-id');
await tauriApi.recording.stop();

// Settings
const settings = await tauriApi.settings.get();
await tauriApi.settings.update({ ... });

// Audio
const devices = await tauriApi.audio.getDevices();
const isWorking = await tauriApi.audio.testDevice('device-id');

// Server
await tauriApi.server.start();
await tauriApi.server.stop();
const status = await tauriApi.server.getStatus();

// Integration
const success = await tauriApi.integration.testWebhook(url, message);
const voices = await tauriApi.integration.getVoices();
```

### Listening to Events

```typescript
import { tauriApi } from '@services/tauri';
import { useEffect } from 'react';

useEffect(() => {
  const unlisten = tauriApi.events.onTranscription((event) => {
    console.log('New transcription:', event.payload.transcription);
  });

  return () => {
    unlisten();
  };
}, []);
```

## Working with Types

### Import Types

```typescript
import type {
  AppState,
  AppSettings,
  UserProfile,
  Transcription,
  ServerStatus,
  RecordingState,
} from '@types/index';
```

### Using Enums

```typescript
import { ServerStatus, RecordingState } from '@types/index';

const status: ServerStatus = 'connected';
const recording: RecordingState = 'listening';
```

### Type-Safe Updates

```typescript
import type { AppSettings } from '@types/index';

const partialSettings: Partial<AppSettings> = {
  voice: {
    model: 'base',
    language: 'en',
    // Other fields optional
  }
};

await updateSettings(partialSettings);
```

## Styling Components

### Using MUI Components

```typescript
import { Box, Paper, Typography, Button } from '@mui/material';

<Paper sx={{ p: 3 }}>
  <Typography variant="h6" gutterBottom>
    Title
  </Typography>
  <Button variant="contained" color="primary">
    Action
  </Button>
</Paper>
```

### Theme Colors

```typescript
import { getRecordingColor, getStatusColor } from '@theme/index';

const color = getRecordingColor('listening'); // '#2196F3'
const statusColor = getStatusColor('connected'); // '#4CAF50'

<Box sx={{ bgcolor: color }}>...</Box>
```

### Responsive Styles

```typescript
<Box
  sx={{
    display: 'flex',
    flexDirection: { xs: 'column', md: 'row' },
    gap: 2,
  }}
>
  ...
</Box>
```

## Adding a New Tauri Command

1. **Add to service layer**
```typescript
// src/services/tauri.ts

export const myCommands = {
  async doSomething(param: string): Promise<ResultType> {
    return invokeCommand<ResultType>('do_something', { param });
  },
};

export const tauriApi = {
  // ... existing
  my: myCommands,
};
```

2. **Use in component**
```typescript
import { tauriApi } from '@services/tauri';

const result = await tauriApi.my.doSomething('value');
```

## Error Handling

### In Hooks

```typescript
try {
  await tauriApi.recording.start(profileId);
  setRecording('listening');
} catch (err) {
  setRecording('error');
  addNotification({
    id: crypto.randomUUID(),
    type: 'error',
    title: 'Recording Failed',
    message: err instanceof Error ? err.message : 'Unknown error',
    timestamp: new Date().toISOString(),
  });
}
```

### In Components

```typescript
const [loading, setLoading] = useState(false);

const handleStart = async () => {
  setLoading(true);
  try {
    await startRecording();
  } catch (error) {
    console.error('Failed to start recording:', error);
    // Error already handled by hook
  } finally {
    setLoading(false);
  }
};
```

## Adding a New Setting

1. **Update types**
```typescript
// src/types/index.ts

export interface AdvancedSettings {
  // ... existing
  myNewSetting: boolean;
}

export const DEFAULT_SETTINGS: AppSettings = {
  // ... existing
  advanced: {
    // ... existing
    myNewSetting: false,
  }
};
```

2. **Add to settings tab**
```typescript
// src/components/Settings/AdvancedTab.tsx

<FormControlLabel
  control={
    <Switch
      checked={settings.advanced.myNewSetting}
      onChange={(e) => updateSettings({
        advanced: {
          ...settings.advanced,
          myNewSetting: e.target.checked
        }
      })}
    />
  }
  label="My New Setting"
/>
```

## Debugging

### State Inspection

```typescript
// In any component
const state = useAppStore();
console.log('Current state:', state);
```

### Zustand DevTools

Install Redux DevTools browser extension - Zustand automatically connects.

### Type Checking

```bash
# Check types without building
npm run type-check
```

### React DevTools

Install React DevTools browser extension for component inspection.

## Common Patterns

### Loading States

```typescript
const [loading, setLoading] = useState(false);

const handleAction = async () => {
  setLoading(true);
  try {
    await someAsyncOperation();
  } finally {
    setLoading(false);
  }
};

<Button disabled={loading}>
  {loading ? 'Loading...' : 'Action'}
</Button>
```

### Conditional Rendering

```typescript
{condition && <Component />}
{condition ? <ComponentA /> : <ComponentB />}
```

### Lists with Keys

```typescript
{items.map((item) => (
  <ListItem key={item.id}>
    {item.name}
  </ListItem>
))}
```

## Testing (Future)

### Unit Test Example

```typescript
// src/hooks/__tests__/useTauriState.test.ts
import { renderHook } from '@testing-library/react-hooks';
import { useRecordingControls } from '../useTauriState';

test('starts recording', async () => {
  const { result } = renderHook(() => useRecordingControls());

  await result.current.startRecording();

  expect(result.current.recording).toBe('listening');
});
```

### Component Test Example

```typescript
// src/components/__tests__/RecordingControls.test.tsx
import { render, screen, fireEvent } from '@testing-library/react';
import RecordingControls from '../Dashboard/RecordingControls';

test('shows start button when idle', () => {
  render(<RecordingControls />);
  expect(screen.getByText('Start Recording')).toBeInTheDocument();
});
```

## Performance Tips

1. **Use selectors**: Only subscribe to needed state
   ```typescript
   const recording = useAppStore((state) => state.recording);
   ```

2. **Memoize expensive computations**
   ```typescript
   const filteredItems = useMemo(
     () => items.filter(predicate),
     [items, predicate]
   );
   ```

3. **Avoid inline functions in render**
   ```typescript
   const handleClick = useCallback(() => { ... }, [deps]);
   ```

4. **Slice large arrays**
   ```typescript
   const recentItems = items.slice(0, 10);
   ```

## Useful VS Code Extensions

- ESLint
- Prettier
- TypeScript Vue Plugin (Volar)
- Material-UI snippets
- Error Lens
- Auto Rename Tag
- Path Intellisense

## Common Issues

### Import Errors

**Problem**: `Cannot find module '@components/...'`

**Solution**: Check `tsconfig.json` and `vite.config.ts` have matching path aliases.

### Type Errors

**Problem**: `Type 'X' is not assignable to type 'Y'`

**Solution**: Check type definitions in `src/types/index.ts`. Ensure strict null checks.

### State Not Updating

**Problem**: Component doesn't re-render on state change

**Solution**: Use Zustand selectors, not direct state access.

### Event Listener Leaks

**Problem**: Event listeners not cleaning up

**Solution**: Return cleanup function from useEffect:
```typescript
useEffect(() => {
  const unlisten = setupListener();
  return unlisten; // Cleanup
}, []);
```

## Resources

- [React Docs](https://react.dev)
- [TypeScript Handbook](https://www.typescriptlang.org/docs/)
- [Material-UI Docs](https://mui.com/)
- [Zustand Docs](https://github.com/pmndrs/zustand)
- [Tauri Docs](https://tauri.app/)
- [Vite Docs](https://vitejs.dev/)

## Getting Help

1. Check FRONTEND_ARCHITECTURE.md for design decisions
2. Check IMPLEMENTATION_SUMMARY.md for feature overview
3. Review existing component implementations
4. Check TypeScript types in src/types/index.ts
5. Test in isolation with npm run dev
