# Building TonnyTray from Source

Comprehensive guide for building TonnyTray on different platforms.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Platform-Specific Instructions](#platform-specific-instructions)
  - [Linux](#linux)
  - [macOS](#macos)
  - [Windows](#windows)
- [Build Configurations](#build-configurations)
- [Docker Builds](#docker-builds)
- [Troubleshooting](#troubleshooting)

## Prerequisites

### All Platforms

- **Node.js** 18+ with npm
- **Rust** 1.70+ (install from [rustup.rs](https://rustup.rs))
- **Git**

### Installation

**Node.js:**
```bash
# Using nvm (recommended)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.0/install.sh | bash
nvm install 20
nvm use 20

# Verify
node --version
npm --version
```

**Rust:**
```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source $HOME/.cargo/env

# Verify
rustc --version
cargo --version
```

## Quick Start

```bash
# Clone repository
git clone https://github.com/yourusername/tonnytray.git
cd tonnytray

# Run setup script
./scripts/setup-dev.sh

# Build
npm run tauri:build

# Output location:
# Linux: src-tauri/target/release/bundle/deb/ and src-tauri/target/release/bundle/appimage/
# macOS: src-tauri/target/release/bundle/dmg/ and src-tauri/target/release/bundle/macos/
# Windows: src-tauri/target/release/bundle/msi/ and src-tauri/target/release/bundle/nsis/
```

## Platform-Specific Instructions

### Linux

#### Ubuntu/Debian

1. **Install System Dependencies**
   ```bash
   sudo apt-get update
   sudo apt-get install -y \
       libwebkit2gtk-4.0-dev \
       build-essential \
       curl \
       wget \
       file \
       libssl-dev \
       libgtk-3-dev \
       libayatana-appindicator3-dev \
       librsvg2-dev \
       libasound2-dev \
       libpulse-dev \
       pkg-config
   ```

2. **Install Node.js and Rust** (see prerequisites)

3. **Clone and Build**
   ```bash
   git clone https://github.com/yourusername/tonnytray.git
   cd tonnytray
   npm ci
   npm run tauri:build
   ```

4. **Install the Built Package**
   ```bash
   # DEB package
   sudo dpkg -i src-tauri/target/release/bundle/deb/*.deb
   sudo apt-get install -f

   # Or use AppImage
   chmod +x src-tauri/target/release/bundle/appimage/*.AppImage
   ./src-tauri/target/release/bundle/appimage/*.AppImage
   ```

#### Fedora/RHEL

```bash
sudo dnf install -y \
    webkit2gtk3-devel \
    openssl-devel \
    curl \
    wget \
    file \
    gtk3-devel \
    libappindicator-gtk3-devel \
    librsvg2-devel \
    alsa-lib-devel \
    pulseaudio-libs-devel

# Then follow steps 2-4 from Ubuntu instructions
```

#### Arch Linux

```bash
sudo pacman -S --needed \
    webkit2gtk \
    base-devel \
    curl \
    wget \
    file \
    openssl \
    gtk3 \
    libappindicator-gtk3 \
    librsvg \
    alsa-lib \
    libpulse

# Then follow steps 2-4 from Ubuntu instructions
```

#### openSUSE

```bash
sudo zypper install -y \
    webkit2gtk3-devel \
    gcc \
    gcc-c++ \
    make \
    curl \
    wget \
    file \
    libopenssl-devel \
    gtk3-devel \
    libappindicator3-1 \
    librsvg-devel \
    alsa-devel \
    libpulse-devel

# Then follow steps 2-4 from Ubuntu instructions
```

### macOS

1. **Install Xcode Command Line Tools**
   ```bash
   xcode-select --install
   ```

2. **Install Homebrew** (if not installed)
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

3. **Install Node.js and Rust** (see prerequisites)

4. **Clone and Build**
   ```bash
   git clone https://github.com/yourusername/tonnytray.git
   cd tonnytray
   npm ci
   npm run tauri:build
   ```

5. **Build Outputs**
   - **DMG**: `src-tauri/target/release/bundle/dmg/TonnyTray_*.dmg`
   - **App Bundle**: `src-tauri/target/release/bundle/macos/TonnyTray.app`

6. **Install**
   ```bash
   # Open DMG
   open src-tauri/target/release/bundle/dmg/TonnyTray_*.dmg

   # Or copy app directly
   cp -R src-tauri/target/release/bundle/macos/TonnyTray.app /Applications/
   ```

#### Code Signing (Optional)

```bash
# Create signing certificate in Keychain Access
# Or use existing Apple Developer certificate

# Sign the app
codesign --force --deep --sign "Developer ID Application: Your Name" \
  src-tauri/target/release/bundle/macos/TonnyTray.app

# Verify signature
codesign --verify --verbose src-tauri/target/release/bundle/macos/TonnyTray.app
```

### Windows

#### Prerequisites

1. **Install Visual Studio Build Tools**
   - Download from: https://visualstudio.microsoft.com/downloads/
   - Install "Desktop development with C++"
   - Or install Visual Studio 2019/2022 with C++ workload

2. **Install Node.js**
   - Download from: https://nodejs.org/
   - Install LTS version

3. **Install Rust**
   ```powershell
   # Download and run rustup-init.exe from https://rustup.rs/
   # Or use:
   winget install Rustlang.Rustup
   ```

4. **Install WebView2 Runtime** (usually pre-installed on Windows 10/11)
   - Download from: https://developer.microsoft.com/en-us/microsoft-edge/webview2/

#### Building

```powershell
# Clone repository
git clone https://github.com/yourusername/tonnytray.git
cd tonnytray

# Install dependencies
npm ci

# Build
npm run tauri:build
```

#### Build Outputs

- **MSI Installer**: `src-tauri\target\release\bundle\msi\TonnyTray_*.msi`
- **NSIS Installer**: `src-tauri\target\release\bundle\nsis\TonnyTray_*-setup.exe`
- **Portable**: `src-tauri\target\release\tonnytray.exe`

#### Code Signing (Optional)

```powershell
# Install signtool (comes with Windows SDK)
# Sign the installer
signtool sign /f your-certificate.pfx /p password /t http://timestamp.digicert.com src-tauri\target\release\bundle\msi\TonnyTray_*.msi
```

## Build Configurations

### Debug Build

```bash
# Faster compilation, includes debug symbols
npm run tauri dev

# Or build without running
cd src-tauri
cargo build
```

**Output**: `src-tauri/target/debug/tonnytray`

### Release Build

```bash
# Optimized build (slower compile, smaller binary)
npm run tauri:build

# Or
cd src-tauri
cargo build --release
```

**Output**: `src-tauri/target/release/tonnytray`

### Development Build with Hot Reload

```bash
# Frontend changes auto-reload
npm run tauri:dev
```

### Custom Build Options

**Optimize for Size:**
```bash
# Already configured in Cargo.toml
# profile.release: opt-level = "z", lto = true, strip = true
cd src-tauri
cargo build --release
```

**Optimize for Performance:**
```toml
# Edit src-tauri/Cargo.toml
[profile.release]
opt-level = 3
lto = "thin"
```

**Build Specific Targets:**
```bash
# Linux only DEB
npm run tauri build -- --bundles deb

# Linux only AppImage
npm run tauri build -- --bundles appimage

# macOS only DMG
npm run tauri build -- --bundles dmg

# Windows only MSI
npm run tauri build -- --bundles msi

# All bundles
npm run tauri build -- --bundles all
```

## Docker Builds

### Build in Docker (Linux Only)

```bash
# Build development image
docker build -t tonnytray:dev --target development .

# Build with all tests
docker build -t tonnytray:test --target test .

# Extract built artifacts
docker create --name tonnytray-extract tonnytray:dev
docker cp tonnytray-extract:/app/src-tauri/target/release/bundle ./build-output
docker rm tonnytray-extract
```

### Multi-Platform Build with Docker Buildx

```bash
# Setup buildx
docker buildx create --name multiplatform --use

# Build for multiple architectures
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --target development \
  -t tonnytray:multi \
  --push \
  .
```

## Cross-Compilation

### Linux to Windows

```bash
# Install MinGW
sudo apt-get install mingw-w64

# Add Windows target
rustup target add x86_64-pc-windows-gnu

# Build
cd src-tauri
cargo build --release --target x86_64-pc-windows-gnu
```

### macOS Universal Binary (M1 + Intel)

```bash
# Add targets
rustup target add x86_64-apple-darwin
rustup target add aarch64-apple-darwin

# Build for both architectures
cd src-tauri
cargo build --release --target x86_64-apple-darwin
cargo build --release --target aarch64-apple-darwin

# Create universal binary
lipo -create \
  target/x86_64-apple-darwin/release/tonnytray \
  target/aarch64-apple-darwin/release/tonnytray \
  -output target/release/tonnytray-universal
```

## Optimization Tips

### Reduce Binary Size

1. **Enable LTO (Link-Time Optimization)**
   ```toml
   # Already in Cargo.toml
   [profile.release]
   lto = true
   ```

2. **Strip Symbols**
   ```toml
   [profile.release]
   strip = true
   ```

3. **Use UPX (Universal Packer for eXecutables)**
   ```bash
   # Install UPX
   sudo apt-get install upx  # Linux
   brew install upx          # macOS

   # Compress binary
   upx --best --lzma src-tauri/target/release/tonnytray
   ```

### Faster Compilation

1. **Use Faster Linker**
   ```bash
   # Install mold (Linux)
   sudo apt-get install mold

   # Configure Cargo to use mold
   # Add to ~/.cargo/config.toml:
   [target.x86_64-unknown-linux-gnu]
   linker = "clang"
   rustflags = ["-C", "link-arg=-fuse-ld=mold"]
   ```

2. **Parallel Compilation**
   ```bash
   # Use all CPU cores
   export CARGO_BUILD_JOBS=$(nproc)
   cargo build --release
   ```

3. **Cache Dependencies**
   ```bash
   # Use sccache
   cargo install sccache
   export RUSTC_WRAPPER=sccache
   ```

## Troubleshooting

### Common Issues

#### "webkit2gtk not found" (Linux)

```bash
sudo apt-get install libwebkit2gtk-4.0-dev
```

#### "Failed to run custom build command for openssl-sys" (Linux)

```bash
sudo apt-get install pkg-config libssl-dev
```

#### "Linking with cc failed" (Linux)

```bash
sudo apt-get install build-essential
```

#### "xcrun: error: unable to find utility" (macOS)

```bash
xcode-select --install
```

#### "LINK : fatal error LNK1181: cannot open input file" (Windows)

- Ensure Visual Studio Build Tools are installed
- Restart your terminal after installation

#### Out of Memory During Build

```bash
# Reduce parallel jobs
CARGO_BUILD_JOBS=2 cargo build --release
```

#### Slow Build Times

```bash
# Use incremental compilation
export CARGO_INCREMENTAL=1

# Use faster linker (see Optimization Tips)
```

### Debug Build Issues

```bash
# Clean build artifacts
cargo clean
rm -rf node_modules
npm ci

# Enable verbose output
cargo build --release --verbose

# Check Rust version
rustc --version  # Should be 1.70+

# Check Node version
node --version   # Should be 18+
```

### Getting Help

- Check [DEPLOYMENT.md](DEPLOYMENT.md) for runtime issues
- Check [CONTRIBUTING.md](CONTRIBUTING.md) for development setup
- Open an issue: https://github.com/yourusername/tonnytray/issues
- Join Discord: https://discord.gg/yourserver

## CI/CD Builds

The project uses GitHub Actions for automated builds:

- **CI**: `.github/workflows/ci.yml` - Runs on every push/PR
- **Release**: `.github/workflows/release.yml` - Runs on tags
- **Security**: `.github/workflows/security-scan.yml` - Weekly scans

View build status: https://github.com/yourusername/tonnytray/actions

## Build Artifacts

### Size Estimates

- **Linux DEB**: ~15-20 MB
- **Linux AppImage**: ~25-30 MB
- **macOS DMG**: ~20-25 MB
- **Windows MSI**: ~15-20 MB
- **Windows NSIS**: ~15-20 MB

### Checksums

```bash
# Generate checksums
cd src-tauri/target/release/bundle
sha256sum deb/*.deb > SHA256SUMS
sha256sum appimage/*.AppImage >> SHA256SUMS

# Verify
sha256sum -c SHA256SUMS
```

## Next Steps

After building:
- See [DEPLOYMENT.md](DEPLOYMENT.md) for installation instructions
- See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines
- See [README.md](README.md) for usage instructions
