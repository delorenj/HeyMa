#!/usr/bin/env bash
# TonnyTray Installation Script for Linux
# Installs TonnyTray and sets up all necessary dependencies

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Constants
APP_NAME="TonnyTray"
INSTALL_DIR="/opt/tonnytray"
BIN_DIR="/usr/local/bin"
DESKTOP_DIR="/usr/share/applications"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
CONFIG_DIR="${HOME}/.config/tonnytray"
DATA_DIR="${HOME}/.local/share/tonnytray"
LOG_DIR="${HOME}/.local/share/tonnytray/logs"

echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}TonnyTray Installation${NC}"
echo -e "${BLUE}======================================${NC}"
echo ""

# Check if running as root for system-wide installation
SYSTEM_INSTALL=false
if [ "$EUID" -eq 0 ]; then
    SYSTEM_INSTALL=true
    echo -e "${YELLOW}Installing system-wide (requires root)${NC}"
else
    echo -e "${GREEN}Installing for current user${NC}"
fi

# Detect package manager
detect_package_manager() {
    if command -v apt-get &> /dev/null; then
        echo "apt"
    elif command -v dnf &> /dev/null; then
        echo "dnf"
    elif command -v yum &> /dev/null; then
        echo "yum"
    elif command -v pacman &> /dev/null; then
        echo "pacman"
    elif command -v zypper &> /dev/null; then
        echo "zypper"
    else
        echo "unknown"
    fi
}

PKG_MANAGER=$(detect_package_manager)

# Install system dependencies
install_dependencies() {
    echo -e "${BLUE}Installing system dependencies...${NC}"

    case $PKG_MANAGER in
        apt)
            sudo apt-get update
            sudo apt-get install -y \
                libwebkit2gtk-4.0-37 \
                libgtk-3-0 \
                libayatana-appindicator3-1 \
                libasound2 \
                libpulse0 \
                curl \
                wget
            ;;
        dnf|yum)
            sudo $PKG_MANAGER install -y \
                webkit2gtk3 \
                gtk3 \
                libappindicator-gtk3 \
                alsa-lib \
                pulseaudio-libs \
                curl \
                wget
            ;;
        pacman)
            sudo pacman -S --noconfirm \
                webkit2gtk \
                gtk3 \
                libappindicator-gtk3 \
                alsa-lib \
                libpulse \
                curl \
                wget
            ;;
        zypper)
            sudo zypper install -y \
                webkit2gtk3 \
                gtk3 \
                libappindicator3-1 \
                alsa \
                libpulse0 \
                curl \
                wget
            ;;
        *)
            echo -e "${YELLOW}Unknown package manager. Please install dependencies manually:${NC}"
            echo "  - webkit2gtk-4.0"
            echo "  - gtk-3"
            echo "  - libappindicator3"
            echo "  - alsa/pulseaudio"
            read -p "Press Enter to continue after installing dependencies..."
            ;;
    esac

    echo -e "${GREEN}Dependencies installed successfully${NC}"
}

# Create necessary directories
create_directories() {
    echo -e "${BLUE}Creating directories...${NC}"

    mkdir -p "$CONFIG_DIR"
    mkdir -p "$DATA_DIR"
    mkdir -p "$LOG_DIR"
    mkdir -p "$SYSTEMD_USER_DIR"

    echo -e "${GREEN}Directories created${NC}"
}

# Install from DEB package
install_from_deb() {
    local deb_file=$1
    echo -e "${BLUE}Installing from DEB package...${NC}"

    if [ ! -f "$deb_file" ]; then
        echo -e "${RED}Error: DEB file not found: $deb_file${NC}"
        exit 1
    fi

    sudo dpkg -i "$deb_file"
    sudo apt-get install -f -y  # Fix dependencies

    echo -e "${GREEN}Installation complete${NC}"
}

# Install from AppImage
install_from_appimage() {
    local appimage_file=$1
    echo -e "${BLUE}Installing from AppImage...${NC}"

    if [ ! -f "$appimage_file" ]; then
        echo -e "${RED}Error: AppImage file not found: $appimage_file${NC}"
        exit 1
    fi

    # Make executable
    chmod +x "$appimage_file"

    # Move to install location
    if [ "$SYSTEM_INSTALL" = true ]; then
        sudo mkdir -p "$INSTALL_DIR"
        sudo cp "$appimage_file" "$INSTALL_DIR/tonnytray"
        sudo ln -sf "$INSTALL_DIR/tonnytray" "$BIN_DIR/tonnytray"
    else
        mkdir -p "${HOME}/.local/bin"
        cp "$appimage_file" "${HOME}/.local/bin/tonnytray"
    fi

    echo -e "${GREEN}Installation complete${NC}"
}

