/**
 * Tauri state management hook
 * Provides reactive state synchronization with Tauri backend
 */

import { useEffect, useState, useCallback } from 'react';
import { create } from 'zustand';
import { tauriApi } from '@services/tauri';
import {
  ServerStatus,
  DEFAULT_SETTINGS,
  DEFAULT_GUEST_PROFILE,
} from '@types';
import type {
  AppState,
  RecordingState,
  UserProfile,
  AppSettings,
  Transcription,
  Notification,
  AudioLevel,
  LogEntry,
  Statistics,
  AudioDevice,
} from '@types';

/**
 * Initial state
 */
const initialState: AppState = {
  recording: 'idle' as RecordingState,
  audioLevel: null,
  connectionStatus: {
    server: ServerStatus.Stopped,
    n8n: false,
    elevenLabs: false,
    lastChecked: new Date().toISOString(),
  },
  serverStatus: ServerStatus.Stopped,
  lastTranscription: null,
  activeProfile: DEFAULT_GUEST_PROFILE,
  profiles: [DEFAULT_GUEST_PROFILE],
  settings: DEFAULT_SETTINGS,
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

/**
 * Zustand store for app state
 */
export const useAppStore = create<AppState & {
  // State setters
  setRecording: (state: RecordingState) => void;
  setAudioLevel: (level: AudioLevel | null) => void;
  setServerStatus: (status: ServerStatus) => void;
  setLastTranscription: (transcription: Transcription | null) => void;
  setActiveProfile: (profile: UserProfile) => void;
  setProfiles: (profiles: UserProfile[]) => void;
  setSettings: (settings: AppSettings) => void;
  setSettingsOpen: (open: boolean) => void;
  setDashboardOpen: (open: boolean) => void;
  addTranscription: (transcription: Transcription) => void;
  addLog: (log: LogEntry) => void;
  addNotification: (notification: Notification) => void;
  removeNotification: (id: string) => void;
  setStatistics: (stats: Statistics) => void;
  setAvailableDevices: (devices: AudioDevice[]) => void;
  setAvailableVoices: (voices: Array<{ id: string; name: string }>) => void;
  updateConnectionStatus: (partial: Partial<AppState['connectionStatus']>) => void;
}>((set) => ({
  ...initialState,

  setRecording: (recording) => set({ recording }),
  setAudioLevel: (audioLevel) => set({ audioLevel }),
  setServerStatus: (serverStatus) =>
    set((state) => ({
      serverStatus,
      connectionStatus: { ...state.connectionStatus, server: serverStatus },
    })),
  setLastTranscription: (lastTranscription) => set({ lastTranscription }),
  setActiveProfile: (activeProfile) => set({ activeProfile }),
  setProfiles: (profiles) => set({ profiles }),
  setSettings: (settings) => set({ settings }),
  setSettingsOpen: (settingsOpen) => set({ settingsOpen }),
  setDashboardOpen: (dashboardOpen) => set({ dashboardOpen }),

  addTranscription: (transcription) =>
    set((state) => ({
      transcriptions: [transcription, ...state.transcriptions].slice(0, 50),
      lastTranscription: transcription,
    })),

  addLog: (log) =>
    set((state) => ({
      logs: [log, ...state.logs].slice(0, state.settings.advanced.maxLogEntries),
    })),

  addNotification: (notification) =>
    set((state) => ({
      notifications: [notification, ...state.notifications],
    })),

  removeNotification: (id) =>
    set((state) => ({
      notifications: state.notifications.filter((n) => n.id !== id),
    })),

  setStatistics: (statistics) => set({ statistics }),
  setAvailableDevices: (availableDevices) => set({ availableDevices }),
  setAvailableVoices: (availableVoices) => set({ availableVoices }),

  updateConnectionStatus: (partial) =>
    set((state) => ({
      connectionStatus: {
        ...state.connectionStatus,
        ...partial,
        lastChecked: new Date().toISOString(),
      },
    })),
}));

/**
 * Hook to initialize and sync state with Tauri backend
 */
export function useTauriState() {
  const [isInitialized, setIsInitialized] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const store = useAppStore();

  const initialize = useCallback(async () => {
    try {
      // Try to get basic state first
      const settings = await tauriApi.settings.get().catch(() => DEFAULT_SETTINGS);
      
      store.setSettings(settings);
      
      // Try to get profiles, fallback to default
      const profiles = await tauriApi.profile.getAll().catch(() => [DEFAULT_GUEST_PROFILE]);
      store.setProfiles(profiles);
      
      // Set active profile
      const activeProfile = profiles.find((p) => p.permissions === 'admin') ?? profiles[0];
      if (activeProfile) {
        store.setActiveProfile(activeProfile);
      }

      // Try to get server status
      const serverStatus = await tauriApi.server.getStatus().catch(() => ServerStatus.Stopped);
      store.setServerStatus(serverStatus);

      setIsInitialized(true);
      setError(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to initialize';
      setError(message);
      console.error('Failed to initialize Tauri state:', err);
    }
  }, [store]);

  /**
   * Setup event listeners
   */
  useEffect(() => {
    const unlisteners: Array<() => void> = [];

    const setupListeners = async () => {
      // Transcription events
      const unlistenTranscription = await tauriApi.events.onTranscription((event) => {
        store.addTranscription(event.payload.transcription);
      });
      unlisteners.push(unlistenTranscription);

      // Status updates
      const unlistenStatus = await tauriApi.events.onStatusUpdate((event) => {
        store.setServerStatus(event.payload.status);
      });
      unlisteners.push(unlistenStatus);

      // Audio level updates
      const unlistenAudioLevel = await tauriApi.events.onAudioLevel((event) => {
        store.setAudioLevel(event.payload.level);
      });
      unlisteners.push(unlistenAudioLevel);

      // Notifications
      const unlistenNotification = await tauriApi.events.onNotification((event) => {
        store.addNotification(event.payload.notification);
      });
      unlisteners.push(unlistenNotification);

      // Errors
      const unlistenError = await tauriApi.events.onError((event) => {
        const log: LogEntry = {
          id: crypto.randomUUID(),
          timestamp: new Date().toISOString(),
          level: 'error',
          component: event.payload.component,
          message: event.payload.error,
        };
        store.addLog(log);
      });
      unlisteners.push(unlistenError);
    };

    setupListeners().catch((err) => {
      console.error('Failed to setup event listeners:', err);
    });

    return () => {
      unlisteners.forEach((unlisten) => unlisten());
    };
  }, [store]);

  /**
   * Initialize on mount
   */
  useEffect(() => {
    initialize();
  }, [initialize]);

  return {
    isInitialized,
    error,
    retry: initialize,
  };
}

/**
 * Hook for recording controls
 */
export function useRecordingControls() {
  const { recording, activeProfile, setRecording } = useAppStore();

  const startRecording = useCallback(async () => {
    try {
      setRecording('listening' as RecordingState);
      await tauriApi.recording.start();
    } catch (err) {
      setRecording('error' as RecordingState);
      throw err;
    }
  }, [setRecording]);

  const stopRecording = useCallback(async () => {
    try {
      await tauriApi.recording.stop();
      setRecording('idle' as RecordingState);
    } catch (err) {
      console.error('Failed to stop recording:', err);
      throw err;
    }
  }, [setRecording]);

  const pauseRecording = useCallback(async () => {
    try {
      await tauriApi.recording.pause();
      setRecording('idle' as RecordingState);
    } catch (err) {
      console.error('Failed to pause recording:', err);
      throw err;
    }
  }, [setRecording]);

  const resumeRecording = useCallback(async () => {
    try {
      await tauriApi.recording.resume();
      setRecording('listening' as RecordingState);
    } catch (err) {
      console.error('Failed to resume recording:', err);
      throw err;
    }
  }, [setRecording]);

  return {
    recording,
    startRecording,
    stopRecording,
    pauseRecording,
    resumeRecording,
    isRecording: recording === 'listening' || recording === 'processing',
  };
}

/**
 * Hook for settings management
 */
export function useSettings() {
  const { settings, setSettings } = useAppStore();

  const updateSettings = useCallback(
    async (partial: Partial<AppSettings>) => {
      try {
        await tauriApi.settings.update(partial);
        setSettings({ ...settings, ...partial });
      } catch (err) {
        console.error('Failed to update settings:', err);
        throw err;
      }
    },
    [settings, setSettings]
  );

  const resetSettings = useCallback(async () => {
    try {
      await tauriApi.settings.reset();
      setSettings(DEFAULT_SETTINGS);
    } catch (err) {
      console.error('Failed to reset settings:', err);
      throw err;
    }
  }, [setSettings]);

  return {
    settings,
    updateSettings,
    resetSettings,
  };
}

/**
 * Hook for profile management
 */
export function useProfiles() {
  const { activeProfile, profiles, setActiveProfile, setProfiles } = useAppStore();

  const switchProfile = useCallback(
    async (profileId: string) => {
      try {
        await tauriApi.profile.switch(profileId);
        const profile = profiles.find((p) => p.id === profileId);
        if (profile) {
          setActiveProfile(profile);
        }
      } catch (err) {
        console.error('Failed to switch profile:', err);
        throw err;
      }
    },
    [profiles, setActiveProfile]
  );

  const createProfile = useCallback(
    async (profile: Omit<UserProfile, 'id'>) => {
      try {
        const newProfile = await tauriApi.profile.create(profile);
        setProfiles([...profiles, newProfile]);
        return newProfile;
      } catch (err) {
        console.error('Failed to create profile:', err);
        throw err;
      }
    },
    [profiles, setProfiles]
  );

  const updateProfile = useCallback(
    async (profileId: string, partial: Partial<UserProfile>) => {
      try {
        await tauriApi.profile.update(profileId, partial);
        setProfiles(
          profiles.map((p) => (p.id === profileId ? { ...p, ...partial } : p))
        );
      } catch (err) {
        console.error('Failed to update profile:', err);
        throw err;
      }
    },
    [profiles, setProfiles]
  );

  const deleteProfile = useCallback(
    async (profileId: string) => {
      try {
        await tauriApi.profile.delete(profileId);
        setProfiles(profiles.filter((p) => p.id !== profileId));
      } catch (err) {
        console.error('Failed to delete profile:', err);
        throw err;
      }
    },
    [profiles, setProfiles]
  );

  return {
    activeProfile,
    profiles,
    switchProfile,
    createProfile,
    updateProfile,
    deleteProfile,
  };
}

/**
 * Hook for server controls
 */
export function useServerControls() {
  const { serverStatus, setServerStatus } = useAppStore();

  const startServer = useCallback(async () => {
    try {
      await tauriApi.server.start();
      setServerStatus(ServerStatus.Starting);
    } catch (err) {
      setServerStatus(ServerStatus.Error);
      throw err;
    }
  }, [setServerStatus]);

  const stopServer = useCallback(async () => {
    try {
      await tauriApi.server.stop();
      setServerStatus(ServerStatus.Stopped);
    } catch (err) {
      console.error('Failed to stop server:', err);
      throw err;
    }
  }, [setServerStatus]);

  const restartServer = useCallback(async () => {
    try {
      await tauriApi.server.restart();
      setServerStatus(ServerStatus.Starting);
    } catch (err) {
      setServerStatus(ServerStatus.Error);
      throw err;
    }
  }, [setServerStatus]);

  return {
    serverStatus,
    startServer,
    stopServer,
    restartServer,
    isConnected: serverStatus === ServerStatus.Running,
  };
}
