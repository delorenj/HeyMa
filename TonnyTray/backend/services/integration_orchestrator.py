"""
Integration Orchestrator

Main service that coordinates all integrations for the end-to-end flow.
"""
import asyncio
import time
import sqlite3
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, asdict
from enum import Enum
import logging
import json
from pathlib import Path
import uuid

from ..integrations.n8n_client import N8nClient, N8nConfig
from ..integrations.elevenlabs_client import ElevenLabsClient
from ..integrations.whisperlivekit_client import (
    WhisperLiveKitClient,
    WhisperConfig,
    Transcription,
    TranscriptionType
)
from ..integrations.rabbitmq_client import RabbitMQClient, RabbitMQConfig, EventPriority
from ..services.audio_pipeline import AudioPipeline, AudioConfig, AudioMode
from ..utils.offline_queue import OfflineQueue

logger = logging.getLogger(__name__)


class SystemState(Enum):
    """System states"""
    INITIALIZING = "initializing"
    READY = "ready"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    ERROR = "error"
    SHUTTING_DOWN = "shutting_down"


@dataclass
class IntegrationConfig:
    """Complete integration configuration"""
    # Database
    database_path: str = "tonny_integration.db"

    # n8n
    n8n_webhook_url: str = "http://localhost:5678/webhook/tonny"
    n8n_websocket_url: Optional[str] = None
    n8n_api_key: Optional[str] = None
    n8n_webhook_secret: Optional[str] = None

    # ElevenLabs
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_cache_dir: str = "elevenlabs_cache"

    # WhisperLiveKit
    whisper_websocket_url: str = "ws://localhost:9090"
    whisper_http_url: str = "http://localhost:9090"
    whisper_model: str = "base"
    whisper_language: str = "en"

    # RabbitMQ (Optional)
    rabbitmq_enabled: bool = False
    rabbitmq_url: str = "amqp://guest:guest@localhost/"

    # Audio
    audio_mode: AudioMode = AudioMode.VOICE_ACTIVATION
    audio_input_device: Optional[int] = None
    audio_output_device: Optional[int] = None

    # System
    enable_logging: bool = True
    log_level: str = "INFO"
    health_check_interval: float = 30.0


@dataclass
class ConversationEntry:
    """Database conversation entry"""
    id: Optional[int] = None
    session_id: str = ""
    timestamp: float = 0
    type: str = ""  # transcription, response, tts, error
    content: str = ""
    metadata: Optional[Dict[str, Any]] = None


