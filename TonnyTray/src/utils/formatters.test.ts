/**
 * Tests for formatter utility functions
 */

import { describe, it, expect } from 'vitest';
import {
  formatBytes,
  formatNumber,
  formatPercentage,
  formatConfidence,
  formatUptime,
  truncate,
  formatHotkey,
  isValidUrl,
  isValidEmail,
  clamp,
  mapRange,
} from './formatters';

describe('formatters', () => {
  describe('formatBytes', () => {
    it('should format bytes correctly', () => {
      expect(formatBytes(0)).toBe('0 Bytes');
      expect(formatBytes(1024)).toBe('1 KB');
      expect(formatBytes(1024 * 1024)).toBe('1 MB');
      expect(formatBytes(1536)).toBe('1.5 KB');
    });
  });

  describe('formatNumber', () => {
    it('should format numbers with thousands separator', () => {
      expect(formatNumber(1000)).toBe('1,000');
      expect(formatNumber(1000000)).toBe('1,000,000');
      expect(formatNumber(123456)).toBe('123,456');
    });
  });

  describe('formatPercentage', () => {
    it('should format percentages correctly', () => {
      expect(formatPercentage(50, 100)).toBe('50.0%');
      expect(formatPercentage(33, 100)).toBe('33.0%');
      expect(formatPercentage(0, 100)).toBe('0.0%');
      expect(formatPercentage(100, 100)).toBe('100.0%');
    });

    it('should handle zero total', () => {
      expect(formatPercentage(10, 0)).toBe('0%');
    });
  });

  describe('formatConfidence', () => {
    it('should format confidence scores', () => {
      expect(formatConfidence(0.95)).toBe('95%');
      expect(formatConfidence(0.5)).toBe('50%');
      expect(formatConfidence(1.0)).toBe('100%');
    });
  });

  describe('formatUptime', () => {
    it('should format uptime correctly', () => {
      expect(formatUptime(0)).toBe('0s');
      expect(formatUptime(30)).toBe('30s');
      expect(formatUptime(60)).toBe('1m');
      expect(formatUptime(3600)).toBe('1h');
      expect(formatUptime(86400)).toBe('1d');
      expect(formatUptime(90061)).toBe('1d 1h 1m 1s');
    });
  });

  describe('truncate', () => {
    it('should truncate long text', () => {
      expect(truncate('Hello World', 5)).toBe('Hello...');
      expect(truncate('Hi', 5)).toBe('Hi');
      expect(truncate('Exactly 10', 10)).toBe('Exactly 10');
    });
  });

  describe('formatHotkey', () => {
    it('should format hotkey strings', () => {
      expect(formatHotkey('ctrl+shift+v')).toBe('Ctrl + Shift + V');
      expect(formatHotkey('alt+f4')).toBe('Alt + F4');
    });
  });

  describe('isValidUrl', () => {
    it('should validate URLs', () => {
      expect(isValidUrl('https://example.com')).toBe(true);
      expect(isValidUrl('http://localhost:8080')).toBe(true);
      expect(isValidUrl('not a url')).toBe(false);
      expect(isValidUrl('ftp://files.com')).toBe(true);
    });
  });

  describe('isValidEmail', () => {
    it('should validate email addresses', () => {
      expect(isValidEmail('user@example.com')).toBe(true);
      expect(isValidEmail('test.user@domain.co.uk')).toBe(true);
      expect(isValidEmail('invalid')).toBe(false);
      expect(isValidEmail('@example.com')).toBe(false);
      expect(isValidEmail('user@')).toBe(false);
    });
  });

  describe('clamp', () => {
    it('should clamp values between min and max', () => {
      expect(clamp(5, 0, 10)).toBe(5);
      expect(clamp(-5, 0, 10)).toBe(0);
      expect(clamp(15, 0, 10)).toBe(10);
    });
  });

  describe('mapRange', () => {
    it('should map values from one range to another', () => {
      expect(mapRange(5, 0, 10, 0, 100)).toBe(50);
      expect(mapRange(0, 0, 10, 0, 100)).toBe(0);
      expect(mapRange(10, 0, 10, 0, 100)).toBe(100);
    });
  });
});
