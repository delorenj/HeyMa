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

from __future__ import annotations

import gc
import logging
from pathlib import Path
from urllib.parse import quote

def _load_gtk():
    global gi, AppIndicator, GLib, Gio, Gtk
    import gi
    gi.require_version("Gtk", "3.0")
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator
    from gi.repository import GLib, Gio, Gtk

GLib = Gio = Gtk = AppIndicator = None

from . import component, desktop

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


def registered_as(indicator_id: str, item: str) -> bool:
    """Is this StatusNotifier registration string the indicator `indicator_id`?

    libappindicator exports `/org/ayatana/NotificationItem/<id>`, and the watcher
    lists it as `<bus-name>@<path>`. Match the id as the final path segment: a
    substring test would let "wax" claim "heyma-wax" (or a foreign item).
    """
    return item.rstrip("/").rsplit("/", 1)[-1] == indicator_id


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


REASON_WIDTH = 48  # a menu row has no room for a full evidence line
# Fallback when the ledger cannot be imported (the HeyMa tray runs under
# /usr/bin/python3); the canonical tuple is ledger.DURABILITY_CAUSES.
_DURABILITY_FALLBACK = ("archive_failed", "s3_reverify_failed")


def durability_causes() -> tuple[str, ...]:
    """Causes whose audio has no verified backup yet: never offer Skip for them."""
    try:
        from .ledger import DURABILITY_CAUSES
        return tuple(DURABILITY_CAUSES)
    except Exception:  # noqa: BLE001 - the tray must render without the ledger
        return _DURABILITY_FALLBACK


def _clip(text, width: int) -> str:
    head = str(text or "").strip().split("\n", 1)[0].strip()
    return head if len(head) <= width else head[:width - 1] + "…"


