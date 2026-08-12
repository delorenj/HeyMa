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
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from . import frontmatter, ledger, paths, sentinel

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
    if o and o.get("status") == "success":
        try:
            return int(o.get("size"))
        except (TypeError, ValueError):
            pass

    # Some S3 gateways permit PutObject/ListBucket but reject HeadObject.
    # `mc stat` then reports "Insufficient permissions" even though the exact
    # object exists.  Ask `mc ls` for the full object path as a verification
    # fallback; without the exact-key check a prefix match could bless the
    # wrong object.
    o = _mc_json(["ls", "--json", f"{ALIAS}/{BUCKET}/{key}"])
    if not o or o.get("status") != "success" or o.get("type") != "file":
        return None
    if o.get("key") != Path(key).name:
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
    # An idempotent re-archive must not erase enrichment written after the
    # original upload. Preserve the transcript projection and any future
    # sidecar fields, then repair the authoritative archive identity values.
    doc = _cat_json(f"{ALIAS}/{BUCKET}/{key}.wax.json") or {}
    doc.update({
        "item_id": item_id,
        "sha256": sha,
        "bytes": size,
        "orig_name": path.name,
        "s3_key": key,
        "archived_at": doc.get("archived_at") or sentinel.utcnow(),
    })
    _write_sidecar_doc(doc, strict=False)


def _sidecar_destinations(doc: dict[str, Any]) -> tuple[str, str]:
    return (
        f"{ALIAS}/{BUCKET}/{doc['s3_key']}.wax.json",
        f"{ALIAS}/{BUCKET}/.by-content/{doc['sha256']}.json",
    )


def _cat_json(target: str) -> Optional[dict[str, Any]]:
    try:
        result = subprocess.run(
            [MC, "cat", target], capture_output=True, text=True, timeout=120,
        )
        value = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_sidecar_doc(doc: dict[str, Any], *, strict: bool) -> list[str]:
    """Write both recovery projections; strict mode also reads them back."""
    paths.VAR.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix=".wax-sidecar-", dir=paths.VAR, delete=False,
    ) as handle:
        json.dump(doc, handle, indent=2, sort_keys=True)
        handle.flush()
        tmp = Path(handle.name)
    try:
        for dest in _sidecar_destinations(doc):
            try:
                result = subprocess.run(
                    [MC, "cp", "--quiet", str(tmp), dest],
                    capture_output=True, text=True, timeout=300,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append(f"{dest}: {exc}")
                continue
            if result.returncode != 0:
                failures.append(f"{dest}: {(result.stderr or 'mc cp failed')[-300:]}")
                continue
            if strict and _cat_json(dest) != doc:
                failures.append(f"{dest}: read-back mismatch")
    finally:
        tmp.unlink(missing_ok=True)
    if strict and failures:
        raise ArchiveError("transcript sidecar update failed — " + "; ".join(failures))
    return failures


def references(item_id: str) -> list[dict[str, Any]]:
    """Stable archive references for a content-identified item."""
    rows = ledger.connect().execute(
        "SELECT i.sha256,i.orig_name,i.bytes,b.s3_key,b.bucket,b.verified_at "
        "FROM items i JOIN backups b USING(item_id) WHERE i.item_id=? "
        "ORDER BY b.verified_at DESC,b.s3_key",
        (item_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _tag_audio_object(ref: dict[str, Any], transcript_name: str, title_slug: str) -> Optional[str]:
    """Mirror the link as S3 object tags. Tag failure never moves the object."""
    tags = {
        "Transcription": "Complete",
        "ItemId": str(ref.get("sha256") or "")[:16],
        "Transcript": transcript_name[:256],
        "TitleSlug": title_slug[:256],
    }
    target = f"{ALIAS}/{ref['bucket']}/{ref['s3_key']}"
    try:
        result = subprocess.run(
            [MC, "tag", "set", "--json", target, urlencode(tags)],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if result.returncode != 0:
        return (result.stderr or result.stdout or "mc tag set failed")[-300:]
    return None


def link_transcript(item_id: str, md: Path) -> dict[str, Any]:
    """Link immutable S3 audio to its mutable human-facing transcript name.

    Renaming an S3 object is a copy+delete operation. Wax instead updates the
    two small recovery sidecars and object tags, leaving the verified audio key
    and bytes untouched.
    """
    refs = references(item_id)
    if not refs:
        raise ArchiveError(f"no verified S3 backup recorded for item {item_id}")
    fm, _ = frontmatter.read(md)
    title = str(fm.get("title") or md.stem)
    title_slug = str(fm.get("title-slug") or md.stem)
    try:
        vault_path = str(md.resolve().relative_to(paths.VAULT.parent.resolve()))
    except ValueError:
        vault_path = md.name
    transcript = {
        "filename": md.name,
        "vault_path": vault_path,
        "title": title,
        "title_slug": title_slug,
        "linked_at": sentinel.utcnow(),
    }
    if fm.get("summary"):
        transcript["summary"] = fm["summary"]

    tag_failures: list[dict[str, str]] = []
    for ref in refs:
        sidecar_target = f"{ALIAS}/{ref['bucket']}/{ref['s3_key']}.wax.json"
        doc = _cat_json(sidecar_target) or {
            "item_id": item_id,
            "sha256": ref["sha256"],
            "bytes": ref["bytes"],
            "orig_name": ref["orig_name"],
            "s3_key": ref["s3_key"],
            "archived_at": ref["verified_at"],
        }
        # Always repair the identity fields from the authoritative ledger.
        doc.update({
            "item_id": item_id,
            "sha256": ref["sha256"],
            "bytes": ref["bytes"],
            "orig_name": ref["orig_name"],
            "s3_key": ref["s3_key"],
            "transcript": transcript,
        })
        _write_sidecar_doc(doc, strict=True)
        tag_error = _tag_audio_object(ref, md.name, title_slug)
        if tag_error:
            tag_failures.append({"s3_key": ref["s3_key"], "error": tag_error})
    if tag_failures:
        raise ArchiveError(
            "transcript sidecars verified but S3 object tags failed — "
            + "; ".join(f"{failure['s3_key']}: {failure['error']}" for failure in tag_failures)
        )
    return {
        "item_id": item_id,
        "transcript": transcript,
        "s3_keys": [ref["s3_key"] for ref in refs],
        "sidecars_verified": True,
        "tags_updated": True,
    }


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
