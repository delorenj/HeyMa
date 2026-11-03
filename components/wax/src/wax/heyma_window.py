"""The HeyMa window: Settings and a live Logs tail, opened from the tray.

    python3 -m wax.heyma_window [--page settings|logs]

GTK 4 + libadwaita, in its OWN process. The tray is GTK 3 (libappindicator has
no GTK 4 build) and the two toolkits cannot share an interpreter, so the tray
spawns this module instead of building a window itself. `Adw.Application`
makes it single-instance over D-Bus: clicking "Settings" again presents the
open window rather than stacking a second one.

The window owns no pipeline state. Every read and every change goes through the
same `wax` CLI the tray and n8n use (`bin/wax` beside this component, never a
PATH lookup), so Wax keeps owning every mutation:

    Clear finished      wax queue --clear-completed
    Retry failed        wax retry
    pass switches       wax ep enable|disable <slug>   (var/settings.json overlay)
    background worker   wax pipeline enable|disable
    Logs                journalctl --user -f -u waxd.service ...

Layout: a collapsible left drawer (Adw.OverlaySplitView; F9 or the header
button toggles it, and it folds into an overlay on a narrow window) beside a
full-width content area. Every page scrolls vertically and never horizontally.

Pure helpers (row shaping, log-line levels) sit at module level with no `gi`
import so the test suite can exercise them under mise's Python, which has no gi.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from . import component, paths

APP_ID = "sh.delo.HeyMa"
WAX_CLI = component.ROOT / "bin" / "wax"
MAX_LOG_LINES = 5000
LOG_BACKLOG = 400

# (label, systemd user units). The first is the default: waxd is the single
# owner of the pipeline, so "current Wax activity" is its journal.
LOG_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("waxd", ("waxd.service",)),
    ("All Wax services", ("waxd.service", "wax-capture-guard.service", "wax-alert.service",
                          "telnyx-s3-intake.service", "telnyx-s3-intake-alert.service",
                          "syncthing-audio-policy.service")),
)

PAGES = (
    ("settings", "Settings", "emblem-system-symbolic"),
    ("logs", "Logs", "utilities-terminal-symbolic"),
)

_ERROR = re.compile(r"\b(ERROR|CRITICAL|Traceback|Exception)\b")
_WARN = re.compile(r"\bWARNING\b|\bWARN\b")


# The logo's two inks: terracotta "Hey" (#c37f60) and parchment "Ma!" (#e8d7b0).
# The accent is the terracotta darkened until white text on it clears WCAG AA
# (4.57:1). Only the background is set: libadwaita derives the standalone
# `--accent-color` from it per colour scheme, so text accents stay legible on
# light and dark without a media query (Adw's forced schemes do not reach CSS
# `prefers-color-scheme`, so one would silently pick the wrong branch).
#
# Parchment on a light sidebar all but vanishes, so the wordmark always sits on
# a warm dark card: parchment 10.4:1 and terracotta 4.59:1 on it, in either scheme.
STYLESHEET = """
:root {
  --accent-bg-color: #b0603f;
  --accent-fg-color: #ffffff;
}
.heyma-brand {
  margin: 6px 12px 12px 12px;
  padding: 14px 16px;
  border-radius: 12px;
  background-color: #2f2622;
}
/* The system monospace face at full UI size wraps a log line into five; a tail
   wants density. */
