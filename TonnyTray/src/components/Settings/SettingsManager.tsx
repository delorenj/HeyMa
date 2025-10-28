/**
 * Settings import/export and reset functionality
 */

import { useState } from 'react';
import {
  Box,
  Paper,
  Typography,
  Button,
  Stack,
  Divider,
  Alert,
  CircularProgress,
} from '@mui/material';
import DownloadIcon from '@mui/icons-material/Download';
import UploadIcon from '@mui/icons-material/Upload';
import RestartAltIcon from '@mui/icons-material/RestartAlt';
import SaveIcon from '@mui/icons-material/Save';
import { useSettings } from '@hooks/useTauriState';
import { useConfirmDialog } from '@components/Common/ConfirmDialog';
import { useNotification } from '@hooks/useNotification';
import { tauriApi } from '@services/tauri';

export default function SettingsManager() {
  const { settings, resetSettings } = useSettings();
  const { showConfirm, dialog } = useConfirmDialog();
  const { showSuccess, showError, showInfo } = useNotification();
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);

  /**
   * Export settings to JSON file
   */
  const handleExport = async () => {
    setExporting(true);
    try {
      const filename = `tonnytray-settings-${Date.now()}.json`;
      await tauriApi.settings.export(filename);
      showSuccess(`Settings exported to ${filename}`);
    } catch (error) {
      showError(`Failed to export settings: ${error}`);
    } finally {
      setExporting(false);
    }
  };

  /**
   * Import settings from JSON file
   */
  const handleImport = async () => {
    await showConfirm({
      title: 'Import Settings?',
      message:
        'This will replace your current settings with those from the file. Your current settings will be lost unless you export them first.',
      severity: 'warning',
      confirmText: 'Import',
      confirmColor: 'primary',
      onConfirm: async () => {
        setImporting(true);
        try {
          // Use Tauri's file dialog to select file
          await tauriApi.settings.import('');
          showSuccess('Settings imported successfully');
          showInfo('Please restart the application for all changes to take effect');
        } catch (error) {
          showError(`Failed to import settings: ${error}`);
        } finally {
          setImporting(false);
        }
      },
    });
  };

  /**
   * Reset settings to defaults
   */
  const handleReset = async () => {
    await showConfirm({
      title: 'Reset Settings?',
      message:
        'This will reset all settings to their default values. This action cannot be undone. Consider exporting your settings first.',
      severity: 'error',
      confirmText: 'Reset to Defaults',
      confirmColor: 'error',
      onConfirm: async () => {
        try {
          await resetSettings();
          showSuccess('Settings reset to defaults');
          showInfo('Please restart the application for all changes to take effect');
        } catch (error) {
          showError(`Failed to reset settings: ${error}`);
        }
      },
    });
  };

  /**
   * Create backup of current settings
   */
  const handleBackup = async () => {
    try {
      const filename = `tonnytray-backup-${Date.now()}.json`;
      await tauriApi.settings.export(filename);
      showSuccess(`Backup created: ${filename}`);
    } catch (error) {
      showError(`Failed to create backup: ${error}`);
    }
  };

  return (
    <Box>
      <Paper elevation={2} sx={{ p: 3 }}>
        <Typography variant="h6" gutterBottom>
          Settings Management
        </Typography>

        <Stack spacing={3} sx={{ mt: 2 }}>
          {/* Export Section */}
          <Box>
            <Typography variant="subtitle2" gutterBottom sx={{ fontWeight: 'bold' }}>
              Export Settings
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              Save your current settings to a JSON file. You can import this file later to restore
              your configuration or transfer it to another machine.
            </Typography>
            <Stack direction="row" spacing={2}>
              <Button
                variant="outlined"
                startIcon={exporting ? <CircularProgress size={16} /> : <DownloadIcon />}
                onClick={handleExport}
                disabled={exporting}
              >
                Export Settings
              </Button>
              <Button
                variant="outlined"
                startIcon={<SaveIcon />}
                onClick={handleBackup}
                color="secondary"
              >
                Create Backup
              </Button>
            </Stack>
          </Box>

          <Divider />

          {/* Import Section */}
          <Box>
            <Typography variant="subtitle2" gutterBottom sx={{ fontWeight: 'bold' }}>
              Import Settings
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              Load settings from a previously exported JSON file. This will replace your current
              configuration.
            </Typography>
            <Button
              variant="outlined"
              startIcon={importing ? <CircularProgress size={16} /> : <UploadIcon />}
              onClick={handleImport}
              disabled={importing}
            >
              Import Settings
            </Button>
          </Box>

          <Divider />

          {/* Reset Section */}
          <Box>
            <Typography variant="subtitle2" gutterBottom sx={{ fontWeight: 'bold' }}>
              Reset to Defaults
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              Reset all settings to their factory defaults. This cannot be undone, so make sure to
              export your settings first if you want to keep them.
            </Typography>
            <Button
              variant="outlined"
              startIcon={<RestartAltIcon />}
              onClick={handleReset}
              color="error"
            >
              Reset to Defaults
            </Button>
          </Box>

          <Divider />

          {/* Current Settings Info */}
          <Box>
            <Typography variant="subtitle2" gutterBottom sx={{ fontWeight: 'bold' }}>
              Current Configuration
            </Typography>
            <Stack spacing={1}>
              <Typography variant="body2" color="text.secondary">
                <strong>Model:</strong> {settings.voice.model}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                <strong>Language:</strong>{' '}
                {settings.voice.autoDetectLanguage ? 'Auto-detect' : settings.voice.language}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                <strong>Server:</strong> {settings.server.url}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                <strong>ElevenLabs:</strong>{' '}
                {settings.integration.elevenLabsEnabled ? 'Enabled' : 'Disabled'}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                <strong>Response Mode:</strong> {settings.integration.responseMode}
              </Typography>
            </Stack>
          </Box>

          {/* Warning */}
          <Alert severity="warning">
            <Typography variant="body2">
              <strong>Important:</strong> Always export your settings before importing or
              resetting. Some changes may require restarting the application.
            </Typography>
          </Alert>
        </Stack>
      </Paper>

      {dialog}
    </Box>
  );
}
