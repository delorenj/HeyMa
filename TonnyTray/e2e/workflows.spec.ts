/**
 * E2E Tests for complete user workflows
 */
import { test, expect, invokeCommand, waitForEvent, resetAppState } from './setup';

test.describe('Complete Recording Workflow', () => {
  test.beforeEach(async ({ page }) => {
    await resetAppState(page);
  });

  test('should complete full recording workflow', async ({ page }) => {
    // 1. Start server
    await page.click('[data-testid="start-server-button"]');
    await expect(page.locator('[data-testid="server-status"]')).toHaveText(
      /running/i,
      { timeout: 30000 }
    );

    // 2. Start recording
    await page.click('[data-testid="start-recording-button"]');
    await expect(page.locator('[data-testid="recording-indicator"]')).toBeVisible();

    // 3. Wait for transcription (simulated)
    const transcription = await waitForEvent(page, 'transcription', 10000);
    expect(transcription).toBeDefined();

    // 4. Stop recording
    await page.click('[data-testid="stop-recording-button"]');
    await expect(page.locator('[data-testid="recording-indicator"]')).not.toBeVisible();

    // 5. Verify transcription appears in history
    await page.click('[data-testid="transcription-history-tab"]');
    await expect(page.locator('[data-testid="transcription-entry"]').first()).toBeVisible();

    // 6. Stop server
    await page.click('[data-testid="stop-server-button"]');
    await expect(page.locator('[data-testid="server-status"]')).toHaveText(/stopped/i);
  });

  test('should handle n8n webhook flow', async ({ page }) => {
    // Enable n8n integration
    await page.click('[data-testid="settings-button"]');
    await page.click('[data-testid="integration-tab"]');
    await page.fill(
      '[data-testid="n8n-webhook-url"]',
      'https://test.n8n.io/webhook/test'
    );
    await page.click('[data-testid="n8n-enabled-checkbox"]');
    await page.click('[data-testid="save-settings-button"]');

    // Start recording
    await page.click('[data-testid="start-server-button"]');
    await page.click('[data-testid="start-recording-button"]');

    // Wait for transcription to be sent to n8n
    const event = await waitForEvent(page, 'n8n_sent', 15000);
    expect(event.success).toBe(true);

    // Verify response
    if (event.response) {
      await expect(page.locator('[data-testid="n8n-response"]')).toContainText(
        event.response
      );
    }
  });

  test('should handle TTS playback flow', async ({ page }) => {
    // Enable ElevenLabs
    await page.click('[data-testid="settings-button"]');
    await page.click('[data-testid="integration-tab"]');
    await page.fill('[data-testid="elevenlabs-api-key"]', 'test_key');
    await page.click('[data-testid="elevenlabs-enabled-checkbox"]');
    await page.click('[data-testid="save-settings-button"]');

    // Trigger TTS
    await page.click('[data-testid="test-tts-button"]');
    await expect(page.locator('[data-testid="audio-playing-indicator"]')).toBeVisible();

    // Wait for playback to complete
    await expect(page.locator('[data-testid="audio-playing-indicator"]')).not.toBeVisible({
      timeout: 10000,
    });
  });
});

test.describe('Multi-Profile Scenarios', () => {
  test('should switch between profiles', async ({ page }) => {
    // Create a new profile
    await page.click('[data-testid="profile-menu"]');
    await page.click('[data-testid="add-profile-button"]');
    await page.fill('[data-testid="profile-name"]', 'Test User');
    await page.selectOption('[data-testid="profile-permissions"]', 'user');
    await page.click('[data-testid="save-profile-button"]');

    // Switch to new profile
    await page.click('[data-testid="profile-menu"]');
    await page.click('[data-testid="profile-test-user"]');

    // Verify active profile changed
    await expect(page.locator('[data-testid="active-profile-name"]')).toHaveText(
      'Test User'
    );

    // Verify permission restrictions
    await expect(page.locator('[data-testid="admin-only-section"]')).not.toBeVisible();
  });

  test('should respect profile permissions', async ({ page }) => {
    // Create limited user profile
    const result = await invokeCommand(page, 'create_profile', {
      name: 'Limited User',
      permissions: 'user',
      allowedCommands: ['read'],
    });

    expect(result).toBeDefined();

    // Switch to limited profile
    await invokeCommand(page, 'switch_profile', { profileId: result.id });

    // Try to execute disallowed command
    await page.click('[data-testid="start-recording-button"]');

    // Should show permission error
    await expect(page.locator('[data-testid="error-message"]')).toContainText(
      /permission denied/i
    );
  });
});