.heyma-log { font-size: 80%; }
"""


# ------------------------------------------------------------ pure helpers ----

def log_level(line: str) -> str:
    """'error', 'warn' or 'info' for one journal line (drives its colour)."""
    if _ERROR.search(line):
        return "error"
    if _WARN.search(line):
        return "warn"
    return "info"


_JOURNAL_LINE = re.compile(
    r"^(?P<stamp>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\S*\s+\S+\s+"
    r"(?P<unit>[^\s\[:]+)(?:\[\d+\])?:\s?(?P<message>.*)$")


def split_journal_line(line: str) -> tuple[str, str, str]:
    """(stamp, unit, message) from one `journalctl -o short-iso` line.

    The host and pid are dropped — one machine, and the pid is noise in a tail;
    the stamp loses its year and offset ("11-02 14:39:50"). A line that is not
    journal-shaped (journalctl's own "-- No entries --", a continuation) comes
    back whole as the message.
    """
    match = _JOURNAL_LINE.match(line)
    if not match:
        return "", "", line
    stamp = match["stamp"][5:].replace("T", " ")
    return stamp, match["unit"], match["message"]


def journal_argv(units: tuple[str, ...], backlog: int = LOG_BACKLOG) -> list[str]:
    argv = ["journalctl", "--user", "--follow", "--no-pager", "--output=short-iso",
            f"--lines={int(backlog)}"]
    for unit in units:
        argv += ["--unit", unit]
    return argv


def finished_count(queue_rows: list[dict[str, Any]]) -> int:
    """Rows `wax queue --clear-completed` would hide: the completed ones."""
    return sum(1 for row in queue_rows or [] if row.get("state") == "complete")


def failed_count(queue_rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in queue_rows or [] if row.get("state") in ("failed", "suspect"))


def pipeline_membership(catalog: dict[str, Any]) -> dict[str, list[str]]:
    """{slug: [enabled compiled pipelines whose frozen graph holds it]}."""
    out: dict[str, list[str]] = {}
    pipelines = ((catalog or {}).get("pipelines") or {}).get("pipelines") or []
    for pipeline in pipelines:
        if not pipeline.get("enabled"):
            continue
        for node in pipeline.get("nodes") or []:
            slug = node.get("slug")
            if slug:
                out.setdefault(slug, []).append(str(pipeline.get("name")))
    return {slug: sorted(names) for slug, names in out.items()}


def pass_rows(ep_rows: list[dict[str, Any]], catalog: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Shape `wax ep list` rows for the Enrichment passes section.

    `active` are passes that can run today: enabled in passes.d or frozen into
    an enabled compiled pipeline. Their switch is the operator overlay. The rest
    are `inactive` — listed so the section is the whole registry, but with no
    switch, because flipping the overlay on a pass no plan contains does nothing.
    """
    membership = pipeline_membership(catalog)
    active, inactive = [], []
    for row in ep_rows or []:
        slug = row.get("slug")
        if not slug:
            continue
        description = str(row.get("description") or "").strip().splitlines()
        runs_in = membership.get(slug, [])
        bits = []
        if description:
            bits.append(description[0])
        meta = [f"v{row.get('version', 1)}"]
        if runs_in:
            meta.append("runs in " + ", ".join(runs_in))
        elif row.get("enabled") and row.get("auto"):
            meta.append("registry plan")
        elif row.get("enabled"):
            meta.append("manual")
        if row.get("failed"):
            meta.append(f"{row['failed']} failing")
        if row.get("quarantined"):
            meta.append(f"quarantined: {row['quarantined']}")
        shaped = {
            "slug": slug,
            "title": slug,
            "subtitle": "\n".join([*bits, " · ".join(meta)]),
            "operator_enabled": bool(row.get("operator_enabled", True)),
            "pipelines": runs_in,
            "failed": int(row.get("failed") or 0),
        }
        if row.get("enabled") or runs_in:
            active.append(shaped)
        else:
            shaped["subtitle"] = "\n".join(
                [*bits, f"enabled: false in {Path(str(row.get('path') or slug)).name}"])
            inactive.append(shaped)
    return {"active": active, "inactive": inactive}


def wax(*args: str, timeout: float = 120) -> tuple[bool, Any, str]:
    """Run `bin/wax --json ARGS`. Returns (ok, parsed stdout or None, error text)."""
    try:
        proc = subprocess.run([str(WAX_CLI), "--json", *args], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, None, f"{type(exc).__name__}: {exc}"
    data = None
    if proc.stdout.strip():
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            data = proc.stdout
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, data, err[-1] if err else f"wax {' '.join(args)} exited {proc.returncode}"
    return True, data, ""


# --------------------------------------------------------------------- GTK ----

def _load_gtk():
    global GLib, GObject, Gio, Gtk, Gdk, Adw, Pango
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: F401


GLib = GObject = Gio = Gtk = Gdk = Adw = Pango = None


def _background(work: Callable[[], Any], done: Callable[[Any], None]) -> None:
    """Run `work` off the main loop; hand its result to `done` on it."""
    def runner():
        result = work()
        GLib.idle_add(lambda: (done(result), False)[1])
    threading.Thread(target=runner, daemon=True).start()


def _markup(text: str) -> str:
    """Adw rows parse titles/subtitles as Pango markup; pass text through safely."""
    return GLib.markup_escape_text(str(text))


def _scroller(child) -> "Gtk.ScrolledWindow":
    """Vertical-only scrolling: the content is full width and never pans sideways."""
    scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                  vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                  hexpand=True, vexpand=True)
    scroller.set_child(child)
    return scroller


class SettingsPage:
    def __init__(self, toast: Callable[[str], None]):
        self.toast = toast
        self._pass_rows: list[Any] = []
        self._syncing = False

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                       margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
                       hexpand=True)
        self.widget = _scroller(body)

        # -- Tray -----------------------------------------------------------
        tray = Adw.PreferencesGroup(title="Tray",
                                    description="The Wax tray's Queue submenu.")
        self.clear_row = Adw.ActionRow(title="Clear finished items",
                                       subtitle="Hide completed rows from the tray queue. "
                                                "Notes, audio and ledger history are kept.")
        self.clear_button = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        self.clear_button.add_css_class("suggested-action")
        self.clear_button.connect("clicked", self._clear_finished)
        self.clear_row.add_suffix(self.clear_button)
        tray.add(self.clear_row)

        self.retry_row = Adw.ActionRow(title="Retry failed items",
                                       subtitle="Requeue every failed or suspect inbox item.")
        self.retry_button = Gtk.Button(label="Retry", valign=Gtk.Align.CENTER)
        self.retry_button.connect("clicked", self._retry_failed)
        self.retry_row.add_suffix(self.retry_button)
        tray.add(self.retry_row)
        body.append(tray)

        # -- Enrichment passes ------------------------------------------------
        self.passes_group = Adw.PreferencesGroup(
            title="Enrichment passes",
            description="A pass switched off is recorded as skipped (operator_disabled) on every "
                        "new transcript and document, in every pipeline, until switched back on. "
                        "Notes already enriched are not touched.")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text="Reload passes")
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_: self.refresh())
        self.passes_group.set_header_suffix(refresh)
        body.append(self.passes_group)

        # -- Pipeline -----------------------------------------------------------
        pipeline = Adw.PreferencesGroup(title="Pipeline")
        self.worker_row = Adw.SwitchRow(
            title="Background worker",
            subtitle="Drain the inbox and sweep failed passes on their own "
                     "(wax pipeline enable|disable). Off: nothing processes until `wax drain`.")
        self.worker_row.connect("notify::active", self._worker_toggled)
        pipeline.add(self.worker_row)
        for title, where in (("Wax root", paths.AUDIO), ("Inbox", paths.INBOX),
                             ("Transcripts vault", paths.VAULT)):
            row = Adw.ActionRow(title=title, subtitle=str(where))
            row.add_css_class("property")
            button = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text=f"Open {where}")
            button.add_css_class("flat")
            button.connect("clicked", lambda _b, p=where: Gio.AppInfo.launch_default_for_uri(
                Gio.File.new_for_path(str(p)).get_uri(), None))
            row.add_suffix(button)
            pipeline.add(row)
        body.append(pipeline)

    # -- data -------------------------------------------------------------------
    def refresh(self) -> None:
        def work():
            return {
                "ep": wax("ep", "list"),
                "catalog": wax("catalog"),
                "queue": wax("queue"),
                "worker": wax("pipeline", "status"),
            }
        _background(work, self._apply)

    def _apply(self, got: dict[str, tuple[bool, Any, str]]) -> None:
        ok, queue_rows, _ = got["queue"]
        rows = queue_rows if ok and isinstance(queue_rows, list) else []
        finished, failed = finished_count(rows), failed_count(rows)
        self.clear_row.set_subtitle(
            f"{finished} finished item(s) in the tray. Hides them from the queue; "
            "notes, audio and ledger history are kept." if ok else
            _markup(f"queue unavailable: {got['queue'][2]}"))
        self.clear_button.set_sensitive(ok and finished > 0)
        self.retry_row.set_subtitle(f"{failed} failed or suspect inbox item(s)." if ok
                                    else "queue unavailable")
        self.retry_button.set_sensitive(ok and failed > 0)

        ok_w, worker, _ = got["worker"]
        self._syncing = True
        self.worker_row.set_active(bool(ok_w and isinstance(worker, dict) and worker.get("enabled")))
        self.worker_row.set_sensitive(ok_w)
        self._syncing = False

        for row in self._pass_rows:
            self.passes_group.remove(row)
        self._pass_rows = []
        ok_e, ep_rows, err = got["ep"]
        catalog = got["catalog"][1] if got["catalog"][0] and isinstance(got["catalog"][1], dict) else {}
        if not ok_e or not isinstance(ep_rows, list):
            row = Adw.ActionRow(title="Passes unavailable", subtitle=_markup(err or "wax ep list failed"))
            self._add_pass_row(row)
            return
        shaped = pass_rows(ep_rows, catalog)
        for entry in shaped["active"]:
            row = Adw.SwitchRow(title=entry["title"], subtitle=_markup(entry["subtitle"]))
            row.set_active(entry["operator_enabled"])
            row.connect("notify::active", self._pass_toggled, entry["slug"])
            self._add_pass_row(row)
        if shaped["inactive"]:
            expander = Adw.ExpanderRow(
                title=f"Not in any plan ({len(shaped['inactive'])})",
                subtitle="Disabled in passes.d and absent from every compiled pipeline.")
            for entry in shaped["inactive"]:
                child = Adw.ActionRow(title=entry["title"], subtitle=_markup(entry["subtitle"]))
                child.add_css_class("dim-label")
                expander.add_row(child)
            self._add_pass_row(expander)

    def _add_pass_row(self, row) -> None:
        self.passes_group.add(row)
        self._pass_rows.append(row)

    # -- actions ---------------------------------------------------------------
    def _clear_finished(self, button) -> None:
        button.set_sensitive(False)

        def done(result):
            ok, _data, err = result
            self.toast("Finished items cleared from the tray" if ok else f"Clear failed: {err}")
            self.refresh()
        _background(lambda: wax("queue", "--clear-completed"), done)

    def _retry_failed(self, button) -> None:
        button.set_sensitive(False)

        def done(result):
            ok, data, err = result
            n = len(data) if ok and isinstance(data, list) else 0
            self.toast(f"Requeued {n} item(s)" if ok else f"Retry failed: {err}")
            self.refresh()
        _background(lambda: wax("retry"), done)

    def _pass_toggled(self, row, _pspec, slug: str) -> None:
        enabled = row.get_active()
        row.set_sensitive(False)

        def done(result):
            ok, _data, err = result
            row.set_sensitive(True)
            if ok:
                self.toast(f"{slug} switched {'on' if enabled else 'off'}")
                return
            self.toast(f"{slug}: {err}")
            # Put the switch back where Wax says it is, without re-firing.
            row.handler_block_by_func(self._pass_toggled)
            row.set_active(not enabled)
            row.handler_unblock_by_func(self._pass_toggled)
        _background(lambda: wax("ep", "enable" if enabled else "disable", slug), done)

    def _worker_toggled(self, row, _pspec) -> None:
        if self._syncing:
            return
        enabled = row.get_active()
        row.set_sensitive(False)

        def done(result):
            ok, _data, err = result
            row.set_sensitive(True)
            self.toast(("Background worker on" if enabled else "Background worker off")
                       if ok else f"Worker: {err}")
            if not ok:
                self._syncing = True
                row.set_active(not enabled)
                self._syncing = False
        _background(lambda: wax("pipeline", "enable" if enabled else "disable"), done)


