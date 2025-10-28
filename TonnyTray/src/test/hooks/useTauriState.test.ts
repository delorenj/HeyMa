/**
 * Tests for useTauriState hook
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useAppStore, useTauriState, useRecordingControls, useSettings, useServerControls } from '@hooks/useTauriState';
import { tauriApi } from '@services/tauri';
import {
  ServerStatus,
  RecordingState,
  DEFAULT_SETTINGS,
  DEFAULT_GUEST_PROFILE,
} from '@types';
import type { AppSettings, UserProfile, AudioDevice, Statistics, Transcription, Notification } from '@types';

// Mock Tauri API
vi.mock('@services/tauri', () => ({
  tauriApi: {
    settings: {
      get: vi.fn(),
      update: vi.fn(),
      reset: vi.fn(),
    },
    profile: {
      getAll: vi.fn(),
      switch: vi.fn(),
      create: vi.fn(),
      update: vi.fn(),
      delete: vi.fn(),
    },
    audio: {
      getDevices: vi.fn(),
    },
    history: {
      getStatistics: vi.fn(),
    },
    server: {
      getStatus: vi.fn(),
      start: vi.fn(),
      stop: vi.fn(),
      restart: vi.fn(),
    },
    recording: {
      start: vi.fn(),
      stop: vi.fn(),
      pause: vi.fn(),
      resume: vi.fn(),
    },
    integration: {
      getVoices: vi.fn(),
    },
    events: {
      onTranscription: vi.fn(() => Promise.resolve(() => {})),
      onStatusUpdate: vi.fn(() => Promise.resolve(() => {})),
      onAudioLevel: vi.fn(() => Promise.resolve(() => {})),
      onNotification: vi.fn(() => Promise.resolve(() => {})),
      onError: vi.fn(() => Promise.resolve(() => {})),
    },
  },
}));

const cloneSettings = (): AppSettings =>
  JSON.parse(JSON.stringify(DEFAULT_SETTINGS)) as AppSettings;

const cloneProfile = (overrides: Partial<UserProfile> = {}): UserProfile => ({
  ...DEFAULT_GUEST_PROFILE,
  settings: { ...DEFAULT_GUEST_PROFILE.settings },
  allowedCommands: [...DEFAULT_GUEST_PROFILE.allowedCommands],
  blockedCommands: [...DEFAULT_GUEST_PROFILE.blockedCommands],
  usageStats: { ...DEFAULT_GUEST_PROFILE.usageStats },
  ...overrides,
});

const createTranscription = (overrides: Partial<Transcription> = {}): Transcription => ({
  id: 'transcription-id',
  timestamp: new Date().toISOString(),
  text: 'Transcription text',
  confidence: 0.95,
  duration: 1.2,
  profileId: DEFAULT_GUEST_PROFILE.id,
  success: true,
  ...overrides,
});

const createNotification = (overrides: Partial<Notification> = {}): Notification => ({
  id: 'notification-id',
  type: 'info',
  title: 'Notification',
  message: 'Notification message',
  timestamp: new Date().toISOString(),
  ...overrides,
});

const createDevice = (overrides: Partial<AudioDevice> = {}): AudioDevice => ({
  id: 'device-id',
  name: 'Default Microphone',
  isDefault: false,
  sampleRate: 44100,
  channels: 2,
  ...overrides,
});

describe('useAppStore', () => {
  beforeEach(() => {
    // Reset store to initial state
    useAppStore.setState({
      recording: RecordingState.Idle,
      audioLevel: null,
      connectionStatus: {
        server: ServerStatus.Stopped,
        n8n: false,
        elevenLabs: false,
        lastChecked: new Date().toISOString(),
      },
      serverStatus: ServerStatus.Stopped,
      lastTranscription: null,
      activeProfile: cloneProfile(),
      profiles: [cloneProfile()],
      settings: cloneSettings(),
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
    });
  });

  it('should initialize with default state', () => {
    const { result } = renderHook(() => useAppStore());

    expect(result.current.recording).toBe(RecordingState.Idle);
    expect(result.current.serverStatus).toBe(ServerStatus.Stopped);
    expect(result.current.transcriptions).toEqual([]);
    expect(result.current.logs).toEqual([]);
  });

  it('should update recording state', () => {
    const { result } = renderHook(() => useAppStore());

    act(() => {
      result.current.setRecording(RecordingState.Listening);
    });

    expect(result.current.recording).toBe(RecordingState.Listening);
  });

  it('should update server status and connection status', () => {
    const { result } = renderHook(() => useAppStore());

    act(() => {
      result.current.setServerStatus(ServerStatus.Running);
    });

    expect(result.current.serverStatus).toBe(ServerStatus.Running);
    expect(result.current.connectionStatus.server).toBe(ServerStatus.Running);
  });

  it('should add transcriptions with limit', () => {
    const { result } = renderHook(() => useAppStore());

    act(() => {
      // Add 60 transcriptions
      for (let i = 0; i < 60; i++) {
        result.current.addTranscription(
          createTranscription({
            id: `trans-${i}`,
            text: `Transcription ${i}`,
          })
        );
      }
    });

    // Should keep only 50 most recent
    expect(result.current.transcriptions.length).toBe(50);
    expect(result.current.transcriptions[0]?.text).toBe('Transcription 59');
    expect(result.current.lastTranscription?.text).toBe('Transcription 59');
  });

  it('should add and remove notifications', () => {
    const { result } = renderHook(() => useAppStore());

    const notification = createNotification({ id: 'notif-1', message: 'Test notification' });

    act(() => {
      result.current.addNotification(notification);
    });

    expect(result.current.notifications.length).toBe(1);
    expect(result.current.notifications[0]?.message).toBe('Test notification');

    act(() => {
      result.current.removeNotification('notif-1');
    });

    expect(result.current.notifications.length).toBe(0);
  });

  it('should update statistics', () => {
    const { result } = renderHook(() => useAppStore());

    const stats: Statistics = {
      totalCommands: 100,
      successfulCommands: 85,
      failedCommands: 15,
      averageResponseTime: 250,
      uptime: 3600,
    };

    act(() => {
      result.current.setStatistics(stats);
    });

    expect(result.current.statistics.totalCommands).toBe(100);
    expect(result.current.statistics.successfulCommands).toBe(85);
  });

  it('should manage profiles', () => {
    const { result } = renderHook(() => useAppStore());

    const profiles: UserProfile[] = [
      cloneProfile({
        id: 'admin',
        name: 'Admin',
        permissions: 'admin',
        allowedCommands: [],
        blockedCommands: [],
      }),
      cloneProfile({
        id: 'user',
        name: 'User',
        permissions: 'user',
        allowedCommands: ['read'],
        blockedCommands: [],
      }),
    ];

    act(() => {
      result.current.setProfiles(profiles);
      result.current.setActiveProfile(profiles[0]!);
    });

    expect(result.current.profiles.length).toBe(2);
    expect(result.current.activeProfile.name).toBe('Admin');
  });

  it('should manage audio devices', () => {
    const { result } = renderHook(() => useAppStore());

    const devices: AudioDevice[] = [
      createDevice({ id: 'device-1', name: 'Microphone 1', isDefault: true }),
      createDevice({ id: 'device-2', name: 'Microphone 2', isDefault: false }),
    ];

    act(() => {
      result.current.setAvailableDevices(devices);
    });

    expect(result.current.availableDevices.length).toBe(2);
    expect(result.current.availableDevices[0]?.isDefault).toBe(true);
  });
});

describe('useTauriState', () => {
  const mockSettings: AppSettings = (() => {
    const settings = cloneSettings();
    settings.voice = { ...settings.voice, model: 'small', language: 'en' };
    settings.integration = { ...settings.integration, responseMode: 'text' };
    settings.server = {
      ...settings.server,
      url: 'ws://localhost:9000/asr',
      port: 9000,
    };
    return settings;
  })();

  const mockProfiles: UserProfile[] = [
    cloneProfile({
      id: 'admin',
      name: 'Admin',
      permissions: 'admin',
      allowedCommands: [],
      blockedCommands: [],
    }),
  ];

  const mockDevices: AudioDevice[] = [
    createDevice({ id: 'default', name: 'Default Microphone', isDefault: true }),
  ];

  const mockStatistics: Statistics = {
    totalCommands: 0,
    successfulCommands: 0,
    failedCommands: 0,
    averageResponseTime: 0,
    uptime: 0,
  };

  beforeEach(() => {
    vi.mocked(tauriApi.settings.get).mockResolvedValue(mockSettings);
    vi.mocked(tauriApi.profile.getAll).mockResolvedValue(mockProfiles);
    vi.mocked(tauriApi.audio.getDevices).mockResolvedValue(mockDevices);
    vi.mocked(tauriApi.history.getStatistics).mockResolvedValue(mockStatistics);
    vi.mocked(tauriApi.server.getStatus).mockResolvedValue(ServerStatus.Stopped);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('should initialize state from backend', async () => {
    const { result } = renderHook(() => useTauriState());

    await waitFor(() => {
      expect(result.current.isInitialized).toBe(true);
    });

    expect(result.current.error).toBeNull();
    expect(tauriApi.settings.get).toHaveBeenCalled();
    expect(tauriApi.profile.getAll).toHaveBeenCalled();
    expect(tauriApi.audio.getDevices).toHaveBeenCalled();
  });

  it('should handle initialization error', async () => {
    vi.mocked(tauriApi.settings.get).mockRejectedValue(new Error('Network error'));

    const { result } = renderHook(() => useTauriState());

    await waitFor(() => {
      expect(result.current.error).not.toBeNull();
    });

    expect(result.current.error).toContain('Network error');
    expect(result.current.isInitialized).toBe(false);
  });

  it('should retry initialization', async () => {
    vi.mocked(tauriApi.settings.get).mockRejectedValueOnce(new Error('Timeout'));

    const { result } = renderHook(() => useTauriState());

    await waitFor(() => {
      expect(result.current.error).not.toBeNull();
    });

    // Reset mock to succeed
    vi.mocked(tauriApi.settings.get).mockResolvedValue(mockSettings);

    act(() => {
      result.current.retry();
    });

    await waitFor(() => {
      expect(result.current.isInitialized).toBe(true);
    });
  });
});

describe('useRecordingControls', () => {
  beforeEach(() => {
    useAppStore.setState({
      recording: RecordingState.Idle,
      activeProfile: cloneProfile({
        id: 'test-profile',
        name: 'Test',
        permissions: 'admin',
        allowedCommands: [],
        blockedCommands: [],
      }),
      profiles: [
        cloneProfile({
          id: 'test-profile',
          name: 'Test',
          permissions: 'admin',
          allowedCommands: [],
          blockedCommands: [],
        }),
      ],
    } as any);
  });

  it('should start recording', async () => {
    vi.mocked(tauriApi.recording.start).mockResolvedValue(undefined);

    const { result } = renderHook(() => useRecordingControls());

    await act(async () => {
      await result.current.startRecording();
    });

    expect(tauriApi.recording.start).toHaveBeenCalledWith('test-profile');
    expect(result.current.recording).toBe(RecordingState.Listening);
    expect(result.current.isRecording).toBe(true);
  });

  it('should stop recording', async () => {
    vi.mocked(tauriApi.recording.stop).mockResolvedValue(undefined);

    useAppStore.setState({ recording: RecordingState.Listening } as any);

    const { result } = renderHook(() => useRecordingControls());

    await act(async () => {
      await result.current.stopRecording();
    });

    expect(tauriApi.recording.stop).toHaveBeenCalled();
    expect(result.current.recording).toBe(RecordingState.Idle);
    expect(result.current.isRecording).toBe(false);
  });

  it('should handle recording errors', async () => {
    vi.mocked(tauriApi.recording.start).mockRejectedValue(new Error('Microphone error'));

    const { result } = renderHook(() => useRecordingControls());

    await expect(async () => {
      await act(async () => {
        await result.current.startRecording();
      });
    }).rejects.toThrow('Microphone error');

    expect(result.current.recording).toBe(RecordingState.Error);
  });
});

describe('useSettings', () => {
  const mockSettings: AppSettings = (() => {
    const settings = cloneSettings();
    settings.server = {
      ...settings.server,
      url: 'ws://localhost:8888/asr',
      port: 8888,
    };
    return settings;
  })();

  beforeEach(() => {
    useAppStore.setState({ settings: mockSettings } as any);
  });

  it('should update settings', async () => {
    vi.mocked(tauriApi.settings.update).mockResolvedValue(undefined);

    const { result } = renderHook(() => useSettings());

    const updates = {
      server: { ...mockSettings.server, port: 9999 },
    };

    await act(async () => {
      await result.current.updateSettings(updates);
    });

    expect(tauriApi.settings.update).toHaveBeenCalledWith(updates);
    expect(result.current.settings.server.port).toBe(9999);
  });

  it('should reset settings', async () => {
    vi.mocked(tauriApi.settings.reset).mockResolvedValue(undefined);

    const { result } = renderHook(() => useSettings());

    await act(async () => {
      await result.current.resetSettings();
    });

    expect(tauriApi.settings.reset).toHaveBeenCalled();
  });
});

describe('useServerControls', () => {
  beforeEach(() => {
    useAppStore.setState({
      serverStatus: ServerStatus.Stopped,
    } as any);
  });

  it('should start server', async () => {
    vi.mocked(tauriApi.server.start).mockResolvedValue(undefined);

    const { result } = renderHook(() => useServerControls());

    await act(async () => {
      await result.current.startServer();
    });

    expect(tauriApi.server.start).toHaveBeenCalled();
    expect(result.current.serverStatus).toBe(ServerStatus.Starting);
  });

  it('should stop server', async () => {
    vi.mocked(tauriApi.server.stop).mockResolvedValue(undefined);

    useAppStore.setState({ serverStatus: ServerStatus.Running } as any);

    const { result } = renderHook(() => useServerControls());

    await act(async () => {
      await result.current.stopServer();
    });

    expect(tauriApi.server.stop).toHaveBeenCalled();
    expect(result.current.serverStatus).toBe(ServerStatus.Stopped);
  });

  it('should restart server', async () => {
    vi.mocked(tauriApi.server.restart).mockResolvedValue(undefined);

    const { result } = renderHook(() => useServerControls());

    await act(async () => {
      await result.current.restartServer();
    });

    expect(tauriApi.server.restart).toHaveBeenCalled();
    expect(result.current.serverStatus).toBe(ServerStatus.Starting);
  });
});
