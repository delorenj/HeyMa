import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from heyma_shell import adapters, cli, registry

from fakes import FakeWorld, ok, wax_snapshot


def world():
    return FakeWorld(
        present=["/repo/bin/wax", "/repo/bin/telephony", "/usr/bin/vinyl"],
        files={"/home/u/HeyMa/var/state.json": wax_snapshot()},
        cli={("/repo/bin/wax", "--json", "status"): ok(wax_snapshot()),
             ("/usr/bin/vinyl", "status"): ok({"state": "idle"}),
             ("/repo/bin/telephony", "--json", "status"): ok({"mode": "shadow", "ring_enabled": False})},
        http={"https://phonograph.intellifornia.com/healthz": (200, "{}")})


class RegistryTest(unittest.TestCase):
    def test_collect_returns_all_four_sections_in_order(self):
        doc = registry.collect(world().context())
        self.assertEqual(doc["schema"], "heyma.shell.status.v1")
        self.assertEqual([s["id"] for s in doc["sections"]],
                         ["wax", "vinyl", "telephony", "phonograph"])
        self.assertTrue(all(s["installed"] for s in doc["sections"]))
        json.dumps(doc)  # the whole document must be serialisable

    def test_one_raising_adapter_is_a_failed_section_not_a_failed_registry(self):
        with patch.dict(adapters.ADAPTERS, {"vinyl": lambda ctx: 1 / 0}):
            doc = registry.collect(world().context())
        by_id = registry.by_id(doc)
        self.assertEqual(by_id["vinyl"]["health"], "fail")
        self.assertIn("ZeroDivisionError", by_id["vinyl"]["summary"])
        self.assertEqual(by_id["wax"]["health"], "ok")

    def test_text_form_has_one_line_per_product(self):
        text = registry.render_text(registry.collect(world().context()))
        self.assertEqual(len(text.splitlines()), 4)
        self.assertTrue(text.startswith("wax"))


class CliRoutingTest(unittest.TestCase):
    def test_no_args_and_page_open_the_window(self):
        for argv in ([], ["--page", "settings"], ["--page=logs"]):
            with self.subTest(argv=argv), patch.object(cli, "open_window", return_value=0) as win:
                cli.main(argv)
                win.assert_called_once_with(argv)

    def test_window_launcher_is_the_existing_wax_one(self):
        self.assertEqual(cli.WINDOW.parts[-4:], ("components", "wax", "bin", "heyma"))

    def test_status_json_prints_the_registry(self):
        with patch.object(registry, "collect", return_value={"schema": "x", "sections": []}), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["status", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["schema"], "x")

    def test_status_exit_code_is_0_only_when_nothing_is_degraded(self):
        """`heyma status && …` must not pass with waxd down (review 2026-11-18)."""
        for healths, code in ((["ok", "off", "ok"], 0), (["ok", "warn"], 2), (["fail", "ok"], 2),
                              ([], 0)):
            doc = {"schema": "x", "sections": [{"id": f"p{i}", "health": h, "summary": ""}
                                                for i, h in enumerate(healths)]}
            for argv in (["status"], ["status", "--json"]):
                with self.subTest(healths=healths, argv=argv), \
                        patch.object(registry, "collect", return_value=doc), redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(argv), code)

    def test_status_text(self):
        doc = registry.collect(world().context())
        with patch.object(registry, "collect", return_value=doc), redirect_stdout(io.StringIO()) as out:
            cli.main(["status"])
        self.assertIn("phonograph", out.getvalue())

    def test_tray_subcommand_runs_the_tray(self):
        import sys
        import types
        fake = types.ModuleType("heyma_shell.tray_ui")
        fake.main = lambda: 7
        with patch.dict(sys.modules, {"heyma_shell.tray_ui": fake}), \
                patch("heyma_shell.tray_ui", fake, create=True):
            self.assertEqual(cli.main(["tray"]), 7)


if __name__ == "__main__":
    unittest.main()
