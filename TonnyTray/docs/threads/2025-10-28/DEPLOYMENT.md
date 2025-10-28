# TonnyTray Deployment Guide

Comprehensive guide for deploying TonnyTray across different platforms and environments.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Platform-Specific Deployment](#platform-specific-deployment)
  - [Linux](#linux-deployment)
  - [macOS](#macos-deployment)
  - [Windows](#windows-deployment)
- [Backend Services](#backend-services)
- [Configuration](#configuration)
- [Monitoring](#monitoring)
- [Troubleshooting](#troubleshooting)

## Prerequisites

### System Requirements

**Minimum:**
- 2GB RAM
- 1GB free disk space
- Modern CPU (2 cores)

**Recommended:**
- 4GB RAM
- 2GB free disk space
- Modern CPU (4+ cores)
- SSD storage

### Required Software

- **WhisperLiveKit Server** (parent project)
- **Node.js** 18+ (for development)
- **Rust** 1.70+ (for building from source)
- **Docker** and **Docker Compose** (optional, for backend services)

## Quick Start

### Install from Release

#### Linux (DEB)
```bash
# Download latest release
wget https://github.com/yourusername/tonnytray/releases/latest/download/tonnytray_v0.1.0_amd64.deb

# Install
sudo dpkg -i tonnytray_v0.1.0_amd64.deb
sudo apt-get install -f

# Start
tonnytray
```

#### Linux (AppImage)
```bash
# Download
wget https://github.com/yourusername/tonnytray/releases/latest/download/tonnytray_v0.1.0_amd64.AppImage

# Make executable
chmod +x tonnytray_v0.1.0_amd64.AppImage

# Run
./tonnytray_v0.1.0_amd64.AppImage
```

#### macOS
```bash
# Download DMG
curl -LO https://github.com/yourusername/tonnytray/releases/latest/download/TonnyTray_v0.1.0_x64.dmg

# Mount and install
open TonnyTray_v0.1.0_x64.dmg
# Drag TonnyTray.app to Applications folder
```

#### Windows
```powershell
# Download installer
Invoke-WebRequest -Uri "https://github.com/yourusername/tonnytray/releases/latest/download/TonnyTray_v0.1.0_x64-setup.exe" -OutFile "TonnyTray-setup.exe"

# Run installer
.\TonnyTray-setup.exe
```

## Platform-Specific Deployment

### Linux Deployment

#### Using Installation Script

```bash
# Clone repository
git clone https://github.com/yourusername/tonnytray.git
cd tonnytray

# Run installation script
./scripts/install.sh

# Or install from DEB package
./scripts/install.sh path/to/tonnytray.deb
```

#### Manual Installation

1. **Install System Dependencies**

   **Ubuntu/Debian:**
   ```bash
   sudo apt-get update
   sudo apt-get install -y \
       libwebkit2gtk-4.0-37 \
       libgtk-3-0 \
       libayatana-appindicator3-1 \
       libasound2 \
       libpulse0
   ```

   **Fedora/RHEL:**
   ```bash
   sudo dnf install -y \
       webkit2gtk3 \
       gtk3 \
       libappindicator-gtk3 \
       alsa-lib \
       pulseaudio-libs
   ```

   **Arch Linux:**
   ```bash
   sudo pacman -S \
       webkit2gtk \
       gtk3 \
       libappindicator-gtk3 \
       alsa-lib \
       libpulse
   ```

2. **Install TonnyTray**
   ```bash
   sudo dpkg -i tonnytray_*.deb
   sudo apt-get install -f
   ```

3. **Setup Configuration**
   ```bash
   mkdir -p ~/.config/tonnytray
   cp config.example.json ~/.config/tonnytray/config.json
   nano ~/.config/tonnytray/config.json
   ```

4. **Enable Auto-start**
   ```bash
   # Copy systemd service
   mkdir -p ~/.config/systemd/user
   cp systemd/tonnytray.service ~/.config/systemd/user/

   # Enable and start
   systemctl --user daemon-reload
   systemctl --user enable tonnytray.service
   systemctl --user start tonnytray.service
   ```

5. **Verify Installation**
   ```bash
   systemctl --user status tonnytray
   ```

#### Desktop Environment Integration

**GNOME:**
```bash
# Install GNOME Shell extension for system tray
sudo apt-get install gnome-shell-extension-appindicator

# Enable extension
gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
```

**KDE Plasma:**
- Right-click on system tray
- Configure System Tray
- Add "Status Notifier Items"

**XFCE:**
- Panel → Add New Items → Notification Area

### macOS Deployment

#### Installation

1. **Download DMG from releases**

2. **Install Application**
   ```bash
   # Mount DMG
   hdiutil attach TonnyTray_v0.1.0_x64.dmg

   # Copy to Applications
   cp -R /Volumes/TonnyTray/TonnyTray.app /Applications/

   # Unmount
   hdiutil detach /Volumes/TonnyTray
   ```

3. **First Run**
   - Open System Preferences → Security & Privacy
   - Allow TonnyTray to run (if blocked)
   - Grant microphone permissions

4. **Auto-start Configuration**
   ```bash
   # Add to Login Items via System Preferences
   # Or use launchd:
   mkdir -p ~/Library/LaunchAgents
   ```

   Create `~/Library/LaunchAgents/com.tonnytray.app.plist`:
   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
       <key>Label</key>
       <string>com.tonnytray.app</string>
       <key>ProgramArguments</key>
       <array>
           <string>/Applications/TonnyTray.app/Contents/MacOS/tonnytray</string>
       </array>
       <key>RunAtLoad</key>
       <true/>
       <key>KeepAlive</key>
       <true/>
   </dict>
   </plist>
   ```

   ```bash
   launchctl load ~/Library/LaunchAgents/com.tonnytray.app.plist
   ```

### Windows Deployment

#### Installation

1. **Run Installer**
   - Download `TonnyTray_v0.1.0_x64-setup.exe`
   - Run as Administrator
   - Follow installation wizard

2. **Or use MSI Package**
   ```powershell
   msiexec /i TonnyTray_v0.1.0_x64.msi /qn
   ```

3. **Configuration**
   - Configuration file location: `%APPDATA%\tonnytray\config.json`
   - Data directory: `%LOCALAPPDATA%\tonnytray`

4. **Auto-start**
   - Installer adds to Startup folder automatically
   - Manual: Win+R → `shell:startup` → Create shortcut

#### Registry Settings (Advanced)

```powershell
# Add to Run key
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "TonnyTray" /t REG_SZ /d "C:\Program Files\TonnyTray\tonnytray.exe" /f
```

## Backend Services

### Using Docker Compose

#### Development Environment

```bash
# Start all services
docker compose -f docker-compose.dev.yml up -d

# View logs
docker compose -f docker-compose.dev.yml logs -f

# Stop services
docker compose -f docker-compose.dev.yml down
```

#### Production Environment

```bash
# Setup environment
cp .env.example .env
nano .env  # Configure your settings

# Validate configuration
./scripts/validate-env.sh

# Start services
docker compose -f docker-compose.prod.yml up -d

# View status
docker compose -f docker-compose.prod.yml ps

# View logs
docker compose -f docker-compose.prod.yml logs -f whisperlivekit
```

### Individual Services

#### PostgreSQL

```bash
# Using Docker
docker run -d \
  --name tonnytray-postgres \
  -e POSTGRES_USER=tonnytray \
  -e POSTGRES_PASSWORD=your_password \
  -e POSTGRES_DB=tonnytray \
  -p 5432:5432 \
  -v postgres_data:/var/lib/postgresql/data \
  postgres:16-alpine

# Native installation (Ubuntu)
sudo apt-get install postgresql-16
sudo -u postgres createuser tonnytray
sudo -u postgres createdb -O tonnytray tonnytray
```

#### Redis

```bash
# Using Docker
docker run -d \
  --name tonnytray-redis \
  -p 6379:6379 \
  -v redis_data:/data \
  redis:7-alpine \
  redis-server --appendonly yes --requirepass your_password

# Native installation (Ubuntu)
sudo apt-get install redis-server
sudo systemctl enable redis-server
sudo systemctl start redis-server
```

#### WhisperLiveKit Server

```bash
# From parent directory
cd ../
python -m whisperlivekit.server \
  --model base \
  --language en \
  --host 0.0.0.0 \
  --port 8888
```

### Systemd Service for Backend

```bash
# Copy service file
sudo cp systemd/tonnytray-backend.service /etc/systemd/system/

# Edit WorkingDirectory in service file
sudo nano /etc/systemd/system/tonnytray-backend.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable tonnytray-backend.service
sudo systemctl start tonnytray-backend.service
```

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and configure:

```bash
# Required
WHISPER_MODEL=base
WHISPER_PORT=8888
VITE_WS_URL=ws://localhost:8888/asr
VITE_N8N_WEBHOOK_URL=https://your-n8n.com/webhook/ask-tonny

# Optional
ELEVENLABS_API_KEY=your_key_here
ELEVENLABS_VOICE_ID=your_voice_id
DATABASE_TYPE=sqlite  # or postgresql
```

### Application Configuration

Edit `~/.config/tonnytray/config.json`:

```json
{
  "server": {
    "url": "ws://localhost:8888/asr",
    "model": "base",
    "language": "en",
    "auto_start": true
  },
  "n8n": {
    "webhook_url": "https://n8n.example.com/webhook/ask-tonny"
  },
  "elevenlabs": {
    "api_key": "sk_...",
    "voice_id": "21m00Tcm4TlvDq8ikWAM",
    "enabled": false
  },
  "audio": {
    "device": "default",
    "sample_rate": 16000
  },
  "ui": {
    "theme": "auto",
    "minimize_to_tray": true,
    "start_minimized": true
  }
}
```

### Database Setup

#### SQLite (Default)

```bash
# Initialize database
./scripts/migrate.sh

# Backup
./scripts/backup.sh backup

# Restore
./scripts/backup.sh restore backups/tonnytray_sqlite_20241016.db.gz
```

#### PostgreSQL

```bash
# Set environment
export DATABASE_TYPE=postgresql
export DATABASE_URL=postgresql://user:pass@localhost:5432/tonnytray

# Run migrations
./scripts/migrate.sh

# Create backup
./scripts/backup.sh backup

# Schedule automatic backups
crontab -e
# Add: 0 2 * * * /path/to/tonnytray/scripts/backup.sh backup
```

## Monitoring

### Health Checks

```bash
# Check TonnyTray status
systemctl --user status tonnytray

# Check backend services
docker compose -f docker-compose.prod.yml ps

# Check logs
journalctl --user -u tonnytray -f
docker compose -f docker-compose.prod.yml logs -f
```

### Metrics (Prometheus + Grafana)

```bash
# Start monitoring stack
docker compose -f docker-compose.prod.yml up -d prometheus grafana

# Access Grafana
open http://localhost:3000
# Default: admin / (check GRAFANA_PASSWORD in .env)

# Access Prometheus
open http://localhost:9090
```

### Log Management

**Systemd Logs:**
```bash
# View logs
journalctl --user -u tonnytray -f

# Export logs
journalctl --user -u tonnytray --since "1 hour ago" > tonnytray.log

# Rotate logs
sudo journalctl --vacuum-time=7d
```

**Application Logs:**
```bash
# Location: ~/.local/share/tonnytray/logs/
tail -f ~/.local/share/tonnytray/logs/tonnytray.log
```

## Troubleshooting

### Common Issues

#### Audio Not Working

**Linux:**
```bash
# Check audio devices
aplay -l
pactl list sources

# Test recording
arecord -d 3 test.wav

# Check permissions
groups | grep audio
sudo usermod -aG audio $USER
```

**macOS:**
- System Preferences → Security & Privacy → Microphone
- Grant permission to TonnyTray

**Windows:**
- Settings → Privacy → Microphone
- Enable for TonnyTray

#### Server Connection Failed

```bash
# Check WhisperLiveKit server
curl -v ws://localhost:8888/asr

# Check firewall
sudo ufw status
sudo ufw allow 8888/tcp

# Verify configuration
cat ~/.config/tonnytray/config.json
```

#### System Tray Not Showing

**Linux (GNOME):**
```bash
# Install AppIndicator extension
sudo apt-get install gnome-shell-extension-appindicator
gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
```

**Linux (Other DEs):**
- Ensure system tray component is enabled in your panel

### Logs and Debugging

```bash
# Enable debug logging
export RUST_LOG=debug
export RUST_BACKTRACE=1

# Run with debug output
tonnytray

# Check systemd logs
journalctl --user -u tonnytray --since today

# Validate environment
./scripts/validate-env.sh
```

### Performance Optimization

#### CPU Usage
```bash
# Lower Whisper model size
# Edit config.json: "model": "tiny" or "base"

# Limit CPU
systemctl --user edit tonnytray
# Add: CPUQuota=50%
```

#### Memory Usage
```bash
# Set memory limit
systemctl --user edit tonnytray
# Add: MemoryMax=512M
```

## Backup and Recovery

### Backup Strategy

```bash
# Manual backup
./scripts/backup.sh backup

# Automated daily backups
crontab -e
# Add: 0 2 * * * /path/to/tonnytray/scripts/backup.sh backup
# Add: 0 3 * * 0 /path/to/tonnytray/scripts/backup.sh cleanup
```

### Restore from Backup

```bash
# List backups
./scripts/backup.sh list

# Restore
./scripts/backup.sh restore backups/tonnytray_sqlite_20241016_120000.db.gz
```

### Migration Between Machines

```bash
# On source machine
tar czf tonnytray-backup.tar.gz \
  ~/.config/tonnytray \
  ~/.local/share/tonnytray

# Transfer to new machine
scp tonnytray-backup.tar.gz user@newmachine:~

# On new machine
tar xzf tonnytray-backup.tar.gz -C ~/
./scripts/install.sh
```

## Uninstallation

```bash
# Using uninstall script
./scripts/uninstall.sh

# Or manual
systemctl --user stop tonnytray
systemctl --user disable tonnytray
rm -f ~/.local/bin/tonnytray
rm -rf ~/.config/tonnytray
rm -rf ~/.local/share/tonnytray
```

## Support

- GitHub Issues: https://github.com/yourusername/tonnytray/issues
- Documentation: https://github.com/yourusername/tonnytray/wiki
- Discussions: https://github.com/yourusername/tonnytray/discussions
