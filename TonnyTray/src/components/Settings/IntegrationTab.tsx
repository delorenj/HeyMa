/**
 * Integration settings tab
 * n8n webhook, ElevenLabs API, and response mode configuration
 */

import { useState } from 'react';
import {
  Box,
  Typography,
  TextField,
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  Switch,
  FormControlLabel,
  Button,
  Stack,
  IconButton,
  InputAdornment,
  Alert,
  CircularProgress,
} from '@mui/material';
import VisibilityIcon from '@mui/icons-material/Visibility';
import VisibilityOffIcon from '@mui/icons-material/VisibilityOff';
import TestIcon from '@mui/icons-material/Science';
import VolumeUpIcon from '@mui/icons-material/VolumeUp';
import { useSettings, useAppStore } from '@hooks/useTauriState';
import { tauriApi } from '@services/tauri';
import type { ResponseMode } from '@types';

export default function IntegrationTab() {
  const { settings, updateSettings } = useSettings();
  const { availableVoices } = useAppStore();
  const [showApiKey, setShowApiKey] = useState(false);
  const [testingWebhook, setTestingWebhook] = useState(false);
  const [testingTTS, setTestingTTS] = useState(false);
  const [webhookResult, setWebhookResult] = useState<{
    success: boolean;
    message: string;
  } | null>(null);

  const handleWebhookUrlChange = (url: string) => {
    updateSettings({
      integration: { ...settings.integration, n8nWebhookUrl: url },
    });
  };

  const handleApiKeyChange = (apiKey: string) => {
    updateSettings({
      integration: { ...settings.integration, elevenLabsApiKey: apiKey },
    });
  };

  const handleVoiceIdChange = (voiceId: string) => {
    updateSettings({
      integration: { ...settings.integration, elevenLabsVoiceId: voiceId },
    });
  };

  const handleElevenLabsToggle = (enabled: boolean) => {
    updateSettings({
      integration: { ...settings.integration, elevenLabsEnabled: enabled },
    });
  };

  const handleResponseModeChange = (mode: ResponseMode) => {
    updateSettings({
      integration: { ...settings.integration, responseMode: mode },
    });
  };

  const handleTestWebhook = async () => {
    setTestingWebhook(true);
    setWebhookResult(null);

    try {
      const success = await tauriApi.integration.testWebhook(
        settings.integration.n8nWebhookUrl,
        'Test message from TonnyTray'
      );
      setWebhookResult({
        success,
        message: success
          ? 'Webhook connection successful!'
          : 'Failed to connect to webhook',
      });
    } catch (error) {
      setWebhookResult({
        success: false,
        message: error instanceof Error ? error.message : 'Unknown error',
      });
    } finally {
      setTestingWebhook(false);
    }
  };

  const handleTestTTS = async () => {
    setTestingTTS(true);

    try {
      await tauriApi.integration.testTTS(
        'Hello! This is a test of the text to speech system.',
        settings.integration.elevenLabsVoiceId
      );
    } catch (error) {
      console.error('TTS test failed:', error);
    } finally {
      setTestingTTS(false);
    }
  };

  return (
    <Stack spacing={3}>
      <Box>
        <Typography variant="h6" gutterBottom>
          n8n Integration
        </Typography>
        <TextField
          label="Webhook URL"
          value={settings.integration.n8nWebhookUrl}
          onChange={(e) => handleWebhookUrlChange(e.target.value)}
          placeholder="https://n8n.example.com/webhook/ask-tonny"
          fullWidth
          sx={{ mb: 2 }}
        />
        <Button
          variant="outlined"
          startIcon={testingWebhook ? <CircularProgress size={16} /> : <TestIcon />}
          onClick={handleTestWebhook}
          disabled={!settings.integration.n8nWebhookUrl || testingWebhook}
          fullWidth
        >
          Test Webhook
        </Button>

        {webhookResult && (
          <Alert severity={webhookResult.success ? 'success' : 'error'} sx={{ mt: 2 }}>
            {webhookResult.message}
          </Alert>
        )}
      </Box>

      <Box>
        <Typography variant="h6" gutterBottom>
          ElevenLabs Text-to-Speech
        </Typography>
        <FormControlLabel
          control={
            <Switch
              checked={settings.integration.elevenLabsEnabled}
              onChange={(e) => handleElevenLabsToggle(e.target.checked)}
            />
          }
          label="Enable ElevenLabs TTS"
          sx={{ mb: 2 }}
        />

        {settings.integration.elevenLabsEnabled && (
          <Stack spacing={2}>
            <TextField
              label="API Key"
              type={showApiKey ? 'text' : 'password'}
              value={settings.integration.elevenLabsApiKey}
              onChange={(e) => handleApiKeyChange(e.target.value)}
              placeholder="sk_..."
              fullWidth
              InputProps={{
                endAdornment: (
                  <InputAdornment position="end">
                    <IconButton
                      onClick={() => setShowApiKey(!showApiKey)}
                      edge="end"
                    >
                      {showApiKey ? <VisibilityOffIcon /> : <VisibilityIcon />}
                    </IconButton>
                  </InputAdornment>
                ),
              }}
            />

            <FormControl fullWidth>
              <InputLabel>Voice</InputLabel>
              <Select
                value={settings.integration.elevenLabsVoiceId}
                label="Voice"
                onChange={(e) => handleVoiceIdChange(e.target.value)}
              >
                {availableVoices.length > 0 ? (
                  availableVoices.map((voice) => (
                    <MenuItem key={voice.id} value={voice.id}>
                      {voice.name}
                    </MenuItem>
                  ))
                ) : (
                  <MenuItem value="">No voices available</MenuItem>
                )}
              </Select>
            </FormControl>

            <Button
              variant="outlined"
              startIcon={
                testingTTS ? <CircularProgress size={16} /> : <VolumeUpIcon />
              }
              onClick={handleTestTTS}
              disabled={
                !settings.integration.elevenLabsApiKey ||
                !settings.integration.elevenLabsVoiceId ||
                testingTTS
              }
              fullWidth
            >
              Test Voice
            </Button>
          </Stack>
        )}
      </Box>

      <Box>
        <Typography variant="h6" gutterBottom>
          Response Mode
        </Typography>
        <FormControl fullWidth>
          <InputLabel>How should responses be delivered?</InputLabel>
          <Select
            value={settings.integration.responseMode}
            label="How should responses be delivered?"
            onChange={(e) => handleResponseModeChange(e.target.value as ResponseMode)}
          >
            <MenuItem value="text">Text Only (Type responses)</MenuItem>
            <MenuItem value="voice">Voice Only (Speak responses)</MenuItem>
            <MenuItem value="both">Both (Type and speak)</MenuItem>
          </Select>
        </FormControl>
        <Alert severity="info" sx={{ mt: 1 }}>
          {settings.integration.responseMode === 'text' &&
            'Responses will be typed automatically'}
          {settings.integration.responseMode === 'voice' &&
            'Responses will be spoken using ElevenLabs'}
          {settings.integration.responseMode === 'both' &&
            'Responses will be both typed and spoken'}
        </Alert>
      </Box>
    </Stack>
  );
}