# Download latest release
download_latest() {
    echo -e "${BLUE}Downloading latest release...${NC}"

    # Get latest release info from GitHub
    local repo="yourusername/tonnytray"
    local release_url="https://api.github.com/repos/${repo}/releases/latest"

    # Detect architecture
    local arch=$(uname -m)
    local download_url=""

    if [ "$arch" = "x86_64" ]; then
        # Prefer DEB on Debian-based systems
        if [ "$PKG_MANAGER" = "apt" ]; then
            download_url=$(curl -s "$release_url" | grep "browser_download_url.*amd64.deb" | cut -d '"' -f 4)
            local filename="tonnytray.deb"
        else
            download_url=$(curl -s "$release_url" | grep "browser_download_url.*amd64.AppImage" | cut -d '"' -f 4)
            local filename="tonnytray.AppImage"
        fi
    else
        echo -e "${RED}Unsupported architecture: $arch${NC}"
        exit 1
    fi

    if [ -z "$download_url" ]; then
        echo -e "${RED}Could not find download URL${NC}"
        exit 1
    fi

    echo -e "${GREEN}Downloading: $download_url${NC}"
    wget -O "$filename" "$download_url"

    if [[ "$filename" == *.deb ]]; then
        install_from_deb "$filename"
    elif [[ "$filename" == *.AppImage ]]; then
        install_from_appimage "$filename"
    fi

    rm -f "$filename"
}

# Setup configuration
setup_config() {
    echo -e "${BLUE}Setting up configuration...${NC}"

    # Copy example config if it doesn't exist
    if [ ! -f "$CONFIG_DIR/config.json" ]; then
        if [ -f "config.example.json" ]; then
            cp config.example.json "$CONFIG_DIR/config.json"
            echo -e "${GREEN}Created default configuration${NC}"
        else
            cat > "$CONFIG_DIR/config.json" <<EOF
{
  "server": {
    "url": "ws://localhost:8888/asr",
    "model": "base",
    "language": "en",
    "auto_start": true
  },
  "n8n": {
    "webhook_url": ""
  },
  "elevenlabs": {
    "api_key": "",
    "voice_id": "",
    "enabled": false
  }
}
EOF
            echo -e "${YELLOW}Created minimal configuration. Please edit $CONFIG_DIR/config.json${NC}"
        fi
    else
        echo -e "${GREEN}Configuration already exists${NC}"
    fi
}

# Setup systemd service for auto-start
setup_systemd() {
    echo -e "${BLUE}Setting up systemd service...${NC}"

    cat > "$SYSTEMD_USER_DIR/tonnytray.service" <<EOF
[Unit]
Description=TonnyTray - WhisperLiveKit System Tray
After=network.target

[Service]
Type=simple
ExecStart=${HOME}/.local/bin/tonnytray
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

    # Reload systemd and enable service
    systemctl --user daemon-reload
    systemctl --user enable tonnytray.service

    echo -e "${GREEN}Systemd service installed and enabled${NC}"
}

# Create desktop entry
create_desktop_entry() {
    echo -e "${BLUE}Creating desktop entry...${NC}"

    local desktop_file="${HOME}/.local/share/applications/tonnytray.desktop"

    cat > "$desktop_file" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=TonnyTray
Comment=WhisperLiveKit System Tray Application
Exec=tonnytray
Icon=tonnytray
Terminal=false
Categories=Utility;Audio;
StartupNotify=false
X-GNOME-Autostart-enabled=true
EOF

    chmod +x "$desktop_file"
    update-desktop-database "${HOME}/.local/share/applications" 2>/dev/null || true

    echo -e "${GREEN}Desktop entry created${NC}"
}

# Main installation flow
main() {
    echo -e "${BLUE}Starting installation...${NC}"
    echo ""

    # Install dependencies
    install_dependencies

    # Create directories
    create_directories

    # Install from provided file or download latest
    if [ $# -gt 0 ]; then
        local install_file=$1
        if [[ "$install_file" == *.deb ]]; then
            install_from_deb "$install_file"
        elif [[ "$install_file" == *.AppImage ]]; then
            install_from_appimage "$install_file"
        else
            echo -e "${RED}Unsupported file format${NC}"
            exit 1
        fi
    else
        download_latest
    fi

    # Setup configuration
    setup_config

    # Setup systemd service
    setup_systemd

    # Create desktop entry
    create_desktop_entry

    echo ""
    echo -e "${GREEN}======================================${NC}"
    echo -e "${GREEN}Installation Complete!${NC}"
    echo -e "${GREEN}======================================${NC}"
    echo ""
    echo -e "To start TonnyTray now:"
    echo -e "  ${BLUE}systemctl --user start tonnytray${NC}"
    echo ""
    echo -e "To check status:"
    echo -e "  ${BLUE}systemctl --user status tonnytray${NC}"
    echo ""
    echo -e "Configuration file:"
    echo -e "  ${BLUE}$CONFIG_DIR/config.json${NC}"
    echo ""
    echo -e "Logs:"
    echo -e "  ${BLUE}journalctl --user -u tonnytray -f${NC}"
    echo ""
}

# Run main installation
main "$@"
