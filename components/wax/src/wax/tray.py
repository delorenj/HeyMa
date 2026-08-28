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
import logging
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

from . import component, desktop  # noqa: E402

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

ICON_DIR = component.TRAY_ASSETS
WATCHER = "org.kde.StatusNotifierWatcher"
# WAX-DESIGN.md:262 and :359 both name `outbox_backlog>50` as the NATS-outage
# alarm. The value has been computed and mirrored into state.json since day one;
# colour_for simply never read it, so a broker outage accumulated behind a green
# icon with nothing anywhere saying so.
OUTBOX_BACKLOG_ALARM = 50

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


def _as_int(value) -> int:
    """Snapshot fields arrive from JSON and SQLite; they can be None or a str."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _failing_slugs(item: dict) -> list[str]:
    """Enrichment passes whose latest attempt failed on this row (contract A)."""
    return [str(slug) for slug in (item.get("passes_failed_slugs") or []) if slug]


def _has_stage_failure(item: dict) -> bool:
    """Item state cannot answer this question.

    worker.process() parks the audio and marks the item `complete` 215 ms AFTER
    the pass is recorded failed, so `state` alone renders "✓ done" over a burning
    pass — which is precisely what it did, 11 times, for five days.
    """
    return bool(_failing_slugs(item)) or _as_int(item.get("passes_failed")) > 0


def _slug_list(slugs: list[str], limit: int = 2) -> str:
    """Name the failing passes; a tray row has no room for an unbounded list."""
    if not slugs:
        return "enrichment"
    if len(slugs) <= limit:
        return ", ".join(slugs)
    return ", ".join(slugs[:limit]) + f" +{len(slugs) - limit}"


def queue_label(item: dict, spinner_frame: int = 0) -> str:
    stage_failed = _has_stage_failure(item)
    if item.get("active"):
        icon = SPINNER[spinner_frame % len(SPINNER)]
    elif item.get("state") in ("failed", "suspect") or stage_failed:
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
    elif item.get("active") and stage:
        prefix = f"{stage}: "
    elif stage_failed:
        # Name the pass, not just the fault. "⚠ meeting.ogg" sends you to the
        # ledger; "⚠ title-slug failed: meeting.ogg" sends you to the provider.
        prefix = f"{_slug_list(_failing_slugs(item))} failed: "
    else:
        prefix = ""
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

    RED stays reserved for capture-side states. A degraded downstream stage — a
    failing enrichment pass, diarization producing nothing, an outbox nobody is
    draining — is YELLOW: the output is impoverished, but no recording is lost.
    """
    stream = snap.get("stream") or {}
    inbox = snap.get("inbox") or {}
    passes = snap.get("passes") or {}
    diarization = snap.get("diarization") or {}
    s, i = stream.get("state"), inbox.get("state")

    tip = f"stream: {s}\ninbox: {i}"
    # Use failures physically present in the actionable tray queue. Historical
    # ledger failures are audit history and must not keep the icon yellow.
    failed = (snap.get("queue") or {}).get("failed", 0)
    # The three sub-stage terms below are the whole fix. `queue.failed` tallies
    # ITEM states, and worker.process() marks the item `complete` 215 ms after
    # recording the pass failed — so a stage running at 100% failure was
    # structurally unrepresentable here, and stayed green for five days.
    passes_failed = _as_int(passes.get("failed"))
    slugs = [str(slug) for slug in (passes.get("slugs") or [])]
    degraded = bool(diarization.get("degraded"))
    undiarized = _as_int(diarization.get("recent_undiarized"))
    backlog = _as_int(snap.get("outbox_backlog"))

    cause = stream.get("cause_code") or inbox.get("cause_code")
    if cause:
        tip += f"\ncause: {cause}"
    if failed:
        tip += f"\nfailed items: {failed}"
    if passes_failed:
        # A bare count sends you to the ledger; the slug sends you to the
        # provider that actually broke.
        tip += f"\nfailed passes: {passes_failed} item(s) — {', '.join(slugs) or 'unknown pass'}"
    if degraded:
        tip += f"\ndiarization degraded: last {undiarized} transcript(s) undiarized"
    if backlog > OUTBOX_BACKLOG_ALARM:
        tip += f"\nevent outbox backlog: {backlog} unpublished"

    if s == "recording":
        return "red", tip
    if (failed or passes_failed or degraded or backlog > OUTBOX_BACKLOG_ALARM
            or s in ("not-ready", "error-partial", "error")
            or i in ("error",)
            or ((inbox.get("pending") or 0) and i != "ready-and-active")):
        return "yellow", tip
    if i == "stopped":
        return "yellow", tip
    return "green", tip


