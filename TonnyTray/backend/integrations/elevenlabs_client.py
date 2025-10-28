"""
ElevenLabs Text-to-Speech Integration

Provides TTS capabilities with streaming support, voice selection,
and audio playback management.
"""
import asyncio
import aiohttp
import json
import time
import io
import os
from typing import Optional, Dict, Any, List, AsyncGenerator, Tuple
from dataclasses import dataclass, field
from enum import Enum
import logging
from pathlib import Path
import hashlib
import wave
import struct

# Audio playback libraries
try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False
    logging.warning("pyaudio not available, audio playback disabled")

try:
    import sounddevice as sd
    import soundfile as sf
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    SOUNDDEVICE_AVAILABLE = False
    logging.warning("sounddevice not available, fallback audio disabled")

from ..utils.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

logger = logging.getLogger(__name__)


class AudioFormat(Enum):
    """Supported audio formats"""
    MP3_44100_128 = "mp3_44100_128"
    MP3_44100_64 = "mp3_44100_64"
    PCM_16000 = "pcm_16000"
    PCM_22050 = "pcm_22050"
    PCM_24000 = "pcm_24000"
    PCM_44100 = "pcm_44100"


class PlaybackState(Enum):
    """Audio playback states"""
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass
class Voice:
    """Voice model information"""
    voice_id: str
    name: str
    category: Optional[str] = None
    description: Optional[str] = None
    preview_url: Optional[str] = None
    settings: Optional[Dict[str, float]] = None


@dataclass
class TTSRequest:
    """TTS request parameters"""
    text: str
    voice_id: str
    model_id: str = "eleven_monolingual_v1"
    voice_settings: Optional[Dict[str, float]] = None
    output_format: AudioFormat = AudioFormat.MP3_44100_128
    optimize_streaming_latency: int = 0
    stream: bool = False


@dataclass
class AudioQueueItem:
    """Audio playback queue item"""
    id: str
    audio_data: bytes
    text: str
    voice_id: str
    format: AudioFormat
    timestamp: float = field(default_factory=time.time)
    cached_path: Optional[str] = None


