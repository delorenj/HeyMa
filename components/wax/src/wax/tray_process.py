import os
import subprocess
import sys
import time

from . import component, paths


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
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
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
