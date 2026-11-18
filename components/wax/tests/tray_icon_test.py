import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
sys.path.insert(0, str(COMPONENT_ROOT / "src"))


def import_tray_without_desktop_dependencies():
    gi = types.ModuleType("gi")
    gi.require_version = lambda *_: None

    repository = types.ModuleType("gi.repository")
    repository.AyatanaAppIndicator3 = types.SimpleNamespace()
    repository.GLib = types.SimpleNamespace()
    repository.Gio = types.SimpleNamespace()
    repository.Gtk = types.SimpleNamespace(Menu=type("Menu", (), {}))

    with patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
        sys.modules.pop("wax.tray", None)
        return importlib.import_module("wax.tray")


class FakeMenu:
    def __init__(self):
        self.children = []

    def append(self, child):
        self.children.append(child)

    def get_children(self):
        return list(self.children)

    def remove(self, child):
        self.children.remove(child)

    def show_all(self):
        pass


class FakeMenuItem:
    def __init__(self, label=""):
        self.label = label
        self.sensitive = True
        self.tooltip = ""
        self.signals = {}

    def set_label(self, label):
        self.label = label

    def set_sensitive(self, sensitive):
        self.sensitive = sensitive

    def set_tooltip_text(self, tooltip):
        self.tooltip = tooltip

    def set_submenu(self, submenu):
        self.submenu = submenu

    def destroy(self):
        self.destroyed = True

    def connect(self, signal, callback, *args):
        self.signals[signal] = (callback, args)

    def activate(self):
        callback, args = self.signals["activate"]
        callback(self, *args)


class FakeSeparatorMenuItem(FakeMenuItem):
    pass


class TrayIconTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tray = import_tray_without_desktop_dependencies()

    def test_repo_assets_supply_all_icon_colours(self):
        expected = {
            "green": COMPONENT_ROOT / "assets/tray/wax-tray-icon-green.png",
            "red": COMPONENT_ROOT / "assets/tray/wax-tray-icon-red.png",
            "yellow": COMPONENT_ROOT / "assets/tray/wax-tray-icon-yellow.png",
        }
        self.assertEqual(Path(self.tray.ICON_DIR), COMPONENT_ROOT / "assets/tray")
        self.assertEqual(self.tray.ICONS, {key: str(path) for key, path in expected.items()})
        for path in expected.values():
            self.assertTrue(path.is_file(), path)

    def test_ready_and_waiting_are_green(self):
        for stream_state, inbox_state in (("ready", "ready-and-active"), ("waiting", "ready")):
            with self.subTest(stream=stream_state, inbox=inbox_state):
                colour, _ = self.tray.colour_for(
                    {"stream": {"state": stream_state}, "inbox": {"state": inbox_state}}
                )
                self.assertEqual(colour, "green")

    def test_pipeline_faults_and_stopped_are_yellow(self):
        snapshots = (
            {"stream": {"state": "error"}, "inbox": {"state": "ready-and-active"}},
            {"stream": {"state": "ready"}, "inbox": {"state": "error"}},
            {"stream": {"state": "ready"}, "inbox": {"state": "stopped"}},
        )
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                self.assertEqual(self.tray.colour_for(snapshot)[0], "yellow")

    def test_switched_off_passes_are_named_but_do_not_tint_the_icon(self):
        colour, tip = self.tray.colour_for({
            "stream": {"state": "ready"}, "inbox": {"state": "ready-and-active"},
            "passes": {"failed": 0, "slugs": [], "operator_disabled": ["summarize", "title-slug"]},
        })
        self.assertEqual(colour, "green")
        self.assertIn("passes off: summarize, title-slug", tip)

    def test_recording_is_red_even_when_pipeline_has_faults(self):
        snapshot = {
            "stream": {"state": "recording", "cause_code": "stream_fault"},
            "inbox": {"state": "error", "pending": 4, "cause_code": "pipeline_fault"},
        }
        self.assertEqual(self.tray.colour_for(snapshot)[0], "red")

    def test_queue_labels_show_state_name_and_size(self):
        queued = self.tray.queue_label({
            "item_id": "abc", "orig_name": "meeting.ogg", "bytes": 1572864,
            "state": "pending", "active": False,
        })
        active = self.tray.queue_label({
            "item_id": "abc", "orig_name": "meeting.ogg", "bytes": 1024,
            "duration_s": 125, "state": "archived", "active": True,
            "stage": "diarize", "progress_pct": 37,
        })
        done = self.tray.queue_label({
            "item_id": "abc", "orig_name": "meeting.ogg", "bytes": 1,
            "state": "complete", "active": False, "md_path": "/vault/derived-note.md",
        })
        self.assertEqual(queued, "• meeting.ogg  (1.5 MiB)")
        self.assertEqual(active, "◐ Diarizing 37%: meeting.ogg  (2:05 · 1.0 KiB)")
        self.assertTrue(done.startswith("✓ derived-note.md"))
        failed = self.tray.queue_label({
            "item_id": "bad", "orig_name": "broken.ogg", "bytes": 12,
            "state": "failed", "active": False,
        })
        self.assertTrue(failed.startswith("⚠ Failed: broken.ogg"))

    def test_failed_items_force_yellow_even_while_worker_is_active(self):
        colour, tooltip = self.tray.colour_for({
            "stream": {"state": "ready"},
            "inbox": {"state": "ready-and-active", "pending": 4},
            "queue": {"failed": 2},
        })
        self.assertEqual(colour, "yellow")
        self.assertIn("failed items: 2", tooltip)

    def test_historical_failures_do_not_tint_an_empty_queue(self):
        colour, tooltip = self.tray.colour_for({
            "stream": {"state": "ready"},
            "inbox": {"state": "ready-and-waiting", "pending": 0},
            "queue": {"failed": 0},
            "items": {"failed": 99},
        })
        self.assertEqual(colour, "green")
        self.assertNotIn("failed items", tooltip)

    def test_obsidian_uri_uses_absolute_transcript_path(self):
        uri = self.tray.obsidian_uri("/home/me/My Vault/a note.md")
        self.assertEqual(uri, "obsidian://open?path=/home/me/My%20Vault/a%20note.md")

    def test_registration_matches_the_exact_indicator_id(self):
        registered = self.tray.registered_as
        self.assertTrue(registered("heyma", ":1.9@/org/ayatana/NotificationItem/heyma"))
        self.assertTrue(registered("wax", ":1.9@/org/ayatana/NotificationItem/wax"))
        self.assertFalse(registered("wax", ":1.9@/org/ayatana/NotificationItem/heyma-wax"))
        self.assertFalse(registered("vinyl", ":1.9@/org/ayatana/NotificationItem/heyma"))
        self.assertFalse(registered("heyma", ":1.9@/StatusNotifierItem"))

    def test_both_trays_share_one_indicator_lifecycle(self):
        host, queue = self.tray.IndicatorHost, self.tray.QueueMenu
        self.assertTrue(issubclass(self.tray.Tray, host) and issubclass(self.tray.Tray, queue))
        self.assertEqual((self.tray.Tray.INDICATOR_ID, self.tray.Tray.TITLE), ("wax", "Wax"))

    def render_queue(self, items, silent=None):
        subject = self.tray.Tray.__new__(self.tray.Tray)
        subject.queue_menu = FakeMenu()
        subject.item_queue = FakeMenuItem()
        subject._queue_items = items
        subject._silent = silent or {}
        subject._queue_rows = {}
        subject._spinner_frame = 0
        subject.on_retry_item = MagicMock()
        subject.on_retry_passes = MagicMock()
        subject.on_open_transcript = MagicMock()
        subject.on_clear_completed = MagicMock()
        subject.on_skip = MagicMock()
        fake_gtk = types.SimpleNamespace(
            Menu=FakeMenu,
            MenuItem=FakeMenuItem,
            SeparatorMenuItem=FakeSeparatorMenuItem,
        )
        with patch.object(self.tray, "Gtk", fake_gtk):
            subject._render_queue()
        return subject

    @staticmethod
    def submenu_labels(row):
        return [child.label for child in row.submenu.children]

    @staticmethod
    def submenu_row(row, prefix):
        return next(child for child in row.submenu.children if child.label.startswith(prefix))

    def test_failed_item_submenu_says_why_and_offers_retry_and_skip(self):
        subject = self.render_queue([{
            "item_id": "failed-item",
            "orig_name": "memo.m4a",
            "state": "failed",
            "active": False,
            "error": {"cause_code": "transcribe_failed",
                      "evidence": "EMPTY TRANSCRIPT from 384.5s of audio; not published"},
        }])

        row = subject._queue_rows["failed-item"]
        # The reason is in the LABEL: dbusmenu hosts drop tooltips.
        self.assertIn("— transcribe_failed: EMPTY TRANSCRIPT", row.label)
        labels = self.submenu_labels(row)
        self.assertTrue(labels[0].startswith("Why: EMPTY TRANSCRIPT from 384.5s"))
        self.assertFalse(row.submenu.children[0].sensitive)

        self.submenu_row(row, "Retry").activate()
        self.assertFalse(row.sensitive)
        subject.on_retry_item.assert_called_once_with("failed-item")

        self.submenu_row(row, "Skip").activate()
        subject.on_skip.assert_called_once_with("failed-item")
        subject.on_retry_passes.assert_not_called()

    def test_failed_item_without_a_verified_backup_is_never_offered_skip(self):
        subject = self.render_queue([{
            "item_id": "no-backup",
            "orig_name": "memo.m4a",
            "state": "failed",
            "active": False,
            "error": {"cause_code": "archive_failed", "evidence": "S3 upload failed"},
        }])

        row = subject._queue_rows["no-backup"]
        skip = self.submenu_row(row, "Skip")
        self.assertIn("no verified backup", skip.label)
        self.assertFalse(skip.sensitive)
        self.assertNotIn("activate", skip.signals)
        self.submenu_row(row, "Retry").activate()
        subject.on_retry_item.assert_called_once_with("no-backup")

    def test_waiting_row_skip_is_a_submenu_not_a_right_click(self):
        subject = self.render_queue([{
            "item_id": "waiting", "orig_name": "w.m4a", "state": "pending", "active": False,
        }])
        row = subject._queue_rows["waiting"]
        self.assertNotIn("button-press-event", row.signals)
        self.submenu_row(row, "Skip").activate()
        subject.on_skip.assert_called_once_with("waiting")

    def test_silent_recordings_are_listed_with_levels_and_transcribe_anyway(self):
        silent = {
            "count": 2, "more": 0,
            "sources": [{"source": "dropoff", "count": 2}],
            "recent": [
                {"item_id": "s1", "orig_name": "a.m4a", "duration_s": 4748.7,
                 "mean_db": -79.5, "max_db": -40.1, "source": "dropoff"},
                {"item_id": "s2", "orig_name": "b.m4a", "duration_s": 61,
                 "mean_db": -81.9, "max_db": -53.5, "source": "dropoff"},
            ],
        }
        subject = self.render_queue([], silent=silent)
        labels = [child.label for child in subject.queue_menu.children]

        self.assertIn("0 queued · 0 done · 0 failed · 2 silent", labels[0])
        self.assertTrue(any("2 silent recording(s) parked" in x and "check the mic" in x
                            for x in labels))
        self.assertTrue(any("source silent: dropoff sent 2 in a row" in x for x in labels))
        row = next(c for c in subject.queue_menu.children if c.label.startswith("🔇 a.m4a"))
        self.assertEqual(row.label, "🔇 a.m4a  (79:08 · mean -80 dB)")
        self.assertIn("mean -79.5 dB, peak -40.1 dB", self.submenu_labels(row)[0])
        self.submenu_row(row, "Transcribe anyway").activate()
        subject.on_retry_item.assert_called_once_with("s1")
        self.assertIn("2 silent", subject.item_queue.label)

    def test_no_silent_section_when_nothing_is_parked(self):
        subject = self.render_queue([], silent={"count": 0})
        self.assertEqual(len(subject.queue_menu.children), 1)
        self.assertNotIn("silent", subject.queue_menu.children[0].label)

    def test_failed_tooltip_names_causes_and_outcome_terms_tint_yellow(self):
        colour, tip = self.tray.colour_for({
            "stream": {"state": "ready"}, "inbox": {"state": "error"},
            "queue": {"failed": 3},
            "failed": [{"cause_code": "transcribe_failed"}] * 2 + [{"cause_code": "archive_failed"}],
        })
        self.assertEqual(colour, "yellow")
        self.assertIn("failed items: 3 (transcribe_failed x2, archive_failed x1)", tip)

        for extra, expected in (
            ({"transcription": {"degraded": True, "newest_failed": ["a", "b", "c"]}},
             "transcription degraded: newest 3 recording(s) produced no transcript"),
            ({"silent": {"count": 5, "sources": [{"source": "dropoff", "count": 5}]}},
             "source silent: dropoff sent 5 silent recording(s) in a row"),
        ):
            with self.subTest(extra=extra):
                colour, tip = self.tray.colour_for({
                    "stream": {"state": "ready"}, "inbox": {"state": "ready-and-waiting"}, **extra})
                self.assertEqual(colour, "yellow")
                self.assertIn(expected, tip)

    def test_parked_silence_without_a_silent_source_is_named_but_stays_green(self):
        colour, tip = self.tray.colour_for({
            "stream": {"state": "ready"}, "inbox": {"state": "ready-and-waiting"},
            "silent": {"count": 1, "sources": []},
        })
        self.assertEqual(colour, "green")
        self.assertIn("silent recordings parked: 1 (not transcribed)", tip)

    def test_rebuild_destroys_old_rows_so_submenus_cannot_leak(self):
        items = [{"item_id": "f", "orig_name": "m.m4a", "state": "failed", "active": False,
                  "error": {"cause_code": "transcribe_failed", "evidence": "x"}}]
        subject = self.render_queue(items)
        old = list(subject.queue_menu.children)
        fake_gtk = types.SimpleNamespace(Menu=FakeMenu, MenuItem=FakeMenuItem,
                                         SeparatorMenuItem=FakeSeparatorMenuItem)
        with patch.object(self.tray, "Gtk", fake_gtk):
            subject._render_queue()
        self.assertTrue(all(getattr(child, "destroyed", False) for child in old))

    def test_cause_histogram_never_reads_a_sample_as_the_total(self):
        _, tip = self.tray.colour_for({
            "stream": {"state": "ready"}, "inbox": {"state": "error"}, "queue": {"failed": 12},
            "failed": [{"cause_code": "transcribe_failed"}] * 5, "failed_more": 7,
        })
        self.assertIn("failed items: 12 (transcribe_failed x5, +7 more)", tip)

    def test_silent_source_is_explained_even_with_nothing_parked(self):
        subject = self.render_queue([], silent={"count": 0,
                                                "sources": [{"source": "dropoff", "count": 3}]})
        labels = [child.label for child in subject.queue_menu.children]
        self.assertTrue(any("source silent: dropoff sent 3 in a row" in x for x in labels))

    def test_long_names_are_clipped_so_the_reason_survives(self):
        label = self.tray.queue_label({
            "item_id": "t", "orig_name": "telnyx-" + "a" * 160 + ".mp3", "state": "failed",
            "active": False, "error": {"cause_code": "asr_empty_output", "evidence": "no words"},
        })
        self.assertIn("— asr_empty_output: no words", label)
        self.assertLess(len(label), 140)

    def test_same_cause_new_failure_still_rebuilds(self):
        subject = self.tray.Tray.__new__(self.tray.Tray)
        subject._queue_signature = None
        subject._queue_items, subject._silent = [], {}
        row = {"item_id": "f", "state": "failed", "error": {"cause_code": "c", "evidence": "e",
                                                            "at": "t1"}}
        glib = types.SimpleNamespace(idle_add=MagicMock())
        with patch.object(self.tray, "GLib", glib):
            subject.set_queue([row])
            subject.set_queue([dict(row, error={**row["error"], "at": "t2"})])
        self.assertEqual(glib.idle_add.call_count, 2)

    def test_set_queue_rebuilds_when_only_the_silent_list_changes(self):
        subject = self.tray.Tray.__new__(self.tray.Tray)
        subject._queue_signature = None
        subject._queue_items, subject._silent = [], {}
        glib = types.SimpleNamespace(idle_add=MagicMock())
        with patch.object(self.tray, "GLib", glib):
            subject.set_queue([], {"count": 0})
            subject.set_queue([], {"count": 0})
            subject.set_queue([], {"count": 1, "recent": [{"item_id": "s1", "mean_db": -80}]})
        self.assertEqual(glib.idle_add.call_count, 2)
        self.assertEqual(subject._silent["count"], 1)

    def test_clicking_failed_pass_retries_only_that_rows_passes(self):
        subject = self.render_queue([{
            "item_id": "completed-item",
            "orig_name": "recording.ogg",
            "state": "complete",
            "active": False,
            "md_path": "/vault/recording.md",
            "passes_failed": 2,
            "passes_failed_slugs": ["title-slug", "wikification"],
        }])

        row = subject._queue_rows["completed-item"]
        self.assertIn("Click to retry the failed pass.", row.tooltip)
        row.activate()

        self.assertFalse(row.sensitive)
        subject.on_retry_passes.assert_called_once_with(
            "completed-item", ("title-slug", "wikification")
        )
        subject.on_open_transcript.assert_not_called()

    def test_silent_recording_stays_red_and_flashes_yellow(self):
        silent = {"stream": {"state": "recording", "silent": True}, "inbox": {"state": "ready"}}
        colour, tooltip = self.tray.colour_for(silent)
        self.assertEqual(colour, "red")
        self.assertIn("mic muted", tooltip)
        self.assertEqual(self.tray.flash_for(silent), "yellow")

    def test_flash_needs_both_recording_and_silence(self):
        for stream in ({"state": "recording", "silent": False},
                       {"state": "recording"},
                       {"state": "ready", "silent": True}):
            with self.subTest(stream=stream):
                snap = {"stream": stream, "inbox": {"state": "ready"}}
                self.assertIsNone(self.tray.flash_for(snap))
                self.assertNotIn("mic muted", self.tray.colour_for(snap)[1])

    def blinking_tray(self):
        subject = self.tray.Tray.__new__(self.tray.Tray)
        subject.indicator = MagicMock()
        subject._colour, subject._tip, subject._flash, subject._flash_on = None, "", None, False
        return subject

    def shown(self, subject):
        return subject.indicator.set_icon_full.call_args[0][0]

    def test_blink_alternates_without_logging_a_transition_per_frame(self):
        subject = self.blinking_tray()
        fake_glib = types.SimpleNamespace(idle_add=lambda fn: fn())
        with patch.object(self.tray, "GLib", fake_glib), \
                self.assertLogs(self.tray.log, "INFO") as logs:
            subject.set("red", "tip", True, flash="yellow")
            frames = []
            for _ in range(3):
                subject._blink()
                frames.append(self.shown(subject))
            # Mid-flash (yellow phase), the 1 Hz update must render the current
            # phase, not snap back to the steady red.
            subject.set("red", "tip", True, flash="yellow")
            self.assertEqual(self.shown(subject), self.tray.ICONS["yellow"])
        self.assertEqual(frames, [self.tray.ICONS["yellow"], self.tray.ICONS["red"],
                                  self.tray.ICONS["yellow"]])
        self.assertEqual(len(logs.records), 1)

    def test_silence_ending_restores_the_steady_icon(self):
        subject = self.blinking_tray()
        fake_glib = types.SimpleNamespace(idle_add=lambda fn: fn())
        with patch.object(self.tray, "GLib", fake_glib):
            subject.set("red", "tip", True, flash="yellow")
            subject._blink()
            self.assertEqual(self.shown(subject), self.tray.ICONS["yellow"])
            subject.set("red", "tip", True)
            self.assertEqual(self.shown(subject), self.tray.ICONS["red"])
            subject.indicator.set_icon_full.reset_mock()
            subject._blink()
        subject.indicator.set_icon_full.assert_not_called()
        self.assertFalse(subject._flash_on)


