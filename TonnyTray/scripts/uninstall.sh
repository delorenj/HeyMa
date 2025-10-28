#!/usr/bin/env bash
# TonnyTray Uninstallation Script for Linux

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Directories
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
CONFIG_DIR="${HOME}/.config/tonnytray"
DATA_DIR="${HOME}/.local/share/tonnytray"
BIN_DIR="${HOME}/.local/bin"
DESKTOP_FILE="${HOME}/.local/share/applications/tonnytray.desktop"

echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}TonnyTray Uninstallation${NC}"
echo -e "${BLUE}======================================${NC}"
echo ""

# Confirm uninstallation
read -p "Are you sure you want to uninstall TonnyTray? (y/N): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${YELLOW}Uninstallation cancelled${NC}"
    exit 0
fi

# Ask about data removal
REMOVE_DATA=false
read -p "Remove all data and configuration? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    REMOVE_DATA=true
fi

# Stop and disable service
echo -e "${BLUE}Stopping and disabling service...${NC}"
if systemctl --user is-active --quiet tonnytray.service; then
    systemctl --user stop tonnytray.service
    echo -e "${GREEN}Service stopped${NC}"
fi

if systemctl --user is-enabled --quiet tonnytray.service 2>/dev/null; then
    systemctl --user disable tonnytray.service
    echo -e "${GREEN}Service disabled${NC}"
fi

# Remove systemd service file
if [ -f "$SYSTEMD_USER_DIR/tonnytray.service" ]; then
    rm -f "$SYSTEMD_USER_DIR/tonnytray.service"
    systemctl --user daemon-reload
    echo -e "${GREEN}Systemd service removed${NC}"
fi

# Remove binary
if [ -f "$BIN_DIR/tonnytray" ]; then
    rm -f "$BIN_DIR/tonnytray"
    echo -e "${GREEN}Binary removed${NC}"
fi

# Remove desktop entry
if [ -f "$DESKTOP_FILE" ]; then
    rm -f "$DESKTOP_FILE"
    update-desktop-database "${HOME}/.local/share/applications" 2>/dev/null || true
    echo -e "${GREEN}Desktop entry removed${NC}"
fi

# Remove DEB package if installed
if command -v dpkg &> /dev/null; then
    if dpkg -l | grep -q tonnytray; then
        echo -e "${BLUE}Removing DEB package...${NC}"
        sudo dpkg -r tonnytray
        echo -e "${GREEN}Package removed${NC}"
    fi
fi

# Remove data and configuration if requested
if [ "$REMOVE_DATA" = true ]; then
    echo -e "${BLUE}Removing data and configuration...${NC}"

    if [ -d "$CONFIG_DIR" ]; then
        rm -rf "$CONFIG_DIR"
        echo -e "${GREEN}Configuration removed${NC}"
    fi

    if [ -d "$DATA_DIR" ]; then
        rm -rf "$DATA_DIR"
        echo -e "${GREEN}Data removed${NC}"
    fi
else
    echo -e "${YELLOW}Configuration and data preserved at:${NC}"
    echo -e "  Config: ${BLUE}$CONFIG_DIR${NC}"
    echo -e "  Data:   ${BLUE}$DATA_DIR${NC}"
fi

echo ""
echo -e "${GREEN}======================================${NC}"
echo -e "${GREEN}Uninstallation Complete!${NC}"
echo -e "${GREEN}======================================${NC}"
echo ""

if [ "$REMOVE_DATA" = false ]; then
    echo -e "${YELLOW}Note: Configuration and data were preserved.${NC}"
    echo -e "To remove them manually, run:"
    echo -e "  ${BLUE}rm -rf $CONFIG_DIR${NC}"
    echo -e "  ${BLUE}rm -rf $DATA_DIR${NC}"
fi
