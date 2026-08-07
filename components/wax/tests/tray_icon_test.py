import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


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
            "duration_s": 125, "state": "archived", "active": True, "stage": "transcribe",
        })
        done = self.tray.queue_label({
            "item_id": "abc", "orig_name": "meeting.ogg", "bytes": 1,
            "state": "complete", "active": False,
        })
        self.assertEqual(queued, "• meeting.ogg  (1.5 MiB)")
        self.assertEqual(active, "◐ Transcribing: meeting.ogg  (2:05 · 1.0 KiB)")
        self.assertTrue(done.startswith("✓ meeting.ogg"))
        failed = self.tray.queue_label({
            "item_id": "bad", "orig_name": "broken.ogg", "bytes": 12,
            "state": "failed", "active": False,
        })
        self.assertTrue(failed.startswith("⚠ Failed: broken.ogg"))

    def test_failed_items_force_yellow_even_while_worker_is_active(self):
        colour, tooltip = self.tray.colour_for({
            "stream": {"state": "ready"},
            "inbox": {"state": "ready-and-active", "pending": 4},
            "items": {"failed": 2},
        })
        self.assertEqual(colour, "yellow")
        self.assertIn("failed items: 2", tooltip)


if __name__ == "__main__":
    unittest.main()
