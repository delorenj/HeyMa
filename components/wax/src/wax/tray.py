"""Tray indicator: RED recording / GREEN ready / YELLOW something's wrong.

Two hard rules, both learned the expensive way:

1. The tray NEVER blocks recording. If the indicator cannot be created, we
   notify and carry on. A daemon that refuses to record because its icon failed
   to load is worse than a daemon with no icon.

2. The watcher comes and goes, so we re-register when it returns. GNOME
   disables extensions that don't declare `unlock-dialog` session mode while the
   screen is locked, which takes `org.kde.StatusNotifierWatcher` off the bus
   entirely — every extension reads INACTIVE and the icon vanishes. It comes
   back on unlock, and libappindicator does not always re-advertise itself, so
   we watch NameOwnerChanged and rebuild the indicator ourselves.

3. Rebuilding means DESTROYING the old indicator first. libappindicator exports
   the item on a fixed D-Bus path derived from its id ("wax"), so building a
   replacement while the previous object is still alive fails with "An object is
   already exported for the interface org.kde.StatusNotifierItem" — a
   libappindicator *warning*, not a Python exception, so the old code happily
   reported `registered: True` while the icon was gone for the rest of the
   session. `registered` is now verified against the watcher instead of assumed.
"""

import gc

import gi

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):  # pragma: no cover - fallback for older stacks
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator

from gi.repository import GLib, Gio, Gtk  # noqa: E402

from . import component

ICON_DIR = component.TRAY_ASSETS
WATCHER = "org.kde.StatusNotifierWatcher"

ICONS = {
    "green": str(ICON_DIR / "wax-tray-icon-green.png"),
    "red": str(ICON_DIR / "wax-tray-icon-red.png"),
    "yellow": str(ICON_DIR / "wax-tray-icon-yellow.png"),
}


def colour_for(snap: dict) -> tuple[str, str]:
    """Map a state snapshot to (colour, tooltip). Recording always wins.

    You must always be able to see that you are recording, even when the
    pipeline behind it is on fire.
    """
    stream = snap.get("stream") or {}
    inbox = snap.get("inbox") or {}
    s, i = stream.get("state"), inbox.get("state")

    tip = f"stream: {s}\ninbox: {i}"
    cause = stream.get("cause_code") or inbox.get("cause_code")
    if cause:
        tip += f"\ncause: {cause}"

    if s == "recording":
        return "red", tip
    if s in ("not-ready", "error-partial", "error") or i in ("error",) or (inbox.get("pending") or 0) and i != "ready-and-active":
        return "yellow", tip
    if i == "stopped":
        return "yellow", tip
    return "green", tip


