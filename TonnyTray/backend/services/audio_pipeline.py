"""
Audio Pipeline Service

Manages the complete audio capture, transcription, and playback flow.
"""
import asyncio
import time
import threading
from typing import Optional, Callable, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum
import logging
import numpy as np
import struct
import collections

try:
    import sounddevice as sd
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    SOUNDDEVICE_AVAILABLE = False
    logging.warning("sounddevice not available")

try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False
    logging.warning("pyaudio not available")

try:
    import webrtcvad
    VAD_AVAILABLE = True
except ImportError:
    VAD_AVAILABLE = False
    logging.warning("webrtcvad not available, VAD disabled")

logger = logging.getLogger(__name__)


class AudioMode(Enum):
    """Audio capture modes"""
    CONTINUOUS = "continuous"  # Always listening
    PUSH_TO_TALK = "push_to_talk"  # Manual activation
    VOICE_ACTIVATION = "voice_activation"  # VAD-based


class AudioState(Enum):
    """Audio pipeline states"""
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    ERROR = "error"


@dataclass
class AudioConfig:
    """Audio pipeline configuration"""
    sample_rate: int = 16000
    channels: int = 1
    chunk_duration: float = 0.03  # 30ms chunks for VAD
    input_device: Optional[int] = None
    output_device: Optional[int] = None
    mode: AudioMode = AudioMode.VOICE_ACTIVATION
    vad_aggressiveness: int = 2  # 0-3, higher = more aggressive
    vad_padding_duration: float = 0.3  # Continue after speech ends
    silence_threshold: float = 0.01  # RMS threshold for silence
    audio_level_callback: Optional[Callable[[float], None]] = None
    push_to_talk_key: Optional[str] = None


@dataclass
class AudioMetrics:
    """Audio pipeline metrics"""
    total_audio_captured: float = 0.0  # seconds
    total_speech_detected: float = 0.0  # seconds
    average_audio_level: float = 0.0
    peak_audio_level: float = 0.0
    vad_activations: int = 0
    device_changes: int = 0
    errors: int = 0


class VoiceActivityDetector:
    """Voice Activity Detection wrapper"""

    def __init__(self, sample_rate: int, aggressiveness: int = 2):
        self.sample_rate = sample_rate
        self.aggressiveness = aggressiveness
        self.vad = None

        if VAD_AVAILABLE:
            self.vad = webrtcvad.Vad(aggressiveness)
        else:
            logger.warning("VAD not available, using amplitude-based detection")

    def is_speech(self, audio_data: bytes, sample_rate: Optional[int] = None) -> bool:
        """Check if audio contains speech"""
        if self.vad:
            # webrtcvad requires specific sample rates and frame durations
            sr = sample_rate or self.sample_rate
            if sr not in [8000, 16000, 32000, 48000]:
                sr = 16000  # Default to 16kHz

            # Frame must be 10, 20, or 30 ms
            frame_duration = len(audio_data) * 1000 // (sr * 2)  # 16-bit samples
            if frame_duration not in [10, 20, 30]:
                return False

            try:
                return self.vad.is_speech(audio_data, sr)
            except Exception as e:
                logger.error(f"VAD error: {e}")
                return self._amplitude_based_detection(audio_data)
        else:
            return self._amplitude_based_detection(audio_data)

    def _amplitude_based_detection(self, audio_data: bytes) -> bool:
        """Fallback amplitude-based speech detection"""
        # Convert bytes to numpy array
        samples = np.frombuffer(audio_data, dtype=np.int16)

        # Calculate RMS
        rms = np.sqrt(np.mean(samples.astype(float) ** 2))

        # Normalize to 0-1 range
        normalized_rms = rms / 32768.0

        # Simple threshold
        return normalized_rms > 0.01


