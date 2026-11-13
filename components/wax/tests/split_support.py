"""Shared harness for the `wax split` tests (not a test file).

`runtime(tmp)` points every `wax.paths` directory, the ledger connection and `state`'s claim file at a
throwaway root, the way finalization_test does, so in-process tests of split/worker/capture never touch
the sandbox root the suite pins or the real ~/HeyMa. `fixture_ogg` renders (and caches per session) one
of tests/split_fixtures.py's recordings.
"""

import atexit
import contextlib
import shutil
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import split_fixtures
from wax import ledger, paths, state

SCALE = 0.1
# Thresholds scaled with the fixtures (10x shorter than a real meeting; ordinary pauses top out at 9 s).
SCALED = dict(min_quiet_s=30.0, tail_slack_s=10.0, min_active_s=20.0, pad_s=2.0)

_cache: dict[str, Path] = {}
_cache_dir = Path(tempfile.mkdtemp(prefix="heyma-split-fx-"))
atexit.register(shutil.rmtree, _cache_dir, ignore_errors=True)   # ogg fixtures are per-run scratch


def fixture_ogg(name: str) -> Path:
    if name not in _cache:
        split_fixtures.build(name, _cache_dir, scale=SCALE)
        _cache[name] = _cache_dir / f"{name}.ogg"
    return _cache[name]


def fixture_info(name: str) -> dict:
    import json
    fixture_ogg(name)
    return json.loads((_cache_dir / f"{name}.fixture.json").read_text())


def close_ledger() -> None:
    conn = getattr(ledger._local, "conn", None)
    if conn is not None:
        conn.close()
        del ledger._local.conn


@contextlib.contextmanager
def runtime(root: Path):
    """Patch wax.paths (and state's derived constants) to live under `root`; yields the root."""
    root.mkdir(parents=True, exist_ok=True)
    names = {"HEYMA": root, "AUDIO": root, "STREAM": root / "stream", "INBOX": root / "inbox",
             "DROPOFF": root / "dropoff", "ARCHIVE": root / "archive", "QUARANTINE": root / "quarantine",
             "RECOVERED": root / "recovered", "SKIPPED": root / "skipped", "VAR": root / "var",
             "LOGS": root / "var" / "logs", "LOCK": root / "var" / "waxd.lock", "SOCK": root / "var" / "waxd.sock",
             "DB": root / "var" / "wax.db", "STATE_JSON": root / "var" / "state.json", "VAULT": root / "vault"}
    with contextlib.ExitStack() as stack:
        for name, value in names.items():
            stack.enter_context(patch.object(paths, name, value))
        stack.enter_context(patch.object(state, "CLAIM_FILE", root / "var" / "inbox.claim"))
        stack.enter_context(patch.object(state, "ENABLED_FLAG", root / "var" / "pipeline.enabled"))
        for d in (names[k] for k in ('STREAM', 'INBOX', 'DROPOFF', 'ARCHIVE', 'QUARANTINE', 'RECOVERED', 'SKIPPED',
                                      'VAR', 'LOGS', 'VAULT')):
            d.mkdir(parents=True, exist_ok=True)
        close_ledger()
        try:
            yield root
        finally:
            close_ledger()


def fake_transcriber(directory: Path, body: str) -> Path:
    """An executable stand-in for bin/transcribe (WAX_TRANSCRIBE). `body` is shell; $1 is the audio."""
    script = directory / "fake-transcribe"
    script.write_text("#!/bin/bash\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def cli(root: Path, *args: str, timeout: float = 300) -> subprocess.CompletedProcess:
    """Run the real bin/wax against an isolated root (never WAX_ROOT of the suite sandbox or ~/HeyMa)."""
    wax = Path(__file__).resolve().parents[1] / "bin" / "wax"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env.update(WAX_ROOT=str(root), WAX_VAULT=str(root / "vault"), TRANSCRIPTS=str(root / "vault"))
    return subprocess.run([sys.executable, str(wax), *args], capture_output=True, text=True, env=env,
                          timeout=timeout)
