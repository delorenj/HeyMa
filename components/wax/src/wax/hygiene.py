"""Document hygiene: keep the vault's inbox folders free of dead notes.

Obsidian creates an empty ``Untitled.md`` every time someone presses "new
note" and walks away. Two waxd jobs clean that up, and a third gives the
survivors a name:

- the **empty-note sweep** (every ``WAX_EMPTY_DOC_SWEEP_INTERVAL_S``) moves
  empty notes into the vault's Obsidian trash and writes off the ledger rows of
  files that vanished;
- the **nightly untitled sweep** (once per local date, from
  ``WAX_UNTITLED_SWEEP_HOUR``) trashes empty ``Untitled*.md`` notes and runs
  the ``untitled-standard`` pipeline (summarize, then title-slug) over the rest,
  renaming each note to its generated ``title-slug``.

These jobs move the user's own notes, so every safety rule lives in ONE choke
point that both jobs, the CLI and the dry run all go through (``inspect_note`` and
``trash_note`` / ``rename_note``), not in the callers:

- only ``*.md`` files in the configured folders (``WAX_DOC_HYGIENE_FOLDERS``),
  each of which must sit inside an Obsidian vault (``.obsidian/`` above it);
- nothing under a dot-directory (``.trash``, ``.obsidian``), no dot-files (Wax's
  own ``.wax-ep-*`` staging among them), and no symlink is ever followed or
  moved: the file is opened ``O_NOFOLLOW`` and its real parent must stay inside
  the swept folder;
- the file's mtime must be older than ``WAX_DOC_HYGIENE_MIN_AGE_S`` (default
  300): a note somebody is typing into is not abandoned;
- "empty" is strict: a whitespace-only body AND no real frontmatter value
  outside a short auto-generated key set. Frontmatter that does not parse is
  NOT empty, nor is anything that cannot be read as UTF-8 or is large;
- emptiness and age are checked again, under the document's run lock, right
  before the move, and the moved file is verified afterwards (a write that
  slipped in between puts the note back);
- a note a live pipeline run owns (its run lock is held) is skipped;
- "delete" is a move to ``<vault>/.trash/<path relative to the vault>`` with
  ``rename.move_noclobber``: nothing in the trash is overwritten and nothing is
  ever unlinked.

The ``documents`` ledger follows the files: a trashed note's rows become
``trashed``, a vanished one's ``gone`` (``ledger.passes_failed`` ignores
failures behind either), and a renamed note's rows are re-pointed at the new
path so a binding never mistakes it for a new arrival.
"""

import errno
import json
import logging
import os
import re
import stat
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, NamedTuple, Optional

import yaml

from . import documents, frontmatter, ledger, paths, pipelines, rename, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])