def failure_causes(rows) -> str:
    """'transcribe_failed x3, archive_failed x1' from failed rows; '' when none carry a cause."""
    counts: dict[str, int] = {}
    for row in rows or []:
        if isinstance(row, dict):
            cause = str(row.get("cause_code") or (row.get("error") or {}).get("cause_code") or "unknown")
            counts[cause] = counts.get(cause, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{cause} x{n}" for cause, n in ordered)


def failure_reason(item: dict, width: int = REASON_WIDTH) -> str:
    """'<cause>: <first evidence words>' for a failed/suspect row, '' when unknown."""
    error = item.get("error") or {}
    cause = error.get("cause_code") or item.get("cause_code")
    evidence = error.get("evidence") or item.get("evidence")
    if not cause and not evidence:
        return ""
    if cause and evidence:
        return _clip(f"{cause}: {evidence}", width)
    return _clip(cause or evidence, width)


def failure_signature(item: dict) -> tuple:
    """Everything the failed row and its Why submenu draw: a re-failure with a new
    reason, or the same one, must rebuild the menu (and re-enable a greyed row)."""
    error = item.get("error") or {}
    return (error.get("cause_code") or item.get("cause_code"),
            _clip(error.get("evidence") or item.get("evidence"), 160),
            error.get("at") or item.get("updated_at"))


def silent_label(row: dict) -> str:
    """'🔇 memo.m4a  (79:08 · mean -80 dB)' for one parked silent recording."""
    parts = []
    if row.get("duration_s"):
        seconds = int(row["duration_s"])
        parts.append(f"{seconds // 60}:{seconds % 60:02d}")
    if row.get("mean_db") is not None:
        parts.append(f"mean {round(float(row['mean_db']))} dB")
    detail = f"  ({' · '.join(parts)})" if parts else ""
    return f"🔇 {row.get('orig_name') or row.get('item_id')}{detail}"


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
    if item.get("state") in ("failed", "suspect"):
        # Menus exported over dbusmenu drop tooltips, so the reason has to be
        # in the label itself or nobody ever sees it.
        reason = failure_reason(item)
        if reason:
            # Clip the name, not the reason: Telnyx names run ~170 characters
            # and a width-limited menu would cut exactly the part that matters.
            display_name = f"{_clip(display_name, 48)} — {reason}"
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

    # Whether the newest recordings come out the other end as transcripts, and
    # whether a source keeps sending silence: the two outcome terms that let five
    # silent Voice Memos sit behind a green icon on 2026-11-18.
    transcription = snap.get("transcription") or {}
    tx_degraded = bool(transcription.get("degraded"))
    silent = snap.get("silent") or {}
    silent_sources = [src for src in (silent.get("sources") or []) if isinstance(src, dict)]

    cause = stream.get("cause_code") or inbox.get("cause_code")
    if cause:
        tip += f"\ncause: {cause}"
    if failed:
        # The count alone sends you to the ledger; the cause sends you to the fix.
        histogram = failure_causes(snap.get("failed") or [])
        shown_more = _as_int(snap.get("failed_more"))
        if histogram and shown_more:
            # snap["failed"] carries the newest rows only; never let a sample read as the total.
            histogram += f", +{shown_more} more"
        tip += f"\nfailed items: {failed}" + (f" ({histogram})" if histogram else "")
    if tx_degraded:
        newest = [str(name) for name in (transcription.get("newest_failed") or []) if name]
        tip += (f"\ntranscription degraded: newest {len(newest) or 'recent'} recording(s) "
                "produced no transcript")
    for src in silent_sources:
        tip += (f"\nsource silent: {src.get('source') or 'unknown'} sent {_as_int(src.get('count'))} "
                "silent recording(s) in a row — check the mic/input on that device")
    if _as_int(silent.get("count")) and not silent_sources:
        tip += f"\nsilent recordings parked: {_as_int(silent.get('count'))} (not transcribed)"
    if passes_failed:
        # A bare count sends you to the ledger; the slug sends you to the
        # provider that actually broke.
        tip += f"\nfailed passes: {passes_failed} item(s) — {', '.join(slugs) or 'unknown pass'}"
    if degraded:
        tip += f"\ndiarization degraded: last {undiarized} transcript(s) undiarized"
    if backlog > OUTBOX_BACKLOG_ALARM:
        tip += f"\nevent outbox backlog: {backlog} unpublished"
    if s == "recording" and stream.get("silent"):
        tip += "\nsilence: the encoder hears nothing — is the mic muted?"
    switched_off = [str(slug) for slug in (passes.get("operator_disabled") or [])]
    if switched_off:
        # Deliberate, so not a colour change — but never invisible either.
        tip += f"\npasses off: {', '.join(switched_off)}"

    if s == "recording":
        return "red", tip
    if (failed or passes_failed or degraded or tx_degraded or silent_sources
            or backlog > OUTBOX_BACKLOG_ALARM
            or s in ("not-ready", "error-partial", "error")
            or i in ("error",)
            or ((inbox.get("pending") or 0) and i != "ready-and-active")):
        return "yellow", tip
    if i == "stopped":
        return "yellow", tip
    return "green", tip


def flash_for(snap: dict) -> str | None:
    """The colour a recording icon alternates with, or None for a steady icon.

    A muted mic records hours of nothing behind a steady red icon. waxd tails
    the encoder's silencedetect and marks `stream.silent`; flashing toward
    yellow keeps "you are recording" visible while saying the input is dead.
    """
    stream = snap.get("stream") or {}
    if stream.get("state") == "recording" and stream.get("silent"):
        return "yellow"
    return None


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

class IndicatorHost:
    """The StatusNotifier lifecycle every HeyMa tray shares.

    waxd's own tray (`Tray`) and the HeyMa shell's one tray (`heyma_shell`) both
    ride this class, so the three lessons in the module docstring are learned
    once. A subclass supplies INDICATOR_ID, TITLE, `_menu()`, `_drop_menu_refs()`
    and `_reapply()`; it must set `self.indicator = None`, `self.registered =
    False`, `self._colour`, `self._tip`, `self._flash` and `self._flash_on`
    before calling `_build()`.
    """

    INDICATOR_ID = "wax"
    TITLE = "Wax"
    ICON_DIR = ICON_DIR
    ICONS = ICONS
    UNAVAILABLE_HINT = "Recording still works; use `wax status`."

    def _menu(self):
        raise NotImplementedError

    def _drop_menu_refs(self) -> None:
        """Forget every widget that lives in the old menu."""

    def _reapply(self) -> None:
        """Repaint the last known state onto a freshly built indicator."""
        if self._colour:
            self.set(self._colour, self._tip)

    def _teardown(self) -> None:
        """Release the old indicator so its D-Bus path is free for the rebuild.

        The menu items hold lambdas that close over `self`, so the indicator sits
        in a reference cycle and plain refcounting will not finalize it. Without
        the explicit collect the unexport happens *after* the new indicator has
        already tried (and failed) to claim the path.
        """
        ind, self.indicator = self.indicator, None
        self._drop_menu_refs()
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
                self.INDICATOR_ID, self.ICONS["green"],
                AppIndicator.IndicatorCategory.APPLICATION_STATUS, str(self.ICON_DIR)
            )
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_title(self.TITLE)
            self.indicator.set_menu(self._menu())
        except Exception as e:  # noqa: BLE001 - never fatal
            self.indicator, self.registered = None, False
            log.error("indicator construction failed: %s: %s", type(e).__name__, e)
            _notify(f"{self.TITLE}: tray unavailable", f"{e}\n{self.UNAVAILABLE_HINT}")
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
            listed = any(registered_as(self.INDICATOR_ID, str(i)) for i in items)
        except Exception:  # noqa: BLE001 - no watcher is a vanish, handled elsewhere
            return False
        self.registered = listed
        if not listed and not getattr(self, "_rebuilt_once", False):
            log.warning("SNI watcher does not list %s; rebuilding the indicator", self.INDICATOR_ID)
            self._rebuilt_once = True
            self._build()
            self._reapply()
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
            self._reapply()
        return False

    def _on_watcher_vanished(self, *_):
        # Screen lock, or gnome-shell reloading extensions. Not an error and not
        # worth notifying about — but we must drop the indicator NOW, while the
        # watcher is away, or the rebuild on return collides with it on the bus.
        log.info("SNI watcher vanished (screen lock or shell reload); indicator dropped")
        self.registered = False
        self._teardown()
        return False

    def _blink(self) -> bool:
        """Swap a flashing icon between its colour and the flash colour."""
        if not self._flash:
            self._flash_on = False
            return True
        self._flash_on = not self._flash_on
        if self.indicator is not None:
            shown = self._flash if self._flash_on else self._colour
            try:
                self.indicator.set_icon_full(self.ICONS.get(shown, self.ICONS["green"]), self._tip)
            except Exception:  # noqa: BLE001
                self.registered = False
        return True