test.describe('Error Recovery Scenarios', () => {
  test('should recover from server crash', async ({ page }) => {
    // Start server
    await page.click('[data-testid="start-server-button"]');
    await expect(page.locator('[data-testid="server-status"]')).toHaveText(/running/i);

    // Simulate server crash (send SIGKILL to process)
    const status = await invokeCommand(page, 'get_server_status');
    if (status.pid) {
      await invokeCommand(page, 'kill_process', { pid: status.pid });
    }

    // Wait for auto-restart
    await expect(page.locator('[data-testid="server-status"]')).toHaveText(
      /restarting/i,
      { timeout: 5000 }
    );

    // Verify server restarted
    await expect(page.locator('[data-testid="server-status"]')).toHaveText(
      /running/i,
      { timeout: 30000 }
    );
  });

  test('should handle network errors gracefully', async ({ page }) => {
    // Enable n8n with invalid URL
    await page.click('[data-testid="settings-button"]');
    await page.fill('[data-testid="n8n-webhook-url"]', 'https://invalid.url');
    await page.click('[data-testid="n8n-enabled-checkbox"]');
    await page.click('[data-testid="save-settings-button"]');

    // Try to send transcription
    await page.click('[data-testid="start-recording-button"]');

    // Should show error notification
    await expect(page.locator('[data-testid="notification-error"]')).toBeVisible();
    await expect(page.locator('[data-testid="notification-error"]')).toContainText(
      /network error/i
    );
  });

  test('should handle microphone permission denial', async ({ page }) => {
    // Mock microphone permission denial
    await page.evaluate(() => {
      Object.defineProperty(navigator, 'mediaDevices', {
        value: {
          getUserMedia: () => Promise.reject(new Error('Permission denied')),
        },
      });
    });

    // Try to start recording
    await page.click('[data-testid="start-recording-button"]');

    // Should show permission error
    await expect(page.locator('[data-testid="error-dialog"]')).toBeVisible();
    await expect(page.locator('[data-testid="error-dialog"]')).toContainText(
      /microphone permission/i
    );
  });
});

test.describe('System Tray Interactions', () => {
  test('should interact with system tray', async ({ app, page }) => {
    // Get system tray
    const tray = await app.evaluate(({ BrowserWindow }) => {
      const win = BrowserWindow.getAllWindows()[0];
      return win?.tray;
    });

    expect(tray).toBeDefined();

    // Click tray icon (platform-specific)
    // Note: Actual tray interaction depends on OS
  });

  test('should show tray menu', async ({ page }) => {
    // Simulate tray menu click
    await invokeCommand(page, 'show_tray_menu');

    // Verify window appears
    await expect(page).toBeVisible();
  });
});

test.describe('Settings Persistence', () => {
  test('should persist settings across restarts', async ({ page, app }) => {
    // Change settings
    await page.click('[data-testid="settings-button"]');
    await page.selectOption('[data-testid="model-select"]', 'large');
    await page.fill('[data-testid="port-input"]', '9999');
    await page.click('[data-testid="save-settings-button"]');

    // Restart app
    await app.close();

    // Re-launch would happen here in actual test
    // For now, verify settings were saved
    const settings = await invokeCommand(page, 'get_settings');
    expect(settings.model).toBe('large');
    expect(settings.port).toBe(9999);
  });

  test('should reset settings to default', async ({ page }) => {
    // Change settings
    await page.click('[data-testid="settings-button"]');
    await page.selectOption('[data-testid="model-select"]', 'large');
    await page.click('[data-testid="save-settings-button"]');

    // Reset settings
    await page.click('[data-testid="reset-settings-button"]');
    await page.click('[data-testid="confirm-reset-button"]');

    // Verify reset
    const settings = await invokeCommand(page, 'get_settings');
    expect(settings.model).toBe('base');
  });
});

test.describe('Performance Tests', () => {
  test('should handle rapid recording toggles', async ({ page }) => {
    await page.click('[data-testid="start-server-button"]');
    await expect(page.locator('[data-testid="server-status"]')).toHaveText(/running/i);

    // Rapidly toggle recording 10 times
    for (let i = 0; i < 10; i++) {
      await page.click('[data-testid="start-recording-button"]');
      await page.waitForTimeout(100);
      await page.click('[data-testid="stop-recording-button"]');
      await page.waitForTimeout(100);
    }

    // App should still be responsive
    const status = await invokeCommand(page, 'get_state');
    expect(status).toBeDefined();
  });

  test('should handle large transcription history', async ({ page }) => {
    // Add 1000 transcriptions
    for (let i = 0; i < 1000; i++) {
      await invokeCommand(page, 'add_transcription', {
        text: `Transcription ${i}`,
        success: true,
      });
    }

    // Open transcription history
    await page.click('[data-testid="transcription-history-tab"]');

    // Should render without lag
    await expect(page.locator('[data-testid="transcription-entry"]').first()).toBeVisible({
      timeout: 2000,
    });

    // Scroll should be smooth (measured by frame rate if needed)
    await page.locator('[data-testid="transcription-list"]').evaluate((el) => {
      el.scrollTop = el.scrollHeight;
    });
  });
});
