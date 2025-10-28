#!/bin/bash
# Stop WhisperLiveKit Server

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/whisper.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "[Not Running] No PID file found"
    exit 1
fi

PID=$(cat "$PID_FILE")

if ! ps -p "$PID" > /dev/null 2>&1; then
    echo "[Not Running] Process $PID not found"
    rm "$PID_FILE"
    exit 1
fi

echo "[Stopping] WhisperLiveKit Server (PID: $PID)"
kill "$PID"

# Wait for process to stop
for i in {1..10}; do
    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "[Stopped] Server shut down successfully"
        rm "$PID_FILE"
        exit 0
    fi
    sleep 1
    echo -n "."
done

echo ""
echo "[Force Kill] Server didn't stop gracefully, forcing..."
kill -9 "$PID" 2>/dev/null
rm "$PID_FILE"
echo "[Stopped] Server terminated"
