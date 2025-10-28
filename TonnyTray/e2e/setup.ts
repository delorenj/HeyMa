/**
 * E2E Test Setup for Playwright/Tauri
 */
import { test as base, expect } from '@playwright/test';
import { _electron as electron, ElectronApplication, Page } from 'playwright';
import path from 'path';
import fs from 'fs';

export interface TauriTestFixtures {
  app: ElectronApplication;
  page: Page;
}

/**
 * Extended test fixture for Tauri applications
 */
export const test = base.extend<TauriTestFixtures>({
  app: async ({}, use) => {
    // Find the Tauri binary
    const binaryPath = findTauriBinary();

    // Launch the app
    const app = await electron.launch({
      args: [binaryPath],
      env: {
        ...process.env,
        TAURI_SKIP_DEVTOOLS: '1',
        TEST_MODE: '1',
      },
    });

    await use(app);

    // Cleanup
    await app.close();
  },

  page: async ({ app }, use) => {
    // Wait for the main window
    const page = await app.firstWindow();

    // Set viewport
    await page.setViewportSize({ width: 1280, height: 720 });

    await use(page);
  },
});

export { expect };

/**
 * Find the Tauri binary based on platform and build mode
 */
function findTauriBinary(): string {
  const platform = process.platform;
  const buildMode = process.env.BUILD_MODE || 'debug';

  let binaryName: string;
  let binaryDir: string;

  if (platform === 'darwin') {
    binaryName = 'TonnyTray.app/Contents/MacOS/TonnyTray';
    binaryDir = path.join('src-tauri', 'target', buildMode, 'bundle', 'macos');
  } else if (platform === 'win32') {
    binaryName = 'tonnytray.exe';
    binaryDir = path.join('src-tauri', 'target', buildMode);
  } else {
    // Linux
    binaryName = 'tonnytray';
    binaryDir = path.join('src-tauri', 'target', buildMode);
  }

  const binaryPath = path.join(process.cwd(), binaryDir, binaryName);

  if (!fs.existsSync(binaryPath)) {
    throw new Error(
      `Tauri binary not found at ${binaryPath}. Please build the app first with 'npm run tauri:build'`
    );
  }

  return binaryPath;
}

/**
 * Helper to wait for IPC response
 */
export async function waitForIPC(
  page: Page,
  command: string,
  timeout = 5000
): Promise<any> {
  return page.evaluate(
    async ({ command, timeout }) => {
      const { invoke } = (window as any).__TAURI__.core;
      return Promise.race([
        invoke(command),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error('IPC timeout')), timeout)
        ),
      ]);
    },
    { command, timeout }
  );
}

/**
 * Helper to wait for event
 */
export async function waitForEvent(
  page: Page,
  eventName: string,
  timeout = 5000
): Promise<any> {
  return page.evaluate(
    async ({ eventName, timeout }) => {
      const { listen } = (window as any).__TAURI__.event;
      return Promise.race([
        new Promise((resolve) => {
          listen(eventName, (event: any) => resolve(event.payload));
        }),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error('Event timeout')), timeout)
        ),
      ]);
    },
    { eventName, timeout }
  );
}

/**
 * Helper to invoke Tauri command
 */
export async function invokeCommand<T>(
  page: Page,
  command: string,
  args?: Record<string, any>
): Promise<T> {
  return page.evaluate(
    async ({ command, args }) => {
      const { invoke } = (window as any).__TAURI__.core;
      return invoke(command, args);
    },
    { command, args }
  );
}

/**
 * Helper to reset app state
 */
export async function resetAppState(page: Page): Promise<void> {
  await invokeCommand(page, 'stop_server');
  await invokeCommand(page, 'stop_recording');
  await page.evaluate(() => {
    localStorage.clear();
  });
}

/**
 * Helper to take screenshot with timestamp
 */
export async function takeScreenshot(page: Page, name: string): Promise<void> {
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  const filename = `${name}-${timestamp}.png`;
  await page.screenshot({
    path: path.join('e2e', 'screenshots', filename),
    fullPage: true,
  });
}
