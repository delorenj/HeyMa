import os
import subprocess
import sys
import time
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


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class TrayProcess:
    def __init__(self):
        self.child = None
        self.next_attempt = 0
        self.available = False
        self.reason = "not started"

    def poll(self):
        if self.child is not None:
            if self.child.poll() is None:
                self.available = True
                return
            self.reason = f"tray child exited {self.child.returncode}"
            self.child = None
            self.available = False
            self.next_attempt = time.monotonic() + 10
        if time.monotonic() < self.next_attempt:
            return
        if not _has_display():
            # Into os.environ, not just the child's env: xdg-open and the
            # silence alarm are waxd children that need the display too.
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
    from . import tray
    tray._load_gtk()
    if not tray.Gtk.init_check()[0]:
        return 1
    cli = component.ROOT / "bin" / "wax"

    def call(*args):
        subprocess.Popen([str(cli), *args])

    indicator = tray.Tray(
        on_toggle=lambda: call("rec", "toggle"),
        on_quit=tray.Gtk.main_quit,
        on_open=lambda: subprocess.Popen(["xdg-open", str(paths.INBOX)]),
        on_skip=lambda item: call("skip", item),
        on_retry_item=lambda item: call("retry", item),
        on_retry_passes=lambda item, slugs: call("ep", "run-selected", item, *slugs),
        on_clear_completed=lambda: call("queue", "--clear-completed"),
        on_open_transcript=lambda md: subprocess.Popen(["xdg-open", tray.obsidian_uri(md)]),
    )

    def update():
        try:
            snap = json.loads(paths.STATE_JSON.read_text())
            colour, tip = tray.colour_for(snap)
            indicator.set(colour, tip, (snap.get("stream") or {}).get("state") == "recording")
            indicator.set_queue(snap.get("tray_items") or [])
        except (OSError, ValueError):
            pass
        return True

    tray.GLib.timeout_add(1000, update)
    update()
    tray.Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