class IntegrationOrchestrator:
    """
    Main orchestrator for all integrations

    Manages the complete flow:
    1. Audio capture → WhisperLiveKit transcription
    2. Transcription → n8n processing
    3. n8n response → ElevenLabs TTS
    4. TTS audio → Playback
    5. All events → Database + RabbitMQ (optional)
    """

    def __init__(self, config: IntegrationConfig):
        self.config = config
        self.state = SystemState.INITIALIZING
        self.session_id = str(uuid.uuid4())
        self.start_time = time.time()

        # Initialize components
        self._init_database()

        # Integration clients
        self.n8n_client: Optional[N8nClient] = None
        self.elevenlabs_client: Optional[ElevenLabsClient] = None
        self.whisper_client: Optional[WhisperLiveKitClient] = None
        self.rabbitmq_client: Optional[RabbitMQClient] = None

        # Audio pipeline
        self.audio_pipeline: Optional[AudioPipeline] = None

        # Background tasks
        self._health_check_task: Optional[asyncio.Task] = None
        self._offline_queue_task: Optional[asyncio.Task] = None

        # Statistics
        self.stats = {
            "transcriptions_processed": 0,
            "n8n_requests_sent": 0,
            "tts_generated": 0,
            "errors": 0,
            "uptime": 0
        }

        # Current conversation context
        self._current_transcription = ""
        self._conversation_history: List[ConversationEntry] = []

    def _init_database(self):
        """Initialize SQLite database"""
        db_path = Path(self.config.database_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(db_path) as conn:
            # Create conversations table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create metrics table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    metric_name TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    metadata TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create indices
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_session
                ON conversations(session_id, timestamp)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_metrics_timestamp
                ON metrics(timestamp, metric_name)
            """)

            conn.commit()

        logger.info(f"Database initialized at {db_path}")

    async def initialize(self):
        """Initialize all integrations"""
        logger.info("Initializing integration orchestrator...")

        try:
            # Initialize n8n client
            n8n_config = N8nConfig(
                webhook_url=self.config.n8n_webhook_url,
                websocket_url=self.config.n8n_websocket_url,
                api_key=self.config.n8n_api_key,
                webhook_secret=self.config.n8n_webhook_secret,
                enable_offline_queue=True
            )
            self.n8n_client = N8nClient(n8n_config)
            await self.n8n_client.connect()

            # Initialize ElevenLabs client
            if self.config.elevenlabs_api_key:
                self.elevenlabs_client = ElevenLabsClient(
                    api_key=self.config.elevenlabs_api_key,
                    cache_dir=self.config.elevenlabs_cache_dir
                )
                await self.elevenlabs_client.initialize()
            else:
                logger.warning("ElevenLabs API key not configured, TTS disabled")

            # Initialize WhisperLiveKit client
            whisper_config = WhisperConfig(
                websocket_url=self.config.whisper_websocket_url,
                http_url=self.config.whisper_http_url,
                model=self.config.whisper_model,
                language=self.config.whisper_language
            )
            self.whisper_client = WhisperLiveKitClient(whisper_config)
            self.whisper_client.register_transcription_handler(
                self._handle_transcription
            )
            await self.whisper_client.connect()

            # Initialize RabbitMQ client (optional)
            if self.config.rabbitmq_enabled:
                rabbitmq_config = RabbitMQConfig(
                    url=self.config.rabbitmq_url,
                    enabled=True
                )
                self.rabbitmq_client = RabbitMQClient(rabbitmq_config)
                await self.rabbitmq_client.connect()

            # Initialize audio pipeline
            audio_config = AudioConfig(
                mode=self.config.audio_mode,
                input_device=self.config.audio_input_device,
                output_device=self.config.audio_output_device,
                audio_level_callback=self._handle_audio_level
            )
            self.audio_pipeline = AudioPipeline(audio_config)
            self.audio_pipeline.set_audio_callback(self._handle_audio_data)
            self.audio_pipeline.set_speech_callbacks(
                start_callback=self._handle_speech_start,
                end_callback=self._handle_speech_end
            )

            # Start background tasks
            self._health_check_task = asyncio.create_task(self._health_check_loop())

            # Start offline queue processor
            if self.n8n_client and self.n8n_client.offline_queue:
                self._offline_queue_task = asyncio.create_task(
                    self.n8n_client.offline_queue.start_background_processor()
                )

            self.state = SystemState.READY
            logger.info("Integration orchestrator initialized successfully")

            # Log initial status
            await self._log_system_status()

        except Exception as e:
            logger.error(f"Failed to initialize orchestrator: {e}")
            self.state = SystemState.ERROR
            self.stats["errors"] += 1
            raise

    async def start_listening(self):
        """Start listening for audio input"""
        if self.state != SystemState.READY:
            logger.warning(f"Cannot start listening in state: {self.state}")
            return

        logger.info("Starting audio listening...")
        self.state = SystemState.LISTENING

        # Start audio pipeline
        await self.audio_pipeline.start()

        # Log event
        await self._log_conversation_entry(
            "system",
            "Started listening",
            {"mode": self.config.audio_mode.value}
        )

    async def stop_listening(self):
        """Stop listening for audio"""
        logger.info("Stopping audio listening...")
        self.state = SystemState.READY

        # Stop audio pipeline
        await self.audio_pipeline.stop()

        # Log event
        await self._log_conversation_entry(
            "system",
            "Stopped listening"
        )

    async def _handle_audio_data(self, audio_data: bytes):
        """Handle raw audio data from pipeline"""
        # Send to WhisperLiveKit for transcription
        if self.whisper_client:
            await self.whisper_client.send_audio(audio_data)

    async def _handle_audio_level(self, level: float):
        """Handle audio level updates"""
        # Could update UI or trigger visualizations
        pass

    async def _handle_speech_start(self):
        """Handle speech start event"""
        logger.debug("Speech started")
        self.state = SystemState.LISTENING

        # Publish event if RabbitMQ enabled
        if self.rabbitmq_client:
            await self.rabbitmq_client.publish_event(
                "thread.tonny.speech_start",
                {"session_id": self.session_id, "timestamp": time.time()}
            )

    async def _handle_speech_end(self):
        """Handle speech end event"""
        logger.debug("Speech ended")

        # Publish event if RabbitMQ enabled
        if self.rabbitmq_client:
            await self.rabbitmq_client.publish_event(
                "thread.tonny.speech_end",
                {"session_id": self.session_id, "timestamp": time.time()}
            )

    async def _handle_transcription(self, transcription: Transcription):
        """Handle transcription from WhisperLiveKit"""
        self.stats["transcriptions_processed"] += 1

        # Log transcription
        await self._log_conversation_entry(
            "transcription",
            transcription.text,
            {
                "type": transcription.type.value,
                "confidence": transcription.confidence,
                "language": transcription.language
            }
        )

        # Handle based on type
        if transcription.type == TranscriptionType.PARTIAL:
            # Update current transcription
            self._current_transcription = transcription.text
            logger.debug(f"Partial: {transcription.text[:50]}...")

        elif transcription.type == TranscriptionType.FINAL:
            # Process final transcription
            logger.info(f"Final transcription: {transcription.text}")
            self.state = SystemState.PROCESSING

            # Send to n8n for processing
            await self._process_with_n8n(transcription.text)

            # Clear current transcription
            self._current_transcription = ""

    async def _process_with_n8n(self, text: str):
        """Process transcription with n8n"""
        try:
            # Prepare request
            request_data = {
                "session_id": self.session_id,
                "text": text,
                "timestamp": time.time(),
                "context": self._get_conversation_context()
            }

            # Send to n8n
            response = await self.n8n_client.send_request(
                "process_prompt",
                request_data
            )

            self.stats["n8n_requests_sent"] += 1

            # Log response
            await self._log_conversation_entry(
                "response",
                response.data.get("text", ""),
                {"success": response.success, "processing_time": response.processing_time}
            )

            # Process response
            if response.success and response.data:
                response_text = response.data.get("text", "")

                if response_text:
                    # Generate TTS
                    await self._generate_tts(response_text)

                # Handle any commands in response
                commands = response.data.get("commands", [])
                for command in commands:
                    await self._handle_command(command)

        except Exception as e:
            logger.error(f"n8n processing failed: {e}")
            self.stats["errors"] += 1

            # Log error
            await self._log_conversation_entry(
                "error",
                str(e),
                {"stage": "n8n_processing"}
            )

    async def _generate_tts(self, text: str):
        """Generate and play TTS audio"""
        if not self.elevenlabs_client:
            logger.warning("ElevenLabs not configured, skipping TTS")
            return

        try:
            self.state = SystemState.SPEAKING

            # Add to TTS queue
            queue_id = await self.elevenlabs_client.add_to_queue(
                text,
                self.config.elevenlabs_voice_id or "default"
            )

            self.stats["tts_generated"] += 1

            # Log TTS generation
            await self._log_conversation_entry(
                "tts",
                text,
                {"queue_id": queue_id, "voice_id": self.config.elevenlabs_voice_id}
            )

            # Wait for playback to complete (simplified)
            while self.elevenlabs_client.playback_queue:
                await asyncio.sleep(0.1)

            self.state = SystemState.LISTENING

        except Exception as e:
            logger.error(f"TTS generation failed: {e}")
            self.stats["errors"] += 1
            self.state = SystemState.LISTENING

    async def _handle_command(self, command: Dict[str, Any]):
        """Handle system command from n8n"""
        cmd_type = command.get("type")
        cmd_data = command.get("data", {})

        logger.info(f"Handling command: {cmd_type}")

        if cmd_type == "set_mode":
            mode = AudioMode(cmd_data.get("mode", "voice_activation"))
            self.audio_pipeline.set_mode(mode)

        elif cmd_type == "set_language":
            language = cmd_data.get("language", "en")
            await self.whisper_client.set_language(language)

        elif cmd_type == "clear_history":
            self._conversation_history.clear()
            self.whisper_client.clear_history()

        else:
            logger.warning(f"Unknown command type: {cmd_type}")

    def _get_conversation_context(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Get recent conversation context"""
        context = []

        for entry in self._conversation_history[-limit:]:
            if entry.type in ["transcription", "response"]:
                context.append({
                    "type": entry.type,
                    "content": entry.content,
                    "timestamp": entry.timestamp
                })

        return context

    async def _log_conversation_entry(
        self,
        entry_type: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """Log conversation entry to database"""
        entry = ConversationEntry(
            session_id=self.session_id,
            timestamp=time.time(),
            type=entry_type,
            content=content,
            metadata=metadata
        )

        # Add to memory
        self._conversation_history.append(entry)

        # Save to database
        try:
            with sqlite3.connect(self.config.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO conversations
                    (session_id, timestamp, type, content, metadata)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        entry.session_id,
                        entry.timestamp,
                        entry.type,
                        entry.content,
                        json.dumps(entry.metadata) if entry.metadata else None
                    )
                )
                conn.commit()

        except Exception as e:
            logger.error(f"Failed to log conversation entry: {e}")

        # Publish to RabbitMQ if enabled
        if self.rabbitmq_client:
            await self.rabbitmq_client.publish_event(
                f"thread.tonny.conversation.{entry_type}",
                asdict(entry),
                priority=EventPriority.NORMAL
            )

    async def _log_metric(
        self,
        metric_name: str,
        metric_value: float,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """Log metric to database"""
        try:
            with sqlite3.connect(self.config.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO metrics
                    (timestamp, metric_name, metric_value, metadata)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        time.time(),
                        metric_name,
                        metric_value,
                        json.dumps(metadata) if metadata else None
                    )
                )
                conn.commit()

        except Exception as e:
            logger.error(f"Failed to log metric: {e}")

    async def _health_check_loop(self):
        """Periodic health check of all integrations"""
        while self.state not in [SystemState.ERROR, SystemState.SHUTTING_DOWN]:
            try:
                await asyncio.sleep(self.config.health_check_interval)

                # Check all integrations
                health_status = await self.get_health_status()

                # Log metrics
                for integration, status in health_status.items():
                    await self._log_metric(
                        f"health.{integration}",
                        1.0 if status.get("healthy", False) else 0.0,
                        status
                    )

                # Update system state if unhealthy
                if not all(s.get("healthy", False) for s in health_status.values()):
                    logger.warning("Some integrations are unhealthy")

            except Exception as e:
                logger.error(f"Health check error: {e}")

    async def get_health_status(self) -> Dict[str, Dict[str, Any]]:
        """Get health status of all integrations"""
        status = {
            "system": {
                "healthy": self.state not in [SystemState.ERROR, SystemState.SHUTTING_DOWN],
                "state": self.state.value,
                "uptime": time.time() - self.start_time,
                "stats": self.stats
            }
        }

        # Check n8n
        if self.n8n_client:
            status["n8n"] = await self.n8n_client.test_connection()
            status["n8n"]["healthy"] = status["n8n"].get("websocket", {}).get("connected", False) or \
                                      status["n8n"].get("webhook", {}).get("reachable", False)

        # Check WhisperLiveKit
        if self.whisper_client:
            whisper_healthy = await self.whisper_client.health_check()
            status["whisper"] = {
                "healthy": whisper_healthy,
                "stats": self.whisper_client.get_statistics()
            }

        # Check ElevenLabs
        if self.elevenlabs_client:
            try:
                user_info = await self.elevenlabs_client.get_user_info()
                status["elevenlabs"] = {
                    "healthy": True,
                    "quota": self.elevenlabs_client.quota_info
                }
            except:
                status["elevenlabs"] = {"healthy": False}

        # Check RabbitMQ
        if self.rabbitmq_client:
            status["rabbitmq"] = await self.rabbitmq_client.test_connection()

        # Check audio pipeline
        if self.audio_pipeline:
            status["audio"] = {
                "healthy": True,
                "metrics": self.audio_pipeline.get_metrics()
            }

        return status

    async def _log_system_status(self):
        """Log complete system status"""
        status = await self.get_health_status()
        logger.info(f"System Status: {json.dumps(status, indent=2)}")

        # Log to database
        await self._log_conversation_entry(
            "system_status",
            json.dumps(status),
            {"session_id": self.session_id}
        )

    async def test_integrations(self) -> Dict[str, Any]:
        """Run integration tests"""
        logger.info("Running integration tests...")
        results = {}

        # Test n8n
        if self.n8n_client:
            try:
                test_response = await self.n8n_client.send_request(
                    "test",
                    {"message": "Integration test"}
                )
                results["n8n"] = {
                    "success": test_response.success,
                    "details": await self.n8n_client.test_connection()
                }
            except Exception as e:
                results["n8n"] = {"success": False, "error": str(e)}

        # Test WhisperLiveKit
        if self.whisper_client:
            results["whisper"] = {
                "success": await self.whisper_client.health_check(),
                "models": await self.whisper_client.get_models()
            }

        # Test ElevenLabs
        if self.elevenlabs_client:
            try:
                voices = await self.elevenlabs_client.get_voices()
                results["elevenlabs"] = {
                    "success": True,
                    "voices_count": len(voices),
                    "quota": self.elevenlabs_client.quota_info
                }
            except Exception as e:
                results["elevenlabs"] = {"success": False, "error": str(e)}

        # Test RabbitMQ
        if self.rabbitmq_client:
            results["rabbitmq"] = await self.rabbitmq_client.test_connection()

        # Test audio devices
        if self.audio_pipeline:
            results["audio"] = {
                "success": True,
                "devices": self.audio_pipeline.get_audio_devices()
            }

        logger.info(f"Integration test results: {json.dumps(results, indent=2)}")
        return results

    async def shutdown(self):
        """Shutdown orchestrator and all integrations"""
        logger.info("Shutting down integration orchestrator...")
        self.state = SystemState.SHUTTING_DOWN

        # Stop audio pipeline
        if self.audio_pipeline:
            await self.audio_pipeline.stop()

        # Cancel background tasks
        for task in [self._health_check_task, self._offline_queue_task]:
            if task:
                task.cancel()

        # Close all clients
        if self.n8n_client:
            await self.n8n_client.close()

        if self.whisper_client:
            await self.whisper_client.close()

        if self.elevenlabs_client:
            await self.elevenlabs_client.close()

        if self.rabbitmq_client:
            await self.rabbitmq_client.close()

        # Final status log
        await self._log_conversation_entry(
            "system",
            "Shutdown complete",
            {"session_id": self.session_id, "stats": self.stats}
        )

        logger.info("Integration orchestrator shutdown complete")