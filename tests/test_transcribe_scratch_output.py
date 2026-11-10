"""bin/transcribe: an ad-hoc run on scratch audio never lands in the vault.

Incident 2026-11: junk notes (`tmp*.md`, `proof.md`) reached ~/d/Transcripts from ad-hoc runs on audio in
~/.claude/tmp, ~/.hermes/**/cache and /tmp that did not pass --ephemeral. scripts/transcribe.py writes
~/d/Transcripts/<stem>.md unless handed `--output` (it never reads $TRANSCRIPTS), and the remote branch of the
wrapper writes ${TRANSCRIPTS}/<name>.md, so a source under a temp/scratch location must be treated as --ephemeral
for output placement: the transcript goes to the ephemeral dir and the wrapper says so on stderr.

The wrapper is driven with a stand-in for the venv python that behaves like scripts/transcribe.py: it writes to
`--output` when given, else to $HOME/d/Transcripts/<stem>.md. Every path lives in a temp tree this test owns
(HOME, TRANSCRIPTS, the ephemeral dir, the locks, the stash, MC_BIN), so nothing here can reach the real vault.
"""
import os
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "transcribe"


def _is_scratch(path: Path) -> bool:
    """Under a scratch root the wrapper always knows (the tests hand it neither $TMPDIR nor the real $HOME)."""
    real = os.path.realpath(path)
    return any(real.startswith(os.path.realpath(r) + os.sep) for r in ("/tmp", "/var/tmp"))


def _under(path, parent) -> bool:
    return os.path.realpath(path).startswith(os.path.realpath(parent) + os.sep)


class _WrapperCase(unittest.TestCase):
    """A tmp tree for the runner stub and outputs (`self.d`), and a NON-scratch tree for sources (`self.src`)."""

    def setUp(self):
        # Sources that must read as ordinary files live inside the checkout, not under /tmp or $TMPDIR.
        if _is_scratch(ROOT / "tests"):
            self.skipTest("the checkout itself sits under a scratch location")
        # /tmp, not $TMPDIR: the wrapper is run without a $TMPDIR, and /tmp is scratch to it unconditionally.
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="scratch-guard-")
        self.d = Path(os.path.realpath(self.tmp.name))
        self.src = Path(os.path.realpath(tempfile.mkdtemp(dir=ROOT / "tests", prefix=".scratch-guard-")))
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.home = self.src / "home"
        self.vault = self.home / "d" / "Transcripts"          # where the real transcribe.py defaults to
        self.eph = self.d / "ephemeral"
        (self.d / "scripts").mkdir()
        (self.d / "scripts" / "transcribe.py").write_text("")
        self.args_file = self.d / "args"
        fake = self.d / "python"
        fake.write_text(f"""#!/bin/sh
# bin/transcribe probes the interpreter for cuDNN with `python -`; answer it so it never falls back to `uv run`.
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
        self.mc_log = self.d / "mc.log"
        mc = self.d / "mc"
        mc.write_text(f"""#!/bin/sh
printf '%s\\n' "$*" >> {self.mc_log}
case "$1" in stat) echo '{{"size": 0}}' ;; esac
exit 0
""")
        mc.chmod(0o755)
        self.fakebin = self.d / "fakebin"
        self.fakebin.mkdir()
        self.ssh_log = self.d / "ssh.log"
        (self.fakebin / "ssh").write_text(f"""#!/bin/sh
for last; do :; done
printf '%s\\n' "$last" >> {self.ssh_log}
case "$last" in *"grep '^Written:'"*) echo /remote/staging/out.md ;; esac
exit 0
""")
        (self.fakebin / "scp").write_text("""#!/bin/sh