def _env_number(name: str, default: float, cast=float):
    """A numeric knob; a malformed value falls back rather than keep waxd down."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


DEFAULT_FOLDERS = "~/d/Inbox:~/d/Sandbox"
DEFAULT_MIN_AGE_S = 300.0
# Interval job cadence (the worker reads this once; `wax documents sweep-empty`
# ignores it).
EMPTY_SWEEP_INTERVAL_S = _env_number("WAX_EMPTY_DOC_SWEEP_INTERVAL_S", 300.0)
# Nightly job: due from this local hour, once per local date.
UNTITLED_SWEEP_HOUR = min(23, max(0, int(_env_number("WAX_UNTITLED_SWEEP_HOUR", 3, int))))
# A run is serial with the inbox drain, so bound how many LLM-backed pipeline
# runs one night may start; the rest wait for the retry below.
UNTITLED_MAX_PER_RUN = max(1, int(_env_number("WAX_UNTITLED_SWEEP_MAX", 50, int)))
# After an incomplete nightly run (pipeline not compiled, folder misconfigured,
# budget spent) the worker tries again no sooner than this.
UNTITLED_RETRY_S = 3600.0

UNTITLED_PIPELINE = "untitled-standard"
UNTITLED_RE = re.compile(r"^untitled(?:[ _-]?\d+)?$", re.IGNORECASE)
# Frontmatter keys that exist without anyone having written anything: a note
# carrying only these is still empty. Anything else with a value is content.
AUTO_KEYS = frozenset({
    "created", "modified", "updated", "date", "created_at", "updated_at", "id",
    "aliases", "tags", "cssclasses", "wax", "wax-item-id"})
# A genuinely empty note is tiny. Past this size it is never read as "empty".
MAX_EMPTY_BYTES = 1 << 20
TRASH_DIR = ".trash"
# Wax's own staging files (passes.py) start with this and are dot-files anyway;
# named so the exclusion is explicit rather than incidental.
STAGING_PREFIX = ".wax-ep-"

META_EMPTY = "hygiene_empty_sweep"
META_UNTITLED = "hygiene_untitled_sweep"
META_UNTITLED_DATE = "untitled_sweep_last_date"
META_FIRST_RUN = "hygiene_first_run_at"
# Rows seen missing by the previous sweep; a row is only written off when it is
# missing on two consecutive sweeps.
META_VANISHED_PENDING = "hygiene_vanished_pending"


class HygieneError(RuntimeError):
    """A refusal or fault that must not be swallowed as 'nothing to do'."""


class Folder(NamedTuple):
    root: Path    # real path of a swept folder
    vault: Path   # real path of the Obsidian vault that contains it


class Resolved(NamedTuple):
    folders: list
    missing: list
    errors: list


class Inspection(NamedTuple):
    verdict: str                      # ok | excluded | symlink | escape | not_file | young | vanished
    kind: str                         # empty | content | unparseable | unreadable (only when ok)
    stat: Optional[os.stat_result]
    detail: str = ""


# --------------------------------------------------------------- seams -----

def _now_epoch() -> float:
    return time.time()


def _now_local() -> datetime:
    """Local wall clock, naive. The one seam tests patch for the nightly gate."""
    return datetime.now()


def min_age_s() -> float:
    return max(0.0, _env_number("WAX_DOC_HYGIENE_MIN_AGE_S", DEFAULT_MIN_AGE_S))


def runtime_ledger_is_live() -> bool:
    """Whether the ledger in use belongs to the runtime root's own ``var/``.

    The worker acts on a real vault, so it refuses to run against a ledger that
    is not the runtime root's (a scratch ledger under a temp dir in a test, a
    copy somewhere else): a job that moves notes must never be driven by state
    that is not the daemon's.
    """
    try:
        return Path(paths.DB).resolve().is_relative_to(Path(paths.AUDIO).resolve())
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------- folders -----

def vault_root_for(path: Path) -> Optional[Path]:
    """Nearest ancestor (or the folder itself) holding an ``.obsidian/`` dir."""
    here = Path(os.path.realpath(path))
    for candidate in (here, *here.parents):
        if (candidate / ".obsidian").is_dir():
            return candidate
    return None


def resolve_folders(spec: Optional[str] = None) -> Resolved:
    """The swept folders from ``WAX_DOC_HYGIENE_FOLDERS`` (os.pathsep-separated).

    Each is expanded and resolved to its real path. A folder that is not inside
    a vault, IS the vault root, or sits inside a dot-directory of its vault is
    an ERROR and is never swept; a configured folder that does not exist yet
    (Obsidian creates ``Sandbox/`` on first use) is merely ``missing``.
    """
    spec = spec if spec is not None else os.environ.get("WAX_DOC_HYGIENE_FOLDERS") or DEFAULT_FOLDERS
    folders: list[Folder] = []
    missing: list[str] = []
    errors: list[str] = []
    seen: set[Path] = set()
    for raw in spec.split(os.pathsep):
        raw = raw.strip()
        if not raw:
            continue
        root = Path(os.path.realpath(os.path.expanduser(raw)))
        if root in seen:
            continue
        seen.add(root)
        vault = vault_root_for(root)
        if vault is None:
            errors.append(f"{root}: no Obsidian vault (.obsidian/) above this folder; "
                          "refusing to sweep it")
            continue
        if root == vault:
            errors.append(f"{root}: is the vault root itself; refusing to sweep a whole vault")
            continue
        if any(part.startswith(".") for part in root.relative_to(vault).parts):
            errors.append(f"{root}: sits inside a dot-directory of its vault; refusing")
            continue
        if not root.is_dir():
            missing.append(str(root))
            continue
        folders.append(Folder(root, vault))
    return Resolved(folders, missing, errors)


def iter_candidates(folder: Folder, *, untitled_only: bool = False) -> Iterator[Path]:
    """``*.md`` files under the folder: no dot-dirs, dot-files or symlinks."""
    for dirpath, dirnames, filenames in os.walk(folder.root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".")
                             and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            full = Path(dirpath, name)
            if os.path.islink(full):
                continue
            if untitled_only and not UNTITLED_RE.match(full.stem):
                continue
            yield full


# --------------------------------------------------------- emptiness -------

def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, frozenset)):
        return all(_blank(v) for v in value)
    if isinstance(value, dict):
        return all(_blank(v) for v in value.values())
    return False


def classify_text(text: str) -> str:
    """``empty``, ``content`` or ``unparseable`` for a note's full text.

    Conservative on every doubt: anything but provable emptiness is content.
    """
    if not text.replace("﻿", "").strip():
        return "empty"
    if not text.startswith(frontmatter.DELIM):
        return "content"
    lines = text.split("\n")
    if lines[0].strip() != frontmatter.DELIM:
        return "content"
    end = next((i for i, line in enumerate(lines[1:], start=1)
                if line.strip() == frontmatter.DELIM), None)
    if end is None:
        return "content"
    try:
        fm = yaml.safe_load("\n".join(lines[1:end]))
    except Exception:  # noqa: BLE001 - any parse fault means "do not touch"
        return "unparseable"
    if fm is None:
        fm = {}
    if not isinstance(fm, dict):
        return "unparseable"
    if "\n".join(lines[end + 1:]).strip():
        return "content"
    for key, value in fm.items():
        if key in AUTO_KEYS:
            continue
        if not _blank(value):
            return "content"
    return "empty"


def static_verdict(path: Path, folder: Folder) -> Optional[Inspection]:
    """The path-only rules: a refusal, or None when the path is acceptable."""
    try:
        rel = path.relative_to(folder.root)
    except ValueError:
        return Inspection("escape", "", None, f"{path} is outside {folder.root}")
    if (path.name.startswith(".") or path.suffix != ".md"
            or any(part.startswith(".") for part in rel.parts[:-1])):
        return Inspection("excluded", "", None, "not a plain *.md outside dot-directories")
    if os.path.islink(path):
        return Inspection("symlink", "", None, "symlinks are never followed")
    real_parent = os.path.realpath(path.parent)
    if real_parent != str(folder.root) and not real_parent.startswith(str(folder.root) + os.sep):
        return Inspection("escape", "", None, f"{path.parent} resolves outside {folder.root}")
    return None


def inspect_note(path: Path, folder: Folder, *, min_age: float, now: float) -> Inspection:
    """Everything a hygiene action must establish about one file, in one place.

    Reads through a single ``O_NOFOLLOW`` descriptor so the stat, the age and
    the bytes judged are those of the same inode.
    """
    refused = static_verdict(path, folder)
    if refused is not None:
        return refused
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return Inspection("vanished", "", None)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return Inspection("symlink", "", None, "symlinks are never followed")
        return Inspection("excluded", "", None, f"cannot open: {exc}")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return Inspection("not_file", "", st)
        if now - st.st_mtime <= min_age:
            return Inspection("young", "", st, f"modified {max(0, int(now - st.st_mtime))}s ago")
        if st.st_size > MAX_EMPTY_BYTES:
            return Inspection("ok", "content", st, "too large to be empty")
        data = b""
        while len(data) <= st.st_size:
            block = os.read(fd, st.st_size - len(data) + 1)
            if not block:
                break
            data += block
    except OSError as exc:
        return Inspection("ok", "unreadable", None, str(exc))
    finally:
        os.close(fd)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return Inspection("ok", "unreadable", st, "not valid UTF-8")
    return Inspection("ok", classify_text(text), st)


# ------------------------------------------------------------- trash -------

def trash_destination(path: Path, folder: Folder) -> Path:
    """``<vault>/.trash/<path relative to the vault>``."""
    return folder.vault / TRASH_DIR / os.path.relpath(path, folder.vault)


def _move_to_trash(path: Path, folder: Folder, before: os.stat_result) -> tuple[Path, bool]:
    """The one place a note leaves the vault proper. Returns (final path, restored).

    Private on purpose: callers reach it only through ``trash_note``, which has
    run every gate under the document's run lock.
    """
    trash_root = folder.vault / TRASH_DIR
    if trash_root.is_symlink():
        raise HygieneError(f"{trash_root} is a symlink; refusing to move notes through it")
    dst = trash_destination(path, folder)
    dst.parent.mkdir(parents=True, exist_ok=True)
    real_vault = os.path.realpath(folder.vault)
    real_dst_parent = os.path.realpath(dst.parent)
    if real_dst_parent != real_vault and not real_dst_parent.startswith(real_vault + os.sep):
        raise HygieneError(f"trash destination {dst.parent} resolves outside the vault")
    used = rename.move_noclobber(path, dst)
    after = os.lstat(used)
    same = ((after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns)
            == (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns))
    if same:
        return used, False
    # Something wrote to the note between the last check and the rename. It is
    # not abandoned after all: put it back (never over a file now at the path).
    log.warning("hygiene: %s changed while being trashed; restoring it", path)
    restored = rename.move_noclobber(used, path)
    return restored, True


def _row_keys(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(r["binding_id"], r["doc_id"]) for r in rows}


def trash_note(path: Path, folder: Folder, *, min_age: Optional[float] = None,
               now: Optional[float] = None, dry_run: bool = False) -> dict[str, Any]:
    """Trash one note if, and only if, every safety rule holds. Never raises
    for a refusal: the outcome says why nothing happened."""
    min_age = min_age_s() if min_age is None else min_age
    first = inspect_note(path, folder, min_age=min_age,
                    now=_now_epoch() if now is None else now)
    if first.verdict != "ok":
        return {"path": str(path), "outcome": first.verdict, "detail": first.detail}
    if first.kind != "empty":
        return {"path": str(path), "outcome": "not_empty" if first.kind == "content"
                else first.kind, "detail": first.detail}
    rows = documents.rows_for_path(path)
    if dry_run:
        if any(documents.run_lock_held(r["binding_id"], r["doc_id"]) for r in rows):
            return {"path": str(path), "outcome": "locked"}
        return {"path": str(path), "outcome": "would_trash",
                "to": str(trash_destination(path, folder))}
    with documents.hold_run_locks(rows) as held:
        if not held:
            return {"path": str(path), "outcome": "locked"}
        # Re-check immediately before the move, under the locks: a pipeline run
        # or a keystroke since the first look changes the answer.
        second = inspect_note(path, folder, min_age=min_age, now=_now_epoch())
        if second.verdict != "ok" or second.kind != "empty":
            return {"path": str(path), "outcome": "changed",
                    "detail": second.verdict if second.verdict != "ok" else second.kind}
        if _row_keys(documents.rows_for_path(path)) != _row_keys(rows):
            return {"path": str(path), "outcome": "changed", "detail": "ledger rows changed"}
        used, restored = _move_to_trash(path, folder, second.stat)
        if restored:
            return {"path": str(path), "outcome": "restored", "to": str(used)}
        for binding_id, doc_id in sorted(_row_keys(rows)):
            documents.mark_state(binding_id, doc_id, "trashed")
        log.info("hygiene: trashed empty note %s -> %s", path, used)
        return {"path": str(path), "outcome": "trashed", "to": str(used)}


# ------------------------------------------------------------ summaries ----

def _summary(kind: str, *, started: float, counts: dict[str, int],
             errors: list[str], resolved: Resolved, dry_run: bool,
             **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind, "last_run_at": sentinel.utcnow(),
        "duration_s": round(time.time() - started, 3),
        "interval_s": EMPTY_SWEEP_INTERVAL_S if kind == "empty" else None,
        "dry_run": dry_run, "counts": dict(sorted(counts.items())),
        "last_error": ("; ".join(errors[:3])[:500] if errors else None),
        "folders": [str(f.root) for f in resolved.folders],
        "missing_folders": resolved.missing, **extra,
    }


def _record(kind: str, summary: dict[str, Any]) -> None:
    """Persist a run summary where `wax doctor` (another process) can read it."""
    key = META_EMPTY if kind == "empty" else META_UNTITLED
    ledger.meta_set(key, json.dumps(summary, sort_keys=True, default=str))
    if ledger.meta_get(META_FIRST_RUN) is None:
        ledger.meta_set(META_FIRST_RUN, sentinel.utcnow())


def record_failure(kind: str, exc: BaseException) -> None:
    """Best effort: a job that crashed outright still leaves its last_error."""
    try:
        _record(kind, {"kind": kind, "last_run_at": sentinel.utcnow(),
                       "interval_s": EMPTY_SWEEP_INTERVAL_S if kind == "empty" else None,
                       "counts": {}, "last_error": f"{type(exc).__name__}: {exc}"[:500]})
    except Exception:  # noqa: BLE001 - the failure being recorded may be the DB itself
        log.warning("hygiene: could not record the %s sweep failure", kind)


def load_summary(kind: str) -> Optional[dict[str, Any]]:
    raw = ledger.meta_get(META_EMPTY if kind == "empty" else META_UNTITLED)
    if not raw:
        return None
    try:
        out = json.loads(raw)
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


# ---------------------------------------------------------- job A: empty ---

def _write_off_vanished(resolved: Resolved, counts: dict[str, int], *,
                        dry_run: bool) -> list[dict[str, Any]]:
    """Mark ledger rows whose file vanished as ``gone``. Careful, because gone
    is hard to undo and a wrong guess hides real failures and re-runs enriched notes.

    - only rows under a folder this sweep just ACCEPTED (it exists, it is in a
      vault): an unmounted, renamed or refused tree writes nothing off, and a
      row outside every swept folder is none of this job's business;
    - only rows whose absence is PROVEN (documents.file_absent: absolute path,
      ENOENT, nothing unknown);
    - only after the row was missing on the previous sweep too, so a vault that
      blinked, a note mid-move or a Syncthing rename gets one sweep of grace.
    """
    roots = [str(f.root) for f in resolved.folders]
    try:
        previous = {tuple(k) for k in json.loads(ledger.meta_get(META_VANISHED_PENDING) or "[]")}
    except (ValueError, TypeError):
        previous = set()
    gone: list[dict[str, Any]] = []
    pending: set[tuple[str, str, str]] = set()
    for row in documents.missing_rows(roots):
        key = (row["binding_id"], row["doc_id"], row["path"])
        entry = (documents.mark_gone(row["binding_id"], row["doc_id"], roots, dry_run=dry_run)
                 if key in previous else None)
        if entry is not None:
            gone.append(entry)
        else:
            pending.add(key)
    counts["gone_pending"] = len(pending)
    if not dry_run:
        ledger.meta_set(META_VANISHED_PENDING, json.dumps(sorted(pending)))
    return gone


def sweep_empty(*, dry_run: bool = False, record: bool = False,
                folders_spec: Optional[str] = None, min_age: Optional[float] = None,
                now: Optional[float] = None) -> dict[str, Any]:
    """Trash empty notes in the swept folders and write off vanished rows.

    ``record`` stores the summary in ``ledger.meta`` for the doctor probe. The
    worker records; the CLI does not, because a manual run must not make a dead
    worker look alive. A dry run never records and changes nothing.
    """
    started = time.time()
    resolved = resolve_folders(folders_spec)
    errors = list(resolved.errors)
    counts: dict[str, int] = {}
    acted: list[dict[str, Any]] = []
    for folder in resolved.folders:
        for path in iter_candidates(folder):
            counts["scanned"] = counts.get("scanned", 0) + 1
            try:
                result = trash_note(path, folder, min_age=min_age, now=now, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - one note never stops the sweep
                log.warning("hygiene: could not sweep %s: %s: %s", path,
                            type(exc).__name__, exc)
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
                counts["errors"] = counts.get("errors", 0) + 1
                continue
            counts[result["outcome"]] = counts.get(result["outcome"], 0) + 1
            if result["outcome"] in ("trashed", "would_trash", "restored"):
                acted.append(result)
    gone: list[dict[str, Any]] = []
    try:
        gone = _write_off_vanished(resolved, counts, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        log.warning("hygiene: marking vanished documents failed: %s: %s",
                    type(exc).__name__, exc)
        errors.append(f"mark_vanished: {type(exc).__name__}: {exc}")
    counts["gone_marked"] = len(gone)
    for entry in gone:
        log.info("hygiene: document %s%s is gone: %s",
                 entry["doc_id"], f"@{entry['binding_id']}" if entry["binding_id"] else "",
                 entry["path"])
    summary = _summary("empty", started=started, counts=counts, errors=errors,
                       resolved=resolved, dry_run=dry_run,
                       acted=acted[:50], gone=gone[:50])
    if record and not dry_run:
        _record("empty", summary)
    changed = counts.get("trashed", 0) + counts.get("restored", 0) + len(gone)
    log.log(logging.INFO if changed else logging.DEBUG,
            "empty-note sweep: %s", json.dumps(summary["counts"], sort_keys=True))
    return summary


# ------------------------------------------------------- job B: untitled ---

def safe_title_name(slug: Any) -> Optional[str]:
    """The ``<title-slug>.md`` file name for a slug, or None if it cannot be a
    plain, new-looking file name."""
    if not isinstance(slug, str):
        return None
    slug = slug.strip()
    if slug.endswith(".md"):
        slug = slug[:-3]
    if not re.fullmatch(r"\w[\w. \-]{0,119}", slug) or slug.endswith((".", " ")):
        return None
    if UNTITLED_RE.match(slug):
        return None  # renaming Untitled 3 to "untitled" would only loop
    return slug + ".md"


def rename_note(path: Path, folder: Folder, slug: Any) -> dict[str, Any]:
    """Rename a processed note to ``<title-slug>.md`` in its own folder.

    Under the run locks of every ledger row at the old path; afterwards each of
    those rows is re-pointed at the new path with the file's current hash.
    """
    name = safe_title_name(slug)
    if name is None:
        return {"path": str(path), "outcome": "unsafe_title", "detail": repr(slug)[:80]}
    if name == path.name:
        return {"path": str(path), "outcome": "already_named"}
    rows = documents.rows_for_path(path)
    with documents.hold_run_locks(rows) as held:
        if not held:
            return {"path": str(path), "outcome": "rename_locked"}
        if _row_keys(documents.rows_for_path(path)) != _row_keys(rows):
            return {"path": str(path), "outcome": "changed", "detail": "ledger rows changed"}
        refused = static_verdict(path, folder)
        if refused is not None or not path.is_file():
            return {"path": str(path), "outcome": refused.verdict if refused else "vanished"}
        used = rename.move_noclobber(path, path.with_name(name))
        try:
            digest: Optional[str] = documents.sha256_file(used)
        except OSError:
            digest = None
        for binding_id, doc_id in sorted(_row_keys(rows)):
            documents.repoint(binding_id, doc_id, used, digest)
        return {"path": str(path), "outcome": "renamed", "to": str(used)}


PLACEHOLDER_TITLE_RE = re.compile(r"^transcription: .+\.(?:ogg|mp3|m4a|wav|opus|flac|webm)$",
                                  re.IGNORECASE)


def placeholder_title(value: Any) -> bool:
    """A title nobody chose: Obsidian's ``Untitled N``, or the retired
    pipeline's ``Transcription: <file>.ogg``. title-slug keeps an existing
    title verbatim and the runner never clobbers one, so a placeholder left in
    place would name the note ``untitled-5.md`` forever."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    return bool(UNTITLED_RE.match(value) or PLACEHOLDER_TITLE_RE.match(value))


