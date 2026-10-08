import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("mac_audio", Path(__file__).parents[1] / "macos/ssh_big_chungus.py")
mac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mac)


def test_coreaudio_defaults_win_over_pulseaudio_virtual_music_input():
    devices = {"SPAudioDataType": [{"_items": [
        {"_name": "Background Music"},
        {"_name": "MacBook Air Microphone", "coreaudio_default_audio_input_device": "spaudio_yes"},
        {"_name": "MacBook Air Speakers", "coreaudio_default_audio_output_device": "spaudio_yes"},
    ]}]}
    pulse = {"source": [{"name": "music", "description": "Background Music"},
                        {"name": "1", "description": "MacBook Air Microphone"}],
             "sink": [{"name": "1__2", "description": "MacBook Air Speakers"}]}
    assert mac.select_devices(devices, pulse) == {"source": "1", "sink": "1__2"}


def test_missing_os_default_is_reported_instead_of_capturing_another_device():
    with pytest.raises(RuntimeError, match="selected input"):
        mac.select_devices({"SPAudioDataType": [{"_items": []}]}, {"source": [], "sink": []})
