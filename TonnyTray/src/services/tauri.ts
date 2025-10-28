/**
 * Minimal Tauri API service layer
 */

import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { AppSettings, ServerStatus, UserProfile } from '@types';

async function invokeCommand<T>(command: string, payload?: Record<string, unknown>): Promise<T> {
  try {
    return await invoke<T>(command, payload);
  } catch (error) {
    console.error(`Tauri command '${command}' failed:`, error);
    throw error;
  }
}

export const tauriApi = {
  state: {
    async get() {
      return invokeCommand('get_state');
    }
  },
  
  settings: {
    async get(): Promise<AppSettings> {
      return invokeCommand('get_settings');
    },
    async update(settings: Partial<AppSettings>) {
      return invokeCommand('update_settings', { settings });
    },
    async reset() {
      return invokeCommand('reset_settings');
    },
    async export(path: string) {
      return invokeCommand('export_settings', { path });
    },
    async import(path: string) {
      return invokeCommand('import_settings', { path });
    }
  },

  profile: {
    async getAll(): Promise<UserProfile[]> {
      return invokeCommand('get_profiles');
    },
    async switch(profileId: string) {
      return invokeCommand('switch_profile', { id: profileId });
    },
    async create(profile: Omit<UserProfile, 'id'>): Promise<UserProfile> {
      return invokeCommand('create_profile', { profile });
    },
    async update(profileId: string, partial: Partial<UserProfile>) {
      return invokeCommand('update_profile', { id: profileId, profile: partial });
    },
    async delete(profileId: string) {
      return invokeCommand('delete_profile', { id: profileId });
    }
  },

  server: {
    async getStatus(): Promise<ServerStatus> {
      return invokeCommand('get_server_status');
    },
    async start() {
      return invokeCommand('start_server');
    },
    async stop() {
      return invokeCommand('stop_server');
    },
    async restart() {
      return invokeCommand('restart_server');
    }
  },

  recording: {
    async start() {
      return invokeCommand('start_recording');
    },
    async stop() {
      return invokeCommand('stop_recording');
    },
    async pause() {
      return invokeCommand('pause_recording');
    },
    async resume() {
      return invokeCommand('resume_recording');
    }
  },

  audio: {
    async getDevices() {
      return invokeCommand('list_audio_devices').catch(() => []);
    },
    async testDevice(deviceId: string) {
      return invokeCommand('test_audio_device', { device_id: deviceId });
    }
  },

  integration: {
    async getVoices() {
      return invokeCommand('list_elevenlabs_voices').catch(() => []);
    },
    async testWebhook() {
      return invokeCommand('test_n8n_connection');
    },
    async testTTS(text: string) {
      return invokeCommand('speak_text', { text });
    },
    async sendCommand(command: string, profileId: string) {
      return invokeCommand('send_command', { command, profile_id: profileId });
    }
  },

  history: {
    async getStatistics() {
      return invokeCommand('get_statistics').catch(() => ({
        totalCommands: 0,
        successfulCommands: 0,
        failedCommands: 0,
        averageResponseTime: 0,
        uptime: 0
      }));
    }
  },

  logs: {
    async get(level?: string, limit?: number) {
      return invokeCommand('get_logs', { level, limit }).catch(() => []);
    },
    async clear() {
      return invokeCommand('clear_logs');
    },
    async export(path: string) {
      return invokeCommand('export_logs', { path });
    }
  },

  events: {
    async onTranscription(callback: (event: any) => void) {
      return listen('transcription', callback);
    },
    async onStatusUpdate(callback: (event: any) => void) {
      return listen('status-update', callback);
    },
    async onAudioLevel(callback: (event: any) => void) {
      return listen('audio-level', callback);
    },
    async onNotification(callback: (event: any) => void) {
      return listen('notification', callback);
    },
    async onError(callback: (event: any) => void) {
      return listen('error', callback);
    }
  }
};
