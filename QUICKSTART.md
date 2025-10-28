# WhisperLiveKit Quick Start

## Server is Already Running! ✅

Server: http://localhost:8888
Remote: https://whisper.delo.sh

---

## 1. Auto-Type Client (Type transcriptions into any app)

### Run It:

```bash
# Easy way
./auto-type

# Or with uv
uv run python auto_type_client.py

# From remote machine
./auto-type --remote whisper.delo.sh
```

### What It Does:

1. Connects to WhisperLiveKit
2. Captures audio from Yeti microphone (auto-detected)
3. Resamples 48kHz → 16kHz automatically
4. Types transcriptions into your active window

### Tips:

- Open a text editor first (gedit, code, etc.)
- Speak clearly
- Watch the audio meter: `████████░░░░░░░░░░░░`
- Press Ctrl+C to stop

---

## 2. n8n Webhook Client (Send to automation workflows)

### Run It:

```bash
# With n8n webhook
./n8n-webhook --n8n-webhook https://n8n.delo.sh/webhook/transcription

# Or with uv
uv run python n8n_webhook_client.py --n8n-webhook https://n8n.delo.sh/webhook/transcription
```

### What It Does:

1. Transcribes your speech
2. Sends JSON to n8n webhook
3. Triggers your automation workflows

---

## 3. Chrome Extension (Browser-based)

Located in: `chrome-extension/`

1. Open Chrome: `chrome://extensions/`
2. Enable "Developer mode"
3. "Load unpacked" → select `chrome-extension/`
4. Click extension icon to use

---

## Troubleshooting

### Server Not Running:

```bash
./start_server.sh
```

### Check Logs:

```bash
tail -f whisper.log
```

### List Audio Devices:

```bash
./auto-type --list-devices
```

### Manually Select Device:

```bash
./auto-type --device 6  # Yeti is usually device 6
```

### Test Connection:

```bash
uv run python test_connection.py
```

---

## Project Uses `uv` Package Manager

### Add Dependencies:

```bash
uv add package-name
```

### Run Scripts:

```bash
uv run python script.py

# Or use wrapper scripts
./auto-type
./n8n-webhook
```

### Dependencies Already Installed:

- numpy
- scipy
- websockets
- pyaudio
- requests

---

## Documentation

- **Full Integration Guide:** `INTEGRATION_GUIDE.md`
- **Server Management:** `start_server.sh`, `stop_server.sh`
- **Connection Test:** `test_connection.py`

---

## Quick Commands Cheat Sheet

```bash
# Start server
./start_server.sh

# Stop server
./stop_server.sh

# Auto-type locally
./auto-type

# Auto-type from remote
./auto-type --remote whisper.delo.sh

# Send to n8n
./n8n-webhook --n8n-webhook https://n8n.delo.sh/webhook/test

# Test connection
uv run python test_connection.py

# Check logs
tail -f whisper.log
```

---

**Ready to transcribe!** 🎤✨
