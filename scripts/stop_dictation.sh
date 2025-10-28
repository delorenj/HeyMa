#!/bin/bash
# Stop WhisperLiveKit dictation system

cd "$(dirname "$0")"

echo "[Dictation] Stopping system..."

# Stop auto-type client
pkill -f "auto_type_client.py" 2>/dev/null || true

# Stop server
./stop_server.sh

echo "[Dictation] Stopped"
