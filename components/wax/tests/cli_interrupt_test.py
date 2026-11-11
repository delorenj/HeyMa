"""Ctrl-C while `wax` is still starting must exit 130 quietly, never with a traceback.

`wax` spends ~0.1 s starting before main() runs: Python's own start-up (site),
then wax's imports, then its ~3000-line module body. A Ctrl-C in any of them used
to print a KeyboardInterrupt traceback. Stand-ins on PYTHONPATH hold each phase
open so the signal lands there every time, not by luck of timing: `sitecustomize`
(run by site, before the script's first line), `argparse` (the first import after
the handler goes in) and `yaml` (imported by every wax command, through wax's own
modules). The module body is hit by a tracer that sends the signal at a chosen line.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

COMPONENT_ROOT = next(parent for parent in Path(__file__).resolve().parents
                      if (parent / "pyproject.toml").is_file())
WAX = COMPONENT_ROOT / "bin" / "wax"
SHIM = COMPONENT_ROOT.parent.parent / "bin" / "wax"  # what `wax` on PATH runs
HAVE_PERL = shutil.which("perl") is not None

# Announces itself, waits for the test's go, then hands over to the real module.
SLOW_MODULE = textwrap.dedent("""\
    import pathlib, sys, time
    here = pathlib.Path(__file__).parent
    (here / "importing").touch()
    deadline = time.monotonic() + 30
    while not (here / "go").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    sys.path[:] = [p for p in sys.path if p != str(here)]
    del sys.modules[__name__]
    _real = __import__(__name__)
    sys.modules[__name__] = _real
""")

# The same, during site's start-up: before the script's first line runs.
SLOW_SITE = textwrap.dedent("""\
    import pathlib, time
    here = pathlib.Path(__file__).parent
    (here / "importing").touch()
    deadline = time.monotonic() + 30
    while not (here / "go").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
""")

# Sends the process a Ctrl-C when the wax script's module body reaches line TARGET.
CTRL_C_AT_LINE = textwrap.dedent("""\
    import os, signal, sys
    SCRIPT, TARGET = os.environ["CTRL_C_SCRIPT"], int(os.environ["CTRL_C_LINE"])
    def _line(frame, event, _arg):
        if event == "line" and frame.f_lineno >= TARGET:
            sys.settrace(None)
            frame.f_trace = None
            os.kill(os.getpid(), signal.SIGINT)
            return None
        return _line
    def _call(frame, event, _arg):
        if frame.f_code.co_filename == SCRIPT and frame.f_code.co_name == "<module>":
            return _line
        return None
    sys.settrace(_call)
