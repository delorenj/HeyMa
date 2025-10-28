/**
 * Core type definitions for TonnyTray application
 * Defines all interfaces for state management, settings, and IPC communication
 */

/**
 * Server connection status enumeration
 * Matches Rust ServerStatus enum in src-tauri/src/state.rs
 */
export enum ServerStatus {
  Stopped = 'stopped',
  Starting = 'starting',
  Running = 'running',
  Stopping = 'stopping',
  Error = 'error',
}

/**
 * Type guard to check if ServerStatus is an error
 */
export function isServerStatusError(status: ServerStatus | { Error: string }): status is { Error: string } {
  return typeof status === 'object' && 'Error' in status;
}

/**
 * Extract error message from ServerStatus
 */
export function getServerStatusError(status: ServerStatus | { Error: string }): string | null {
  if (isServerStatusError(status)) {
    return status.Error;
  }
  if (status === ServerStatus.Error) {
    return 'Unknown error';
  }
  return null;
}

/**
 * Recording state enumeration
 */
export enum RecordingState {
  Idle = 'idle',
  Listening = 'listening',
  Processing = 'processing',
  Error = 'error',
}

/**
 * Whisper model size options
 */
export type WhisperModel = 'tiny' | 'base' | 'small' | 'medium' | 'large-v3';

/**
 * Response mode for n8n integration
 */
export type ResponseMode = 'text' | 'voice' | 'both';

/**
 * Confirmation mode for actions
 */
export type ConfirmationMode = 'silent' | 'visual' | 'audio';

/**
 * User permission levels
 */
export type PermissionLevel = 'admin' | 'user' | 'kid' | 'guest';

/**
 * Theme mode
 */
export type ThemeMode = 'light' | 'dark' | 'system';

/**
 * Audio device information
 */
export interface AudioDevice {
  id: string;
  name: string;
  isDefault: boolean;
  sampleRate: number;
  channels: number;
}

/**
 * Voice configuration settings
 */
export interface VoiceConfig {
  model: WhisperModel;
  language: string;
  autoDetectLanguage: boolean;
  microphone: string | null;
  pushToTalk: boolean;
  pushToTalkHotkey: string;
  voiceActivation: boolean;
  voiceActivationThreshold: number;
}

/**
 * Integration settings for external services
 */
export interface IntegrationSettings {
  n8nWebhookUrl: string;
  elevenLabsApiKey: string;
  elevenLabsVoiceId: string;
  elevenLabsEnabled: boolean;
  responseMode: ResponseMode;
  volume: number;
  speed: number;
  pitch: number;
}

/**
 * Server configuration
 */
export interface ServerConfig {
  url: string;
  port: number;
  autoStart: boolean;
  autoRestart: boolean;
  pythonPath: string | null;
}

/**
 * Advanced application settings
 */
export interface AdvancedSettings {
  autoTyping: boolean;
  typingSpeed: number;
  targetApplicationFilter: string;
  commandPrefix: string;
  confirmationMode: ConfirmationMode;
  logLevel: 'debug' | 'info' | 'warn' | 'error';
  maxLogEntries: number;
}

/**
 * Complete application settings
 */
export interface AppSettings {
  voice: VoiceConfig;
  integration: IntegrationSettings;
  server: ServerConfig;
  advanced: AdvancedSettings;
  theme: ThemeMode;
}

/**
 * User profile definition
 */
export interface UserProfile {
  id: string;
  name: string;
  avatar?: string;
  permissions: PermissionLevel;
  settings: Partial<AppSettings>;
  allowedCommands: string[];
  blockedCommands: string[];
  usageStats: {
    totalCommands: number;
    successfulCommands: number;
    lastUsed: string | null;
  };
}

/**
 * Quick action preset
 */
export interface QuickAction {
  id: string;
  name: string;
  icon: string;
  command: string;
  requiresConfirmation: boolean;
}

/**
 * Transcription entry
 */
