/**
 * Utility functions for formatting various data types
 */

import { formatDistanceToNow, format, formatDuration, intervalToDuration } from 'date-fns';

/**
 * Format timestamp to relative time (e.g., "2 minutes ago")
 */
export function formatRelativeTime(timestamp: string | Date): string {
  try {
    return formatDistanceToNow(new Date(timestamp), { addSuffix: true });
  } catch {
    return 'Unknown';
  }
}

/**
 * Format timestamp to full date/time
 */
export function formatDateTime(timestamp: string | Date): string {
  try {
    return format(new Date(timestamp), 'MMM d, yyyy h:mm:ss a');
  } catch {
    return 'Invalid date';
  }
}

/**
 * Format timestamp to time only
 */
export function formatTime(timestamp: string | Date): string {
  try {
    return format(new Date(timestamp), 'h:mm:ss a');
  } catch {
    return 'Invalid time';
  }
}

/**
 * Format duration in milliseconds to human-readable format
 */
export function formatDurationMs(ms: number): string {
  if (ms < 1000) {
    return `${ms}ms`;
  }

  const duration = intervalToDuration({ start: 0, end: ms });
  return formatDuration(duration, {
    format: ['hours', 'minutes', 'seconds'],
    zero: false,
    delimiter: ', ',
  });
}

/**
 * Format bytes to human-readable size
 */
export function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 Bytes';

  const k = 1024;
  const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));

  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`;
}

/**
 * Format number with thousands separator
 */
export function formatNumber(num: number): string {
  return new Intl.NumberFormat('en-US').format(num);
}

/**
 * Format percentage
 */
export function formatPercentage(value: number, total: number): string {
  if (total === 0) return '0%';
  const percentage = (value / total) * 100;
  return `${percentage.toFixed(1)}%`;
}

/**
 * Format confidence score (0-1) to percentage
 */
export function formatConfidence(confidence: number): string {
  return `${(confidence * 100).toFixed(0)}%`;
}

/**
 * Format audio level (0-1) to decibels
 */
export function formatAudioLevel(level: number): string {
  if (level === 0) return '-∞ dB';
  const db = 20 * Math.log10(level);
  return `${db.toFixed(1)} dB`;
}

/**
 * Format uptime in seconds to human-readable format
 */
export function formatUptime(seconds: number): string {
  const duration = intervalToDuration({ start: 0, end: seconds * 1000 });

  const parts: string[] = [];
  if (duration.days && duration.days > 0) parts.push(`${duration.days}d`);
  if (duration.hours && duration.hours > 0) parts.push(`${duration.hours}h`);
  if (duration.minutes && duration.minutes > 0) parts.push(`${duration.minutes}m`);
  if (duration.seconds && duration.seconds > 0) parts.push(`${duration.seconds}s`);

  return parts.length > 0 ? parts.join(' ') : '0s';
}

/**
 * Truncate text with ellipsis
 */
export function truncate(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength)}...`;
}

/**
 * Format hotkey string for display
 */
export function formatHotkey(hotkey: string): string {
  return hotkey
    .split('+')
    .map((key) => key.trim())
    .map((key) => {
      // Capitalize first letter
      return key.charAt(0).toUpperCase() + key.slice(1);
    })
    .join(' + ');
}

/**
 * Validate URL format
 */
export function isValidUrl(url: string): boolean {
  try {
    new URL(url);
    return true;
  } catch {
    return false;
  }
}

/**
 * Validate email format
 */
export function isValidEmail(email: string): boolean {
  const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  return emailRegex.test(email);
}

/**
 * Generate random ID
 */
export function generateId(): string {
  return crypto.randomUUID();
}

/**
 * Sleep utility for async operations
 */
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Clamp value between min and max
 */
export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

/**
 * Map value from one range to another
 */
export function mapRange(
  value: number,
  inMin: number,
  inMax: number,
  outMin: number,
  outMax: number
): number {
  return ((value - inMin) * (outMax - outMin)) / (inMax - inMin) + outMin;
}
