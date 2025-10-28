/**
 * Audio level visualization component
 * Displays real-time audio input levels with smooth animations
 */

import { useEffect, useRef, useState } from 'react';
import { Box, Paper, Typography, Stack, useTheme } from '@mui/material';
import { motion, AnimatePresence } from 'framer-motion';
import MicIcon from '@mui/icons-material/Mic';
import MicOffIcon from '@mui/icons-material/MicOff';
import { useAppStore } from '@hooks/useTauriState';
import { formatAudioLevel } from '@utils/formatters';

interface AudioLevelMeterProps {
  showLabel?: boolean;
  showDbValue?: boolean;
  height?: number;
  minDb?: number;
}

export default function AudioLevelMeter({
  showLabel = true,
  showDbValue = false,
  height = 200,
  minDb = -60,
}: AudioLevelMeterProps) {
  const theme = useTheme();
  const { audioLevel, recording } = useAppStore();
  const [smoothedLevel, setSmoothedLevel] = useState(0);
  const [peak, setPeak] = useState(0);
  const peakTimeoutRef = useRef<NodeJS.Timeout>();

  // Smooth the audio level with interpolation
  useEffect(() => {
    if (!audioLevel) {
      setSmoothedLevel(0);
      return;
    }

    const targetLevel = audioLevel.level;
    const smoothing = 0.3; // Lower = smoother

    const interval = setInterval(() => {
      setSmoothedLevel((prev) => {
        const diff = targetLevel - prev;
        return prev + diff * smoothing;
      });
    }, 16); // ~60fps

    return () => clearInterval(interval);
  }, [audioLevel]);

  // Update peak hold
  useEffect(() => {
    if (!audioLevel) return;

    if (audioLevel.peak > peak) {
      setPeak(audioLevel.peak);

      // Clear existing timeout
      if (peakTimeoutRef.current) {
        clearTimeout(peakTimeoutRef.current);
      }

      // Reset peak after 1 second
      peakTimeoutRef.current = setTimeout(() => {
        setPeak(0);
      }, 1000);
    }
  }, [audioLevel, peak]);

  // Calculate visual level (0-1 range)
  const visualLevel = smoothedLevel;
  const visualPeak = peak;

  // Map to dB for display
  const currentDb = smoothedLevel > 0 ? 20 * Math.log10(smoothedLevel) : minDb;

  // Calculate color based on level
  const getColor = (level: number) => {
    if (level < 0.3) return theme.palette.success.main;
    if (level < 0.7) return theme.palette.warning.main;
    return theme.palette.error.main;
  };

  const isActive = recording === 'listening' || recording === 'processing';

  return (
    <Paper
      elevation={2}
      sx={{
        p: 2,
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        bgcolor: 'background.paper',
      }}
    >
      {showLabel && (
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 2 }}>
          {isActive ? (
            <MicIcon color="primary" />
          ) : (
            <MicOffIcon sx={{ color: 'text.disabled' }} />
          )}
          <Typography variant="subtitle2" color={isActive ? 'primary' : 'text.disabled'}>
            Audio Level
          </Typography>
          {showDbValue && (
            <Typography variant="caption" color="text.secondary" sx={{ ml: 'auto' }}>
              {formatAudioLevel(smoothedLevel)}
            </Typography>
          )}
        </Stack>
      )}

      {/* Vertical level meter */}
      <Box
        sx={{
          flex: 1,
          position: 'relative',
          bgcolor: 'grey.900',
          borderRadius: 1,
          overflow: 'hidden',
          minHeight: height,
        }}
      >
        {/* Background grid lines */}
        <Box
          sx={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'space-between',
            py: 0.5,
          }}
        >
          {[0, -10, -20, -30, -40, -50, -60].map((db) => (
            <Box
              key={db}
              sx={{
                height: 1,
                bgcolor: 'grey.800',
                borderTop: 1,
                borderColor: 'grey.700',
              }}
            />
          ))}
        </Box>

        {/* Level bar */}
        <AnimatePresence>
          <motion.div
            style={{
              position: 'absolute',
              bottom: 0,
              left: 0,
              right: 0,
              height: `${visualLevel * 100}%`,
              background: `linear-gradient(to top, ${getColor(visualLevel)}, ${getColor(
                visualLevel * 0.5
              )})`,
              boxShadow: `0 0 20px ${getColor(visualLevel)}40`,
            }}
            initial={{ height: 0 }}
            animate={{ height: `${visualLevel * 100}%` }}
            exit={{ height: 0 }}
            transition={{ duration: 0.05 }}
          />
        </AnimatePresence>

        {/* Peak indicator */}
        {visualPeak > 0 && (
          <motion.div
            style={{
              position: 'absolute',
              bottom: `${visualPeak * 100}%`,
              left: 0,
              right: 0,
              height: 2,
              backgroundColor: theme.palette.error.main,
              boxShadow: `0 0 10px ${theme.palette.error.main}`,
            }}
            initial={{ opacity: 1 }}
            animate={{ opacity: 0.8 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.3 }}
          />
        )}

        {/* dB scale markers */}
        <Box
          sx={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'space-between',
            py: 0.5,
            px: 1,
            pointerEvents: 'none',
          }}
        >
          {[0, -10, -20, -30, -40, -50, -60].map((db) => (
            <Typography
              key={db}
              variant="caption"
              sx={{
                fontSize: '0.65rem',
                color: 'grey.600',
                textAlign: 'right',
              }}
            >
              {db}
            </Typography>
          ))}
        </Box>
      </Box>

      {/* Numeric display */}
      {showDbValue && (
        <Box sx={{ mt: 1, textAlign: 'center' }}>
          <Typography variant="h6" color={getColor(visualLevel)}>
            {currentDb > minDb ? currentDb.toFixed(1) : minDb} dB
          </Typography>
        </Box>
      )}
    </Paper>
  );
}

/**
 * Horizontal compact audio level meter
 */
export function CompactAudioLevelMeter() {
  const theme = useTheme();
  const { audioLevel, recording } = useAppStore();
  const [smoothedLevel, setSmoothedLevel] = useState(0);

  useEffect(() => {
    if (!audioLevel) {
      setSmoothedLevel(0);
      return;
    }

    const targetLevel = audioLevel.level;
    const smoothing = 0.3;

    const interval = setInterval(() => {
      setSmoothedLevel((prev) => prev + (targetLevel - prev) * smoothing);
    }, 16);

    return () => clearInterval(interval);
  }, [audioLevel]);

  const getColor = (level: number) => {
    if (level < 0.3) return theme.palette.success.main;
    if (level < 0.7) return theme.palette.warning.main;
    return theme.palette.error.main;
  };

  const isActive = recording === 'listening' || recording === 'processing';

  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
      {isActive ? <MicIcon fontSize="small" color="primary" /> : <MicOffIcon fontSize="small" />}
      <Box
        sx={{
          flex: 1,
          height: 8,
          bgcolor: 'grey.800',
          borderRadius: 1,
          overflow: 'hidden',
          position: 'relative',
        }}
      >
        <motion.div
          style={{
            position: 'absolute',
            left: 0,
            top: 0,
            bottom: 0,
            width: `${smoothedLevel * 100}%`,
            backgroundColor: getColor(smoothedLevel),
            boxShadow: `0 0 8px ${getColor(smoothedLevel)}80`,
          }}
          animate={{ width: `${smoothedLevel * 100}%` }}
          transition={{ duration: 0.05 }}
        />
      </Box>
    </Box>
  );
}
