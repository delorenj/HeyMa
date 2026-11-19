"""The one Linux tray: GTK3 AppIndicator id `heyma`, run by /usr/bin/python3.

Everything hard lives elsewhere. The indicator lifecycle (re-register when the
StatusNotifier watcher returns, destroy before rebuild, verify against the
watcher) is `wax.tray.IndicatorHost`; the Queue submenu is `wax.tray.QueueMenu`;
the telephony ringer is `wax.telephony_presentation.Ringer`. What this module
adds is the wiring: it draws `model.MENU` and `model.verdict()`, and every
action shells out to the product's own CLI. The tray owns no capture and no
worker, so a crash here cannot touch dictation or a recording.

Cadence (a GLib redraw loop is the UI, not a Wax clock):
  * every 1 s  — read var/state.json and vinyl's state.json, recompute the icon;
  * every 30 s — run the registry (`heyma status`) on a worker thread, which is
    what the Vinyl-daemon, Telephony and Phonograph rows are judged by.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import model, paths, registry
from .adapters import (Context, phonograph_base_url, quick_installed, vinyl_command,
                       vinyl_settings_command)

log = logging.getLogger("heyma.tray")

FAST_MS = 1000
SLOW_S = 30


def _wax_tray():
    """wax.tray with GTK loaded (the shared lifecycle classes live there)."""
    if str(paths.WAX_SRC) not in sys.path:
        sys.path.insert(0, str(paths.WAX_SRC))
    from wax import tray
    tray._load_gtk()
    return tray


def _popen(argv, **kwargs):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **kwargs)


def build_tray_class(wax_tray):
    """Subclass the shared lifecycle once GTK exists (the bases need Gtk at call time)."""

    class HeymaTray(wax_tray.IndicatorHost, wax_tray.QueueMenu):
        INDICATOR_ID = "heyma"
        TITLE = "HeyMa"
        UNAVAILABLE_HINT = "Dictation and recording still work; use `heyma status`."

        def __init__(self, actions: dict, vinyl_settings: bool):
            self.actions = actions
            self.vinyl_settings = vinyl_settings
            self._init_queue(
                on_skip=lambda item: actions["wax"]("skip", item),
                on_clear_completed=lambda: actions["wax"]("queue", "--clear-completed"),
                on_open_transcript=lambda md: _popen(["xdg-open", wax_tray.obsidian_uri(md)]),
                on_retry_item=lambda item: actions["wax"]("retry", item),
                on_retry_passes=lambda item, slugs: actions["wax"]("ep", "run-selected", item, *slugs),
            )
            self.indicator = None
            self.registered = False
            self._colour = None
            self._tip = ""
            self._flash = None
            self._flash_on = False
            self._view = None
            self._labels = {}
            self.rows = {}
            self.sections = {}
            self._build()
            self._watch_bus()
            wax_tray.GLib.timeout_add(250, self._animate_queue)
            wax_tray.GLib.timeout_add(500, self._blink)

        # -- menu construction, from model.MENU ------------------------------
        def _menu(self):
            Gtk = wax_tray.Gtk
            self.rows, self.sections = {}, {}
            menu = Gtk.Menu()
            self._fill(menu, model.MENU)
            menu.show_all()
            if self._labels:
                self._paint(self._labels)
            return menu

        def _fill(self, menu, spec):
            Gtk = wax_tray.Gtk
            for entry in spec:
                kind = entry["kind"]
                if kind == "separator":
                    menu.append(Gtk.SeparatorMenuItem())
                elif kind == "wax-body":
                    self._append_wax_items(menu, self.actions["open_inbox"],
                                           lambda: self.actions["page"]("settings"),
                                           lambda: self.actions["page"]("telephony"))
                    self.rows["wax.status"] = self.item_status
                else:
                    row = Gtk.MenuItem(label=entry["label"])
                    self.rows[entry["id"]] = row
                    if kind == "status":
                        row.set_sensitive(False)
                    elif kind == "item":
                        row.connect("activate", lambda *_, key=entry["id"]: self.actions[key]())
                    elif kind == "submenu":
                        sub = Gtk.Menu()
                        row.set_submenu(sub)
                        self._fill(sub, entry["children"])
                        sub.show_all()
                        # Hidden until the model says the product is installed.
                        row.set_no_show_all(True)
                        self.sections[entry["id"]] = row
                    menu.append(row)

        def _drop_menu_refs(self) -> None:
            self.rows, self.sections = {}, {}
            self.item_status = self.item_queue = self.queue_menu = None

        def _reapply(self) -> None:
            if self._view is not None:
                self.apply(self._view, self._labels)

        # -- state -----------------------------------------------------------
        def _paint(self, labels: dict) -> None:
            for row_id, props in labels.items():
                widget = self.rows.get(row_id) or self.sections.get(row_id)
                if widget is None:
                    continue
                if "label" in props:
                    widget.set_label(props["label"])
                if "sensitive" in props:
                    widget.set_sensitive(props["sensitive"])
                if "visible" in props:
                    widget.set_visible(props["visible"])

        def apply(self, view, labels: dict) -> None:
            """Thread-safe: draw one verdict and its row labels."""
            if view.colour != self._colour:
                # Once per TRANSITION, never per tick (the journal line whose
                # absence let two outages run for a week).
                log.info("tray %s: %s", view.colour, view.tooltip.replace("\n", " · "))
            self._view, self._labels = view, labels
            self._colour, self._tip, self._flash = view.colour, view.tooltip, view.flash

            def draw():
                if self.indicator is not None:
                    shown = view.flash if view.flash and self._flash_on else view.colour
                    try:
                        self.indicator.set_icon_full(self.ICONS.get(shown, self.ICONS["green"]),
                                                     view.tooltip)
                    except Exception:  # noqa: BLE001
                        self.registered = False
                self._paint(labels)
                return False

            wax_tray.GLib.idle_add(draw)

    return HeymaTray


class Shell:
    """Owns the data feeding the tray: mirrors each second, registry every 30 s."""

    def __init__(self, ctx: Context | None = None):
        self.ctx = ctx or Context()
        self.debounce = model.Debounce()
        self.sections: dict = {}
        self.slow_degraded: set = set()
        self.installed = quick_installed(self.ctx)
        self._collecting = False
        self._lock = threading.Lock()

    def collect_async(self, done) -> None:
        """Run the registry on a worker thread; `done()` is called from it when stored."""
        with self._lock:
            if self._collecting:
                return
            self._collecting = True

        def work():
            try:
                doc = registry.collect(self.ctx)
                rows = registry.by_id(doc)
                with self._lock:
                    self.sections = rows
                    self.slow_degraded = self.debounce.update(rows)
            except Exception:  # noqa: BLE001 - the tray outlives any registry failure
                log.exception("registry collect failed")
            finally:
                with self._lock:
                    self._collecting = False
            done()

        threading.Thread(target=work, daemon=True, name="heyma-registry").start()

    def inputs(self) -> model.Inputs:
        with self._lock:
            sections, slow = dict(self.sections), set(self.slow_degraded)
        return model.Inputs(
            wax_snap=self.ctx.read_json(self.ctx.wax_state_path),
            vinyl_snap=self.ctx.read_json(self.ctx.vinyl_state_path),
            sections=sections, slow_degraded=slow, installed=self.installed,
            now=self.ctx.now())


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    wax_tray = _wax_tray()
    Gtk, GLib = wax_tray.Gtk, wax_tray.GLib
    if not Gtk.init_check()[0]:
        log.error("no display; the HeyMa tray needs a graphical session")
        return 1
    from wax import desktop, telephony_presentation

    shell = Shell()
    ctx = shell.ctx
    vinyl_cmd = vinyl_command(ctx)
    settings_cmd = vinyl_settings_command(ctx)
    wax_cli = str(ctx.wax_cli)
    heyma = str(ctx.root / "bin" / "heyma")
    toggle = str(ctx.root / "components" / "wax" / "bin" / "wax-toggle")

    def copy_trace():
        view = shell.inputs().vinyl_snap or {}
        reference = view.get("trace_ref") or ""
        if not reference or not vinyl_cmd:
            return

        def work():
            try:
                result = subprocess.run(vinyl_cmd + ["trace", "copy", reference],
                                        capture_output=True, text=True, timeout=10)
                if result.returncode:
                    raise RuntimeError(result.stderr.strip())
                _notify("Vinyl: trace copied", "Paste the reference into chat.", "normal")
            except Exception as error:  # noqa: BLE001
                _notify("Vinyl: copy failed", str(error))

        threading.Thread(target=work, daemon=True).start()

    ringer = telephony_presentation.Ringer(telephony_presentation.RingPlayer(),
                                           datetime.now(timezone.utc))
    def wax_action(*args):
        # Retry / Skip / Transcribe anyway / rerun passes / Clear completed: a
        # refused or crashed CLI call raises a notification with its first error
        # line and re-arms the queue so the greyed row comes back.
        def rearm(_message):
            def redraw():
                indicator._queue_signature = None
                return False
            GLib.idle_add(redraw)
        desktop.run_wax_action(wax_cli, args, on_failure=rearm, app="HeyMa")

    actions = {
        # One toggle implementation per product: the hotkey's own entry points.
        "dictation": lambda: vinyl_cmd and _popen(vinyl_cmd + ["toggle"]),
        "recording": lambda: _popen([toggle]),
        "wax": wax_action,
        "open_inbox": lambda: _popen(["xdg-open", str(paths.wax_root(ctx.environ) / "inbox")]),
        "page": lambda page: _popen([heyma, "--page", page]),
        "settings": lambda: _popen([heyma, "--page", "settings"]),
        "quit": Gtk.main_quit,
        "vinyl.trace": copy_trace,
        "vinyl.settings": lambda: settings_cmd and _popen(settings_cmd),
        "telephony.silence": ringer.silence,
        "telephony.page": lambda: _popen([heyma, "--page", "telephony"]),
        "phonograph.open": lambda: _popen(["xdg-open", phonograph_base_url(ctx)]),
    }
    indicator = build_tray_class(wax_tray)(actions, vinyl_settings=settings_cmd is not None)

    def tick():
        try:
            inputs = shell.inputs()
            view = model.verdict(inputs)
            indicator.apply(view, model.labels(inputs, view, vinyl_settings=settings_cmd is not None))
            if isinstance(inputs.wax_snap, dict):
                indicator.set_queue(inputs.wax_snap.get("tray_items") or [],
                                    inputs.wax_snap.get("silent"))
                ringer.tick(inputs.wax_snap.get("telephony"), datetime.now(timezone.utc))
            else:
                ringer.tick(None, datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001 - one bad tick must not end the tray
            log.exception("tray tick failed")
        return True

    def refresh():
        shell.collect_async(lambda: GLib.idle_add(lambda: (tick(), False)[1]))
        return True

    GLib.timeout_add(FAST_MS, tick)
    GLib.timeout_add_seconds(SLOW_S, refresh)
    tick()
    refresh()
    try:
        Gtk.main()
    finally:
        ringer.player.stop()
    return 0


def _notify(title: str, body: str, urgency: str = "critical") -> None:
    subprocess.run(["notify-send", "-a", "HeyMa", "-u", urgency, title, body], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
