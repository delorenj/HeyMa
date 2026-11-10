"""Vault sweep: the whole Obsidian vault's nightly tidy, the vault-wide sibling
of ``wax transcripts prune``.

``wax vault sweep`` walks the vault that contains ``WAX_VAULT`` (the nearest
ancestor with ``.obsidian/``) and plans five actions. It is a DRY RUN unless
``--apply`` is given; with ``--apply`` every change is a move (into
``<vault>/.trash/<relpath>``, to a new name, or into ``Inbox/``) or an
``rmdir`` of an empty directory. Nothing is ever unlinked or overwritten by a
sweep, and every change is journaled to ``var/vault-sweep.jsonl`` BEFORE it
happens, so ``wax vault unsweep --run RUN`` can put a whole run back (a note the
untitled pipeline rewrote is put back byte for byte from its pre-image, but only
while it is still exactly what the pipeline wrote).

Rule table (the hub's ``references/vault-sweep.md`` carries the same table):

| action | what it matches | what --apply does |
|---|---|---|
| ``prune-empty`` | a zero-byte file of any type (bar ``__init__.py``/``py.typed``); a ``*.md`` with a blank body whose frontmatter holds only blank values or ``created``/``modified``/``updated``-style stamps (NOT ``aliases``, ``tags``, ``date``, ``id``: those are the author's); an UNFILLED Templater stub (operator order 2026-11-10): at least one unexpanded ``<% ... %>`` tag, only stamps in the frontmatter, and nothing in the body but headings, tags, empty list items and unchecked tasks that at least 3 stubs in the same folder share (a ticked or ``[/]`` box, a typed word or a task only you wrote keeps it) | trash (link-guarded) |
| ``prune-temp`` | DROPPINGS by suffix: ``.tmp .temp .swp .swo .part .partial .crdownload`` or a trailing ``~`` -> trash whatever they hold. BACKUPS ``.bak .bak-* .orig`` -> trash only while the original sits beside them, else report. NAMES whose whole stem is ``tmp``/``temp``/``tmep`` (+ up to 3 digits), ``scratch``/``scratchpad`` (+ digits), ``<x>-scratch``/``<x>_scratch``, or ``tmp`` + 8 lowercase chars with a digit -> trash whatever it holds (operator order 2026-11-10: "I don't want any of them"); only the whole stem counts, so "Learning Scratch" or "Build a Database from Scratch" never match. Age floor ``max(min-age, WAX_VAULT_SWEEP_TEMP_MIN_AGE_S)`` | trash (link-guarded) |
| ``untitled`` | stem ``Untitled``, ``Untitled 2``, ``untitled-3``, ``UNTITLED CHAT 2024-12-01 15 18 32 1`` ... | ``*.md`` blank or stamp-only: trash. ``*.md`` with content: save a pre-image under ``.trash/.vault-sweep/<run>/``, run ``untitled-standard`` (summarize, title-slug), then rename to the slug (at most ``WAX_UNTITLED_SWEEP_MAX`` per run; a case-only clash with a sibling is held). A default ``.base``/empty ``.canvas``: trash. Anything else: report |
| ``consolidate`` | ``X copy``, ``X copy 2``, ``X (1)``, Syncthing ``.sync-conflict-``/``conflicted copy`` beside ``X``; a bare ``X 1``..``X 9`` only while ``X`` itself exists | a copy identical to the survivor (``X``, else the oldest copy; bytes, or text after CRLF/trailing-space normalisation): trash. ``X`` missing and ONE Syncthing conflict file left: rename it to exactly ``X`` (never a case-only clash). A lone ``X copy``/``X (1)`` is a name, not a conflict: report. Differing copies, bare-N orphans, same-folder identical notes: report |
| ``organize`` | a loose ``*.md`` in the vault root (not ALL-CAPS, not Home/Index/Dashboard/README); an empty directory below the top level | move the note to exactly ``Inbox/<name>`` (refused on any collision, case-insensitively); ``rmdir`` the directory, deepest first |

Each file gets at most one action per run, in the order above. A held file
(locked, linked, changed, collision) is claimed and left alone; a report does
not claim it.

Hard exclusions, in ONE choke point (``Sweep.refusal`` for files, the walk's
pruning for directories), applied at plan time AND again under the document's
run lock right before the move:

- any dot-file or dot-directory (``.obsidian``, ``.trash``, ``.git`` ...) and any
  ``_``-prefixed directory (``_vault`` templates, ``_triage`` lanes); a name that
  is not valid UTF-8 (listed under ``skipped``, never fatal);
- symlinks, never followed or moved, and any file whose real parent leaves the vault;
- ``Transcripts/`` and the real ``WAX_VAULT`` (``wax transcripts prune`` owns it),
  ``Inbox/Wax`` and every folder a document pipeline is bound to, the Obsidian
  template folders, and ``WAX_VAULT_SWEEP_EXCLUDE`` (``:``-separated, relative to
  the vault; default ``agents``);
- code projects: a directory below the vault root holding a ``.git``,
  ``package.json``, ``pyproject.toml``, ``mise.toml``, ``__init__.py``, ``node_modules`` ...
  marker is skipped whole, and ``node_modules``/``.venv``/``venv``/``__pycache__``/``target``
  are never entered;
- files whose mtime OR ctime is younger than ``WAX_VAULT_SWEEP_MIN_AGE_S`` (default
  86400; a file that just arrived through Syncthing or was put back by hand counts
  as new) or larger than ``WAX_VAULT_SWEEP_MAX_BYTES`` (default 16 MiB);
- a file the sweep itself trashed or moved earlier and somebody put back (same
  inode, mtime and size as a journal line): the operator's "keep" is remembered;
- a document whose run lock a live pipeline run holds;
- the link guard: a temp, copy, untitled, empty or root note that some other note,
  canvas, base or ``.obsidian`` plugin/bookmark config links to (wikilink, markdown
  link, HTML ``href``, canvas ``file``, a quoted name in a ``.base``, a quoted path or
  file name in a config; matched on the target's base name, a config path on its
  exact path, every ``bookmarks.json`` entry an exact path; ``workspace*.json``,
  ``graph.json`` and the recent-files history are UI state and do not count) is
  held, never trashed, renamed or moved.

One bad file or one failing action never stops the run: each is an ``errors``
line (exit 2) and the rest carries on, so the run id and the last-run meta row
always land.

Exit 2 (in the CLI) on any error, including a refusal such as an apply run while
another holds ``var/vault-sweep.lock`` or an uncompiled ``untitled-standard``: a
refusal never reads as "nothing to do". Scheduling is Candybar's, never this
module's.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Optional

from . import documents, frontmatter, hygiene, ledger, paths, pipelines, rename, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

ACTIONS = ("prune-empty", "prune-temp", "untitled", "consolidate", "organize")
MANIFEST = "vault-sweep.jsonl"
LOCK_NAME = "vault-sweep.lock"
META_LAST_RUN = "vault_sweep_last_run"
INBOX = "Inbox"
DEFAULT_EXCLUDE = "agents"
LINK_SCAN_CHUNK = 8 << 20
LINK_SCAN_OVERLAP = 1024
OBSIDIAN_CONFIG_MAX_BYTES = 8 << 20
# UI state, not references: "Untitled" is every empty tab's title, so scanning it would hold every Untitled note.
OBSIDIAN_STATE_PREFIXES = ("workspace", "graph")
# Plugins whose data.json is a history of files opened, not a reference to them:
# a note you once opened is not a note you keep (2026-11-10: recent-files held
# Inbox/tmp.md and every Untitled it ever showed).
OBSIDIAN_HISTORY_PLUGINS = frozenset({"recent-files-obsidian"})
# Configs whose quoted values are always vault-relative paths, so a bare
# ``"scratch.md"`` there means the vault-root file, never any scratch.md.
OBSIDIAN_PATH_CONFIGS = frozenset({"bookmarks.json"})
PREIMAGE_DIR = ".vault-sweep"
IDENTICAL_MAX_BYTES = 4 << 20
KEEP_EMPTY_NAMES = frozenset({"__init__.py", "py.typed"})
TEXT_SUFFIXES = frozenset({".md", ".txt", ".canvas", ".csv", ".json", ".yaml", ".yml", ".base"})

CODE_MARKERS = frozenset({
    ".git", ".hg", ".svn", "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "Pipfile", "Cargo.toml", "go.mod", "Gemfile", "pom.xml", "build.gradle",
    "composer.json", "deno.json", "tsconfig.json", "Makefile", "CMakeLists.txt", "mise.toml",
    ".mise.toml", "__init__.py", "node_modules", ".venv", "venv", ".tox"})
NEVER_DESCEND = frozenset({"node_modules", ".venv", "venv", "__pycache__", "target"})

# A name that merely STARTS like a temp name is not one: the whole stem must match.
TEMP_STEM_RE = re.compile(
    r"^(?:(?:tmp|temp|tmep)(?:[ _-]?\d{1,3})?|scratch(?:pad)?(?:[ _-]?\d{1,3})?"
    r"|.+[_-]scratch(?:pad)?)$", re.IGNORECASE)
# Python's tempfile: "tmp" + 8 of [a-z0-9_]; a digit keeps "tmpanalysis" a word.
TMP_RANDOM_RE = re.compile(r"^tmp(?=[a-z_]*[0-9])[a-z0-9_]{8}$")
TEMP_DROPPING_SUFFIXES = (".tmp", ".temp", ".swp", ".swo", ".part", ".partial", ".crdownload")
TEMP_BACKUP_SUFFIXES = (".bak", ".orig")
TEMP_BAK_RE = re.compile(r"\.bak[-.]\S*$", re.IGNORECASE)

UNTITLED_STEM_RE = re.compile(
    r"^untitled(?:[ _-]+(?:chat|note|document|drawing|diagram|canvas|base|board|page|file))?"
    r"(?:[ _-]*\d(?:[\d :._-]*\d)?)?$", re.IGNORECASE)
# Frontmatter keys that exist without anyone having written anything. hygiene.AUTO_KEYS also lists
# aliases/tags/date/id/cssclasses; a vault-wide sweep must not call those empty: they are the
# author's, and an alias is something other notes link to.
VAULT_AUTO_KEYS = frozenset({"created", "modified", "updated", "created_at", "updated_at",
                             "wax", "wax-item-id"})
STAMP_KEYS = VAULT_AUTO_KEYS | {"pipeline-status", "captured", "migrated_from", "migrated_at"}

# (regex on the stem, kind). "copy"/"paren"/"conflict" are unambiguous; "n" (a bare " 2") is not.
COPY_MARKERS = (
    (re.compile(r"^(?P<base>.+?)\.sync-conflict-\d{8}-\d{6}-[A-Za-z0-9]{7}$"), "conflict"),
    (re.compile(r"^(?P<base>.+?) \([^()]*conflicted copy[^()]*\)$", re.IGNORECASE), "conflict"),
    (re.compile(r"^(?P<base>.+?) copy(?: (?P<n>[1-9]\d?))?$", re.IGNORECASE), "copy"),
    (re.compile(r"^(?P<base>.+?) \((?P<n>[1-9]\d?)\)$"), "paren"),
    (re.compile(r"^(?P<base>.+?) (?P<n>[1-9])$"), "n"),
)
ROOT_KEEP = frozenset({"home", "index", "dashboard", "readme"})
ALL_CAPS_RE = re.compile(r"^[A-Z0-9_ -]+$")

WIKILINK_RE = re.compile(r"!?\[\[([^\[\]\|#\^\n]+)")
MDLINK_RE = re.compile(r"\]\(\s*(<[^>\n]+>|[^)\s]+)")
FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,6}$")
HTMLLINK_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"'\n]+)["']""", re.IGNORECASE)
CANVAS_FILE_RE = re.compile(r'"file"\s*:\s*"([^"]+)"')
# A .base filter or a plugin config names a note as a quoted string (`file.name == "tmp"`, a capture path).
QUOTED_RE = re.compile(r"""["']([^"'\n]{1,300})["']""")