class TrayProcessDisplayTest(unittest.TestCase):
    """waxd started by an autologin before the compositor published its display
    must still get a tray once the session catches up (2026-10-09)."""

    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("wax.tray_process")

    def setUp(self):
        env = patch.dict("os.environ", {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        import os
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "WAX_TRAY"):
            os.environ.pop(key, None)

    def test_late_display_spawns_tray_on_retry(self):
        import os
        subject = self.mod.TrayProcess()
        with patch.object(self.mod, "session_env", return_value={}), \
                patch.object(self.mod.subprocess, "Popen") as popen:
            subject.poll()
        self.assertEqual(subject.reason, "no display")
        popen.assert_not_called()

        subject.next_attempt = 0
        with patch.object(self.mod, "session_env", return_value={"WAYLAND_DISPLAY": "wayland-0"}), \
                patch.object(self.mod.subprocess, "Popen") as popen:
            subject.poll()
        self.assertEqual(subject.reason, "starting")
        self.assertEqual(popen.call_args.kwargs["env"]["WAYLAND_DISPLAY"], "wayland-0")
        self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-0")

    def test_tray_is_on_unless_switched_off(self):
        for value in ("", "on", "1", "anything"):
            with self.subTest(value=value):
                self.assertTrue(self.mod.tray_enabled({"WAX_TRAY": value}))
        self.assertTrue(self.mod.tray_enabled({}))
        for value in ("off", "OFF", "0", "false", "no", " heyma ", "external"):
            with self.subTest(value=value):
                self.assertFalse(self.mod.tray_enabled({"WAX_TRAY": value}))

    def test_switched_off_spawns_nothing_and_reports_healthy(self):
        import os
        os.environ["WAX_TRAY"] = "off"
        subject = self.mod.TrayProcess()
        with patch.object(self.mod, "session_env", return_value={"WAYLAND_DISPLAY": "wayland-0"}), \
                patch.object(self.mod, "owner_state", return_value="active"), \
                patch.object(self.mod.subprocess, "Popen") as popen:
            subject.poll()
        popen.assert_not_called()
        # waxd's `tray_unavailable` condition keys on available/reason, and the
        # icon is not missing: the HeyMa tray owns it.
        self.assertTrue(subject.available)
        self.assertEqual(subject.reason, self.mod.EXTERNAL_REASON)

    def test_switching_off_stops_a_tray_child_already_running(self):
        import os
        subject = self.mod.TrayProcess()
        child = MagicMock()
        child.poll.return_value = None
        subject.child = child
        os.environ["WAX_TRAY"] = "off"
        with patch.object(self.mod, "owner_state", return_value="active"):
            subject.poll()
        child.terminate.assert_called_once()
        self.assertIsNone(subject.child)

    def poll_with_owner(self, subject, state):
        subject.owner_next_check = 0
        with patch.object(self.mod, "session_env", return_value={"WAYLAND_DISPLAY": "wayland-0"}), \
                patch.object(self.mod, "owner_state", return_value=state), \
                patch.object(self.mod.subprocess, "Popen") as popen:
            subject.poll()
        return popen

    def test_switched_off_with_the_owner_gone_takes_the_icon_back(self):
        """Quit HeyMa (or a disabled heyma-tray) must not leave a recorder with no
        icon behind a green `tray` block (review 2026-11-18)."""
        import os
        os.environ["WAX_TRAY"] = "off"
        subject = self.mod.TrayProcess()
        # One "inactive" is inside the grace: a login or a restart, not a quit.
        self.poll_with_owner(subject, "inactive").assert_not_called()
        self.assertEqual(subject.reason, self.mod.EXTERNAL_REASON)
        popen = self.poll_with_owner(subject, "inactive")
        popen.assert_called_once()
        subject.child = popen.return_value
        subject.child.poll.return_value = None
        self.poll_with_owner(subject, "inactive")
        self.assertTrue(subject.available)
        self.assertIn("fallback: heyma-tray.service is inactive", subject.reason)

    def test_owner_returning_hands_the_icon_back(self):
        import os
        os.environ["WAX_TRAY"] = "off"
        subject = self.mod.TrayProcess()
        subject.owner_missing = self.mod.OWNER_GRACE
        child = MagicMock()
        child.poll.return_value = None
        subject.child = child
        self.poll_with_owner(subject, "activating")
        child.terminate.assert_called_once()
        self.assertIsNone(subject.child)
        self.assertEqual(subject.reason, self.mod.EXTERNAL_REASON)

    def test_an_unknown_owner_state_never_flips_the_icon(self):
        import os
        os.environ["WAX_TRAY"] = "off"
        subject = self.mod.TrayProcess()
        for _ in range(5):
            self.poll_with_owner(subject, "unknown").assert_not_called()
        self.assertEqual(subject.reason, self.mod.EXTERNAL_REASON)

    def test_owner_state_is_asked_at_most_every_few_seconds(self):
        import os
        os.environ["WAX_TRAY"] = "off"
        subject = self.mod.TrayProcess()
        with patch.object(self.mod, "owner_state", return_value="active") as asked:
            for _ in range(10):
                subject.poll()
        self.assertEqual(asked.call_count, 1)

    def test_owner_state_reads_systemctl_and_survives_its_absence(self):
        with patch.object(self.mod.subprocess, "run",
                          return_value=MagicMock(stdout="activating\n")) as run:
            self.assertEqual(self.mod.owner_state(), "activating")
        self.assertEqual(run.call_args.args[0],
                         ["systemctl", "--user", "is-active", "heyma-tray.service"])
        with patch.object(self.mod.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(self.mod.owner_state(), "unknown")

    def test_default_without_the_switch_still_spawns_the_wax_tray(self):
        subject = self.mod.TrayProcess()
        with patch.object(self.mod, "session_env", return_value={"WAYLAND_DISPLAY": "wayland-0"}), \
                patch.object(self.mod.subprocess, "Popen") as popen:
            subject.poll()
        popen.assert_called_once()

    def test_session_env_reads_the_user_manager(self):
        out = "PATH=/usr/bin\nDISPLAY=:0\nWAYLAND_DISPLAY=wayland-0\nXAUTHORITY=/run/user/1000/x\n"
        with patch.object(self.mod.subprocess, "run",
                          return_value=MagicMock(stdout=out)):
            env = self.mod.session_env()
        self.assertEqual(env, {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0",
                               "XAUTHORITY": "/run/user/1000/x"})

    def test_session_env_falls_back_to_runtime_wayland_socket(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as runtime:
            for name in ("wayland-1", "wayland-1.lock"):
                Path(runtime, name).touch()
            os.environ["XDG_RUNTIME_DIR"] = runtime
            with patch.object(self.mod.subprocess, "run", side_effect=OSError("no systemctl")):
                env = self.mod.session_env()
        self.assertEqual(env, {"WAYLAND_DISPLAY": "wayland-1"})


if __name__ == "__main__":
    unittest.main()
