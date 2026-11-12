"""Suite-wide isolation of the root tests (bin/transcribe, bin/hermes-transcribe-adapter, the gateway) from the
REAL vault, the REAL ledger and the machine-wide transcription slot.

Incident 2026-11: junk notes reached ~/d/Transcripts (fixture notes from an unpinned test run; `tmp*.md` and
`proof.md` from ad-hoc agent runs). The mechanism for the transcribe ones: the interactive shell exports
TRANSCRIPTS=$HOME/d/Transcripts globally and these tests spawn bin/transcribe with `dict(os.environ, ...)`, so a
non-ephemeral run inherits it and moves its output into the real vault.

So, before any test runs, one session sandbox is created in the system temp dir and these are pinned to it; every
subprocess the tests spawn with an inherited environment resolves there:

    TRANSCRIPTS, WAX_VAULT, WAX_ROOT           the vault, and the runtime root (ledger var/wax.db, inbox/, ...)
    TRANSCRIBE_STASH_DIR                       bin/transcribe's safety copies (default ~/audio/recovered)
    TRANSCRIBE_LOCK_FILE, ..._FAST_LOCK_FILE   the machine-wide slot (default ~/.cache/heyma-transcribe*.lock):
                                               a real transcription must not stall, or be stalled by, a test
    TRANSCRIBE_EPHEMERAL_DIR                   where a throwaway run's transcript goes
    MC_BIN                                     S3 uploads (`--archive-s3`) have no client to upload with
    HERMES_TRANSCRIBE_BIN                      the adapter's default is the real ~/.local/bin/transcribe

A test that builds a MINIMAL env (`env={"PATH": ...}`) does not inherit them and must carry its own.
`tests/test_isolation_guard.py` is the regression guard, and the autouse fixture below runs the same check before
every test: it fails (never skips) if the effective vault/ledger of this process is the real one (~/d/Transcripts,
its realpath ~/code/DeLoDocs/Transcripts, ~/HeyMa/var/wax.db). It never inspects the CONTENTS of the real vault:
waxd publishes notes there concurrently, so a before/after diff would false-positive.

The wax component suite (components/wax/tests/conftest.py) pins the same names; keep the two in step.
"""

import atexit
import functools
import os
import shutil
import tempfile
import types
from pathlib import Path

import pytest

# One sandbox per pytest run, shared by both suites (`pytest tests components/wax/tests`): the first conftest
# loaded creates it and removes it at exit, the second reuses it, so the pins agree.
_SANDBOX_PREFIX = "heyma-test-sandbox-"
_shared = os.environ.get("HEYMA_TEST_SANDBOX", "")
if _shared and Path(_shared).name.startswith(_SANDBOX_PREFIX) and Path(_shared).is_dir():
    SANDBOX = Path(_shared)
else:
    SANDBOX = Path(os.path.realpath(tempfile.mkdtemp(prefix=_SANDBOX_PREFIX)))
    atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)
    os.environ["HEYMA_TEST_SANDBOX"] = str(SANDBOX)
SANDBOX_ROOT = SANDBOX / "wax-root"
SANDBOX_VAULT = SANDBOX / "vault"

PINNED_ENV = {
    "WAX_ROOT": str(SANDBOX_ROOT),
    "WAX_VAULT": str(SANDBOX_VAULT),
    "TRANSCRIPTS": str(SANDBOX_VAULT),
    "TRANSCRIBE_STASH_DIR": str(SANDBOX / "stash"),
    "TRANSCRIBE_LOCK_FILE": str(SANDBOX / "transcribe.lock"),
    "TRANSCRIBE_FAST_LOCK_FILE": str(SANDBOX / "transcribe-fast.lock"),
    "TRANSCRIBE_EPHEMERAL_DIR": str(SANDBOX / "ephemeral"),
    "MC_BIN": "/nonexistent/heyma-root-tests/mc",
    "HERMES_TRANSCRIBE_BIN": "/nonexistent/heyma-root-tests/transcribe",
    "WAX_VOICES_DIR": str(SANDBOX / "voices"),     # the voice registry's notes (default ~/d/Voices)
}
os.environ.update(PINNED_ENV)
os.environ["WAX_VOICE_NOTIFY"] = "0"  # the voice-id pass never announces a new voice from a test
os.environ["WAX_VOICE_CONTEXT"] = "0"  # ... nor asks Jev about a speaker (tests that do pass a fake)
os.environ.pop("WAX_AUDIO_ROOT", None)  # legacy alias of WAX_ROOT; the shell may export the real root

