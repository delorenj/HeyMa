"""Clipboard desk effects are bounded and success requires a real backend exit."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "config/passes.d/bin/clipboard-copy"
loader = importlib.machinery.SourceFileLoader("wax_clipboard_copy_test", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
clipboard = importlib.util.module_from_spec(spec)
loader.exec_module(clipboard)


class ClipboardCopyTest(unittest.TestCase):
    def run_fake_entrypoint(self, manager_output, *, manager_exit=0, inherited=None,
                            runtime_wayland=False):
        """Only private tools/runtime paths are reachable, never the real desktop."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "bin"
            tools.mkdir()
            runtime = root / "runtime"
            runtime.mkdir()
            if runtime_wayland:
                (runtime / "wayland-private").touch()
            note = root / "note.md"
            note.write_text("---\ntitle: Example\n---\nprivate speech\n")
            log = root / "calls.jsonl"
            fake = (
                "#!/usr/bin/python3\n"
                "import json, os, sys\nfrom pathlib import Path\n"
                "tool = Path(sys.argv[0]).name\n"
                "with open(os.environ['FAKE_LOG'], 'a') as f:\n"
                "    f.write(json.dumps({'tool': tool, 'display': os.getenv('DISPLAY'), "
                "'auth': os.getenv('XAUTHORITY'), 'wayland': os.getenv('WAYLAND_DISPLAY')}) + '\\n')\n"
                "if tool == 'systemctl':\n"
                "    print(os.environ['FAKE_MANAGER_OUTPUT'])\n"
                "    sys.exit(int(os.environ['FAKE_MANAGER_EXIT']))\n"
                "if tool == 'wl-copy': sys.exit(1)\n"
                "if tool == 'xclip': assert sys.stdin.read() == 'private speech'\n"
            )
            for tool in ("systemctl", "wl-copy", "xclip", "paplay"):
                path = tools / tool
                path.write_text(fake)
                path.chmod(0o755)
            env = {
                "PATH": str(tools), "HOME": str(root), "XDG_RUNTIME_DIR": str(runtime),
                "FAKE_LOG": str(log), "FAKE_MANAGER_OUTPUT": manager_output,
                "FAKE_MANAGER_EXIT": str(manager_exit),
                **(inherited or {}),
            }
            result = subprocess.run([str(SCRIPT), str(note)], env=env,
                                    capture_output=True, text=True, timeout=10)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            return result, calls

    def test_entrypoint_wayland_only_manager_drops_stale_x11(self):
        result, calls = self.run_fake_entrypoint(
            "WAYLAND_DISPLAY=wayland-current",
            inherited={"DISPLAY": ":stale", "XAUTHORITY": "/stale/auth"})
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr, calls))
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.startswith("reason_code=clipboard_unavailable\n"))
        self.assertEqual([call["tool"] for call in calls], ["systemctl", "wl-copy"])
        self.assertIsNone(calls[-1]["display"])
        self.assertIsNone(calls[-1]["auth"])
        self.assertEqual(calls[-1]["wayland"], "wayland-current")

    def test_entrypoint_current_x11_replaces_stale_session(self):
        result, calls = self.run_fake_entrypoint(
            "DISPLAY=:current\nXAUTHORITY=/current/auth",
            inherited={"DISPLAY": ":stale", "XAUTHORITY": "/stale/auth",
                       "WAYLAND_DISPLAY": "wayland-stale"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["evidence"]["clipboard_backend"], "xclip")
        self.assertEqual([call["tool"] for call in calls], ["systemctl", "xclip", "paplay"])
        self.assertEqual(calls[1]["display"], ":current")
        self.assertEqual(calls[1]["auth"], "/current/auth")
        self.assertIsNone(calls[1]["wayland"])

    def test_entrypoint_current_x11_does_not_inherit_old_auth(self):
        result, calls = self.run_fake_entrypoint(
            "DISPLAY=:current", inherited={"XAUTHORITY": "/stale/auth"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[1]["display"], ":current")
        self.assertIsNone(calls[1]["auth"])

    def test_entrypoint_authoritative_empty_session_fails_closed(self):
        for output in ("", "DISPLAY=\nWAYLAND_DISPLAY=\nXAUTHORITY="):
            with self.subTest(output=output):
                result, calls = self.run_fake_entrypoint(
                    output, inherited={"DISPLAY": ":stale", "XAUTHORITY": "/stale/auth",
                                       "WAYLAND_DISPLAY": "wayland-stale"},
                    runtime_wayland=True)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.startswith("reason_code=clipboard_unavailable\n"))
                self.assertEqual([call["tool"] for call in calls], ["systemctl"])

    def test_entrypoint_failed_manager_preserves_explicit_x11(self):
        result, calls = self.run_fake_entrypoint(
            "DISPLAY=:ignore-partial-output", manager_exit=1,
            inherited={"DISPLAY": ":explicit", "XAUTHORITY": "/explicit/auth"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[1]["display"], ":explicit")
        self.assertEqual(calls[1]["auth"], "/explicit/auth")

    def test_entrypoint_failed_manager_preserves_explicit_wayland(self):
        result, calls = self.run_fake_entrypoint(
            "", manager_exit=1, runtime_wayland=True,
            inherited={"WAYLAND_DISPLAY": "wayland-explicit"})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual([call["tool"] for call in calls], ["systemctl", "wl-copy"])
        self.assertEqual(calls[1]["wayland"], "wayland-explicit")

    def test_entrypoint_failed_manager_discovers_only_private_wayland(self):
        result, calls = self.run_fake_entrypoint(
            "", manager_exit=1, runtime_wayland=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual([call["tool"] for call in calls], ["systemctl", "wl-copy"])
        self.assertEqual(calls[1]["wayland"], "wayland-private")
        self.assertIsNone(calls[1]["display"])

    def test_entrypoint_failed_manager_without_context_fails_closed(self):
        result, calls = self.run_fake_entrypoint("", manager_exit=1)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual([call["tool"] for call in calls], ["systemctl"])

    def test_wayland_timeout_uses_authenticated_x11_and_reports_fallback(self):
        env = {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":2", "XAUTHORITY": "/current/auth"}
        with patch.object(clipboard.subprocess, "run", side_effect=[
            subprocess.TimeoutExpired("wl-copy", 5), subprocess.CompletedProcess("xclip", 0)
        ]) as run:
            result = clipboard.copy_body("speech", env)
        self.assertEqual(result, {"clipboard_backend": "xclip", "clipboard_fallbacks": [
            {"backend": "wl-copy", "reason": "timeout"}]})
        self.assertEqual(run.call_args.kwargs["env"], env)
        self.assertEqual(run.call_args.kwargs["input"], b"speech")
        self.assertEqual(run.call_args.args[0],
                         ["xclip", "-selection", "clipboard", "-in", "-target", "UTF8_STRING"])
        self.assertTrue(all(c.kwargs["timeout"] == 5 for c in run.call_args_list))

    def test_successful_wayland_does_not_write_a_second_time(self):
        with patch.object(clipboard.subprocess, "run") as run:
            result = clipboard.copy_body("speech", {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":2"})
        self.assertEqual(result["clipboard_backend"], "wl-copy")
        run.assert_called_once()
        self.assertIn("text/plain;charset=utf-8", run.call_args.args[0])

    def test_no_display_never_guesses_another_session(self):
        with patch.object(clipboard.subprocess, "run") as run:
            with self.assertRaisesRegex(clipboard.ClipboardUnavailable, "no current graphical display"):
                clipboard.copy_body("private speech", {})
        run.assert_not_called()

    def test_exhausted_backends_fail_with_reason_and_no_success_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            note = Path(directory) / "note.md"
            note.write_text("---\ntitle: Example\n---\nprivate speech\n")
            out, err = io.StringIO(), io.StringIO()
            with patch.object(clipboard.sys, "argv", [str(SCRIPT), str(note)]), \
                    patch.object(clipboard, "desktop_env", return_value={"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":2"}), \
                    patch.object(clipboard.subprocess, "run", side_effect=[
                        subprocess.TimeoutExpired("wl-copy", 5), FileNotFoundError("xclip")]), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(clipboard.main(), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(err.getvalue().startswith("reason_code=clipboard_unavailable\n"))
        self.assertIn("wl-copy=timeout; xclip=unavailable", err.getvalue())
        self.assertNotIn("private speech", err.getvalue())

    def test_current_session_overrides_stale_inherited_display(self):
        with patch.dict(os.environ, {"DISPLAY": ":old", "XAUTHORITY": "/old"}, clear=True), \
                patch.object(clipboard.os, "getuid", return_value=1234), \
                patch.object(clipboard.subprocess, "run", return_value=subprocess.CompletedProcess(
                    "systemctl", 0, stdout="DISPLAY=:new\nXAUTHORITY=/new")):
            env = clipboard.desktop_env()
        self.assertEqual(env["DISPLAY"], ":new")
        self.assertEqual(env["XAUTHORITY"], "/new")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1234")

    def test_manager_timeout_or_missing_tool_preserves_explicit_context(self):
        with tempfile.TemporaryDirectory() as runtime:
            inherited = {"DISPLAY": ":explicit", "XAUTHORITY": "/explicit/auth",
                         "XDG_RUNTIME_DIR": runtime}
            for error in (subprocess.TimeoutExpired("systemctl", 2), FileNotFoundError()):
                with self.subTest(error=type(error).__name__), \
                        patch.dict(os.environ, inherited, clear=True), \
                        patch.object(clipboard.subprocess, "run", side_effect=error):
                    self.assertEqual(clipboard.desktop_env(), inherited)

    def test_chime_timeout_does_not_fail_a_successful_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            note = Path(directory) / "note.md"
            note.write_text("---\ntitle: Example\n---\nprivate speech\n")
            out = io.StringIO()
            with patch.object(clipboard.sys, "argv", [str(SCRIPT), str(note)]), \
                    patch.object(clipboard, "desktop_env", return_value={"WAYLAND_DISPLAY": "wayland-1"}), \
                    patch.object(clipboard.subprocess, "run", side_effect=[
                        subprocess.CompletedProcess("wl-copy", 0), subprocess.TimeoutExpired("paplay", 3)]), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(clipboard.main(), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["evidence"]["clipboard_backend"], "wl-copy")
        self.assertEqual(result["evidence"]["sound"], "unavailable")
        self.assertNotIn("private speech", out.getvalue())

    def test_real_stalled_child_is_killed_before_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory)
            stalled = tools / "wl-copy"
            stalled.write_text("#!/usr/bin/python3\nimport os,time\nfrom pathlib import Path\n"
                               "Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))\ntime.sleep(60)\n")
            stalled.chmod(0o755)
            fallback = tools / "xclip"
            fallback.write_text("#!/usr/bin/python3\nimport sys\nassert sys.stdin.buffer.read() == b'speech'\n")
            fallback.chmod(0o755)
            with patch.object(clipboard, "COPY_TIMEOUT_S", 0.2):
                result = clipboard.copy_body("speech", {"PATH": directory, "WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":2"})
            self.assertEqual(result["clipboard_backend"], "xclip")
            pid = int(stalled.with_suffix('.pid').read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
