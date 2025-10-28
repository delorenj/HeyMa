/**
 * Voice configuration tab
 * Model selection, language, microphone, and voice activation settings
 */

import { useState } from 'react';
import {
  Box,
  Typography,
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  Switch,
  FormControlLabel,
  TextField,
  Slider,
  Button,
  LinearProgress,
  Stack,
  Alert,
} from '@mui/material';
import VolumeUpIcon from '@mui/icons-material/VolumeUp';
import { useSettings, useAppStore } from '@hooks/useTauriState';
import { tauriApi } from '@services/tauri';
import type { WhisperModel } from '@types';

const WHISPER_MODELS: WhisperModel[] = ['tiny', 'base', 'small', 'medium', 'large-v3'];

const LANGUAGES = [
  { code: 'auto', name: 'Auto Detect' },
  { code: 'en', name: 'English' },
  { code: 'es', name: 'Spanish' },
  { code: 'fr', name: 'French' },
  { code: 'de', name: 'German' },
  { code: 'it', name: 'Italian' },
  { code: 'pt', name: 'Portuguese' },
  { code: 'nl', name: 'Dutch' },
  { code: 'pl', name: 'Polish' },
  { code: 'ru', name: 'Russian' },
  { code: 'zh', name: 'Chinese' },
  { code: 'ja', name: 'Japanese' },
  { code: 'ko', name: 'Korean' },
];

export default function VoiceConfigTab() {
  const { settings, updateSettings } = useSettings();
  const { availableDevices, audioLevel } = useAppStore();
  const [testing, setTesting] = useState(false);

  const handleModelChange = (model: WhisperModel) => {
    updateSettings({
      voice: { ...settings.voice, model },
    });
  };

  const handleLanguageChange = (language: string) => {
    updateSettings({
      voice: {
        ...settings.voice,
        language,
        autoDetectLanguage: language === 'auto',
      },
    });
  };

  const handleMicrophoneChange = (microphone: string) => {
    updateSettings({
      voice: { ...settings.voice, microphone },
    });
  };

  const handlePushToTalkToggle = (pushToTalk: boolean) => {
    updateSettings({
      voice: { ...settings.voice, pushToTalk },
    });
  };

  const handleVoiceActivationToggle = (voiceActivation: boolean) => {
    updateSettings({
      voice: { ...settings.voice, voiceActivation },
    });
  };

  const handleThresholdChange = (_event: Event, value: number | number[]) => {
    updateSettings({
      voice: {
        ...settings.voice,
        voiceActivationThreshold: value as number,
      },
    });
  };

  const handleTestMicrophone = async () => {
    if (!settings.voice.microphone) return;

    setTesting(true);
    try {
      await tauriApi.audio.testDevice(settings.voice.microphone);
    } catch (error) {
      console.error('Microphone test failed:', error);
    } finally {
      setTesting(false);
    }
  };

  return (
    <Stack spacing={3}>
      <Box>
        <Typography variant="h6" gutterBottom>
          Model Selection
        </Typography>
        <FormControl fullWidth>
          <InputLabel>Whisper Model</InputLabel>
          <Select
            value={settings.voice.model}
            label="Whisper Model"
            onChange={(e) => handleModelChange(e.target.value as WhisperModel)}
          >
            {WHISPER_MODELS.map((model) => (
              <MenuItem key={model} value={model}>
                {model}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
        <Alert severity="info" sx={{ mt: 1 }}>
          Larger models are more accurate but slower. Recommended: base or small
        </Alert>
      </Box>

      <Box>
        <Typography variant="h6" gutterBottom>
          Language
        </Typography>
        <FormControl fullWidth>
          <InputLabel>Language</InputLabel>
          <Select
            value={
              settings.voice.autoDetectLanguage ? 'auto' : settings.voice.language
            }
            label="Language"
            onChange={(e) => handleLanguageChange(e.target.value)}
          >
            {LANGUAGES.map((lang) => (
              <MenuItem key={lang.code} value={lang.code}>
                {lang.name}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      </Box>

      <Box>
        <Typography variant="h6" gutterBottom>
          Microphone
        </Typography>
        <FormControl fullWidth sx={{ mb: 2 }}>
          <InputLabel>Input Device</InputLabel>
          <Select
            value={settings.voice.microphone || ''}
            label="Input Device"
            onChange={(e) => handleMicrophoneChange(e.target.value)}
          >
            {availableDevices.map((device) => (
              <MenuItem key={device.id} value={device.id}>
                {device.name}
                {device.isDefault && ' (Default)'}
              </MenuItem>
            ))}
          </Select>
        </FormControl>

        <Button
          variant="outlined"
          startIcon={<VolumeUpIcon />}
          onClick={handleTestMicrophone}
          disabled={!settings.voice.microphone || testing}
          fullWidth
        >
          Test Microphone
        </Button>

        {audioLevel && (
          <Box sx={{ mt: 2 }}>
            <Typography variant="body2" color="text.secondary" gutterBottom>
              Audio Level
            </Typography>
            <LinearProgress
              variant="determinate"
              value={audioLevel.level * 100}
              sx={{ height: 8, borderRadius: 1 }}
            />
          </Box>
        )}
      </Box>

      <Box>
        <Typography variant="h6" gutterBottom>
          Recording Mode
        </Typography>
        <Stack spacing={2}>
          <FormControlLabel
            control={
              <Switch
                checked={settings.voice.pushToTalk}
                onChange={(e) => handlePushToTalkToggle(e.target.checked)}
              />
            }
            label="Push-to-Talk Mode"
          />

          {settings.voice.pushToTalk && (
            <TextField
              label="Hotkey"
              value={settings.voice.pushToTalkHotkey}
              disabled
              helperText="Press the key combination you want to use"
              fullWidth
            />
          )}

          <FormControlLabel
            control={
              <Switch
                checked={settings.voice.voiceActivation}
                onChange={(e) => handleVoiceActivationToggle(e.target.checked)}
              />
            }
            label="Voice Activation"
          />

          {settings.voice.voiceActivation && (
            <Box>
              <Typography variant="body2" gutterBottom>
                Activation Threshold
              </Typography>
              <Slider
                value={settings.voice.voiceActivationThreshold}
                onChange={handleThresholdChange}
                min={0}
                max={1}
                step={0.05}
                marks
                valueLabelDisplay="auto"
                valueLabelFormat={(value) => `${Math.round(value * 100)}%`}
              />
            </Box>
          )}
        </Stack>
      </Box>
    </Stack>
  );
}
