/**
 * Error screen component
 * Shown when initialization fails
 */

import { Box, Typography, Button, Alert } from '@mui/material';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import RefreshIcon from '@mui/icons-material/Refresh';

interface ErrorScreenProps {
  error: string;
  onRetry: () => void;
}

export default function ErrorScreen({ error, onRetry }: ErrorScreenProps) {
  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100vh',
        gap: 3,
        px: 4,
      }}
    >
      <ErrorOutlineIcon sx={{ fontSize: 64, color: 'error.main' }} />
      <Typography variant="h5" align="center">
        Failed to Initialize
      </Typography>
      <Alert severity="error" sx={{ maxWidth: 500 }}>
        {error}
      </Alert>
      <Button
        variant="contained"
        startIcon={<RefreshIcon />}
        onClick={onRetry}
        size="large"
      >
        Retry
      </Button>
    </Box>
  );
}
