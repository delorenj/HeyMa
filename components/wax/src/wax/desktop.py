"""Best-effort desktop feedback for long-running transcription work."""

import shutil
import subprocess


SOUNDS = {"start": "message", "complete": "complete"}


def ding(event: str) -> bool:
    """Play a desktop event sound without ever blocking or failing the worker."""
    sound = SOUNDS.get(event, "message")
    player = shutil.which("canberra-gtk-play")
    if not player:
        return False
    try:
        subprocess.Popen(
            [player, "--id", sound, "--description", f"Wax transcription {event}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False
