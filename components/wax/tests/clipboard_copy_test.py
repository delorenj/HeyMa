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
                patch.object(clipboard, "session_env", return_value={"DISPLAY": ":new", "XAUTHORITY": "/new"}):
            env = clipboard.desktop_env()
        self.assertEqual(env["DISPLAY"], ":new")
        self.assertEqual(env["XAUTHORITY"], "/new")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1234")

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