class AudioPipeline:
    """
    Main audio pipeline orchestrator

    Manages the complete flow:
    - Audio capture from microphone
    - Voice activity detection
    - Send to WhisperLiveKit for transcription
    - Handle device changes
    - Audio level monitoring
    """

    def __init__(self, config: AudioConfig):
        self.config = config
        self.state = AudioState.IDLE
        self.metrics = AudioMetrics()

        # Audio capture
        self._input_stream = None
        self._capture_thread: Optional[threading.Thread] = None
        self._capture_running = False

        # Voice activity detection
        self.vad = VoiceActivityDetector(
            config.sample_rate,
            config.vad_aggressiveness
        )
        self._speech_buffer = collections.deque(maxlen=100)
        self._is_speaking = False
        self._silence_frames = 0
        self._speech_start_time: Optional[float] = None

        # Audio level monitoring
        self._audio_levels = collections.deque(maxlen=100)
        self._last_audio_level = 0.0

        # Callbacks
        self._audio_callback: Optional[Callable[[bytes], None]] = None
        self._speech_start_callback: Optional[Callable[[], None]] = None
        self._speech_end_callback: Optional[Callable[[], None]] = None

        # Push-to-talk state
        self._ptt_active = False

        # Device monitoring
        self._device_check_task: Optional[asyncio.Task] = None
        self._last_devices = None

    def set_audio_callback(self, callback: Callable[[bytes], None]):
        """Set callback for audio data"""
        self._audio_callback = callback

    def set_speech_callbacks(
        self,
        start_callback: Optional[Callable[[], None]] = None,
        end_callback: Optional[Callable[[], None]] = None
    ):
        """Set callbacks for speech events"""
        self._speech_start_callback = start_callback
        self._speech_end_callback = end_callback

    async def start(self):
        """Start audio pipeline"""
        if self.state == AudioState.LISTENING:
            logger.debug("Audio pipeline already running")
            return

        logger.info(f"Starting audio pipeline (mode: {self.config.mode.value})")
        self.state = AudioState.LISTENING

        # Start device monitoring
        self._device_check_task = asyncio.create_task(self._monitor_devices())

        # Start audio capture
        await self._start_capture()

    async def stop(self):
        """Stop audio pipeline"""
        logger.info("Stopping audio pipeline")
        self.state = AudioState.IDLE

        # Stop device monitoring
        if self._device_check_task:
            self._device_check_task.cancel()

        # Stop audio capture
        await self._stop_capture()

    async def _start_capture(self):
        """Start audio capture"""
        if SOUNDDEVICE_AVAILABLE:
            await self._start_sounddevice_capture()
        elif PYAUDIO_AVAILABLE:
            await self._start_pyaudio_capture()
        else:
            logger.error("No audio capture library available")
            self.state = AudioState.ERROR

    async def _start_sounddevice_capture(self):
        """Start capture using sounddevice"""
        try:
            # Calculate chunk size in samples
            chunk_samples = int(self.config.sample_rate * self.config.chunk_duration)

            def audio_callback(indata, frames, time_info, status):
                """Sounddevice callback"""
                if status:
                    logger.warning(f"Audio status: {status}")

                # Process audio
                self._process_audio_chunk(indata.tobytes())

            self._input_stream = sd.InputStream(
                device=self.config.input_device,
                channels=self.config.channels,
                samplerate=self.config.sample_rate,
                blocksize=chunk_samples,
                callback=audio_callback
            )

            self._input_stream.start()
            logger.info(f"Started sounddevice capture (device: {self.config.input_device})")

        except Exception as e:
            logger.error(f"Failed to start sounddevice capture: {e}")
            self.state = AudioState.ERROR
            self.metrics.errors += 1

    async def _start_pyaudio_capture(self):
        """Start capture using pyaudio"""
        try:
            p = pyaudio.PyAudio()

            # Calculate chunk size
            chunk_samples = int(self.config.sample_rate * self.config.chunk_duration)

            def audio_callback(in_data, frame_count, time_info, status):
                """PyAudio callback"""
                if status:
                    logger.warning(f"Audio status: {status}")

                # Process audio
                self._process_audio_chunk(in_data)

                return (in_data, pyaudio.paContinue)

            self._input_stream = p.open(
                format=pyaudio.paInt16,
                channels=self.config.channels,
                rate=self.config.sample_rate,
                input=True,
                input_device_index=self.config.input_device,
                frames_per_buffer=chunk_samples,
                stream_callback=audio_callback
            )

            self._input_stream.start_stream()
            logger.info(f"Started pyaudio capture (device: {self.config.input_device})")

        except Exception as e:
            logger.error(f"Failed to start pyaudio capture: {e}")
            self.state = AudioState.ERROR
            self.metrics.errors += 1

    async def _stop_capture(self):
        """Stop audio capture"""
        if self._input_stream:
            try:
                if SOUNDDEVICE_AVAILABLE and hasattr(self._input_stream, 'stop'):
                    self._input_stream.stop()
                    self._input_stream.close()
                elif PYAUDIO_AVAILABLE:
                    self._input_stream.stop_stream()
                    self._input_stream.close()
            except Exception as e:
                logger.error(f"Error stopping capture: {e}")

            self._input_stream = None

        logger.info("Audio capture stopped")

    def _process_audio_chunk(self, audio_data: bytes):
        """Process audio chunk"""
        # Update metrics
        chunk_duration = len(audio_data) / (self.config.sample_rate * 2)  # 16-bit
        self.metrics.total_audio_captured += chunk_duration

        # Calculate audio level
        audio_level = self._calculate_audio_level(audio_data)
        self._audio_levels.append(audio_level)
        self._last_audio_level = audio_level

        # Update average and peak
        if self._audio_levels:
            self.metrics.average_audio_level = np.mean(self._audio_levels)
            self.metrics.peak_audio_level = max(self.metrics.peak_audio_level, audio_level)

        # Notify level callback
        if self.config.audio_level_callback:
            try:
                self.config.audio_level_callback(audio_level)
            except Exception as e:
                logger.error(f"Audio level callback error: {e}")

        # Process based on mode
        should_send = False

        if self.config.mode == AudioMode.CONTINUOUS:
            should_send = True

        elif self.config.mode == AudioMode.PUSH_TO_TALK:
            should_send = self._ptt_active

        elif self.config.mode == AudioMode.VOICE_ACTIVATION:
            should_send = self._process_vad(audio_data)

        # Send audio if needed
        if should_send and self._audio_callback:
            try:
                # Run callback in thread pool to avoid blocking
                asyncio.create_task(
                    asyncio.to_thread(self._audio_callback, audio_data)
                )
            except Exception as e:
                logger.error(f"Audio callback error: {e}")

    def _calculate_audio_level(self, audio_data: bytes) -> float:
        """Calculate normalized audio level (0-1)"""
        samples = np.frombuffer(audio_data, dtype=np.int16)
        rms = np.sqrt(np.mean(samples.astype(float) ** 2))
        return min(1.0, rms / 32768.0)

    def _process_vad(self, audio_data: bytes) -> bool:
        """Process voice activity detection"""
        is_speech = self.vad.is_speech(audio_data, self.config.sample_rate)

        if is_speech:
            if not self._is_speaking:
                # Speech started
                self._is_speaking = True
                self._speech_start_time = time.time()
                self.metrics.vad_activations += 1

                if self._speech_start_callback:
                    try:
                        asyncio.create_task(
                            asyncio.to_thread(self._speech_start_callback)
                        )
                    except Exception as e:
                        logger.error(f"Speech start callback error: {e}")

                logger.debug("Speech started")

            self._silence_frames = 0
            self._speech_buffer.append(audio_data)

            # Update speech duration
            if self._speech_start_time:
                speech_duration = time.time() - self._speech_start_time
                self.metrics.total_speech_detected = speech_duration

            return True

        else:
            if self._is_speaking:
                # Still in speech, but current frame is silence
                self._silence_frames += 1

                # Check if enough silence to end speech
                silence_duration = (self._silence_frames *
                                  self.config.chunk_duration)

                if silence_duration >= self.config.vad_padding_duration:
                    # Speech ended
                    self._is_speaking = False
                    self._speech_start_time = None
                    self._silence_frames = 0
                    self._speech_buffer.clear()

                    if self._speech_end_callback:
                        try:
                            asyncio.create_task(
                                asyncio.to_thread(self._speech_end_callback)
                            )
                        except Exception as e:
                            logger.error(f"Speech end callback error: {e}")

                    logger.debug("Speech ended")
                    return False
                else:
                    # Still considering as speech (padding)
                    return True

            return False

    def set_push_to_talk(self, active: bool):
        """Set push-to-talk state"""
        if self.config.mode != AudioMode.PUSH_TO_TALK:
            logger.warning("Push-to-talk called but not in PTT mode")
            return

        self._ptt_active = active
        logger.debug(f"Push-to-talk: {'active' if active else 'inactive'}")

        if active and self._speech_start_callback:
            self._speech_start_callback()
        elif not active and self._speech_end_callback:
            self._speech_end_callback()

    async def _monitor_devices(self):
        """Monitor audio devices for changes"""
        while self.state == AudioState.LISTENING:
            try:
                current_devices = self.get_audio_devices()

                if self._last_devices is not None:
                    # Check for changes
                    if current_devices != self._last_devices:
                        logger.warning("Audio device change detected")
                        self.metrics.device_changes += 1

                        # Restart capture with new devices
                        await self._stop_capture()
                        await asyncio.sleep(0.5)
                        await self._start_capture()

                self._last_devices = current_devices

            except Exception as e:
                logger.error(f"Device monitoring error: {e}")

            # Check every 5 seconds
            await asyncio.sleep(5)

    def get_audio_devices(self) -> Dict[str, List[Dict[str, Any]]]:
        """Get available audio devices"""
        devices = {
            "input": [],
            "output": []
        }

        if SOUNDDEVICE_AVAILABLE:
            try:
                for idx, device in enumerate(sd.query_devices()):
                    info = {
                        "index": idx,
                        "name": device['name'],
                        "channels": device['max_input_channels'] or device['max_output_channels'],
                        "sample_rate": device['default_samplerate']
                    }

                    if device['max_input_channels'] > 0:
                        devices["input"].append(info)
                    if device['max_output_channels'] > 0:
                        devices["output"].append(info)

            except Exception as e:
                logger.error(f"Failed to query sounddevice devices: {e}")

        elif PYAUDIO_AVAILABLE:
            try:
                p = pyaudio.PyAudio()

                for idx in range(p.get_device_count()):
                    info = p.get_device_info_by_index(idx)
                    device_info = {
                        "index": idx,
                        "name": info['name'],
                        "channels": info['maxInputChannels'] or info['maxOutputChannels'],
                        "sample_rate": info['defaultSampleRate']
                    }

                    if info['maxInputChannels'] > 0:
                        devices["input"].append(device_info)
                    if info['maxOutputChannels'] > 0:
                        devices["output"].append(device_info)

                p.terminate()

            except Exception as e:
                logger.error(f"Failed to query pyaudio devices: {e}")

        return devices

    def set_input_device(self, device_index: Optional[int]):
        """Change input device"""
        self.config.input_device = device_index
        logger.info(f"Input device set to: {device_index}")

        # Restart capture if running
        if self.state == AudioState.LISTENING:
            asyncio.create_task(self._restart_capture())

    async def _restart_capture(self):
        """Restart audio capture"""
        await self._stop_capture()
        await asyncio.sleep(0.5)
        await self._start_capture()

    def set_mode(self, mode: AudioMode):
        """Change audio capture mode"""
        self.config.mode = mode
        logger.info(f"Audio mode set to: {mode.value}")

        # Reset mode-specific state
        self._ptt_active = False
        self._is_speaking = False
        self._silence_frames = 0

    def get_metrics(self) -> Dict[str, Any]:
        """Get pipeline metrics"""
        return {
            "state": self.state.value,
            "mode": self.config.mode.value,
            "total_audio_captured": self.metrics.total_audio_captured,
            "total_speech_detected": self.metrics.total_speech_detected,
            "average_audio_level": self.metrics.average_audio_level,
            "peak_audio_level": self.metrics.peak_audio_level,
            "vad_activations": self.metrics.vad_activations,
            "device_changes": self.metrics.device_changes,
            "errors": self.metrics.errors,
            "current_audio_level": self._last_audio_level,
            "is_speaking": self._is_speaking
        }

    def get_audio_level(self) -> float:
        """Get current audio level (0-1)"""
        return self._last_audio_level

    def is_speaking(self) -> bool:
        """Check if currently detecting speech"""
        return self._is_speaking