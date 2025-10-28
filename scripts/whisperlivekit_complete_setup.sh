#!/bin/bash
# WhisperLiveKit Complete Setup Script
# Sets up server autostart + keybindings + system tray integration

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🚀 WhisperLiveKit Complete Setup"
echo "=================================="

# Function to check command existence
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# 1. Install system dependencies
echo ""
echo "📦 Installing system dependencies..."
if command_exists apt-get; then
    sudo apt-get update
    sudo apt-get install -y \
        xbindkeys \
        xdotool \
        wtype \
        curl \
        jq
elif command_exists pacman; then
    sudo pacman -S --needed --noconfirm \
        xbindkeys \
        xdotool \
        wtype \
        curl \
        jq
else
    echo "⚠️  Please install these packages manually: xbindkeys, xdotool, wtype, curl, jq"
fi

# 2. Set up systemd user service for WhisperLiveKit server
echo ""
echo "🔄 Setting up WhisperLiveKit server autostart..."

SERVICE_FILE="$HOME/.config/systemd/user/whisperlivekit-server.service"

mkdir -p "$HOME/.config/systemd/user"

cat > "$SERVICE_FILE" << 'EOF'
[Unit]
Description=WhisperLiveKit Server
After=network.target
Wants=network.target

[Service]
Type=simple
WorkingDirectory=$HOME/code/utils/dictation/WhisperLiveKit
ExecStart=/bin/bash -c "uv run whisperlivekit-server --model base --language en --host 0.0.0.0 --port 8888 --disable-fast-encoder --backend faster-whisper"
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=whisperlivekit-server

# Resource limits
CPUQuota=50%
MemoryLimit=2G
Nice=10

[Install]
WantedBy=default.target
EOF

# Load and enable the service
systemctl --user daemon-reload
systemctl --user enable whisperlivekit-server.service

echo "✅ Server service installed (starts on login)"

# 3. Set up global keybindings
echo ""
echo "⌨️  Setting up global keybindings..."

# xbindkeys configuration
XBINDKEYS_FILE="$HOME/.xbindkeysrc.dictation"

cat > "$XBINDKEYS_FILE" << 'EOF'
# WhisperLiveKit dictation controls
# Toggle dictation: Ctrl+\ (backslash)
"bash -c 'cd /home/delorenj/code/utils/dictation/WhisperLiveKit && ./auto_type_toggle.sh'"
    control + backslash

# Emergency stop: Ctrl+Backspace  
"bash -c 'cd /home/delorenj/code/utils/dictation/WhisperLiveKit && pkill -f auto_type_client.py'"
    control + BackSpace

# Alternative bindings for numeric keypad
"bash -c 'cd /home/delorenj/code/utils/dictation/WhisperLiveKit && ./auto_type_toggle.sh'"
    control + KP_Enter
EOF

# Merge with existing xbindkeys config if it exists
if [ -f "$HOME/.xbindkeysrc" ]; then
    echo "# WhisperLiveKit bindings" >> "$HOME/.xbindkeysrc"
    cat "$XBINDKEYS_FILE" >> "$HOME/.xbindkeysrc"
    rm "$XBINDKEYS_FILE"
else
    mv "$XBINDKEYS_FILE" "$HOME/.xbindkeysrc"
fi

# Kill and restart xbindkeys to apply changes
pkill xbindkeys || true
xbindkeys &

echo "✅ Keybindings configured: Ctrl+\ to toggle, Ctrl+Backspace to emergency stop"

# 4. Set up i3/sway keybindings (if applicable)
echo ""
if [ -n "$DISPLAY" ] && (pgrep -x i3 >/dev/null || pgrep -x sway >/dev/null); then
    echo "🎯 Setting up i3/sway keybindings..."
    
    SWAY_CONFIG="$HOME/.config/sway/config"
    I3_CONFIG="$HOME/.config/i3/config"
    SXHKD_CONFIG="$HOME/.config/sxhkd/sxhkdrc"
    
    # sxhkd (works with both i3 and sway)
    mkdir -p "$(dirname "$SXHKD_CONFIG")"
    
    cat >> "$SXHKD_CONFIG" << 'EOF'

# WhisperLiveKit controls
super + grave
    bash -c 'cd /home/delorenj/code/utils/dictation/WhisperLiveKit && ./auto_type_toggle.sh'

super + shift + grave  
    bash -c 'cd /home/delorenj/code/utils/dictation/WhisperLiveKit && pkill -f auto_type_client.py'
EOF
    
    pkill sxhkd || true
    sxhkd &
    
    echo "✅ sxhkd keybindings added (super+grave to toggle)"
else
    echo "ℹ️  i3/sway not detected, skipping window manager keybindings"
fi

