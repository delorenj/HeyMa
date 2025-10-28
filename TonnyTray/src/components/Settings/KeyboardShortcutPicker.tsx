/**
 * Keyboard shortcut picker component
 * Allows users to configure custom keyboard shortcuts
 */

import { useState } from 'react';
import {
  Box,
  TextField,
  Button,
  Chip,
  Stack,
  Typography,
  InputAdornment,
  Alert,
} from '@mui/material';
import KeyboardIcon from '@mui/icons-material/Keyboard';
import EditIcon from '@mui/icons-material/Edit';
import { useHotkeyCapture } from '@hooks/useKeyboardShortcut';
import { formatHotkey } from '@utils/formatters';

interface KeyboardShortcutPickerProps {
  label: string;
  value: string;
  onChange: (hotkey: string) => void;
  helperText?: string | undefined;
  error?: boolean;
  errorText?: string | undefined;
}

export default function KeyboardShortcutPicker({
  label,
  value,
  onChange,
  helperText,
  error = false,
  errorText,
}: KeyboardShortcutPickerProps) {
  const { isCapturing, startCapture, stopCapture, capturedKeys } = useHotkeyCapture((hotkey) => {
    handleChange(hotkey);
  });

  const [showConflict, setShowConflict] = useState(false);

  // Visual display of current hotkey
  const displayValue = value ? formatHotkey(value) : 'Not set';

  // Check for common conflicts
  const checkConflict = (hotkey: string) => {
    const commonShortcuts = [
      'Ctrl+C',
      'Ctrl+V',
      'Ctrl+X',
      'Ctrl+Z',
      'Ctrl+Y',
      'Ctrl+A',
      'Ctrl+S',
      'Alt+F4',
      'Alt+Tab',
    ];
    return commonShortcuts.includes(hotkey);
  };

  const handleChange = (newHotkey: string) => {
    if (checkConflict(newHotkey)) {
      setShowConflict(true);
      setTimeout(() => setShowConflict(false), 3000);
      return;
    }
    onChange(newHotkey);
  };

  return (
    <Box>
      <Typography variant="body2" sx={{ mb: 1, fontWeight: 'medium' }}>
        {label}
      </Typography>

      <Stack spacing={2}>
        {/* Display current shortcut */}
        <TextField
          value={displayValue}
          disabled
          fullWidth
          error={error}
          helperText={error ? errorText : helperText}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <KeyboardIcon />
              </InputAdornment>
            ),
            endAdornment: (
              <InputAdornment position="end">
                {!isCapturing && (
                  <Button
                    size="small"
                    startIcon={<EditIcon />}
                    onClick={startCapture}
                    variant="outlined"
                  >
                    Change
                  </Button>
                )}
                {isCapturing && (
                  <Button size="small" onClick={stopCapture} variant="text" color="error">
                    Cancel
                  </Button>
                )}
              </InputAdornment>
            ),
          }}
        />

        {/* Capture mode indicator */}
        {isCapturing && (
          <Alert severity="info" icon={<KeyboardIcon />}>
            <Typography variant="body2" sx={{ fontWeight: 'bold' }}>
              Press your desired key combination now...
            </Typography>
            {capturedKeys.length > 0 && (
              <Box sx={{ mt: 1 }}>
                <Stack direction="row" spacing={0.5}>
                  {capturedKeys.map((key, index) => (
                    <Chip key={index} label={key} size="small" color="primary" />
                  ))}
                </Stack>
              </Box>
            )}
          </Alert>
        )}

        {/* Conflict warning */}
        {showConflict && (
          <Alert severity="warning">
            This shortcut conflicts with a common system shortcut. Please choose a different
            combination.
          </Alert>
        )}

        {/* Current keys being pressed */}
        {value && !isCapturing && (
          <Box>
            <Typography variant="caption" color="text.secondary" sx={{ mb: 0.5, display: 'block' }}>
              Current shortcut:
            </Typography>
            <Stack direction="row" spacing={0.5}>
              {value.split('+').map((key, index) => (
                <Chip key={index} label={key.trim()} size="small" variant="outlined" />
              ))}
            </Stack>
          </Box>
        )}
      </Stack>
    </Box>
  );
}

/**
 * Multiple shortcuts manager
 */
interface ShortcutConfig {
  id: string;
  label: string;
  value: string;
  description?: string;
}

interface KeyboardShortcutsManagerProps {
  shortcuts: ShortcutConfig[];
  onChange: (id: string, hotkey: string) => void;
}

export function KeyboardShortcutsManager({ shortcuts, onChange }: KeyboardShortcutsManagerProps) {
  return (
    <Stack spacing={3}>
      <Typography variant="h6">Keyboard Shortcuts</Typography>

      {shortcuts.map((shortcut) => (
        <KeyboardShortcutPicker
          key={shortcut.id}
          label={shortcut.label}
          value={shortcut.value}
          onChange={(hotkey) => onChange(shortcut.id, hotkey)}
          helperText={shortcut.description}
        />
      ))}

      <Alert severity="info">
        <Typography variant="body2">
          <strong>Tips:</strong>
        </Typography>
        <ul style={{ margin: '8px 0', paddingLeft: '20px' }}>
          <li>Use modifier keys (Ctrl, Shift, Alt) for better compatibility</li>
          <li>Avoid common system shortcuts</li>
          <li>Function keys (F1-F12) are good choices</li>
        </ul>
      </Alert>
    </Stack>
  );
}
