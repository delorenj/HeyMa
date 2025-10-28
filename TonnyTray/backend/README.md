# TonnyTray Integration Layer

Complete backend integration service for TonnyTray, providing resilient connections to all external services with comprehensive error handling, offline queuing, and monitoring.

## Features

### 🔌 Service Integrations

1. **n8n Workflow Automation**
   - WebSocket and HTTP webhook support
   - Automatic reconnection with exponential backoff
   - Circuit breaker pattern (fails after 3 attempts, recovers after 30s)
   - Offline queue for failed requests
   - Request/response logging to SQLite database
   - Webhook signature verification

2. **ElevenLabs Text-to-Speech**
   - Streaming TTS generation
   - Voice selection and management
   - Audio playback queue with priority
   - Caching system for generated audio
   - Volume, speed, and pitch controls
   - Quota management and limit handling

3. **WhisperLiveKit Transcription**
   - Real-time WebSocket streaming
   - Health monitoring with auto-reconnect
   - Confidence score tracking
   - Voice Activity Detection (VAD)
   - Multi-language support
   - Model switching on-the-fly

4. **RabbitMQ Event Bus (Optional)**
   - AMQP client with connection pooling
   - Topic-based event publishing
   - Automatic reconnection
   - Event schema validation
   - Configurable exchange and routing

5. **Audio Pipeline**
   - Multiple capture modes (continuous, VAD, push-to-talk)
   - Real-time audio level monitoring
   - Automatic device change detection
   - Support for multiple audio backends (sounddevice, pyaudio)
   - Configurable sample rates and formats

### 🛡️ Resilience Features

- **Circuit Breaker Pattern**: Prevents cascading failures by failing fast
- **Offline Queue**: Stores failed requests for retry when service recovers
- **Automatic Reconnection**: All services reconnect automatically with backoff
- **Health Monitoring**: Continuous health checks with detailed diagnostics
- **Error Recovery**: Graceful degradation and recovery strategies

### 📊 Monitoring & Logging

- **SQLite Database**: Complete conversation and metrics logging
- **Performance Metrics**: Track latency, throughput, and error rates
- **Health Dashboard**: Real-time status of all integrations
- **Detailed Logging**: Configurable log levels with file and console output

## Installation

```bash
# Navigate to backend directory
cd /home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray/backend

# Install dependencies
pip install -r requirements.txt

# Optional: Install with RabbitMQ support
pip install aio-pika
```

## Configuration

Create a `config.json` file:

```json
{
  "database_path": "tonny_integration.db",

  "n8n_webhook_url": "http://localhost:5678/webhook/tonny",
  "n8n_websocket_url": "ws://localhost:5678/ws",
  "n8n_api_key": "your-api-key",
  "n8n_webhook_secret": "your-webhook-secret",

  "elevenlabs_api_key": "your-elevenlabs-key",
  "elevenlabs_voice_id": "voice-id",
  "elevenlabs_cache_dir": "tts_cache",

  "whisper_websocket_url": "ws://localhost:9090",
  "whisper_http_url": "http://localhost:9090",
  "whisper_model": "base",
  "whisper_language": "en",

  "rabbitmq_enabled": false,
  "rabbitmq_url": "amqp://guest:guest@localhost/",

  "audio_mode": "voice_activation",
  "audio_input_device": null,
  "audio_output_device": null,

  "enable_logging": true,
  "log_level": "INFO",
  "health_check_interval": 30.0
}
```

## Usage

### Starting the Service

```bash
# Start with default configuration
python main.py start

# Start with custom config
python main.py --config config.json start

# Start with debug logging
python main.py --log-level DEBUG start
```

### Testing Integrations

```bash
# Test all integrations
python main.py test

# Check health status
python main.py health

# Test audio pipeline
python main.py audio --mode voice_activation

# Test TTS
python main.py tts "Hello world" --voice voice-id

# Publish RabbitMQ event (if enabled)
python main.py publish "thread.tonny.test" '{"message": "test"}'
```

## API Usage

### Python Integration

```python
import asyncio
from backend import IntegrationOrchestrator, IntegrationConfig

async def main():
    # Create configuration
    config = IntegrationConfig(
        n8n_webhook_url="http://localhost:5678/webhook/tonny",
        elevenlabs_api_key="your-key",
        whisper_websocket_url="ws://localhost:9090"
    )

    # Initialize orchestrator
    orchestrator = IntegrationOrchestrator(config)
    await orchestrator.initialize()

    # Start listening
    await orchestrator.start_listening()

    # Run tests
    results = await orchestrator.test_integrations()
    print(f"Test results: {results}")

    # Get health status
    health = await orchestrator.get_health_status()
    print(f"Health: {health}")

    # Shutdown
    await orchestrator.shutdown()

asyncio.run(main())
```