def _without_keys(text: str, keys: frozenset) -> Optional[str]:
    """The note with top-level frontmatter ``keys`` cut out as TEXT, or None.

    Not a YAML round-trip: re-dumping rewrites every other value (a
    ``2026-10-09 01:22:51-04:00`` comes back as ``2026-10-09 05:22:51``, the
    offset silently lost). A key's continuation lines (indented, or ``- ``
    items) go with it. The cut is verified by re-parsing: the result must be
    exactly the old frontmatter minus those keys, or nothing is written.
    """
    fm, _ = frontmatter.split(text)
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines[1:], start=1)
                if line.strip() == frontmatter.DELIM), None)
    if not fm or end is None:
        return None
    kept: list[str] = []
    dropping = False
    for line in lines[1:end]:
        key = re.match(r"^([^\s:#][^:]*?)\s*:(?:\s|$)", line)
        if key:
            dropping = key.group(1).strip("'\"") in keys
        elif not (line[:1].isspace() or line.startswith("- ")):
            dropping = False
        if not dropping:
            kept.append(line)
    out = "\n".join([lines[0], *kept, *lines[end:]])
    new_fm, _ = frontmatter.split(out)
    if new_fm != {k: v for k, v in fm.items() if k not in keys}:
        return None
    return out


def clear_placeholder_title(path: Path, folder: Folder) -> bool:
    """Drop a placeholder ``title`` (and the ``title-slug`` derived from it) so
    title-slug can propose a real one. Under the note's run locks, through a
    dot-staged copy and one atomic replace, and only if the file did not change
    while being rewritten. Every other byte of the note is left as it was.
    Returns whether it rewrote the note."""
    rows = documents.rows_for_path(path)
    with documents.hold_run_locks(rows) as held:
        if not held or static_verdict(path, folder) is not None:
            return False
        before = os.lstat(path)
        raw = path.read_bytes().decode("utf-8")
        fm, _ = frontmatter.split(raw)
        if not placeholder_title(fm.get("title")):
            return False
        out = _without_keys(raw, frozenset({"title", "title-slug"}))
        if out is None:
            log.warning("hygiene: could not cut the placeholder title out of %s cleanly; "
                        "leaving it", path)
            return False
        staged = path.with_name(f"{STAGING_PREFIX}hygiene-{path.name}")
        staged.write_bytes(out.encode("utf-8"))
        after = os.lstat(path)
        if (after.st_ino, after.st_size, after.st_mtime_ns) != (
                before.st_ino, before.st_size, before.st_mtime_ns):
            staged.unlink(missing_ok=True)
            return False
        os.replace(staged, path)
        log.info("hygiene: cleared placeholder title from %s", path)
        return True


