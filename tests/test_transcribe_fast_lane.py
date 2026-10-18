"""bin/transcribe's fast lane: a short clip never queues behind a long job holding the heavy slot."""
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "bin" / "transcribe"


class FastLaneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = Path(self.tmp.name)
        (d / "scripts").mkdir()
        (d / "scripts" / "transcribe.py").write_text("")
        self.args_file = d / "args"
        # Stands in for the venv python: records its argv and env, writes a transcript, reports it like the real one.
        fake = d / "python"
        fake.write_text(f"""#!/bin/sh
shift
printf '%s\\n' "$@" > {self.args_file}
env > {self.args_file}.env
echo "Written: {d}/out.md" >&2
echo hi > {d}/out.md
""")
        fake.chmod(0o755)
        self.env = dict(os.environ, TRANSCRIBE_HOSTNAME=socket.gethostname(), TRANSCRIBE_DIR=str(d), DIARIZATION_PYTHON=str(fake),
                        TRANSCRIBE_LOCK_FILE=str(d / "heavy.lock"), TRANSCRIBE_FAST_LOCK_FILE=str(d / "fast.lock"),
                        TRANSCRIBE_LOG_FILE=str(d / "run.log"), PYTHONPATH="/leaked/site-packages")

    def tearDown(self):
        self.tmp.cleanup()

    def clip(self, seconds):
        p = self.d / f"clip{seconds}.ogg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono", "-t", str(seconds), "-c:a", "libopus", str(p)], check=True)
        return p

    def run_wrapper(self, audio, *extra, timeout=20):
        return subprocess.run([str(WRAPPER), str(audio), *extra], env=self.env, capture_output=True, text=True, timeout=timeout)

    def assert_waits(self, audio, *extra):
        """The run queues on the heavy slot: it says so, and is still waiting when the test gives up."""
        p = subprocess.Popen([str(WRAPPER), str(audio), *extra], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                p.wait(timeout=4)
        finally:
            p.kill()
            _, err = p.communicate()
        self.assertIn("Another transcription holds the slot", err)
        self.assertNotIn("fast lane", err)

    def hold_heavy(self):
        holder = subprocess.Popen(["flock", str(self.d / "heavy.lock"), "sleep", "60"])
        self.addCleanup(holder.kill)
        time.sleep(0.5)

    def args(self):
        return self.args_file.read_text().split()

    def test_short_clip_takes_the_fast_lane_when_the_heavy_slot_is_busy(self):
        self.hold_heavy()
        r = self.run_wrapper(self.clip(5), "--ephemeral", "--model", "large-v3", "--diarization")
        self.assertEqual(r.returncode, 0, r.stderr)
        a = self.args()
        self.assertEqual(a[a.index("--model") + 1], "small")
        self.assertIn("--no-diarization", a)
        self.assertNotIn("--diarization", a)
        self.assertEqual(a[a.index("--device") + 1], "cpu")
        self.assertIn("fast lane", r.stderr)

    def test_a_caller_small_model_is_kept(self):
        self.hold_heavy()
        r = self.run_wrapper(self.clip(5), "--ephemeral", "--model", "base")
        self.assertEqual(r.returncode, 0, r.stderr)
        a = self.args()
        self.assertEqual(a.count("--model"), 1)
        self.assertEqual(a[a.index("--model") + 1], "base")

    def test_long_clip_waits_its_turn(self):
        self.hold_heavy()
        self.assert_waits(self.clip(130), "--ephemeral")

    def test_pinned_device_waits_its_turn(self):
        self.hold_heavy()
        self.assert_waits(self.clip(5), "--ephemeral", "--device", "cuda")

    def test_wax_style_short_clip_is_never_downgraded(self):
        """Wax's 60 s segments are not --ephemeral: they wait for the heavy slot and keep their model."""
        self.hold_heavy()
        self.assert_waits(self.clip(60), "--no-diarization")

    def test_container_without_a_duration_does_not_crash(self):
        bogus = self.d / "nodur.ogg"
        bogus.write_bytes(b"not really audio")
        r = self.run_wrapper(bogus, "--ephemeral")
        self.assertNotIn("unbound variable", r.stderr)

    def test_free_slot_runs_normally_and_keeps_caller_args(self):
        r = self.run_wrapper(self.clip(5), "--model", "large-v3")
        self.assertEqual(r.returncode, 0, r.stderr)
        a = self.args()
        self.assertEqual(a[a.index("--model") + 1], "large-v3")
        self.assertNotIn("fast lane", r.stderr)

    def test_callers_python_environment_never_reaches_the_runner(self):
        r = self.run_wrapper(self.clip(2))
        self.assertEqual(r.returncode, 0, r.stderr)
        env = Path(f"{self.args_file}.env").read_text()
        self.assertNotIn("PYTHONPATH=", env)
        self.assertIn("PYTHONNOUSERSITE=1", env)


if __name__ == "__main__":
    unittest.main()
