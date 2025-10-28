/**
 * Test utilities and helpers
 */

import { ReactElement } from 'react';
import { render, RenderOptions } from '@testing-library/react';
import { ThemeProvider } from '@mui/material/styles';
import { SnackbarProvider } from 'notistack';
import { BrowserRouter } from 'react-router-dom';
import { createTheme } from '@mui/material/styles';

const theme = createTheme();

interface AllProvidersProps {
  children: React.ReactNode;
}

function AllProviders({ children }: AllProvidersProps) {
  return (
    <ThemeProvider theme={theme}>
      <SnackbarProvider>
        <BrowserRouter>{children}</BrowserRouter>
      </SnackbarProvider>
    </ThemeProvider>
  );
}

function customRender(ui: ReactElement, options?: Omit<RenderOptions, 'wrapper'>) {
  return render(ui, { wrapper: AllProviders, ...options });
}

export * from '@testing-library/react';
export { customRender as render };

/**
 * Mock Tauri commands
 */
export const mockTauriCommands = {
  startRecording: vi.fn(),
  stopRecording: vi.fn(),
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
  getProfiles: vi.fn(),
  switchProfile: vi.fn(),
};

/**
 * Create mock app state
 */
export function createMockAppState() {
  return {
    recording: 'idle' as const,
    audioLevel: null,
    connectionStatus: {
      server: 'connected' as const,
      n8n: true,
      elevenLabs: true,
      lastChecked: new Date().toISOString(),
    },
    serverStatus: 'connected' as const,
    lastTranscription: null,
    activeProfile: {
      id: 'test-profile',
      name: 'Test User',
      permissions: 'admin' as const,
      settings: {},
      allowedCommands: [],
      blockedCommands: [],
      usageStats: {
        totalCommands: 0,
        successfulCommands: 0,
        lastUsed: null,
      },
    },
    profiles: [],
    settings: {
      voice: {
        model: 'base' as const,
        language: 'en',
        autoDetectLanguage: true,
        microphone: null,
        pushToTalk: false,
        pushToTalkHotkey: 'Ctrl+Shift+V',
        voiceActivation: true,
        voiceActivationThreshold: 0.3,
      },
      integration: {
        n8nWebhookUrl: '',
        elevenLabsApiKey: '',
        elevenLabsVoiceId: '',
        elevenLabsEnabled: false,
        responseMode: 'both' as const,
        volume: 0.8,
        speed: 1.0,
        pitch: 1.0,
      },
      server: {
        url: 'ws://localhost:8888/asr',
        port: 8888,
        autoStart: true,
        autoRestart: true,
        pythonPath: null,
      },
      advanced: {
        autoTyping: false,
        typingSpeed: 50,
        targetApplicationFilter: '',
        commandPrefix: 'Computer,',
        confirmationMode: 'visual' as const,
        logLevel: 'info' as const,
        maxLogEntries: 1000,
      },
      theme: 'system' as const,
    },
    settingsOpen: false,
    dashboardOpen: false,
    quickActions: [],
    transcriptions: [],
    logs: [],
    notifications: [],
    statistics: {
      totalCommands: 0,
      successfulCommands: 0,
      failedCommands: 0,
      averageResponseTime: 0,
      uptime: 0,
    },
    availableDevices: [],
    availableVoices: [],
  };
}