def _known_identity(path: Path) -> Optional[str]:
    """The doc_id an earlier unbound run already gave THIS note, if any.

    A document's identity is its content hash at first sight, and every run
    (failures included) rewrites the note's frontmatter. Without adopting the
    recorded row each nightly attempt would mint a new identity: a new receipt
    whose attempt bound can never trip, a new set of failed pass rows, and a
    retry that never ends. Adopting keeps ONE row, so a completed receipt is
    "already processed" (we go on to rename), an exhausted one stops the retries
    and a later success overwrites the failed pass rows instead of orphaning them.

    Only a row whose recorded bytes match the file counts: a note somebody has
    edited since is new content and earns a new identity.
    """
    try:
        digest = documents.sha256_file(path)
    except OSError:
        return None
    mine = [r for r in documents.rows_for_path(path)
            if not r["binding_id"] and r["state"] not in documents.VANISHED_STATES
            and digest in (r["current_sha256"], r["sha256"])]
    if not mine:
        return None
    return max(mine, key=lambda r: r["updated_at"] or "")["doc_id"]


def _process_untitled(path: Path, folder: Folder, *, min_age: float, now: float,
                      dry_run: bool, budget: dict[str, int]) -> dict[str, Any]:
    insp = inspect_note(path, folder, min_age=min_age, now=now)
    if insp.verdict != "ok":
        return {"path": str(path), "outcome": insp.verdict, "detail": insp.detail}
    if insp.kind == "empty":
        return trash_note(path, folder, min_age=min_age, now=now, dry_run=dry_run)
    if insp.kind != "content":
        return {"path": str(path), "outcome": insp.kind, "detail": insp.detail}
    if dry_run:
        return {"path": str(path), "outcome": "would_summarize"}
    if budget["executed"] >= budget["max"]:
        return {"path": str(path), "outcome": "deferred"}
    clear_placeholder_title(path, folder)
    run = pipelines.run_document(UNTITLED_PIPELINE, path, explicit=True,
                                 doc_id=_known_identity(path))
    skipped = run.get("skipped")
    if not skipped:
        budget["executed"] += 1
    already = isinstance(skipped, str) and skipped.startswith("already processed")
    if (skipped and not already) or run.get("failed") or run.get("halted"):
        reason = next((str(r.get("reason_code") or r.get("state"))
                       for r in run.get("results") or []
                       if r.get("state") == "failed"), "")
        return {"path": str(path),
                "outcome": "skipped" if skipped and not already else "failed",
                "detail": str(skipped or reason or ("halted" if run.get("halted") else ""))[:160]}
    slug = frontmatter.read(path)[0].get("title-slug")
    if not (isinstance(slug, str) and slug.strip()):
        return {"path": str(path), "outcome": "no_title"}
    renamed = rename_note(path, folder, slug)
    if renamed["outcome"] == "renamed":
        return {"path": str(path), "outcome": "renamed", "to": renamed["to"]}
    return {"path": str(path), "outcome": "summarized_" + renamed["outcome"],
            "detail": renamed.get("detail", "")}


