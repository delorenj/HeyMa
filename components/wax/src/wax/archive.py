"""Archive an item to S3 and prove it actually landed.

The old pipeline verified uploads with `mc stat`, which only proves an object
EXISTS. Because files were moved while still being written, the bucket ended up
holding 256 KiB stubs recorded as successful backups — the 16.5-hour
record_0016.mp3 was 950,785,004 B locally and 262,144 B in S3, and nothing
complained. A truncated backup is worse than no backup, because it stops you
looking.

So: verify by SIZE, every time, and refuse to claim success otherwise. Keys are
content-addressed (YYYY-MM-DD/<sha12>-<name>) so re-archiving identical bytes is
idempotent instead of minting mtime-derived twins.
"""

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import ledger, paths, sentinel

ALIAS = "delo"
BUCKET = "recordings"
MC = "/usr/local/bin/mc"
ATTEMPTS = 3


class ArchiveError(RuntimeError):
    pass


def _mc_json(args: list[str]) -> Optional[dict[str, Any]]:
    try:
        r = subprocess.run([MC, *args], capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def key_for(path: Path, sha256: str) -> str:
    day = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    return f"{day}/{sha256[:12]}-{path.name}"


def remote_size(key: str) -> Optional[int]:
    o = _mc_json(["stat", "--json", f"{ALIAS}/{BUCKET}/{key}"])
    if not o:
        return None
    try:
        return int(o.get("size"))
    except (TypeError, ValueError):
        return None


def archive(path: Path, *, item_id: Optional[str] = None) -> dict[str, Any]:
    """Upload and size-verify. Raises ArchiveError rather than claiming success."""
    if not path.is_file():
        raise ArchiveError(f"not a file: {path}")
    item_id = item_id or ledger.identify(path)
    sha = ledger.sha256_file(path)
    local = path.stat().st_size
    key = key_for(path, sha)

    # Idempotent: identical content already verified at this key is a no-op.
    existing = remote_size(key)
    if existing == local:
        _record(item_id, key, local, "already-present")
        # Write the sidecar here too. The early return used to skip it, so an
        # idempotent re-archive left no .by-content entry — and the rebuild then
        # could not recover that backup at all (found by destroying the ledger
        # and getting 5 of 6 backups back).
        _write_sidecar(item_id, key, sha, local, path)
        return {"item_id": item_id, "s3_key": key, "bytes": local,
                "verified": True, "uploaded": False}

    last = ""
    for attempt in range(1, ATTEMPTS + 1):
        r = subprocess.run([MC, "cp", "--quiet", str(path), f"{ALIAS}/{BUCKET}/{key}"],
                           capture_output=True, text=True, timeout=7200)
        got = remote_size(key)
        if got == local:
            _record(item_id, key, local, f"verified-attempt-{attempt}")
            _write_sidecar(item_id, key, sha, local, path)
            return {"item_id": item_id, "s3_key": key, "bytes": local,
                    "verified": True, "uploaded": True, "attempt": attempt}
        last = f"attempt {attempt}: remote={got} local={local} rc={r.returncode} {r.stderr[-200:]}"
        time.sleep(2 * attempt)

    raise ArchiveError(f"upload could not be size-verified after {ATTEMPTS} attempts — {last}")


def _record(item_id: Optional[str], key: str, size: int, method: str) -> None:
    if not item_id:
        return
    ledger.connect().execute(
        "INSERT INTO backups(item_id,s3_key,bucket,bytes,verified_at,method) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(item_id,s3_key) DO UPDATE SET bytes=excluded.bytes, "
        "verified_at=excluded.verified_at, method=excluded.method",
        (item_id, key, BUCKET, size, sentinel.utcnow(), method),
    )


def _write_sidecar(item_id: Optional[str], key: str, sha: str, size: int, path: Path) -> None:
    """A JSON sidecar next to the object, plus a by-content index entry.

    These are what make `wax reconcile --rebuild` possible: the ledger can be
    thrown away and reconstructed from S3 + the vault.
    """
    doc = {"item_id": item_id, "sha256": sha, "bytes": size, "orig_name": path.name,
           "s3_key": key, "archived_at": sentinel.utcnow()}
    blob = json.dumps(doc, indent=2, sort_keys=True)
    tmp = paths.VAR / f".sidecar-{sha[:12]}.json"
    tmp.write_text(blob)
    for dest in (f"{ALIAS}/{BUCKET}/{key}.wax.json",
                 f"{ALIAS}/{BUCKET}/.by-content/{sha}.json"):
        subprocess.run([MC, "cp", "--quiet", str(tmp), dest],
                       capture_output=True, text=True, timeout=300)
    tmp.unlink(missing_ok=True)


def is_backed_up(item_id: str) -> bool:
    row = ledger.connect().execute(
        "SELECT 1 FROM backups WHERE item_id=? AND verified_at IS NOT NULL LIMIT 1", (item_id,)
    ).fetchone()
    return row is not None


def audit(paths_to_check: list[Path]) -> list[dict[str, Any]]:
    """Report every file whose S3 copy is missing or smaller than local."""
    out = []
    for p in paths_to_check:
        if not p.is_file():
            continue
        sha = ledger.sha256_file(p)
        key = key_for(p, sha)
        local = p.stat().st_size
        got = remote_size(key)
        if got != local:
            out.append({"path": str(p), "local_bytes": local, "remote_bytes": got, "s3_key": key})
    return out
