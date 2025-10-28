# TonnyTray Component Usage Guide

## Table of Contents
1. [Common Components](#common-components)
2. [Audio Components](#audio-components)
3. [Dashboard Components](#dashboard-components)
4. [Settings Components](#settings-components)
5. [Logs Components](#logs-components)
6. [Custom Hooks](#custom-hooks)
7. [Usage Examples](#usage-examples)

---

## Common Components

### ErrorBoundary
Wraps components to catch and recover from errors.

```tsx
import { ErrorBoundary } from '@components/Common/ErrorBoundary';

<ErrorBoundary
  onError={(error, errorInfo) => {
    console.error('Error caught:', error, errorInfo);
  }}
  fallback={<CustomErrorUI />}
>
  <YourComponent />
</ErrorBoundary>
```

**Props:**
- `children`: ReactNode - Components to protect
- `fallback?`: ReactNode - Custom error UI (optional)
- `onError?`: (error, errorInfo) => void - Error callback (optional)

---

### ConfirmDialog
Reusable confirmation dialog with severity levels.

```tsx
import { useConfirmDialog } from '@components/Common/ConfirmDialog';

function MyComponent() {
  const { showConfirm, dialog } = useConfirmDialog();

  const handleDelete = async () => {
    await showConfirm({
      title: 'Delete Item?',
      message: 'This action cannot be undone.',
      severity: 'error',
      confirmText: 'Delete',
      confirmColor: 'error',
      onConfirm: async () => {
        await deleteItem();
      },
    });
  };

  return (
    <>
      <Button onClick={handleDelete}>Delete</Button>
      {dialog}
    </>
  );
}
```

**Props:**
- `open`: boolean - Dialog visibility
- `title`: string - Dialog title
- `message`: string - Dialog message
- `severity?`: 'info' | 'warning' | 'error' - Visual style
- `confirmText?`: string - Confirm button text
- `cancelText?`: string - Cancel button text
- `confirmColor?`: ButtonColor - Confirm button color
- `onConfirm`: () => void | Promise<void> - Confirm handler
- `onCancel`: () => void - Cancel handler
- `loading?`: boolean - Loading state

---

### Skeleton Loaders
Pre-rendered loading placeholders.

```tsx
import {
  StatusPanelSkeleton,
  TranscriptionPanelSkeleton,
  ProfileSelectorSkeleton,
  QuickActionsSkeleton,
  LogEntrySkeleton,
  StatisticsCardSkeleton,
  ListSkeleton,
  ContentSkeleton,
} from '@components/Common/Skeletons';

// Usage
{loading ? <StatusPanelSkeleton /> : <StatusPanel />}
{loading ? <ListSkeleton count={5} /> : <ItemList />}
```

---

## Audio Components

### AudioLevelMeter
Vertical audio level visualization with dB scale.

```tsx
import AudioLevelMeter from '@components/Audio/AudioLevelMeter';

<AudioLevelMeter
  showLabel={true}
  showDbValue={true}
  height={200}
  minDb={-60}
  maxDb={0}
/>
```

**Props:**
- `showLabel?`: boolean - Show "Audio Level" label
- `showDbValue?`: boolean - Show numeric dB value
- `height?`: number - Meter height in pixels
- `minDb?`: number - Minimum dB value (-60)
- `maxDb?`: number - Maximum dB value (0)

### CompactAudioLevelMeter
Horizontal compact audio level meter.

```tsx
import { CompactAudioLevelMeter } from '@components/Audio/AudioLevelMeter';

<CompactAudioLevelMeter />
```

---

## Dashboard Components

### StatisticsWidgets
Pre-built statistics display widgets.

```tsx
import {
  TotalCommandsWidget,
  SuccessRateWidget,
  FailedCommandsWidget,
  AverageResponseTimeWidget,
  UptimeWidget,
  SuccessBreakdownWidget,
  StatisticsGrid,
} from '@components/Dashboard/StatisticsWidgets';

// Individual widgets
<TotalCommandsWidget />
<SuccessRateWidget />
<AverageResponseTimeWidget />

// All widgets in responsive grid
<StatisticsGrid />
```

**Features:**
- Animated counters
- Color-coded by performance
- Trend indicators
- Tooltips for context

---

### RecordingControls
Enhanced recording controls with animations.

```tsx
import RecordingControls from '@components/Dashboard/RecordingControls';

<RecordingControls />
```

**Features:**
- Start/stop recording
- Pause/resume recording
- Visual state indicators
- Pulse animations during recording
- Keyboard shortcut hints
- Toast notifications

---

## Settings Components

### KeyboardShortcutPicker
Interactive keyboard shortcut configuration.

```tsx
import KeyboardShortcutPicker from '@components/Settings/KeyboardShortcutPicker';

<KeyboardShortcutPicker
  label="Push-to-Talk Hotkey"
  value={settings.voice.pushToTalkHotkey}
  onChange={(hotkey) => updateHotkey(hotkey)}
  helperText="Press the key combination you want to use"
  error={hasError}
  errorText="This shortcut conflicts with a system shortcut"
/>
```

**Props:**
- `label`: string - Field label
- `value`: string - Current hotkey (e.g., "Ctrl+Shift+V")
- `onChange`: (hotkey: string) => void - Update handler
- `helperText?`: string - Help text
- `error?`: boolean - Error state
- `errorText?`: string - Error message

**Features:**
- Live key capture mode
- Visual key display
- Conflict detection
- Format validation

### KeyboardShortcutsManager
Manage multiple keyboard shortcuts.

```tsx
import { KeyboardShortcutsManager } from '@components/Settings/KeyboardShortcutPicker';

const shortcuts = [
  { id: 'record', label: 'Start/Stop Recording', value: 'Ctrl+Shift+V', description: 'Toggle recording' },
  { id: 'pause', label: 'Pause Recording', value: 'Ctrl+Shift+P', description: 'Pause/resume recording' },
];

<KeyboardShortcutsManager
  shortcuts={shortcuts}
  onChange={(id, hotkey) => updateShortcut(id, hotkey)}
/>
```

---

### SettingsManager
Import/export/reset settings functionality.

```tsx
import SettingsManager from '@components/Settings/SettingsManager';

<SettingsManager />
```

**Features:**
- Export settings to JSON
- Import settings from JSON
- Create backup
- Reset to defaults
- Current config display
- Confirmation dialogs

---

## Logs Components

### LogsViewer
Comprehensive filterable logs viewer.

```tsx
import LogsViewer from '@components/Logs/LogsViewer';

<LogsViewer />
```

**Features:**
- Multi-level filtering (debug/info/warn/error)
- Component-based filtering
- Full-text search with debouncing
- Export logs to file
- Clear all logs
- Expandable metadata
- Real-time updates
- Animated entries

---

## Custom Hooks

### useNotification
Easy toast notifications.

```tsx
import { useNotification } from '@hooks/useNotification';

function MyComponent() {
  const { showSuccess, showError, showWarning, showInfo } = useNotification();

  const handleAction = async () => {
    try {
      await doSomething();
      showSuccess('Operation completed successfully');
    } catch (error) {
      showError(`Operation failed: ${error.message}`);
    }
  };

  return <Button onClick={handleAction}>Do Something</Button>;
}
```

**Methods:**
- `showSuccess(message)` - Green success notification
- `showError(message)` - Red error notification (7s duration)
- `showWarning(message)` - Orange warning notification
- `showInfo(message)` - Blue info notification
- `showNotification(message, options)` - Custom notification
- `closeNotification(key)` - Close specific notification

---

### useDebounce
Debounce rapidly changing values.

```tsx
import { useDebounce } from '@hooks/useDebounce';

function SearchComponent() {
  const [searchTerm, setSearchTerm] = useState('');
  const debouncedSearch = useDebounce(searchTerm, 300);

  useEffect(() => {
    // This only runs 300ms after user stops typing
    performSearch(debouncedSearch);
  }, [debouncedSearch]);

  return <TextField value={searchTerm} onChange={(e) => setSearchTerm(e.target.value)} />;
}
```

**Params:**
- `value`: T - Value to debounce
- `delay?`: number - Delay in milliseconds (default 500)

---

### useAsync
Manage async operations with loading/error/data states.

```tsx
import { useAsync } from '@hooks/useAsync';

function DataComponent() {
  const { loading, error, data, execute, reset } = useAsync(
    async () => {
      const response = await fetchData();
      return response.json();
    },
    true // Execute immediately
  );

  if (loading) return <Loading />;
  if (error) return <Error message={error.message} />;
  if (!data) return <NoData />;

  return <DisplayData data={data} />;
}
```

**Returns:**
- `loading`: boolean - Loading state
- `error`: Error | null - Error object
- `data`: T | null - Response data
- `execute`: () => Promise<void> - Re-execute function
- `reset`: () => void - Reset to initial state

---

### useLocalStorage
Sync state with localStorage.

```tsx
import { useLocalStorage } from '@hooks/useLocalStorage';

function PreferencesComponent() {
  const [theme, setTheme, removeTheme] = useLocalStorage('theme', 'light');

  return (
    <Select value={theme} onChange={(e) => setTheme(e.target.value)}>
      <MenuItem value="light">Light</MenuItem>
      <MenuItem value="dark">Dark</MenuItem>
    </Select>
  );
}
```

**Params:**
- `key`: string - localStorage key
- `initialValue`: T - Default value

**Returns:**
- `[value, setValue, removeValue]` - Like useState with remove

---

### useKeyboardShortcut
Register global keyboard shortcuts.

```tsx
import { useKeyboardShortcut } from '@hooks/useKeyboardShortcut';

function Component() {
  useKeyboardShortcut(
    'Ctrl+Shift+V',
    (event) => {
      console.log('Shortcut triggered!');
      startRecording();
    },
    {
      enabled: true,
      preventDefault: true,
      stopPropagation: false,
    }
  );

  return <div>Press Ctrl+Shift+V to record</div>;
}
```

**Params:**
- `hotkey`: string - Key combination (e.g., "Ctrl+Shift+V")
- `callback`: (event: KeyboardEvent) => void - Handler
- `options?`: KeyboardShortcutOptions - Configuration

---

### useConfirmDialog (Hook)
Programmatic confirmation dialogs.

```tsx
import { useConfirmDialog } from '@components/Common/ConfirmDialog';

function Component() {
  const { showConfirm, dialog } = useConfirmDialog();

  const handleDelete = async () => {
    // Returns Promise<boolean>
    await showConfirm({
      title: 'Delete Item?',
      message: 'This cannot be undone.',
      severity: 'error',
      confirmText: 'Delete',
      confirmColor: 'error',
      onConfirm: async () => {
        await api.delete();
      },
    });
  };

  return (
    <>
      <Button onClick={handleDelete}>Delete</Button>
      {dialog}
    </>
  );
}
```

---

## Usage Examples

### Example 1: Create a Settings Page
```tsx
import { useState } from 'react';
import { Box, Tabs, Tab } from '@mui/material';
import {
  VoiceConfigTab,
  IntegrationTab,
  ServerConfigTab,
  AdvancedTab,
  SettingsManager,
} from '@components';

function SettingsPage() {
  const [tab, setTab] = useState(0);

  return (
    <Box>
      <Tabs value={tab} onChange={(_, v) => setTab(v)}>
        <Tab label="Voice" />
        <Tab label="Integration" />
        <Tab label="Server" />
        <Tab label="Advanced" />
        <Tab label="Management" />
      </Tabs>

      {tab === 0 && <VoiceConfigTab />}
      {tab === 1 && <IntegrationTab />}
      {tab === 2 && <ServerConfigTab />}
      {tab === 3 && <AdvancedTab />}
      {tab === 4 && <SettingsManager />}
    </Box>
  );
}
```

### Example 2: Dashboard with Statistics
```tsx
import { Grid } from '@mui/material';
import {
  RecordingControls,
  StatusPanel,
  TranscriptionPanel,
  StatisticsGrid,
  QuickActions,
} from '@components';

function Dashboard() {
  return (
    <Grid container spacing={3}>
      <Grid item xs={12}>
        <RecordingControls />
      </Grid>
      <Grid item xs={12} md={6}>
        <StatusPanel />
      </Grid>
      <Grid item xs={12} md={6}>
        <TranscriptionPanel />
      </Grid>
      <Grid item xs={12}>
        <QuickActions />
      </Grid>
      <Grid item xs={12}>
        <StatisticsGrid />
      </Grid>
    </Grid>
  );
}
```

### Example 3: Logs Page
```tsx
import { Box, Paper, Typography } from '@mui/material';
import { LogsViewer } from '@components';

function LogsPage() {
  return (
    <Box sx={{ height: '100vh', display: 'flex', flexDirection: 'column', p: 2 }}>
      <Typography variant="h4" gutterBottom>
        Application Logs
      </Typography>
      <Box sx={{ flex: 1, minHeight: 0 }}>
        <LogsViewer />
      </Box>
    </Box>
  );
}
```

### Example 4: Audio Level Display
```tsx
import { Grid } from '@mui/material';
import AudioLevelMeter, { CompactAudioLevelMeter } from '@components/Audio/AudioLevelMeter';

function AudioPage() {
  return (
    <Grid container spacing={3}>
      {/* Full meter */}
      <Grid item xs={12} md={6}>
        <AudioLevelMeter
          showLabel={true}
          showDbValue={true}
          height={300}
        />
      </Grid>

      {/* Compact meter */}
      <Grid item xs={12}>
        <CompactAudioLevelMeter />
      </Grid>
    </Grid>
  );
}
```

### Example 5: Error Handling
```tsx
import { ErrorBoundary } from '@components';
import { useNotification } from '@hooks';

function App() {
  return (
    <ErrorBoundary
      onError={(error, errorInfo) => {
        // Log to external service
        logErrorToService(error, errorInfo);
      }}
    >
      <AppContent />
    </ErrorBoundary>
  );
}

function AppContent() {
  const { showError, showSuccess } = useNotification();

  const handleAction = async () => {
    try {
      await riskyOperation();
      showSuccess('Operation completed');
    } catch (error) {
      showError(`Failed: ${error.message}`);
    }
  };

  return <Button onClick={handleAction}>Try It</Button>;
}
```

---

## Component Import Patterns

### Individual imports
```tsx
import AudioLevelMeter from '@components/Audio/AudioLevelMeter';
import { useNotification } from '@hooks/useNotification';
```

### Barrel imports
```tsx
import { AudioLevelMeter, LogsViewer, ConfirmDialog } from '@components';
import { useNotification, useDebounce, useAsync } from '@hooks';
```

### Type imports
```tsx
import type { Transcription, LogEntry, AppSettings } from '@types/index';
```

---

## Best Practices

1. **Use TypeScript**: All components are fully typed
2. **Error Handling**: Wrap risky components in ErrorBoundary
3. **Loading States**: Use skeleton loaders for better UX
4. **Notifications**: Use toast notifications instead of alerts
5. **Confirmation**: Use ConfirmDialog for destructive actions
6. **Accessibility**: Components include ARIA attributes
7. **Performance**: Components use React.memo where appropriate
8. **Testing**: All components are testable with Vitest

---

## Troubleshooting

### Component not rendering
- Check if wrapped in proper providers (ThemeProvider, SnackbarProvider)
- Verify Tauri state is initialized
- Check browser console for errors

### Animations not working
- Ensure framer-motion is installed
- Check for motion reduce preference in OS

### Hooks not working
- Ensure hooks are called at top level
- Check if component is inside Router/ThemeProvider

### Tests failing
- Run `npm test -- --ui` for detailed view
- Check mock setup in `src/test/setup.ts`
- Verify test environment is happy-dom

---

## Additional Resources

- [Material-UI Documentation](https://mui.com/)
- [Framer Motion Documentation](https://www.framer.com/motion/)
- [Tauri Documentation](https://tauri.app/)
- [Vitest Documentation](https://vitest.dev/)
- [React TypeScript Cheatsheet](https://react-typescript-cheatsheet.netlify.app/)
