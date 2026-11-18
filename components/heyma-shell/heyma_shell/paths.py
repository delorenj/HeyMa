"""Where things are, resolved from this file (PATH never decides which code runs)."""

from __future__ import annotations

import os
from pathlib import Path

# components/heyma-shell/heyma_shell/paths.py -> the HeyMa checkout
ROOT = Path(os.environ.get("HEYMA_ROOT") or Path(__file__).resolve().parents[3])
WAX_SRC = ROOT / "components" / "wax" / "src"


def wax_root(environ=None, home: Path | None = None) -> Path:
    """The Wax runtime root (`var/`, `inbox/`); the checkout by default, like wax.paths."""
    env = environ if environ is not None else os.environ
    return Path(env.get("WAX_ROOT") or env.get("WAX_AUDIO_ROOT") or (home or Path.home()) / "HeyMa")


def vinyl_state_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".local" / "state" / "vinyl" / "state.json"