class LogsPage:
    """`journalctl --follow` into a text view, batched, colour-coded, capped."""

    def __init__(self):
        self.proc = None
        self.cancel = None
        self.pending: list[str] = []
        self.source_index = 0

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      margin_top=12, margin_bottom=12, margin_start=24, margin_end=24)
        self.source = Gtk.DropDown.new_from_strings([label for label, _ in LOG_SOURCES])
        self.source.set_tooltip_text("Which journal to tail")
        self.source.connect("notify::selected", self._source_changed)
        bar.append(self.source)
        self.follow = Gtk.ToggleButton(label="Follow", active=True,
                                       tooltip_text="Keep the newest line in view")
        self.follow.connect("toggled", lambda b: b.get_active() and self._scroll_to_end())
        bar.append(self.follow)
        clear = Gtk.Button(label="Clear", tooltip_text="Empty the view (the journal is untouched)")
        clear.connect("clicked", lambda *_: self.buffer.set_text(""))
        bar.append(clear)
        self.status = Gtk.Label(xalign=1, hexpand=True)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        bar.append(self.status)
        outer.append(bar)
        outer.append(Gtk.Separator())

        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 top_margin=12, bottom_margin=12, left_margin=24, right_margin=24)
        self.buffer = self.view.get_buffer()
        self.buffer.create_tag("error", foreground="#e01b24", weight=Pango.Weight.BOLD)
        self.buffer.create_tag("warn", foreground="#e5a50a")
        self.buffer.create_tag("stamp", foreground="#77767b")
        self.buffer.create_tag("unit", foreground="#3584e4")
        self.view.add_css_class("heyma-log")
        self.scroller = _scroller(self.view)
        self.scroller.get_vadjustment().connect("notify::upper", self._upper_changed)
        self.scroller.get_vadjustment().connect("notify::page-size", self._upper_changed)
        outer.append(self.scroller)
        self.widget = outer
        GLib.timeout_add(150, self._flush)

    # -- process -----------------------------------------------------------------
    def start(self) -> None:
        if self.proc is not None:
            return
        label, units = LOG_SOURCES[self.source_index]
        argv = journal_argv(units)
        try:
            self.proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE
                                           | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as exc:
            self.status.set_label(f"journalctl unavailable: {exc.message}")
            self.proc = None
            return
        self.cancel = Gio.Cancellable()
        self.stream = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self.status.set_label(f"live · {label}")
        self._read()

    def stop(self) -> None:
        if self.cancel is not None:
            self.cancel.cancel()
        if self.proc is not None:
            self.proc.force_exit()
        self.proc = self.cancel = None

    def _read(self) -> None:
        self.stream.read_line_async(GLib.PRIORITY_DEFAULT, self.cancel, self._on_line, self.proc)

    def _on_line(self, stream, result, proc) -> None:
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            return  # cancelled (source switch / window closed)
        if proc is not self.proc:
            return
        if line is None:
            self.status.set_label("journalctl ended")
            self.proc = None
            return
        self.pending.append(line)
        self._read()

    def _source_changed(self, dropdown, _pspec) -> None:
        self.source_index = int(dropdown.get_selected())
        self.stop()
        self.pending.clear()
        self.buffer.set_text("")
        self.start()

    # -- rendering -----------------------------------------------------------------
    def _flush(self) -> bool:
        if not self.pending:
            return True
        lines, self.pending = self.pending, []
        show_unit = len(LOG_SOURCES[self.source_index][1]) > 1
        for line in lines:
            level = log_level(line)
            stamp, unit, message = split_journal_line(line)
            if stamp:
                self.buffer.insert_with_tags_by_name(self.buffer.get_end_iter(), stamp + "  ", "stamp")
            if unit and show_unit:
                self.buffer.insert_with_tags_by_name(self.buffer.get_end_iter(), unit + "  ", "unit")
            end = self.buffer.get_end_iter()
            if level == "info":
                self.buffer.insert(end, message + "\n")
            else:
                self.buffer.insert_with_tags_by_name(end, message + "\n", level)
        overflow = self.buffer.get_line_count() - MAX_LOG_LINES
        if overflow > 0:
            start = self.buffer.get_start_iter()
            cut = self.buffer.get_iter_at_line(overflow)
            cut = cut[1] if isinstance(cut, tuple) else cut
            self.buffer.delete(start, cut)
        if self.follow.get_active():
            self._scroll_to_end()
        return True

    def _scroll_to_end(self) -> None:
        adj = self.scroller.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())

    def _upper_changed(self, adj, _pspec) -> None:
        # A TextView measures new lines lazily, so `upper` keeps growing for a
        # while after an insert; a one-shot scroll lands somewhere in the
        # middle of a fresh backlog. Following = chase every growth.
        if self.follow.get_active():
            adj.set_value(adj.get_upper() - adj.get_page_size())