def untitled_due(now_local: Optional[datetime] = None) -> bool:
    """Whether the nightly sweep owes a run: from the configured local hour on,
    and not yet done for today's local date (a durable fact, so a waxd restart
    neither repeats nor skips the night)."""
    now_local = now_local or _now_local()
    if now_local.hour < UNTITLED_SWEEP_HOUR:
        return False
    return ledger.meta_get(META_UNTITLED_DATE) != now_local.date().isoformat()


def sweep_untitled(*, dry_run: bool = False, nightly: bool = False,
                   folders_spec: Optional[str] = None, min_age: Optional[float] = None,
                   now: Optional[float] = None, max_runs: Optional[int] = None,
                   ) -> dict[str, Any]:
    """Trash empty ``Untitled*.md`` notes; summarize and rename the rest.

    ``nightly`` is the worker's form: it records the summary and, only when the
    run completed, today's date (a crash or an incomplete run retries). The CLI
    form runs now, whatever the hour or date, and records nothing.
    """
    started = time.time()
    local_date = _now_local().date().isoformat()
    resolved = resolve_folders(folders_spec)
    errors = list(resolved.errors)
    counts: dict[str, int] = {}
    notes: list[dict[str, Any]] = []
    incomplete = bool(resolved.errors)
    min_age = min_age_s() if min_age is None else min_age
    if UNTITLED_PIPELINE not in pipelines.compiled():
        # Touch nothing: without the pipeline there is no title to rename to,
        # and trashing alone is the interval job's business.
        errors.append(f"pipeline {UNTITLED_PIPELINE!r} is not compiled; "
                      f"run `wax pipeline compile {UNTITLED_PIPELINE}`")
        incomplete = True
        resolved = Resolved([], resolved.missing, resolved.errors)
    budget = {"executed": 0, "max": UNTITLED_MAX_PER_RUN if max_runs is None else max_runs}
    for folder in resolved.folders:
        for path in iter_candidates(folder, untitled_only=True):
            counts["scanned"] = counts.get("scanned", 0) + 1
            try:
                result = _process_untitled(
                    path, folder, min_age=min_age,
                    now=_now_epoch() if now is None else now,
                    dry_run=dry_run, budget=budget)
            except Exception as exc:  # noqa: BLE001 - one bad note never stops the run
                log.warning("hygiene: untitled sweep failed on %s: %s: %s", path,
                            type(exc).__name__, exc)
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
                counts["errors"] = counts.get("errors", 0) + 1
                continue
            outcome = result["outcome"]
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome == "deferred":
                incomplete = True
            if outcome not in ("young", "excluded"):
                notes.append(result)
            if not dry_run:
                _log_untitled(result)
    summary = _summary("untitled", started=started, counts=counts, errors=errors,
                       resolved=resolved, dry_run=dry_run, complete=not incomplete,
                       date=local_date, notes=notes[:100])
    if nightly and not dry_run:
        _record("untitled", summary)
        if not incomplete:
            ledger.meta_set(META_UNTITLED_DATE, local_date)
    log.info("untitled sweep: %s%s", json.dumps(summary["counts"], sort_keys=True),
             "" if not incomplete else " (incomplete; will retry)")
    return summary


