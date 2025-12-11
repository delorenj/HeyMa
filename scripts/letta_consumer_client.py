#!/usr/bin/env python3
"""
Letta Consumer Client

Captures audio from microphone, sends to WhisperLiveKit for transcription,
and publishes transcriptions to RabbitMQ for Letta processing.

Flow:
  Microphone → WhisperLiveKit WebSocket → This Client → RabbitMQ → Letta Bridge
"""
import asyncio
import json
import websockets
import pyaudio
import argparse
import sys
import os
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional
import logging

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from TonnyTray.backend.integrations.rabbitmq_client import RabbitMQClient, RabbitMQConfig

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(name)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class LettaConsumerClient:
    """
    Audio capture client that publishes transcriptions to RabbitMQ
    """

    def __init__(
        self,
        whisper_url: str = "ws://localhost:8888/asr",
        rabbitmq_url: str = "amqp://guest:guest@localhost/",
        routing_key: str = "thread.letta.prompt",
        device_index: Optional[int] = None
    ):
        """
        Initialize Letta consumer client

        Args:
            whisper_url: WhisperLiveKit WebSocket URL
            rabbitmq_url: RabbitMQ connection URL
            routing_key: Routing key for transcription messages
            device_index: Audio device index (optional, auto-detects)
        """
        self.whisper_url = whisper_url
        self.routing_key = routing_key
        self.device_index = device_index
        self.session_id = str(uuid.uuid4())

        # Audio configuration
        self.audio_format = pyaudio.paInt16
        self.channels = 1
        self.target_rate = 16000  # WhisperLiveKit expects 16kHz
        self.chunk = 1024
        self.native_rate = None

        # Initialize RabbitMQ publisher
        rabbitmq_config = RabbitMQConfig(
            url=rabbitmq_url,
            exchange_name="amq.topic"
        )
        self.rabbitmq = RabbitMQClient(rabbitmq_config)

        # Statistics
        self.stats = {
            "transcriptions_sent": 0,
            "errors": 0,
            "start_time": datetime.now()
        }

    def get_default_device(self, audio):
        """Get default microphone device with Yeti preference"""
        try:
            device_count = audio.get_device_count()
            yeti_device = None
            scarlett_device = None
            default_device = audio.get_default_input_device_info()

            logger.info("Scanning audio devices...")

            for i in range(device_count):
                try:
                    info = audio.get_device_info_by_index(i)
                    if info["maxInputChannels"] > 0:
                        name = info["name"].lower()
                        if "yeti" in name:
                            yeti_device = i
                            logger.info(f"  Found Yeti microphone: Device {i}")
                        elif "scarlett" in name:
                            scarlett_device = i
                            logger.info(f"  Found Scarlett: Device {i}")
                except Exception:
                    pass

            # Preference order: Yeti > Scarlett > Default
            if yeti_device is not None:
                logger.info(f"Using Yeti Stereo Microphone (device {yeti_device})")
                return yeti_device
            elif scarlett_device is not None:
                logger.info(f"Using Scarlett (device {scarlett_device})")
                return scarlett_device
            else:
                device_idx = default_device["index"]
                logger.info(f"Using default device: {default_device['name']} (device {device_idx})")
                return device_idx

        except Exception as e:
            logger.error(f"Error getting default device: {e}")
            return None

    async def connect_and_stream(self):
        """Connect to WhisperLiveKit and stream audio"""
        logger.info(f"Connecting to WhisperLiveKit at {self.whisper_url}")

        # Initialize audio
        audio = pyaudio.PyAudio()

        try:
            # Get device
            device_index = self.device_index
            if device_index is None:
                device_index = self.get_default_device(audio)

            if device_index is None:
                logger.error("No suitable audio device found")
                return

            # Get device info
            device_info = audio.get_device_info_by_index(device_index)
            self.native_rate = int(device_info["defaultSampleRate"])
            logger.info(f"Device sample rate: {self.native_rate}Hz → Resampling to {self.target_rate}Hz")

            # Open audio stream
            stream = audio.open(
                format=self.audio_format,
                channels=self.channels,
                rate=self.native_rate,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=self.chunk
            )

            # Connect to RabbitMQ
            await self.rabbitmq.connect()
            logger.info("Connected to RabbitMQ")

            # Connect to WhisperLiveKit
            async with websockets.connect(self.whisper_url) as websocket:
                logger.info("Connected to WhisperLiveKit")
                logger.info(f"Session ID: {self.session_id}")
                logger.info("Listening... Speak into microphone (Ctrl+C to stop)")

                # Create tasks for audio streaming and receiving transcriptions
                audio_task = asyncio.create_task(
                    self._stream_audio(stream, websocket)
                )
                receive_task = asyncio.create_task(
                    self._receive_transcriptions(websocket)
                )

                # Wait for either task to complete
                done, pending = await asyncio.wait(
                    [audio_task, receive_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                # Cancel pending tasks
                for task in pending:
                    task.cancel()

        except KeyboardInterrupt:
            logger.info("\nShutting down...")
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
        finally:
            stream.stop_stream()
            stream.close()
            audio.terminate()
            await self.rabbitmq.disconnect()
            logger.info("Disconnected")

    async def _stream_audio(self, stream, websocket):
        """Stream audio to WhisperLiveKit"""
        try:
            while True:
                # Read audio chunk
                data = stream.read(self.chunk, exception_on_overflow=False)

                # Send to WhisperLiveKit
                await websocket.send(data)
                await asyncio.sleep(0.01)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Audio streaming error: {e}")

    async def _receive_transcriptions(self, websocket):
        """Receive transcriptions from WhisperLiveKit"""
        try:
            async for message in websocket:
                try:
                    # Parse transcription
                    data = json.loads(message)
                    text = data.get("text", "").strip()

                    if not text:
                        continue

                    is_final = data.get("ready_to_stop", False)

                    if is_final:
                        logger.info(f"[Transcription] {text}")

                        # Publish to RabbitMQ
                        await self._publish_transcription(text)

                except json.JSONDecodeError:
                    logger.warning("Failed to parse transcription")
                except Exception as e:
                    logger.error(f"Error processing transcription: {e}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Receive error: {e}")

    async def _publish_transcription(self, text: str):
        """
        Publish transcription to RabbitMQ

        Args:
            text: Transcription text
        """
        try:
            # Build message
            message = {
                "event_id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()),
                "session_id": self.session_id,
                "timestamp": datetime.now().timestamp(),
                "event_type": "transcription",
                "payload": {
                    "text": text,
                    "source": "whisperlivekit",
                    "device": "microphone"
                }
            }

            # Publish to RabbitMQ
            success = await self.rabbitmq.publish_event(
                routing_key=self.routing_key,
                payload=message,
                correlation_id=message["request_id"]
            )

            if success:
                self.stats["transcriptions_sent"] += 1
                logger.info(f"[Published] Transcription to RabbitMQ ({self.routing_key})")
            else:
                logger.error("Failed to publish to RabbitMQ")
                self.stats["errors"] += 1

        except Exception as e:
            logger.error(f"Error publishing transcription: {e}")
            self.stats["errors"] += 1

    def get_stats(self):
        """Get client statistics"""
        uptime = (datetime.now() - self.stats["start_time"]).total_seconds()
        return {
            **self.stats,
            "uptime_seconds": uptime,
            "session_id": self.session_id
        }


def list_audio_devices():
    """List available audio input devices"""
    audio = pyaudio.PyAudio()
    print("\n[Audio Input Devices]")
    for i in range(audio.get_device_count()):
        try:
            info = audio.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                print(f"  [{i}] {info['name']}")
        except Exception:
            pass
    audio.terminate()


async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Letta Consumer Client")
    parser.add_argument(
        "--whisper-url",
        default=os.getenv("WHISPER_URL", "ws://localhost:8888/asr"),
        help="WhisperLiveKit WebSocket URL (default: ws://localhost:8888/asr)"
    )
    parser.add_argument(
        "--rabbitmq-url",
        default=os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost/"),
        help="RabbitMQ URL (default: amqp://guest:guest@localhost/)"
    )
    parser.add_argument(
        "--routing-key",
        default="thread.letta.prompt",
        help="RabbitMQ routing key for transcriptions"
    )
    parser.add_argument(
        "--device",
        type=int,
        help="Audio device index (use --list-devices to see options)"
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available audio devices and exit"
    )

    args = parser.parse_args()

    if args.list_devices:
        list_audio_devices()
        return

    # Create and run client
    client = LettaConsumerClient(
        whisper_url=args.whisper_url,
        rabbitmq_url=args.rabbitmq_url,
        routing_key=args.routing_key,
        device_index=args.device
    )

    await client.connect_and_stream()


if __name__ == "__main__":
    asyncio.run(main())
