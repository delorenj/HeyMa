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

WAX-DESIGN.md:42 and :279 have promised "mc cp + ETag verify x3" and "compares
ETag+size rather than trusting exit 0" since the rewrite, but `grep -rni etag
src/` returned nothing: only the size half was ever written. The ETag is now
captured, stored in the backups row and the sidecar, and — for SINGLE-PART
objects, where this MinIO's ETag is exactly the object's MD5 (verified
2026-08-19: the 286 B sidecar 0421a08dde35-20260809-143450-rec.ogg.wax.json
carries etag 085a44747e4ab1d057df974bf48c90ef, which is its md5sum) — compared
against a locally computed MD5.

A multipart ETag is md5-of-md5s over parts the server chose and never reports,
so recomputing it means guessing mc's part size, and a wrong guess reads as
corruption on a perfectly good backup. Those verify by size and SAY so in the
recorded method. That is the common case here, not a corner: mc used 16 MiB
parts for both audio objects measured that day (63,895,126 B -> etag "...-4",
172,811,232 B -> "...-11"), so essentially every recording is multipart and only
the small sidecars get a plain MD5. Recording the server's ETag regardless costs
nothing and is the value any later cross-check has to compare against.
"""

import hashlib
import json
import logging
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from . import frontmatter, ledger, paths, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

ALIAS = "delo"
BUCKET = "recordings"
MC = "/usr/local/bin/mc"
ATTEMPTS = 3

# A single-part ETag on this bucket is the object's MD5 in lowercase hex. Any
# other shape — a "-<parts>" multipart suffix, a base64 checksum from some other
# gateway, an SSE-mangled value — is opaque to us and must NEVER be read as a
# checksum mismatch: falsely failing a good upload leaves the audio sitting in
# the inbox forever, which is strictly worse than verifying by size and saying
# that is what happened.
_MD5_ETAG = re.compile(r"[0-9a-f]{32}")


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


def _size_and_etag(o: dict[str, Any]) -> Optional[dict[str, Any]]:
    try:
        size = int(o.get("size"))
    except (TypeError, ValueError):
        return None
    # mc quotes the ETag in some code paths and not others; normalise once here
    # so every comparison downstream sees the same string.
    return {"size": size, "etag": str(o.get("etag") or "").strip('"')}


def remote_stat(key: str) -> Optional[dict[str, Any]]:
    """Size AND ETag for one exact object, or None if presence cannot be proven.

    Split out of remote_size() so verification can compare more than a byte
    count. remote_size() keeps its old signature because its callers
    (worker's park re-verify, audit) only ever cared about the size.
    """
    o = _mc_json(["stat", "--json", f"{ALIAS}/{BUCKET}/{key}"])
    if o and o.get("status") == "success":
        got = _size_and_etag(o)
        if got:
            return got

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
    return _size_and_etag(o)


def remote_size(key: str) -> Optional[int]:
    got = remote_stat(key)
    return None if got is None else got["size"]


def _md5_file(path: Path, chunk: int = 1 << 20) -> str:
    """MD5 of the local bytes, for comparison against a single-part ETag.

    Only ever called when the remote ETag CAN be an MD5, which on this bucket
    means an object mc did not split — a few-MB read, not a second full pass
    over a 950 MB recording on top of the sha256 the ledger already took.
    """
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def etag_is_multipart(etag: str) -> bool:
    """S3 marks a multipart ETag with a "-<partcount>" suffix; an MD5 never has one."""
    return "-" in etag


def verify_remote(path: Path, key: str, local: int) -> dict[str, Any]:
    """Prove the object at `key` IS these bytes. Size first, then ETag.

    Returns a verdict rather than a bool because the ledger row and the sidecar
    both want to record HOW the bytes were proven. "verified" with no method
    beside it is exactly the claim under which a 262,144 B stub stood in for a
    16.5-hour recording.
    """
    remote = remote_stat(key)
    if remote is None:
        return {"ok": False, "bytes": None, "etag": "",
                "method": "absent", "detail": "no remote object"}
    size, etag = remote["size"], remote["etag"]
    if size != local:
        return {"ok": False, "bytes": size, "etag": etag,
                "method": "size", "detail": f"remote={size} local={local}"}
    if not etag:
        return {"ok": True, "bytes": size, "etag": "", "method": "size",
                "detail": "gateway returned no ETag"}
    if etag_is_multipart(etag):
        return {"ok": True, "bytes": size, "etag": etag, "method": "size+multipart-etag",
                "detail": "multipart ETag is md5-of-md5s over server-chosen parts"}
    if not _MD5_ETAG.fullmatch(etag):
        return {"ok": True, "bytes": size, "etag": etag, "method": "size",
                "detail": "ETag is not MD5-shaped"}
    local_md5 = _md5_file(path)
    if local_md5 != etag:
        # Same length, different bytes. Size verification alone calls this a
        # good backup, which is the entire failure mode this module exists to
        # refuse; re-uploading is the correct answer.
        return {"ok": False, "bytes": size, "etag": etag, "method": "md5",
                "detail": f"ETag {etag} != local MD5 {local_md5} at matching size {size}"}
    return {"ok": True, "bytes": size, "etag": etag, "method": "size+md5", "detail": ""}


def archive(path: Path, *, item_id: Optional[str] = None) -> dict[str, Any]:
    """Upload and verify. Raises ArchiveError rather than claiming success."""
    if not path.is_file():
        raise ArchiveError(f"not a file: {path}")
    item_id = item_id or ledger.identify(path)
    sha = ledger.sha256_file(path)
    local = path.stat().st_size
    key = key_for(path, sha)

    # Idempotent: identical content already verified at this key is a no-op.
    check = verify_remote(path, key, local)
    if check["ok"]:
        _record(item_id, key, local, "already-present", check)
        # Write the sidecar here too. The early return used to skip it, so an
        # idempotent re-archive left no .by-content entry — and the rebuild then
        # could not recover that backup at all (found by destroying the ledger
        # and getting 5 of 6 backups back).
        _write_sidecar(item_id, key, sha, local, path, etag=check["etag"])
        return {"item_id": item_id, "s3_key": key, "bytes": local,
                "verified": True, "uploaded": False,
                "etag": check["etag"], "verified_by": check["method"]}

    last = ""
    for attempt in range(1, ATTEMPTS + 1):
        r = subprocess.run([MC, "cp", "--quiet", str(path), f"{ALIAS}/{BUCKET}/{key}"],
                           capture_output=True, text=True, timeout=7200)
        check = verify_remote(path, key, local)
        if check["ok"]:
            _record(item_id, key, local, f"verified-attempt-{attempt}", check)
            _write_sidecar(item_id, key, sha, local, path, etag=check["etag"])
            return {"item_id": item_id, "s3_key": key, "bytes": local,
                    "verified": True, "uploaded": True, "attempt": attempt,
                    "etag": check["etag"], "verified_by": check["method"]}
        last = (f"attempt {attempt}: {check['detail']} rc={r.returncode} "
                f"{(r.stderr or '')[-200:]}")
        log.warning("archive of %s failed verification: %s", key, last)
        time.sleep(2 * attempt)

    raise ArchiveError(f"upload could not be verified after {ATTEMPTS} attempts — {last}")


def _record(item_id: Optional[str], key: str, size: int, method: str,
            verdict: Optional[dict[str, Any]] = None) -> None:
    if not item_id:
        return
    conn = ledger.connect()
    verdict = verdict or {}
    # HOW the bytes were proven belongs beside the claim that they were, so
    # "size only, because multipart" is legible without re-reading this file.
    how = f"{method}/{verdict['method']}" if verdict.get("method") else method
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(backups)")}
    if "etag" in columns:
        conn.execute(
            "INSERT INTO backups(item_id,s3_key,bucket,bytes,verified_at,method,etag) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(item_id,s3_key) DO UPDATE SET bytes=excluded.bytes, "
            "verified_at=excluded.verified_at, method=excluded.method, etag=excluded.etag",
            (item_id, key, BUCKET, size, sentinel.utcnow(), how, verdict.get("etag") or ""),
        )
        return
    # The live var/wax.db predates the column and ledger.py owns the schema, so
    # this degrades instead of migrating: the method string still records the
    # verification method and the sidecar still carries the ETag value itself.
    conn.execute(
        "INSERT INTO backups(item_id,s3_key,bucket,bytes,verified_at,method) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(item_id,s3_key) DO UPDATE SET bytes=excluded.bytes, "
        "verified_at=excluded.verified_at, method=excluded.method",
        (item_id, key, BUCKET, size, sentinel.utcnow(), how),
    )


def _write_sidecar(item_id: Optional[str], key: str, sha: str, size: int, path: Path,
                   *, etag: str = "") -> None:
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
    if etag:
        # The server's own checksum, kept where `wax reconcile --rebuild` looks.
        # Until backups grows an `etag` column this is the only durable copy.
        doc["etag"] = etag
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
    # An un-slugged note is a FIRST-CLASS case, not an accident. Linkage no
    # longer waits on the title-slug pass returning a slug — that gate is what
    # switched off this entire subsystem for the five days the pass 404'd — so
    # the ordinary state of a freshly published note is `title-slug: <unset>`
    # and a stem that is still the raw 20260815-143450 timestamp. That is a
    # worse label than a slug and an infinitely better one than no link at all:
    # "which audio has no transcript?" has to stay answerable from S3 alone
    # while every LLM pass in the system is down. `slugged` tells a reader which
    # of the two they are looking at, so a later backfill can find these.
    slugged = bool(fm.get("title-slug"))
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
        "slugged": slugged,
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


def unlinked_transcripts(limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Backed-up items that HAVE a transcript but no sidecar projection of it.

    This is precisely the backlog the title-slug outage created: linkage hung
    off an enrichment pass returning a slug, so every item processed while that
    pass was 404ing ended up with a verified backup, a published transcript, and
    a sidecar that says nothing about either.
    """
    rows = ledger.connect().execute(
        "SELECT b.item_id AS item_id, b.bucket AS bucket, b.s3_key AS s3_key, "
        "t.md_path AS md_path FROM backups b JOIN transcripts t USING(item_id) "
        "WHERE b.verified_at IS NOT NULL ORDER BY t.created_at, b.s3_key",
    ).fetchall()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        # One `mc cat` per ITEM, not per backup row: link_transcript writes every
        # ref for an item at once, so a second key adds nothing but a round trip.
        if row["item_id"] in seen:
            continue
        seen.add(row["item_id"])
        doc = _cat_json(f"{ALIAS}/{row['bucket']}/{row['s3_key']}.wax.json") or {}
        projection = doc.get("transcript")
        if isinstance(projection, dict) and projection.get("filename"):
            continue
        out.append({"item_id": row["item_id"], "s3_key": row["s3_key"],
                    "md_path": row["md_path"]})
        if limit is not None and len(out) >= limit:
            break
    return out


def link_all_unlinked(*, dry_run: bool = False, limit: Optional[int] = None) -> dict[str, Any]:
    """Backfill the sidecar transcript projection for every item missing one.

    One item's failure never stops the sweep. A backfill that abandons the
    remaining backlog because a single object was unreachable is how the gap it
    is supposed to close stays open.
    """
    pending = unlinked_transcripts(limit=limit)
    linked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for entry in pending:
        md = Path(entry["md_path"])
        if not md.is_file():
            skipped.append({**entry, "reason": "transcript missing from the vault"})
            continue
        if dry_run:
            linked.append({**entry, "dry_run": True})
            continue
        try:
            result = link_transcript(entry["item_id"], md)
        except (ArchiveError, OSError) as exc:
            log.warning("backfill link failed for %s (%s): %s",
                        entry["item_id"], md.name, str(exc)[:300])
            failed.append({**entry, "error": str(exc)[:400]})
            continue
        log.info("backfill linked %s -> %s (%d key(s))",
                 entry["item_id"], md.name, len(result["s3_keys"]))
        linked.append({**entry, "s3_keys": result["s3_keys"],
                       "title_slug": result["transcript"]["title_slug"],
                       "slugged": result["transcript"]["slugged"]})
    log.info("link backfill: %d candidate(s), %d linked, %d skipped, %d failed",
             len(pending), len(linked), len(skipped), len(failed))
    return {"candidates": len(pending), "linked": linked,
            "skipped": skipped, "failed": failed, "dry_run": dry_run}


def is_backed_up(item_id: str) -> bool:
    row = ledger.connect().execute(
        "SELECT 1 FROM backups WHERE item_id=? AND verified_at IS NOT NULL LIMIT 1", (item_id,)
    ).fetchone()
    return row is not None


def audit(paths_to_check: list[Path]) -> list[dict[str, Any]]:
    """Report every file whose S3 copy is missing, short, or checksum-mismatched."""
    out = []
    for p in paths_to_check:
        if not p.is_file():
            continue
        sha = ledger.sha256_file(p)
        key = key_for(p, sha)
        local = p.stat().st_size
        check = verify_remote(p, key, local)
        if not check["ok"]:
            out.append({"path": str(p), "local_bytes": local,
                        "remote_bytes": check["bytes"], "s3_key": key,
                        "etag": check["etag"], "detail": check["detail"]})
    return out
