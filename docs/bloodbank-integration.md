# WhisperLiveKit Bloodbank Integration

## Overview

WhisperLiveKit now publishes transcription events to the Bloodbank event bus, enabling event-driven workflows across the 33GOD ecosystem.

## Architecture

```
┌──────────────┐      WebSocket      ┌──────────────────┐
│              │ ───────────────────> │                  │
│   Client     │                      │ WhisperLiveKit   │
│ (Mobile/Web) │ <─────────────────── │                  │
└──────────────┘      Transcription   └──────────────────┘
                                              │
                                              │ Publish Event
                                              ▼
                                      ┌──────────────────┐
                                      │    Bloodbank     │
                                      │   (RabbitMQ)     │
                                      └──────────────────┘
                                              │
                        ┌─────────────────────┼─────────────────────┐
                        │                     │                     │
                        ▼                     ▼                     ▼
                ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
                │   Candybar   │     │    Tonny     │     │  Candystore  │
                │  (Monitor)   │     │   (Agent)    │     │  (Storage)   │
                └──────────────┘     └──────────────┘     └──────────────┘
```

## Features

### 1. HolyFields Schema Validation

Transcription events conform to a strict JSON schema defined in HolyFields:

```json
{
  "text": "The transcribed text",
  "timestamp": "2026-01-27T10:00:00Z",
  "source": "whisperlivekit",
  "target": "tonny",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "audio_metadata": {
    "duration_seconds": 3.5,
    "sample_rate": 16000,
    "confidence": 0.95,
    "language": "en",
    "model": "base",
    "speaker_id": "speaker_1"
  },
  "context": {
    "device_type": "mobile",
    "user_id": "user123",
    "tags": ["meeting", "notes"]
  }
}
```

Schema location: `/home/delorenj/code/33GOD/holyfields/trunk-main/docs/schemas/voice/transcription.v1.schema.json`

### 2. Session ID Tracking

Each WebSocket connection receives a unique session ID (UUID v4) that:

- Identifies the connection across its lifetime
- Included in all transcription events for that session
- Sent to client in handshake message:

```json
{
  "type": "session_info",
  "session_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### 3. Bloodbank Event Publishing

Events are published via the `bb` CLI command with:

- **Routing key**: `transcription.voice.completed`
- **Exchange**: `33god.events` (topic exchange)
- **Retry logic**: Up to 3 attempts with exponential backoff
- **Durability**: Write-ahead log (WAL) ensures zero data loss

### 4. Write-Ahead Log (WAL)

Events are persisted to `raw_voice_ingest.jsonl` before publishing:

- **Durability**: Events survive system crashes
- **Recovery**: WAL can be replayed via `publisher.replay_wal()`
- **Format**: One JSON event per line (JSONL)
- **Cleanup**: WAL cleared after successful replay

## Implementation Details

### BloodbankPublisher Class

Located: `whisperlivekit/bloodbank_publisher.py`

Key methods:

```python
async def publish_transcription(
    text: str,
    session_id: UUID,
    source: str = "whisperlivekit",
    target: Optional[str] = None,
    audio_metadata: Optional[dict] = None,
    context: Optional[dict] = None,
) -> bool:
    """Publish transcription event to Bloodbank."""
```

```python
async def replay_wal() -> int:
    """Replay events from WAL (for recovery)."""
```

### Integration Points

1. **Server Initialization** (`basic_server.py`):
   - Creates `BloodbankPublisher` instance
   - Checks `bb` CLI availability
   - Logs configuration status

2. **WebSocket Connection** (`basic_server.py`):
   - Generates unique session ID
   - Sends session ID to client
   - Passes publisher to `AudioProcessor`

3. **Transcription Processing** (`audio_processor.py`):
   - Publishes events when new tokens are finalized
   - Includes audio metadata (duration, sample rate, model, language)
   - Includes speaker ID if diarization enabled
   - Non-blocking publish (fire-and-forget)

## Usage

### Starting the Server

```bash
# Start WhisperLiveKit with default settings
cd ~/code/33GOD/HeyMa/trunk-main
./scripts/start_server.sh

# Server will log:
# - Bloodbank publisher initialization
# - bb CLI availability status
# - Session IDs for each connection
```

### Monitoring Events

```bash
# Watch events in real-time via Bloodbank CLI
bb watch transcription.voice.completed

# Or use Candybar desktop app for visual monitoring
cd ~/code/33GOD/candybar/trunk-main
npm run dev
```

### Replaying WAL (Recovery)

If events were saved to WAL during downtime:

```python
from whisperlivekit.bloodbank_publisher import BloodbankPublisher

