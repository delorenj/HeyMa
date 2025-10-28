/**
 * Advanced settings tab
 * Auto-typing, command prefix, confirmation mode, and logging
 */

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
  Slider,
  Stack,
  Button,
  Alert,
} from '@mui/material';
import DeleteIcon from '@mui/icons-material/Delete';
import { useSettings } from '@hooks/useTauriState';
import type { ConfirmationMode } from '@types';

export default function AdvancedTab() {
  const { settings, updateSettings, resetSettings } = useSettings();

  const handleAutoTypingToggle = (autoTyping: boolean) => {
    updateSettings({
      advanced: { ...settings.advanced, autoTyping },
    });
  };

  const handleTypingSpeedChange = (_event: Event, value: number | number[]) => {
    updateSettings({
      advanced: { ...settings.advanced, typingSpeed: value as number },
    });
  };

  const handleTargetFilterChange = (filter: string) => {
    updateSettings({
      advanced: { ...settings.advanced, targetApplicationFilter: filter },
    });
  };

  const handleCommandPrefixChange = (prefix: string) => {
    updateSettings({
      advanced: { ...settings.advanced, commandPrefix: prefix },
    });
  };

  const handleConfirmationModeChange = (mode: ConfirmationMode) => {
    updateSettings({
      advanced: { ...settings.advanced, confirmationMode: mode },
    });
  };

  const handleLogLevelChange = (
    level: 'debug' | 'info' | 'warn' | 'error'
  ) => {
    updateSettings({
      advanced: { ...settings.advanced, logLevel: level },
    });
  };

  const handleMaxLogEntriesChange = (max: number) => {
    updateSettings({
      advanced: { ...settings.advanced, maxLogEntries: max },
    });
  };

  const handleResetSettings = async () => {
    if (
      window.confirm(
        'Are you sure you want to reset all settings to defaults? This cannot be undone.'
      )
    ) {
      try {
        await resetSettings();
      } catch (error) {
        console.error('Failed to reset settings:', error);
      }
    }
  };

  return (
    <Stack spacing={3}>
      <Box>
        <Typography variant="h6" gutterBottom>
          Auto-Typing Behavior
        </Typography>
        <Stack spacing={2}>
          <FormControlLabel
            control={
              <Switch
                checked={settings.advanced.autoTyping}
                onChange={(e) => handleAutoTypingToggle(e.target.checked)}
              />
            }
            label="Enable automatic typing of responses"
          />

          {settings.advanced.autoTyping && (
            <>
              <Box>
                <Typography variant="body2" gutterBottom>
                  Typing Speed (characters per second)
                </Typography>
                <Slider
                  value={settings.advanced.typingSpeed}
                  onChange={handleTypingSpeedChange}
                  min={10}
                  max={200}
                  step={5}
                  marks={[
                    { value: 10, label: 'Slow' },
                    { value: 100, label: 'Medium' },
                    { value: 200, label: 'Fast' },
                  ]}
                  valueLabelDisplay="auto"
                />
              </Box>

              <TextField
                label="Target Application Filter (Optional)"
                value={settings.advanced.targetApplicationFilter}
                onChange={(e) => handleTargetFilterChange(e.target.value)}
                placeholder="e.g., chrome, vscode, terminal"
                helperText="Only type in these applications (comma-separated). Leave empty for all apps."
                fullWidth
              />
            </>
          )}
        </Stack>
      </Box>

      <Box>
        <Typography variant="h6" gutterBottom>
          Command Processing
        </Typography>
        <Stack spacing={2}>
          <TextField
            label="Command Prefix"
            value={settings.advanced.commandPrefix}
            onChange={(e) => handleCommandPrefixChange(e.target.value)}
            placeholder="Computer,"
            helperText="Commands must start with this prefix to be processed"
            fullWidth
          />

          <FormControl fullWidth>
            <InputLabel>Confirmation Mode</InputLabel>
            <Select
              value={settings.advanced.confirmationMode}
              label="Confirmation Mode"
              onChange={(e) =>
                handleConfirmationModeChange(e.target.value as ConfirmationMode)
              }
            >
              <MenuItem value="silent">Silent (No confirmation)</MenuItem>
              <MenuItem value="visual">Visual (Show notification)</MenuItem>
              <MenuItem value="audio">Audio (Play beep/voice)</MenuItem>
            </Select>
          </FormControl>
        </Stack>
      </Box>

      <Box>
        <Typography variant="h6" gutterBottom>
          Logging
        </Typography>
        <Stack spacing={2}>
          <FormControl fullWidth>
            <InputLabel>Log Level</InputLabel>
            <Select
              value={settings.advanced.logLevel}
              label="Log Level"
              onChange={(e) =>
                handleLogLevelChange(
                  e.target.value as 'debug' | 'info' | 'warn' | 'error'
                )
              }
            >
              <MenuItem value="debug">Debug (Most verbose)</MenuItem>
              <MenuItem value="info">Info</MenuItem>
              <MenuItem value="warn">Warnings only</MenuItem>
              <MenuItem value="error">Errors only</MenuItem>
            </Select>
          </FormControl>

          <TextField
            label="Maximum Log Entries"
            type="number"
            value={settings.advanced.maxLogEntries}
            onChange={(e) => handleMaxLogEntriesChange(parseInt(e.target.value, 10))}
            helperText="Older entries will be automatically deleted"
            fullWidth
          />
        </Stack>
      </Box>

      <Box>
        <Typography variant="h6" gutterBottom>
          Danger Zone
        </Typography>
        <Alert severity="warning" sx={{ mb: 2 }}>
          Resetting settings will restore all configuration to default values. This
          action cannot be undone.
        </Alert>
        <Button
          variant="outlined"
          color="error"
          startIcon={<DeleteIcon />}
          onClick={handleResetSettings}
          fullWidth
        >
          Reset All Settings to Defaults
        </Button>
      </Box>
    </Stack>
  );
}
