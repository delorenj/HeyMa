#!/usr/bin/env python3
"""Debug client to see exactly what's happening"""

import asyncio
import websockets
import pyaudio
import json
import numpy as np
from scipy import signal


async def debug_transcription():
    url = "ws://localhost:8888/asr"

    print(f"[Connecting] {url}")

    async with websockets.connect(url) as ws:
        print("[Connected] WebSocket established")

        # Setup audio
        p = pyaudio.PyAudio()

        # Use Yeti
        device = 6
        native_rate = 48000
        target_rate = 16000

        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=native_rate,
            input=True,
            input_device_index=device,
            frames_per_buffer=1024,
        )

        print(f"[Audio] Recording from device {device} at {native_rate} Hz")
        print(f"[Audio] Resampling to {target_rate} Hz")
        print("[Ready] Speak now... Will run for 10 seconds")
        print()

        async def send_audio():
            """Send audio to server"""
            try:
                for i in range(100):  # ~10 seconds
                    audio_data = stream.read(1024, exception_on_overflow=False)
                    audio_array = np.frombuffer(audio_data, dtype=np.int16)

                    # Show volume
                    volume = np.abs(audio_array).mean()
                    if volume > 100:
                        bars = int((volume / 3000) * 20)
                        bars = min(bars, 20)
                        meter = "█" * bars
                        print(f"\r[Audio] {meter:<20} {volume:>5.0f}", end="")

                    # Resample
                    num_samples = int(len(audio_array) * target_rate / native_rate)
                    resampled = signal.resample(audio_array, num_samples)
                    resampled = np.clip(resampled, -32768, 32767).astype(np.int16)

                    await ws.send(resampled.tobytes())
                    await asyncio.sleep(0.01)

                print("\n[Done] Sending empty to stop")
                await ws.send(b"")  # Signal end

            except Exception as e:
                print(f"\n[Error] Audio send: {e}")
            finally:
                stream.stop_stream()
                stream.close()
                p.terminate()

        async def receive_messages():
            """Receive and display all messages from server"""
            try:
                async for message in ws:
                    data = json.loads(message)
                    msg_type = data.get("type", "unknown")

                    print(f"\n[Server] Type: {msg_type}")

                    if msg_type == "transcript":
                        print(f"         Text: '{data.get('text', '')}'")
                        print(f"         Speaker: {data.get('speaker', 'N/A')}")
                    elif msg_type == "partial":
                        print(f"         Partial: '{data.get('text', '')}'")
                    elif msg_type == "ready_to_stop":
                        print(f"         Server ready to close")
                    else:
                        # Print full message for unknown types
                        print(f"         Data: {data}")

            except websockets.exceptions.ConnectionClosed:
                print("\n[Closed] Connection closed")
            except Exception as e:
                print(f"\n[Error] Receive: {e}")

        # Run both concurrently
        await asyncio.gather(send_audio(), receive_messages())

        print("\n[Complete] Debug session finished")


if __name__ == "__main__":
    asyncio.run(debug_transcription())
