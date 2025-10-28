# TonnyTray Frontend Quick Start

Get up and running with the TonnyTray frontend in 5 minutes.

## Prerequisites

- Node.js 18+ (check: `node --version`)
- npm or yarn (check: `npm --version`)
- Basic React + TypeScript knowledge

## Installation

```bash
cd /home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray
npm install
```

## Development

### Frontend Only (for UI development)

```bash
npm run dev
```

Opens at http://localhost:1420

### With Tauri Backend

```bash
npm run tauri:dev
```

Opens as native desktop app

## Project Tour

### Main Files

- `src/App.tsx` - Root component, routing, theming
- `src/types/index.ts` - All TypeScript types
- `src/services/tauri.ts` - Backend communication
- `src/hooks/useTauriState.ts` - State management

### Key Components

- `src/components/Dashboard/Dashboard.tsx` - Main view
- `src/components/Settings/Settings.tsx` - Settings panel
- `src/components/Profile/ProfileSelector.tsx` - User switching

## Making Your First Change

### 1. Add a New Quick Action

Edit `src/components/Dashboard/QuickActions.tsx`:

```typescript
const myAction = {
  id: '5',
  name: 'My Action',
  icon: <HomeIcon />,
  command: 'My custom command',
  color: '#9C27B0',
};

// Add to defaultActions array
```

### 2. Add a New Setting

Edit `src/types/index.ts`:

```typescript
export interface AdvancedSettings {
  // ... existing fields
  myNewFeature: boolean;
}

export const DEFAULT_SETTINGS: AppSettings = {
  // ... existing
  advanced: {
    // ... existing
    myNewFeature: false,
  }
};
```

Edit `src/components/Settings/AdvancedTab.tsx`:

```typescript
<FormControlLabel
  control={
    <Switch
      checked={settings.advanced.myNewFeature}
      onChange={(e) => updateSettings({
        advanced: {
          ...settings.advanced,
          myNewFeature: e.target.checked
        }
      })}
    />
  }
  label="Enable My Feature"
/>
```

### 3. Call a Backend Command

```typescript
import { tauriApi } from '@services/tauri';

const handleClick = async () => {
  try {
    const result = await tauriApi.server.getStatus();
    console.log('Server status:', result);
  } catch (error) {
    console.error('Failed:', error);
  }
};
```

## Common Tasks

### Read Current State

```typescript
import { useAppStore } from '@hooks/useTauriState';

const { recording, serverStatus, settings } = useAppStore();
```

### Update State

```typescript
import { useSettings } from '@hooks/useTauriState';

const { updateSettings } = useSettings();

await updateSettings({
  voice: { model: 'base' }
});
```

### Add Event Listener

```typescript
import { tauriApi } from '@services/tauri';
import { useEffect } from 'react';

useEffect(() => {
  const unlisten = tauriApi.events.onTranscription((event) => {
    console.log('New:', event.payload.transcription.text);
  });

  return unlisten; // Cleanup
}, []);
```

## Folder Structure

```
src/
├── components/
│   ├── Common/         # Shared UI (Loading, Error, Notifications)
│   ├── Dashboard/      # Main view (Recording, Status, etc.)
│   ├── Profile/        # User management
│   └── Settings/       # Configuration tabs
├── hooks/              # State management (useTauriState)
├── services/           # Backend calls (tauri.ts)
├── theme/              # MUI theme (colors, styles)
├── types/              # TypeScript definitions
└── App.tsx             # Root component
```

## Available Commands

```bash
npm run dev              # Start dev server
npm run tauri:dev        # Start Tauri app
npm run build            # Build for production
npm run type-check       # Check TypeScript
npm run lint             # Run ESLint
```

## Debugging

### Check State

```typescript
const state = useAppStore();
console.log('Current state:', state);
```

### Check Types

```bash
npm run type-check
```

### React DevTools

Install browser extension: React Developer Tools

### Zustand DevTools

Install browser extension: Redux DevTools

## Next Steps

1. Read DEVELOPER_GUIDE.md for detailed patterns
2. Read FRONTEND_ARCHITECTURE.md for design decisions
3. Explore existing components for examples
4. Check types in src/types/index.ts

## Getting Help

- Check component implementations for examples
- Read inline comments in code
- Review TypeScript error messages carefully
- Test changes with `npm run type-check`

## Tips

- Use TypeScript autocomplete (Ctrl+Space)
- Check console for errors
- Keep components small and focused
- Use custom hooks for logic
- Follow existing patterns

Happy coding!
