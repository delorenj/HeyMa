"""bin/transcribe: the scratch-audio guard and the other path resolution work on a Mac.

The guard (commit 1684458) used GNU `realpath -m`. macOS 13+ ships /bin/realpath WITHOUT -m, older macOS has no
realpath, and the `2>/dev/null || true` around every call turned both into an empty answer, so on a Mac every
comparison silently failed and scratch audio went to the vault. `AUDIO="$(realpath "$AUDIO")"` aborted the whole
run on a Mac with no realpath, and the self-locating TRANSCRIBE_DIR fell back to the symlink's own directory.

Where bin/transcribe runs on a Mac: it ships in none of the Mac packages (the `heyma` cask is the UI only, the
`vinyl-client` formula and the direct installer carry only `bin/vinyl`), so it runs there only from a HeyMa
checkout, in its REMOTE mode (hostname is not big-chungus: scp to the host, transcribe there, scp the note back).
There the guard decides where the local copy-back lands and whether the remote transcribe.py is told an --output.

A Mac is simulated on this Linux host with a PATH of its own: symlinks to the tools the wrapper needs, plus

    bsd   a `realpath` shim that behaves like macOS 13+: `realpath [-q] [path ...]`, "illegal option" for -m,
          an error for a path that does not exist
    none  no `realpath` at all (macOS 12 and older)
    one   a one-operand `realpath` script (macOS 12 and older, the classic python/perl shim): no option parsing, so
          `realpath -m -- /x` takes "-m" for the path and answers "$PWD/-m" with exit 0
    bare  neither realpath nor readlink, so a symlink cannot be followed at all (the lexical fallback)

and macOS's /private aliases (/tmp, /var -> /private/...) with a symlink farm in a temp dir plus $TMPDIR, because
/tmp itself cannot be repointed here. The lexical /private spelling is checked on the extracted shell functions.

TRANSCRIBE_WRAPPER_UNDER_TEST points the whole file at another copy of the wrapper (the proof that these tests
fail on the pre-fix version: `git show HEAD~:bin/transcribe > /tmp/x/transcribe`).
"""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_transcribe_scratch_output import ROOT, _under, _WrapperCase

WRAPPER = Path(os.environ.get("TRANSCRIBE_WRAPPER_UNDER_TEST") or ROOT / "bin" / "transcribe")

# What bin/transcribe runs on the local and the remote path (the stand-in python and mc are scripts of their own).
TOOLS = ("bash env sh hostname nice dirname basename mkdir tee awk flock stat date cp rm sleep cat ls sed grep tr wc "
         "head tail sort cut mktemp uname id ffprobe")

BSD_REALPATH = """#!/bin/sh
# macOS 13+ /bin/realpath: `realpath [-q] [path ...]`. No -m, no -e, no GNU long options, and a path that does not
# exist is an error. Every call is logged so a test can see that the wrapper asked for -m and was refused.
printf '%s\\n' "$*" >> "$REALPATH_SHIM_LOG"
quiet=0
while [ $# -gt 0 ]; do
  case "$1" in
    -q) quiet=1; shift ;;
    --) shift; break ;;
    -*) echo "realpath: illegal option -- ${1#-}" >&2; echo "usage: realpath [-q] [path ...]" >&2; exit 1 ;;
    *) break ;;
  esac
done
rc=0
for p in "$@"; do
  if [ ! -e "$p" ]; then [ "$quiet" = 1 ] || echo "realpath: $p: No such file or directory" >&2; rc=1; continue; fi
  "$GNU_REALPATH" "$p" || rc=1
done
exit $rc
"""


ONE_OPERAND_REALPATH = """#!/bin/sh
# The classic pre-13 macOS shim, `realpath() { python -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"; }`
# as a script: ONE operand and no option parsing. Asked `-m -- /x` it resolves "-m" and exits 0.
printf '%s\\n' "$*" >> "$REALPATH_SHIM_LOG"
exec "$GNU_REALPATH" -- "$1"
"""


