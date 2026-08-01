"""Migration planner: fold the legacy dirs into the one inbox, losing nothing.

`plan()` touches NO AUDIO — it moves, copies and deletes nothing. It is not
side-effect free: hashing populates the ledger's identity cache (files_seen).
Saying "strictly read-only" would be a false safety claim, and this component
exists because a false safety claim (`mc stat` proving a backup) cost 16.5 hours.

It hashes every candidate, groups by content,
finds the name collisions that a naive `mv` would silently destroy, and reports
what each file already has (S3 backup, transcript) so the apply step can be
judged before it runs rather than explained afterwards.

The collision case is not hypothetical: inbox/clip_0057.mp3 (2,826,092 B) and
ingest/clip_0057.mp3 (9,658,988 B) are DIFFERENT recordings sharing a name. A
bare `mv` destroys one of them.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from . import ledger, paths, sentinel, state

# Sources folded into the inbox, with HOW each is taken. `dropoff` is a
# Syncthing receive-only folder, so it is COPIED and never moved — a local
# mutation there gets reverted, which is how a recording died on 2026-06-29.
# It must still be planned: excluding it hides real collisions, e.g.
# dropoff/clip_0057.mp3 (2,826,092 B) vs ingest/clip_0057.mp3 (9,658,988 B) are
# DIFFERENT recordings that both want the same name in the inbox.
SOURCE_DIRS = {"ingest": "move", "dropoff": "copy"}
ROOT_STRAYS = True


def _s3_index() -> dict[tuple[str, int], str]:
    """(basename, size) -> s3 key, from the live bucket listing.

    Keys look like YYYY-MM-DD/HHMMSS-<basename>. Matching on name+size is a
    planning heuristic only; the apply step verifies by content.
    """
    idx: dict[tuple[str, int], str] = {}
    try:
        r = subprocess.run(["mc", "ls", "--recursive", "--json", "delo/recordings/"],
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return idx
    for line in r.stdout.splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = o.get("key") or ""
        size = int(o.get("size") or 0)
        base = key.rsplit("/", 1)[-1]
        # Two key conventions exist in this bucket: the legacy mtime scheme
        # YYYY-MM-DD/HHMMSS-<name>, and the content-addressed <sha12>-<name>.
        # Normalise both or a correctly-backed-up file reads as unbacked.
        base = re.sub(r"^(\d{6}|[0-9a-f]{12})-", "", base)
        idx[(base, size)] = key
    return idx


def _transcript_index() -> set[str]:
    stems = set()
    for d in (Path.home() / "d/Transcripts", Path.home() / "d/Notes/Transcripts"):
        if d.is_dir():
            stems |= {p.name[:-3] for p in d.glob("*.md")}
    return stems


def _has_transcript(stem: str, stems: set[str]) -> Optional[str]:
    for cand in stems:
        if cand == stem or re.fullmatch(re.escape(stem) + r"-\d+", cand) \
           or re.fullmatch(r"\d{6}-" + re.escape(stem), cand) \
           or re.fullmatch(r"\d{6}-" + re.escape(stem) + r"-\d+", cand):
            return cand
    return None


def candidates() -> list[tuple[Path, str]]:
    """(file, how) where how is 'move' or 'copy'."""
    out: list[tuple[Path, str]] = []
    for d, how in SOURCE_DIRS.items():
        p = paths.AUDIO / d
        if p.is_dir():
            out += [(f, how) for f in sorted(p.iterdir())
                    if f.is_file() and not f.name.startswith(".")
                    and f.suffix.lower() in state.MEDIA_SUFFIXES]
    if ROOT_STRAYS:
        out += [(f, "move") for f in sorted(paths.AUDIO.iterdir())
                if f.is_file() and f.suffix.lower() in state.MEDIA_SUFFIXES]
    return out


def plan() -> dict[str, Any]:
    """Classify every candidate. Moves nothing, writes only the manifest."""
    s3 = _s3_index()
    tstems = _transcript_index()
    inbox_taken = {p.name for p in paths.INBOX.iterdir()} if paths.INBOX.exists() else set()

    by_sha: dict[str, list[dict[str, Any]]] = {}
    entries: list[dict[str, Any]] = []

    unreadable: list[str] = []
    for f, how in candidates():
        # identify() returns None only when stat() failed. Re-reading the same
        # path to "recover" just raises the same error and kills the whole plan.
        item_id = ledger.identify(f)
        if item_id is None:
            unreadable.append(str(f))
            continue
        try:
            st = f.stat()
        except OSError:
            unreadable.append(str(f))
            continue
        sha = _sha_for(item_id, f)
        e = {
            "path": str(f),
            "name": f.name,
            "bytes": st.st_size,
            "item_id": item_id,
            "sha256": sha,
            "s3_key": s3.get((f.name, st.st_size)),
            "transcript": _has_transcript(f.stem, tstems),
            "how": how,
            "source": Path(f).parent.name,
        }
        by_sha.setdefault(sha, []).append(e)
        entries.append(e)

    # Exact-duplicate groups: identical bytes under one or more names.
    dupes = {sha: [e["path"] for e in g] for sha, g in by_sha.items() if len(g) > 1}

    # Name collisions: same target filename, DIFFERENT content. These are the
    # ones a naive mv would destroy.
    by_name: dict[str, set[str]] = {}
    for e in entries:
        by_name.setdefault(e["name"], set()).add(e["sha256"])
    collisions = {n: sorted(s) for n, s in by_name.items() if len(s) > 1}

    # Seed the content set with what is ALREADY in the inbox. Seeding only
    # NAMES (as before) made apply() non-idempotent: after a partial run the
    # dropoff sources still exist, so a re-run saw them as first-occurrence,
    # found their preferred name taken, and copied them AGAIN under "-1" names.
    planned, used = [], set(inbox_taken)
    seen_sha: set[str] = set()
    for p_ in (paths.INBOX.iterdir() if paths.INBOX.exists() else []):
        if p_.is_file() and not p_.name.startswith("."):
            iid = ledger.identify(p_)
            if iid:
                sha_in = ledger.cached_sha(p_)
                if sha_in:
                    seen_sha.add(sha_in)

    # Within a duplicate group prefer the "move" candidate over the "copy" one:
    # an atomic rename out of a directory nobody else writes beats reading from
    # the Syncthing receive-only folder, which can change under us.
    entries.sort(key=lambda e: (0 if e["how"] == "move" else 1))
    for e in entries:
        if e["sha256"] in seen_sha:
            planned.append({**e, "action": "skip-duplicate", "dest": None})
            continue
        seen_sha.add(e["sha256"])
        dest = e["name"]
        if dest in used:
            stem, dot, ext = dest.rpartition(".")
            src_dir = Path(e["path"]).parent.name
            dest = f"{stem}__{src_dir}{dot}{ext}"
            n = 1
            while dest in used:
                dest = f"{stem}__{src_dir}-{n}{dot}{ext}"
                n += 1
        used.add(dest)
        planned.append({**e, "action": e["how"], "dest": dest})

    manifest = {
        "generated_at": sentinel.utcnow(),
        "source_dirs": SOURCE_DIRS,
        "totals": {
            "candidates": len(entries),
            "unique_content": len(by_sha),
            "exact_duplicates": sum(len(v) - 1 for v in dupes.values()),
            "name_collisions": len(collisions),
            "with_s3_backup": sum(1 for e in entries if e["s3_key"]),
            "without_s3_backup": sum(1 for e in entries if not e["s3_key"]),
            "with_transcript": sum(1 for e in entries if e["transcript"]),
            "without_transcript": sum(1 for e in entries if not e["transcript"]),
            "bytes": sum(e["bytes"] for e in entries),
        },
        "unreadable": unreadable,
        "name_collisions": collisions,
        "exact_duplicates": dupes,
        "planned": planned,
    }
    return manifest


def _sha_for(item_id: Optional[str], path: Path) -> str:
    row = ledger.connect().execute("SELECT sha256 FROM items WHERE item_id=?", (item_id,)).fetchone()
    if row:
        return row["sha256"]
    return ledger.sha256_file(path)


def write_manifest(manifest: dict[str, Any]) -> Path:
    paths.VAR.mkdir(parents=True, exist_ok=True)
    ts = manifest["generated_at"].replace(":", "").replace("-", "")[:15]
    p = paths.VAR / f"migration-{ts}.json"
    p.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return p


def apply(dry_run: bool = True) -> dict[str, Any]:
    """Execute the plan. Moves from ingest, COPIES from dropoff, deletes nothing.

    Re-plans from scratch rather than trusting a stored manifest: the disk may
    have changed since the plan was written, and acting on a stale picture of
    100 irreplaceable files is exactly the class of mistake this component
    exists to prevent. Because plan() now dedupes against the inbox's CONTENT,
    re-running after a partial failure is safe and idempotent.
    """
    import shutil
    from . import rename

    m = plan()
    results: list[dict[str, Any]] = []
    moved = copied = skipped = failed = 0

    journal = None
    if not dry_run:
        jp = paths.VAR / f"migration-applied-{sentinel.utcnow().replace(':','').replace('-','')[:15]}.jsonl"
        journal = open(jp, "a", buffering=1)   # line-buffered: survives a hard exit

    def note(rec: dict[str, Any]) -> None:
        results.append(rec)
        if journal:
            journal.write(json.dumps(rec, sort_keys=True) + "\n")

    try:
        for e in m["planned"]:
            src = Path(e["path"])
            action, dest_name = e["action"], e["dest"]

            if action == "skip-duplicate":
                note({**e, "result": "skipped", "reason": "identical content already placed"})
                skipped += 1
                continue
            if not src.is_file():
                note({**e, "result": "vanished"})
                failed += 1
                continue

            dest = paths.INBOX / dest_name
            if dry_run:
                note({**e, "result": f"would-{action}", "dest_path": str(dest)})
                continue

            if action == "move":
                try:
                    final = rename.move_noclobber(src, dest)
                    moved += 1
                    note({**e, "result": "move", "dest_path": str(final), "verified": "atomic-rename"})
                except (OSError, rename.RenameCollision) as exc:
                    note({**e, "result": "failed", "error": str(exc)})
                    failed += 1
                continue

            # dropoff is Syncthing receive-only: COPY out, never move. A local
            # mutation there gets reverted, which destroyed a recording on
            # 2026-06-29. Stage, verify by CONTENT, then rename — so a partial
            # or mutated copy can never appear in the inbox as a finished item.
            staging = paths.INBOX / f".staging-{dest_name}"
            try:
                shutil.copy2(src, staging)
                got, want = staging.stat().st_size, int(e["bytes"])
                if got != want:
                    raise OSError(f"short copy: {got} != {want} bytes")
                got_sha = ledger.sha256_file(staging)
                if got_sha != e["sha256"]:
                    raise OSError(f"content mismatch: {got_sha[:16]} != {e['sha256'][:16]} "
                                  "(source changed mid-copy?)")
                final = rename.move_noclobber(staging, dest)
                copied += 1
                note({**e, "result": "copy", "dest_path": str(final), "verified": "sha256"})
            except (OSError, rename.RenameCollision) as exc:
                # NEVER leave an invisible orphan: a dot-prefixed file in the
                # inbox is skipped by inbox_items() and scan_local() alike, so
                # it would consume disk that no tool reports. Promote it to a
                # visible quarantine path instead.
                preserved = None
                if staging.exists():
                    try:
                        qdir = paths.RECOVERED / "migration-staging"
                        qdir.mkdir(parents=True, exist_ok=True)
                        preserved = str(rename.move_noclobber(staging, qdir / dest_name))
                    except (OSError, rename.RenameCollision):
                        preserved = f"left at {staging}"
                note({**e, "result": "failed", "error": str(exc),
                      **({"staging_preserved": preserved} if preserved else {})})
                failed += 1
    finally:
        if journal:
            journal.close()

    out = {
        "dry_run": dry_run,
        "at": sentinel.utcnow(),
        "moved": moved, "copied": copied, "skipped": skipped, "failed": failed,
        "total": len(m["planned"]),
        "unreadable": m.get("unreadable", []),
        "results": results,
    }
    if not dry_run:
        out["journal"] = str(jp)
    return out
