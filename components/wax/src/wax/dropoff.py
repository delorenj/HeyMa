"""Copy accepted Syncthing arrivals into Wax's queue; never mutate the feed."""

import json
import logging
import os
from pathlib import Path
import shutil
import uuid

from . import ledger, paths, rename, state

log = logging.getLogger("wax.dropoff")


def copy_received() -> list[Path]:
    """Accept only filenames committed by the Syncthing audio policy.

    Its exclusions freeze the received version. Source bytes are preserved;
    completed/failed/skipped ledger items are never requeued by this importer.
    """
    policy = paths.VAR / "syncthing-audio" / "received.json"
    if not policy.is_file() or not paths.DROPOFF.is_dir():
        return []
    accepted = set(json.loads(policy.read_text()))
    conn = ledger.connect()
    imported = []
    for source in sorted(paths.DROPOFF.iterdir()):
        if source.name not in accepted or source.name.startswith(".") \
                or source.suffix.lower() not in state.MEDIA_SUFFIXES \
                or not source.is_file() or source.is_symlink():
            continue
        try:
            item_id = ledger.identify(source)
            if item_id is None or conn.execute(
                    "SELECT 1 FROM items WHERE item_id=?", (item_id,)).fetchone():
                continue
            digest = ledger.cached_sha(source)
            destination = paths.INBOX / source.name
            if destination.exists() and ledger.identify(destination) != item_id:
                destination = paths.INBOX / f"{source.stem}--{item_id}{source.suffix}"
            if not destination.exists() or ledger.identify(destination) != item_id:
                staging_dir = paths.INBOX / ".staging"
                staging_dir.mkdir(parents=True, exist_ok=True)
                staging = staging_dir / f"dropoff-{uuid.uuid4().hex}.part"
                with source.open("rb") as src, staging.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
                if ledger.sha256_file(staging) != digest:
                    raise RuntimeError(f"source changed during copy; preserved partial at {staging}")
                destination = rename.move_noclobber(staging, destination)
            ledger.upsert_item(destination, origin="dropoff")
            conn.execute("UPDATE items SET orig_name=? WHERE item_id=?", (source.name, item_id))
            imported.append(destination)
            log.info("copied received recording %s into inbox (%s)", source.name, item_id)
        except (OSError, RuntimeError):
            log.exception("could not copy received recording %s; source preserved", source)
    return imported