CHANGES = frozenset({"trashed", "renamed", "moved", "removed", "titled_renamed"})
WOULD = frozenset({"would_trash", "would_rename", "would_title", "would_move", "would_remove"})
# outcomes after which the file is no longer there under its old name
MOVED_AWAY = frozenset({"trashed", "moved", "renamed", "titled_renamed", "would_trash", "would_move",
                        "would_rename"})


class VaultSweepError(RuntimeError):
    """A refusal or fault that must not be swallowed as 'nothing to do'."""


class Entry(NamedTuple):
    path: Path
    st: os.stat_result


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise VaultSweepError(f"{name}={raw!r} is not a number") from exc


def min_age_s() -> float:
    return _env_number("WAX_VAULT_SWEEP_MIN_AGE_S", 86400.0)


def temp_min_age_s() -> float:
    return _env_number("WAX_VAULT_SWEEP_TEMP_MIN_AGE_S", 604800.0)


def max_bytes() -> int:
    return int(_env_number("WAX_VAULT_SWEEP_MAX_BYTES", float(16 << 20)))


def manifest_path() -> Path:
    return paths.VAR / MANIFEST


def vault_root() -> Path:
    """The real path of the Obsidian vault that contains ``WAX_VAULT``."""
    store = Path(os.path.realpath(paths.VAULT))
    root = hygiene.vault_root_for(store)
    if root is None:
        raise VaultSweepError(f"{paths.VAULT} is not inside an Obsidian vault (no .obsidian/ above it)")
    root = Path(os.path.realpath(root))
    if root == store:
        raise VaultSweepError(f"WAX_VAULT ({paths.VAULT}) is the vault root itself; refusing to sweep it")
    if root in (Path("/"), Path.home()):
        raise VaultSweepError(f"refusing to sweep {root}")
    return root


def read_manifest() -> list[dict[str, Any]]:
    path = manifest_path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _append_manifest(entry: dict[str, Any]) -> None:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _new_run_id() -> str:
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    taken = {e.get("run") for e in read_manifest()}
    run, n = base, 1
    while run in taken:
        n += 1
        run = f"{base}-{n}"
    return run


def _sig(st: os.stat_result) -> tuple:
    return (st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns)


