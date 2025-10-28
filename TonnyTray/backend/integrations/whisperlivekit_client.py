"""
WhisperLiveKit Integration

WebSocket client for WhisperLiveKit transcription service with
health monitoring and auto-reconnection.
"""
import asyncio
import json
import time
import struct
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field
from enum import Enum
import logging
import websockets
import aiohttp
import numpy as np

from ..utils.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

logger = logging.getLogger(__name__)


class TranscriptionState(Enum):
    """Transcription states"""
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    ERROR = "error"


class TranscriptionType(Enum):
    """Transcription types"""
    PARTIAL = "partial"
    FINAL = "final"


@dataclass
class WhisperConfig:
    """WhisperLiveKit configuration"""
    websocket_url: str = "ws://localhost:9090"
    http_url: str = "http://localhost:9090"
    sample_rate: int = 16000
    chunk_size: int = 1024
    language: str = "en"
    model: str = "base"
    vad_enabled: bool = True
    vad_threshold: float = 0.5
    reconnect_interval: float = 5.0
    health_check_interval: float = 30.0
    max_reconnect_attempts: int = 10


@dataclass
class Transcription:
    """Transcription result"""
    text: str
    type: TranscriptionType
    confidence: float
    timestamp: float = field(default_factory=time.time)
    language: Optional[str] = None
    segments: Optional[List[Dict[str, Any]]] = None
    audio_duration: Optional[float] = None


@dataclass
class AudioChunk:
    """Audio chunk for processing"""
    data: bytes
    sample_rate: int
    timestamp: float = field(default_factory=time.time)
    is_speech: Optional[bool] = None


