/**
 * Status panel component
 * Shows connection status for all services
 */

import { Paper, Box, Typography, Chip, Stack, Divider } from '@mui/material';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import ErrorIcon from '@mui/icons-material/Error';
import PendingIcon from '@mui/icons-material/Pending';
import CancelIcon from '@mui/icons-material/Cancel';
import { useAppStore } from '@hooks/useTauriState';
import { ServerStatus } from '@types';
import { getStatusColor } from '@theme/index';

export default function StatusPanel() {
  const { connectionStatus, serverStatus, statistics } = useAppStore();

  const getStatusIcon = (connected: boolean) => {
    return connected ? (
      <CheckCircleIcon sx={{ fontSize: 16 }} />
    ) : (
      <CancelIcon sx={{ fontSize: 16 }} />
    );
  };

  const getServerStatusIcon = () => {
    switch (serverStatus) {
      case ServerStatus.Running:
        return <CheckCircleIcon sx={{ fontSize: 16 }} />;
      case ServerStatus.Starting:
        return <PendingIcon sx={{ fontSize: 16 }} />;
      case ServerStatus.Error:
        return <ErrorIcon sx={{ fontSize: 16 }} />;
      default:
        return <CancelIcon sx={{ fontSize: 16 }} />;
    }
  };

  return (
    <Paper sx={{ p: 3 }}>
      <Typography variant="h6" gutterBottom>
        Status
      </Typography>

      <Stack spacing={2}>
        <Box>
          <Typography variant="body2" color="text.secondary" gutterBottom>
            Services
          </Typography>
          <Stack spacing={1}>
            <Chip
              icon={getServerStatusIcon()}
              label={`WhisperLiveKit: ${serverStatus}`}
              size="small"
              sx={{
                bgcolor: getStatusColor(serverStatus) + '20',
                color: getStatusColor(serverStatus),
                '& .MuiChip-icon': { color: getStatusColor(serverStatus) },
              }}
            />
            <Chip
              icon={getStatusIcon(connectionStatus.n8n)}
              label={`n8n: ${connectionStatus.n8n ? 'Connected' : 'Disconnected'}`}
              size="small"
              sx={{
                bgcolor: connectionStatus.n8n ? '#4CAF5020' : '#9E9E9E20',
                color: connectionStatus.n8n ? '#4CAF50' : '#9E9E9E',
                '& .MuiChip-icon': {
                  color: connectionStatus.n8n ? '#4CAF50' : '#9E9E9E',
                },
              }}
            />
            <Chip
              icon={getStatusIcon(connectionStatus.elevenLabs)}
              label={`ElevenLabs: ${connectionStatus.elevenLabs ? 'Connected' : 'Disconnected'}`}
              size="small"
              sx={{
                bgcolor: connectionStatus.elevenLabs ? '#4CAF5020' : '#9E9E9E20',
                color: connectionStatus.elevenLabs ? '#4CAF50' : '#9E9E9E',
                '& .MuiChip-icon': {
                  color: connectionStatus.elevenLabs ? '#4CAF50' : '#9E9E9E',
                },
              }}
            />
          </Stack>
        </Box>

        <Divider />

        <Box>
          <Typography variant="body2" color="text.secondary" gutterBottom>
            Statistics
          </Typography>
          <Stack spacing={0.5}>
            <Typography variant="body2">
              Total Commands: {statistics.totalCommands}
            </Typography>
            <Typography variant="body2">
              Success Rate:{' '}
              {statistics.totalCommands > 0
                ? Math.round(
                    (statistics.successfulCommands / statistics.totalCommands) * 100
                  )
                : 0}
              %
            </Typography>
            <Typography variant="body2">
              Avg Response Time: {statistics.averageResponseTime.toFixed(0)}ms
            </Typography>
          </Stack>
        </Box>
      </Stack>
    </Paper>
  );
}
