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

    def render_queue(self, items):
        subject = self.tray.Tray.__new__(self.tray.Tray)
        subject.queue_menu = FakeMenu()
        subject.item_queue = FakeMenuItem()
        subject._queue_items = items
        subject._queue_rows = {}
        subject._spinner_frame = 0
        subject.on_retry_item = MagicMock()
        subject.on_retry_passes = MagicMock()
        subject.on_open_transcript = MagicMock()
        subject.on_clear_completed = MagicMock()
        fake_gtk = types.SimpleNamespace(
            MenuItem=FakeMenuItem,
            SeparatorMenuItem=FakeSeparatorMenuItem,
        )
        with patch.object(self.tray, "Gtk", fake_gtk):
            subject._render_queue()
        return subject

    def test_clicking_failed_item_invokes_item_retry_and_disables_row(self):
        subject = self.render_queue([{
            "item_id": "failed-item",
            "orig_name": "failed.ogg",
            "state": "failed",
            "active": False,
            "error": {"cause_code": "archive_failed"},
        }])

        row = subject._queue_rows["failed-item"]
        self.assertIn("Click to retry this item.", row.tooltip)
        row.activate()

        self.assertFalse(row.sensitive)
        subject.on_retry_item.assert_called_once_with("failed-item")
        subject.on_retry_passes.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
