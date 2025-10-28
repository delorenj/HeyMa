/**
 * Server configuration tab
 * WhisperLiveKit server settings and management
 */

import { useState } from 'react';
import {
  Box,
  Typography,
  TextField,
  Switch,
  FormControlLabel,
  Button,
  Stack,
  Alert,
  Chip,
} from '@mui/material';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import StopIcon from '@mui/icons-material/Stop';
import RestartAltIcon from '@mui/icons-material/RestartAlt';
import { useSettings, useServerControls } from '@hooks/useTauriState';
import { ServerStatus } from '@types';
import { getStatusColor } from '@theme/index';

export default function ServerConfigTab() {
  const { settings, updateSettings } = useSettings();
  const { serverStatus, startServer, stopServer, restartServer } =
    useServerControls();
  const [loading, setLoading] = useState(false);

  const handleUrlChange = (url: string) => {
    updateSettings({
      server: { ...settings.server, url },
    });
  };

  const handlePortChange = (port: number) => {
    updateSettings({
      server: { ...settings.server, port },
    });
  };

  const handleAutoStartToggle = (autoStart: boolean) => {
    updateSettings({
      server: { ...settings.server, autoStart },
    });
  };

  const handleAutoRestartToggle = (autoRestart: boolean) => {
    updateSettings({
      server: { ...settings.server, autoRestart },
    });
  };

  const handlePythonPathChange = (pythonPath: string) => {
    updateSettings({
      server: { ...settings.server, pythonPath: pythonPath || null },
    });
  };

  const handleStartServer = async () => {
    setLoading(true);
    try {
      await startServer();
    } catch (error) {
      console.error('Failed to start server:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleStopServer = async () => {
    setLoading(true);
    try {
      await stopServer();
    } catch (error) {
      console.error('Failed to stop server:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleRestartServer = async () => {
    setLoading(true);
    try {
      await restartServer();
    } catch (error) {
      console.error('Failed to restart server:', error);
    } finally {
      setLoading(false);
    }
  };

  return (
    <Stack spacing={3}>
      <Box>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 2 }}>
          <Typography variant="h6">Server Status</Typography>
          <Chip
            label={serverStatus}
            size="small"
            sx={{
              bgcolor: getStatusColor(serverStatus) + '20',
              color: getStatusColor(serverStatus),
              textTransform: 'capitalize',
            }}
          />
        </Box>

        <Stack direction="row" spacing={2}>
          <Button
            variant="contained"
            startIcon={<PlayArrowIcon />}
            onClick={handleStartServer}
            disabled={serverStatus === ServerStatus.Running || loading}
            fullWidth
          >
            Start Server
          </Button>
          <Button
            variant="outlined"
            startIcon={<StopIcon />}
            onClick={handleStopServer}
            disabled={serverStatus === ServerStatus.Stopped || loading}
            fullWidth
          >
            Stop Server
          </Button>
          <Button
            variant="outlined"
            startIcon={<RestartAltIcon />}
            onClick={handleRestartServer}
            disabled={serverStatus === ServerStatus.Stopped || loading}
            fullWidth
          >
            Restart
          </Button>
        </Stack>
      </Box>

      <Box>
        <Typography variant="h6" gutterBottom>
          Connection Settings
        </Typography>
        <Stack spacing={2}>
          <TextField
            label="Server URL"
            value={settings.server.url}
            onChange={(e) => handleUrlChange(e.target.value)}
            placeholder="ws://localhost:8888/asr"
            fullWidth
          />

          <TextField
            label="Port"
            type="number"
            value={settings.server.port}
            onChange={(e) => handlePortChange(parseInt(e.target.value, 10))}
            fullWidth
          />

          <TextField
            label="Python Path (Optional)"
            value={settings.server.pythonPath || ''}
            onChange={(e) => handlePythonPathChange(e.target.value)}
            placeholder="/usr/bin/python3"
            helperText="Leave empty to use system default"
            fullWidth
          />
        </Stack>
      </Box>

      <Box>
        <Typography variant="h6" gutterBottom>
          Startup Options
        </Typography>
        <Stack spacing={2}>
          <FormControlLabel
            control={
              <Switch
                checked={settings.server.autoStart}
                onChange={(e) => handleAutoStartToggle(e.target.checked)}
              />
            }
            label="Start server on boot"
          />

          <FormControlLabel
            control={
              <Switch
                checked={settings.server.autoRestart}
                onChange={(e) => handleAutoRestartToggle(e.target.checked)}
              />
            }
            label="Auto-restart on crash"
          />
        </Stack>
      </Box>

      <Alert severity="info">
        The WhisperLiveKit server must be running to process voice commands. When
        auto-start is enabled, the server will automatically start when TonnyTray
        launches.
      </Alert>
    </Stack>
  );
}