def _read_bytes(path: Path, limit: int) -> Optional[bytes]:
    """The file's bytes through O_NOFOLLOW, or None (symlink, unreadable, too large)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        chunks, total = [], 0
        while total <= st.st_size:
            block = os.read(fd, 1 << 20)
            if not block:
                break
            chunks.append(block)
            total += len(block)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _text(path: Path, limit: int = hygiene.MAX_EMPTY_BYTES) -> Optional[str]:
    data = _read_bytes(path, limit)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def blank_note(text: str, allowed: frozenset) -> bool:
    """A blank body whose frontmatter holds no value except blanks and the ``allowed`` keys."""
    if hygiene.classify_text(text) not in ("empty", "content"):
        return False
    plain = text.lstrip("\ufeff")
    if not plain.strip():
        return True
    if not plain.startswith("---"):
        return False
    fm, body = frontmatter.split(plain)
    if body.strip() or not isinstance(fm, dict):
        return False
    return all(key in allowed or (key == "source" and value == "vault-migration") or hygiene._blank(value)
               for key, value in fm.items())


def strictly_empty(text: str) -> bool:
    """Nothing the author wrote: blank body, and only blank values or creation stamps in the frontmatter."""
    return blank_note(text, VAULT_AUTO_KEYS)


def stamp_only(text: str) -> bool:
    """Blank body whose frontmatter holds only auto-stamped keys (``created``, ``pipeline-status`` ...)."""
    return blank_note(text, STAMP_KEYS)


# An unfilled Templater daily stub (operator order 2026-11-10, "trash the daily
# stubs"): the template's tags were never expanded and nobody wrote in it.
TEMPLATER_TAG_RE = re.compile(r"<%.*?%>")
LIST_MARK_RE = re.compile(r"^(?:[-*+]|\d+[.)])(?:\s+\[ \])?(?:\s+|$)")
UNCHECKED_TASK_RE = re.compile(r"^[-*+]\s+\[ \]\s+\S")
TEMPLATE_STUB_MAX_BYTES = 16 << 10
# An unchecked task with text is template boilerplate only while this many stubs
# in the same folder carry the identical line; a task you typed is yours.
TEMPLATE_BOILERPLATE_MIN = 3


def template_stub_tasks(text: str) -> Optional[list[str]]:
    """The unchecked task lines of an UNFILLED Templater stub, or None when the note is anything else.

    A stub still holds at least one unexpanded ``<% ... %>`` tag, its frontmatter only stamps, and every
    body line is a heading, a tag (optionally after a list marker), an empty list item or an unchecked
    task. A ticked or in-progress box, a completion stamp or one typed word makes it a real note."""
    if hygiene.classify_text(text) not in ("empty", "content"):
        return None
    plain = text.lstrip("﻿")
    body = plain
    if plain.startswith("---"):
        fm, body = frontmatter.split(plain)
        if not isinstance(fm, dict) or not all(
                key in STAMP_KEYS or (key == "source" and value == "vault-migration") or hygiene._blank(value)
                for key, value in fm.items()):
            return None
    if not TEMPLATER_TAG_RE.search(body):
        return None
    tasks: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if UNCHECKED_TASK_RE.match(line) and not TEMPLATER_TAG_RE.search(line):
            tasks.append(line)
            continue
        rest = LIST_MARK_RE.sub("", TEMPLATER_TAG_RE.sub("", line).strip(), count=1).strip()
        if rest:
            return None
    return tasks


def default_base(text: str) -> bool:
    """An Obsidian ``.base`` nobody configured: only unnamed-ish default views."""
    import yaml
    try:
        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - unparseable is not default
        return False
    if data is None:
        return True
    if not isinstance(data, dict) or set(data) - {"views"}:
        return False
    views = data.get("views") or []
    return isinstance(views, list) and all(
        isinstance(v, dict) and not set(v) - {"type", "name"} for v in views)


def empty_canvas(text: str) -> bool:
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return False
    return isinstance(data, dict) and not data.get("nodes") and not data.get("edges") and \
        not set(data) - {"nodes", "edges"}


def temp_match(name: str) -> Optional[tuple[str, str]]:
    """(kind, why) when ``name`` looks like a temp/scratch file, else None.

    ``dropping`` (a suffix editors and downloaders leave) is trashed whatever it holds; ``backup``
    only while its original sits beside it; ``name`` (a stem such as ``tmp`` or ``scratch``) only
    when the text is blank, because a person can type a real note into a file named ``tmp.md``.
    """
    low = name.lower()
    if low.endswith("~") and low != "~":
        return "dropping", "trailing ~"
    for suffix in TEMP_DROPPING_SUFFIXES:
        if low.endswith(suffix) and low != suffix:
            return "dropping", f"{suffix} file"
    if backup_original(name) is not None:
        return "backup", "backup copy"
    stem = name.rsplit(".", 1)[0] if "." in name.strip(".") else name
    if TEMP_STEM_RE.match(stem) or TMP_RANDOM_RE.match(stem):
        return "name", "temp/scratch name"
    return None


def backup_original(name: str) -> Optional[str]:
    """The file a ``X.bak``, ``X.bak-<stamp>`` or ``X.orig`` is a backup of, or None."""
    low = name.lower()
    for suffix in TEMP_BACKUP_SUFFIXES:
        if low.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    match = TEMP_BAK_RE.search(name)
    if match and match.start() > 0:
        return name[:match.start()]
    return None


def copy_marker(stem: str) -> Optional[tuple[str, str]]:
    """(base stem, kind) for a copy-looking stem, or None."""
    for regex, kind in COPY_MARKERS:
        m = regex.match(stem)
        if m and m.group("base").strip():
            return m.group("base"), kind
    return None


def _normalized(data: bytes) -> Optional[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).rstrip("\n")


def identical(a: Path, b: Path, limit: int) -> Optional[bool]:
    """Whether two files hold the same content; None when either cannot be read (not 'differs')."""
    da, db = _read_bytes(a, limit), _read_bytes(b, limit)
    if da is None or db is None:
        return None
    if da == db:
        return True
    if a.suffix.lower() in TEXT_SUFFIXES and b.suffix.lower() in TEXT_SUFFIXES:
        na, nb = _normalized(da), _normalized(db)
        return na is not None and na == nb
    return False


def _touched(st: os.stat_result) -> float:
    """When a file last changed, by mtime or ctime: a file Syncthing just wrote, or somebody just
    put back, is new however old its mtime is. Tests patch this to mtime (they cannot backdate ctime)."""
    return max(st.st_mtime, st.st_ctime)


def _printable(text: str) -> str:
    """``text`` with any undecodable byte (a surrogate escape) spelled out, safe to print or JSON-encode."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def casefold_clash(directory: Path, name: str, *, ignore: frozenset = frozenset()) -> str:
    """The name of an entry in ``directory`` that equals ``name`` ignoring case (itself included),
    or "". Syncthing peers on macOS, iOS and Windows cannot hold two names differing only by case."""
    folded = name.casefold()
    try:
        names = os.listdir(directory)
    except OSError:
        return ""
    for existing in names:
        if existing.casefold() == folded and (directory / existing) not in ignore:
            return existing
    return ""


