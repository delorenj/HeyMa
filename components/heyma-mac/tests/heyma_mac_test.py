"""Source contract for the Mac app. Swift is only compiled on a Mac (build-app.sh),
so these tests pin the behaviour that has broken before and the release invariants."""
import plistlib
import re
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SOURCE = (HERE / "main.swift").read_text()


class AppSourceTest(unittest.TestCase):
    def test_settings_window_retained_after_close(self):
        self.assertIn("window.isReleasedWhenClosed = false", SOURCE)

    def test_login_checkbox_repairs_a_stale_agent(self):
        self.assertIn("toggleAgent.prepareCheckbox(loginCheckbox)", SOURCE)
        self.assertIn("!FileManager.default.fileExists(atPath: executable)", SOURCE)
        self.assertIn("executable != current.path", SOURCE)

    def test_section_model_matches_the_linux_tray(self):
        # Top toggle, then Wax seam, Vinyl, Phonograph, Settings, Quit.
        order = [SOURCE.index(text) for text in (
            'dictationItem.target = self', "waxSectionItems()", 'NSMenu(title: "Vinyl")',
            'NSMenu(title: "Phonograph")', 'withTitle: "Settings…"', 'withTitle: "Quit HeyMa"')]
        self.assertEqual(order, sorted(order))

    def test_wax_seam_adds_no_dead_item(self):
        self.assertIn("private func waxSectionItems() -> [NSMenuItem] { [] }", SOURCE)
        self.assertIn("if !wax.isEmpty", SOURCE)

    def test_vinyl_endpoint_comes_from_status_with_client_json_fallback(self):
        self.assertIn('object["server"] as? String', SOURCE)
        self.assertIn("self.runningServer = endpoint()", SOURCE)
        self.assertIn('"Connected: \\(running)"', SOURCE)
        self.assertIn('"Restart the client to use \\(configured)"', SOURCE)
        self.assertIn('"kickstart", "-k", "gui/\\(getuid())/\\(clientLabel)"', SOURCE)

    def test_restart_never_kills_a_live_dictation(self):
        restart = SOURCE[SOURCE.index("@objc private func restartClient"):]
        self.assertIn("guard reachable, !listening, !busy", restart.split("cli.work")[0])

    def test_dropped_hotkey_press_gets_feedback(self):
        toggle = SOURCE[SOURCE.index("private func toggleDictation()"):]
        self.assertIn("pressFeedback(", toggle.split("private func pressFeedback")[0])
        self.assertIn("NSSound.beep()", SOURCE)

    def test_phonograph_health_probe(self):
        self.assertIn('"/healthz"', SOURCE)
        self.assertIn("https://phonograph.intellifornia.com", SOURCE)


class ReleaseContractTest(unittest.TestCase):
    def test_version_is_its_own_and_valid(self):
        version = (HERE / "VERSION").read_text().strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")

    def test_info_plist_identity(self):
        info = plistlib.loads((HERE / "Info.plist").read_bytes())
        self.assertEqual(info["CFBundleIdentifier"], "sh.delo.heyma")
        self.assertEqual(info["CFBundleExecutable"], "HeyMa")

    def test_signing_is_hardened_and_timestamped_with_developer_id(self):
        build = (HERE / "build-app.sh").read_text()
        self.assertIn("--options runtime --timestamp", build)
        self.assertIn("Developer ID Application: Jarad DeLorenzo (YJ7W7Z36FP)", build)

    def test_no_secret_values_in_scripts(self):
        for name in ("build-app.sh", "release.sh", "mac-sign-notarize.sh"):
            text = (HERE / name).read_text()
            self.assertNotRegex(text, r"BEGIN [A-Z ]*PRIVATE KEY-----\s*\n[A-Za-z0-9+/]{20}")
            self.assertNotRegex(text, r"-p ['\"][^$]")  # no literal keychain password
        release = (HERE / "release.sh").read_text()
        self.assertIn("op://DeLoSecrets/", release)

    def test_scripts_parse(self):
        for name in ("build-app.sh", "release.sh", "mac-sign-notarize.sh"):
            subprocess.run(["bash", "-n", str(HERE / name)], check=True)

    def test_unlock_and_codesign_share_one_process(self):
        notarize = (HERE / "mac-sign-notarize.sh").read_text()
        self.assertLess(notarize.index("security unlock-keychain"), notarize.index("build-app.sh"))


if __name__ == "__main__":
    unittest.main()