# ---------------------------------------------------------------------------------------------------------
# The real runtime these tests must never reach (kept in step with components/wax/tests/conftest.py).
_REAL_HOME = Path.home()
_REAL_ROOT = _REAL_HOME / "HeyMa"
_RUNTIME_DIRS = ("var", "inbox", "stream", "dropoff", "archive", "quarantine", "recovered", "skipped")


def _real(path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


# What each of these falls back to when it is NOT set (bin/transcribe). Every default is the real thing, so a pin
# that is merely dropped is a leak as surely as one re-pointed at the real vault: the check below covers both.
UNSET_DEFAULTS = {
    "TRANSCRIBE_STASH_DIR": "~/audio/recovered",
    "TRANSCRIBE_LOCK_FILE": "~/.cache/heyma-transcribe.lock",
    "TRANSCRIBE_FAST_LOCK_FILE": "~/.cache/heyma-transcribe-fast.lock",
}


@functools.lru_cache(maxsize=1)
def real_runtime() -> dict[str, list[Path]]:
    """The real vault, ledger, runtime root and transcription slot, each spelled every way it can be reached."""
    vault = [_REAL_HOME / "d" / "Transcripts", _REAL_HOME / "code" / "DeLoDocs" / "Transcripts"]
    vault += [_real(p) for p in vault]  # the realpath of ~/d/Transcripts today; follows the symlink if it moves
    ledger = [_REAL_ROOT / "var" / "wax.db"]
    ledger += [_real(p) for p in ledger]
    root = [_REAL_ROOT, _real(_REAL_ROOT)]
    shared = [_REAL_HOME / "audio", _REAL_HOME / ".cache" / "heyma-transcribe.lock",
              _REAL_HOME / ".cache" / "heyma-transcribe-fast.lock"]
    return {"vault": vault, "ledger": ledger, "root": root, "shared": shared}


def _under(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def real_path_hit(path) -> str:
    """Why `path` is the real vault/ledger/runtime (or lies inside it); "" when it is not."""
    if path is None or str(path) == "":
        return ""
    for candidate in {Path(os.path.abspath(os.path.expanduser(str(path)))), _real(path)}:
        for kind, spellings in real_runtime().items():
            for real in spellings:
                if kind == "root":
                    # ~/HeyMa is also this checkout: only the runtime root itself and its runtime
                    # directories are off limits, not the repo's source tree.
                    hit = candidate == real or any(_under(candidate, real / d) for d in _RUNTIME_DIRS)
                    hit = hit or candidate == real / "Dictionary.yaml"
                else:
                    hit = _under(candidate, real)
                if hit:
                    return f"{candidate} is the real {kind} ({real})"
    return ""


def isolation_leaks(environ=None) -> list[str]:
    """Every way a process (or a subprocess inheriting `environ`) could still reach the real vault or ledger."""
    environ = os.environ if environ is None else environ
    found = []
    for name in ("WAX_ROOT", "WAX_VAULT", "TRANSCRIPTS"):
        if not environ.get(name):
            found.append(f"${name} is unset: a spawned bin/transcribe or bin/wax would fall back to the real default")
        elif real_path_hit(environ[name]):
            found.append(f"${name}: {real_path_hit(environ[name])}")
    for name in ("WAX_AUDIO_ROOT", "TRANSCRIBE_STASH_DIR", "TRANSCRIBE_LOCK_FILE", "TRANSCRIBE_FAST_LOCK_FILE",
                 "TRANSCRIBE_EPHEMERAL_DIR"):
        raw = environ.get(name) or ""
        value = raw or UNSET_DEFAULTS.get(name, "")
        if real_path_hit(value):
            how = f"${name}: " if raw else f"${name} is unset and falls back to {value}: "
            found.append(f"{how}{real_path_hit(value)}")
    return found


@pytest.fixture(autouse=True)
def _never_the_real_vault_or_ledger():
    """Fails the test (it does not skip it) when the process could write the real vault or the real ledger."""
    leaks = isolation_leaks()
    if leaks:
        pytest.fail("test process can reach the REAL vault/ledger:\n  " + "\n  ".join(leaks), pytrace=False)
    yield


@pytest.fixture(scope="session")
def isolation():
    """The sandbox and the guard helpers, for tests/test_isolation_guard.py (a fixture, not `import conftest`,
    because that name is ambiguous when two suites share one pytest run)."""
    return types.SimpleNamespace(
        sandbox=SANDBOX, root=SANDBOX_ROOT, vault=SANDBOX_VAULT, pinned_env=dict(PINNED_ENV),
        real_runtime=real_runtime, real_path_hit=real_path_hit, leaks=isolation_leaks,
        unset_defaults=dict(UNSET_DEFAULTS))