class Tray:
    def __init__(self, on_toggle=None, on_quit=None, on_open=None):
        self.on_toggle, self.on_quit, self.on_open = on_toggle, on_quit, on_open
        self.indicator = None
        self.registered = False
        self._colour = None
        self._tip = ""
        self._build()
        self._watch_bus()

    # -- construction ----------------------------------------------------
    def _menu(self) -> Gtk.Menu:
        m = Gtk.Menu()
        self.item_toggle = Gtk.MenuItem(label="Start recording")
        self.item_toggle.connect("activate", lambda *_: self.on_toggle and self.on_toggle())
        m.append(self.item_toggle)

        self.item_status = Gtk.MenuItem(label="…")
        self.item_status.set_sensitive(False)
        m.append(self.item_status)

        m.append(Gtk.SeparatorMenuItem())
        op = Gtk.MenuItem(label="Open inbox")
        op.connect("activate", lambda *_: self.on_open and self.on_open())
        m.append(op)
        q = Gtk.MenuItem(label="Quit waxd")
        q.connect("activate", lambda *_: self.on_quit and self.on_quit())
        m.append(q)
        m.show_all()
        return m

    def _teardown(self) -> None:
        """Release the old indicator so its D-Bus path is free for the rebuild.

        The menu items hold lambdas that close over `self`, so the indicator sits
        in a reference cycle and plain refcounting will not finalize it. Without
        the explicit collect the unexport happens *after* the new indicator has
        already tried (and failed) to claim the path.
        """
        ind, self.indicator = self.indicator, None
        self.item_toggle = self.item_status = None
        if ind is not None:
            try:
                ind.set_status(AppIndicator.IndicatorStatus.PASSIVE)
            except Exception:  # noqa: BLE001 - teardown must never raise
                pass
        del ind
        gc.collect()

    def _build(self) -> None:
        self._teardown()
        try:
            self.indicator = AppIndicator.Indicator.new_with_path(
                "wax", ICONS["green"], AppIndicator.IndicatorCategory.APPLICATION_STATUS, str(ICON_DIR)
            )
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_title("Wax")
            self.indicator.set_menu(self._menu())
        except Exception as e:  # noqa: BLE001 - never fatal
            self.indicator, self.registered = None, False
            _notify("Wax: tray unavailable", f"{e}\nRecording still works; use `wax status`.")
            return
        # Registration is asynchronous and can fail without raising, so ask the
        # watcher rather than believing ourselves. Anything that reports a green
        # tray while no icon exists is worse than no status at all.
        self.registered = True
        GLib.timeout_add(1500, self._verify_registered)

    def _verify_registered(self) -> bool:
        """Confirm the watcher actually lists us; retry once if it does not."""
        listed = False
        try:
            bus = getattr(self, "_bus", None) or Gio.bus_get_sync(Gio.BusType.SESSION, None)
            items = bus.call_sync(
                WATCHER, "/StatusNotifierWatcher", "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (WATCHER, "RegisteredStatusNotifierItems")),
                GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 2000, None,
            )[0]
            listed = any("wax" in str(i) for i in items)
        except Exception:  # noqa: BLE001 - no watcher is a vanish, handled elsewhere
            return False
        self.registered = listed
        if not listed and not getattr(self, "_rebuilt_once", False):
            self._rebuilt_once = True
            self._build()
            if self._colour:
                self.set(self._colour, self._tip)
        elif listed:
            self._rebuilt_once = False
        return False

    # -- bus watching ----------------------------------------------------
    def _watch_bus(self) -> None:
        """Rebuild the indicator whenever the SNI watcher reappears."""
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception:  # noqa: BLE001
            return
        Gio.bus_watch_name_on_connection(
            self._bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
            lambda *_: GLib.idle_add(self._on_watcher_appeared),
            lambda *_: GLib.idle_add(self._on_watcher_vanished),
        )

    def _on_watcher_appeared(self, *_):
        if not self.registered or self.indicator is None:
            self._build()
            if self._colour:
                self.set(self._colour, self._tip)
        return False

    def _on_watcher_vanished(self, *_):
        # Screen lock, or gnome-shell reloading extensions. Not an error and not
        # worth notifying about — but we must drop the indicator NOW, while the
        # watcher is away, or the rebuild on return collides with it on the bus.
        self.registered = False
        self._teardown()
        return False

    # -- updates ---------------------------------------------------------
    def set(self, colour: str, tooltip: str = "", recording: bool = False) -> None:
        """Thread-safe icon update."""
        self._colour, self._tip = colour, tooltip

        def apply():
            if self.indicator is not None:
                try:
                    self.indicator.set_icon_full(ICONS.get(colour, ICONS["green"]), tooltip)
                except Exception:  # noqa: BLE001
                    self.registered = False
            if getattr(self, "item_status", None) is not None:
                self.item_status.set_label(tooltip.replace("\n", " · "))
            if getattr(self, "item_toggle", None) is not None:
                self.item_toggle.set_label("Stop recording" if recording else "Start recording")
            return False

        GLib.idle_add(apply)


def _notify(title: str, body: str) -> None:
    import subprocess
    subprocess.run(["notify-send", "-a", "Wax", "-u", "critical", title, body], check=False)