class QueueMenu:
    """The Queue submenu: one row per tray item, with retry / skip / open actions.

    Shared by waxd's own tray and the Wax section of the HeyMa tray. A host sets
    `queue_menu` (the Gtk.Menu) and `item_queue` (the Gtk.MenuItem that owns it)
    and calls `_init_queue(...)` once.
    """

    def _init_queue(self, on_skip=None, on_clear_completed=None, on_open_transcript=None,
                    on_retry_item=None, on_retry_passes=None) -> None:
        self.on_skip, self.on_clear_completed = on_skip, on_clear_completed
        self.on_open_transcript = on_open_transcript
        self.on_retry_item, self.on_retry_passes = on_retry_item, on_retry_passes
        self.queue_menu = self.item_queue = None
        self._queue_items = []
        self._silent = {}
        self._queue_signature = None
        self._queue_rows = {}
        self._spinner_frame = 0

    # -- updates ---------------------------------------------------------
    def _render_queue(self) -> None:
        menu = getattr(self, "queue_menu", None)
        if menu is None:
            return
        for child in menu.get_children():
            menu.remove(child)
            # Destroy, not just remove: submenu callbacks hold their parent row,
            # and GTK keeps every Gtk.Menu alive in its own toplevel, so a removed
            # row with a submenu is never collected (~20 KiB per row per rebuild).
            destroy = getattr(child, "destroy", None)
            if destroy is not None:
                destroy()
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
        silent = getattr(self, "_silent", None) or {}
        silent_count = _as_int(silent.get("count"))
        if getattr(self, "item_queue", None) is not None:
            stage = STAGE_VERBS.get((active or {}).get("stage"), "Working") if active else "Idle"
            if active and active.get("progress_pct") is not None:
                stage = f"{stage} {int(active['progress_pct'])}%"
            suffix = f" · {len(errors)} failed" if errors else ""
            suffix += f" · {silent_count} silent" if silent_count else ""
            self.item_queue.set_label(f"Queue — {stage} · {len(queued)} remaining{suffix}")
        summary_text = f"{len(queued)} queued · {len(completed)} done · {len(errors)} failed"
        if silent_count:
            summary_text += f" · {silent_count} silent"
        summary = Gtk.MenuItem(label=summary_text)
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
            if item.get("state") in ("failed", "suspect"):
                tooltip = f"Open for Retry / Skip.\n{tooltip}"
            elif _has_stage_failure(item):
                tooltip = f"Click to retry the failed pass.\n{tooltip}"
            row.set_tooltip_text(tooltip)
            if (not item.get("active") and
                    item.get("state") in ("failed", "suspect")):
                # A submenu, not a click-to-retry: retry is not the only honest
                # answer (a dead source needs Skip), and dbusmenu hosts never
                # deliver right-clicks or tooltips, so the reason and both verbs
                # have to be menu rows.
                self._attach_failed_actions(row, item)
            elif not item.get("active") and _has_stage_failure(item) and self.on_retry_passes:
                row.connect(
                    "activate", self._retry_passes, item["item_id"], tuple(_failing_slugs(item)),
                )
            elif item.get("state") == "complete" and item.get("md_path"):
                row.connect(
                    "activate",
                    lambda _widget, path=item["md_path"]: self.on_open_transcript
                    and self.on_open_transcript(path),
                )
            elif (not item.get("active") and
                    item.get("state") not in ("complete", "failed", "suspect")):
                self._attach_submenu(row, [("Skip (preserve audio)",
                                            lambda r=row, i=item["item_id"]: self._skip_item(r, i))])
            else:
                row.set_sensitive(False)
            menu.append(row)
        self._render_silent(menu, silent)
        if completed:
            menu.append(Gtk.SeparatorMenuItem())
            clear = Gtk.MenuItem(label="Clear completed")
            clear.connect("activate", lambda *_: self.on_clear_completed and self.on_clear_completed())
            menu.append(clear)
        menu.show_all()

    def _append_wax_items(self, menu, on_open=None, on_settings=None, on_telephony=None) -> None:
        """The Wax menu body: status line, Queue, then inbox / settings / telephony.

        waxd's own tray appends this to its top-level menu; the HeyMa tray puts
        it in the Wax submenu. One definition, so the section cannot drift from
        what the Wax tray always showed.
        """
        self.item_status = Gtk.MenuItem(label="…")
        self.item_status.set_sensitive(False)
        menu.append(self.item_status)

        self.item_queue = Gtk.MenuItem(label="Queue")
        self.queue_menu = Gtk.Menu()
        self.item_queue.set_submenu(self.queue_menu)
        menu.append(self.item_queue)
        self._render_queue()

        menu.append(Gtk.SeparatorMenuItem())
        for label, callback in (("Open inbox", on_open), ("Settings…", on_settings),
                                ("Telephony…", on_telephony)):
            row = Gtk.MenuItem(label=label)
            row.connect("activate", lambda *_, cb=callback: cb and cb())
            menu.append(row)

    def _retry_item(self, row, item_id: str) -> None:
        """Disable a clicked failure immediately, then hand it to the daemon."""
        row.set_sensitive(False)
        if self.on_retry_item:
            self.on_retry_item(item_id)

    def _retry_passes(self, row, item_id: str, slugs: tuple[str, ...]) -> None:
        """Retry only the failed passes represented by this completed row."""
        row.set_sensitive(False)
        if self.on_retry_passes:
            self.on_retry_passes(item_id, slugs)

    def _animate_queue(self) -> bool:
        """Animate only the active row; no menu rebuild and no ledger polling."""
        self._spinner_frame = (self._spinner_frame + 1) % len(SPINNER)
        active = next((x for x in self._queue_items if x.get("active")), None)
        row = self._queue_rows.get(active.get("item_id")) if active else None
        if row is not None:
            row.set_label(queue_label(active, self._spinner_frame))
        return True


    # -- per-row submenus (the only interaction dbusmenu reliably exports) -----
    def _attach_submenu(self, row, entries) -> None:
        """entries: (label, callback-or-None); None draws an insensitive info row."""
        sub = Gtk.Menu()
        for label, callback in entries:
            child = Gtk.MenuItem(label=label)
            if callback is None:
                child.set_sensitive(False)
            else:
                child.connect("activate", lambda *_, cb=callback: cb())
            sub.append(child)
        sub.show_all()
        row.set_submenu(sub)

    def _skip_item(self, row, item_id: str) -> None:
        row.set_sensitive(False)
        if self.on_skip:
            self.on_skip(item_id)

    def _attach_failed_actions(self, row, item: dict) -> None:
        """Why, Retry, and Skip (unless the audio has no verified backup yet)."""
        item_id = item["item_id"]
        error = item.get("error") or {}
        cause = error.get("cause_code") or item.get("cause_code") or ""
        why = error.get("evidence") or cause or "no reason recorded (see `wax queue`)"
        entries = [(f"Why: {_clip(why, 90)}", None)]
        if self.on_retry_item:
            entries.append(("Retry (re-archive and re-transcribe)",
                            lambda: self._retry_item(row, item_id)))
        if cause in durability_causes():
            entries.append(("Skip unavailable: no verified backup yet (Retry re-archives)", None))
        elif getattr(self, "on_skip", None):
            entries.append(("Skip (preserve audio in skipped/)",
                            lambda: self._skip_item(row, item_id)))
        self._attach_submenu(row, entries)

    def _render_silent(self, menu, silent: dict) -> None:
        """Recordings parked as silent: not failures, but a source someone must fix."""
        count = _as_int(silent.get("count"))
        sources = [src for src in (silent.get("sources") or []) if isinstance(src, dict)]
        if not count and not sources:
            return
        menu.append(Gtk.SeparatorMenuItem())
        if count:
            header = Gtk.MenuItem(label=f"🔇 {count} silent recording(s) parked, not transcribed "
                                        "— check the mic/input")
            header.set_sensitive(False)
            menu.append(header)
        # Drawn even with nothing parked: the source streak alone tints the icon
        # yellow, so the menu must always say why.
        for src in sources:
            line = Gtk.MenuItem(label=f"⚠ source silent: {src.get('source') or 'unknown'} sent "
                                      f"{_as_int(src.get('count'))} in a row, none audible since")
            line.set_sensitive(False)
            menu.append(line)
        for rec in silent.get("recent") or []:
            if not isinstance(rec, dict) or not rec.get("item_id"):
                continue
            row = Gtk.MenuItem(label=silent_label(rec))
            levels = ""
            if rec.get("mean_db") is not None:
                levels = f"mean {rec['mean_db']} dB, peak {rec.get('max_db')} dB (speech is about -25 dB)"
            entries = [(f"Silent: {levels or 'measured below the speech floor'}", None),
                       (f"From: {rec.get('source') or 'unknown'} · parked in skipped/silent/, audio kept", None)]
            if self.on_retry_item:
                entries.append(("Transcribe anyway",
                                lambda r=row, i=rec["item_id"]: self._retry_item(r, i)))
            self._attach_submenu(row, entries)
            menu.append(row)
        if _as_int(silent.get("more")):
            more = Gtk.MenuItem(label=f"+{_as_int(silent['more'])} more (wax status)")
            more.set_sensitive(False)
            menu.append(more)

    def set_queue(self, items: list[dict], silent: dict | None = None) -> None:
        """Thread-safe queue-menu update; rebuild only when visible data changes."""
        silent = silent if isinstance(silent, dict) else {}
        # The pass fields are part of the signature: a pass failing on an
        # otherwise unchanged row changes nothing else, so omitting them would
        # keep the stale "✓" rendered until some other field happened to move.
        signature = tuple(
            (x.get("item_id"), x.get("state"), x.get("active"), x.get("stage"),
             x.get("progress_pct"), x.get("progress_detail"), x.get("bytes"),
             x.get("duration_s"), x.get("md_path"),
             _as_int(x.get("passes_failed")), tuple(_failing_slugs(x)),
             failure_signature(x) if x.get("state") in ("failed", "suspect") else ())
            for x in items
        ) + (
            _as_int(silent.get("count")), _as_int(silent.get("more")),
            # `at` too: a re-park within one tick must still re-enable a greyed row.
            tuple((r.get("item_id"), r.get("mean_db"), r.get("at") or r.get("updated_at"))
                  for r in (silent.get("recent") or [])
                  if isinstance(r, dict)),
            tuple((s.get("source"), s.get("count")) for s in (silent.get("sources") or [])
                  if isinstance(s, dict)),
        )
        if signature == self._queue_signature:
            return
        self._queue_signature = signature
        self._queue_items = [dict(x) for x in items]
        self._silent = dict(silent)
        GLib.idle_add(lambda: (self._render_queue(), False)[1])


