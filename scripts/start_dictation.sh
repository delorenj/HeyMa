#!/bin/bash
# Complete WhisperLiveKit dictation startup script

set -e
cd "$(dirname "$0")"

echo "[Dictation] Starting WhisperLiveKit system..."

# 1. Check if server is running, start if needed
if ! pgrep -f "whisperlivekit-server" > /dev/null; then
    echo "[Server] Starting WhisperLiveKit server..."
    ./start_server.sh
    sleep 3
else
    echo "[Server] Already running"
fi

# 2. Check if ydotoold is running
if ! pgrep -f "ydotoold" > /dev/null; then
    echo "[Typing] Starting ydotoold daemon..."
    sudo systemctl start ydotoold
    sleep 1
fi

# 3. Start auto-type client
echo "[Client] Starting auto-type client..."
exec ./auto-type --whisper-url ws://localhost:8888/asr
