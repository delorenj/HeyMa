"""Canonical filesystem layout for Wax inside the HeyMa project root.

Everything lives under one root so `renameat2` between stream/ and inbox/ is a
genuine same-filesystem atomic rename (verified: dev=66306 for all of them).
"""

import os
from pathlib import Path

HOME = Path(os.path.expanduser("~"))

HEYMA = Path(
    os.environ.get("WAX_ROOT")
    or os.environ.get("WAX_AUDIO_ROOT")
    or HOME / "HeyMa"
)
# Keep the original public name for compatibility with existing Wax modules.
# WAX_AUDIO_ROOT remains a supported legacy override; new deployments should
# use WAX_ROOT.
AUDIO = HEYMA

STREAM = AUDIO / "stream"        # in-flight capture; the "event of recording"
INBOX = AUDIO / "inbox"          # the ONE inbox; post-recording pipeline
DROPOFF = AUDIO / "dropoff"      # Syncthing receiveonly device feed; we only READ
ARCHIVE = AUDIO / "archive"      # audio parked after S3 verify
QUARANTINE = AUDIO / "quarantine"
RECOVERED = AUDIO / "recovered"  # salvage + S3-failure stash
VAR = AUDIO / "var"

LOCK = VAR / "waxd.lock"
SOCK = VAR / "waxd.sock"
DB = VAR / "wax.db"
STATE_JSON = VAR / "state.json"
LOGS = VAR / "logs"

VAULT = Path(os.environ.get("WAX_VAULT", HOME / "d" / "Transcripts"))

# Free-space floor below which we refuse to start a capture. A long meeting can
# run for hours; running out of disk mid-recording is the one failure that
# produces an unrecoverable partial.
MIN_FREE_BYTES = int(os.environ.get("WAX_MIN_FREE_BYTES", 5 * 1024**3))

ALL_DIRS = (STREAM, INBOX, ARCHIVE, QUARANTINE, RECOVERED, VAR, LOGS)


def ensure_dirs() -> None:
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


# Capture is written as a sequence of closed segments rather than one long
# file. Measured: the Ogg muxer buffers 256 KiB before its first disk write
# (~46 s at 32 kbps), so a single-file capture loses everything up to that point
# on a SIGKILL/OOM/power loss. Each completed segment gets its own trailer and
# is independently valid, so a hard kill costs at most the in-flight segment.
SEGMENT_SECONDS = int(os.environ.get("WAX_SEGMENT_SECONDS", "60"))


def segdir(rid: str) -> Path:
    return STREAM / f"{rid}.segs"


def partial_path(rid: str) -> Path:
    """Staging path for the concatenated result during finalize (transient)."""
    return STREAM / f"{rid}.ogg.partial"


def rec_path(rid: str) -> Path:
    return STREAM / f"{rid}.rec.json"


def stop_path(rid: str) -> Path:
    return STREAM / f"{rid}.stop"


def fin_path(rid: str) -> Path:
    return STREAM / f"{rid}.fin.json"


def ctl_path(rid: str) -> Path:
    """FIFO on the encoder's stdin — the ONLY reliable way to stop it cleanly.

    Signals do not work: this ffmpeg build catches SIGINT/SIGTERM but does not
    act on them during a pulse capture (measured: still running after 15 s,
    0 bytes, unreadable output). Writing 'q' exits rc=0 in ~0.2 s with a valid
    file. A FIFO rather than an anonymous pipe so a restarted waxd — or a cold
    `wax` CLI that never owned the process — can still stop it.
    """
    return STREAM / f"{rid}.ctl"
