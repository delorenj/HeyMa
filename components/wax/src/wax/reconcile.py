"""Rebuild the ledger from durable sources.

The ledger is a convenience, not the truth. Everything in it is derivable from
three things that survive independently of it:

  * the filesystem   — what audio exists, and where
  * S3 sidecars      — .by-content/<sha256>.json, what was backed up and verified
  * vault frontmatter— wax-item-id + the wax: block, what was transcribed/enriched

If wax.db is lost or corrupted, `wax reconcile --rebuild` reconstructs it. That
property is what lets the ledger be trusted without being precious: it is never
the only copy of anything.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

from . import archive, frontmatter, ledger, paths, sentinel


def scan_local() -> dict[str, int]:
    """Register every audio file we can see, wherever it currently lives."""
    found = 0
    for root, origin in ((paths.INBOX, "inbox"), (paths.ARCHIVE, "archive")):
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            from . import state as st
            if p.suffix.lower() not in st.MEDIA_SUFFIXES:
                continue
            if ledger.upsert_item(p, origin=origin):
                found += 1
    return {"local_files": found}


def scan_s3() -> dict[str, int]:
    """Recover backup records from the .by-content sidecar index."""
    recovered = 0
    try:
        r = subprocess.run(
            ["mc", "cat"], capture_output=True, text=True, timeout=5)  # probe mc exists
    except (OSError, subprocess.SubprocessError):
        return {"s3_sidecars": 0, "note": "mc unavailable"}

    try:
        listing = subprocess.run(
            ["mc", "ls", "--json", f"{archive.ALIAS}/{archive.BUCKET}/.by-content/"],
            capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return {"s3_sidecars": 0, "note": "listing failed"}

    conn = ledger.connect()
    for line in listing.stdout.splitlines():
        try:
            key = json.loads(line).get("key") or ""
        except json.JSONDecodeError:
            continue
        if not key.endswith(".json"):
            continue
        try:
            blob = subprocess.run(
                ["mc", "cat", f"{archive.ALIAS}/{archive.BUCKET}/.by-content/{key}"],
                capture_output=True, text=True, timeout=60)
            doc = json.loads(blob.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            continue
        item_id, s3_key = doc.get("item_id"), doc.get("s3_key")
        if not item_id or not s3_key:
            continue
        conn.execute(
            "INSERT INTO backups(item_id,s3_key,bucket,bytes,verified_at,method) "
            "VALUES(?,?,?,?,?,'rebuilt-from-sidecar') "
            "ON CONFLICT(item_id,s3_key) DO UPDATE SET bytes=excluded.bytes",
            (item_id, s3_key, archive.BUCKET, doc.get("bytes") or 0, doc.get("archived_at")),
        )
        recovered += 1
    return {"s3_sidecars": recovered}


def scan_vault() -> dict[str, int]:
    """Recover transcript + pass records from the notes themselves."""
    transcripts = passes_found = 0
    conn = ledger.connect()
    if not paths.VAULT.is_dir():
        return {"vault_transcripts": 0, "vault_passes": 0}

    for md in sorted(paths.VAULT.glob("*.md")):
        fm, _ = frontmatter.read(md)
        item_id = fm.get(frontmatter.ITEM_KEY)
        if not item_id:
            continue
        wax = fm.get(frontmatter.WAX_KEY) or {}
        conn.execute(
            "INSERT INTO transcripts(item_id,md_path,audio_duration,duration_ratio,created_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET md_path=excluded.md_path",
            (item_id, str(md), wax.get("audio_duration_s"), wax.get("duration_ratio"),
             wax.get("transcribed_at") or sentinel.utcnow()),
        )
        transcripts += 1
        for slug, rec in (wax.get("passes") or {}).items():
            if not isinstance(rec, dict):
                continue
            conn.execute(
                "INSERT INTO passes(item_id,ep_slug,state,attempt,command_id,updated_at,detail) "
                "VALUES(?,?,?,?,?,?,'rebuilt-from-frontmatter') "
                "ON CONFLICT(item_id,ep_slug) DO UPDATE SET state=excluded.state",
                (item_id, slug, rec.get("state") or "unknown", rec.get("attempt") or 1,
                 rec.get("command_id"), rec.get("at") or sentinel.utcnow()),
            )
            passes_found += 1
    return {"vault_transcripts": transcripts, "vault_passes": passes_found}


def infer_states() -> dict[str, int]:
    """Apply the cold-start rules to items with no recorded state.

    These are the user's three predicates, hardened:
      * a local non-markdown file with no backup record -> assume NOT backed up
      * an item with a verified backup but no transcript -> archived
      * an item with a transcript -> transcribed (complete if parked out of inbox)
    """
    conn = ledger.connect()
    updated = 0
    for row in conn.execute("SELECT item_id, path, state FROM items").fetchall():
        has_backup = conn.execute(
            "SELECT 1 FROM backups WHERE item_id=? LIMIT 1", (row["item_id"],)).fetchone()
        has_tx = conn.execute(
            "SELECT 1 FROM transcripts WHERE item_id=? LIMIT 1", (row["item_id"],)).fetchone()
        in_inbox = str(paths.INBOX) in (row["path"] or "")
        if has_tx and not in_inbox:
            want = "complete"
        elif has_tx:
            want = "transcribed"
        elif has_backup:
            want = "archived"
        else:
            want = "pending"
        if row["state"] != want:
            conn.execute("UPDATE items SET state=?, updated_at=? WHERE item_id=?",
                         (want, sentinel.utcnow(), row["item_id"]))
            updated += 1
    return {"states_inferred": updated}


def rebuild() -> dict[str, Any]:
    out: dict[str, Any] = {"rebuilt_at": sentinel.utcnow()}
    out.update(scan_local())
    out.update(scan_s3())
    out.update(scan_vault())
    out.update(infer_states())
    out["counts"] = ledger.counts()
    ledger.record_transition("system", None, None, "rebuilt", "reconcile",
                             json.dumps({k: v for k, v in out.items() if k != "counts"}))
    return out