def _log_untitled(result: dict[str, Any]) -> None:
    outcome, path = result["outcome"], result["path"]
    if outcome == "renamed":
        log.info("hygiene: summarized+renamed %s -> %s", path, result["to"])
    elif outcome == "trashed":
        log.info("hygiene: trashed empty untitled note %s -> %s", path, result.get("to"))
    elif outcome in ("failed", "skipped"):
        log.info("hygiene: untitled note %s %s: %s", path, outcome, result.get("detail"))
    elif outcome not in ("young", "excluded", "not_empty"):
        log.info("hygiene: untitled note %s: %s%s", path, outcome,
                 f" ({result['detail']})" if result.get("detail") else "")


# --------------------------------------------------------------- health ----

def health(*, pipeline_enabled: bool, waxd_running: bool,
           worker: Optional[dict[str, Any]] = None, now: Optional[float] = None,
           today: Optional[str] = None) -> dict[str, str]:
    """The `wax doctor` "document hygiene" probe, as status/detail/fix.

    Green only on evidence: the empty sweep's recorded summary has no error and
    is fresh (3x its own interval) while the worker is meant to be running it.
    """
    now = _now_epoch() if now is None else now
    today_d = date.fromisoformat(today) if today else _now_local().date()
    notes: list[str] = []
    fail: list[str] = []
    warn: list[str] = []
    fix = ""
    empty = load_summary("empty")
    untitled = load_summary("untitled")
    if empty is None:
        if pipeline_enabled and waxd_running:
            fail.append("the empty-note sweep has never recorded a run")
            fix = "systemctl --user restart waxd   # the worker runs it on its first tick"
        else:
            notes.append("no empty-note sweep recorded yet")
    else:
        if empty.get("last_error"):
            fail.append(f"last empty-note sweep failed: {empty['last_error']}")
            fix = "wax documents sweep-empty --dry-run"
        interval = float(empty.get("interval_s") or EMPTY_SWEEP_INTERVAL_S)
        ran = documents._parse_iso(empty.get("last_run_at"))
        age = None if ran is None else max(0.0, now - ran)
        if age is None:
            fail.append("the empty-note sweep's last_run_at is unreadable")
        elif age > 3 * interval and pipeline_enabled and waxd_running:
            busy = bool(worker and worker.get("state") == "live" and worker.get("busy"))
            msg = (f"last empty-note sweep {int(age)}s ago (limit {int(3 * interval)}s)")
            if busy:
                warn.append(msg + f"; the worker is busy ({worker.get('busy')})")
            else:
                fail.append(msg)
                fix = fix or "systemctl --user restart waxd"
        elif age is not None:
            counts = empty.get("counts") or {}
            notes.append(f"empty sweep {int(age)}s ago: {counts.get('trashed', 0)} trashed, "
                         f"{counts.get('gone_marked', 0)} gone")
    date_s = ledger.meta_get(META_UNTITLED_DATE)
    if untitled and untitled.get("last_error"):
        warn.append(f"last nightly untitled sweep: {untitled['last_error']}")
    if date_s:
        try:
            days = (today_d - date.fromisoformat(date_s)).days
        except ValueError:
            days = None
        if days is None or days > 2:
            warn.append(f"nightly untitled sweep last completed {date_s}")
        else:
            notes.append(f"untitled sweep {date_s}")
    elif pipeline_enabled and waxd_running:
        first = ledger.meta_get(META_FIRST_RUN)
        ran = documents._parse_iso(first) if first else None
        if ran is not None and now - ran > 2 * 86400:
            warn.append("nightly untitled sweep has never completed")
        else:
            notes.append("nightly untitled sweep not yet run")
    status = "fail" if fail else ("warn" if warn else "pass")
    detail = "; ".join(fail + warn + notes) or "ok"
    return {"status": status, "detail": detail, "fix": fix}
