"""Suite-wide isolation from the REAL `transcriptions` store.

Every `passes.run` and `transcribe_adapter.transcribe` now captures a snapshot
for the versioned store (docs/TRANSCRIPTIONS-STORE.md D.3), and the store's
config falls back to the INSTALLED waxd unit, which carries the production DSN.
Under /usr/bin/python3 (it has psycopg) a fixture note could then be projected
into the real database, whose versions are append-only and cannot be removed.

So, for the whole session and for every `bin/wax` subprocess a test spawns:
- the installed-unit fallback points at a path that does not exist, and
- no DSN or password inherited from the shell survives.

Tests that exercise the store set their own DSN (tests/transcriptions_pg_test.py,
against a disposable container) and patch `config.UNIT_PATH` themselves.

The same discipline now covers the REAL vault and the REAL ledger (incident 2026-11: fixture notes
`a-first.ogg.md` / `b-second.ogg.md` landed in ~/d/Transcripts; tmp*.md and proof.md came from ad-hoc
bin/transcribe runs that inherited the shell's TRANSCRIPTS). Before `wax` is imported, one
session sandbox is created in the system temp dir and these are pinned to it, so every in-process path
(`wax.paths.*`) and every `bin/wax` / `bin/transcribe` subprocess that inherits `os.environ` resolves there:

    WAX_ROOT, WAX_VAULT, TRANSCRIPTS, WAX_DICTIONARY, WAX_DOC_HYGIENE_FOLDERS,
    TRANSCRIBE_STASH_DIR, TRANSCRIBE_LOCK_FILE, TRANSCRIBE_FAST_LOCK_FILE.

A test that builds a MINIMAL subprocess env (`env={"WAX_ROOT": ..., "PATH": ...}`) does not inherit them and
must pass its own WAX_VAULT. `tests/isolation_guard_test.py` is the regression guard, and the autouse fixture
below runs the same check before every test: it fails (never skips) if the effective vault or ledger of this
process is the real one (~/d/Transcripts, its realpath ~/code/DeLoDocs/Transcripts, ~/HeyMa/var/wax.db).
"""

import atexit
import functools
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest

NO_UNIT = "/nonexistent/wax-test-suite/waxd.service"

# ---------------------------------------------------------------------------------------------------------
# The sandbox runtime. Nothing is created inside it eagerly: a test that needs a directory makes its own, and
# one that silently relied on the real one now fails on a missing path instead of writing to the real vault.
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
    "WAX_ROOT": str(SANDBOX_ROOT),                 # ledger (var/wax.db), inbox/, stream/, archive/, Dictionary.yaml
    "WAX_VAULT": str(SANDBOX_VAULT),               # wax.paths.VAULT
    "TRANSCRIPTS": str(SANDBOX_VAULT),             # bin/transcribe's output dir (the shell exports the real vault)
    "WAX_DICTIONARY": str(SANDBOX_ROOT / "Dictionary.yaml"),
    "WAX_DOC_HYGIENE_FOLDERS": str(SANDBOX / "hygiene"),   # default is ~/d/Inbox:~/d/Sandbox, which it rewrites
    "TRANSCRIBE_STASH_DIR": str(SANDBOX / "stash"),
    "TRANSCRIBE_LOCK_FILE": str(SANDBOX / "transcribe.lock"),
    "TRANSCRIBE_FAST_LOCK_FILE": str(SANDBOX / "transcribe-fast.lock"),
}
os.environ.update(PINNED_ENV)
os.environ.pop("WAX_AUDIO_ROOT", None)  # legacy alias of WAX_ROOT; the shell may export the real root

os.environ["WAX_TRANSCRIPTS_UNIT_FILE"] = NO_UNIT
# The harvest-link backstop (sync, tick) scans pilot's real output directory by default; a test that
# wants it sets its own directory.
os.environ["WAX_HARVEST_DIR"] = "off"
for _name in list(os.environ):
    if _name.startswith("WAX_TRANSCRIPTS_") and ("DSN" in _name or "PASSWORD" in _name):
        os.environ.pop(_name, None)

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wax.transcriptions import config as _transcriptions_config  # noqa: E402

# A test that clears os.environ (patch.dict(..., clear=True)) drops the override above; the module
# default must not be the real unit either.
_transcriptions_config.UNIT_PATH = Path(NO_UNIT)

from wax import paths as _paths  # noqa: E402  (imported AFTER the pins above, so its constants bind to them)

# Same reason as UNIT_PATH: wax.paths binds VAULT/HEYMA/DB... at import, from os.environ as it was THEN, so a
# test that clears os.environ keeps the sandbox. If something imported wax.paths before this file ran, the
# constants are already the real ones: refuse to run rather than test against them.
if Path(_paths.VAULT) != SANDBOX_VAULT or Path(_paths.HEYMA) != SANDBOX_ROOT:
    raise pytest.UsageError(
        f"wax.paths was imported before tests/conftest.py pinned the sandbox (VAULT={_paths.VAULT}, "
        f"HEYMA={_paths.HEYMA}); refusing to run a suite that could write the real vault or ledger")


