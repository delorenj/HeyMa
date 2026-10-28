"""Configuration for the `transcriptions` store (design D.9).

Every knob is read from the process environment first and then from the
`Environment=` lines of the INSTALLED waxd unit, so the CLI and the daemon share
one source of truth without a credential file ever being written. The unit only
ever carries `op://` references and a password-less DSN; passwords are resolved
at runtime by `db.py`.

The embedding model name, digest and dimensions are NOT knobs: they are code
constants in `embed.py`, checked against the active `tx.embedding_model` row,
because changing them without a migration would corrupt the index.
"""

from __future__ import annotations

import os
import shlex
import threading
from pathlib import Path
from typing import Optional

# Tests patch this; the default is where deploy/install-systemd-user installs it.
UNIT_PATH = Path(os.path.expanduser("~/.config/systemd/user/waxd.service"))
# Overrides UNIT_PATH. The test suite's conftest points it at a path that does not exist so that no
# test (nor a `bin/wax` subprocess one spawns) can pick up the INSTALLED unit's DSN and project
# fixtures into the real, append-only store.
UNIT_FILE_ENV = "WAX_TRANSCRIPTS_UNIT_FILE"

_lock = threading.Lock()
_unit_cache: dict[str, object] = {}

_OFF = {"0", "false", "no", "off"}


def unit_path() -> Path:
    override = os.environ.get(UNIT_FILE_ENV, "").strip()
    return Path(os.path.expanduser(override)) if override else Path(UNIT_PATH)


def _unit_env() -> dict[str, str]:
    """The installed unit's Environment= assignments, re-read when the file changes."""
    path = unit_path()
    try:
        st = path.stat()
    except OSError:
        return {}
    stamp = (str(path), st.st_mtime_ns, st.st_size)
    with _lock:
        if _unit_cache.get("stamp") == stamp:
            return dict(_unit_cache.get("env") or {})
        env: dict[str, str] = {}
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line.startswith("Environment="):
                continue
            try:
                words = shlex.split(line[len("Environment="):])
            except ValueError:
                continue
            for word in words:
                key, sep, value = word.partition("=")
                if sep and key:
                    env[key] = value
        _unit_cache["stamp"] = stamp
        _unit_cache["env"] = env
        return dict(env)


def value(name: str, default: Optional[str] = None) -> Optional[str]:
    """`$name` from the environment, else the installed unit, else `default`."""
    direct = os.environ.get(name)
    if direct is not None and direct.strip() != "":
        return direct.strip()
    unit = _unit_env().get(name)
    if unit is not None and unit.strip() != "":
        return unit.strip()
    return default


def _number(name: str, default: float, minimum: float = 0.0) -> float:
    raw = value(name)
    if raw is None:
        return default
    try:
        number = float(raw)
    except ValueError:
        return default
    return number if number >= minimum else default


def enabled() -> bool:
    """`WAX_TRANSCRIPTS=0` disables capture and projection entirely (rollback switch)."""
    return (value("WAX_TRANSCRIPTS", "1") or "1").lower() not in _OFF


def dsn() -> Optional[str]:
    """Password-less libpq URI for the app role; unset means capture-only."""
    return value("WAX_TRANSCRIPTS_DSN")


def password_op() -> Optional[str]:
    return value("WAX_TRANSCRIPTS_PASSWORD_OP")


def owner_dsn() -> Optional[str]:
    return value("WAX_TRANSCRIPTS_OWNER_DSN")


def owner_password_op() -> Optional[str]:
    return value("WAX_TRANSCRIPTS_OWNER_PASSWORD_OP")


def tick_s() -> float:
    return _number("WAX_TRANSCRIPTS_TICK_S", 15.0, minimum=1.0)


def reconcile_interval_s() -> float:
    return _number("WAX_TRANSCRIPTS_RECONCILE_INTERVAL_S", 600.0)


def sync_breaker_s() -> float:
    return _number("WAX_TRANSCRIPTS_SYNC_BREAKER_S", 300.0)


def lag_fail_s() -> float:
    return _number("WAX_TRANSCRIPTS_LAG_FAIL_S", 3600.0, minimum=1.0)


# Oldest queued snapshot older than this reads `lagging` (design D.8).
LAG_WARN_S = 600.0
# A chunk this old is embedded even if its body has not settled (design D.5).
EMBED_FORCE_AFTER_S = 6 * 3600.0
# Doctor warns when the oldest unembedded chunk is older than this (design D.8).
EMBED_STALE_WARN_S = 3600.0


def local_tz_name() -> str:
    return value("WAX_TRANSCRIPTS_LOCAL_TZ", "America/New_York") or "America/New_York"


def local_tz():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(local_tz_name())
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("America/New_York")


def embed_url() -> str:
    return (value("WAX_EMBED_URL", "http://localhost:11434") or "http://localhost:11434").rstrip("/")


def embed_batch() -> int:
    return max(1, min(8, int(_number("WAX_EMBED_BATCH", 8.0, minimum=1.0))))


def embed_keep_alive() -> str:
    # Short on purpose (design G.7): the 5.3 GB embedder shares the GPU with whisper, whose 5,000 MB
    # free-VRAM floor it broke while resident for D.9's original 30m. 2m keeps it warm across
    # back-to-back 15 s ticks and a run of searches, then hands the VRAM back.
    return value("WAX_EMBED_KEEP_ALIVE", "2m") or "2m"


def embed_timeout_s() -> float:
    return _number("WAX_EMBED_TIMEOUT_S", 60.0, minimum=1.0)


def s3_endpoint() -> Optional[str]:
    return value("WAX_S3_ENDPOINT")