class Tray(IndicatorHost, QueueMenu):
    """waxd's own tray: the default on hosts that run no HeyMa shell tray."""

    def __init__(self, on_toggle=None, on_quit=None, on_open=None,
                 on_skip=None, on_clear_completed=None, on_open_transcript=None,
                 on_retry_item=None, on_retry_passes=None, on_settings=None,
                 on_telephony=None, on_silence=None):
        _load_gtk()
        self.on_settings = on_settings
        self.on_telephony, self.on_silence = on_telephony, on_silence
        self.on_toggle, self.on_quit, self.on_open = on_toggle, on_quit, on_open
        self._init_queue(on_skip, on_clear_completed, on_open_transcript,
                         on_retry_item, on_retry_passes)
        self.indicator = None
        self.registered = False
        self._colour = None
        self._tip = ""
        self._flash = None
        self._flash_on = False
        self._build()
        self._watch_bus()
        GLib.timeout_add(250, self._animate_queue)
        GLib.timeout_add(500, self._blink)

    # -- construction ----------------------------------------------------
    def _menu(self) -> Gtk.Menu:
        m = Gtk.Menu()
        self.item_toggle = Gtk.MenuItem(label="Start recording")
        self.item_toggle.connect("activate", lambda *_: self.on_toggle and self.on_toggle())
        m.append(self.item_toggle)

        self._append_wax_items(m, self.on_open, self.on_settings, self.on_telephony)
        silence = Gtk.MenuItem(label="Silence local ringtone")
        silence.connect("activate", lambda *_: self.on_silence and self.on_silence())
        m.append(silence)
        q = Gtk.MenuItem(label="Quit waxd")
        q.connect("activate", lambda *_: self.on_quit and self.on_quit())
        m.append(q)
        m.show_all()
        return m

    def _drop_menu_refs(self) -> None:
        self.item_toggle = self.item_status = self.item_queue = self.queue_menu = None

    # -- updates ---------------------------------------------------------
    def set(self, colour: str, tooltip: str = "", recording: bool = False,
            flash: str | None = None) -> None:
        """Thread-safe icon update. `flash` alternates the icon toward that colour."""
        if colour != self._colour:
            # Once per TRANSITION, never per tick — waxd calls this at 1 Hz. This
            # is the journal line whose absence let both outages run for a week
            # with zero waxd entries to grep for.
            log.info("tray %s: %s", colour, tooltip.replace("\n", " · "))
        # The flash is not a transition: it rides beside the steady colour, so
        # a muted mic does not log a line every half second.
        self._colour, self._tip, self._flash = colour, tooltip, flash

        def apply():
            if self.indicator is not None:
                shown = flash if flash and self._flash_on else colour
                try:
                    self.indicator.set_icon_full(ICONS.get(shown, ICONS["green"]), tooltip)
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
