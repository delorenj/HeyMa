#!/usr/bin/env python3
"""
WhisperLiveKit → Auto-Type Client

Connects to WhisperLiveKit WebSocket and types transcriptions into active window.
Similar to nerd-dictation but using your remote WhisperLiveKit server!
"""

import asyncio
import json
import websockets
import pyaudio
import argparse
import subprocess
import sys
import requests
from datetime import datetime
import shutil
from difflib import SequenceMatcher


class WhisperAutoTypeClient:
    def __init__(
        self, whisper_url="ws://localhost:8888/asr", use_wtype=True, device_index=None, send_to_n8n=False
    ):
        self.whisper_url = whisper_url
        self.use_wtype = use_wtype  # wtype for Wayland, xdotool for X11
        self.device_index = device_index  # Manual device selection
        self.send_to_n8n = send_to_n8n
        self.n8n_url = "https://n8n.delo.sh/webhook/ask-tonny"
        self.last_n8n_text = ""  # Track last text sent to n8n
        self.audio_format = pyaudio.paInt16
        self.channels = 1
        self.target_rate = 16000  # WhisperLiveKit expects 16kHz
        self.chunk = 1024
        self.accumulated_text = ""
        self.last_final_text = ""
        self.native_rate = None  # Will be set based on device capabilities

    def type_text(self, text):
        """Type text into the active window"""
        if not text.strip():
            return

        try:
            if self.use_wtype:
                # Try ydotool first for Wayland
                try:
                    subprocess.run(
                        ["ydotool", "type", text],
                        check=True,
                        text=True,
                        capture_output=True,
                    )
                    print(f"[Typed] {text}")
                    return
                except (FileNotFoundError, subprocess.CalledProcessError):
                    # Fallback to wtype
                    subprocess.run(
                        ["wtype", text],
                        check=True,
                        text=True,
                        capture_output=True,
                    )
                    print(f"[Typed] {text}")
            else:
                # X11 fallback
                subprocess.run(
                    ["xdotool", "type", "--", text],
                    check=True,
                    text=True,
                    capture_output=True,
                )
                print(f"[Typed] {text}")
        except FileNotFoundError:
            print(f"[Error] {'wtype' if self.use_wtype else 'xdotool'} not found!")
            print("Install: sudo apt install wtype (Wayland) or xdotool (X11)")
            sys.exit(1)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            stdout = (e.stdout or "").strip()
            combined = "\n".join(part for part in [stdout, stderr] if part)
            print(f"[Type Error] {e}")
            if self.use_wtype and "virtual keyboard" in combined.lower():
                wl_copy = shutil.which("wl-copy")
                if wl_copy:
                    try:
                        subprocess.run([wl_copy], input=text, text=True, check=True)
                        print(
                            "[Clipboard] Compositor lacks virtual keyboard support; copied text to clipboard. Paste manually."
                        )
                    except Exception as copy_err:
                        print(f"[Clipboard Error] Failed to copy text: {copy_err}")
                else:
                    print(
                        "[Hint] Compositor lacks virtual keyboard support. Install wl-clipboard for clipboard fallback or rerun with --use-xdotool under X11."
                    )
        
        # Send to n8n if enabled
        if self.send_to_n8n:
            self.send_to_n8n_webhook(text)

    def send_to_n8n_webhook(self, text):
        """Send transcribed text to n8n webhook"""
        print(f"[N8N Debug] Attempting to send: {text}")
        try:
            payload = {
                "timestamp": datetime.now().isoformat(),
                "text": text,
                "source": "voice_transcription"
            }
            headers = {
                "Content-Type": "application/json"
            }
            print(f"[N8N Debug] Sending to {self.n8n_url}")
            response = requests.post(self.n8n_url, json=payload, headers=headers, timeout=5)
            print(f"[N8N] Sent: {text[:50]}... Status: {response.status_code}")
        except Exception as e:
            print(f"[N8N Error] {e}")

    def _type_final_text(self, final_text: str):
        """Type only the new portion of the final transcript."""
        final_text = (final_text or "").strip()
        if not final_text:
            return

        if self.last_final_text:
            if final_text.startswith(self.last_final_text):
                new_text = final_text[len(self.last_final_text):].lstrip()
            else:
                matcher = SequenceMatcher(None, self.last_final_text, final_text)
                match = matcher.find_longest_match(
                    0,
                    len(self.last_final_text),
                    0,
                    len(final_text),
                )
                if match.size > 0:
                    new_text = final_text[match.b + match.size :].lstrip()
                else:
                    new_text = final_text
        else:
            new_text = final_text

        if not new_text:
            self.last_final_text = final_text
            return

        needs_space = (
            bool(self.accumulated_text)
            and not self.accumulated_text.endswith((" ", "\n"))
            and not new_text.startswith((" ", "\n"))
        )
        if needs_space:
            self.type_text(" ")
            self.accumulated_text += " "

        self.type_text(new_text)
        self.accumulated_text += new_text
        self.last_final_text = final_text
        
        # Send complete final text to n8n if enabled and text changed
        if self.send_to_n8n and final_text and final_text != self.last_n8n_text:
            self.send_to_n8n_webhook(final_text)
            self.last_n8n_text = final_text

    async def audio_stream(self, websocket):
        """Stream audio from microphone to WhisperLiveKit"""
        p = pyaudio.PyAudio()

        # Find the best input device
        info = p.get_host_api_info_by_index(0)
        num_devices = info.get("deviceCount")

        print("[Audio Devices]")
        default_device = self.device_index  # Use manual selection if provided

        for i in range(num_devices):
            device_info = p.get_device_info_by_host_api_device_index(0, i)
            if device_info.get("maxInputChannels") > 0:
                name = device_info.get("name")
                is_default = (
                    "(default)"
                    if i == p.get_default_input_device_info()["index"]
                    else ""
                )
                selected = " ← SELECTED" if i == default_device else ""
                print(f"  [{i}] {name} {is_default}{selected}")

                # Auto-detect if not manually specified
                if default_device is None:
                    # Prefer Yeti, then pulse, then default
                    if "yeti" in name.lower():
                        default_device = i
                        print(f"      ^ Auto-detected Yeti microphone")
                    elif default_device is None and "pulse" in name.lower():
                        default_device = i

        if default_device is None:
            default_device = p.get_default_input_device_info()["index"]
            print(f"\n[Using default device {default_device}]")
        else:
            device_name = p.get_device_info_by_index(default_device)["name"]
            print(f"\n[Selected] Device {default_device}: {device_name}")

        # Check what sample rates the device supports
        device_info = p.get_device_info_by_index(default_device)
        native_rate = int(device_info["defaultSampleRate"])

        # Try to use 16kHz if supported, otherwise use native rate and resample
        test_rates = [16000, 44100, 48000]
        supported_rate = native_rate

        for rate in test_rates:
            try:
                if p.is_format_supported(
                    rate,
                    input_device=default_device,
                    input_channels=self.channels,
                    input_format=self.audio_format,
                ):
                    supported_rate = rate
                    if rate == self.target_rate:
                        break
            except:
                pass

        self.native_rate = supported_rate
        need_resample = self.native_rate != self.target_rate

        print(f"\n[Audio] Opening device {default_device} at {self.native_rate} Hz")
        if need_resample:
            print(
                f"[Resampling] {self.native_rate} Hz → {self.target_rate} Hz for WhisperLiveKit"
            )

        stream = p.open(
            format=self.audio_format,
            channels=self.channels,
            rate=self.native_rate,
            input=True,
            input_device_index=default_device,
            frames_per_buffer=self.chunk,
        )

        print("\n[Ready] Speak now - text will be typed automatically!")
        print("[Control] Press Ctrl+C to stop")
        print("[Audio Meter] Watch for bars when speaking\n")

        try:
            import numpy as np
            from scipy import signal

            chunk_count = 0
            need_resample = self.native_rate != self.target_rate
            total_bytes_sent = 0
            total_chunks = 0

            while True:
                audio_data = stream.read(self.chunk, exception_on_overflow=False)
                audio_array = np.frombuffer(audio_data, dtype=np.int16)

                # Resample if needed
                if need_resample:
                    # Calculate resampling ratio
                    num_samples = int(
                        len(audio_array) * self.target_rate / self.native_rate
                    )
                    # Use scipy's resample for high-quality resampling
                    resampled = signal.resample(audio_array, num_samples)
                    # Convert back to int16
                    resampled = np.clip(resampled, -32768, 32767).astype(np.int16)
                    audio_to_send = resampled.tobytes()
                else:
                    audio_to_send = audio_data

                # Debug: Check if we're sending data
                if total_chunks < 5:
                    print(
                        f"\n[Debug] Chunk {total_chunks}: {len(audio_to_send)} bytes, volume: {np.abs(audio_array).mean():.0f}"
                    )

                total_bytes_sent += len(audio_to_send)
                total_chunks += 1

                await websocket.send(audio_to_send)

                # Show audio level every 10 chunks (~0.1 seconds)
                chunk_count += 1
                if chunk_count % 10 == 0:
                    volume = np.abs(audio_array).mean()
                    # Scale to 20 bars
                    bars = int((volume / 3000) * 20)
                    bars = min(bars, 20)
                    meter = "█" * bars + "░" * (20 - bars)
                    print(f"\r[Audio] {meter} {volume:>5.0f}", end="", flush=True)

                await asyncio.sleep(0.01)
        except KeyboardInterrupt:
            print(f"\n[Stopping] Finalizing transcription...")
            print(f"[Stats] Sent {total_chunks} chunks, {total_bytes_sent} bytes total")
        finally:
            stream.stop_stream()
            stream.close()
            p.terminate()
            # Send empty message to signal end
            print("[Sending] Empty message to stop server")
            await websocket.send(b"")

    async def receive_and_type(self, websocket):
        """Receive transcriptions and type them automatically"""
        try:
            async for message in websocket:
                data = json.loads(message)
                msg_type = data.get("type")

                if msg_type == "transcript":
                    text = data.get("text", "").strip()
                    if text:
                        self._type_final_text(text)

                elif msg_type == "partial":
                    partial = data.get("text", "")
                    if partial:
                        print(f"[Preview] {partial}", end="\r")

                elif msg_type == "ready_to_stop":
                    print("\n\n[Complete] Transcription finished!")
                    print(f"[Total Text] {self.accumulated_text.strip()}")
                    break

                elif "lines" in data:
                    lines = data.get("lines") or []
                    buffer_transcription = (data.get("buffer_transcription") or "").strip()
                    status = data.get("status", "")

                    final_lines = [
                        (line.get("text") or "").strip()
                        for line in lines
                        if (line.get("text") or "").strip()
                    ]
                    final_text = "\n".join(final_lines).strip()

                    if buffer_transcription:
                        preview = " ".join(
                            part
                            for part in [final_text, buffer_transcription]
                            if part
                        ).strip()
                        if preview:
                            print(f"[Preview] {preview} …", end="\r")
                        continue

                    if status != "no_audio_detected" and final_text:
                        self._type_final_text(final_text)

        except websockets.exceptions.ConnectionClosed:
            print("[WebSocket] Connection closed")

    async def run(self):
        """Main client loop"""
        print(f"[Connecting] {self.whisper_url}")
        print(
            f"[Mode] Auto-type using {'wtype (Wayland)' if self.use_wtype else 'xdotool (X11)'}\n"
        )

        async with websockets.connect(self.whisper_url) as websocket:
            await asyncio.gather(
                self.audio_stream(websocket), self.receive_and_type(websocket)
            )


