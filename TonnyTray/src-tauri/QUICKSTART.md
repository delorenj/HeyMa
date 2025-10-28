# TonnyTray Backend - Quick Start Guide

## Installation

1. Install Rust:
```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source $HOME/.cargo/env
```

2. Install system dependencies (Linux):
```bash
sudo apt update
sudo apt install libwebkit2gtk-4.0-dev build-essential curl wget \
  libssl-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev \
  libasound2-dev
```

3. Install Tauri CLI:
```bash
cargo install tauri-cli
```

## Running

### Development Mode
```bash
cd /home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray
cargo tauri dev
```

### Run Tests
```bash
cargo test --manifest-path=src-tauri/Cargo.toml
```

### Build Release
```bash
cargo tauri build
```

Output: `src-tauri/target/release/bundle/`

## Configuration

Configuration file: `~/.config/tonnytray/config.json`

Copy example config:
```bash
cp config.example.json ~/.config/tonnytray/config.json
```

Edit the file to add:
- Your n8n webhook URL
- Your ElevenLabs API key
- Microphone device (optional)

## Verify Installation

```bash
# Check Rust version (need 1.75+)
rustc --version

# Check if dependencies compile
cargo check --manifest-path=src-tauri/Cargo.toml

# Run tests
cargo test --manifest-path=src-tauri/Cargo.toml
```

## Common Issues

### Build Fails
```bash
# Update Rust
rustup update

# Clean build
cargo clean --manifest-path=src-tauri/Cargo.toml
cargo build --manifest-path=src-tauri/Cargo.toml
```

### Missing Dependencies
```bash
# Debian/Ubuntu
sudo apt install pkg-config libssl-dev libgtk-3-dev libayatana-appindicator3-dev

# Check if webkit is installed
dpkg -l | grep webkit2gtk
```

### Audio Issues
```bash
# Install ALSA development files
sudo apt install libasound2-dev

# List audio devices
cargo run --manifest-path=src-tauri/Cargo.toml -- --list-devices
```

## Next Steps

1. Configure settings in `~/.config/tonnytray/config.json`
2. Start WhisperLiveKit server (if not auto-starting)
3. Test n8n connection
4. Test ElevenLabs API
5. Start recording!

## Documentation

- Architecture: `../ARCHITECTURE.md`
- Backend Details: `README.md`
- Full Summary: `../BACKEND_SUMMARY.md`
- PRD: `../../TRAY_APP_PRD.md`

## Support

Check logs:
```bash
tail -f ~/.config/tonnytray/logs/tonnytray.log
```

Enable debug logging:
```bash
RUST_LOG=debug cargo tauri dev
```
