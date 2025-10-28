# WhisperLiveKit Integration Guide

## What WhisperLiveKit Does By Default

**Current Flow:** Microphone → WhisperLiveKit Server → Browser UI → Display Only

The transcribed text just sits in the browser. Here's how to DO something with it:

---

## Integration Option 1: Auto-Type Into Active Window (Like nerd-dictation!)

### Use Case:

Type transcriptions directly into any application - editors, terminals, browsers, etc.

### Setup:

```bash
cd /home/delorenj/code/utils/dictation/WhisperLiveKit

# Install dependencies (automatically added via uv)
uv add numpy scipy websockets pyaudio requests

# For Wayland (GNOME/KDE) - required for typing
sudo apt install wtype

# For X11 (if not using Wayland)
sudo apt install xdotool
```

### Usage:

```bash
# Local server (easy wrapper)
./auto-type

# Or with uv directly
uv run python auto_type_client.py

# Remote server (from any machine)
./auto-type --remote whisper.delo.sh

# X11 systems
./auto-type --use-xdotool

# Select specific device
./auto-type --device 6

# List available devices
./auto-type --list-devices
```

### How It Works:

1. Client connects to WhisperLiveKit WebSocket
2. Captures your microphone
3. Receives transcriptions in real-time
4. Types text into your active window using `wtype` or `xdotool`

---

## Integration Option 2: Send to n8n Workflows

### Use Case:

Trigger n8n automations with voice commands! Examples:

- "Create a task for tomorrow" → n8n → Notion/Todoist
- "Email John about the meeting" → n8n → Gmail
- "Log 8 hours to project Alpha" → n8n → Time tracking system

### Setup in n8n:

1. Create a webhook trigger node
2. Set path: `/webhook/whisper-transcription`
3. Copy the webhook URL

### Usage:

```bash
cd /home/delorenj/code/utils/dictation/WhisperLiveKit

# Dependencies already added via uv add (see above)

# Run with n8n webhook (easy wrapper)
./n8n-webhook --n8n-webhook https://n8n.delo.sh/webhook/whisper-transcription

# Or with uv directly
uv run python n8n_webhook_client.py --n8n-webhook https://n8n.delo.sh/webhook/whisper-transcription

# Remote WhisperLiveKit + n8n
./n8n-webhook \
  --whisper-url wss://whisper.delo.sh/asr \
  --n8n-webhook https://n8n.delo.sh/webhook/whisper-transcription
```

### Webhook Payload:

```json
{
  "type": "final",
  "text": "create a task for tomorrow",
  "timestamp": "2025-10-10T12:30:45Z",
  "speaker": "Speaker_01"
}
```

### Example n8n Workflow:

```
Webhook Trigger
  ↓
Parse Voice Command (AI/Regex)
  ↓
Branch Based on Intent
  ├─ Create Task → Notion
  ├─ Send Email → Gmail
  └─ Log Time → Clockify
```

---

## Integration Option 3: Chrome Extension (Already Included!)

### Use Case:

Transcribe directly in your browser - great for:

- Filling forms
- Writing emails
- Creating content

### Setup:

```bash
cd /home/delorenj/code/utils/dictation/WhisperLiveKit/chrome-extension

# Load in Chrome:
# 1. Open chrome://extensions/
# 2. Enable "Developer mode"
# 3. Click "Load unpacked"
# 4. Select the chrome-extension directory
```

### Usage:

1. Click the extension icon
2. Configure WhisperLiveKit server URL
3. Start transcribing!

---

## Comparison

| Method               | Use Case             | Best For             | Requires      |
| -------------------- | -------------------- | -------------------- | ------------- |
| **Auto-Type**        | Type anywhere        | Local productivity   | wtype/xdotool |
| **n8n Webhooks**     | Automation/workflows | Complex integrations | n8n server    |
| **Chrome Extension** | Browser-based        | Web forms, email     | Chrome        |
| **Browser UI**       | Simple viewing       | Testing, demos       | Nothing       |

---

## Custom Integrations

### WebSocket API

Connect to `ws://localhost:8888/asr` or `wss://whisper.delo.sh/asr`

**Send:** Raw PCM audio (16kHz, mono, 16-bit)
**Receive:** JSON messages

```python
import websockets
import asyncio

async def listen():
    async with websockets.connect('wss://whisper.delo.sh/asr') as ws:
        async for message in ws:
            data = json.loads(message)
            if data['type'] == 'transcript':
                print(data['text'])
                # Do whatever you want with the text!
```

---

## Advanced: Multi-User Scenarios

### Example: Team Transcription Hub

```bash
# Server (beefy machine)
whisperlivekit-server \
  --host 0.0.0.0 \
  --port 8888 \
  --model large-v3 \
  --preloaded-model-count 5  # Support 5 concurrent users

# User 1: Auto-type client
./auto_type_client.py --remote whisper.delo.sh

# User 2: n8n integration
./n8n_webhook_client.py \
  --whisper-url wss://whisper.delo.sh/asr \
  --n8n-webhook https://n8n.company.com/webhook/transcribe

# User 3: Browser UI
open https://whisper.delo.sh
```

---

## Troubleshooting

### Auto-Type Client

**Issue:** Text not typing

- **Wayland:** Make sure `wtype` is installed
- **X11:** Use `--use-xdotool` flag
- **Permissions:** Check if input methods are blocked

### n8n Webhook

**Issue:** Webhook not receiving data

- Test webhook manually: `curl -X POST https://n8n.delo.sh/webhook/test -d '{"text":"test"}'`
- Check n8n workflow is active
- Verify webhook URL is correct

### Chrome Extension

**Issue:** Can't connect to server

- Check server is running: `curl https://whisper.delo.sh`
- Verify CORS is enabled (it is by default)
- Check HTTPS/WSS for remote servers

---

## Performance Tips

### For Low Latency:

```bash
whisperlivekit-server \
  --model base \  # Fastest model
  --backend faster-whisper \  # Stable backend
  --min-chunk-size 0.5  # Quicker processing
```

### For Accuracy:

```bash
whisperlivekit-server \
  --model large-v3 \  # Most accurate
  --backend simulstreaming \  # SOTA quality
  --diarization  # Speaker identification
```

---

## Created Scripts

- `n8n_webhook_client.py` - Send transcriptions to n8n
- `auto_type_client.py` - Type transcriptions into active window
- Both support local and remote WhisperLiveKit servers!
