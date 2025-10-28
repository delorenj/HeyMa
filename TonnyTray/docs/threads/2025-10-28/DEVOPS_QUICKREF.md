# TonnyTray DevOps Quick Reference

Essential commands and workflows for common DevOps tasks.

## Development

```bash
# Initial setup
./scripts/setup-dev.sh

# Start development
npm run tauri:dev

# Start backend services
docker compose -f docker-compose.dev.yml up -d

# Watch logs
docker compose -f docker-compose.dev.yml logs -f whisperlivekit

# Stop services
docker compose -f docker-compose.dev.yml down
```

## Testing

```bash
# Frontend tests
npm run test
npm run test:ui
npm run test:coverage

# Rust tests
cd src-tauri && cargo test

# Linting
npm run lint
cd src-tauri && cargo clippy

# Type checking
npm run type-check

# Format checking
npx prettier --check "src/**/*.{ts,tsx}"
cd src-tauri && cargo fmt -- --check
```

## Building

```bash
# Development build
npm run tauri:dev

# Release build (all platforms)
npm run tauri:build

# Platform-specific builds
npm run tauri build -- --bundles deb      # Linux DEB
npm run tauri build -- --bundles appimage # Linux AppImage
npm run tauri build -- --bundles dmg      # macOS DMG
npm run tauri build -- --bundles msi      # Windows MSI
```

## Docker Operations

```bash
# Build image
docker build -t tonnytray:dev .

# Development environment
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
docker compose -f docker-compose.dev.yml logs -f
docker compose -f docker-compose.dev.yml down

# Production environment
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f
docker compose -f docker-compose.prod.yml down

# Rebuild service
docker compose -f docker-compose.dev.yml up -d --build whisperlivekit

# Clean up
docker compose -f docker-compose.dev.yml down -v
docker system prune -a
```

## Database Operations

```bash
# Run migrations
./scripts/migrate.sh migrate

# Create new migration
./scripts/migrate.sh create migration_name

# Backup database
./scripts/backup.sh backup

# List backups
./scripts/backup.sh list

# Restore backup
./scripts/backup.sh restore backups/tonnytray_sqlite_*.db.gz

# Cleanup old backups
./scripts/backup.sh cleanup
```

## Environment Management

```bash
# Setup environment
cp .env.example .env
nano .env

# Validate environment
./scripts/validate-env.sh

# Load environment
set -a && source .env && set +a
```

## Installation

```bash
# Install from package
./scripts/install.sh path/to/tonnytray.deb

# Install latest release
./scripts/install.sh

# Uninstall
./scripts/uninstall.sh
```

## Systemd Management

```bash
# User service (TonnyTray app)
systemctl --user start tonnytray
systemctl --user stop tonnytray
systemctl --user restart tonnytray
systemctl --user status tonnytray
systemctl --user enable tonnytray
systemctl --user disable tonnytray

# View logs
journalctl --user -u tonnytray -f
journalctl --user -u tonnytray --since "1 hour ago"

# System service (Backend)
sudo systemctl start tonnytray-backend
sudo systemctl stop tonnytray-backend
sudo systemctl status tonnytray-backend
```

## Git Operations

```bash
# Create feature branch
git checkout -b feature/my-feature

# Commit with conventional format
git commit -m "feat(audio): add noise reduction"
git commit -m "fix(ui): resolve tray icon issue"
git commit -m "docs: update installation guide"

# Update from upstream
git fetch upstream
git rebase upstream/main

# Push feature branch
git push origin feature/my-feature
```

## Release Process

```bash
# Update versions
npm version minor  # or major/patch
cd src-tauri && nano Cargo.toml  # Update version

# Update changelog
nano CHANGELOG.md

# Create release commit and tag
git add .
git commit -m "chore: release v0.2.0"
git tag -a v0.2.0 -m "Release v0.2.0"
git push origin main --tags

# GitHub Actions will automatically build and release
```

## CI/CD

```bash
# Trigger CI manually
gh workflow run ci.yml

# Trigger release manually
gh workflow run release.yml -f version=v0.2.0

# View workflow runs
gh run list

# View workflow logs
gh run view <run-id>

# Download artifacts
gh run download <run-id>
```

## Monitoring

```bash
# Access Grafana
open http://localhost:3000

# Access Prometheus
open http://localhost:9090

# Access PgAdmin
open http://localhost:5050

# Access Redis Commander
open http://localhost:8081

# Access RabbitMQ Management
open http://localhost:15672
```

