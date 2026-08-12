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
from pathlib import Path
from urllib.parse import quote

import gi

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):  # pragma: no cover - fallback for older stacks
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator

from gi.repository import GLib, Gio, Gtk  # noqa: E402

from . import component  # noqa: E402

ICON_DIR = component.TRAY_ASSETS
WATCHER = "org.kde.StatusNotifierWatcher"

ICONS = {
    "green": str(ICON_DIR / "wax-tray-icon-green.png"),
    "red": str(ICON_DIR / "wax-tray-icon-red.png"),
    "yellow": str(ICON_DIR / "wax-tray-icon-yellow.png"),
}
SPINNER = ("◐", "◓", "◑", "◒")
STAGE_VERBS = {
    "claimed": "Starting",
    "archive": "Uploading",
    "preparing": "Preparing",
    "transcribe": "Transcribing",
    "diarize": "Diarizing",
    "finalize": "Finalizing",
    "enrich": "Enriching",
    "park": "Finishing",
}


def format_bytes(value) -> str:
    """Compact binary size for queue rows."""
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "?"


def queue_label(item: dict, spinner_frame: int = 0) -> str:
    if item.get("active"):
        icon = SPINNER[spinner_frame % len(SPINNER)]
    else:
        if item.get("state") in ("failed", "suspect"):
            icon = "⚠"
        else:
            icon = "✓" if item.get("state") == "complete" else "•"
    detail = format_bytes(item.get("bytes"))
    if item.get("duration_s"):
        seconds = int(item["duration_s"])
        detail = f"{seconds // 60}:{seconds % 60:02d} · {detail}"
    stage = STAGE_VERBS.get(item.get("stage"), item.get("stage") or "")
    if item.get("active") and item.get("progress_pct") is not None:
        stage = f"{stage} {int(item['progress_pct'])}%"
    if item.get("state") in ("failed", "suspect"):
        prefix = f"{item['state'].title()}: "
    else:
        prefix = f"{stage}: " if item.get("active") and stage else ""
    display_name = item.get("orig_name") or item.get("item_id")
    if item.get("state") == "complete" and item.get("md_path"):
        display_name = Path(item["md_path"]).name
    return f"{icon} {prefix}{display_name}  ({detail})"


def obsidian_uri(path: str) -> str:
    """Obsidian's absolute-path URI, escaped without losing path separators."""
    return "obsidian://open?path=" + quote(str(Path(path).resolve()), safe="/")


def colour_for(snap: dict) -> tuple[str, str]:
    """Map a state snapshot to (colour, tooltip). Recording always wins.

    You must always be able to see that you are recording, even when the
    pipeline behind it is on fire.
    """
    stream = snap.get("stream") or {}
    inbox = snap.get("inbox") or {}
    s, i = stream.get("state"), inbox.get("state")

    tip = f"stream: {s}\ninbox: {i}"
    # Use failures physically present in the actionable tray queue. Historical
    # ledger failures are audit history and must not keep the icon yellow.
    failed = (snap.get("queue") or {}).get("failed", 0)
    cause = stream.get("cause_code") or inbox.get("cause_code")
    if cause:
        tip += f"\ncause: {cause}"
    if failed:
        tip += f"\nfailed items: {failed}"

    if s == "recording":
        return "red", tip
    if failed or s in ("not-ready", "error-partial", "error") or i in ("error",) or (inbox.get("pending") or 0) and i != "ready-and-active":
        return "yellow", tip
    if i == "stopped":
        return "yellow", tip
    return "green", tip


