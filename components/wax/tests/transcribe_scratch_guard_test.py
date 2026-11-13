"""waxd's run of bin/transcribe is untouched by the wrapper's scratch-source guard.

bin/transcribe treats audio under /tmp, $TMPDIR, ~/.claude/tmp, ~/.cache and ~/.hermes/**/cache as a throwaway and
writes its transcript outside the vault (tests/test_transcribe_scratch_output.py, repo root). waxd must never be
caught by that: it transcribes files under WAX_ROOT (inbox/, stream/) and reads the transcript path the wrapper
prints. This builds the command and environment with the REAL `transcribe_adapter.transcribe` (the subprocess is
captured, not run), checks that the command carries nothing that would opt into the guard, then executes exactly
that command through the real wrapper, with a stand-in interpreter, to show where the transcript goes.
"""

import os
import socket
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from wax import paths, transcribe_adapter

REPO = Path(__file__).resolve().parents[3]
WRAPPER = REPO / "bin" / "transcribe"
ITEM = "0123456789abcdef"


class WaxdInvocationTest(unittest.TestCase):
    def setUp(self):
        if not WRAPPER.is_file():
            self.skipTest(f"{WRAPPER} not found: not running inside the HeyMa checkout")
        # /tmp on purpose: the wrapper calls it scratch, so only the WAX_ROOT exemption can protect waxd here.
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="waxd-scratch-guard-")
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(os.path.realpath(self.tmp.name))
        self.wax_root = self.d / "waxroot"
        self.home = self.d / "home"
        self.vault = self.home / "d" / "Transcripts"
        self.eph = self.d / "ephemeral"
        self.args_file = self.d / "args"
        (self.d / "repo" / "scripts").mkdir(parents=True)
        (self.d / "repo" / "scripts" / "transcribe.py").write_text("")
        fake = self.d / "python"
        fake.write_text(f"""#!/bin/sh
if [ "$1" = "-" ]; then echo /nonexistent-cudnn-lib; exit 0; fi
shift
printf '%s\\n' "$@" > {self.args_file}
audio="$1"; shift
out=""
while [ $# -gt 0 ]; do
  case "$1" in -o|--output) out="$2"; shift ;; --output=*) out="${{1#--output=}}" ;; esac
  shift
done
if [ -z "$out" ]; then
  stem=$(basename "$audio"); stem="${{stem%.*}}"
  mkdir -p "$HOME/d/Transcripts"
  out="$HOME/d/Transcripts/$stem.md"
fi
echo hi > "$out"
echo "Written: $out" >&2
""")
        fake.chmod(0o755)
        self.fake = fake

    def waxd_command(self, audio: Path):
        """The (cmd, env) `transcribe_adapter.transcribe` hands to fence.run_tracked (the tracked subprocess.run) for `audio`."""
        calls = []

        def capture(item_id, cmd, **kwargs):          # fence.run_tracked(item_id, argv, env=..., timeout=...)
            calls.append((list(cmd), dict(kwargs["env"])))
            return subprocess.CompletedProcess(cmd, 1, "", "stopped after capture")

        with ExitStack() as stack:
            for name, value in {"VAR": self.wax_root / "var", "LOGS": self.wax_root / "var" / "logs",
                                "DB": self.wax_root / "var" / "wax.db"}.items():
                stack.enter_context(patch.object(paths, name, value))
            stack.enter_context(patch.dict(os.environ, {"WAX_TRANSCRIBE": str(WRAPPER), "WAX_ROOT": str(self.wax_root)}))
            stack.enter_context(patch.object(transcribe_adapter.desktop, "ding"))
            stack.enter_context(patch.object(transcribe_adapter.sanity, "probe_duration", return_value=None))
            stack.enter_context(patch.object(transcribe_adapter.fence, "run_tracked", side_effect=capture))
            with self.assertRaises(transcribe_adapter.TranscribeError):
                transcribe_adapter.transcribe(audio, item_id=ITEM)
        self.assertEqual(len(calls), 1)
        return calls[0]

    def run_it(self, cmd, env):
        env = dict(env, HOME=str(self.home), TRANSCRIBE_HOSTNAME=socket.gethostname(),
                   TRANSCRIBE_DIR=str(self.d / "repo"), DIARIZATION_PYTHON=str(self.fake),
                   TRANSCRIBE_LOCK_FILE=str(self.d / "heavy.lock"), TRANSCRIBE_FAST_LOCK_FILE=str(self.d / "fast.lock"),
                   TRANSCRIBE_LOG_FILE=str(self.d / "run.log"), TRANSCRIBE_STASH_DIR=str(self.d / "stash"),
                   TRANSCRIBE_EPHEMERAL_DIR=str(self.eph), TRANSCRIPTS=str(self.vault))
        env.pop("TMPDIR", None)
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)

    def test_waxd_asks_for_nothing_the_guard_keys_on(self):
        audio = self.wax_root / "inbox" / "20261110-120000-clip.ogg"
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"x")
        cmd, _ = self.waxd_command(audio)
        self.assertEqual(cmd[:3], ["nice", "-n", "15"])
        self.assertEqual(Path(cmd[3]), WRAPPER.resolve())
        self.assertEqual(cmd[4], str(audio))
        flags = cmd[5:]
        for opted in ("--ephemeral", "--archive-s3", "--output", "-o"):
            self.assertNotIn(opted, flags)
        self.assertFalse([f for f in flags if f.startswith("--output=")])
        self.assertEqual(flags[:1], ["--no-diarization"])
        self.assertEqual(flags[flags.index("--asr-output") + 1], str(self.wax_root / "var" / "logs" / ITEM / "asr.json"))

    def test_the_wrapper_leaves_waxds_transcript_where_transcribe_py_puts_it(self):
        for sub in ("inbox", "stream"):
            with self.subTest(sub):
                audio = self.wax_root / sub / f"clip-{sub}.ogg"
                audio.parent.mkdir(parents=True, exist_ok=True)
                audio.write_bytes(b"x")
                cmd, env = self.waxd_command(audio)
                r = self.run_it(cmd, env)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.splitlines(), [str(self.vault / f"clip-{sub}.md")])
                self.assertNotIn("[scratch]", r.stderr)
                self.assertNotIn("--output", self.args_file.read_text().split("\n"))
                self.assertFalse(self.eph.exists())

    def test_the_same_run_outside_wax_root_would_be_rerouted(self):
        """The control: it is the WAX_ROOT exemption, nothing else, that keeps waxd's /tmp-rooted runs unguarded."""
        audio = self.wax_root / "inbox" / "clip.ogg"
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"x")
        cmd, env = self.waxd_command(audio)
        elsewhere = self.d / "elsewhere" / "clip.ogg"
        elsewhere.parent.mkdir()
        elsewhere.write_bytes(b"x")
        r = self.run_it(cmd[:4] + [str(elsewhere)] + cmd[5:], env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[scratch]", r.stderr)
        self.assertEqual(Path(r.stdout.strip()).parent, self.eph)
        self.assertFalse(self.vault.exists())


if __name__ == "__main__":
    unittest.main()