def _brand():
    """The HeyMa wordmark at the head of the drawer; plain text if the asset is gone.

    A missing logo is cosmetic, so it never stops the window opening — but it
    says so in the drawer rather than quietly leaving a hole.
    """
    if component.LOGO.is_file():
        logo = Gtk.Picture.new_for_filename(str(component.LOGO))
        logo.set_content_fit(Gtk.ContentFit.CONTAIN)
        logo.set_can_shrink(True)
        logo.set_alternative_text("HeyMa")
        logo.set_size_request(-1, 96)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("heyma-brand")
        card.append(logo)
        return card
    title = Adw.WindowTitle(title="HeyMa", subtitle=f"logo missing: {component.LOGO.name}",
                            margin_bottom=12)
    return title


def install_style() -> None:
    """Load STYLESHEET once per display, above the theme and below user CSS."""
    display = Gdk.Display.get_default()
    if display is None or getattr(display, "_heyma_styled", False):
        return
    css = Gtk.CssProvider()
    css.load_from_string(STYLESHEET)
    Gtk.StyleContext.add_provider_for_display(display, css,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    display._heyma_styled = True


class HeyMaWindow:
    def __init__(self, app, page: str = "settings"):
        install_style()
        self.window = Adw.ApplicationWindow(application=app, title="HeyMa",
                                            default_width=1100, default_height=760)
        self.window.set_size_request(360, 360)
        self.toasts = Adw.ToastOverlay()

        self.settings = SettingsPage(self.toast)
        self.logs = LogsPage()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               hexpand=True, vexpand=True)
        self.stack.add_named(self.settings.widget, "settings")
        self.stack.add_named(self.logs.widget, "logs")
        self.toasts.set_child(self.stack)

        # -- drawer -------------------------------------------------------------
        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.nav.add_css_class("navigation-sidebar")
        self._nav_rows = {}
        for key, label, icon in PAGES:
            box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=label, xalign=0))
            row = Gtk.ListBoxRow(child=box)
            row.page_key = key
            self.nav.append(row)
            self._nav_rows[key] = row
        self.nav.connect("row-selected", self._nav_selected)

        sidebar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar(show_title=False)
        sidebar.add_top_bar(sidebar_header)
        drawer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        drawer.append(_brand())
        drawer.append(self.nav)
        sidebar.set_content(_scroller(drawer))

        # -- content ------------------------------------------------------------
        content = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="Settings")
        header.set_title_widget(self.title)
        self.drawer_button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic",
                                              tooltip_text="Toggle the drawer (F9)")
        header.pack_start(self.drawer_button)
        content.add_top_bar(header)
        content.set_content(self.toasts)

        self.split = Adw.OverlaySplitView(sidebar=sidebar, content=content,
                                          min_sidebar_width=180, max_sidebar_width=240)
        self.split.bind_property("show-sidebar", self.drawer_button, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL
                                 | GObject.BindingFlags.SYNC_CREATE)
        self.window.set_content(self.split)

        # Narrow window: the drawer folds into an overlay instead of squeezing
        # the content, and starts hidden.
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 600px"))
        breakpoint.add_setter(self.split, "collapsed", True)
        self.window.add_breakpoint(breakpoint)

        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("F9"),
            Gtk.CallbackAction.new(lambda *_: (self.split.set_show_sidebar(
                not self.split.get_show_sidebar()), True)[1])))
        keys.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>w"),
            Gtk.CallbackAction.new(lambda *_: (self.window.close(), True)[1])))
        self.window.add_controller(keys)
        self.window.connect("close-request", self._closing)

        self.show_page(page)
        self.settings.refresh()

    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=4))

    def show_page(self, key: str) -> None:
        row = self._nav_rows.get(key) or self._nav_rows["settings"]
        self.nav.select_row(row)

    def _nav_selected(self, _listbox, row) -> None:
        if row is None:
            return
        key = row.page_key
        self.stack.set_visible_child_name(key)
        self.title.set_title(dict((k, label) for k, label, _ in PAGES)[key])
        if key == "logs":
            self.logs.start()  # lazily: no journalctl until someone looks
        elif key == "settings":
            self.settings.refresh()
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _closing(self, *_):
        self.logs.stop()
        return False


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="heyma")
    ap.add_argument("--page", choices=[key for key, _, _ in PAGES], default="settings")
    args, rest = ap.parse_known_args(argv)
    _load_gtk()
    # The installed icon (deploy/install-desktop-entry) carries the app id, so
    # X11 window decorations find it too; Wayland docks read the .desktop file.
    Gtk.Window.set_default_icon_name(APP_ID)
    app = Adw.Application(application_id=APP_ID,
                          flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    state: dict[str, Any] = {}

    def activate(application):
        window = state.get("window")
        if window is None:
            window = state["window"] = HeyMaWindow(application, args.page)
        else:
            window.show_page(args.page)
        window.window.present()

    app.connect("activate", activate)
    return app.run([sys.argv[0], *rest])


if __name__ == "__main__":
    raise SystemExit(main())
