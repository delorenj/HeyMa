"""Regression guard: this suite can never reach the REAL vault or the REAL ledger.

Incident 2026-11: fixture notes (`a-first.ogg.md`, `b-second.ogg.md`, body "body", wax-item-ids that are not in
the ledger) were written through `paths.VAULT` into ~/d/Transcripts by an early test run, because nothing
pinned WAX_VAULT. tests/conftest.py now pins the vault, the runtime root and bin/transcribe's output dir to a
session sandbox before `wax` is imported, and an autouse fixture there runs the same `isolation_leaks()`
check before every test. These tests keep that machinery honest:

- the live process and a spawned subprocess resolve to the sandbox;
- the check really flags every spelling of the real vault/ledger (so it cannot rot into a no-op);
- re-pointing `paths.VAULT` / `paths.DB` at the real thing is caught.

It deliberately never inspects the CONTENTS of the real vault: waxd publishes notes there concurrently, so a
before/after diff would fail for reasons that are not this suite's.
"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from wax import dictionary, hygiene, paths

REAL_VAULT = Path.home() / "d" / "Transcripts"
REAL_DELODOCS = Path.home() / "code" / "DeLoDocs" / "Transcripts"
REAL_ROOT = Path.home() / "HeyMa"
REAL_DB = REAL_ROOT / "var" / "wax.db"


def test_process_resolves_to_the_sandbox(isolation):
    assert isolation.leaks() == []
    assert paths.VAULT == isolation.vault
    assert paths.HEYMA == isolation.root
    assert paths.DB == isolation.root / "var" / "wax.db"
    assert dictionary.default_path() == isolation.root / "Dictionary.yaml"
    for name, value in isolation.pinned_env.items():
        assert os.environ[name] == value, name
    assert "WAX_AUDIO_ROOT" not in os.environ
    for path in (paths.VAULT, paths.DB, paths.INBOX, paths.STREAM, paths.ARCHIVE, paths.SOCK, paths.STATE_JSON):
        assert isolation.sandbox in Path(os.path.realpath(path)).parents, path


def test_a_spawned_wax_process_inherits_the_sandbox(isolation):
    code = ("import os; from wax import paths; "
            "print(paths.VAULT); print(paths.DB); print(os.environ['TRANSCRIPTS'])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONPATH": str(isolation.src)}).stdout.split()
    assert out == [str(isolation.vault), str(isolation.root / "var" / "wax.db"), str(isolation.vault)]


def test_clearing_os_environ_keeps_the_module_constants_in_the_sandbox(isolation):
    """`patch.dict(os.environ, clear=True)` drops the pins, but `wax.paths` bound its constants at import."""
    with patch.dict(os.environ, {}, clear=True):
        assert paths.VAULT == isolation.vault
        assert paths.DB == isolation.root / "var" / "wax.db"
        assert dictionary.default_path() == isolation.root / "Dictionary.yaml"
        assert not isolation.real_path_hit(paths.VAULT) and not isolation.real_path_hit(paths.DB)
        # ...and the guard sees that the ENV pins are gone, which is what a spawned process would inherit.
        assert any("is unset" in leak for leak in isolation.leaks())


def test_the_check_flags_every_spelling_of_the_real_runtime(isolation):
    realpath_of_vault = Path(os.path.realpath(REAL_VAULT))
    for real in (REAL_VAULT, REAL_VAULT / "20261009-120000-rec.md", realpath_of_vault, realpath_of_vault / "x.md",
                 REAL_DELODOCS, REAL_DELODOCS / "x.md", REAL_DB, REAL_ROOT / "var", REAL_ROOT / "var" / "wax.db-wal",
                 REAL_ROOT / "inbox" / "a.ogg", REAL_ROOT / "archive", REAL_ROOT, REAL_ROOT / "Dictionary.yaml",
                 REAL_VAULT.parent / "Inbox" / "Wax" / "n.md", REAL_VAULT.parent / "Sandbox",   # hygiene sweeps
                 Path.home() / "audio" / "recovered", Path.home() / ".cache" / "heyma-transcribe.lock",
                 "~/d/Transcripts", str(REAL_VAULT) + "/../Transcripts"):
        assert isolation.real_path_hit(real), real


def test_the_check_leaves_everything_else_alone(isolation):
    """~/HeyMa is also this checkout, and the real vault's neighbours are not the vault."""
    for fine in (isolation.vault, isolation.root / "var" / "wax.db", Path("/tmp/anything.md"),
                 REAL_ROOT / "components" / "wax" / "src", REAL_ROOT / "skills" / "heyma" / "SKILL.md",
                 REAL_VAULT.parent / "Notes" / "x.md", REAL_VAULT.parent / "Transcripts-old"):
        assert isolation.real_path_hit(fine) == "", fine


def test_repointing_the_vault_or_ledger_at_the_real_thing_is_caught(isolation):
    for name, real in (("VAULT", REAL_VAULT), ("VAULT", Path(os.path.realpath(REAL_VAULT))),
                       ("VAULT", REAL_DELODOCS), ("DB", REAL_DB), ("HEYMA", REAL_ROOT), ("INBOX", REAL_ROOT / "inbox")):
        with patch.object(paths, name, real):
            leaks = isolation.leaks()
            assert any(f"wax.paths.{name}" in leak for leak in leaks), (name, real, leaks)
    assert isolation.leaks() == []


def test_an_unpinned_or_real_env_is_caught(isolation):
    pinned = dict(isolation.pinned_env)
    assert isolation.leaks(pinned) == []
    for name, real in (("WAX_VAULT", REAL_VAULT), ("TRANSCRIPTS", REAL_VAULT), ("WAX_ROOT", REAL_ROOT),
                       ("WAX_VAULT", REAL_DELODOCS)):
        leaks = isolation.leaks({**pinned, name: str(real)})
        assert any(leak.startswith(f"${name}:") for leak in leaks), (name, real, leaks)
    for name in ("WAX_VAULT", "TRANSCRIPTS", "WAX_ROOT"):
        leaks = isolation.leaks({k: v for k, v in pinned.items() if k != name})
        assert any(f"${name} is unset" in leak for leak in leaks), (name, leaks)
    hygiene = isolation.leaks({**pinned, "WAX_DOC_HYGIENE_FOLDERS": f"{REAL_ROOT}/x{os.pathsep}{REAL_VAULT}"})
    assert any(leak.startswith("$WAX_DOC_HYGIENE_FOLDERS:") for leak in hygiene)


def test_a_dropped_pin_is_a_leak_because_every_default_is_the_real_thing(isolation):
    """Dropping a pin from conftest leaves the variable UNSET, and bin/transcribe / hygiene then use the real
    default. Checking only the values that are set let exactly that through."""
    assert isolation.unset_defaults["WAX_DOC_HYGIENE_FOLDERS"] == hygiene.DEFAULT_FOLDERS  # cannot drift
    pinned = dict(isolation.pinned_env)
    for name in isolation.unset_defaults:
        assert name in pinned, f"{name} has a real default but conftest does not pin it"
        leaks = isolation.leaks({k: v for k, v in pinned.items() if k != name})
        assert any(leak.startswith(f"${name} is unset and falls back to") for leak in leaks), (name, leaks)