class Tray:
    def __init__(self, on_toggle=None, on_quit=None, on_open=None,
                 on_skip=None, on_clear_completed=None, on_open_transcript=None):
        self.on_toggle, self.on_quit, self.on_open = on_toggle, on_quit, on_open
        self.on_skip, self.on_clear_completed = on_skip, on_clear_completed
        self.on_open_transcript = on_open_transcript
        self.indicator = None
        self.registered = False
        self._colour = None
        self._tip = ""
        self._queue_items = []
        self._queue_signature = None
        self._queue_rows = {}
        self._spinner_frame = 0
        self._build()
        self._watch_bus()
        GLib.timeout_add(250, self._animate_queue)

    # -- construction ----------------------------------------------------
    def _menu(self) -> Gtk.Menu:
        m = Gtk.Menu()
        self.item_toggle = Gtk.MenuItem(label="Start recording")
        self.item_toggle.connect("activate", lambda *_: self.on_toggle and self.on_toggle())
        m.append(self.item_toggle)

        self.item_status = Gtk.MenuItem(label="…")
        self.item_status.set_sensitive(False)
        m.append(self.item_status)

        self.item_queue = Gtk.MenuItem(label="Queue")
        self.queue_menu = Gtk.Menu()
        self.item_queue.set_submenu(self.queue_menu)
        m.append(self.item_queue)
        self._render_queue()

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
        self.item_toggle = self.item_status = self.item_queue = self.queue_menu = None
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
    def _render_queue(self) -> None:
        menu = getattr(self, "queue_menu", None)
        if menu is None:
            return
        for child in menu.get_children():
            menu.remove(child)
        errors = [x for x in self._queue_items if x.get("state") in ("failed", "suspect")]
        queued = [x for x in self._queue_items
                  if x.get("state") not in ("complete", "failed", "suspect")]
        completed = [x for x in self._queue_items if x.get("state") == "complete"]
        active = next((x for x in queued if x.get("active")), None)
        if getattr(self, "item_queue", None) is not None:
            stage = STAGE_VERBS.get((active or {}).get("stage"), "Working") if active else "Idle"
            if active and active.get("progress_pct") is not None:
                stage = f"{stage} {int(active['progress_pct'])}%"
            suffix = f" · {len(errors)} failed" if errors else ""
            self.item_queue.set_label(f"Queue — {stage} · {len(queued)} remaining{suffix}")
        summary = Gtk.MenuItem(
            label=f"{len(queued)} queued · {len(completed)} done · {len(errors)} failed"
        )
        summary.set_sensitive(False)
        menu.append(summary)
        if self._queue_items:
            menu.append(Gtk.SeparatorMenuItem())
        self._queue_rows = {}
        for item in self._queue_items:
            row = Gtk.MenuItem(label=queue_label(item, self._spinner_frame))
            self._queue_rows[item["item_id"]] = row
            error = item.get("error") or {}
            tooltip = error.get("evidence") or error.get("cause_code") or item.get("path") or ""
            row.set_tooltip_text(tooltip)
            if item.get("state") == "complete" and item.get("md_path"):
                row.set_tooltip_text(item["md_path"])
                row.connect(
                    "activate",
                    lambda _widget, path=item["md_path"]: self.on_open_transcript
                    and self.on_open_transcript(path),
                )
            elif (not item.get("active") and
                    item.get("state") not in ("complete", "failed", "suspect")):
                row.connect("button-press-event", self._queue_right_click, item["item_id"])
            else:
                row.set_sensitive(False)
            menu.append(row)
        if completed:
            menu.append(Gtk.SeparatorMenuItem())
            clear = Gtk.MenuItem(label="Clear completed")
            clear.connect("activate", lambda *_: self.on_clear_completed and self.on_clear_completed())
            menu.append(clear)
        menu.show_all()

    def _animate_queue(self) -> bool:
        """Animate only the active row; no menu rebuild and no ledger polling."""
        self._spinner_frame = (self._spinner_frame + 1) % len(SPINNER)
        active = next((x for x in self._queue_items if x.get("active")), None)
        row = self._queue_rows.get(active.get("item_id")) if active else None
        if row is not None:
            row.set_label(queue_label(active, self._spinner_frame))
        return True

    def _queue_right_click(self, _widget, event, item_id: str):
        if getattr(event, "button", 0) != 3:
            return False
        context = Gtk.Menu()
        skip = Gtk.MenuItem(label="Skip (preserve audio)")
        skip.connect("activate", lambda *_: self.on_skip and self.on_skip(item_id))
        context.append(skip)
        context.show_all()
        context.popup_at_pointer(event)
        return True

    def set_queue(self, items: list[dict]) -> None:
        """Thread-safe queue-menu update; rebuild only when visible data changes."""
        signature = tuple(
            (x.get("item_id"), x.get("state"), x.get("active"), x.get("stage"),
             x.get("progress_pct"), x.get("progress_detail"), x.get("bytes"),
             x.get("duration_s"), x.get("md_path"))
            for x in items
        )
        if signature == self._queue_signature:
            return
        self._queue_signature = signature
        self._queue_items = [dict(x) for x in items]
        GLib.idle_add(lambda: (self._render_queue(), False)[1])

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
