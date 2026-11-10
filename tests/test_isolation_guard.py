"""Regression guard: the root tests can never reach the REAL vault or the REAL ledger.

Incident 2026-11: junk notes (`tmp*.md`, `proof.md`) landed in ~/d/Transcripts from ad-hoc runs that inherited
the shell's TRANSCRIPTS=$HOME/d/Transcripts; these tests spawn bin/transcribe with `dict(os.environ, ...)`, so
they share that mechanism. The pins and the autouse fixture live in tests/conftest.py; these tests keep that
machinery honest:

- the live process, and a subprocess that inherits it, resolve to the sandbox;
- the check really flags every spelling of the real vault/ledger (so it cannot rot into a no-op);
- an unpinned or real value is caught.

It never inspects the CONTENTS of the real vault: waxd publishes notes there concurrently, so a
before/after diff would fail for reasons that are not this suite's.
"""
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

REAL_VAULT = Path.home() / "d" / "Transcripts"
REAL_DELODOCS = Path.home() / "code" / "DeLoDocs" / "Transcripts"
REAL_ROOT = Path.home() / "HeyMa"
REAL_DB = REAL_ROOT / "var" / "wax.db"


def test_process_env_is_pinned_to_the_sandbox(isolation):
    assert isolation.leaks() == []
    for name, value in isolation.pinned_env.items():
        assert os.environ[name] == value, name
    assert "WAX_AUDIO_ROOT" not in os.environ
    for name in ("WAX_ROOT", "WAX_VAULT", "TRANSCRIPTS", "TRANSCRIBE_STASH_DIR", "TRANSCRIBE_LOCK_FILE",
                 "TRANSCRIBE_FAST_LOCK_FILE", "TRANSCRIBE_EPHEMERAL_DIR"):
        assert isolation.sandbox in Path(os.path.realpath(os.environ[name])).parents, name


def test_a_spawned_wrapper_inherits_the_sandbox(isolation):
    """What `dict(os.environ, ...)` hands bin/transcribe: this is the exact path the real vault used to leak by."""
    out = subprocess.run(["sh", "-c", 'printf "%s\\n%s\\n%s\\n" "$TRANSCRIPTS" "$WAX_VAULT" "$WAX_ROOT"'],
                         capture_output=True, text=True, check=True, env=dict(os.environ, EXTRA="1")).stdout.split()
    assert out == [str(isolation.vault), str(isolation.vault), str(isolation.root)]


def test_the_check_flags_every_spelling_of_the_real_runtime(isolation):
    realpath_of_vault = Path(os.path.realpath(REAL_VAULT))
    for real in (REAL_VAULT, REAL_VAULT / "20261009-120000-rec.md", realpath_of_vault, realpath_of_vault / "x.md",
                 REAL_DELODOCS, REAL_DELODOCS / "x.md", REAL_DB, REAL_ROOT / "var", REAL_ROOT / "var" / "wax.db-wal",
                 REAL_ROOT / "inbox" / "a.ogg", REAL_ROOT / "archive", REAL_ROOT, REAL_ROOT / "Dictionary.yaml",
                 Path.home() / "audio" / "recovered", Path.home() / ".cache" / "heyma-transcribe.lock",
                 "~/d/Transcripts", str(REAL_VAULT) + "/../Transcripts"):
        assert isolation.real_path_hit(real), real


def test_the_check_leaves_everything_else_alone(isolation):
    """~/HeyMa is also this checkout, and the real vault's neighbours are not the vault."""
    for fine in (isolation.vault, isolation.root / "var" / "wax.db", Path("/tmp/anything.md"),
                 REAL_ROOT / "bin" / "transcribe", REAL_ROOT / "tests", REAL_ROOT / "skills" / "heyma" / "SKILL.md",
                 REAL_VAULT.parent / "Notes" / "x.md", REAL_VAULT.parent / "Transcripts-old",
                 Path("/tmp/ephemeral_transcripts")):
        assert isolation.real_path_hit(fine) == "", fine


def test_an_unpinned_or_real_env_is_caught(isolation):
    pinned = dict(isolation.pinned_env)
    assert isolation.leaks(pinned) == []
    for name, real in (("WAX_VAULT", REAL_VAULT), ("TRANSCRIPTS", REAL_VAULT), ("TRANSCRIPTS", REAL_DELODOCS),
                       ("WAX_ROOT", REAL_ROOT), ("TRANSCRIBE_STASH_DIR", Path.home() / "audio" / "recovered"),
                       ("TRANSCRIBE_LOCK_FILE", Path.home() / ".cache" / "heyma-transcribe.lock")):
        leaks = isolation.leaks({**pinned, name: str(real)})
        assert any(leak.startswith(f"${name}:") for leak in leaks), (name, real, leaks)
    for name in ("WAX_VAULT", "TRANSCRIPTS", "WAX_ROOT"):
        leaks = isolation.leaks({k: v for k, v in pinned.items() if k != name})
        assert any(f"${name} is unset" in leak for leak in leaks), (name, leaks)


def test_the_live_process_is_caught_when_it_is_repointed(isolation):
    """The autouse fixture asks `isolation_leaks()` with the live os.environ; prove it sees a change."""
    with patch.dict(os.environ, {"TRANSCRIPTS": str(REAL_VAULT)}):
        assert any(leak.startswith("$TRANSCRIPTS:") for leak in isolation.leaks())
    with patch.dict(os.environ, {}, clear=True):
        assert any("is unset" in leak for leak in isolation.leaks())
    assert isolation.leaks() == []


def test_a_dropped_pin_is_a_leak_because_every_default_is_the_real_thing(isolation):
    """Dropping a pin from conftest leaves the variable UNSET, and bin/transcribe then uses the real default
    (~/audio/recovered, ~/.cache/heyma-transcribe*.lock). Checking only the values that are set let that through."""
    pinned = dict(isolation.pinned_env)
    for name in isolation.unset_defaults:
        assert name in pinned, f"{name} has a real default but conftest does not pin it"
        leaks = isolation.leaks({k: v for k, v in pinned.items() if k != name})
        assert any(leak.startswith(f"${name} is unset and falls back to") for leak in leaks), (name, leaks)
