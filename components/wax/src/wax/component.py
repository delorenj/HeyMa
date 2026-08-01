"""Locations for Wax's installed source component, independent of the runtime root."""

import os
from pathlib import Path


def _component_root() -> Path:
    override = os.environ.get("WAX_COMPONENT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("cannot locate Wax component root (pyproject.toml not found)")


ROOT = _component_root()
ASSETS = ROOT / "assets"
TRAY_ASSETS = ASSETS / "tray"
CONFIG = ROOT / "config"
PASSES = CONFIG / "passes.d"
