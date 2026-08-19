"""Best-effort desktop feedback for long-running transcription work.

Also the home of the *failure* channel, and deliberately so: tray.py imports
`gi` at module scope and mise's python3 (3.11.13, first on PATH) has no gi, so a
worker thread that wanted to raise an alarm "through the tray" could not import
the module outside the daemon. Nothing below touches a display server.
"""

import logging
import shutil
import subprocess
import threading

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

# `failed` is the sound the machine owed you for a week. `complete` fires at the
# end of transcription — BEFORE the enrichment passes run — so eleven consecutive
# title-slug failures were each announced with an affirmative chime. dialog-error
# is present in /usr/share/sounds/freedesktop/stereo on this box (verified) and
# is the freedesktop-standard negative event.
SOUNDS = {"start": "message", "complete": "complete", "failed": "dialog-error"}

# A dead provider fails on EVERY item, so the interesting event is the FIRST
# failure of a given kind, not the fortieth. Notify once per distinct
# (slug, reason_code) and stay quiet until clear_stage_failure() re-arms it.
_NOTIFIED: set[tuple[str, str]] = set()
_NOTIFY_LOCK = threading.Lock()


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


def notify(title: str, body: str, *, urgency: str = "critical") -> bool:
    """Raise a desktop notification. Never raises: the caller is mid-failure.

    A swallowed notification is still logged, because a headless session is
    exactly where "nothing reached the desktop" must not also mean "nothing
    reached the journal".
    """
    if not shutil.which("notify-send"):
        log.warning("notify-send absent, unreported: %s — %s", title, body.replace("\n", " · "))
        return False
    try:
        subprocess.run(["notify-send", "-a", "Wax", "-u", urgency, title, body], check=False)
    except OSError as e:
        log.warning("notify-send failed: %s: %s", type(e).__name__, e)
        return False
    return True


def notify_stage_failure(slug: str, reason_code: str, *, item: str = "",
                         detail: str = "") -> bool:
    """Announce a failed pipeline sub-stage on the desktop, once per kind.

    Returns True only when a notification was actually raised, so a caller can
    tell "told the user" from "already told them". `detail` is truncated to the
    same 300 chars contract D writes into the note.
    """
    key = (str(slug or "?"), str(reason_code or "unknown"))
    with _NOTIFY_LOCK:
        if key in _NOTIFIED:
            log.info("stage failure repeats, notification suppressed: slug=%s reason_code=%s",
                     key[0], key[1])
            return False
        _NOTIFIED.add(key)
    body = key[1]
    if item:
        body += f" · {item}"
    if detail:
        body += "\n" + detail.strip()[:300]
    log.warning("stage failed: slug=%s reason_code=%s item=%s", key[0], key[1], item or "-")
    return notify(f"Wax: {key[0]} failed", body)


def clear_stage_failure(slug: str | None = None) -> None:
    """Re-arm the desktop alarm after a recovery — all slugs when None.

    Without this a provider that breaks, gets fixed, and breaks again a month
    later would fail in total silence for the rest of the daemon's life.
    """
    with _NOTIFY_LOCK:
        if slug is None:
            _NOTIFIED.clear()
            return
        for key in [k for k in _NOTIFIED if k[0] == slug]:
            _NOTIFIED.discard(key)
