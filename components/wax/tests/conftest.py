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
"""

import os
import sys
from pathlib import Path

NO_UNIT = "/nonexistent/wax-test-suite/waxd.service"

os.environ["WAX_TRANSCRIPTS_UNIT_FILE"] = NO_UNIT
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