### Individual Service Usage

```python
# n8n Client
from backend.integrations import N8nClient, N8nConfig

config = N8nConfig(webhook_url="http://localhost:5678/webhook")
client = N8nClient(config)
await client.connect()
response = await client.send_request("process", {"text": "Hello"})

# ElevenLabs Client
from backend.integrations import ElevenLabsClient

client = ElevenLabsClient(api_key="your-key")
await client.initialize()
voices = await client.get_voices()
audio = await client.text_to_speech("Hello", voice_id)

# WhisperLiveKit Client
from backend.integrations import WhisperLiveKitClient, WhisperConfig

config = WhisperConfig(websocket_url="ws://localhost:9090")
client = WhisperLiveKitClient(config)
await client.connect()
await client.send_audio(audio_bytes)
```

## Architecture

### End-to-End Flow

1. **Audio Capture** → Microphone input with VAD
2. **Transcription** → WhisperLiveKit processes audio
3. **Processing** → n8n workflow handles transcription
4. **Response Generation** → n8n returns response
5. **Text-to-Speech** → ElevenLabs generates audio
6. **Audio Playback** → Response played to user
7. **Logging** → All events logged to database

### Component Interaction

```
┌─────────────────┐
│  Audio Pipeline │
└────────┬────────┘
         │ Audio Data
         ▼
┌─────────────────┐     ┌─────────────────┐
│ WhisperLiveKit  │────▶│   Orchestrator  │
└─────────────────┘     └────────┬────────┘
   Transcription                 │
                                │ Coordinates
┌─────────────────┐             │
│   n8n Client    │◀────────────┤
└─────────────────┘             │
     Response                    │
                                │
┌─────────────────┐             │
│  ElevenLabs TTS │◀────────────┤
└─────────────────┘             │
                                │
┌─────────────────┐             │
│    RabbitMQ     │◀────────────┤
└─────────────────┘             │
    (Optional)                   │
                                │
┌─────────────────┐             │
│  SQLite Database│◀────────────┘
└─────────────────┘
      Logging
```

## Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test suite
pytest tests/test_integrations.py::TestN8nIntegration -v

# Run with coverage
pytest tests/ --cov=backend --cov-report=html

# Run performance tests
pytest tests/test_integrations.py::TestPerformance -v
```

## Monitoring

### Database Queries

```sql
-- View recent conversations
SELECT * FROM conversations
WHERE session_id = 'session-id'
ORDER BY timestamp DESC
LIMIT 10;

-- View system metrics
SELECT * FROM metrics
WHERE metric_name = 'health.n8n'
ORDER BY timestamp DESC
LIMIT 100;

-- Get conversation statistics
SELECT
  type,
  COUNT(*) as count,
  AVG(json_extract(metadata, '$.confidence')) as avg_confidence
FROM conversations
GROUP BY type;
```

### Health Monitoring

The system continuously monitors:
- Service connectivity
- Response times
- Error rates
- Audio levels
- Memory usage
- Queue sizes

## Troubleshooting

### Common Issues

1. **WebSocket Connection Failed**
   - Check service URLs in configuration
   - Verify services are running
   - Check firewall/network settings

2. **Audio Not Detected**
   - Run `python main.py audio` to test devices
   - Check microphone permissions
   - Verify correct device index

3. **TTS Not Playing**
   - Check ElevenLabs API key and quota
   - Verify audio output device
   - Check system volume settings

4. **High CPU Usage**
   - Adjust VAD sensitivity
   - Increase chunk duration
   - Use lighter Whisper model

### Debug Mode

Enable debug logging for detailed diagnostics:

```bash
python main.py --log-level DEBUG start
```

Check logs in `tonny_integration.log` for detailed error information.

## Performance Optimization

### Recommended Settings

- **VAD Aggressiveness**: 2 (balanced)
- **Chunk Duration**: 30ms
- **Whisper Model**: "base" for low latency
- **Cache Size**: 100MB for TTS audio
- **Queue Size**: 1000 items max

### Scaling Considerations

- Use RabbitMQ for distributed processing
- Enable caching for frequently used TTS
- Use connection pooling for database
- Implement rate limiting for API calls
- Monitor circuit breaker statistics

## Contributing

1. Follow existing code structure
2. Add tests for new features
3. Update documentation
4. Run tests before submitting
5. Use type hints and docstrings

## License

See main TonnyTray license file.