def build_path_dir(where: Path, *, realpath: str | None, readlink: bool = True) -> Path:
    """A directory that is the WHOLE of a simulated Mac's PATH. `realpath` is "bsd", "one" or None (absent)."""
    where.mkdir(parents=True)
    names = TOOLS.split() + (["readlink"] if readlink else [])
    for name in names:
        found = shutil.which(name)
        if found:
            (where / name).symlink_to(found)
    if realpath in ("bsd", "one"):
        shim = where / "realpath"
        shim.write_text(BSD_REALPATH if realpath == "bsd" else ONE_OPERAND_REALPATH)
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return where


def portable_block() -> str:
    match = re.search(r"^# >>> portable-path.*?^# <<< portable-path$", WRAPPER.read_text(), re.S | re.M)
    if not match:
        raise AssertionError(f"{WRAPPER} has no '# >>> portable-path' block: it still calls GNU realpath -m directly")
    return match.group(0)


def shell_function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{$.*?^\}}$", WRAPPER.read_text(), re.S | re.M)
    if not match:
        raise AssertionError(f"{WRAPPER} has no function {name}")
    return match.group(0)


def run_bash(script: str, *args: str, path: str, cwd=None, env=None) -> subprocess.CompletedProcess:
    """`script` runs under the wrapper's own shell options, with PATH set to exactly `path`."""
    full = dict(os.environ if env is None else env, PATH=path)
    return subprocess.run([shutil.which("bash"), "-c", "set -euo pipefail\n" + script, "bash", *args], env=full,
                          cwd=cwd, capture_output=True, text=True, timeout=60)


class _MacCase(_WrapperCase):
    """The scratch-guard fixture of tests/test_transcribe_scratch_output.py, run with a Mac's PATH."""

    realpath: str | None = "bsd"          # what the simulated Mac has: "bsd", None (nothing) ...
    readlink = True                       # ... and whether it still has readlink

    def setUp(self):
        super().setUp()
        self.macbin = build_path_dir(self.d / "macbin", realpath=self.realpath, readlink=self.readlink)
        self.realpath_log = self.d / "realpath.log"
        (self.d / "pwd").write_text("")
        # The stand-in interpreter also says where it was started: the wrapper cd's into TRANSCRIBE_DIR first.
        fake = self.d / "python"
        fake.write_text(fake.read_text().replace('shift\n', 'shift\npwd -P > "$(dirname "$0")/pwd"\n', 1))

    def mac_path(self, *front: Path) -> str:
        return ":".join([*map(str, front), str(self.macbin)])

    def run_wrapper(self, audio, *extra, **env):
        env.setdefault("PATH", self.mac_path())
        env.setdefault("REALPATH_SHIM_LOG", str(self.realpath_log))
        env.setdefault("GNU_REALPATH", shutil.which("realpath") or "")
        return subprocess.run([str(WRAPPER), str(audio), *extra], env=self.env(**env), capture_output=True,
                              text=True, timeout=60)

    def run_remote(self, audio, *extra, **env):
        return self.run_wrapper(audio, *extra, TRANSCRIBE_HOSTNAME="not-this-host.invalid",
                                PATH=self.mac_path(self.fakebin), **env)

    def mac_tree(self) -> tuple[Path, Path, Path]:
        """macOS's layout in a temp dir: real files under private/, /var as a symlink into it, $TMPDIR via the alias.
        Returns (alias TMPDIR with a trailing slash, the same directory spelled through /private, the sibling C/)."""
        mac = self.src / "macfs"
        real_tmp = mac / "private" / "var" / "folders" / "xx" / "yyyy" / "T"
        (mac / "private" / "var" / "folders" / "xx" / "yyyy" / "C").mkdir(parents=True)
        real_tmp.mkdir(parents=True)
        (mac / "var").symlink_to("private/var")
        alias_tmpdir = mac / "var" / "folders" / "xx" / "yyyy" / "T"
        return Path(f"{alias_tmpdir}/"), real_tmp, real_tmp.parent / "C"


