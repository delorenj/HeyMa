# Contributing to TonnyTray

Thank you for considering contributing to TonnyTray! This document provides guidelines and instructions for contributing.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Development Workflow](#development-workflow)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Pull Request Process](#pull-request-process)
- [Release Process](#release-process)

## Code of Conduct

### Our Standards

- Be respectful and inclusive
- Welcome newcomers and help them learn
- Focus on what is best for the community
- Show empathy towards other community members

### Unacceptable Behavior

- Harassment, discrimination, or offensive comments
- Trolling or insulting/derogatory comments
- Publishing others' private information
- Other conduct which could reasonably be considered inappropriate

## Getting Started

### Prerequisites

- **Node.js** 18+ and npm
- **Rust** 1.70+ with cargo
- **Git**
- Platform-specific dependencies (see [DEPLOYMENT.md](DEPLOYMENT.md))

### First Time Setup

1. **Fork the repository**
   ```bash
   # Click "Fork" on GitHub, then clone your fork
   git clone https://github.com/YOUR_USERNAME/tonnytray.git
   cd tonnytray
   ```

2. **Add upstream remote**
   ```bash
   git remote add upstream https://github.com/originalowner/tonnytray.git
   git fetch upstream
   ```

3. **Run setup script**
   ```bash
   ./scripts/setup-dev.sh
   ```

4. **Create a branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

## Development Setup

### Quick Setup

```bash
# Install dependencies
npm ci

# Setup environment
cp .env.example .env
nano .env

# Start development server
npm run tauri:dev
```

### With Docker (Backend Services)

```bash
# Start backend services
docker compose -f docker-compose.dev.yml up -d

# View logs
docker compose -f docker-compose.dev.yml logs -f
```

### Manual Backend Setup

```bash
# PostgreSQL
docker run -d --name tonnytray-postgres \
  -e POSTGRES_USER=tonnytray \
  -e POSTGRES_PASSWORD=dev_password \
  -p 5432:5432 \
  postgres:16-alpine

# Redis
docker run -d --name tonnytray-redis \
  -p 6379:6379 \
  redis:7-alpine

# WhisperLiveKit (from parent directory)
cd ../
python -m whisperlivekit.server --model base
```

## Development Workflow

### Branch Naming Convention

- `feature/description` - New features
- `fix/description` - Bug fixes
- `docs/description` - Documentation updates
- `refactor/description` - Code refactoring
- `test/description` - Test additions/fixes
- `chore/description` - Build/tooling updates

### Commit Message Format

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Adding or updating tests
- `chore`: Build process or tooling changes
- `perf`: Performance improvements

**Examples:**
```bash
git commit -m "feat(audio): add voice activation detection"
git commit -m "fix(ui): resolve system tray icon not showing on GNOME"
git commit -m "docs: update installation instructions for Arch Linux"
```

### Development Commands

```bash
# Frontend development
npm run dev              # Vite dev server
npm run build           # Build frontend
npm run lint            # Lint TypeScript
npm run type-check      # TypeScript type checking
npm run test            # Run tests
npm run test:ui         # Run tests with UI

# Tauri development
npm run tauri:dev       # Run Tauri in dev mode
npm run tauri:build     # Build Tauri application

# Rust development (in src-tauri/)
cargo check             # Check compilation
cargo build            # Build debug binary
cargo build --release  # Build release binary
cargo test             # Run Rust tests
cargo fmt              # Format code
cargo clippy           # Lint code

# Watching for changes
cargo watch -x check   # Watch and check
cargo watch -x test    # Watch and test
```

### Hot Reload

- Frontend changes: Automatically reload with Vite
- Rust changes: Require restart of `npm run tauri:dev`

## Coding Standards

### TypeScript/React

**Style Guide:**
- Use functional components with hooks
- Prefer `const` over `let`
- Use TypeScript strict mode
- Use arrow functions for callbacks
- Use destructuring where appropriate

**Example:**
```typescript
import { useState, useEffect } from "react";

interface Props {
  title: string;
  onSubmit: (value: string) => void;
}

export const MyComponent: React.FC<Props> = ({ title, onSubmit }) => {
  const [value, setValue] = useState("");

  useEffect(() => {
    // Effect logic
  }, []);

  const handleSubmit = () => {
    onSubmit(value);
  };

  return (
    <div>
      <h1>{title}</h1>
      <button onClick={handleSubmit}>Submit</button>
    </div>
  );
};
```

**Formatting:**
```bash
# Check formatting
npx prettier --check "src/**/*.{ts,tsx}"

# Fix formatting
npx prettier --write "src/**/*.{ts,tsx}"
```

### Rust

**Style Guide:**
- Follow [Rust API Guidelines](https://rust-lang.github.io/api-guidelines/)
- Use `rustfmt` for formatting
- Use `clippy` for linting
- Write documentation for public APIs
- Use descriptive variable names

**Example:**
```rust
use serde::{Deserialize, Serialize};
use tauri::State;

/// Configuration for the audio processor
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AudioConfig {
    /// Sample rate in Hz
    pub sample_rate: u32,
    /// Number of channels
    pub channels: u16,
}

impl Default for AudioConfig {
    fn default() -> Self {
        Self {
            sample_rate: 16000,
            channels: 1,
        }
    }
}

/// Process audio data and return transcription
#[tauri::command]
pub async fn process_audio(
    data: Vec<f32>,
    config: State<'_, AudioConfig>,
) -> Result<String, String> {
    // Implementation
    Ok("transcription".to_string())
}
```

**Formatting:**
```bash
cd src-tauri

# Check formatting
cargo fmt -- --check

# Fix formatting
cargo fmt

# Run clippy
cargo clippy --all-targets --all-features -- -D warnings
```

### Documentation

- Use JSDoc for TypeScript functions
- Use Rustdoc for Rust functions
- Update README.md for user-facing changes
- Update DEPLOYMENT.md for deployment changes
- Add inline comments for complex logic

## Testing

### Frontend Testing

```bash
# Run tests
npm run test

# Run with UI
npm run test:ui

# Run with coverage
npm run test:coverage
```

**Example Test:**
```typescript
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MyComponent } from "./MyComponent";

describe("MyComponent", () => {
  it("renders title correctly", () => {
    render(<MyComponent title="Test" onSubmit={() => {}} />);
    expect(screen.getByText("Test")).toBeInTheDocument();
  });
});
```

### Rust Testing

```bash
cd src-tauri

# Run all tests
cargo test

# Run specific test
cargo test test_name

# Run with output
cargo test -- --nocapture
```

**Example Test:**
```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_audio_config_default() {
        let config = AudioConfig::default();
        assert_eq!(config.sample_rate, 16000);
        assert_eq!(config.channels, 1);
    }

    #[tokio::test]
    async fn test_process_audio() {
        let data = vec![0.0; 1024];
        let config = AudioConfig::default();
        let result = process_audio(data, config).await;
        assert!(result.is_ok());
    }
}
```

### Integration Testing

```bash
# Start test environment
docker compose -f docker-compose.dev.yml up -d

# Run integration tests
npm run test:integration
```

## Pull Request Process

### Before Submitting

1. **Update your branch**
   ```bash
   git fetch upstream
   git rebase upstream/main
   ```

2. **Run all checks**
   ```bash
   # Frontend
   npm run lint
   npm run type-check
   npm run test

   # Rust
   cd src-tauri
   cargo fmt -- --check
   cargo clippy --all-targets -- -D warnings
   cargo test
   ```

3. **Test manually**
   ```bash
   npm run tauri:build
   # Test the built application
   ```

4. **Update documentation**
   - Update README.md if needed
   - Update DEPLOYMENT.md for deployment changes
   - Add/update code comments

### Submitting

1. **Push to your fork**
   ```bash
   git push origin feature/your-feature-name
   ```

2. **Create Pull Request**
   - Go to GitHub and create a PR from your fork
   - Use a clear, descriptive title
   - Fill out the PR template
   - Link related issues

3. **PR Template**
   ```markdown
   ## Description
   Brief description of changes

   ## Type of Change
   - [ ] Bug fix
   - [ ] New feature
   - [ ] Breaking change
   - [ ] Documentation update

   ## Testing
   - [ ] Tests added/updated
   - [ ] Manual testing completed
   - [ ] All tests passing

   ## Checklist
   - [ ] Code follows style guidelines
   - [ ] Self-review completed
   - [ ] Documentation updated
   - [ ] No new warnings
   ```

### Review Process

- At least one maintainer approval required
- All CI checks must pass
- Address review comments
- Keep PR updated with main branch

### After Merge

1. **Delete your branch**
   ```bash
   git branch -d feature/your-feature-name
   git push origin --delete feature/your-feature-name
   ```

2. **Update your fork**
   ```bash
   git checkout main
   git pull upstream main
   git push origin main
   ```

## Release Process

### Version Numbering

We use [Semantic Versioning](https://semver.org/):
- MAJOR: Breaking changes
- MINOR: New features (backwards compatible)
- PATCH: Bug fixes

### Creating a Release

1. **Update version**
   ```bash
   # Update package.json
   npm version minor  # or major/patch

   # Update Cargo.toml
   cd src-tauri
   # Edit version in Cargo.toml
   ```

2. **Update CHANGELOG**
   ```bash
   # Add entry to CHANGELOG.md
   ```

3. **Create release commit**
   ```bash
   git add .
   git commit -m "chore: release v0.2.0"
   git tag -a v0.2.0 -m "Release v0.2.0"
   ```

4. **Push tag**
   ```bash
   git push origin main --tags
   ```

5. **GitHub Actions will automatically:**
   - Build for all platforms
   - Create GitHub release
   - Upload artifacts
   - Generate checksums

## Getting Help

- **Questions**: Open a [Discussion](https://github.com/yourusername/tonnytray/discussions)
- **Bug Reports**: Open an [Issue](https://github.com/yourusername/tonnytray/issues)
- **Feature Requests**: Open an [Issue](https://github.com/yourusername/tonnytray/issues)
- **Chat**: Join our [Discord](https://discord.gg/yourserver)

## Recognition

Contributors are recognized in:
- GitHub contributors page
- CHANGELOG.md for significant contributions
- README.md for major features

Thank you for contributing to TonnyTray!