""")


def _line_of(text: str) -> int:
    """The last line of the script that reads `text` (the `__main__` guard appears twice)."""
    found = [number for number, line in enumerate(WAX.read_text().splitlines(), 1) if line.strip() == text]
    if not found:
        raise AssertionError(f"{text!r} is not in {WAX}")
    return found[-1]


def _held(sigmask):
    def preexec():
        signal.signal(signal.SIGINT, sigmask[0])
        if sigmask[1]:
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    return preexec


class StartupInterruptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def hold(self, module: str):
        for stale in ("importing", "go", "yaml.py", "argparse.py", "sitecustomize.py"):
            (self.dir / stale).unlink(missing_ok=True)
        (self.dir / f"{module}.py").write_text(SLOW_SITE if module == "sitecustomize" else SLOW_MODULE)

    def start(self, launcher: Path, *argv: str, sigint=signal.SIG_DFL, blocked=False) -> subprocess.Popen:
        env = dict(os.environ, PYTHONPATH=str(self.dir), PYTHONDONTWRITEBYTECODE="1")
        env.pop("WAX_SIGINT_HELD", None)
        proc = subprocess.Popen([str(launcher), *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, preexec_fn=_held((sigint, blocked)))
        deadline = time.monotonic() + 30
        while not (self.dir / "importing").exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                proc.kill()
                self.fail(f"wax never reached the held phase: {proc.communicate()}")
            time.sleep(0.01)
        return proc

    def assert_interrupted(self, proc: subprocess.Popen):
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 130, err)
        self.assertEqual(err, "interrupted\n")
        self.assertEqual(out, "")

    def assert_ran(self, proc: subprocess.Popen):
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(err, "")
        self.assertIn("usage:", out)

    def test_ctrl_c_during_the_imports_exits_130_without_a_traceback(self):
        # argparse pins the handler above the stdlib imports; yaml comes in through wax's own.
        for module in ("argparse", "yaml"):
            for launcher in (WAX, SHIM):
                with self.subTest(module=module, launcher=str(launcher)):
                    self.hold(module)
                    proc = self.start(launcher, "--help")
                    proc.send_signal(signal.SIGINT)
                    self.assert_interrupted(proc)

    def test_ctrl_c_in_the_module_body_exits_130_without_a_traceback(self):
        """After the imports, ~3000 lines of definitions still run before main() is inside its try."""
        (self.dir / "sitecustomize.py").write_text(CTRL_C_AT_LINE)
        targets = {"just after the imports": _line_of("def _emit(obj, as_json: bool) -> None:"),
                   "at the __main__ guard": _line_of('if __name__ == "__main__":'),
                   "as main() is called": _line_of("sys.exit(main())")}
        for where, line in targets.items():
            for launcher in (WAX, SHIM):
                with self.subTest(where=where, launcher=str(launcher)):
                    env = dict(os.environ, PYTHONPATH=str(self.dir), PYTHONDONTWRITEBYTECODE="1",
                               CTRL_C_SCRIPT=str(WAX), CTRL_C_LINE=str(line))
                    proc = subprocess.run([str(launcher), "--help"], capture_output=True, text=True,
                                          env=env, timeout=60, preexec_fn=_held((signal.SIG_DFL, False)))
                    self.assertEqual(proc.returncode, 130, proc.stderr)
                    self.assertEqual(proc.stderr, "interrupted\n")
                    self.assertEqual(proc.stdout, "")

    @unittest.skipUnless(HAVE_PERL, "the launcher holds Ctrl-C with perl")
    def test_ctrl_c_during_python_start_up_waits_for_wax_then_exits_130(self):
        """Before the script runs nothing of wax's can catch it; the launcher holds it until wax can."""
        self.hold("sitecustomize")
        proc = self.start(SHIM, "--help")
        proc.send_signal(signal.SIGINT)
        time.sleep(0.2)
        self.assertIsNone(proc.poll(), "the held Ctrl-C ended Python's start-up")
        (self.dir / "go").touch()
        self.assert_interrupted(proc)

    def test_without_a_ctrl_c_the_launcher_changes_nothing(self):
        self.hold("sitecustomize")
        (self.dir / "go").touch()
        self.assert_ran(self.start(SHIM, "--help"))

    def test_a_launcher_that_ignores_ctrl_c_is_obeyed(self):
        for launcher in (WAX, SHIM):
            with self.subTest(launcher=str(launcher)):
                self.hold("yaml")
                proc = self.start(launcher, "--help", sigint=signal.SIG_IGN)
                proc.send_signal(signal.SIGINT)
                time.sleep(0.2)
                (self.dir / "go").touch()
                self.assert_ran(proc)

    def test_a_launcher_that_blocks_ctrl_c_is_obeyed(self):
        """A caller that masks SIGINT (a supervisor shielding children) keeps it masked."""
        for launcher in (WAX, SHIM):
            with self.subTest(launcher=str(launcher)):
                self.hold("yaml")
                proc = self.start(launcher, "--help", blocked=True)
                proc.send_signal(signal.SIGINT)
                time.sleep(0.2)
                (self.dir / "go").touch()
                self.assert_ran(proc)