def main():
    parser = argparse.ArgumentParser(
        description="WhisperLiveKit Auto-Type Client - Types transcriptions into active window"
    )
    parser.add_argument(
        "--whisper-url",
        default="ws://localhost:8888/asr",
        help="WhisperLiveKit WebSocket URL (default: ws://localhost:8888/asr)",
    )
    parser.add_argument(
        "--use-xdotool",
        action="store_true",
        help="Use xdotool instead of wtype (for X11 instead of Wayland)",
    )
    parser.add_argument(
        "--remote", help="Connect to remote server (e.g., --remote whisper.delo.sh)"
    )
    parser.add_argument(
        "--device", type=int, help="Audio input device index (see device list on start)"
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="List audio devices and exit"
    )
    parser.add_argument(
        "--send-to-n8n", action="store_true", help="Also send transcriptions to n8n webhook"
    )

    args = parser.parse_args()

    # List devices and exit
    if args.list_devices:
        p = pyaudio.PyAudio()
        print("[Audio Input Devices]")
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                print(f"  [{i}] {info['name']}")
        p.terminate()
        return

    # Handle remote server shorthand
    whisper_url = args.whisper_url
    if args.remote:
        protocol = "wss" if "." in args.remote else "ws"  # Use wss for domain names
        whisper_url = f"{protocol}://{args.remote}/asr"

    client = WhisperAutoTypeClient(
        whisper_url=whisper_url,
        use_wtype=not args.use_xdotool,
        device_index=args.device,
        send_to_n8n=args.send_to_n8n,
    )

    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        print("\n[Exit] Stopped by user")


if __name__ == "__main__":
    main()
