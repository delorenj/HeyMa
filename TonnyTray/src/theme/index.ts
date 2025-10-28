/**
 * Theme configuration using Material-UI
 * Supports light, dark, and system-based theme switching
 */

import { createTheme, ThemeOptions } from '@mui/material/styles';
import type { PaletteMode } from '@mui/material';
import { useMemo } from 'react';
import { useAppStore } from '@hooks/useTauriState';

/**
 * Color palette definitions
 */
const colors = {
  primary: {
    main: '#2196F3',
    light: '#64B5F6',
    dark: '#1976D2',
    contrastText: '#FFFFFF',
  },
  secondary: {
    main: '#FF9800',
    light: '#FFB74D',
    dark: '#F57C00',
    contrastText: '#FFFFFF',
  },
  success: {
    main: '#4CAF50',
    light: '#81C784',
    dark: '#388E3C',
  },
  error: {
    main: '#F44336',
    light: '#E57373',
    dark: '#D32F2F',
  },
  warning: {
    main: '#FF9800',
    light: '#FFB74D',
    dark: '#F57C00',
  },
  info: {
    main: '#2196F3',
    light: '#64B5F6',
    dark: '#1976D2',
  },
};

/**
 * Recording state colors
 */
export const recordingStateColors = {
  idle: '#9E9E9E',
  listening: '#2196F3',
  processing: '#FF9800',
  error: '#F44336',
};

/**
 * Server status colors
 */
export const serverStatusColors = {
  stopped: '#9E9E9E',
  starting: '#FF9800',
  running: '#4CAF50',
  stopping: '#FF9800',
  error: '#F44336',
};

/**
 * Get theme options for specified mode
 */
function getThemeOptions(mode: PaletteMode): ThemeOptions {
  return {
    palette: {
      mode,
      primary: colors.primary,
      secondary: colors.secondary,
      success: colors.success,
      error: colors.error,
      warning: colors.warning,
      info: colors.info,
      background: {
        default: mode === 'light' ? '#F5F5F5' : '#121212',
        paper: mode === 'light' ? '#FFFFFF' : '#1E1E1E',
      },
      text: {
        primary: mode === 'light' ? '#212121' : '#FFFFFF',
        secondary: mode === 'light' ? '#757575' : '#B0B0B0',
      },
    },
    typography: {
      fontFamily: [
        '-apple-system',
        'BlinkMacSystemFont',
        '"Segoe UI"',
        'Roboto',
        '"Helvetica Neue"',
        'Arial',
        'sans-serif',
        '"Apple Color Emoji"',
        '"Segoe UI Emoji"',
        '"Segoe UI Symbol"',
      ].join(','),
      h1: {
        fontSize: '2.5rem',
        fontWeight: 600,
        lineHeight: 1.2,
      },
      h2: {
        fontSize: '2rem',
        fontWeight: 600,
        lineHeight: 1.3,
      },
      h3: {
        fontSize: '1.75rem',
        fontWeight: 600,
        lineHeight: 1.4,
      },
      h4: {
        fontSize: '1.5rem',
        fontWeight: 600,
        lineHeight: 1.4,
      },
      h5: {
        fontSize: '1.25rem',
        fontWeight: 600,
        lineHeight: 1.5,
      },
      h6: {
        fontSize: '1rem',
        fontWeight: 600,
        lineHeight: 1.5,
      },
      body1: {
        fontSize: '1rem',
        lineHeight: 1.5,
      },
      body2: {
        fontSize: '0.875rem',
        lineHeight: 1.5,
      },
      button: {
        textTransform: 'none',
        fontWeight: 500,
      },
    },
    shape: {
      borderRadius: 8,
    },
    components: {
      MuiButton: {
        styleOverrides: {
          root: {
            borderRadius: 8,
            padding: '8px 16px',
            fontWeight: 500,
          },
          contained: {
            boxShadow: 'none',
            '&:hover': {
              boxShadow: 'none',
            },
          },
        },
      },
      MuiCard: {
        styleOverrides: {
          root: {
            borderRadius: 12,
            boxShadow:
              mode === 'light'
                ? '0px 2px 8px rgba(0, 0, 0, 0.1)'
                : '0px 2px 8px rgba(0, 0, 0, 0.3)',
          },
        },
      },
      MuiPaper: {
        styleOverrides: {
          root: {
            backgroundImage: 'none',
          },
        },
      },
      MuiTextField: {
        styleOverrides: {
          root: {
            '& .MuiOutlinedInput-root': {
              borderRadius: 8,
            },
          },
        },
      },
      MuiSwitch: {
        styleOverrides: {
          root: {
            width: 42,
            height: 26,
            padding: 0,
            '& .MuiSwitch-switchBase': {
              padding: 0,
              margin: 2,
              transitionDuration: '300ms',
              '&.Mui-checked': {
                transform: 'translateX(16px)',
                color: '#fff',
                '& + .MuiSwitch-track': {
                  backgroundColor: colors.primary.main,
                  opacity: 1,
                  border: 0,
                },
              },
            },
            '& .MuiSwitch-thumb': {
              boxSizing: 'border-box',
              width: 22,
              height: 22,
            },
            '& .MuiSwitch-track': {
              borderRadius: 13,
              backgroundColor: mode === 'light' ? '#E9E9EA' : '#39393D',
              opacity: 1,
            },
          },
        },
      },
      MuiTab: {
        styleOverrides: {
          root: {
            textTransform: 'none',
            fontWeight: 500,
            fontSize: '0.875rem',
            minHeight: 48,
          },
        },
      },
      MuiChip: {
        styleOverrides: {
          root: {
            borderRadius: 6,
            fontWeight: 500,
          },
        },
      },
      MuiTooltip: {
        styleOverrides: {
          tooltip: {
            backgroundColor: mode === 'light' ? '#616161' : '#E0E0E0',
            color: mode === 'light' ? '#FFFFFF' : '#000000',
            fontSize: '0.75rem',
            borderRadius: 6,
          },
        },
      },
    },
  };
}

/**
 * Create theme based on mode
 */
export function createAppTheme(mode: PaletteMode) {
  return createTheme(getThemeOptions(mode));
}

/**
 * Hook to get current theme based on settings
 */
export function useAppTheme() {
  const themeMode = useAppStore((state) => state.settings.theme);

  const prefersDarkMode =
    typeof window !== 'undefined' &&
    window.matchMedia('(prefers-color-scheme: dark)').matches;

  const resolvedMode = useMemo(() => {
    if (themeMode === 'system') {
      return prefersDarkMode ? 'dark' : 'light';
    }
    return themeMode;
  }, [themeMode, prefersDarkMode]);

  const theme = useMemo(() => createAppTheme(resolvedMode), [resolvedMode]);

  return { theme, mode: resolvedMode };
}

/**
 * Get status color based on connection state
 */
export function getStatusColor(
  status: 'stopped' | 'starting' | 'running' | 'stopping' | 'error'
): string {
  return serverStatusColors[status];
}

/**
 * Get recording state color
 */
export function getRecordingColor(
  state: 'idle' | 'listening' | 'processing' | 'error'
): string {
  return recordingStateColors[state];
}