def _text_chunks(path: Path) -> Iterable[str]:
    """The file's text in overlapping chunks (so a link split at a boundary is still seen, and a huge note
    needs no cap), decoded leniently: a stray byte must never hide a link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return
        carry = b""
        while True:
            block = os.read(fd, LINK_SCAN_CHUNK)
            if not block:
                break
            yield (carry + block).decode("utf-8", "replace")
            carry = block[-LINK_SCAN_OVERLAP:]
    except OSError:
        return
    finally:
        os.close(fd)


def _link_key(target: str) -> str:
    target = target.strip().strip("<>").strip().rstrip("\\")  # `[[x\\|alias]]` inside a table
    if not target or "://" in target or target.lower().startswith(("mailto:", "#")):
        return ""
    target = urllib.parse.unquote(target.split("#", 1)[0].split("?", 1)[0]).rstrip("/")
    name = target.rsplit("/", 1)[-1].strip().lower()
    return name[:-3] if name.endswith(".md") else name


class Sweep:
    def __init__(self, *, dry_run: bool, only: Iterable[str] = (), min_age: Optional[float] = None,
                 limit: Optional[int] = None, now: Optional[float] = None) -> None:
        self.vault = vault_root()
        self.dry_run = dry_run
        self.only = tuple(only) or ACTIONS
        unknown = set(self.only) - set(ACTIONS)
        if unknown:
            raise VaultSweepError(f"unknown action(s): {', '.join(sorted(unknown))}; "
                                  f"choose from {', '.join(ACTIONS)}")
        if limit is not None and limit < 1:
            raise VaultSweepError(f"--limit must be at least 1 (got {limit}); leave it out for no limit")
        self.min_age = min_age_s() if min_age is None else float(min_age)
        self.temp_min_age = max(self.min_age, temp_min_age_s())
        self.max_bytes = max_bytes()
        self.limit = limit
        self.now = time.time() if now is None else now
        self.run = ""
        self.folder = hygiene.Folder(self.vault, self.vault)
        self.zones: list[tuple[Path, str]] = []
        self.code_projects: list[str] = []
        self.files: list[Entry] = []
        self.dirs: dict[Path, float] = {}
        self.claimed: set[Path] = set()
        self.leaving: set[Path] = set()    # left its directory (dry run only: apply shows on disk)
        self.vacated: set[Path] = set()    # no longer there under that name, trashed/moved/renamed
        self.skipped: list[dict[str, str]] = []
        self.rescued = self._rescued()
        self.compiled: Optional[bool] = None
        self.results: dict[str, list[dict[str, Any]]] = {a: [] for a in ACTIONS}
        self.errors: list[str] = []
        self.changes = 0
        self._links: Optional[dict[str, set[str]]] = None
        self._path_refs: dict[str, set[str]] = {}
        self.budget = {"executed": 0, "max": hygiene.UNTITLED_MAX_PER_RUN}

    # ------------------------------------------------------------ exclusions --

    def _build_zones(self) -> None:
        def add(path: Path, why: str) -> None:
            real = Path(os.path.realpath(path))
            if real == self.vault or self.vault in real.parents:
                self.zones.append((real, why))
        add(Path(paths.VAULT), "WAX_VAULT (wax transcripts prune owns it)")
        add(self.vault / "Transcripts", "Transcripts")
        add(self.vault / INBOX / "Wax", "Inbox/Wax (document pipeline)")
        try:
            for binding in documents.bindings():
                add(Path(binding["folder"]), f"pipeline binding {binding['binding_id']}")
        except Exception as exc:  # noqa: BLE001 - an unreadable ledger is a refusal, not "no bindings"
            raise VaultSweepError(f"cannot read document bindings: {exc}") from exc
        for config, key in ((".obsidian/templates.json", "folder"),
                            (".obsidian/plugins/templater-obsidian/data.json", "templates_folder")):
            try:
                folder = json.loads((self.vault / config).read_text(encoding="utf-8")).get(key)
            except (OSError, ValueError, AttributeError):
                folder = None
            if isinstance(folder, str) and folder.strip():
                add(self.vault / folder.strip().strip("/"), "Obsidian templates")
        for part in os.environ.get("WAX_VAULT_SWEEP_EXCLUDE", DEFAULT_EXCLUDE).split(os.pathsep):
            if part.strip():
                add(self.vault / os.path.expanduser(part.strip()), "WAX_VAULT_SWEEP_EXCLUDE")

    def zone(self, path: Path) -> str:
        """Why a path (file or directory) is never touched, or ""."""
        try:
            rel = path.relative_to(self.vault)
        except ValueError:
            return "outside the vault"
        if any(p.startswith(".") for p in rel.parts):
            return "dot path"
        if any(p.startswith("_") for p in rel.parts[:-1]) or (path.is_dir() and rel.name.startswith("_")):
            return "_-prefixed directory"
        for root, why in self.zones:
            if path == root or root in path.parents:
                return why
        return ""

    def refusal(self, path: Path, st: os.stat_result, *, min_age: float) -> str:
        """THE choke point every file action goes through, at plan and at apply time."""
        why = self.zone(path)
        if why:
            return why
        if stat.S_ISLNK(st.st_mode) or os.path.islink(path):
            return "symlink"
        if not stat.S_ISREG(st.st_mode):
            return "not a regular file"
        real_parent = os.path.realpath(path.parent)
        if real_parent != str(self.vault) and not real_parent.startswith(str(self.vault) + os.sep):
            return "real parent outside the vault"
        if st.st_size > self.max_bytes:
            return "too large"
        if self.now - _touched(st) <= min_age:
            return "young"
        if (st.st_ino, st.st_dev, st.st_mtime_ns, st.st_size) in self.rescued:
            return "swept before and put back (kept)"
        return ""

    @staticmethod
    def _rescued() -> set[tuple]:
        """(inode, device, mtime, size) of every file an earlier run trashed or moved that is no longer
        where the sweep put it: ``unsweep`` or the operator moved it back, so it is kept, not swept again."""
        entries = read_manifest()
        void = {(e.get("from"), e.get("to")) for e in entries if e.get("op") in ("aborted", "reverted")}
        restored = {(e.get("from"), e.get("to")) for e in entries if e.get("op") == "restore"}
        keep: set[tuple] = set()
        for e in entries:
            op, pair = e.get("op"), (e.get("from"), e.get("to"))
            if op == "restore" and e.get("ino") is not None:
                keep.add((e["ino"], e["dev"], e["mtime_ns"], e["size"]))
            elif (e.get("ino") is not None and pair not in void and
                  (op == "trash" or (op in ("rename", "move") and pair in restored))):
                keep.add((e["ino"], e["dev"], e["mtime_ns"], e["size"]))
        return keep

    # ------------------------------------------------------------------ walk --

    def walk(self) -> None:
        self._build_zones()
        for dirpath, dirnames, filenames in os.walk(self.vault, followlinks=False):
            here = Path(dirpath)
            if here != self.vault:
                if CODE_MARKERS & (set(dirnames) | set(filenames)):
                    self.zones.append((here, "code project"))
                    self.code_projects.append(os.path.relpath(here, self.vault))
                    dirnames[:] = []
                    continue
                try:
                    self.dirs[here] = _touched(os.lstat(here))
                except OSError:
                    pass
            keep = []
            for name in dirnames:
                sub = here / name
                if name.startswith((".", "_")) or name in NEVER_DESCEND or os.path.islink(sub):
                    continue
                if not _utf8(name):
                    self.skipped.append({"path": _printable(self.rel(sub)), "reason": "name is not valid UTF-8"})
                    continue
                if self.zone(sub):
                    continue
                keep.append(name)
            dirnames[:] = sorted(keep)
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                path = here / name
                if not _utf8(name):
                    self.skipped.append({"path": _printable(self.rel(path)), "reason": "name is not valid UTF-8"})
                    continue
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    self.files.append(Entry(path, st))

    def links(self) -> dict[str, set[str]]:
        """base-name key -> files that link to it, over the WHOLE vault (excluded zones too) plus the
        ``.obsidian`` plugin configs. Over-matching only ever holds a file, so every doubt reads as linked."""
        if self._links is not None:
            return self._links
        index: dict[str, set[str]] = {}

        def scan(path: Path, regexes: tuple) -> None:
            rel = _printable(os.path.relpath(path, self.vault))
            for text in _text_chunks(path):
                for regex in regexes:
                    for m in regex.finditer(text):
                        key = _link_key(m.group(1))
                        if key:
                            index.setdefault(key, set()).add(rel)

        for dirpath, dirnames, filenames in os.walk(self.vault, followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in NEVER_DESCEND]
            for name in filenames:
                if name.startswith("."):
                    continue
                low = name.lower()
                if low.endswith(".md"):
                    scan(Path(dirpath) / name, (WIKILINK_RE, MDLINK_RE, HTMLLINK_RE))
                elif low.endswith(".canvas"):
                    scan(Path(dirpath) / name, (CANVAS_FILE_RE, WIKILINK_RE))
                elif low.endswith(".base"):
                    scan(Path(dirpath) / name, (QUOTED_RE, WIKILINK_RE))
        config = self.vault / ".obsidian"
        for dirpath, dirnames, filenames in os.walk(config, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in NEVER_DESCEND and d not in OBSIDIAN_HISTORY_PLUGINS]
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    small = os.lstat(path).st_size <= OBSIDIAN_CONFIG_MAX_BYTES
                except OSError:
                    continue
                if name.endswith(".json") and small and not name.startswith(OBSIDIAN_STATE_PREFIXES):
                    self._scan_config(path, index)
        self._links = index
        return index

    def _scan_config(self, path: Path, index: dict[str, set[str]]) -> None:
        """A plugin or bookmark config names a note as a quoted path (``"Inbox/tmp.md"``, exact) or file name
        (``"tmp.md"``, by name). A bare word (``"Untitled"``) is a label, not a reference."""
        rel = _printable(os.path.relpath(path, self.vault))
        exact = path.name in OBSIDIAN_PATH_CONFIGS and path.parent == self.vault / ".obsidian"
        for text in _text_chunks(path):
            for m in QUOTED_RE.finditer(text):
                value = m.group(1).strip()
                if "://" in value or not FILE_EXT_RE.search(value):
                    continue
                if exact or "/" in value:
                    self._path_refs.setdefault(value.strip("/").lower(), set()).add(rel)
                else:
                    index.setdefault(_link_key(value), set()).add(rel)

    def linked_from(self, path: Path) -> str:
        rel = os.path.relpath(path, self.vault)
        keys = {path.name.lower()}
        if path.suffix.lower() == ".md":
            keys.add(path.stem.lower())
        sources = {s for k in keys for s in self.links().get(k, ())} | self._path_refs.get(rel.lower(), set())
        sources.discard(rel)
        return min(sources) if sources else ""

    # --------------------------------------------------------------- results --

    def rel(self, path: Path) -> str:
        return os.path.relpath(path, self.vault)

    def note(self, action: str, path: Path, outcome: str, *, detail: str = "", to: Optional[Path] = None,
             claim: bool = True) -> dict[str, Any]:
        item: dict[str, Any] = {"path": _printable(self.rel(path)), "outcome": outcome}
        if detail:
            item["detail"] = _printable(detail)
        if to is not None:
            item["to"] = _printable(self.rel(to) if (to == self.vault or self.vault in to.parents) else str(to))
        self.results[action].append(item)
        if claim:
            self.claimed.add(path)
        if outcome in CHANGES or outcome in WOULD:
            self.changes += 1
        if claim and outcome in MOVED_AWAY:
            self.vacated.add(path)
        return item

    def budget_left(self) -> bool:
        return self.limit is None or self.changes < self.limit

    def guard(self, action: str, path: Path, fn, *args) -> None:
        """Run one file's step; whatever it raises is that file's error line, never the run's end."""
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the sweep
            log.warning("vault sweep %s failed on %s: %s: %s", action, path, type(exc).__name__, exc)
            self.errors.append(f"{_printable(self.rel(path))}: {type(exc).__name__}: {exc}")
            self.note(action, path, "error", detail=f"{type(exc).__name__}: {exc}"[:160])

    def occupied(self, directory: Path, name: str) -> str:
        """An entry in ``directory`` that ``name`` would collide with (case-insensitively), ignoring
        names this run has already moved away."""
        return casefold_clash(directory, name, ignore=frozenset(self.vacated))

    # ------------------------------------------------------------- executing --

    def _journal(self, entry: dict[str, Any]) -> None:
        _append_manifest({"v": 1, "run": self.run, "at": sentinel.utcnow(), **entry})

    def _base_entry(self, action: str, op: str, path: Path, st: os.stat_result, reason: str,
                    detail: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            digest: Optional[str] = documents.sha256_file(path)
        except OSError:
            digest = None
        return {"action": action, "op": op, "from": str(path), "ino": st.st_ino, "dev": st.st_dev,
                "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest, "reason": reason,
                "detail": detail, "documents": [[r["binding_id"], r["doc_id"], r["state"]] for r in rows]}

    def execute(self, action: str, entry: Entry, op: str, *, reason: str, detail: str = "",
                dst: Optional[Path] = None, min_age: Optional[float] = None) -> dict[str, Any]:
        """One journaled trash ("trash") or exact move ("move"/"rename"), under the run locks.
        Anything unexpected is this file's error line; the rest of the run carries on."""
        try:
            return self._execute(action, entry, op, reason=reason, detail=detail, dst=dst, min_age=min_age)
        except Exception as exc:  # noqa: BLE001
            log.warning("vault sweep %s failed on %s: %s: %s", action, entry.path, type(exc).__name__, exc)
            self.errors.append(f"{_printable(self.rel(entry.path))}: {type(exc).__name__}: {exc}")
            return self.note(action, entry.path, "error", detail=f"{type(exc).__name__}: {exc}"[:160])

    def _execute(self, action: str, entry: Entry, op: str, *, reason: str, detail: str,
                 dst: Optional[Path], min_age: Optional[float]) -> dict[str, Any]:
        path = entry.path
        min_age = self.min_age if min_age is None else min_age
        rows = documents.rows_for_path(path)
        if self.dry_run:
            if any(documents.run_lock_held(r["binding_id"], r["doc_id"]) for r in rows):
                return self.note(action, path, "locked")
            target = hygiene.trash_destination(path, self.folder) if op == "trash" else dst
            if op == "trash" or (dst is not None and dst.parent != path.parent):
                self.leaving.add(path)
            return self.note(action, path, "would_trash" if op == "trash" else
                             ("would_move" if op == "move" else "would_rename"), detail=detail, to=target)
        with documents.hold_run_locks(rows) as held:
            if not held:
                return self.note(action, path, "locked")
            try:
                now_st = os.lstat(path)
            except FileNotFoundError:
                return self.note(action, path, "vanished")
            why = self.refusal(path, now_st, min_age=min_age)
            if why or _sig(now_st) != _sig(entry.st):
                return self.note(action, path, "changed", detail=why or "modified since planned")
            if hygiene._row_keys(documents.rows_for_path(path)) != hygiene._row_keys(rows):
                return self.note(action, path, "changed", detail="ledger rows changed")
            base = self._base_entry(action, op, path, now_st, reason, detail, rows)
            attempted: list[Path] = []

            def hook(cand: Path) -> None:
                attempted.append(cand)
                self._journal({**base, "to": str(cand)})

            try:
                if op == "trash":
                    used, restored = hygiene._move_to_trash(path, self.folder, now_st, on_destination=hook)
                    if restored:
                        self._journal({"action": action, "op": "reverted", "from": str(path),
                                       "to": str(attempted[-1]) if attempted else str(used)})
                        return self.note(action, path, "changed", detail="written during the move; put back")
                else:
                    assert dst is not None
                    if self.zone(dst):
                        return self.note(action, path, "refused", detail=f"destination {self.zone(dst)}")
                    hook(dst)
                    rename.renameat2_noreplace(path, dst)
                    used = dst
                    after = os.lstat(used)
                    if _sig(after) != _sig(now_st):
                        back = rename.move_noclobber(used, path)
                        self._journal({"action": action, "op": "reverted", "from": str(path), "to": str(dst),
                                       "detail": f"put back at {back}"})
                        return self.note(action, path, "changed", detail="written during the move; put back")
            except rename.RenameCollision:
                if attempted:
                    self._journal({"action": action, "op": "aborted", "from": str(path), "to": str(attempted[-1])})
                return self.note(action, path, "collision", detail=f"{self.rel(dst) if dst else ''} exists")
            except FileNotFoundError:
                if attempted:
                    self._journal({"action": action, "op": "aborted", "from": str(path), "to": str(attempted[-1])})
                return self.note(action, path, "vanished")
            except (OSError, hygiene.HygieneError) as exc:
                if attempted:
                    self._journal({"action": action, "op": "aborted", "from": str(path), "to": str(attempted[-1])})
                self.errors.append(f"{_printable(self.rel(path))}: {type(exc).__name__}: {exc}")
                return self.note(action, path, "error", detail=str(exc)[:160])
            try:
                for binding_id, doc_id in sorted(hygiene._row_keys(rows)):
                    if op == "trash":
                        documents.mark_state(binding_id, doc_id, "trashed")
                    else:
                        documents.repoint(binding_id, doc_id, used, base["sha256"])
            except Exception as exc:  # noqa: BLE001 - the file DID move and is journaled; say so, carry on
                self.errors.append(f"{_printable(self.rel(path))}: moved to {self.rel(used)} but the documents "
                                   f"ledger was not updated: {type(exc).__name__}: {exc}")
            log.info("vault sweep %s: %s %s -> %s", action, op, path, used)
            return self.note(action, path, {"trash": "trashed", "move": "moved"}.get(op, "renamed"),
                             detail=detail, to=used)

    # --------------------------------------------------------------- actions --

    def unclaimed(self) -> Iterable[Entry]:
        return [e for e in self.files if e.path not in self.claimed]

    def act_prune_empty(self) -> None:
        self.stubs = self._template_stubs()
        for e in self.unclaimed():
            self.guard("prune-empty", e.path, self._prune_empty, e)

    def _template_stubs(self) -> set[Path]:
        """Unfilled Templater stubs whose every unchecked task is boilerplate its folder shares."""
        found: dict[Path, list[str]] = {}
        for e in self.unclaimed():
            if e.path.suffix == ".md" and 0 < e.st.st_size <= TEMPLATE_STUB_MAX_BYTES:
                text = _text(e.path)
                tasks = template_stub_tasks(text) if text is not None else None
                if tasks is not None:
                    found[e.path] = tasks
        shared: dict[tuple[Path, str], int] = {}
        for path, tasks in found.items():
            for task in set(tasks):
                shared[(path.parent, task)] = shared.get((path.parent, task), 0) + 1
        return {path for path, tasks in found.items()
                if all(shared[(path.parent, t)] >= TEMPLATE_BOILERPLATE_MIN for t in tasks)}

    def _prune_empty(self, e: Entry) -> None:
        if e.st.st_size == 0:
            if e.path.name in KEEP_EMPTY_NAMES:
                return  # a package marker is empty on purpose
            reason = "zero bytes"
        elif e.path in self.stubs:
            reason = "unfilled template"
        elif e.path.suffix == ".md" and e.st.st_size <= hygiene.MAX_EMPTY_BYTES:
            text = _text(e.path)
            if text is None or not strictly_empty(text):
                return
            reason = "empty note"
        else:
            return
        why = self.refusal(e.path, e.st, min_age=self.min_age)
        source = "" if why else self.linked_from(e.path)
        if why or source:
            self.note("prune-empty", e.path, "held" if why else "linked", detail=why or f"linked from {source}")
        elif self.budget_left():
            self.execute("prune-empty", e, "trash", reason=reason, detail=reason)

    def act_prune_temp(self) -> None:
        for e in self.unclaimed():
            self.guard("prune-temp", e.path, self._prune_temp, e)

    def _prune_temp(self, e: Entry) -> None:
        match = temp_match(e.path.name)
        if match is None:
            return
        kind, reason = match
        why = self.refusal(e.path, e.st, min_age=self.temp_min_age)
        if why:
            self.note("prune-temp", e.path, "held", detail=why)
            return
        if kind == "backup":
            original = backup_original(e.path.name) or ""
            if not os.path.lexists(e.path.parent / original):
                self.note("prune-temp", e.path, "report", claim=False,
                          detail=f"backup, but {original} is not beside it: the only copy, left alone")
                return
        elif kind == "name":
            # Operator order 2026-11-10: temp and scratch files go, whatever
            # they hold ("I don't want any of them"). The whole stem must match
            # (``tmp``, ``scratch-2``, ``notes-scratch``), never a title that
            # merely contains the word, and the link guard below still holds a
            # file something links to. The trash and `wax vault unsweep` undo it.
            text = _text(e.path) if e.path.suffix.lower() in (".md", ".txt") else None
            reason = "temp/scratch name, blank" if text is not None and stamp_only(text) else "temp/scratch name"
        source = self.linked_from(e.path)
        if source:
            self.note("prune-temp", e.path, "linked", detail=f"linked from {source}")
        elif self.budget_left():
            self.execute("prune-temp", e, "trash", reason=reason, detail=reason, min_age=self.temp_min_age)

    def act_untitled(self) -> None:
        candidates = [e for e in self.unclaimed() if UNTITLED_STEM_RE.match(e.path.stem)]
        self.compiled: Optional[bool] = None
        for e in candidates:
            self.guard("untitled", e.path, self._untitled, e)

    def _untitled(self, e: Entry) -> None:
        why = self.refusal(e.path, e.st, min_age=self.min_age)
        if why:
            self.note("untitled", e.path, "held", detail=why)
            return
        suffix = e.path.suffix.lower()
        text = _text(e.path) if suffix in (".md", ".base", ".canvas") else None
        if suffix == ".md":
            if text is None:
                self.note("untitled", e.path, "report", detail="unreadable or large", claim=False)
                return
            if stamp_only(text):
                source = self.linked_from(e.path)
                if source:
                    self.note("untitled", e.path, "linked", detail=f"linked from {source}")
                elif self.budget_left():
                    self.execute("untitled", e, "trash", reason="untitled stub", detail="stamp-only note")
                return
            if hygiene.classify_text(text) == "unparseable":
                self.note("untitled", e.path, "report", detail="frontmatter does not parse", claim=False)
                return
            source = self.linked_from(e.path)
            if source:
                self.note("untitled", e.path, "linked", detail=f"linked from {source}")
                return
            if self.compiled is None:
                self.compiled = hygiene.UNTITLED_PIPELINE in pipelines.compiled()
                if not self.compiled:
                    self.errors.append(f"pipeline '{hygiene.UNTITLED_PIPELINE}' is not compiled; run "
                                       f"`wax pipeline compile {hygiene.UNTITLED_PIPELINE}`")
            if not self.compiled:
                self.note("untitled", e.path, "refused", detail="untitled-standard not compiled")
            elif self.budget_left():
                self.title(e)
        elif (suffix == ".base" and text is not None and default_base(text)) or \
                (suffix == ".canvas" and text is not None and empty_canvas(text)):
            source = self.linked_from(e.path)
            if source:
                self.note("untitled", e.path, "linked", detail=f"linked from {source}")
            elif self.budget_left():
                self.execute("untitled", e, "trash", reason="untitled default", detail=f"default {suffix}")
        else:
            self.note("untitled", e.path, "report", detail="untitled, not a note", claim=False)

    def _preimage(self, path: Path, st: os.stat_result) -> tuple[Path, str]:
        """Copy the note aside (and journal it) BEFORE the pipeline rewrites it, so ``unsweep`` can put it
        back byte for byte. Raises when it cannot: no pre-image, no rewrite."""
        trash_root = self.vault / hygiene.TRASH_DIR
        if trash_root.is_symlink():
            raise VaultSweepError(f"{trash_root} is a symlink; refusing to write a pre-image through it")
        data = _read_bytes(path, self.max_bytes)
        if data is None:
            raise VaultSweepError("cannot read the note to keep a pre-image")
        dst = trash_root / PREIMAGE_DIR / self.run / os.path.relpath(path, self.vault)
        dst.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        self._journal({"action": "untitled", "op": "preimage", "from": str(path), "to": str(dst),
                       "ino": st.st_ino, "dev": st.st_dev, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                       "sha256": digest})
        with open(dst, "xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
        return dst, digest

    def title(self, e: Entry) -> None:
        path = e.path
        if self.dry_run:
            self.note("untitled", path, "would_title", detail=hygiene.UNTITLED_PIPELINE)
            return
        if self.budget["executed"] >= self.budget["max"]:
            self.note("untitled", path, "deferred", detail=f"WAX_UNTITLED_SWEEP_MAX={self.budget['max']}")
            return
        try:  # the plan can be minutes old and a pipeline run is slow: look again before touching the note
            fresh = os.lstat(path)
        except OSError:
            self.note("untitled", path, "vanished")
            return
        why = self.refusal(path, fresh, min_age=self.min_age)
        if why or _sig(fresh) != _sig(e.st):
            self.note("untitled", path, "changed", detail=why or "modified since planned")
            return
        pre, pre_sha = self._preimage(path, fresh)
        try:
            titled = hygiene.summarize_for_title(path, self.folder, self.budget)
        except Exception as exc:  # noqa: BLE001 - one note's failure is an error line, not a dead sweep
            self._journal_rewrite(path, pre, pre_sha, fresh)
            self.errors.append(f"{_printable(self.rel(path))}: {type(exc).__name__}: {exc}")
            self.note("untitled", path, "error", detail=str(exc)[:160])
            return
        self._journal_rewrite(path, pre, pre_sha, fresh)
        if titled["outcome"] != "titled":
            if titled["outcome"] == "failed":
                self.errors.append(f"{_printable(self.rel(path))}: untitled-standard failed: "
                                   f"{titled.get('detail', '')}")
            self.note("untitled", path, titled["outcome"], detail=titled.get("detail", ""))
            return
        slug = titled["slug"]
        name = hygiene.safe_title_name(slug)
        if name is None or UNTITLED_STEM_RE.match(name[:-3]):
            self.note("untitled", path, "unsafe_title", detail=repr(slug)[:80])
            return
        clash = self.occupied(path.parent, name)
        if clash and clash != path.name and clash != name:
            self.note("untitled", path, "collision", detail=f"{clash} exists (differs only by case)")
            return
        if self.zone(path) or os.path.islink(path):
            self.note("untitled", path, "changed", detail=self.zone(path) or "symlink")
            return
        rows = documents.rows_for_path(path)
        try:
            st = os.lstat(path)
            base = self._base_entry("untitled", "rename", path, st, "untitled", f"title-slug {slug}", rows)
        except OSError:
            self.note("untitled", path, "vanished")
            return
        attempted: list[Path] = []

        def hook(cand: Path) -> None:
            attempted.append(cand)
            self._journal({**base, "to": str(cand)})

        try:
            renamed = hygiene.rename_note(path, self.folder, slug, on_destination=hook)
        except (OSError, rename.RenameCollision) as exc:
            if attempted:
                self._journal({"action": "untitled", "op": "aborted", "from": str(path), "to": str(attempted[-1])})
            self.errors.append(f"{_printable(self.rel(path))}: {type(exc).__name__}: {exc}")
            self.note("untitled", path, "error", detail=str(exc)[:160])
            return
        if renamed["outcome"] == "renamed":
            self.note("untitled", path, "titled_renamed", to=Path(renamed["to"]))
        else:
            if attempted:
                self._journal({"action": "untitled", "op": "aborted", "from": str(path), "to": str(attempted[-1])})
            self.note("untitled", path, "summarized_" + renamed["outcome"], detail=renamed.get("detail", ""))

    def _journal_rewrite(self, path: Path, pre: Path, pre_sha: str, before: os.stat_result) -> None:
        """Record what the pipeline left at ``path``, so ``unsweep`` knows when the pre-image may go back."""
        try:
            post = documents.sha256_file(path)
        except OSError:
            return
        if post != pre_sha:
            self._journal({"action": "untitled", "op": "rewrote", "from": str(path), "to": str(pre),
                           "sha256": post, "pre_sha256": pre_sha, "mtime_ns": before.st_mtime_ns,
                           "atime_ns": before.st_atime_ns})

    def act_consolidate(self) -> None:
        by_dir: dict[Path, dict[str, Entry]] = {}
        for e in self.files:
            by_dir.setdefault(e.path.parent, {})[e.path.name] = e
        for folder in sorted(by_dir):
            self.guard("consolidate", folder, self._consolidate_folder, folder, by_dir[folder])

    def _consolidate_folder(self, folder: Path, names: dict[str, Entry]) -> None:
        groups: dict[tuple[str, str], list[tuple[Entry, str]]] = {}
        for name, e in sorted(names.items()):
            if e.path in self.claimed:
                continue
            marked = copy_marker(e.path.stem)
            if marked:
                groups.setdefault((marked[0], e.path.suffix), []).append((e, marked[1]))
        grouped: set[Path] = set()
        for (base, suffix), copies in sorted(groups.items()):
            original_path = folder / (base + suffix)
            original = names.get(original_path.name)
            if original is not None and original.path in self.claimed and original.path in self.vacated:
                original = None  # an earlier action trashed or moved it: it is not there to compare with
            if original is None and os.path.islink(original_path):
                for e, _ in copies:
                    self.note("consolidate", e.path, "report", detail="original is a symlink", claim=False)
                continue
            if original is None:
                # "Week 2" is a copy of "Week" only while "Week" exists: numbered siblings are not copies
                copies = [c for c in copies if c[1] != "n"]
                if not copies:
                    continue
            grouped.update(e.path for e, _ in copies)
            self._consolidate_group(original_path, original, copies)
        self._identical_names(folder, names, grouped)

    def _consolidate_group(self, original_path: Path, original: Optional[Entry],
                           copies: list[tuple[Entry, str]]) -> None:
        if original is not None:
            survivor = original
            rest = copies
        else:
            ordered = sorted(copies, key=lambda c: (c[0].st.st_mtime, c[0].path.name))
            survivor, rest = ordered[0][0], ordered[1:]
        remaining = [survivor] if original is None else []
        for e, kind in rest:
            same = identical(e.path, survivor.path, self.max_bytes)
            if same is None:
                remaining.append(e)
                self.note("consolidate", e.path, "report", claim=False,
                          detail=f"could not read {survivor.path.name} or this file to compare them")
                continue
            if not same:
                remaining.append(e)
                self.note("consolidate", e.path, "report", detail=f"differs from {survivor.path.name}", claim=False)
                continue
            why = self.refusal(e.path, e.st, min_age=self.min_age)
            source = "" if why else self.linked_from(e.path)
            if why or source:
                remaining.append(e)
                self.note("consolidate", e.path, "held" if why else "linked", detail=why or f"linked from {source}")
            elif self.budget_left():
                self.execute("consolidate", e, "trash", reason="duplicate", detail=f"same as {survivor.path.name}")
        if original is not None or len(remaining) != 1:
            return
        lone = remaining[0]
        kind = next((k for e, k in copies if e.path == lone.path), "n")
        if kind != "conflict":
            # "Marketing copy" and "Sprint (2)" are titles people chose, not Syncthing's leftovers
            self.note("consolidate", lone.path, "report", claim=False,
                      detail=f"{original_path.name} is missing; this is not a sync conflict, left as it is")
            return
        why = self.refusal(lone.path, lone.st, min_age=self.min_age)
        source = "" if why else self.linked_from(lone.path)
        clash = "" if why or source else self.occupied(original_path.parent, original_path.name)
        if why or source:
            self.note("consolidate", lone.path, "held" if why else "linked", detail=why or f"linked from {source}")
        elif clash:
            self.note("consolidate", lone.path, "collision", detail=f"{clash} exists")
        elif self.budget_left():
            self.execute("consolidate", lone, "rename", reason="lone conflict copy", dst=original_path,
                         detail=f"only copy of missing {original_path.name}")

    def _identical_names(self, folder: Path, names: dict[str, Entry], grouped: set[Path]) -> None:
        by_size: dict[int, list[Entry]] = {}
        for e in names.values():
            if (e.path.suffix == ".md" and e.path not in grouped and e.path not in self.claimed
                    and 0 < e.st.st_size <= IDENTICAL_MAX_BYTES):
                by_size.setdefault(e.st.st_size, []).append(e)
        for same in by_size.values():
            if len(same) < 2:
                continue
            seen: dict[bytes, list[Entry]] = {}
            for e in sorted(same, key=lambda x: x.path.name):
                data = _read_bytes(e.path, IDENTICAL_MAX_BYTES)
                if data is None:
                    continue
                try:
                    if stamp_only(data.decode("utf-8")):
                        continue
                except UnicodeDecodeError:
                    pass
                seen.setdefault(data, []).append(e)
            for group in seen.values():
                if len(group) < 2:
                    continue
                others = [g.path.name for g in group[1:]]
                shown = ", ".join(others[:3]) + (f" and {len(others) - 3} more" if len(others) > 3 else "")
                self.note("consolidate", group[0].path, "report", claim=False,
                          detail=f"identical content under {len(others)} other name(s): {shown}")

    def act_organize(self) -> None:
        for e in self.unclaimed():
            if e.path.parent == self.vault and e.path.suffix == ".md":
                self.guard("organize", e.path, self._organize_note, e)
        try:
            self._empty_dirs()
        except Exception as exc:  # noqa: BLE001 - the notes above are done; say what failed
            log.warning("vault sweep organize: empty directories failed: %s: %s", type(exc).__name__, exc)
            self.errors.append(f"organize (empty directories): {type(exc).__name__}: {exc}")

    def _organize_note(self, e: Entry) -> None:
        inbox = self.vault / INBOX
        stem = e.path.stem
        if ALL_CAPS_RE.match(stem) or stem.lower() in ROOT_KEEP:
            return
        why = self.refusal(e.path, e.st, min_age=self.min_age)
        source = "" if why else self.linked_from(e.path)
        dst = inbox / e.path.name
        if why or source:
            self.note("organize", e.path, "held" if why else "linked", detail=why or f"linked from {source}")
        elif not inbox.is_dir() or inbox.is_symlink():
            self.note("organize", e.path, "held", detail=f"{INBOX}/ is missing")
        elif self.occupied(inbox, e.path.name):
            self.note("organize", e.path, "collision",
                      detail=f"{INBOX}/{self.occupied(inbox, e.path.name)} exists (names compare ignoring case)")
        elif self.budget_left():
            self.execute("organize", e, "move", reason="loose root note", dst=dst, detail=f"to {INBOX}/")

    def _protected_dir(self, d: Path) -> str:
        if d == self.vault or d.parent == self.vault:
            return "top level"
        return self.zone(d)

    def _empty_dirs(self) -> None:
        empty: dict[Path, bool] = {}
        for d in sorted(self.dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                names = os.listdir(d)
            except OSError:
                empty[d] = False
                continue
            ok = True
            for name in names:
                child = d / name
                if child in self.leaving:
                    continue
                if empty.get(child) and not os.path.islink(child):
                    continue
                ok = False
                break
            empty[d] = ok
        for d in sorted(empty, key=lambda p: (-len(p.parts), str(p))):
            if not empty[d]:
                continue
            why = self._protected_dir(d)
            if why:
                continue
            if self.now - self.dirs[d] <= self.min_age:
                self.note("organize", d, "held", detail="young directory", claim=False)
                empty_parent = d.parent
                while empty_parent in empty:
                    empty[empty_parent] = False
                    empty_parent = empty_parent.parent
                continue
            if not self.budget_left():
                continue
            if self.dry_run:
                self.leaving.add(d)
                self.note("organize", d, "would_remove", detail="empty directory", claim=False)
                continue
            self._journal({"action": "organize", "op": "rmdir", "from": str(d), "to": "",
                           "reason": "empty directory"})
            try:
                os.rmdir(d)
            except OSError as exc:
                self._journal({"action": "organize", "op": "aborted", "from": str(d), "to": ""})
                self.note("organize", d, "held", detail=f"not removed: {exc.strerror}", claim=False)
                continue
            self.note("organize", d, "removed", detail="empty directory", claim=False)

    # ------------------------------------------------------------------ run ---

    def go(self) -> dict[str, Any]:
        started = time.time()
        self.walk()
        steps = {"prune-empty": self.act_prune_empty, "prune-temp": self.act_prune_temp,
                 "untitled": self.act_untitled, "consolidate": self.act_consolidate,
                 "organize": self.act_organize}
        for action in ACTIONS:
            if action in self.only:
                try:
                    steps[action]()
                except Exception as exc:  # noqa: BLE001 - the other actions still run, and the run id still lands
                    log.exception("vault sweep: %s failed", action)
                    self.errors.append(f"{action}: {type(exc).__name__}: {exc}")
        counts = {a: {} for a in self.only}
        for action in self.only:
            for item in self.results[action]:
                counts[action][item["outcome"]] = counts[action].get(item["outcome"], 0) + 1
        return {
            "dry_run": self.dry_run, "run": self.run or None, "vault": str(self.vault),
            "trash": str(self.vault / hygiene.TRASH_DIR), "manifest": str(manifest_path()),
            "only": list(self.only), "min_age": self.min_age, "temp_min_age": self.temp_min_age,
            "max_bytes": self.max_bytes, "limit": self.limit, "changes": self.changes,
            "limit_reached": self.limit is not None and self.changes >= self.limit,
            "counts": counts, "actions": {a: self.results[a] for a in self.only},
            "excluded": {"zones": sorted({self.rel(z) for z, why in self.zones if why != "code project"}),
                         "code_projects": sorted(self.code_projects)},
            "skipped": self.skipped,
            "files_seen": len(self.files), "errors": self.errors,
            "seconds": round(time.time() - started, 2),
        }


class _RunLock:
    def __init__(self) -> None:
        self.path = paths.VAR / LOCK_NAME
        self.fh = None

    def __enter__(self) -> "_RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.fh.close()
            raise VaultSweepError(f"another vault sweep or unsweep holds {self.path}") from exc
        return self

    def __exit__(self, *exc) -> None:
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def sweep(*, apply: bool = False, only: Iterable[str] = (), min_age: Optional[float] = None,
          limit: Optional[int] = None, now: Optional[float] = None) -> dict[str, Any]:
    """Plan (and with ``apply``, carry out) one vault sweep. Raises VaultSweepError on a refusal."""
    job = Sweep(dry_run=not apply, only=only, min_age=min_age, limit=limit, now=now)
    if not apply:
        return job.go()
    with _RunLock():
        job.run = _new_run_id()
        summary = job.go()
    try:
        ledger.meta_set(META_LAST_RUN, json.dumps({
            "run": summary["run"], "at": sentinel.utcnow(), "changes": summary["changes"],
            "errors": len(summary["errors"]), "counts": summary["counts"]}, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - the changes are made and journaled; the operator must still get the run id
        summary["errors"].append(f"could not record {META_LAST_RUN}: {type(exc).__name__}: {exc}")
    return summary


def _restore_entry(run: str, e: dict[str, Any], final: Path, *, note: str = "") -> None:
    """Journal one undone change, with the file's identity now, so the next sweep keeps it."""
    row: dict[str, Any] = {"v": 1, "run": run, "at": sentinel.utcnow(), "op": "restore",
                           "action": e.get("action"), "from": e["from"], "to": e.get("to") or "",
                           "restored_to": str(final)}
    try:
        st = os.lstat(final)
        row.update(ino=st.st_ino, dev=st.st_dev, size=st.st_size, mtime_ns=st.st_mtime_ns)
    except OSError:
        pass
    if note:
        row["detail"] = note
    _append_manifest(row)


def _put_back_content(e: dict[str, Any]) -> tuple[Optional[Path], str]:
    """Undo one ``rewrote`` line: the untitled pipeline edited the note, and its pre-image goes back over it,
    but only while the note is still byte for byte what the pipeline wrote (else nobody's edit is lost).
    Returns (the note, "") when restored, else (None, why not)."""
    note, pre = Path(e["from"]), Path(e["to"])
    try:
        st = os.lstat(note)
    except OSError:
        return None, f"{note} is gone"
    if not stat.S_ISREG(st.st_mode):
        return None, f"{note} is not a regular file"
    if documents.sha256_file(note) != e.get("sha256"):
        return None, f"edited since the sweep; its pre-image stays at {pre}"
    data = _read_bytes(pre, 1 << 40)
    if data is None or hashlib.sha256(data).hexdigest() != e.get("pre_sha256"):
        return None, f"pre-image {pre} is missing or changed"
    staged = note.with_name(f".wax-vault-unsweep-{note.name}")
    with open(staged, "xb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.utime(staged, ns=(e.get("atime_ns") or e["mtime_ns"], e["mtime_ns"]))
    after = os.lstat(note)
    if (after.st_ino, after.st_size, after.st_mtime_ns) != (st.st_ino, st.st_size, st.st_mtime_ns):
        staged.unlink(missing_ok=True)
        return None, "written while it was being restored; left as it is"
    os.replace(staged, note)
    digest = hashlib.sha256(data).hexdigest()
    for row in documents.rows_for_path(note):
        documents.repoint(row["binding_id"], row["doc_id"], note, digest)
    return note, ""


def restore(*, run: str, dry_run: bool = False) -> dict[str, Any]:
    """Undo one apply run from the manifest, newest change first (never over a file now there)."""
    if not run:
        raise VaultSweepError("name a --run")
    entries = [(i, e) for i, e in enumerate(read_manifest()) if e.get("run") == run]
    if not entries:
        raise VaultSweepError(f"no vault-sweep run {run!r} in {manifest_path()}")
    void = {(e.get("from"), e.get("to")) for _, e in entries if e.get("op") in ("aborted", "reverted")}
    restored_before = {(e.get("from"), e.get("to")) for _, e in entries if e.get("op") == "restore"}
    latest: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    for i, e in entries:
        if e.get("op") in ("trash", "rename", "move", "rmdir", "rewrote") and (e.get("from"), e.get("to")) not in void:
            latest[(e["from"], e["op"])] = (i, e)
    out: dict[str, Any] = {"dry_run": dry_run, "run": run, "restored": [], "skipped": [], "errors": []}
    lock = _RunLock() if not dry_run else None
    if lock:
        lock.__enter__()
    try:
        for _, e in sorted(latest.values(), key=lambda x: x[0], reverse=True):
            src, to, op = e["from"], e.get("to") or "", e["op"]
            if (src, to) in restored_before:
                out["skipped"].append({"path": src, "detail": "already restored"})
                continue
            try:
                if op == "rewrote":
                    if dry_run:
                        out["restored"].append({"path": src, "from": to, "op": op, "would": True})
                        continue
                    note, why = _put_back_content(e)
                    if note is None:
                        out["skipped"].append({"path": src, "detail": why})
                        continue
                    _restore_entry(run, e, note)
                    out["restored"].append({"path": src, "from": to, "op": op})
                    continue
                if op == "rmdir":
                    if dry_run:
                        out["restored"].append({"path": src, "op": op, "would": True})
                        continue
                    os.makedirs(src, exist_ok=True)
                    _restore_entry(run, e, Path(src))
                    out["restored"].append({"path": src, "op": op})
                    continue
                moved = Path(to)
                try:
                    st = os.lstat(moved)
                except OSError:
                    out["skipped"].append({"path": src, "detail": f"{to} is gone"})
                    continue
                if not stat.S_ISREG(st.st_mode) or (st.st_ino, st.st_dev) != (e.get("ino"), e.get("dev")):
                    out["skipped"].append({"path": src, "detail": f"{to} is no longer the file the sweep moved"})
                    continue
                if dry_run:
                    out["restored"].append({"path": src, "from": to, "op": op, "would": True})
                    continue
                Path(src).parent.mkdir(parents=True, exist_ok=True)
                used = rename.move_noclobber(moved, Path(src))
                for binding_id, doc_id, *prior in e.get("documents") or []:
                    if op == "trash":
                        documents.mark_state(binding_id, doc_id, (prior and prior[0]) or "processed")
                    documents.repoint(binding_id, doc_id, used, None)
                _restore_entry(run, e, used)
                out["restored"].append({"path": str(used), "from": to, "op": op})
            except Exception as exc:  # noqa: BLE001 - one failed undo must not strand the rest of the run
                out["errors"].append(f"{src}: {type(exc).__name__}: {exc}")
    finally:
        if lock:
            lock.__exit__(None, None, None)
    return out