## Debugging

```bash
# Enable debug logging
export RUST_LOG=debug
export RUST_BACKTRACE=1
npm run tauri:dev

# View application logs
tail -f ~/.local/share/tonnytray/logs/tonnytray.log

# View systemd logs with debug
journalctl --user -u tonnytray -f -p debug

# Check process
ps aux | grep tonnytray

# Check ports
sudo lsof -i :8888
sudo netstat -tulpn | grep 8888
```

## Performance Profiling

```bash
# Rust profiling
cd src-tauri
cargo build --release
cargo flamegraph --bin tonnytray

# Build size analysis
cargo bloat --release

# Check dependencies
cargo tree
```

## Security

```bash
# Audit dependencies
cd src-tauri && cargo audit
npm audit

# Fix vulnerabilities
cargo audit fix
npm audit fix

# Update dependencies
cd src-tauri && cargo update
npm update
```

## Cleanup

```bash
# Clean build artifacts
cd src-tauri && cargo clean
rm -rf node_modules dist

# Clean Docker
docker compose -f docker-compose.dev.yml down -v
docker system prune -a -f

# Clean logs
rm -rf logs/*
sudo journalctl --vacuum-time=7d
```

## Troubleshooting

```bash
# Reset to clean state
./scripts/uninstall.sh
git clean -fdx
./scripts/setup-dev.sh

# Check system dependencies
# Ubuntu/Debian
dpkg -l | grep -E "libwebkit2gtk|libgtk-3|libappindicator"

# Verify Node.js and Rust
node --version  # Should be 18+
npm --version
rustc --version # Should be 1.70+
cargo --version

# Check Docker
docker --version
docker compose version

# Validate configuration
./scripts/validate-env.sh

# Test database connection
# PostgreSQL
psql postgresql://user:pass@localhost:5432/tonnytray

# SQLite
sqlite3 ~/.config/tonnytray/tonnytray.db ".tables"
```

## Common File Locations

```bash
# Configuration
~/.config/tonnytray/config.json
~/.config/tonnytray/.env

# Data
~/.local/share/tonnytray/
~/.local/share/tonnytray/logs/

# Database
~/.config/tonnytray/tonnytray.db  # SQLite

# Binary
~/.local/bin/tonnytray

# Systemd service
~/.config/systemd/user/tonnytray.service

# Desktop entry
~/.local/share/applications/tonnytray.desktop
```

## Environment Variables

```bash
# Required
export WHISPER_MODEL=base
export WHISPER_PORT=8888
export VITE_WS_URL=ws://localhost:8888/asr
export VITE_N8N_WEBHOOK_URL=https://your-n8n.com/webhook

# Optional
export ELEVENLABS_API_KEY=sk_...
export DATABASE_TYPE=sqlite
export RUST_LOG=info
export RUST_BACKTRACE=1
```

## URLs

```bash
# Local services
http://localhost:1420      # Vite dev server
ws://localhost:8888/asr    # WhisperLiveKit
http://localhost:3000      # Grafana
http://localhost:9090      # Prometheus
http://localhost:5050      # PgAdmin
http://localhost:8081      # Redis Commander
http://localhost:15672     # RabbitMQ Management

# Production (configure in .env)
https://your-domain.com
wss://your-domain.com/asr
```

## Quick Fixes

```bash
# Port already in use
sudo lsof -ti:8888 | xargs kill -9

# Permission denied on script
chmod +x scripts/*.sh

# Node modules issues
rm -rf node_modules package-lock.json
npm ci

# Rust compilation issues
cd src-tauri
cargo clean
cargo build

# Docker port conflicts
docker compose -f docker-compose.dev.yml down
docker ps -a | grep tonnytray | awk '{print $1}' | xargs docker rm -f

# Database locked
rm ~/.config/tonnytray/tonnytray.db-shm
rm ~/.config/tonnytray/tonnytray.db-wal
```

## Getting Help

```bash
# View documentation
cat DEPLOYMENT.md | less
cat CONTRIBUTING.md | less
cat BUILD.md | less

# Check GitHub issues
gh issue list

# Create new issue
gh issue create

# Join discussion
gh discussion list
```

---

**Pro Tip**: Add this directory to your PATH for easy access to scripts:
```bash
echo 'export PATH="$PATH:/home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray/scripts"' >> ~/.bashrc
source ~/.bashrc
```