# 5. Create status checking script
echo ""
echo "📊 Setting up status checking..."

cat > "$SCRIPT_DIR/status.sh" << 'EOF'
#!/bin/bash
# Check WhisperLiveKit system status

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🔍 WhisperLiveKit Status"
echo "========================"

# Server status
if systemctl --user is-active whisperlivekit-server.service >/dev/null; then
    echo "✅ Server: RUNNING"
else
    echo "❌ Server: STOPPED"
fi

# Auto-type client status
if pgrep -f "auto_type_client.py" >/dev/null; then
    echo "🎤 Dictation: ACTIVE"
else
    echo "🚫 Dictation: INACTIVE"
fi

# Server health check
if curl -s http://localhost:8888 >/dev/null; then
    echo "💚 Health: GOOD (port 8888)"
else
    echo "💔 Health: UNREACHABLE"
fi

# CPU/Memory usage
SERVER_PID=$(systemctl --user show whisperlivekit-server.service | grep MainPID= | cut -d= -f2)
if [ -n "$SERVER_PID" ] && [ "$SERVER_PID" != "0" ]; then
    CPU=$(ps -p "$SERVER_PID" -o pcpu= | tr -d ' ')
    MEM=$(ps -p "$SERVER_PID" -o pmem= | tr -d ' ')
    echo "📈 Usage: CPU=${CPU}%, MEM=${MEM}%"
fi

echo ""
echo "Controls:"
echo "  Ctrl+\         Toggle dictation"
echo "  Ctrl+Backspace Emergency stop"
echo "  ./status.sh    Check status"
echo "  ./start.sh     Start system"
echo "  ./stop.sh      Stop system"
EOF

chmod +x "$SCRIPT_DIR/status.sh"

# Create convenience scripts
cat > "$SCRIPT_DIR/start.sh" << 'EOF'
#!/bin/bash
# Start WhisperLiveKit system
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

systemctl --user start whisperlivekit-server.service
xbindkeys &
EOF

chmod +x "$SCRIPT_DIR/start.sh"

cat > "$SCRIPT_DIR/stop.sh" << 'EOF'
#!/bin/bash
# Stop WhisperLiveKit system  
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

pkill -f "auto_type_client.py" || true
systemctl --user stop whisperlivekit-server.service
pkill xbindkeys || true
EOF

chmod +x "$SCRIPT_DIR/stop.sh"

echo "✅ Convenience scripts created (start.sh, stop.sh, status.sh)"

# 6. Integration with TonnyTray GUI
echo ""
echo "🔗 Setting up TonnyTray integration..."

# Create API endpoint for TonnyTray to poll status
cat > "$SCRIPT_DIR/tonnytray_status.py" << 'EOF'
#!/usr/bin/env python3
"""
TonnyTray integration API - provides status information
"""

import subprocess
import json
import sys

def get_server_status():
    """Check if WhisperLiveKit server is running"""
    try:
        result = subprocess.run(['systemctl', '--user', 'is-active', 'whisperlivekit-server.service'],
                              capture_output=True, text=True)
        return result.stdout.strip() == 'active'
    except:
        return False

def get_dictation_status():
    """Check if dictation client is running"""
    try:
        result = subprocess.run(['pgrep', '-f', 'auto_type_client.py'],
                              capture_output=True)
        return result.returncode == 0
    except:
        return False

def get_health():
    """Check server health"""
    try:
        import requests
        response = requests.get('http://localhost:8888', timeout=2)
        return response.status_code == 200
    except:
        return False

if __name__ == "__main__":
    status = {
        "server_running": get_server_status(),
        "dictation_active": get_dictation_status(),
        "health_good": get_health()
    }
    
    print(json.dumps(status, indent=2))
EOF

chmod +x "$SCRIPT_DIR/tonnytray_status.py"

echo "✅ TonnyTray integration API created"

# 7. Final setup instructions
echo ""
echo "🎉 Setup Complete!"
echo "=================="
echo ""
echo "Next steps:"
echo "1. 🔄 Log out and back in to enable autostart"
echo "2. 📱 Launch TonnyTray GUI: cd TonnyTray && npm run tauri dev"
echo "3. 🎤 Test dictation: Ctrl+\ to toggle"
echo ""
echo "Full paths created:"
echo "  $SCRIPT_DIR/start.sh              - Manual start"
echo "  $SCRIPT_DIR/stop.sh               - Manual stop"
echo "  $SCRIPT_DIR/status.sh             - Check status"
echo "  $SCRIPT_DIR/auto_type_toggle.sh   - Dictation toggle"
echo "  $SCRIPT_DIR/tonnytray_status.py   - GUI status API"
echo "  $SERVICE_FILE                     - Autostart service"