class ElevenLabsClient:
    """
    ElevenLabs TTS client with streaming and caching support

    Features:
    - Voice listing and selection
    - Streaming TTS generation
    - Audio playback queue
    - Response caching
    - Volume/speed/pitch controls
    - Quota limit handling
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.elevenlabs.io/v1",
        cache_dir: Optional[str] = None,
        enable_cache: bool = True
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.cache_dir = Path(cache_dir or "elevenlabs_cache")
        self.enable_cache = enable_cache

        # Create cache directory
        if enable_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        # HTTP session
        self.session: Optional[aiohttp.ClientSession] = None

        # Circuit breaker for API calls
        self.circuit_breaker = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=3,
                recovery_timeout=60.0,
                expected_exception=Exception,
                name="elevenlabs_circuit"
            )
        )

        # Audio playback
        self.playback_queue: List[AudioQueueItem] = []
        self.current_playback: Optional[AudioQueueItem] = None
        self.playback_state = PlaybackState.IDLE
        self._playback_task: Optional[asyncio.Task] = None
        self._audio_stream = None

        # Voice cache
        self._voices_cache: Optional[List[Voice]] = None
        self._voices_cache_time: float = 0
        self._voices_cache_ttl: float = 3600  # 1 hour

        # Settings
        self.default_voice_settings = {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True
        }
        self.playback_settings = {
            "volume": 1.0,
            "speed": 1.0,
            "pitch": 1.0
        }

        # Quota tracking
        self.quota_info: Dict[str, Any] = {}

    async def initialize(self):
        """Initialize the client"""
        if not self.session:
            self.session = aiohttp.ClientSession(
                headers={
                    "xi-api-key": self.api_key,
                    "Content-Type": "application/json"
                }
            )

        # Test connection and get initial quota
        await self.get_user_info()

    async def get_voices(self, use_cache: bool = True) -> List[Voice]:
        """
        Get available voices

        Args:
            use_cache: Use cached voice list if available

        Returns:
            List of available voices
        """
        # Check cache
        if use_cache and self._voices_cache:
            if time.time() - self._voices_cache_time < self._voices_cache_ttl:
                return self._voices_cache

        try:
            response = await self.circuit_breaker.call(
                self._api_request,
                "GET",
                "/voices"
            )

            voices = []
            for voice_data in response.get("voices", []):
                voice = Voice(
                    voice_id=voice_data["voice_id"],
                    name=voice_data["name"],
                    category=voice_data.get("category"),
                    description=voice_data.get("description"),
                    preview_url=voice_data.get("preview_url"),
                    settings=voice_data.get("settings")
                )
                voices.append(voice)

            # Update cache
            self._voices_cache = voices
            self._voices_cache_time = time.time()

            logger.info(f"Loaded {len(voices)} voices")
            return voices

        except Exception as e:
            logger.error(f"Failed to get voices: {e}")
            # Return cached voices if available
            if self._voices_cache:
                return self._voices_cache
            raise

    async def get_voice_by_name(self, name: str) -> Optional[Voice]:
        """Get voice by name"""
        voices = await self.get_voices()
        for voice in voices:
            if voice.name.lower() == name.lower():
                return voice
        return None

    async def text_to_speech(
        self,
        text: str,
        voice_id: str,
        voice_settings: Optional[Dict[str, float]] = None,
        model_id: str = "eleven_monolingual_v1",
        output_format: AudioFormat = AudioFormat.MP3_44100_128,
        stream: bool = False
    ) -> bytes:
        """
        Convert text to speech

        Args:
            text: Text to convert
            voice_id: Voice ID to use
            voice_settings: Voice settings override
            model_id: Model to use
            output_format: Output audio format
            stream: Enable streaming mode

        Returns:
            Audio data as bytes
        """
        # Check cache
        if self.enable_cache and not stream:
            cached_audio = self._get_cached_audio(text, voice_id, model_id)
            if cached_audio:
                logger.debug("Using cached audio")
                return cached_audio

        # Prepare request
        request = TTSRequest(
            text=text,
            voice_id=voice_id,
            model_id=model_id,
            voice_settings=voice_settings or self.default_voice_settings,
            output_format=output_format,
            stream=stream
        )

        try:
            if stream:
                # Streaming mode
                audio_data = b""
                async for chunk in self._stream_tts(request):
                    audio_data += chunk
                return audio_data
            else:
                # Non-streaming mode
                audio_data = await self.circuit_breaker.call(
                    self._generate_tts,
                    request
                )

                # Cache the result
                if self.enable_cache:
                    self._cache_audio(text, voice_id, model_id, audio_data)

                return audio_data

        except Exception as e:
            logger.error(f"TTS generation failed: {e}")
            raise

    async def _generate_tts(self, request: TTSRequest) -> bytes:
        """Generate TTS audio (non-streaming)"""
        url = f"/text-to-speech/{request.voice_id}"

        payload = {
            "text": request.text,
            "model_id": request.model_id,
            "voice_settings": request.voice_settings
        }

        # Add output format to URL params
        params = {"output_format": request.output_format.value}

        response = await self._api_request(
            "POST",
            url,
            json=payload,
            params=params,
            return_bytes=True
        )

        return response

    async def _stream_tts(
        self,
        request: TTSRequest
    ) -> AsyncGenerator[bytes, None]:
        """Stream TTS audio generation"""
        url = f"{self.base_url}/text-to-speech/{request.voice_id}/stream"

        payload = {
            "text": request.text,
            "model_id": request.model_id,
            "voice_settings": request.voice_settings,
            "optimize_streaming_latency": request.optimize_streaming_latency
        }

        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg"
        }

        async with self.session.post(
            url,
            json=payload,
            headers=headers,
            params={"output_format": request.output_format.value}
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"TTS streaming failed: {response.status} - {error_text}")

            async for chunk in response.content.iter_chunked(1024):
                yield chunk

    async def add_to_queue(
        self,
        text: str,
        voice_id: str,
        **kwargs
    ) -> str:
        """
        Add text to playback queue

        Args:
            text: Text to speak
            voice_id: Voice to use
            **kwargs: Additional TTS parameters

        Returns:
            Queue item ID
        """
        # Generate audio
        audio_data = await self.text_to_speech(text, voice_id, **kwargs)

        # Create queue item
        item_id = f"{time.time()}_{hashlib.md5(text.encode()).hexdigest()[:8]}"
        item = AudioQueueItem(
            id=item_id,
            audio_data=audio_data,
            text=text,
            voice_id=voice_id,
            format=kwargs.get("output_format", AudioFormat.MP3_44100_128)
        )

        self.playback_queue.append(item)
        logger.info(f"Added item {item_id} to playback queue")

        # Start playback if not already playing
        if self.playback_state == PlaybackState.IDLE:
            await self.start_playback()

        return item_id

    async def start_playback(self):
        """Start audio playback"""
        if self.playback_state == PlaybackState.PLAYING:
            logger.debug("Playback already in progress")
            return

        self.playback_state = PlaybackState.PLAYING

        if self._playback_task:
            self._playback_task.cancel()

        self._playback_task = asyncio.create_task(self._playback_loop())
        logger.info("Started audio playback")

    async def _playback_loop(self):
        """Main playback loop"""
        while self.playback_state == PlaybackState.PLAYING:
            if not self.playback_queue:
                self.playback_state = PlaybackState.IDLE
                logger.info("Playback queue empty, stopping")
                break

            # Get next item
            item = self.playback_queue.pop(0)
            self.current_playback = item

            try:
                # Play audio
                await self._play_audio(item)
            except Exception as e:
                logger.error(f"Failed to play audio {item.id}: {e}")

            self.current_playback = None

            # Small delay between items
            await asyncio.sleep(0.1)

    async def _play_audio(self, item: AudioQueueItem):
        """Play audio item"""
        logger.info(f"Playing audio: {item.id}")

        if PYAUDIO_AVAILABLE:
            await self._play_with_pyaudio(item)
        elif SOUNDDEVICE_AVAILABLE:
            await self._play_with_sounddevice(item)
        else:
            logger.warning("No audio playback library available")

    async def _play_with_pyaudio(self, item: AudioQueueItem):
        """Play audio using PyAudio"""
        p = pyaudio.PyAudio()

        try:
            # Determine format parameters based on AudioFormat
            if item.format in [AudioFormat.PCM_16000, AudioFormat.PCM_22050,
                              AudioFormat.PCM_24000, AudioFormat.PCM_44100]:
                # Raw PCM data
                sample_rate = int(item.format.value.split("_")[1])
                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=sample_rate,
                    output=True
                )

                # Apply volume
                audio_data = self._apply_volume(item.audio_data)

                # Play in chunks
                chunk_size = 1024
                for i in range(0, len(audio_data), chunk_size):
                    if self.playback_state != PlaybackState.PLAYING:
                        break
                    chunk = audio_data[i:i + chunk_size]
                    stream.write(chunk)

                stream.stop_stream()
                stream.close()
            else:
                # MP3 format - need to decode first
                # This would require additional libraries like pydub
                logger.warning(f"MP3 playback not implemented for format {item.format}")

        finally:
            p.terminate()

    async def _play_with_sounddevice(self, item: AudioQueueItem):
        """Play audio using sounddevice"""
        # Save to temporary file
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(item.audio_data)
            tmp_path = tmp.name

        try:
            # Read and play
            data, fs = sf.read(tmp_path)

            # Apply volume
            data = data * self.playback_settings["volume"]

            # Play and wait
            sd.play(data, fs)
            await asyncio.sleep(len(data) / fs)

        finally:
            os.unlink(tmp_path)

    def _apply_volume(self, audio_data: bytes) -> bytes:
        """Apply volume adjustment to PCM audio"""
        volume = self.playback_settings["volume"]
        if volume == 1.0:
            return audio_data

        # Assume 16-bit PCM
        samples = struct.unpack(f"{len(audio_data)//2}h", audio_data)
        adjusted = [int(s * volume) for s in samples]
        return struct.pack(f"{len(adjusted)}h", *adjusted)

    async def pause_playback(self):
        """Pause audio playback"""
        if self.playback_state == PlaybackState.PLAYING:
            self.playback_state = PlaybackState.PAUSED
            logger.info("Playback paused")

    async def resume_playback(self):
        """Resume audio playback"""
        if self.playback_state == PlaybackState.PAUSED:
            self.playback_state = PlaybackState.PLAYING
            await self.start_playback()
            logger.info("Playback resumed")

    async def stop_playback(self):
        """Stop audio playback and clear queue"""
        self.playback_state = PlaybackState.STOPPED
        self.playback_queue.clear()
        self.current_playback = None

        if self._playback_task:
            self._playback_task.cancel()

        logger.info("Playback stopped")

    def set_volume(self, volume: float):
        """Set playback volume (0.0 to 1.0)"""
        self.playback_settings["volume"] = max(0.0, min(1.0, volume))
        logger.info(f"Volume set to {self.playback_settings['volume']}")

    def set_speed(self, speed: float):
        """Set playback speed (0.5 to 2.0)"""
        self.playback_settings["speed"] = max(0.5, min(2.0, speed))
        logger.info(f"Speed set to {self.playback_settings['speed']}")

    def _get_cache_key(self, text: str, voice_id: str, model_id: str) -> str:
        """Generate cache key for audio"""
        content = f"{text}_{voice_id}_{model_id}"
        return hashlib.sha256(content.encode()).hexdigest()

    def _get_cached_audio(
        self,
        text: str,
        voice_id: str,
        model_id: str
    ) -> Optional[bytes]:
        """Get cached audio if available"""
        if not self.enable_cache:
            return None

        cache_key = self._get_cache_key(text, voice_id, model_id)
        cache_path = self.cache_dir / f"{cache_key}.mp3"

        if cache_path.exists():
            try:
                with open(cache_path, "rb") as f:
                    return f.read()
            except Exception as e:
                logger.error(f"Failed to read cache: {e}")

        return None

    def _cache_audio(
        self,
        text: str,
        voice_id: str,
        model_id: str,
        audio_data: bytes
    ):
        """Cache audio data"""
        if not self.enable_cache:
            return

        cache_key = self._get_cache_key(text, voice_id, model_id)
        cache_path = self.cache_dir / f"{cache_key}.mp3"

        try:
            with open(cache_path, "wb") as f:
                f.write(audio_data)
            logger.debug(f"Cached audio: {cache_key}")
        except Exception as e:
            logger.error(f"Failed to cache audio: {e}")

    async def get_user_info(self) -> Dict[str, Any]:
        """Get user information including quota"""
        try:
            response = await self._api_request("GET", "/user")

            self.quota_info = {
                "character_count": response.get("subscription", {}).get("character_count", 0),
                "character_limit": response.get("subscription", {}).get("character_limit", 0),
                "available_characters": response.get("subscription", {}).get("available_characters", 0),
                "next_character_count_reset_unix": response.get("subscription", {}).get("next_character_count_reset_unix", 0)
            }

            logger.info(f"Quota: {self.quota_info['available_characters']} characters remaining")
            return response

        except Exception as e:
            logger.error(f"Failed to get user info: {e}")
            raise

    async def _api_request(
        self,
        method: str,
        endpoint: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        return_bytes: bool = False
    ) -> Any:
        """Make API request"""
        url = f"{self.base_url}{endpoint}"

        async with self.session.request(
            method,
            url,
            json=json,
            params=params
        ) as response:
            # Check for quota limits
            if response.status == 429:
                reset_time = response.headers.get("x-ratelimit-reset")
                raise Exception(f"Rate limit exceeded. Reset at: {reset_time}")

            if response.status == 401:
                raise Exception("Invalid API key")

            if response.status >= 400:
                error_text = await response.text()
                raise Exception(f"API error {response.status}: {error_text}")

            if return_bytes:
                return await response.read()
            else:
                return await response.json()

    async def clear_cache(self):
        """Clear audio cache"""
        if not self.enable_cache:
            return

        cache_files = list(self.cache_dir.glob("*.mp3"))
        for file in cache_files:
            try:
                file.unlink()
            except Exception as e:
                logger.error(f"Failed to delete cache file {file}: {e}")

        logger.info(f"Cleared {len(cache_files)} cached audio files")

    async def close(self):
        """Close client and cleanup"""
        await self.stop_playback()

        if self.session:
            await self.session.close()

        logger.info("ElevenLabs client closed")