def notify_stage_failure(slug: str, reason_code: str, *, item: str = "",
                         detail: str = "") -> bool:
    """Public failure channel: put a dead sub-stage on the user's desktop.

    Debounced to the first failure per (slug, reason_code) — see desktop.py,
    which owns the implementation because importing wax.tray drags in `gi` and
    mise's python3 has none. Callers outside the daemon should use
    desktop.notify_stage_failure directly; this name exists so the module that
    owns the *visible* failure surfaces also owns the alarm.
    """
    return desktop.notify_stage_failure(slug, reason_code, item=item, detail=detail)


def clear_stage_failure(slug: str | None = None) -> None:
    """Re-arm the desktop alarm for `slug` after a recovery (all when None)."""
    desktop.clear_stage_failure(slug)


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
            log.error("indicator construction failed: %s: %s", type(e).__name__, e)
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
            log.warning("SNI watcher does not list wax; rebuilding the indicator")
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
            log.info("SNI watcher returned; rebuilding the indicator")
            self._build()
            if self._colour:
                self.set(self._colour, self._tip)
        return False

    def _on_watcher_vanished(self, *_):
        # Screen lock, or gnome-shell reloading extensions. Not an error and not
        # worth notifying about — but we must drop the indicator NOW, while the
        # watcher is away, or the rebuild on return collides with it on the bus.
        log.info("SNI watcher vanished (screen lock or shell reload); indicator dropped")
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
        # A stage failure counts as an error even on a row whose ITEM state is
        # `complete`, which is the normal case: the audio parks successfully and
        # the note is written, minus the enrichment nobody was told was missing.
        # Without this term the submenu printed "0 queued · 33 done · 0 failed"
        # while 11 title-slug passes were burning.
        stage_failed = [x for x in self._queue_items if _has_stage_failure(x)]
        errors = [x for x in self._queue_items
                  if x.get("state") in ("failed", "suspect") or _has_stage_failure(x)]
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
        if stage_failed:
            # `done` and `failed` deliberately overlap here — a parked item with
            # a dead pass is genuinely both — so spell out the second reading
            # rather than leaving the two counts looking inconsistent.
            named = _slug_list(sorted({s for x in stage_failed for s in _failing_slugs(x)}), 3)
            note = Gtk.MenuItem(label=f"⚠ {len(stage_failed)} with a failed pass: {named}")
            note.set_sensitive(False)
            menu.append(note)
        if self._queue_items:
            menu.append(Gtk.SeparatorMenuItem())
        self._queue_rows = {}
        for item in self._queue_items:
            row = Gtk.MenuItem(label=queue_label(item, self._spinner_frame))
            self._queue_rows[item["item_id"]] = row
            error = item.get("error") or {}
            tooltip = error.get("evidence") or error.get("cause_code") or item.get("path") or ""
            if item.get("state") == "complete" and item.get("md_path"):
                tooltip = item["md_path"]
            if _has_stage_failure(item):
                # The note's `wax.passes.<slug>` block carries reason_code and
                # detail (contract D), so naming the pass makes the hover the
                # first step of the diagnosis instead of a dead end.
                tooltip = f"failed pass: {_slug_list(_failing_slugs(item), 4)}\n{tooltip}"
            row.set_tooltip_text(tooltip)
            if item.get("state") == "complete" and item.get("md_path"):
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
        # The pass fields are part of the signature: a pass failing on an
        # otherwise unchanged row changes nothing else, so omitting them would
        # keep the stale "✓" rendered until some other field happened to move.
        signature = tuple(
            (x.get("item_id"), x.get("state"), x.get("active"), x.get("stage"),
             x.get("progress_pct"), x.get("progress_detail"), x.get("bytes"),
             x.get("duration_s"), x.get("md_path"),
             _as_int(x.get("passes_failed")), tuple(_failing_slugs(x)))
            for x in items
        )
        if signature == self._queue_signature:
            return
        self._queue_signature = signature
        self._queue_items = [dict(x) for x in items]
        GLib.idle_add(lambda: (self._render_queue(), False)[1])

    def set(self, colour: str, tooltip: str = "", recording: bool = False) -> None:
        """Thread-safe icon update."""
        if colour != self._colour:
            # Once per TRANSITION, never per tick — waxd calls this at 1 Hz. This
            # is the journal line whose absence let both outages run for a week
            # with zero waxd entries to grep for.
            log.info("tray %s: %s", colour, tooltip.replace("\n", " · "))
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
    """The daemon's existing four notify sites. One implementation, in desktop."""
    desktop.notify(title, body)
