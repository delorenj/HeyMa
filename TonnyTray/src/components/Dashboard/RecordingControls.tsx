/**
 * Recording controls component
 * Provides start/stop/pause controls with visual feedback and animations
 */

import { useState } from 'react';
import {
  Paper,
  Box,
  Button,
  IconButton,
  LinearProgress,
  Typography,
  Stack,
  Tooltip,
} from '@mui/material';
import MicIcon from '@mui/icons-material/Mic';
import MicOffIcon from '@mui/icons-material/MicOff';
import PauseIcon from '@mui/icons-material/Pause';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import { motion, AnimatePresence } from 'framer-motion';
import { useRecordingControls } from '@hooks/useTauriState';
import { useNotification } from '@hooks/useNotification';
import { getRecordingColor } from '@theme/index';

export default function RecordingControls() {
  const {
    recording,
    startRecording,
    stopRecording,
    pauseRecording,
    resumeRecording,
    isRecording,
  } = useRecordingControls();
  const { showError, showSuccess } = useNotification();

  const [loading, setLoading] = useState(false);

  const handleStartStop = async () => {
    setLoading(true);
    try {
      if (isRecording) {
        await stopRecording();
        showSuccess('Recording stopped');
      } else {
        await startRecording();
        showSuccess('Recording started');
      }
    } catch (error) {
      console.error('Recording control error:', error);
      showError(`Failed to ${isRecording ? 'stop' : 'start'} recording`);
    } finally {
      setLoading(false);
    }
  };

  const handlePauseResume = async () => {
    setLoading(true);
    try {
      if (recording === 'listening') {
        await pauseRecording();
        showSuccess('Recording paused');
      } else {
        await resumeRecording();
        showSuccess('Recording resumed');
      }
    } catch (error) {
      console.error('Pause/resume error:', error);
      showError(`Failed to ${recording === 'listening' ? 'pause' : 'resume'} recording`);
    } finally {
      setLoading(false);
    }
  };

  const getStatusText = () => {
    switch (recording) {
      case 'listening':
        return 'Recording...';
      case 'processing':
        return 'Processing...';
      case 'error':
        return 'Error';
      default:
        return 'Ready';
    }
  };

  return (
    <Paper
      component={motion.div}
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      sx={{
        p: 3,
        position: 'relative',
        overflow: 'hidden',
      }}
    >
      {/* Pulsing background for active recording */}
      <AnimatePresence>
        {recording === 'listening' && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: [0.1, 0.2, 0.1] }}
            exit={{ opacity: 0 }}
            transition={{ repeat: Infinity, duration: 2 }}
            style={{
              position: 'absolute',
              inset: 0,
              background: 'radial-gradient(circle, rgba(25, 118, 210, 0.2) 0%, transparent 70%)',
              pointerEvents: 'none',
            }}
          />
        )}
      </AnimatePresence>

      <Stack spacing={2}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
          {/* Main recording button */}
          <Tooltip title="Start/stop voice recording" arrow>
            <Button
              component={motion.button}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
              variant={isRecording ? 'outlined' : 'contained'}
              color={isRecording ? 'error' : 'primary'}
              size="large"
              startIcon={
                isRecording ? (
                  <motion.div
                    animate={{ scale: [1, 1.2, 1] }}
                    transition={{ repeat: Infinity, duration: 1.5 }}
                  >
                    <MicOffIcon />
                  </motion.div>
                ) : (
                  <MicIcon />
                )
              }
              onClick={handleStartStop}
              disabled={loading}
              sx={{ flex: 1, py: 2 }}
            >
              {isRecording ? 'Stop Recording' : 'Start Recording'}
            </Button>
          </Tooltip>

          {/* Pause/Resume button */}
          <AnimatePresence>
            {isRecording && (
              <motion.div
                initial={{ opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 20 }}
                transition={{ duration: 0.2 }}
              >
                <Tooltip
                  title={recording === 'listening' ? 'Pause recording' : 'Resume recording'}
                  arrow
                >
                  <IconButton
                    component={motion.button}
                    whileHover={{ scale: 1.1 }}
                    whileTap={{ scale: 0.9 }}
                    color="primary"
                    onClick={handlePauseResume}
                    disabled={loading}
                    size="large"
                  >
                    {recording === 'listening' ? <PauseIcon /> : <PlayArrowIcon />}
                  </IconButton>
                </Tooltip>
              </motion.div>
            )}
          </AnimatePresence>
        </Box>

        {/* Status indicator */}
        <Box>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5 }}>
            <motion.div
              animate={
                recording === 'listening'
                  ? { scale: [1, 1.3, 1], opacity: [1, 0.5, 1] }
                  : {}
              }
              transition={{ repeat: Infinity, duration: 1.5 }}
            >
              <Box
                sx={{
                  width: 10,
                  height: 10,
                  borderRadius: '50%',
                  bgcolor: getRecordingColor(recording),
                  boxShadow: recording === 'listening' ? `0 0 10px ${getRecordingColor(recording)}` : 'none',
                }}
              />
            </motion.div>
            <Typography variant="body2" color="text.secondary">
              {getStatusText()}
            </Typography>
          </Box>
          <AnimatePresence>
            {recording === 'processing' && (
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
              >
                <LinearProgress />
              </motion.div>
            )}
          </AnimatePresence>
        </Box>

        {/* Keyboard shortcut hint */}
        <Typography variant="caption" color="text.secondary" align="center">
          Press <strong>Ctrl+Shift+V</strong> to start/stop recording
        </Typography>
      </Stack>
    </Paper>
  );
}