export interface Transcription {
  id: string;
  timestamp: string;
  text: string;
  confidence: number;
  duration: number;
  profileId: string;
  success: boolean;
  response?: string;
}

/**
 * Log entry
 */
export interface LogEntry {
  id: string;
  timestamp: string;
  level: 'debug' | 'info' | 'warn' | 'error';
  component: string;
  message: string;
  metadata?: Record<string, unknown>;
}

/**
 * System notification
 */
export interface Notification {
  id: string;
  type: 'success' | 'error' | 'info' | 'warning';
  title: string;
  message: string;
  timestamp: string;
  duration?: number;
  action?: {
    label: string;
    callback: () => void;
  };
}

/**
 * Connection status for external services
 */
export interface ConnectionStatus {
  server: ServerStatus;
  n8n: boolean;
  elevenLabs: boolean;
  lastChecked: string;
}

/**
 * Audio level information
 */
export interface AudioLevel {
  timestamp: number;
  level: number;
  peak: number;
}

/**
 * Statistics and metrics
 */
export interface Statistics {
  totalCommands: number;
  successfulCommands: number;
  failedCommands: number;
  averageResponseTime: number;
  uptime: number;
  lastError?: string;
}

/**
 * Main application state
 */
export interface AppState {
  // Recording state
  recording: RecordingState;
  audioLevel: AudioLevel | null;

  // Status
  connectionStatus: ConnectionStatus;
  serverStatus: ServerStatus;
  lastTranscription: Transcription | null;

  // User management
  activeProfile: UserProfile;
  profiles: UserProfile[];

  // Settings
  settings: AppSettings;

  // UI state
  settingsOpen: boolean;
  dashboardOpen: boolean;

  // Quick actions
  quickActions: QuickAction[];

  // History and logs
  transcriptions: Transcription[];
  logs: LogEntry[];
  notifications: Notification[];

  // Statistics
  statistics: Statistics;

  // Available devices
  availableDevices: AudioDevice[];
  availableVoices: Array<{ id: string; name: string }>;
}

/**
 * Tauri IPC command payloads
 */
export namespace TauriCommands {
  export interface StartRecordingPayload {
    profileId: string;
  }

  export interface StopRecordingPayload {
    save: boolean;
  }

  export interface UpdateSettingsPayload {
    settings: Partial<AppSettings>;
  }

  export interface SwitchProfilePayload {
    profileId: string;
  }

  export interface TestWebhookPayload {
    url: string;
    message: string;
  }

  export interface TestTTSPayload {
    text: string;
    voiceId: string;
  }

  export interface SendCommandPayload {
    command: string;
    profileId: string;
  }

  export interface GetLogsPayload {
    level?: string;
    limit?: number;
    offset?: number;
  }

  export interface ExportLogsPayload {
    path: string;
  }
}

/**
 * Tauri event payloads
 */
export namespace TauriEvents {
  export interface TranscriptionEvent {
    transcription: Transcription;
  }

  export interface StatusUpdateEvent {
    status: ServerStatus;
  }

  export interface AudioLevelEvent {
    level: AudioLevel;
  }

  export interface NotificationEvent {
    notification: Notification;
  }

  export interface ErrorEvent {
    error: string;
    component: string;
  }
}

/**
 * Default values for configuration
 */
export const DEFAULT_SETTINGS: AppSettings = {
  voice: {
    model: 'base',
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
    responseMode: 'both',
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
    confirmationMode: 'visual',
    logLevel: 'info',
    maxLogEntries: 1000,
  },
  theme: 'system',
};

/**
 * Default guest profile
 */
export const DEFAULT_GUEST_PROFILE: UserProfile = {
  id: 'guest',
  name: 'Guest',
  permissions: 'guest',
  settings: {},
  allowedCommands: [
    'turn on *',
    'turn off *',
    "what's the weather",
    'play music',
  ],
  blockedCommands: [
    'unlock *',
    'open door',
    'disable security',
  ],
  usageStats: {
    totalCommands: 0,
    successfulCommands: 0,
    lastUsed: null,
  },
};