class WhisperLiveKitClient:
    """
    WhisperLiveKit integration client

    Features:
    - WebSocket streaming transcription
    - Health monitoring
    - Auto-reconnection
    - Voice Activity Detection
    - Confidence scoring
    - Error recovery
    """

    def __init__(self, config: WhisperConfig):
        self.config = config
        self.state = TranscriptionState.IDLE
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.session: Optional[aiohttp.ClientSession] = None

        # Circuit breaker
        self.circuit_breaker = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=3,
                recovery_timeout=30.0,
                expected_exception=Exception,
                name="whisper_circuit"
            )
        )

        # Connection management
        self._connected = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._audio_stream_task: Optional[asyncio.Task] = None
        self._reconnect_attempts = 0

        # Audio buffering
        self._audio_buffer: List[AudioChunk] = []
        self._buffer_lock = asyncio.Lock()

        # Transcription handling
        self._transcription_handlers: List[Callable[[Transcription], None]] = []
        self._current_transcription = ""
        self._transcription_history: List[Transcription] = []

        # Statistics
        self.stats = {
            "total_audio_processed": 0,
            "total_transcriptions": 0,
            "connection_errors": 0,
            "last_health_check": None,
            "average_confidence": 0.0
        }

    async def connect(self) -> bool:
        """
        Connect to WhisperLiveKit service

        Returns:
            True if connected successfully
        """
        if self._connected:
            logger.debug("Already connected to WhisperLiveKit")
            return True

        logger.info("Connecting to WhisperLiveKit...")

        try:
            # Create HTTP session for health checks
            if not self.session:
                self.session = aiohttp.ClientSession()

            # Test HTTP endpoint first
            if not await self.health_check():
                logger.error("WhisperLiveKit service is not healthy")
                return False

            # Connect WebSocket
            await self._connect_websocket()

            # Start background tasks
            self._start_background_tasks()

            self._connected = True
            self._reconnect_attempts = 0
            return True

        except Exception as e:
            logger.error(f"Failed to connect to WhisperLiveKit: {e}")
            self._schedule_reconnect()
            return False

    async def _connect_websocket(self):
        """Establish WebSocket connection"""
        try:
            self.ws = await websockets.connect(
                self.config.websocket_url,
                ping_interval=30,
                ping_timeout=10
            )

            # Send initial configuration
            await self._send_config()

            self.state = TranscriptionState.LISTENING
            logger.info("WebSocket connected to WhisperLiveKit")

        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            raise

    async def _send_config(self):
        """Send configuration to WhisperLiveKit"""
        config_message = {
            "type": "config",
            "sample_rate": self.config.sample_rate,
            "language": self.config.language,
            "model": self.config.model,
            "vad_enabled": self.config.vad_enabled,
            "vad_threshold": self.config.vad_threshold
        }

        await self.ws.send(json.dumps(config_message))
        logger.debug(f"Sent configuration: {config_message}")

    def _start_background_tasks(self):
        """Start background tasks"""
        if self._health_check_task:
            self._health_check_task.cancel()
        if self._audio_stream_task:
            self._audio_stream_task.cancel()

        self._health_check_task = asyncio.create_task(self._health_check_loop())
        self._audio_stream_task = asyncio.create_task(self._audio_stream_loop())

    async def _health_check_loop(self):
        """Periodic health check"""
        while self._connected:
            try:
                await asyncio.sleep(self.config.health_check_interval)

                if not await self.health_check():
                    logger.warning("Health check failed")
                    await self._handle_disconnect()
                    break

            except Exception as e:
                logger.error(f"Health check error: {e}")

    async def _audio_stream_loop(self):
        """Handle WebSocket messages"""
        while self._connected and self.ws:
            try:
                message = await self.ws.recv()
                await self._handle_message(message)

            except websockets.ConnectionClosed:
                logger.warning("WebSocket connection closed")
                await self._handle_disconnect()
                break

            except Exception as e:
                logger.error(f"Error in audio stream loop: {e}")
                self.stats["connection_errors"] += 1

    async def _handle_message(self, message: str):
        """Process incoming WebSocket message"""
        try:
            data = json.loads(message)
            message_type = data.get("type", "unknown")

            if message_type == "transcription":
                await self._handle_transcription(data)
            elif message_type == "error":
                await self._handle_error(data)
            elif message_type == "status":
                await self._handle_status(data)
            else:
                logger.debug(f"Unknown message type: {message_type}")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON message: {e}")

    async def _handle_transcription(self, data: Dict[str, Any]):
        """Handle transcription message"""
        text = data.get("text", "")
        is_final = data.get("final", False)
        confidence = data.get("confidence", 0.0)
        segments = data.get("segments", [])

        transcription = Transcription(
            text=text,
            type=TranscriptionType.FINAL if is_final else TranscriptionType.PARTIAL,
            confidence=confidence,
            language=data.get("language"),
            segments=segments,
            audio_duration=data.get("audio_duration")
        )

        # Update statistics
        self.stats["total_transcriptions"] += 1
        if confidence > 0:
            # Update running average confidence
            avg = self.stats["average_confidence"]
            count = self.stats["total_transcriptions"]
            self.stats["average_confidence"] = (avg * (count - 1) + confidence) / count

        # Store transcription
        if is_final:
            self._transcription_history.append(transcription)
            self._current_transcription = ""
        else:
            self._current_transcription = text

        # Call handlers
        for handler in self._transcription_handlers:
            try:
                await handler(transcription)
            except Exception as e:
                logger.error(f"Error in transcription handler: {e}")

        logger.debug(f"Transcription ({'final' if is_final else 'partial'}): {text[:50]}...")

    async def _handle_error(self, data: Dict[str, Any]):
        """Handle error message"""
        error_message = data.get("message", "Unknown error")
        error_code = data.get("code", "UNKNOWN")

        logger.error(f"WhisperLiveKit error [{error_code}]: {error_message}")
        self.state = TranscriptionState.ERROR

    async def _handle_status(self, data: Dict[str, Any]):
        """Handle status message"""
        status = data.get("status", "unknown")
        logger.debug(f"WhisperLiveKit status: {status}")

    async def _handle_disconnect(self):
        """Handle disconnection"""
        self._connected = False
        self.state = TranscriptionState.IDLE

        # Cancel background tasks
        for task in [self._health_check_task, self._audio_stream_task]:
            if task:
                task.cancel()

        # Close WebSocket
        if self.ws:
            await self.ws.close()
            self.ws = None

        logger.warning("Disconnected from WhisperLiveKit")
        self._schedule_reconnect()

    def _schedule_reconnect(self):
        """Schedule reconnection attempt"""
        if self._reconnect_task:
            return

        if self._reconnect_attempts >= self.config.max_reconnect_attempts:
            logger.error("Max reconnection attempts reached")
            self.state = TranscriptionState.ERROR
            return

        self._reconnect_attempts += 1
        delay = self.config.reconnect_interval * self._reconnect_attempts

        logger.info(f"Scheduling reconnection in {delay} seconds (attempt {self._reconnect_attempts})")
        self._reconnect_task = asyncio.create_task(self._reconnect(delay))

    async def _reconnect(self, delay: float):
        """Attempt reconnection"""
        await asyncio.sleep(delay)

        try:
            await self.connect()
            self._reconnect_task = None

            # Process buffered audio
            await self._process_audio_buffer()

        except Exception as e:
            logger.error(f"Reconnection failed: {e}")
            self._schedule_reconnect()

    async def send_audio(self, audio_data: bytes, sample_rate: Optional[int] = None):
        """
        Send audio data for transcription

        Args:
            audio_data: Raw audio bytes (PCM format)
            sample_rate: Sample rate (uses config default if not specified)
        """
        if not self._connected:
            # Buffer audio while disconnected
            chunk = AudioChunk(
                data=audio_data,
                sample_rate=sample_rate or self.config.sample_rate
            )
            async with self._buffer_lock:
                self._audio_buffer.append(chunk)

            # Try to reconnect
            if not self._reconnect_task:
                self._schedule_reconnect()
            return

        try:
            # Send audio via WebSocket
            await self.circuit_breaker.call(
                self._send_audio_impl,
                audio_data,
                sample_rate
            )

            self.stats["total_audio_processed"] += len(audio_data)

        except Exception as e:
            logger.error(f"Failed to send audio: {e}")
            # Buffer for retry
            chunk = AudioChunk(
                data=audio_data,
                sample_rate=sample_rate or self.config.sample_rate
            )
            async with self._buffer_lock:
                self._audio_buffer.append(chunk)

    async def _send_audio_impl(self, audio_data: bytes, sample_rate: Optional[int]):
        """Implementation of audio sending"""
        if not self.ws or self.ws.closed:
            raise ConnectionError("WebSocket not connected")

        # Prepare audio message
        message = {
            "type": "audio",
            "sample_rate": sample_rate or self.config.sample_rate,
            "format": "pcm16",
            "data": audio_data.hex()  # Convert to hex for JSON
        }

        await self.ws.send(json.dumps(message))

    async def _process_audio_buffer(self):
        """Process buffered audio after reconnection"""
        async with self._buffer_lock:
            if not self._audio_buffer:
                return

            logger.info(f"Processing {len(self._audio_buffer)} buffered audio chunks")

            for chunk in self._audio_buffer:
                try:
                    await self._send_audio_impl(chunk.data, chunk.sample_rate)
                    await asyncio.sleep(0.01)  # Small delay between chunks
                except Exception as e:
                    logger.error(f"Failed to process buffered audio: {e}")
                    break

            self._audio_buffer.clear()

    async def stream_audio_from_microphone(
        self,
        device_index: Optional[int] = None,
        chunk_duration: float = 0.1
    ):
        """
        Stream audio directly from microphone

        Args:
            device_index: Microphone device index
            chunk_duration: Duration of each audio chunk in seconds
        """
        try:
            import sounddevice as sd
        except ImportError:
            logger.error("sounddevice not installed, microphone streaming unavailable")
            return

        chunk_samples = int(self.config.sample_rate * chunk_duration)

        def audio_callback(indata, frames, time_info, status):
            """Callback for audio stream"""
            if status:
                logger.warning(f"Audio stream status: {status}")

            # Convert to bytes
            audio_bytes = (indata * 32767).astype(np.int16).tobytes()

            # Send to WhisperLiveKit
            asyncio.create_task(self.send_audio(audio_bytes))

        # Start audio stream
        with sd.InputStream(
            device=device_index,
            channels=1,
            samplerate=self.config.sample_rate,
            blocksize=chunk_samples,
            callback=audio_callback
        ):
            logger.info(f"Streaming from microphone (device: {device_index})")

            # Keep streaming until stopped
            while self.state == TranscriptionState.LISTENING:
                await asyncio.sleep(0.1)

    def register_transcription_handler(self, handler: Callable[[Transcription], None]):
        """Register handler for transcription events"""
        self._transcription_handlers.append(handler)

    def unregister_transcription_handler(self, handler: Callable[[Transcription], None]):
        """Unregister transcription handler"""
        if handler in self._transcription_handlers:
            self._transcription_handlers.remove(handler)

    async def health_check(self) -> bool:
        """
        Check WhisperLiveKit service health

        Returns:
            True if service is healthy
        """
        try:
            async with self.session.get(
                f"{self.config.http_url}/health",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    self.stats["last_health_check"] = time.time()

                    # Log health status
                    logger.debug(f"Health check: {data}")

                    return data.get("status") == "healthy"

                return False

        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    async def get_models(self) -> List[str]:
        """Get available Whisper models"""
        try:
            async with self.session.get(
                f"{self.config.http_url}/models",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("models", [])

                return []

        except Exception as e:
            logger.error(f"Failed to get models: {e}")
            return []

    async def set_model(self, model: str):
        """Change Whisper model"""
        self.config.model = model

        if self._connected and self.ws:
            # Update configuration on connected session
            await self._send_config()

    async def set_language(self, language: str):
        """Change transcription language"""
        self.config.language = language

        if self._connected and self.ws:
            # Update configuration on connected session
            await self._send_config()

    def get_transcription_history(self, limit: int = 10) -> List[Transcription]:
        """Get recent transcription history"""
        return self._transcription_history[-limit:]

    def get_current_transcription(self) -> str:
        """Get current partial transcription"""
        return self._current_transcription

    def clear_history(self):
        """Clear transcription history"""
        self._transcription_history.clear()
        self._current_transcription = ""

    def get_statistics(self) -> Dict[str, Any]:
        """Get client statistics"""
        return {
            **self.stats,
            "connected": self._connected,
            "state": self.state.value,
            "reconnect_attempts": self._reconnect_attempts,
            "buffer_size": len(self._audio_buffer),
            "history_size": len(self._transcription_history),
            "circuit_breaker": self.circuit_breaker.get_stats()
        }

    async def close(self):
        """Close connection and cleanup"""
        logger.info("Closing WhisperLiveKit client...")

        self._connected = False

        # Cancel all tasks
        for task in [self._health_check_task, self._audio_stream_task, self._reconnect_task]:
            if task:
                task.cancel()

        # Close WebSocket
        if self.ws:
            await self.ws.close()

        # Close HTTP session
        if self.session:
            await self.session.close()

        self.state = TranscriptionState.IDLE
        logger.info("WhisperLiveKit client closed")