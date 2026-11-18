import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import component, paths

SESSION_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_CURRENT_DESKTOP",
                "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS")


def session_env() -> dict:
    """Display variables of the graphical session as it is NOW.

    waxd's own environment is frozen at exec. When the session starts waxd in
    the same breath as the compositor (an autologin, a GDM restart), the user
    manager has not imported DISPLAY/WAYLAND_DISPLAY yet, and gating on
    os.environ alone left the tray at "no display" for the daemon's whole life
    without a word in the journal (2026-10-09: 10 h with no icon). The session
    publishes them into the manager's environment, so ask the manager on every
    attempt; fall back to a Wayland socket in this user's runtime dir. (Not
    /tmp/.X11-unix: it is shared with the greeter, and a wrong DISPLAY pinned
    into os.environ would never be re-read.)
    """
    env = {}
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                             text=True, timeout=2, check=True).stdout
        for line in out.splitlines():
            key, sep, value = line.partition("=")
            if sep and value and key in SESSION_KEYS:
                env[key] = value
    except (OSError, subprocess.SubprocessError):
        pass
    if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        sockets = sorted(p.name for p in Path(runtime).glob("wayland-*")
                         if not p.name.endswith(".lock")) if runtime else []
        if sockets:
            env["WAYLAND_DISPLAY"] = sockets[0]
    return env


OFF_VALUES = ("off", "0", "false", "no", "external", "heyma")
EXTERNAL_REASON = "external: the HeyMa tray owns the icon (WAX_TRAY=off)"
# The unit that owns the icon when WAX_TRAY=off. The installer writes the drop-in
# and this unit together, so the unit's state is the owner's liveness.
OWNER_UNIT = "heyma-tray.service"
OWNER_LIVE = ("active", "activating", "reloading")  # activating covers Restart= back-off
OWNER_CHECK_S = 5.0
# Consecutive "not running" answers before waxd takes the icon back: one missed
# check during a login or a tray restart must not flash a second icon.
OWNER_GRACE = 2


def tray_enabled(environ=None) -> bool:
    """Does waxd spawn its own tray child?

    Default yes, so a host without the HeyMa shell keeps its Wax tray. A host
    that runs `heyma-tray.service` sets WAX_TRAY=off in a waxd drop-in (written
    by deploy/install-systemd-user), because two icons for one product family is
    the thing the shell exists to remove. WAX_TRAY=off means "the HeyMa tray owns
    the icon", not "no icon": while that unit is not running, waxd shows its own.
    """
    value = (environ if environ is not None else os.environ).get("WAX_TRAY", "")
    return value.strip().lower() not in OFF_VALUES


def owner_state(unit: str = OWNER_UNIT) -> str:
    """`systemctl --user is-active <unit>`, or "unknown" when systemd cannot answer."""
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True,
                             text=True, timeout=2, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class TrayProcess:
    def __init__(self):
        self.child = None
        self.next_attempt = 0
        self.available = False
        self.reason = "not started"
        self.owner = None           # last answer from owner_state()
        self.owner_missing = 0      # consecutive "not running" answers
        self.owner_next_check = 0.0

    def external_owner_live(self) -> bool:
        """Is the HeyMa tray (the icon's owner under WAX_TRAY=off) running?

        Asked every OWNER_CHECK_S, not every tick. An "unknown" answer (no user
        manager, a timeout) changes nothing, so a slow systemctl cannot flap
        the icon. Before the first answer the owner is assumed live.
        """
        now = time.monotonic()
        if now >= self.owner_next_check:
            self.owner_next_check = now + OWNER_CHECK_S
            state = owner_state()
            if state in OWNER_LIVE:
                self.owner_missing = 0
            elif state != "unknown":
                self.owner_missing += 1
            self.owner = state
        return self.owner_missing < OWNER_GRACE

    def poll(self):
        fallback = None
        if not tray_enabled():
            if self.external_owner_live():
                # Someone else owns the icon. Report that as healthy: waxd's
                # `tray_unavailable` condition means "the icon is missing", and
                # it is not.
                if self.child is not None:
                    self.stop()
                    self.child = None
                self.available = True
                self.reason = EXTERNAL_REASON
                return
            # The owner is gone (Quit HeyMa, a disabled or failed unit). Nobody
            # owns the icon, and a recorder with no indicator is the one state
            # this switch must never produce: show waxd's own tray until the
            # owner is back, then hand the icon over again.
            fallback = f"fallback: {OWNER_UNIT} is {self.owner}, so waxd shows its own tray"
        if self.child is not None:
            if self.child.poll() is None:
                self.available = True
                if fallback:
                    self.reason = fallback
                return
            self.reason = f"tray child exited {self.child.returncode}"
            self.child = None
            self.available = False
            self.next_attempt = time.monotonic() + 10
        if time.monotonic() < self.next_attempt:
            return
        if not _has_display():
            # Into os.environ, not just the child's env: xdg-open and
            # notify-send are waxd children that need the display too.
            for key, value in session_env().items():
                os.environ.setdefault(key, value)
        if not _has_display():
            self.reason = "no display"
            self.next_attempt = time.monotonic() + 10
            return
        try:
            self.child = subprocess.Popen([sys.executable, "-m", "wax.tray_process"],
                                          env={**os.environ, "PYTHONPATH": str(component.ROOT / "src")})
            self.reason = "starting"
        except OSError as exc:
            self.reason = str(exc)
            self.next_attempt = time.monotonic() + 10

    def stop(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()


def main():
    import json
    from . import tray, telephony_presentation
    tray._load_gtk()
    if not tray.Gtk.init_check()[0]:
        return 1
    cli = component.ROOT / "bin" / "wax"

    def call(*args):
        subprocess.Popen([str(cli), *args])

    def open_page(page):
        # GTK4 window in a separate process from GTK3 libappindicator.
        subprocess.Popen([sys.executable, "-m", "wax.heyma_window", "--page", page],
                         env={**os.environ, "PYTHONPATH": str(component.ROOT / "src")})

    ringer = telephony_presentation.Ringer(
        telephony_presentation.RingPlayer(), datetime.now(timezone.utc))

    indicator = tray.Tray(
        on_toggle=lambda: call("rec", "toggle"),
        on_quit=tray.Gtk.main_quit,
        on_open=lambda: subprocess.Popen(["xdg-open", str(paths.INBOX)]),
        on_skip=lambda item: call("skip", item),
        on_retry_item=lambda item: call("retry", item),
        on_retry_passes=lambda item, slugs: call("ep", "run-selected", item, *slugs),
        on_clear_completed=lambda: call("queue", "--clear-completed"),
        on_open_transcript=lambda md: subprocess.Popen(["xdg-open", tray.obsidian_uri(md)]),
        on_settings=lambda: open_page("settings"),
        on_telephony=lambda: open_page("telephony"),
        on_silence=ringer.silence,
    )

    def update():
        try:
            snap = json.loads(paths.STATE_JSON.read_text())
            colour, tip = tray.colour_for(snap)
            indicator.set(colour, tip, (snap.get("stream") or {}).get("state") == "recording",
                          flash=tray.flash_for(snap))
            indicator.set_queue(snap.get("tray_items") or [])
            ringer.tick(snap.get("telephony"), datetime.now(timezone.utc))
        except (OSError, ValueError, TypeError):
            ringer.tick(None, datetime.now(timezone.utc))
        return True

    tray.GLib.timeout_add(1000, update)
    update()
    try:
        tray.Gtk.main()
    finally:
        ringer.player.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