publisher = BloodbankPublisher()
count = await publisher.replay_wal()
print(f"Replayed {count} events")
```

## Event Flow Example

1. **Client connects**:
   - Server generates session ID: `550e8400-e29b-41d4-a716-446655440000`
   - Sends handshake: `{"type": "session_info", "session_id": "..."}`

2. **User speaks**: "Log this idea: we need better cooling"

3. **Transcription completes**:
   - New tokens: `["Log", "this", "idea:", ...]`
   - Text finalized: "Log this idea: we need better cooling"

4. **Event published**:
   - WAL write: Event persisted to `raw_voice_ingest.jsonl`
   - bb publish: Event sent to RabbitMQ exchange `33god.events`
   - Routing key: `transcription.voice.completed`

5. **Consumers receive**:
   - Candybar displays event in real-time
   - Tonny agent processes transcription
   - Candystore stores event in database

## Configuration

### Environment Variables

```bash
# RabbitMQ connection (if not using defaults)
export RABBITMQ_URL="amqp://user:pass@rabbitmq.delo.sh/"

# WAL location (optional)
export WAL_PATH="/var/log/whisperlivekit/wal.jsonl"
```

### Code Configuration

```python
# In basic_server.py lifespan
bloodbank_publisher = BloodbankPublisher(
    max_retries=3,          # Number of retry attempts
    retry_delay=1.0,        # Delay between retries (seconds)
    enable_wal=True,        # Enable write-ahead log
    wal_path=Path("./raw_voice_ingest.jsonl"),  # WAL file path
)
```

## Testing

### Unit Tests

```bash
# Run Bloodbank publisher tests
pytest tests/integration/test_bloodbank_publisher.py -v
```

### Integration Tests (Requires RabbitMQ)

```bash
# Run full integration tests with real RabbitMQ
pytest tests/integration/test_rabbitmq_integration.py -v

# Skip if RabbitMQ not available
pytest -m "not rabbitmq"
```

### Manual Testing

1. **Publish test event**:

```bash
bb publish transcription.voice.completed --json '{
  "text": "Test transcription",
  "timestamp": "2026-01-27T10:00:00Z",
  "source": "whisperlivekit",
  "session_id": "550e8400-e29b-41d4-a716-446655440000"
}'
```

2. **Consume events**:

```bash
# Watch for transcription events
bb watch transcription.voice.completed
```

3. **Check WAL**:

```bash
# View raw events saved to WAL
tail -f raw_voice_ingest.jsonl | jq
```

## Troubleshooting

### bb Command Not Found

**Symptom**: `bb command not found - is Bloodbank installed?`

**Solution**:
```bash
# Install Bloodbank
cd ~/code/33GOD/bloodbank/trunk-main
uv sync
uv pip install -e .

# Verify installation
which bb
bb --version
```

### Events Not Appearing in Consumers

**Check 1**: Verify RabbitMQ is running

```bash
# Check RabbitMQ status
sudo systemctl status rabbitmq-server

# Check management UI
open http://localhost:15672
# Login: guest / guest
```

**Check 2**: Verify routing key binding

```bash
# List bindings for transcription events
bb list-events | grep transcription
```

**Check 3**: Check WAL for events

```bash
# Events should be in WAL even if publish failed
cat raw_voice_ingest.jsonl | jq
```

### High WAL Size

**Symptom**: `raw_voice_ingest.jsonl` growing large

**Cause**: Events not being published successfully

**Solution**:
```bash
# Check bb can connect to RabbitMQ
bb publish test-event --mock --dry-run

# Replay WAL to clear backlog
python -c "
import asyncio
from whisperlivekit.bloodbank_publisher import BloodbankPublisher
publisher = BloodbankPublisher()
asyncio.run(publisher.replay_wal())
"
```

## Dependencies

- **Bloodbank**: Event bus CLI (`bb` command)
- **HolyFields**: Schema definitions and validation
- **RabbitMQ**: Message broker (amqp://localhost:5672)
- **aio-pika**: Async RabbitMQ client (for integration tests)

## Implementation Checklist

- [x] STORY-011: Integrate HolyFields Schema Validation
  - [x] Create `transcription.v1.schema.json`
  - [x] Generate Python Pydantic bindings
  - [x] Export models in HolyFields `__init__.py`

- [x] STORY-012: Implement Bloodbank Event Publishing
  - [x] Create `BloodbankPublisher` class
  - [x] Implement publish via `bb` CLI
  - [x] Add retry logic (max 3 attempts)
  - [x] Integrate with `AudioProcessor`

- [x] STORY-013: Add Session ID Tracking
  - [x] Generate UUID for each WebSocket connection
  - [x] Send session ID in handshake
  - [x] Include session ID in all events

- [x] Integration Tests
  - [x] Unit tests for `BloodbankPublisher`
  - [x] Integration tests with RabbitMQ
  - [x] WAL replay tests
  - [x] Schema validation tests

## Future Enhancements

1. **Batch Publishing**: Collect multiple events and publish in batches
2. **Compression**: Compress large transcriptions before publishing
3. **Filtering**: Allow clients to specify target consumers
4. **Metrics**: Track publish success rate, latency, WAL size
5. **Dead Letter Queue**: Handle permanently failed events
6. **Event Correlation**: Link related events via correlation IDs

## Support

For issues or questions:

- **Documentation**: This file
- **Schema Reference**: HolyFields `/docs/schemas/voice/`
- **CLI Help**: `bb help`
- **Logs**: Check WhisperLiveKit server logs for publish status