# ---------------------------------------------------------------------------------------------------------
# The real runtime this suite must never reach (kept in step with tests/conftest.py at the repo root).
_REAL_HOME = Path.home()
_REAL_ROOT = _REAL_HOME / "HeyMa"
_RUNTIME_DIRS = ("var", "inbox", "stream", "dropoff", "archive", "quarantine", "recovered", "skipped")


def _real(path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


# What each of these falls back to when it is NOT set. Every default is the real thing, so a pin that is merely
# dropped is a leak as surely as one re-pointed at the real vault: the check below covers both. (The hygiene
# default is `wax.hygiene.DEFAULT_FOLDERS`; tests/isolation_guard_test.py asserts the two stay equal.)
UNSET_DEFAULTS = {
    "WAX_DOC_HYGIENE_FOLDERS": "~/d/Inbox:~/d/Sandbox",
    "TRANSCRIBE_STASH_DIR": "~/audio/recovered",
    "TRANSCRIBE_LOCK_FILE": "~/.cache/heyma-transcribe.lock",
    "TRANSCRIBE_FAST_LOCK_FILE": "~/.cache/heyma-transcribe-fast.lock",
}


@functools.lru_cache(maxsize=1)
def real_runtime() -> dict[str, list[Path]]:
    """The real vault, ledger, runtime root and transcription slot, each spelled every way it can be reached."""
    vault = [_REAL_HOME / "d" / "Transcripts", _REAL_HOME / "code" / "DeLoDocs" / "Transcripts"]
    # The hygiene sweeps (WAX_DOC_HYGIENE_FOLDERS) rewrite and trash notes in these two vault folders.
    vault += [_REAL_HOME / "d" / "Inbox", _REAL_HOME / "d" / "Sandbox"]
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
    """Every way this process could still reach the real vault or ledger; empty when isolated."""
    from wax import dictionary

    environ = os.environ if environ is None else environ
    found = []
    for name in ("WAX_ROOT", "WAX_VAULT", "TRANSCRIPTS"):
        if not environ.get(name):
            found.append(f"${name} is unset: a spawned bin/wax or bin/transcribe would fall back to the real default")
        elif real_path_hit(environ[name]):
            found.append(f"${name}: {real_path_hit(environ[name])}")
    for name in ("WAX_AUDIO_ROOT", "WAX_DICTIONARY", "WAX_DOC_HYGIENE_FOLDERS", "TRANSCRIBE_STASH_DIR",
                 "TRANSCRIBE_LOCK_FILE", "TRANSCRIBE_FAST_LOCK_FILE"):
        raw = environ.get(name) or ""
        for part in (raw or UNSET_DEFAULTS.get(name, "")).split(os.pathsep):
            if real_path_hit(part):
                how = f"${name}: " if raw else f"${name} is unset and falls back to {part}: "
                found.append(f"{how}{real_path_hit(part)}")
    effective = {name: getattr(_paths, name) for name in (
        "HEYMA", "AUDIO", "STREAM", "INBOX", "DROPOFF", "ARCHIVE", "QUARANTINE", "RECOVERED", "SKIPPED",
        "VAR", "LOGS", "LOCK", "SOCK", "DB", "STATE_JSON", "VAULT")}
    effective["dictionary.default_path()"] = dictionary.default_path()
    for name, value in effective.items():
        if real_path_hit(value):
            found.append(f"wax.paths.{name}: {real_path_hit(value)}")
    return found


@pytest.fixture(autouse=True)
def _never_the_real_vault_or_ledger():
    """Fails the test (it does not skip it) when the process could write the real vault or the real ledger.

    Runs before every test, so a test that patches `paths.VAULT`/`paths.DB` back to the real thing, or a
    future edit that drops a pin above, is caught on the spot. It never looks at what is IN the real vault:
    waxd publishes notes there while the suite runs, so a before/after diff would false-positive.
    """
    leaks = isolation_leaks()
    if leaks:
        pytest.fail("test process can reach the REAL vault/ledger:\n  " + "\n  ".join(leaks), pytrace=False)
    yield


@pytest.fixture(scope="session")
def isolation():
    """The sandbox and the guard helpers, for tests/isolation_guard_test.py (a fixture, not `import conftest`,
    because that name is ambiguous when two suites share one pytest run)."""
    return types.SimpleNamespace(
        sandbox=SANDBOX, root=SANDBOX_ROOT, vault=SANDBOX_VAULT, pinned_env=dict(PINNED_ENV),
        real_runtime=real_runtime, real_path_hit=real_path_hit, leaks=isolation_leaks, src=Path(_SRC),
        unset_defaults=dict(UNSET_DEFAULTS))
