/**
 * useKeyboardShortcut hook
 * Handles keyboard shortcut registration and detection
 */

import { useEffect, useCallback, useRef, useState } from 'react';

interface KeyboardShortcutOptions {
  enabled?: boolean;
  preventDefault?: boolean;
  stopPropagation?: boolean;
}

/**
 * Parse hotkey string (e.g., "Ctrl+Shift+V") into key components
 */
function parseHotkey(hotkey: string): {
  key: string;
  ctrl: boolean;
  shift: boolean;
  alt: boolean;
  meta: boolean;
} {
  const parts = hotkey
    .split('+')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);

  const key = (parts.pop() ?? '').toLowerCase();
  const modifiers = new Set(parts.map((part) => part.toLowerCase()));

  return {
    key,
    ctrl: modifiers.has('ctrl') || modifiers.has('control'),
    shift: modifiers.has('shift'),
    alt: modifiers.has('alt'),
    meta: modifiers.has('meta') || modifiers.has('cmd') || modifiers.has('command'),
  };
}

/**
 * Check if keyboard event matches hotkey
 */
function matchesHotkey(
  event: KeyboardEvent,
  hotkey: { key: string; ctrl: boolean; shift: boolean; alt: boolean; meta: boolean }
): boolean {
  return (
    event.key.toLowerCase() === hotkey.key.toLowerCase() &&
    event.ctrlKey === hotkey.ctrl &&
    event.shiftKey === hotkey.shift &&
    event.altKey === hotkey.alt &&
    event.metaKey === hotkey.meta
  );
}

export function useKeyboardShortcut(
  hotkey: string,
  callback: (event: KeyboardEvent) => void,
  options: KeyboardShortcutOptions = {}
): void {
  const { enabled = true, preventDefault = true, stopPropagation = false } = options;

  const callbackRef = useRef(callback);
  const hotkeyRef = useRef(parseHotkey(hotkey));

  // Update refs on changes
  useEffect(() => {
    callbackRef.current = callback;
    hotkeyRef.current = parseHotkey(hotkey);
  }, [callback, hotkey]);

  const handleKeyDown = useCallback(
    (event: KeyboardEvent) => {
      if (!enabled) return;

      if (matchesHotkey(event, hotkeyRef.current)) {
        if (preventDefault) {
          event.preventDefault();
        }
        if (stopPropagation) {
          event.stopPropagation();
        }
        callbackRef.current(event);
      }
    },
    [enabled, preventDefault, stopPropagation]
  );

  useEffect(() => {
    if (!enabled) return;

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [enabled, handleKeyDown]);
}

/**
 * Hook to capture keyboard input for hotkey configuration
 */
export function useHotkeyCapture(
  onCapture: (hotkey: string) => void
): {
  isCapturing: boolean;
  startCapture: () => void;
  stopCapture: () => void;
  capturedKeys: string[];
} {
  const [isCapturing, setIsCapturing] = useState(false);
  const [capturedKeys, setCapturedKeys] = useState<string[]>([]);

  const handleKeyDown = useCallback(
    (event: KeyboardEvent) => {
      if (!isCapturing) return;

      event.preventDefault();
      event.stopPropagation();

      const keys: string[] = [];
      if (event.ctrlKey) keys.push('Ctrl');
      if (event.shiftKey) keys.push('Shift');
      if (event.altKey) keys.push('Alt');
      if (event.metaKey) keys.push('Meta');

      // Only add non-modifier keys
      if (!['Control', 'Shift', 'Alt', 'Meta'].includes(event.key)) {
        keys.push(event.key);
      }

      if (keys.length > 0) {
        setCapturedKeys(keys);
        const hotkey = keys.join('+');
        onCapture(hotkey);
        setIsCapturing(false);
      }
    },
    [isCapturing, onCapture]
  );

  useEffect(() => {
    if (!isCapturing) return;

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isCapturing, handleKeyDown]);

  const startCapture = useCallback(() => {
    setCapturedKeys([]);
    setIsCapturing(true);
  }, []);

  const stopCapture = useCallback(() => {
    setIsCapturing(false);
    setCapturedKeys([]);
  }, []);

  return { isCapturing, startCapture, stopCapture, capturedKeys };
}
