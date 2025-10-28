#!/usr/bin/env python3
"""Check supported sample rates for audio devices"""

import pyaudio

p = pyaudio.PyAudio()

# Test rates
test_rates = [8000, 11025, 16000, 22050, 32000, 44100, 48000, 96000]

print("Testing Yeti Stereo Microphone (device 6):")
device_info = p.get_device_info_by_index(6)
print(f"Name: {device_info['name']}")
print(f"Max Input Channels: {device_info['maxInputChannels']}")
print(f"Default Sample Rate: {device_info['defaultSampleRate']}")

print("\nSupported sample rates:")
for rate in test_rates:
    try:
        if p.is_format_supported(
            rate,
            input_device=6,
            input_channels=1,
            input_format=pyaudio.paInt16
        ):
            print(f"  ✓ {rate} Hz")
    except ValueError:
        print(f"  ✗ {rate} Hz")

p.terminate()
