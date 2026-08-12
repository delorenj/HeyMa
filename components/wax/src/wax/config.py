"""Runtime policy configuration for Wax."""

import os
import re


def _bytes(value: str) -> int:
    """Parse bytes or a compact decimal/binary size such as 300MB or 300MiB."""
    match = re.fullmatch(r"\s*(\d+)\s*(B|KB|MB|GB|KIB|MIB|GIB)?\s*", value, re.IGNORECASE)
    if not match:
        raise ValueError(f"invalid byte size: {value!r}")
    number = int(match.group(1))
    unit = (match.group(2) or "B").upper()
    multiplier = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
    }[unit]
    return number * multiplier


def max_audio_file_size_for_transcription() -> int:
    """Exclusive transcription ceiling, configurable through the environment."""
    return _bytes(os.environ.get("MAX_AUDIO_FILE_SIZE_FOR_TRANSCRIPTION", "300MB"))


def transcription_size_allowed(size: int) -> bool:
    # "300MB+" is blocked: a file exactly at the configured ceiling is too large.
    return size < max_audio_file_size_for_transcription()
