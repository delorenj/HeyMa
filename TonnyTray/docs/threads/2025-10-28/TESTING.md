# TonnyTray Testing Guide

Comprehensive testing documentation for the TonnyTray application.

## Table of Contents

- [Overview](#overview)
- [Test Structure](#test-structure)
- [Running Tests](#running-tests)
- [Test Coverage](#test-coverage)
- [Writing Tests](#writing-tests)
- [Continuous Integration](#continuous-integration)
- [Troubleshooting](#troubleshooting)

## Overview

TonnyTray uses a multi-layered testing strategy:

1. **Unit Tests**: Test individual functions and modules in isolation
2. **Integration Tests**: Test component interactions and Tauri commands
3. **E2E Tests**: Test complete user workflows
4. **Performance Tests**: Measure and validate performance metrics
5. **Security Tests**: Validate security boundaries and input validation

### Test Coverage Goals

- **Backend (Rust)**: >80% code coverage
- **Frontend (TypeScript/React)**: >70% code coverage
- **E2E Tests**: Cover all critical user workflows
- **Performance**: Latency benchmarks for key operations
- **Security**: Input validation and permission boundary tests

## Test Structure

```
TonnyTray/
├── src-tauri/
│   ├── src/
│   │   └── *.rs                 # Source files with inline tests
│   └── tests/
│       ├── state_tests.rs       # State management unit tests
│       ├── config_tests.rs      # Configuration unit tests
│       ├── integration_tests.rs # Tauri command integration tests
│       ├── process_tests.rs     # Process management tests
│       ├── audio_tests.rs       # Audio processing tests
│       ├── websocket_tests.rs   # WebSocket client tests
│       ├── elevenlabs_tests.rs  # ElevenLabs TTS tests
│       ├── keychain_tests.rs    # Keychain security tests
│       ├── database_tests.rs    # Database operation tests
│       └── performance_tests.rs # Performance benchmarks
├── src/
│   └── test/
│       ├── setup.ts             # Test configuration
│       ├── utils.tsx            # Test utilities
│       ├── hooks/               # Hook tests
│       │   ├── useTauriState.test.ts
│       │   ├── useAsync.test.ts
│       │   └── useDebounce.test.ts
│       └── components/          # Component tests
│           ├── RecordingControls.test.tsx
│           ├── Settings.test.tsx
│           ├── Dashboard.test.tsx
│           └── ProfileSelector.test.tsx
└── e2e/
    ├── setup.ts                 # E2E test setup
    ├── workflows.spec.ts        # Complete user workflows
    ├── integration.spec.ts      # External service integration
    ├── performance.spec.ts      # Performance tests
    ├── security.spec.ts         # Security validation
    └── fixtures/                # Test data and mocks
```

## Running Tests

### Rust Backend Tests

```bash
# Run all backend tests
cd src-tauri
cargo test

# Run specific test file
cargo test --test state_tests

# Run with output
cargo test -- --nocapture

# Run tests with coverage
cargo tarpaulin --out Html

# Run benchmarks
cargo bench
```

### Frontend Tests

```bash
# Run all frontend tests
npm test

# Run with watch mode
npm test -- --watch

# Run specific test file
npm test -- useTauriState.test.ts

# Run with coverage
npm run test:coverage

# Run with UI
npm run test:ui
```

### E2E Tests

```bash
# Build the app first
npm run tauri:build

# Run E2E tests
npx playwright test

# Run specific test suite
npx playwright test workflows

# Run with UI
npx playwright test --ui

# Run in headed mode (see browser)
npx playwright test --headed

# Debug mode
npx playwright test --debug
```

### All Tests

```bash
# Run complete test suite
npm run test:all
```

## Test Coverage

### Viewing Coverage Reports

#### Backend Coverage

```bash
cd src-tauri
cargo tarpaulin --out Html
# Open tarpaulin-report.html
```

#### Frontend Coverage

```bash
npm run test:coverage
# Open coverage/index.html
```

### Coverage Thresholds

Configured in `vitest.config.ts`:

```typescript
coverage: {
  statements: 70,
  branches: 65,
  functions: 70,
  lines: 70,
}
```

## Writing Tests

### Rust Unit Tests

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_feature() {
        // Arrange
        let input = create_test_input();

        // Act
        let result = function_to_test(input);

        // Assert
        assert_eq!(result, expected_output);
    }

    #[tokio::test]
    async fn test_async_feature() {
        // Test async code
        let result = async_function().await;
        assert!(result.is_ok());
    }
}
```

### Frontend Unit Tests

```typescript
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';

describe('MyHook', () => {
  beforeEach(() => {
    // Setup
    vi.clearAllMocks();
  });

  it('should do something', () => {
    const { result } = renderHook(() => useMyHook());

    act(() => {
      result.current.doSomething();
    });

    expect(result.current.state).toBe(expectedValue);
  });
});
```

### Component Tests

```typescript
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MyComponent } from './MyComponent';

describe('MyComponent', () => {
  it('should render correctly', () => {
    render(<MyComponent />);
    expect(screen.getByText('Expected Text')).toBeInTheDocument();
  });

  it('should handle user interaction', async () => {
    const user = userEvent.setup();
    const onClickMock = vi.fn();

    render(<MyComponent onClick={onClickMock} />);

    await user.click(screen.getByRole('button'));
    expect(onClickMock).toHaveBeenCalled();
  });
});
```

### E2E Tests

```typescript
import { test, expect, invokeCommand } from './setup';

test('complete workflow', async ({ page }) => {
  // Navigate
  await page.click('[data-testid="start-button"]');

  // Wait for result
  await expect(page.locator('[data-testid="result"]')).toBeVisible();

  // Invoke Tauri command
  const result = await invokeCommand(page, 'get_state');
  expect(result).toBeDefined();
});
```

## Test Categories

### Unit Tests

Focus on testing individual functions/components in isolation:

- Pure functions
- Component rendering
- Hook behavior
- State management
- Utility functions

### Integration Tests

Test interactions between components:

- Tauri IPC commands
- State synchronization
- Service integration
- Database operations
- WebSocket communication

### E2E Tests

Test complete user workflows:

- Recording flow
- Settings management
- Profile switching
- Error recovery
- System tray interactions

### Performance Tests

Measure and validate performance:

- IPC latency
- Audio processing latency
- Transcription throughput
- Memory usage
- CPU utilization

### Security Tests

Validate security boundaries:

- Input validation
- Permission boundaries
- Secret management
- XSS prevention
- SQL injection prevention

## Mocking

### Mocking Tauri API

```typescript
vi.mock('@tauri-apps/api', () => ({
  invoke: vi.fn(),
  listen: vi.fn(),
}));
```

### Mocking External Services

```rust
#[cfg(test)]
mod tests {
    use mockito::{mock, server_url};

    #[tokio::test]
    async fn test_api_call() {
        let _m = mock("GET", "/api/endpoint")
            .with_status(200)
            .with_body("test response")
            .create();

        let client = create_client(&server_url());
        let result = client.call().await;

        assert!(result.is_ok());
    }
}
```

## Continuous Integration

### GitHub Actions Workflow

Tests run automatically on:

- Pull requests
- Pushes to main
- Scheduled nightly builds

```yaml
name: Tests

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Setup Rust
        uses: actions-rs/toolchain@v1
      - name: Setup Node
        uses: actions/setup-node@v3
      - name: Install dependencies
        run: npm ci
      - name: Run backend tests
        run: cd src-tauri && cargo test
      - name: Run frontend tests
        run: npm run test:coverage
      - name: Upload coverage
        uses: codecov/codecov-action@v3
```

## Test Data

### Fixtures

Test fixtures are located in `e2e/fixtures/`:

- `mock_transcriptions.json`: Sample transcription data
- `test_settings.json`: Test configuration data
- `test_profiles.json`: User profile test data

### Using Fixtures

```typescript
import mockTranscriptions from './fixtures/mock_transcriptions.json';

test('should display transcriptions', async ({ page }) => {
  // Load fixture data
  await invokeCommand(page, 'load_transcriptions', {
    data: mockTranscriptions,
  });

  // Verify display
  await expect(page.locator('[data-testid="transcription-item"]')).toHaveCount(
    mockTranscriptions.length
  );
});
```

## Debugging Tests

### Frontend Tests

```bash
# Run specific test in debug mode
npm test -- --inspect-brk useTauriState.test.ts

# Open Chrome DevTools
chrome://inspect
```

### E2E Tests

```bash
# Debug specific test
npx playwright test --debug workflows.spec.ts

# Generate test trace
npx playwright test --trace on

# View trace
npx playwright show-trace trace.zip
```

### Backend Tests

```bash
# Run with debug output
RUST_LOG=debug cargo test -- --nocapture

# Run under debugger
rust-lldb target/debug/deps/tonnytray-*
```

## Performance Benchmarks

### Running Benchmarks

```bash
cd src-tauri
cargo bench
```

### Benchmark Results

Benchmarks are stored in `src-tauri/target/criterion/`.

View reports: `src-tauri/target/criterion/report/index.html`

### Performance Targets

- IPC call latency: <5ms
- Audio processing: <100ms
- Transcription: <2s for 10s audio
- UI render time: <16ms (60fps)
- Memory footprint: <200MB

## Test Best Practices

### DO

✅ Write descriptive test names
✅ Follow AAA pattern (Arrange, Act, Assert)
✅ Test edge cases and error conditions
✅ Mock external dependencies
✅ Keep tests isolated and independent
✅ Use data-testid attributes for E2E tests
✅ Clean up after tests
✅ Write tests before fixing bugs (TDD)

### DON'T

❌ Test implementation details
❌ Share state between tests
❌ Use sleep/setTimeout (use waitFor)
❌ Ignore test failures
❌ Write tests that depend on execution order
❌ Mock too much (test real behavior when possible)
❌ Skip cleanup in afterEach

## Troubleshooting

### Common Issues

#### Rust Tests Fail to Compile

```bash
# Clean and rebuild
cargo clean
cargo build --tests
```

#### Frontend Tests Timeout

```typescript
// Increase timeout for specific test
test('slow operation', async () => {
  // ...
}, { timeout: 30000 });
```

#### E2E Tests Can't Find Binary

```bash
# Ensure app is built
npm run tauri:build

# Check binary path
ls -la src-tauri/target/release/
```

#### Flaky Tests

- Use `waitFor` instead of fixed delays
- Increase timeouts for CI environment
- Check for race conditions
- Verify proper cleanup

### Getting Help

- Check test logs: `npm test -- --reporter=verbose`
- Enable debug mode: `DEBUG=* npm test`
- Review CI logs in GitHub Actions
- Check test coverage gaps: `npm run test:coverage`

## Resources

- [Vitest Documentation](https://vitest.dev/)
- [Testing Library](https://testing-library.com/)
- [Playwright Documentation](https://playwright.dev/)
- [Rust Testing Guide](https://doc.rust-lang.org/book/ch11-00-testing.html)
- [Tauri Testing Guide](https://tauri.app/v1/guides/testing/)

## Contributing

When contributing tests:

1. Ensure all tests pass locally
2. Add tests for new features
3. Update this documentation if needed
4. Maintain coverage thresholds
5. Follow naming conventions
6. Add meaningful descriptions

## License

Same as TonnyTray project license.