@unittest.skipUnless(HAVE_PERL, "the launcher is perl")
class LauncherTest(unittest.TestCase):
    """The launcher must not change anything a caller can see, other than the Ctrl-C hold."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "bin").mkdir()
        (root / "components" / "wax" / "bin").mkdir(parents=True)
        shutil.copy2(SHIM, root / "bin" / "wax")
        self.shim = root / "bin" / "wax"
        self.target = root / "components" / "wax" / "bin" / "wax"

    def tearDown(self):
        self.tmp.cleanup()

    def run_shim(self, **env):
        return subprocess.run([str(self.shim), "a b", "c"], capture_output=True, text=True, timeout=30,
                              env=dict(os.environ, **env))

    def test_a_missing_or_unrunnable_wax_exits_127_or_126_like_the_shell(self):
        self.assertEqual(self.run_shim().returncode, 127)
        self.target.write_text("#!/bin/sh\n")
        self.assertEqual(self.run_shim().returncode, 126)

    def test_arguments_and_environment_pass_through_and_perl_stays_quiet(self):
        self.target.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, os, sys
            print(json.dumps([sys.argv[1:], os.environ.get("WAX_SIGINT_HELD"), "PERL_BADLANG" in os.environ]))
        """))
        self.target.chmod(0o755)
        proc = self.run_shim(LANG="xx_YY.UTF-8", LC_ALL="xx_YY.UTF-8")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(proc.stdout.strip(), '[["a b", "c"], "1", false]')


class HandlerTest(unittest.TestCase):
    """Run as the command, wax releases the launcher's hold and gives Ctrl-C back to Python;
    imported (tests, SourceFileLoader), it touches nothing."""

    AS_COMMAND = textwrap.dedent("""\
        import runpy, signal, sys
        script = sys.argv[1]
        sys.argv = [script, "--help"]
        try:
            runpy.run_path(script, run_name="__main__")
        except SystemExit:
            pass
        handler = signal.getsignal(signal.SIGINT)
        blocked = signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, [])
        print(("blocked " if blocked else "") + ("default" if handler is signal.default_int_handler else
              "ignored" if handler is signal.SIG_IGN else repr(handler)))
    """)

    AS_MODULE = textwrap.dedent("""\
        import importlib.util, signal, sys, threading
        from importlib.machinery import SourceFileLoader
        def load():
            loader = SourceFileLoader("wax_cli", sys.argv[1])
            loader.exec_module(importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader)))
        if sys.argv[2] == "thread":
            errors = []
            def guarded():
                try:
                    load()
                except BaseException as error:
                    errors.append(error)
            worker = threading.Thread(target=guarded)
            worker.start()
            worker.join()
            if errors:
                raise errors[0]
        else:
            load()
        handler = signal.getsignal(signal.SIGINT)
        blocked = signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, [])
        print(("blocked " if blocked else "") + ("default" if handler is signal.default_int_handler else
              "ignored" if handler is signal.SIG_IGN else repr(handler)))
    """)

    def probe(self, code, sigint, *, held=False, told=False, how="main") -> str:
        env = dict(os.environ)
        env.pop("WAX_SIGINT_HELD", None)
        if told:  # as the repo-root launcher starts it
            env["WAX_SIGINT_HELD"] = "1"
        out = subprocess.run([sys.executable, "-c", code, str(WAX), how], capture_output=True, text=True,
                             timeout=60, env=env, preexec_fn=_held((sigint, held)))
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip().splitlines()[-1]

    def test_as_the_command_the_startup_handler_is_gone_once_main_runs(self):
        self.assertEqual(self.probe(self.AS_COMMAND, signal.SIG_DFL), "default")

    def test_as_the_command_an_ignored_ctrl_c_stays_ignored(self):
        self.assertEqual(self.probe(self.AS_COMMAND, signal.SIG_IGN), "ignored")

    def test_the_launchers_hold_is_released_so_commands_and_their_children_can_be_stopped(self):
        self.assertEqual(self.probe(self.AS_COMMAND, signal.SIG_DFL, held=True, told=True), "default")
        self.assertEqual(self.probe(self.AS_COMMAND, signal.SIG_IGN, held=True, told=True), "ignored")

    def test_a_callers_own_mask_is_kept(self):
        self.assertEqual(self.probe(self.AS_COMMAND, signal.SIG_DFL, held=True), "blocked default")

    def test_importing_wax_changes_no_signal_state(self):
        for how in ("main", "thread"):
            with self.subTest(how=how):
                self.assertEqual(self.probe(self.AS_MODULE, signal.SIG_DFL, held=True, told=True, how=how),
                                 "blocked default")
                self.assertEqual(self.probe(self.AS_MODULE, signal.SIG_DFL, how=how), "default")


if __name__ == "__main__":
    unittest.main()
