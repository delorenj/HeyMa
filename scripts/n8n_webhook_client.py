#!/usr/bin/env python3
"""
WhisperLiveKit → n8n Webhook Integration Client

Connects to WhisperLiveKit WebSocket and sends transcriptions to n8n webhook.
"""

import asyncio
import json
import websockets
import pyaudio
import argparse
from urllib.parse import urljoin
import requests


class WhisperToN8nClient:
    def __init__(self, whisper_url="ws://localhost:8888/asr", n8n_webhook_url=None):
        self.whisper_url = whisper_url
        self.n8n_webhook_url = n8n_webhook_url
        self.audio_format = pyaudio.paInt16
        self.channels = 1
        self.target_rate = 16000
        self.chunk = 1024
        self.native_rate = None

    async def send_to_n8n(self, transcription_data):
        """Send transcription to n8n webhook"""
        if not self.n8n_webhook_url:
            print(f"[Transcription] {transcription_data.get('text', '')}")
            return

        try:
            response = requests.post(
                self.n8n_webhook_url, json=transcription_data, timeout=5
            )
            print(
                f"[n8n] Sent: {transcription_data.get('text', '')} (Status: {response.status_code})"
            )
        except Exception as e:
            print(f"[n8n Error] {e}")

    async def audio_stream(self, websocket):
        """Stream audio from microphone to WhisperLiveKit"""
        import numpy as np
        from scipy import signal

        p = pyaudio.PyAudio()

        # Auto-detect microphone and sample rate
        default_device = None
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                if "yeti" in info["name"].lower():
                    default_device = i
                    break

        if default_device is None:
            default_device = p.get_default_input_device_info()["index"]

        device_info = p.get_device_info_by_index(default_device)
        self.native_rate = int(device_info["defaultSampleRate"])

        # Try 16kHz first, fallback to native rate
        try:
            if not p.is_format_supported(
                self.target_rate,
                input_device=default_device,
                input_channels=self.channels,
                input_format=self.audio_format,
            ):
                print(f"[Resampling] {self.native_rate} Hz → {self.target_rate} Hz")
        except:
            pass

        stream = p.open(
            format=self.audio_format,
            channels=self.channels,
            rate=self.native_rate,
            input=True,
            input_device_index=default_device,
            frames_per_buffer=self.chunk,
        )

        need_resample = self.native_rate != self.target_rate
        print(f"[Mic] Recording at {self.native_rate} Hz... Press Ctrl+C to stop")

        try:
            while True:
                audio_data = stream.read(self.chunk, exception_on_overflow=False)

                # Resample if needed
                if need_resample:
                    audio_array = np.frombuffer(audio_data, dtype=np.int16)
                    num_samples = int(
                        len(audio_array) * self.target_rate / self.native_rate
                    )
                    resampled = signal.resample(audio_array, num_samples)
                    resampled = np.clip(resampled, -32768, 32767).astype(np.int16)
                    audio_data = resampled.tobytes()

                await websocket.send(audio_data)
                await asyncio.sleep(0.01)
        except KeyboardInterrupt:
            print("\n[Mic] Stopping...")
        finally:
            stream.stop_stream()
            stream.close()
            p.terminate()
            # Send empty message to signal end
            await websocket.send(b"")

    async def receive_transcriptions(self, websocket):
        """Receive transcriptions from WhisperLiveKit and forward to n8n"""
        try:
            async for message in websocket:
                data = json.loads(message)

                if data.get("type") == "transcript":
                    # Full transcript received
                    await self.send_to_n8n(
                        {
                            "type": "final",
                            "text": data.get("text", ""),
                            "timestamp": data.get("timestamp"),
                            "speaker": data.get("speaker"),
                        }
                    )
                elif data.get("type") == "partial":
                    # Partial/interim transcript
                    print(f"[Partial] {data.get('text', '')}", end="\r")
                elif data.get("type") == "ready_to_stop":
                    print("\n[Status] Transcription complete")
                    break
        except websockets.exceptions.ConnectionClosed:
            print("[WebSocket] Connection closed")

    async def run(self):
        """Main client loop"""
        print(f"[Connecting] {self.whisper_url}")
        if self.n8n_webhook_url:
            print(f"[n8n Webhook] {self.n8n_webhook_url}")
        else:
            print("[Mode] Local display only (no n8n webhook)")

        async with websockets.connect(self.whisper_url) as websocket:
            # Run audio streaming and transcription receiving concurrently
            await asyncio.gather(
                self.audio_stream(websocket), self.receive_transcriptions(websocket)
            )


def main():
    parser = argparse.ArgumentParser(description="WhisperLiveKit → n8n Integration")
    parser.add_argument(
        "--whisper-url",
        default="ws://localhost:8888/asr",
        help="WhisperLiveKit WebSocket URL",
    )
    parser.add_argument(
        "--n8n-webhook",
        help="n8n webhook URL (e.g., https://n8n.delo.sh/webhook/whisper-transcription)",
    )

    args = parser.parse_args()

    client = WhisperToN8nClient(
        whisper_url=args.whisper_url, n8n_webhook_url=args.n8n_webhook
    )

    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        print("\n[Exit] Goodbye!")


if __name__ == "__main__":
    main()