for last; do :; done
case "$2" in *:*) printf 'hi\\n' > "$last" ;; esac
exit 0
""")
        for name in ("ssh", "scp"):
            (self.fakebin / name).chmod(0o755)

    def env(self, **extra):
        env = dict(os.environ, HOME=str(self.home), TRANSCRIBE_HOSTNAME=socket.gethostname(),
                   TRANSCRIBE_DIR=str(self.d), DIARIZATION_PYTHON=str(self.d / "python"),
                   TRANSCRIBE_LOCK_FILE=str(self.d / "heavy.lock"), TRANSCRIBE_FAST_LOCK_FILE=str(self.d / "fast.lock"),
                   TRANSCRIBE_LOG_FILE=str(self.d / "run.log"), TRANSCRIBE_STASH_DIR=str(self.d / "stash"),
                   TRANSCRIBE_EPHEMERAL_DIR=str(self.eph), TRANSCRIPTS=str(self.vault),
                   WAX_ROOT=str(self.d / "waxroot"), MC_BIN=str(self.d / "mc"))
        env.pop("TMPDIR", None)                 # scratch via $TMPDIR only when a test says so
        env.update(extra)
        return env

    def audio(self, *parts, name="clip.ogg") -> Path:
        path = Path(*parts) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")                  # not decodable: the wrapper treats "no duration" as fine
        return path

    def run_wrapper(self, audio, *extra, **env):
        return subprocess.run([str(WRAPPER), str(audio), *extra], env=self.env(**env), capture_output=True,
                              text=True, timeout=60)

    def args(self):
        return self.args_file.read_text().split("\n")[:-1]

    def written(self, r) -> Path:
        """The transcript path, after checking the stdout contract: exactly one line, an existing file."""
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 1, f"stdout must be exactly the transcript path, got {r.stdout!r}")
        path = Path(lines[0])
        self.assertTrue(path.is_file(), r.stdout)
        return path

    def assert_ephemeral(self, r, audio):
        path = self.written(r)
        self.assertTrue(_under(path, self.eph), f"{path} is not under {self.eph}")
        self.assertFalse(_under(path, self.vault), f"{path} reached the vault")
        self.assertFalse(self.vault.exists() and list(self.vault.glob("*.md")), "something was written to the vault")
        self.assertIn("[scratch]" if "--ephemeral" not in r.args else "", r.stderr)
        return path

    def assert_vault(self, r):
        path = self.written(r)
        self.assertTrue(_under(path, self.vault), f"{path} is not under {self.vault}")
        self.assertNotIn("[scratch]", r.stderr)
        return path


class ScratchSourceLocalTest(_WrapperCase):
    """The local path (the one big-chungus takes): scratch audio is told where to write."""

    def test_a_source_under_a_scratch_location_stays_out_of_the_vault(self):
        cases = {
            "/tmp": lambda: self.audio(tempfile.mkdtemp(dir="/tmp", prefix="scratch-guard-")),
            "~/.claude/tmp": lambda: self.audio(self.home, ".claude", "tmp", "tmpab12"),
            "~/.cache": lambda: self.audio(self.home, ".cache", "voice"),
            "~/.hermes/profiles/*/cache/scratch": lambda: self.audio(
                self.home, ".hermes", "profiles", "ghoul", "cache", "scratch"),
            "~/.hermes/profiles/*/cache/audio": lambda: self.audio(
                self.home, ".hermes", "profiles", "dumply", "cache", "audio"),
            "~/.hermes/cache": lambda: self.audio(self.home, ".hermes", "cache"),
            "~/.hermes/audio_cache": lambda: self.audio(self.home, ".hermes", "audio_cache"),
            "~/.hermes/profiles/*/audio_cache": lambda: self.audio(
                self.home, ".hermes", "profiles", "dumply", "audio_cache"),
        }
        for label, make in cases.items():
            with self.subTest(label):
                audio = make()
                if label == "/tmp":
                    self.addCleanup(shutil.rmtree, audio.parent, ignore_errors=True)
                r = self.run_wrapper(audio)
                path = self.assert_ephemeral(r, audio)
                self.assertEqual(path.parent, self.eph)
                self.assertTrue(path.name.startswith(audio.stem + "-"), path.name)
                a = self.args()
                self.assertEqual(a[a.index("--output") + 1], str(path))
                self.assertEqual(a.count("--output"), 1)
                self.assertIn(str(audio), r.stderr)          # the note names the file it re-routed

    def test_var_tmp_and_tmpdir_are_scratch(self):
        tmpdir = self.src / "custom-tmpdir"
        with self.subTest("$TMPDIR"):
            audio = self.audio(tmpdir, "deep")
            r = self.run_wrapper(audio, TMPDIR=str(tmpdir))
            self.assert_ephemeral(r, audio)
        if os.access("/var/tmp", os.W_OK):
            with self.subTest("/var/tmp"):
                d = Path(tempfile.mkdtemp(dir="/var/tmp", prefix="scratch-guard-"))
                self.addCleanup(shutil.rmtree, d, ignore_errors=True)
                audio = self.audio(d)
                self.assert_ephemeral(self.run_wrapper(audio), audio)

    def test_a_symlink_is_judged_by_where_it_points(self):
        real = self.audio(self.home, ".claude", "tmp", "real")
        link = self.src / "linked.ogg"
        link.symlink_to(real)
        self.assert_ephemeral(self.run_wrapper(link), real)

    def test_an_ordinary_source_keeps_the_default_placement(self):
        audio = self.audio(self.home, "audio")
        r = self.run_wrapper(audio)
        path = self.assert_vault(r)
        self.assertNotIn("--output", self.args())
        self.assertFalse(self.eph.exists())
        self.assertEqual(path.name, "clip.md")

    def test_a_hermes_directory_that_is_not_a_cache_is_ordinary(self):
        audio = self.audio(self.home, ".hermes", "profiles", "ghoul", "inbox")
        self.assert_vault(self.run_wrapper(audio))
        self.assertNotIn("--output", self.args())

    def test_an_explicit_ephemeral_run_is_placed_in_the_ephemeral_dir_too(self):
        audio = self.audio(self.home, "audio")
        path = self.assert_ephemeral(self.run_wrapper(audio, "--ephemeral"), audio)
        self.assertEqual(path.parent, self.eph)
        self.assertNotIn("[scratch]", self.run_wrapper(audio, "--ephemeral").stderr)   # explicit: nothing to explain

    def test_a_json_run_keeps_the_json_extension_scripts_transcribe_py_gives_it(self):
        audio = self.audio(self.home, ".claude", "tmp", "asjson")
        path = self.assert_ephemeral(self.run_wrapper(audio, "--json"), audio)
        self.assertEqual((path.parent, path.suffix), (self.eph, ".json"))
        self.assertTrue(path.name.startswith("clip-"), path.name)

    def test_an_explicit_output_always_wins(self):
        for flag in ("--output", "-o"):
            with self.subTest(flag):
                audio = self.audio(self.home, ".claude", "tmp", "mine")
                dest = self.d / f"chosen{flag.strip('-')}.md"
                r = self.run_wrapper(audio, flag, str(dest))
                self.assertEqual(self.written(r), dest)
                a = self.args()
                self.assertEqual(a.count(flag), 1)
                self.assertNotIn(str(self.eph), " ".join(a))
        audio = self.audio(self.home, "audio", name="eq.ogg")
        dest = self.d / "chosen-eq.md"
        r = self.run_wrapper(audio, "--ephemeral", f"--output={dest}")
        self.assertEqual(self.written(r), dest)

    def test_a_tmpdir_that_contains_home_does_not_make_everything_scratch(self):
        audio = self.audio(self.home, "audio")
        for tmpdir in ("/", str(self.home), str(self.home.parent), str(self.src)):
            with self.subTest(tmpdir):
                self.assert_vault(self.run_wrapper(audio, TMPDIR=tmpdir))

    def test_waxd_style_run_under_wax_root_is_untouched(self):
        """waxd (components/wax/src/wax/transcribe_adapter.py) runs `WAX_TRANSCRIBE <audio> --no-diarization
        --asr-output <asr.json>` on files under WAX_ROOT/inbox or /stream, with its own environment. Even with
        WAX_ROOT and TMPDIR under /tmp, that run must not be given --output or announce a scratch re-route."""
        wax_root = self.d / "waxroot"
        for sub in ("inbox", "stream", "inbox/nested"):
            with self.subTest(sub):
                audio = self.audio(wax_root, sub)
                asr = wax_root / "var" / "logs" / "item" / "asr.json"
                r = self.run_wrapper(audio, "--no-diarization", "--asr-output", str(asr), TMPDIR=str(self.d))
                self.assert_vault(r)
                a = self.args()
                self.assertEqual(a[:4], [str(audio), "--no-diarization", "--asr-output", str(asr)])
                self.assertNotIn("--output", a)
                self.assertNotIn("-o", a)
                self.assertFalse(self.eph.exists())
        with self.subTest("the legacy WAX_AUDIO_ROOT names the same root (wax/paths.py: WAX_ROOT, then WAX_AUDIO_ROOT)"):
            legacy = self.d / "legacyroot"
            audio = self.audio(legacy, "inbox")
            r = self.run_wrapper(audio, "--no-diarization", WAX_ROOT="", WAX_AUDIO_ROOT=str(legacy), TMPDIR=str(self.d))
            self.assert_vault(r)
            self.assertNotIn("--output", self.args())
            self.assertFalse(self.eph.exists())
        with self.subTest("control: the same layout outside WAX_ROOT is scratch"):
            shutil.rmtree(self.vault)           # the runs above legitimately wrote their notes here
            audio = self.audio(self.d / "elsewhere", "inbox")
            self.assert_ephemeral(self.run_wrapper(audio, "--no-diarization"), audio)

    def test_a_scratch_source_still_gets_the_s3_archive_it_asked_for(self):
        """Only output placement follows scratch audio: an explicit --archive-s3 is still honoured."""
        audio = self.audio(self.home, ".claude", "tmp", "keepme")
        r = self.run_wrapper(audio, "--archive-s3")
        self.assert_ephemeral(r, audio)
        self.assertIn("cp --quiet", self.mc_log.read_text())

    def test_an_explicit_ephemeral_run_is_never_archived(self):
        audio = self.audio(self.home, ".claude", "tmp", "dropme")
        self.assert_ephemeral(self.run_wrapper(audio, "--ephemeral", "--archive-s3"), audio)
        self.assertFalse(self.mc_log.exists())

    def test_a_normal_source_is_still_archived(self):
        audio = self.audio(self.home, "audio")
        self.assert_vault(self.run_wrapper(audio, "--archive-s3"))
        self.assertIn("cp --quiet", self.mc_log.read_text())

    def test_scratch_routing_does_not_change_the_fast_lane(self):
        """A scratch source that is not --ephemeral still waits for the heavy slot with its own model."""
        audio = self.audio(self.home, ".claude", "tmp", "notfast")
        self.assert_ephemeral(self.run_wrapper(audio, "--model", "large-v3"), audio)
        a = self.args()
        self.assertEqual(a[a.index("--model") + 1], "large-v3")
        self.assertNotIn("--no-diarization", a)


class ScratchSourceRemoteTest(_WrapperCase):
    """The remote branch (scp the transcript back): $TRANSCRIPTS for ordinary audio, the ephemeral dir otherwise."""

    def run_remote(self, audio, *extra, **env):
        path = f"{self.fakebin}:{os.environ['PATH']}"
        return self.run_wrapper(audio, *extra, TRANSCRIBE_HOSTNAME="not-this-host.invalid", PATH=path, **env)

    def test_an_ordinary_source_lands_in_transcripts(self):
        audio = self.audio(self.home, "audio")
        path = self.assert_vault(self.run_remote(audio))
        self.assertEqual(path, self.vault / "out.md")

    def test_a_scratch_source_lands_in_the_ephemeral_dir(self):
        audio = self.audio(self.home, ".claude", "tmp", "tmpab12")
        r = self.run_remote(audio)
        path = self.assert_ephemeral(r, audio)
        self.assertEqual(path, self.eph / "out.md")

    def test_an_explicit_ephemeral_run_lands_in_the_ephemeral_dir(self):
        audio = self.audio(self.home, "audio")
        self.assertEqual(self.assert_ephemeral(self.run_remote(audio, "--ephemeral"), audio), self.eph / "out.md")

    def test_a_scratch_source_still_gets_the_s3_archive_it_asked_for(self):
        audio = self.audio(self.home, ".claude", "tmp", "keepme")
        self.assert_ephemeral(self.run_remote(audio, "--archive-s3"), audio)
        self.assertIn("mc cp --quiet", self.ssh_log.read_text())

    # What the REMOTE transcribe.py is told: with no --output it writes ~/d/Transcripts/<stem>.md on the remote host
    # (the real vault when that host is big-chungus), whatever the local copy-back does with the transcript.
    def remote_lines(self):
        return self.ssh_log.read_text().splitlines()

    def remote_command(self):
        lines = [line for line in self.remote_lines() if "scripts/transcribe.py" in line]
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    def remote_cleanup(self):
        lines = [line for line in self.remote_lines() if line.startswith("rm -f ")]
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    def test_an_ordinary_source_is_not_given_an_output(self):
        self.assert_vault(self.run_remote(self.audio(self.home, "audio")))
        self.assertNotIn("--output", self.remote_command())

    def test_scratch_and_ephemeral_runs_keep_the_remote_transcript_out_of_the_remote_vault(self):
        cases = {"scratch source": (self.audio(self.home, ".claude", "tmp", "tmpab12", name="tmpab12.ogg"), ()),
                 "--ephemeral": (self.audio(self.home, "audio", name="tmpab12.ogg"), ("--ephemeral",))}
        for label, (audio, extra) in cases.items():
            with self.subTest(label):
                self.ssh_log.unlink(missing_ok=True)
                self.assert_ephemeral(self.run_remote(audio, *extra), audio)
                match = re.search(r"--output (/tmp/transcribe-staging/tmpab12-\d+\.md)(?: |$)", self.remote_command())
                self.assertTrue(match, self.remote_command())
                self.assertEqual(self.remote_command().count("--output"), 1)
                self.assertIn(f"'{match.group(1)}'", self.remote_cleanup())   # removed from the host on exit

    def test_a_remote_json_run_keeps_the_json_extension(self):
        audio = self.audio(self.home, ".claude", "tmp", "asjson", name="asjson.ogg")
        self.run_remote(audio, "--json")
        self.assertRegex(self.remote_command(), r"--output /tmp/transcribe-staging/asjson-\d+\.json")

    def test_an_explicit_output_wins_on_the_remote_branch_too(self):
        audio = self.audio(self.home, ".claude", "tmp", "mine")
        self.run_remote(audio, "--output", "/srv/chosen.md")
        command = self.remote_command()
        self.assertEqual(command.count("--output"), 1)
        self.assertIn("--output /srv/chosen.md", command)
        self.assertEqual(self.remote_cleanup().count("'"), 4)         # staged audio and log only


if __name__ == "__main__":
    unittest.main()