class _MacScratchGuard:
    """The behaviour every simulated Mac must share. Mixed into a case that picks the Mac's realpath."""

    def test_the_simulation_is_what_it_claims(self):
        if self.realpath == "bsd":
            refused = run_bash('realpath -m -- /tmp', path=self.mac_path(), env=dict(
                os.environ, REALPATH_SHIM_LOG=str(self.realpath_log), GNU_REALPATH=shutil.which("realpath")))
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("illegal option -- m", refused.stderr)
            missing = run_bash('realpath /nonexistent/x', path=self.mac_path(), env=dict(
                os.environ, REALPATH_SHIM_LOG=str(self.realpath_log), GNU_REALPATH=shutil.which("realpath")))
            self.assertNotEqual(missing.returncode, 0)
        elif self.realpath == "one":
            lied = run_bash('realpath -m -- /tmp', path=self.mac_path(), cwd="/", env=dict(
                os.environ, REALPATH_SHIM_LOG=str(self.realpath_log), GNU_REALPATH=shutil.which("realpath")))
            self.assertEqual((lied.returncode, lied.stdout.strip()), (0, "/-m"))     # exit 0, absolute, wrong
        else:
            self.assertIsNone(shutil.which("realpath", path=str(self.macbin)))
        self.assertEqual(shutil.which("readlink", path=str(self.macbin)) is not None, self.readlink)

    def test_a_source_under_a_scratch_location_stays_out_of_the_vault(self):
        cases = {
            "/tmp": lambda: self.audio(tempfile.mkdtemp(dir="/tmp", prefix="scratch-guard-")),
            "~/.claude/tmp": lambda: self.audio(self.home, ".claude", "tmp", "tmpab12"),
            "~/.cache": lambda: self.audio(self.home, ".cache", "voice"),
            "~/.hermes/profiles/*/cache/audio": lambda: self.audio(
                self.home, ".hermes", "profiles", "dumply", "cache", "audio"),
            "~/.hermes/audio_cache": lambda: self.audio(self.home, ".hermes", "audio_cache"),
        }
        for label, make in cases.items():
            with self.subTest(label):
                audio = make()
                if label == "/tmp":
                    self.addCleanup(shutil.rmtree, audio.parent, ignore_errors=True)
                r = self.run_wrapper(audio)
                path = self.assert_ephemeral(r, audio)
                self.assertEqual(path.parent, self.eph)
                self.assertIn(str(audio), r.stderr)

    def test_var_tmp_is_scratch(self):
        if not os.access("/var/tmp", os.W_OK):
            self.skipTest("/var/tmp is not writable here")
        d = Path(tempfile.mkdtemp(dir="/var/tmp", prefix="scratch-guard-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        audio = self.audio(d)
        self.assert_ephemeral(self.run_wrapper(audio), audio)

    def test_a_macos_tmpdir_is_scratch_however_it_is_spelled(self):
        """$TMPDIR on a Mac is /var/folders/xx/yyyy/T/ (trailing slash), and /var is a symlink into /private."""
        alias_tmpdir, real_tmp, sibling = self.mac_tree()
        for label, audio, tmpdir in (
            ("resolved audio, aliased TMPDIR", self.audio(real_tmp, "deep"), str(alias_tmpdir)),
            ("aliased audio, aliased TMPDIR", self.audio(Path(f"{alias_tmpdir}"), "deep"), str(alias_tmpdir)),
            ("aliased audio, resolved TMPDIR", self.audio(Path(f"{alias_tmpdir}"), "deep2"), str(real_tmp)),
        ):
            with self.subTest(label):
                r = self.run_wrapper(audio, TMPDIR=tmpdir)
                self.assert_ephemeral(r, audio)
                self.assertIn(str(real_tmp), r.stderr)      # the note names the RESOLVED file the guard judged

    def test_a_sibling_of_the_macos_tmpdir_is_not_scratch(self):
        alias_tmpdir, _real_tmp, sibling = self.mac_tree()
        audio = self.audio(sibling)                         # .../yyyy/C, next to .../yyyy/T
        self.assert_vault(self.run_wrapper(audio, TMPDIR=str(alias_tmpdir)))

    def test_a_symlink_is_judged_by_where_it_points(self):
        real = self.audio(self.home, ".claude", "tmp", "real")
        link = self.src / "linked.ogg"
        link.symlink_to(real)
        r = self.run_wrapper(link)
        self.assert_ephemeral(r, real)
        self.assertNotIn("[warn]", r.stderr)

    def test_an_ordinary_source_keeps_the_default_placement(self):
        audio = self.audio(self.home, "audio")
        r = self.run_wrapper(audio)
        self.assert_vault(r)
        self.assertNotIn("--output", self.args())
        self.assertFalse(self.eph.exists())
        self.assertNotIn("[warn]", r.stderr)

    def test_a_tmpdir_that_contains_home_does_not_make_everything_scratch(self):
        audio = self.audio(self.home, "audio")
        for tmpdir in ("/", str(self.home), str(self.home.parent), str(self.src), f"{self.src}/"):
            with self.subTest(tmpdir):
                self.assert_vault(self.run_wrapper(audio, TMPDIR=tmpdir))

    def test_waxd_style_run_under_wax_root_is_untouched(self):
        """The Linux contract waxd relies on, unchanged: audio under WAX_ROOT is never scratch (stdout is one line,
        no --output is added), even when WAX_ROOT and TMPDIR both sit under /tmp."""
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
                self.assertFalse(self.eph.exists())
        with self.subTest("control: the same layout outside WAX_ROOT is scratch"):
            shutil.rmtree(self.vault)
            audio = self.audio(self.d / "elsewhere", "inbox")
            self.assert_ephemeral(self.run_wrapper(audio, "--no-diarization"), audio)

    def test_a_wax_root_behind_a_macos_alias_still_exempts_waxd(self):
        """WAX_ROOT spelled through /var, audio spelled through /private/var (and the other way round)."""
        alias_tmpdir, real_tmp, _sibling = self.mac_tree()
        for root_spelling, audio_dir in ((alias_tmpdir / "wax", real_tmp / "wax"),
                                         (real_tmp / "wax", Path(f"{alias_tmpdir}") / "wax")):
            with self.subTest(str(root_spelling)):
                audio = self.audio(audio_dir, "inbox")
                r = self.run_wrapper(audio, "--no-diarization", WAX_ROOT=str(root_spelling), TMPDIR=str(alias_tmpdir))
                self.assert_vault(r)
                self.assertNotIn("--output", self.args())

    def test_the_wrapper_finds_its_own_directory_through_a_symlink(self):
        """~/.local/bin/transcribe is a symlink to the repo shim; TRANSCRIBE_DIR defaults to the repo. Without a
        realpath the old fallback was the symlink's own directory."""
        link = self.d / "linkbin" / "transcribe"
        link.parent.mkdir()
        link.symlink_to(WRAPPER.resolve())
        env = self.env(PATH=self.mac_path(), REALPATH_SHIM_LOG=str(self.realpath_log),
                       GNU_REALPATH=shutil.which("realpath") or "")
        env.pop("TRANSCRIBE_DIR")
        audio = self.audio(self.home, "audio")
        r = subprocess.run([str(link), str(audio)], env=env, capture_output=True, text=True, timeout=60)
        self.assert_vault(r)
        self.assertEqual((self.d / "pwd").read_text().strip(), str(WRAPPER.resolve().parent.parent))

    def test_a_missing_audio_file_fails_fast_with_a_message(self):
        r = self.run_wrapper(self.src / "no-such-dir" / "nope.ogg")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertIn("audio file not found", r.stderr)
        self.assertFalse(self.vault.exists() or self.eph.exists())

    # The remote branch is the only one a Mac takes (hostname is not big-chungus).
    def test_remote_a_scratch_source_lands_in_the_ephemeral_dir(self):
        audio = self.audio(self.home, ".claude", "tmp", "tmpab12", name="tmpab12.ogg")
        path = self.assert_ephemeral(self.run_remote(audio), audio)
        self.assertEqual(path, self.eph / "out.md")
        sent = [line for line in self.ssh_log.read_text().splitlines() if "scripts/transcribe.py" in line]
        self.assertEqual(len(sent), 1, sent)
        self.assertRegex(sent[0], r"--output /tmp/transcribe-staging/tmpab12-\d+\.md")

    def test_remote_a_macos_tmpdir_source_lands_in_the_ephemeral_dir(self):
        alias_tmpdir, real_tmp, _sibling = self.mac_tree()
        audio = self.audio(real_tmp)
        self.assert_ephemeral(self.run_remote(audio, TMPDIR=str(alias_tmpdir)), audio)

    def test_remote_an_ordinary_source_lands_in_transcripts(self):
        audio = self.audio(self.home, "audio")
        path = self.assert_vault(self.run_remote(audio))
        self.assertEqual(path, self.vault / "out.md")
        sent = [line for line in self.ssh_log.read_text().splitlines() if "scripts/transcribe.py" in line]
        self.assertNotIn("--output", sent[0])


class BsdRealpathMacTest(_MacScratchGuard, _MacCase):
    """macOS 13+: /bin/realpath exists, has no -m, and refuses a path that does not exist."""

    realpath = "bsd"

    def test_gnu_realpath_is_asked_first_then_refused_and_the_guard_still_works(self):
        audio = self.audio(self.home, ".claude", "tmp", "tmpab12")
        self.assert_ephemeral(self.run_wrapper(audio), audio)
        asked = self.realpath_log.read_text().splitlines()
        self.assertTrue(any(line.startswith("-m ") for line in asked), asked)


class NoRealpathMacTest(_MacScratchGuard, _MacCase):
    """macOS 12 and older: no realpath at all. The old `AUDIO="$(realpath ...)"` killed the run with 127."""

    realpath = None

    def test_nothing_called_realpath(self):
        audio = self.audio(self.home, ".claude", "tmp", "tmpab12")
        r = self.run_wrapper(audio)
        self.assert_ephemeral(r, audio)
        self.assertNotIn("realpath", r.stderr)                 # no "command not found" noise either


class OneOperandRealpathMacTest(_MacScratchGuard, _MacCase):
    """A Mac whose `realpath` is a one-operand script. It exits 0 with an absolute path for `-m -- p`, so a resolver
    that trusts any absolute answer reports every audio file as "not found" (HEAD ran there, guard inert)."""

    realpath = "one"

    def test_the_lie_is_probed_and_refused_and_every_audio_file_is_still_found(self):
        audio = self.audio(self.home, ".claude", "tmp", "tmpab12")
        self.assert_ephemeral(self.run_wrapper(audio), audio)
        ordinary = self.audio(self.home, "audio")
        r = self.run_wrapper(ordinary)
        self.assert_vault(r)
        self.assertNotIn("audio file not found", r.stderr)
        asked = self.realpath_log.read_text().splitlines()
        self.assertTrue(asked and all(line.startswith("-m -- /rp-probe-missing") for line in asked), asked)  # probed only


class NoRealpathNoReadlinkTest(_MacCase):
    """The floor: a symlink cannot be followed at all. A plain path is still judged exactly; a symlinked one is
    judged by its spelling, and the wrapper says so instead of silently trusting a guess."""

    realpath = None
    readlink = False

    def test_a_plain_scratch_path_is_still_detected_and_says_nothing_extra(self):
        audio = self.audio(self.home, ".claude", "tmp", "tmpab12")
        r = self.run_wrapper(audio)
        self.assert_ephemeral(r, audio)
        self.assertNotIn("[warn]", r.stderr)

    def test_an_ordinary_path_is_judged_ordinary_without_a_warning(self):
        r = self.run_wrapper(self.audio(self.home, "audio"))
        self.assert_vault(r)
        self.assertNotIn("[warn]", r.stderr)

    def test_a_macos_tmpdir_is_scratch_when_both_sides_are_spelled_the_same_way(self):
        alias_tmpdir, _real_tmp, _sibling = self.mac_tree()
        audio = self.audio(Path(f"{alias_tmpdir}"), "deep")    # no readlink: (/var) cannot be followed, so the
        r = self.run_wrapper(audio, TMPDIR=str(alias_tmpdir))  # audio and TMPDIR are both compared by spelling
        self.assert_ephemeral(r, audio)

    def test_a_symlinked_source_is_judged_by_its_spelling_and_the_wrapper_says_so(self):
        real = self.audio(self.home, ".claude", "tmp", "real")
        link = self.src / "linked.ogg"
        link.symlink_to(real)
        r = self.run_wrapper(link)
        self.assert_vault(r)                                   # a guess: it looks ordinary ...
        self.assertIn("[warn] could not follow every symlink", r.stderr)   # ... and the wrapper admits it

    def test_a_scratch_verdict_needs_no_warning_even_with_a_symlink(self):
        d = Path(tempfile.mkdtemp(dir="/tmp", prefix="scratch-guard-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        real = self.audio(self.home, "audio")
        link = d / "linked.ogg"
        link.symlink_to(real)                                  # spelled /tmp/...: scratch whatever it points at
        r = self.run_wrapper(link)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[scratch]", r.stderr)
        self.assertNotIn("[warn]", r.stderr)


class ResolverUnitTest(unittest.TestCase):
    """resolve_path and path_under, extracted from the wrapper and run under its shell options."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="resolver-")
        self.addCleanup(self.tmp.cleanup)
        self.t = Path(os.path.realpath(self.tmp.name))
        (self.t / "a" / "b").mkdir(parents=True)
        (self.t / "a" / "b" / "file").write_text("x")
        (self.t / "ln1").symlink_to("a/b")
        (self.t / "ln2").symlink_to("../a")
        (self.t / "abs").symlink_to(self.t / "a")
        (self.t / "flnk").symlink_to("a/b/file")
        (self.t / "chain1").symlink_to("chain2")
        (self.t / "chain2").symlink_to("a")
        (self.t / "loopA").symlink_to("loopB")
        (self.t / "loopB").symlink_to("loopA")
        self.gnu = os.environ["PATH"]
        self.walker = str(build_path_dir(self.t / "_bin_none", realpath=None))
        self.bare = str(build_path_dir(self.t / "_bin_bare", realpath=None, readlink=False))
        self.block = portable_block()

    def resolve(self, paths, *, path, cwd=None):
        script = self.block + '\nfor p in "$@"; do PATH_RESOLVE_LEXICAL=0; resolve_path "$p"; printf "%s|%s\\n" "$RESOLVED_PATH" "$PATH_RESOLVE_LEXICAL"; done\n'
        r = run_bash(script, *paths, path=path, cwd=cwd)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [line.rsplit("|", 1) for line in r.stdout.splitlines()]

    def test_the_shell_walker_agrees_with_gnu_realpath_m(self):
        t = self.t
        paths = ["/", "//", "/tmp", "/tmp/", "//tmp//x/../y", f"{t}/a/b", f"{t}/a/./b/", f"{t}/a/b/../c",
                 f"{t}/ln1", f"{t}/ln1/x/y", f"{t}/ln1/..", f"{t}/ln1/../missing/q", f"{t}/ln2/b", f"{t}/abs/b",
                 f"{t}/flnk", f"{t}/chain1/b/file", f"{t}/missing/../a", f"{t}/we ird*/?", f"{t}/a/b/file/x",
                 "a/b", "./a/../a/b", "-dash", "ln1/file", "../" + t.name + "/a"]
        gnu = self.resolve(paths, path=self.gnu, cwd=t)
        walked = self.resolve(paths, path=self.walker, cwd=t)
        for p, g, w in zip(paths, gnu, walked):
            with self.subTest(p):
                expect = subprocess.run(["realpath", "-m", "--", "./" + p if p.startswith("-") else p],
                                        capture_output=True, text=True, cwd=t).stdout.strip()
                self.assertEqual(g, [expect, "0"])
                self.assertEqual(w, [expect, "0"])

    def test_a_symlink_loop_terminates_lexically_and_is_flagged(self):
        self.assertEqual(self.resolve([f"{self.t}/loopA/x"], path=self.walker)[0][1], "1")

    def test_without_readlink_a_link_is_kept_as_spelled_and_flagged_but_a_plain_path_is_not(self):
        flagged = self.resolve([f"{self.t}/ln1/file", f"{self.t}/a/b/file"], path=self.bare)
        self.assertEqual(flagged[0], [f"{self.t}/ln1/file", "1"])
        self.assertEqual(flagged[1], [f"{self.t}/a/b/file", "0"])

    def test_an_empty_path_is_refused_not_resolved_to_the_cwd(self):
        r = run_bash(self.block + '\nif resolve_path ""; then echo yes; else echo no; fi', path=self.walker)
        self.assertEqual(r.stdout.strip(), "no")

    def test_a_resolver_that_answers_garbage_is_not_trusted(self):
        """A realpath that exits 0 with a relative or empty answer (some shims do) falls through to the walker."""
        liar = self.t / "_bin_liar"
        build_path_dir(liar, realpath=None)
        (liar / "realpath").write_text("#!/bin/sh\necho relative/thing\n")
        (liar / "realpath").chmod(0o755)
        self.assertEqual(self.resolve([f"{self.t}/ln1"], path=str(liar))[0], [f"{self.t}/a/b", "0"])

    def test_a_one_operand_realpath_shim_that_ignores_options_is_not_trusted(self):
        """`realpath -m -- p` on a shim that reads only $1 answers "$PWD/-m" with exit 0 for every p."""
        shim = self.t / "_bin_oneoperand"
        build_path_dir(shim, realpath=None)
        (shim / "realpath").write_text('#!/bin/sh\nprintf "%s/%s\\n" "$PWD" "$1"\n')
        (shim / "realpath").chmod(0o755)
        got = self.resolve([f"{self.t}/ln1", f"{self.t}/a/missing/../b"], path=str(shim), cwd=self.t)
        self.assertEqual(got, [[f"{self.t}/a/b", "0"], [f"{self.t}/a/b", "0"]])

    def test_path_under_knows_the_private_aliases_and_the_edges(self):
        cases = [
            ("/private/tmp/x", "/tmp", "yes"), ("/tmp/x", "/private/tmp", "yes"),
            ("/private/var/tmp/x", "/var/tmp", "yes"), ("/var/tmp/x", "/private/var/tmp", "yes"),
            ("/private/var/folders/xx/T/x", "/var/folders/xx/T", "yes"),
            ("/var/folders/xx/T/x", "/private/var/folders/xx/T", "yes"),
            ("/private/etc/x", "/etc", "yes"), ("/tmp/x", "/tmp", "yes"), ("/tmp/x/y/z", "/tmp", "yes"),
            ("/tmp", "/tmp", "no"),                       # strictly inside: a dir is not under itself
            ("/tmpfoo/x", "/tmp", "no"), ("/private/tmpfoo/x", "/tmp", "no"), ("/private/tmp/x", "/tmpfoo", "no"),
            ("/Users/me/x", "/private/tmp", "no"), ("/home/me/.cache/x", "/var/tmp", "no"),
            ("/anything", "/", "no"), ("/anything", "", "no"), ("", "/tmp", "no"),   # "/" and "" are never a DIR
        ]
        script = self.block + '\nfor pair in "$@"; do if path_under "${pair%%|*}" "${pair##*|}"; then echo yes; else echo no; fi; done\n'
        r = run_bash(script, *[f"{child}|{parent}" for child, parent, _ in cases], path=self.gnu)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), [expect for _, _, expect in cases])

    def scratch(self, audio, *, path, tmpdir="", home=None, wax_root=None) -> bool:
        """is_scratch_audio on a path that need not exist, with no way at all to resolve it (the `bare` PATH)."""
        script = (self.block + "\n" + shell_function("is_scratch_audio") +
                  '\nif is_scratch_audio "$1"; then echo scratch; else echo ordinary; fi')
        env = dict(os.environ, HOME=home or "/Users/me", TMPDIR=tmpdir)
        env.pop("WAX_ROOT", None)
        env.pop("WAX_AUDIO_ROOT", None)
        if wax_root:
            env["WAX_ROOT"] = wax_root
        r = run_bash(script, audio, path=path, env=env, cwd="/")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip() == "scratch"

    def test_the_guard_judges_a_macos_path_with_nothing_to_resolve_it(self):
        mac_tmp = "/var/folders/xx/yyyy/T/"
        for audio, expect in (
            ("/private/tmp/clip.ogg", True),                         # /tmp -> /private/tmp
            ("/tmp/clip.ogg", True),
            ("/private/var/tmp/clip.ogg", True),                     # /var/tmp -> /private/var/tmp
            ("/private/var/folders/xx/yyyy/T/clip.ogg", True),       # $TMPDIR, resolved spelling
            ("/var/folders/xx/yyyy/T/deep/clip.ogg", True),          # $TMPDIR, as given
            ("/private/var/folders/xx/yyyy/C/clip.ogg", False),      # the Mac's cache dir, next to T
            ("/Users/me/Music/clip.ogg", False),
            ("/Users/me/.cache/voice/clip.ogg", True),
            ("/Users/me/.hermes/profiles/ghoul/cache/clip.ogg", True),
            ("/Users/me/.hermes/profiles/ghoul/inbox/clip.ogg", False),
            ("/private/tmpfoo/clip.ogg", False),
        ):
            with self.subTest(audio):
                self.assertEqual(self.scratch(audio, path=self.bare, tmpdir=mac_tmp), expect)

    def test_a_wax_root_on_a_mac_is_exempt_in_either_spelling(self):
        for root, audio in (("/private/tmp/wax", "/tmp/wax/inbox/a.ogg"), ("/tmp/wax", "/private/tmp/wax/inbox/a.ogg")):
            with self.subTest(root):
                self.assertFalse(self.scratch(audio, path=self.bare, wax_root=root))
        self.assertTrue(self.scratch("/tmp/elsewhere/inbox/a.ogg", path=self.bare, wax_root="/tmp/wax"))

    def test_a_tmpdir_that_is_home_or_above_it_is_ignored(self):
        for tmpdir in ("/", "/Users", "/Users/", "/Users/me"):
            with self.subTest(tmpdir):
                self.assertFalse(self.scratch("/Users/me/Music/clip.ogg", path=self.bare, tmpdir=tmpdir))


class LinuxStaysTheSameTest(_WrapperCase):
    """The control: with GNU realpath on PATH (big-chungus) nothing about the answer changed."""

    def run_wrapper(self, audio, *extra, **env):
        return subprocess.run([str(WRAPPER), str(audio), *extra], env=self.env(**env), capture_output=True,
                              text=True, timeout=60)

    def test_scratch_and_ordinary_sources_and_the_stdout_contract(self):
        scratch = self.audio(self.home, ".claude", "tmp", "tmpab12")
        path = self.assert_ephemeral(self.run_wrapper(scratch), scratch)
        self.assertEqual(path.parent, self.eph)
        ordinary = self.audio(self.home, "audio")
        r = self.run_wrapper(ordinary)
        self.assert_vault(r)
        self.assertNotIn("[warn]", r.stderr)

    def test_waxd_audio_under_wax_root_is_never_scratch(self):
        audio = self.audio(self.d / "waxroot", "inbox")
        r = self.run_wrapper(audio, "--no-diarization", TMPDIR=str(self.d))
        self.assert_vault(r)
        self.assertNotIn("--output", self.args())
        self.assertEqual(len(r.stdout.splitlines()), 1)

    def test_a_missing_audio_file_fails_fast_with_a_message(self):
        r = self.run_wrapper(self.src / "no-such-dir" / "nope.ogg")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("audio file not found", r.stderr)


if __name__ == "__main__":
    unittest.main()
