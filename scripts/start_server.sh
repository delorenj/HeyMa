#!/bin/bash
# WhisperLiveKit Server Startup Script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG_FILE="$SCRIPT_DIR/whisper.log"
PID_FILE="$SCRIPT_DIR/whisper.pid"

# Check if already running
if [ -f "$PID_FILE" ]; then
	OLD_PID=$(cat "$PID_FILE")
	if ps -p "$OLD_PID" >/dev/null 2>&1; then
		echo "[Already Running] PID: $OLD_PID"
		echo "Use './stop_server.sh' to stop it first"
		exit 1
	else
		echo "[Cleaning up stale PID file]"
		rm "$PID_FILE"
	fi
fi

echo "[Starting] WhisperLiveKit Server"
echo "[Log] $LOG_FILE"

# Start server in background
CUDA_VISIBLE_DEVICES="" nohup uv run whisperlivekit-server \
	--model base \
	--language en \
	--host 0.0.0.0 \
	--port 8888 \
	--disable-fast-encoder \
	--backend faster-whisper \
	>>"$LOG_FILE" 2>&1 &

# Save PID
SERVER_PID=$!
echo $SERVER_PID >"$PID_FILE"

echo "[Started] PID: $SERVER_PID"
echo "[Status] Waiting for startup..."

# Wait for server to be ready
for i in {1..15}; do
	sleep 1
	if curl -s http://localhost:8888 >/dev/null 2>&1; then
		echo "[Ready] Server is responding!"
		echo ""
		echo "Local:  ws://localhost:8888/asr"
		echo "Remote: wss://whisper.delo.sh/asr"
		echo ""
		echo "Stop with: ./stop_server.sh"
		echo "Logs:      tail -f $LOG_FILE"
		exit 0
	fi
	echo -n "."
done

echo ""
echo "[Warning] Server may still be starting. Check logs:"
echo "  tail -f $LOG_FILE"
