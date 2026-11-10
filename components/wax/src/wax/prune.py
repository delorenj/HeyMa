"""Prune junk transcripts out of the vault (``wax transcripts prune``).

The transcripts folder (``WAX_VAULT``, ``~/d/Transcripts``) collects a lot of
notes nobody will ever read: Jarad's own voicemail greeting recorded every time
a call rolls over, carrier intercepts ("the person you are trying to reach..."),
calls that were nothing but "Hello? Hello?", header-only notes from silent
audio, and one-word dictation misfires. This module finds them and moves them
to the Obsidian trash.

Classification believes the title and summary first: the title-slug and
summarize passes already read the transcript, and when a note's title says
"Voicemail Greeting" or "Insufficient Transcript Content" that is the best
evidence there is. Every title/summary rule is fenced by a word count, so a long
meeting that happens to mention a voicemail is never touched. Rules, first hit
wins:

==============  ============================================================
``empty``       the body has no spoken words (header only, metadata only, a
                lone URL)
``voicemail``   a voicemail by structure (``call-outcome: voicemail``, a
                ``telnyx-vm-*`` source), by title, or by summary on a short
                note: greetings, carrier intercepts and left messages alike
``no-content``  the title or summary says there is nothing there ("empty",
                "no spoken content", "unable to hear", "repeated hello",
                "automated system message"), or Jev classified it
                ``no_content``, on a short note
``too-short``   fewer than ``min_words`` spoken words
``call-noise``  a phone call with at most ``CALL_NOISE_MAX_WORDS`` spoken words
``body-lost``   (reported, not pruned by default) an empty body on a note
                whose ``word-count`` says it once held a real transcript
==============  ============================================================

Safety, all in ``prune_note`` (the one choke point the CLI and dry run share):

- only ``*.md`` directly in the vault folder; no dot-files, no symlinks, no
  dot-directories, no file that resolves outside the folder;
- the folder must sit inside an Obsidian vault, because "delete" is a move to
  ``<vault>/.trash/<path relative to the vault>`` with
  ``rename.move_noclobber``: nothing is overwritten and nothing is unlinked;
- a note younger than ``min_age`` seconds, or whose item is still queued for
  transcription or has a pass running, is left alone: enrichment may not have
  titled it yet;
- the note is re-read and re-classified right before the move, holding the
  item's flock (the one every pass runs under) and the run locks of any
  document rows at the path, so a note a pass just rewrote is judged as it is
  now; the moved inode is verified afterwards (hygiene's ``_move_to_trash``);
- the source audio is never touched (backup-first, never delete).

The ledger follows the files: the transcript row gets ``pruned_at``, which
``ledger.passes_failed``, ``ledger.failed_passes_for_sweep``, the failures
collector and the tray all exclude (a pruned note's leftover failed pass can
never be re-driven, so it is history, not a failure); document rows at the
path become ``trashed``. Every move is appended to ``var/prune.jsonl`` with
both paths and a run id, which ``restore`` reads to put notes back. The
Postgres store needs no write: its reconcile sweep stamps ``note_missing_at``
(prune runs one at the end, best effort) and un-stamps it on restore.
"""

import errno
import json
import logging
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Optional

from . import documents, finalize, frontmatter, hygiene, ledger, operations, paths, rename, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

REASONS = ("empty", "voicemail", "no-content", "too-short", "call-noise", "body-lost")
# body-lost is reported, never pruned unless asked for: the note once had a
# real transcript (its word-count says so) and something wiped the body. That
# is a bug to recover from (the store keeps every version), not junk.
DEFAULT_REASONS = REASONS[:-1]

DEFAULT_MIN_WORDS = 4            # fewer spoken words than this is "too-short"
DEFAULT_MIN_AGE_S = 1800.0       # leave notes alone while enrichment may still title them
CALL_NOISE_MAX_WORDS = 15        # a call with no more than this said is connection noise
SHORT_MAX_WORDS = 60             # title/summary rules only judge notes this short...
VOICEMAIL_SUMMARY_MAX_WORDS = 300  # ...except a voicemail named by its summary
MAX_NOTE_BYTES = 4 << 20         # never classify (or move) a giant file
MANIFEST = "prune.jsonl"
# worker.WORK_STATES (not imported: worker drags in the whole daemon). An item
# in one of these is still headed for transcription; its note is not final.
QUEUED_STATES = ("pending", "archived", "transcribed")

# A voicemail, a greeting, or a carrier intercept. Matched against the title
# (any length) and the summary (short notes only).
VOICEMAIL_RE = re.compile(
    r"\bvoice[\s-]?mail"
    r"|\bmailbox\b"
    r"|\banswering machine\b"
    r"|\boutgoing (?:message|greeting)\b"
    r"|\bleave (?:a |your )?message\b"
    r"|\byou'?ve reached\b"
    r"|\b(?:person|party|number|subscriber) (?:you(?:'re| are) )?(?:trying to reach|calling|being contacted)\b"
    r"|\b(?:unavailable|not available) (?:recipient|subscriber|party|person)\b"
    r"|\bunavailable recipient\b"
    r"|\bintercept message\b",
    re.IGNORECASE)

# The title or summary saying, in so many words, that there is nothing here.
NO_CONTENT_RE = re.compile(
    r"\b(?:empty|blank)\b"
    r"|\bno (?:spoken|speech|transcript(?:ion)?|transcribed|dialogue|discernible|detectable|"
    r"substantive|meaningful|actual|audible|real|usable|intelligible)\b"
    r"|\bno (?:content|dialogue|speech|words|audio)\b"
    r"|\bwithout (?:any )?(?:spoken|dialogue|speech|content|substantive|meaningful|actual)\b"
    r"|\black(?:s|ing)? (?:any )?(?:spoken|dialogue|speech|content|substantive|meaningful|actual)\b"
    r"|\babsence of\b"
    r"|\binsufficient (?:transcript|content|audio|information|text)\b"
    r"|\b(?:only|just) (?:a |the )?(?:file ?name|header|metadata|title)\b"
    r"|\bheader (?:only|exists)\b"
    r"|\bmetadata (?:header|record|only)\b"
    r"|\b(?:can'?t|cannot|could not|couldn'?t|unable to) hear\b"
    r"|\brepeated(?:ly)? (?:hello|greeting)s?\b"
    r"|\bhello (?:greetings?|checks?)\b"
    r"|\b(?:audio|connection|microphone|mic|sound|call|recording|forwarding) (?:check|test)\b"
    r"|\btest (?:recording|call|message)\b"
    r"|\bnonsense\b"
    r"|\b(?:automated|system) (?:message|prompt|readout|announcement)\b"
    r"|\bverification system\b"
    r"|\bmenu prompt\b",
    re.IGNORECASE)

_LABEL_RE = re.compile(r"^\*\*[^*\n]{1,80}?\*\*:?\s*")   # **Speaker 1 (00:00):**
_META_LINE_RE = re.compile(r"^[-*] \*\*[^*\n]{1,40}\*\*:")   # - **Duration**: 2.8s (legacy header)
_WORD_RE = re.compile(r"[A-Za-z0-9]")


class Verdict(NamedTuple):
    reason: Optional[str]   # one of REASONS, or None to keep
    detail: str
    words: int


class PruneError(RuntimeError):
    """A refusal that must not be read as 'nothing to prune'."""


# ------------------------------------------------------------ classify ----

def speech_words(body: str) -> int:
    """Spoken words in a transcript body.

    Skips headings, horizontal rules, the legacy ``- **Source**: ...`` header
    list, speaker labels and URLs, so a header-only note counts 0.
    """
    n = 0
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or set(s) <= {"-", "*", "_"} or _META_LINE_RE.match(s):
            continue
        s = _LABEL_RE.sub("", s)
        for tok in s.split():
            if "://" in tok or not _WORD_RE.search(tok):
                continue
            n += 1
    return n


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _is_call(fm: dict[str, Any], name: str) -> bool:
    return (fm.get("classification") == "phone_call"
            or any(k in fm for k in ("call-session-id", "call-source", "call-outcome"))
            or name.startswith("telnyx-"))


def _source_names(fm: dict[str, Any], body: str, name: str) -> str:
    """Every place the source audio's name shows up, lowercased, for structure checks."""
    head = next((line for line in body.splitlines() if line.startswith("# Transcription:")), "")
    return " ".join([name, head, _text(fm.get("source-audio")), _text(fm.get("source-s3-key"))]).lower()


def classify(fm: dict[str, Any], body: str, name: str = "", *,
             min_words: int = DEFAULT_MIN_WORDS) -> Verdict:
    """Decide whether one note is junk. Pure: no I/O."""
    words = speech_words(body)
    title, summary = _text(fm.get("title")), _text(fm.get("summary"))

    if words == 0:
        claimed = fm.get("word-count")
        if isinstance(claimed, int) and not isinstance(claimed, bool) and claimed > SHORT_MAX_WORDS:
            return Verdict("body-lost", f"body is empty but word-count says {claimed}", words)
        return Verdict("empty", "no spoken words in the body", words)

    if _text(fm.get("call-outcome")).lower() == "voicemail":
        return Verdict("voicemail", "call-outcome: voicemail", words)
    if "telnyx-vm-" in _source_names(fm, body, name):
        return Verdict("voicemail", "telnyx voicemail recording", words)
    m = VOICEMAIL_RE.search(title)
    if m:
        return Verdict("voicemail", f"title says {m.group(0)!r}", words)
    m = VOICEMAIL_RE.search(summary)
    if m and words <= VOICEMAIL_SUMMARY_MAX_WORDS:
        return Verdict("voicemail", f"summary says {m.group(0)!r}", words)

    if words <= SHORT_MAX_WORDS:
        for field, text in (("title", title), ("summary", summary)):
            m = NO_CONTENT_RE.search(text)
            if m:
                return Verdict("no-content", f"{field} says {m.group(0)!r}", words)
        if _text(fm.get("classification-detail")) == "no_content":
            return Verdict("no-content", "classification-detail: no_content", words)

    if words < min_words:
        return Verdict("too-short", f"{words} spoken word{'s' if words != 1 else ''}", words)

    if words <= CALL_NOISE_MAX_WORDS and _is_call(fm, name):
        return Verdict("call-noise", f"phone call with {words} spoken words", words)

    return Verdict(None, "", words)


# --------------------------------------------------------------- inspect ----

class Look(NamedTuple):
    verdict: str                       # ok | excluded | symlink | escape | not_file | young | vanished | too_large | unreadable
    stat: Optional[os.stat_result]
    text: str = ""
    detail: str = ""


def vault_folder() -> hygiene.Folder:
    """``WAX_VAULT`` and the Obsidian vault around it (whose ``.trash`` we use)."""
    root = Path(os.path.realpath(paths.VAULT))
    if not root.is_dir():
        raise PruneError(f"{paths.VAULT} is not a directory")
    vault = hygiene.vault_root_for(root)
    if vault is None:
        raise PruneError(f"{root}: no Obsidian vault (.obsidian/) above it, so there is no .trash "
                         "to move notes into; refusing to prune")
    return hygiene.Folder(root, vault)


def iter_notes(folder: hygiene.Folder) -> Iterable[Path]:
    """Top-level ``*.md`` only, the same set the store reconciles (``list_notes``)."""
    try:
        entries = sorted(folder.root.iterdir())
    except OSError as exc:
        raise PruneError(f"cannot list {folder.root}: {exc}") from exc
    for path in entries:
        if path.suffix == ".md" and not path.name.startswith("."):
            yield path


def look(path: Path, folder: hygiene.Folder, *, min_age: float, now: float) -> Look:
    """Path rules, age and the note's text, through one ``O_NOFOLLOW`` descriptor."""
    refused = hygiene.static_verdict(path, folder)
    if refused is not None:
        return Look(refused.verdict, None, "", refused.detail)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return Look("vanished", None)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return Look("symlink", None, "", "symlinks are never followed")
        return Look("unreadable", None, "", str(exc))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return Look("not_file", st)
        if now - st.st_mtime <= min_age:
            return Look("young", st, "", f"modified {max(0, int(now - st.st_mtime))}s ago")
        if st.st_size > MAX_NOTE_BYTES:
            return Look("too_large", st, "", f"{st.st_size} bytes")
        data = b""
        while len(data) <= st.st_size:
            block = os.read(fd, st.st_size - len(data) + 1)
            if not block:
                break
            data += block
    except OSError as exc:
        return Look("unreadable", None, "", str(exc))
    finally:
        os.close(fd)
    try:
        return Look("ok", st, data.decode("utf-8"))
    except UnicodeDecodeError:
        return Look("unreadable", st, "", "not valid UTF-8")


def judge(path: Path, text: str, *, min_words: int) -> tuple[dict[str, Any], Verdict]:
    fm, body = frontmatter.split(text)
    if not fm and text.startswith(frontmatter.DELIM):
        # Frontmatter that does not parse: we cannot believe a title we cannot read.
        return fm, Verdict(None, "unparseable frontmatter", speech_words(body))
    return fm, classify(fm, body, path.name, min_words=min_words)


# ----------------------------------------------------------------- ledger ----

def _transcript_item(path: Path, fm: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The ledger transcript this note is the output of, if Wax recorded one.

    Matched on the recorded ``md_path`` (exact, then same real file), with the
    note's own ``wax-item-id`` as a tie-breaker: a note is never pinned to an
    item whose recorded note is some other file.
    """
    conn = ledger.connect()
    rows = conn.execute(
        "SELECT t.item_id, t.md_path, t.pruned_at, i.state FROM transcripts t "
        "LEFT JOIN items i USING(item_id) WHERE t.md_path=?", (str(path),)).fetchall()
    if not rows:
        claimed = fm.get(frontmatter.ITEM_KEY)
        if isinstance(claimed, str) and claimed:
            rows = conn.execute(
                "SELECT t.item_id, t.md_path, t.pruned_at, i.state FROM transcripts t "
                "LEFT JOIN items i USING(item_id) WHERE t.item_id=?", (claimed,)).fetchall()
            rows = [r for r in rows if r["md_path"]
                    and os.path.realpath(r["md_path"]) == os.path.realpath(path)]
    if not rows:
        return None
    claimed = fm.get(frontmatter.ITEM_KEY)
    best = next((r for r in rows if r["item_id"] == claimed), rows[0])
    return dict(best)


def _busy_reason(item: Optional[dict[str, Any]]) -> Optional[str]:
    """Why an item's note may not be final yet, or None."""
    if item is None:
        return None
    if item.get("state") in QUEUED_STATES:
        return f"item {item['item_id']} is still {item['state']}"
    running = ledger.connect().execute(
        "SELECT ep_slug FROM passes WHERE item_id=? AND state='running'", (item["item_id"],)).fetchone()
    if running:
        return f"pass {running['ep_slug']} is running"
    return None


def _manifest_path() -> Path:
    return paths.VAR / MANIFEST


def _append_manifest(entry: dict[str, Any]) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_manifest() -> list[dict[str, Any]]:
    path = _manifest_path()
    if not path.is_file():
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


def _held(item: Optional[dict[str, Any]]):
    """try_item_lock for a real item, a no-op True otherwise."""
    if item is None:
        from contextlib import nullcontext
        return nullcontext(True)
    return operations.try_item_lock(item["item_id"])


# ------------------------------------------------------------------- prune ---

def prune_note(path: Path, folder: hygiene.Folder, *, reasons: Iterable[str] = DEFAULT_REASONS,
               min_words: int = DEFAULT_MIN_WORDS, min_age: float = DEFAULT_MIN_AGE_S,
               now: Optional[float] = None, dry_run: bool = True,
               run_id: str = "") -> dict[str, Any]:
    """Judge one note and, unless ``dry_run``, move it to the trash. Never
    raises for a refusal: ``outcome`` says why nothing happened.

    Outcomes: ``pruned`` / ``would_prune``; ``keep`` (not junk, or junk of a
    reason not asked for: ``reason`` is set then); ``in_flight``, ``locked``,
    ``changed``, ``restored`` (a write raced the move and the note was put
    back); or a ``look`` verdict (``young``, ``symlink``, ...).
    """
    reasons = tuple(reasons)
    now = time.time() if now is None else now
    first = look(path, folder, min_age=min_age, now=now)
    if first.verdict != "ok":
        return {"path": str(path), "outcome": first.verdict, "detail": first.detail}
    fm, verdict = judge(path, first.text, min_words=min_words)
    base = {"path": str(path), "name": path.name, "title": frontmatter_title(fm),
            "reason": verdict.reason, "detail": verdict.detail, "words": verdict.words}
    if verdict.reason is None or verdict.reason not in reasons:
        return {**base, "outcome": "keep"}
    item = _transcript_item(path, fm)
    if item is not None:
        base["item_id"] = item["item_id"]
    busy = _busy_reason(item)
    if busy:
        return {**base, "outcome": "in_flight", "detail": busy}
    rows = documents.rows_for_path(path)
    dest = hygiene.trash_destination(path, folder)
    if dry_run:
        if any(documents.run_lock_held(r["binding_id"], r["doc_id"]) for r in rows):
            return {**base, "outcome": "locked"}
        return {**base, "outcome": "would_prune", "to": str(dest)}
    with _held(item) as item_held, documents.hold_run_locks(rows) as docs_held:
        if not (item_held and docs_held):
            return {**base, "outcome": "locked"}
        # Re-check under the locks: a pass that finished since the first look
        # may have re-titled the note or written its transcript.
        second = look(path, folder, min_age=min_age, now=time.time())
        if second.verdict != "ok":
            return {**base, "outcome": "changed", "detail": second.verdict}
        _, again = judge(path, second.text, min_words=min_words)
        if again.reason != verdict.reason:
            return {**base, "outcome": "changed",
                    "detail": f"now {again.reason or 'not junk'}"}
        if _busy_reason(_transcript_item(path, fm)) is not None:
            return {**base, "outcome": "in_flight"}
        if item is not None:
            try:
                # Events a finished pass declared would otherwise wait on a
                # note that no longer exists (finalize: missing_transcript).
                finalize.publish_pass_events(item["item_id"])
            except Exception as exc:  # noqa: BLE001 - never block a prune on the outbox
                log.warning("prune: publishing pass events for %s failed: %s: %s",
                            item["item_id"], type(exc).__name__, exc)
        used, restored = hygiene._move_to_trash(path, folder, second.stat)  # noqa: SLF001
        if restored:
            return {**base, "outcome": "restored", "to": str(used)}
        at = sentinel.utcnow()
        if item is not None:
            ledger.connect().execute("UPDATE transcripts SET pruned_at=? WHERE item_id=?",
                                     (at, item["item_id"]))
            ledger.record_transition("transcript", item["item_id"], "published", "pruned",
                                     verdict.reason or "", f"{path} -> {used}")
        for row in rows:
            documents.mark_state(row["binding_id"], row["doc_id"], "trashed")
        _append_manifest({"action": "prune", "run": run_id, "at": at, "from": str(path),
                          "to": str(used), "reason": verdict.reason, "detail": verdict.detail,
                          "words": verdict.words, "item_id": item and item["item_id"],
                          "documents": [[r["binding_id"], r["doc_id"], r.get("state")] for r in rows]})
        log.info("prune: %s (%s) -> %s", path, verdict.reason, used)
        return {**base, "outcome": "pruned", "to": str(used)}


def frontmatter_title(fm: dict[str, Any]) -> str:
    return _text(fm.get("title"))


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sync_store() -> dict[str, Any]:
    """One store reconcile so ``tx`` / browse drop the moved notes now, not at
    the next 10-minute tick. Best effort: the tick does the same later."""
    try:
        from .transcriptions import reconcile as store_reconcile
        out = store_reconcile.sweep()
    except Exception as exc:  # noqa: BLE001 - store outages never fail a prune
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:300]}
    return {"ok": not out.get("errors"), "vanished": out.get("vanished"),
            "errors": (out.get("errors") or [])[:5]}


def prune(*, apply: bool = False, reasons: Iterable[str] = DEFAULT_REASONS,
          min_words: int = DEFAULT_MIN_WORDS, min_age: float = DEFAULT_MIN_AGE_S,
          limit: int = 0, now: Optional[float] = None, sync_store: bool = True) -> dict[str, Any]:
    """Sweep the vault. A dry run (the default) changes nothing."""
    reasons = tuple(reasons)
    unknown = sorted(set(reasons) - set(REASONS))
    if unknown:
        raise PruneError(f"unknown reason(s): {', '.join(unknown)}")
    folder = vault_folder()
    run_id = _new_run_id() if apply else ""
    counts: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    acted: list[dict[str, Any]] = []
    held_back: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in iter_notes(folder):
        counts["scanned"] = counts.get("scanned", 0) + 1
        try:
            out = prune_note(path, folder, reasons=reasons, min_words=min_words, min_age=min_age,
                             now=now, dry_run=not apply, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - one note never stops the sweep
            log.warning("prune: %s: %s: %s", path, type(exc).__name__, exc)
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            counts["errors"] = counts.get("errors", 0) + 1
            continue
        outcome = out["outcome"]
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome in ("pruned", "would_prune"):
            by_reason[out["reason"]] = by_reason.get(out["reason"], 0) + 1
            acted.append(out)
            if limit and len(acted) >= limit:
                break
        elif outcome == "keep" and out.get("reason"):
            held_back.append(out)        # junk of a reason not asked for (body-lost)
        elif outcome not in ("keep",):
            held_back.append(out)
    summary: dict[str, Any] = {
        "dry_run": not apply, "run": run_id or None, "vault": str(folder.root),
        "trash": str(folder.vault / hygiene.TRASH_DIR / os.path.relpath(folder.root, folder.vault)),
        "reasons": list(reasons), "min_words": min_words, "min_age_s": min_age,
        "counts": counts, "by_reason": by_reason, "notes": acted, "held_back": held_back,
        "errors": errors, "limit": limit or None,
    }
    if apply and counts.get("pruned") and sync_store:
        summary["store"] = _sync_store()
    return summary


# ----------------------------------------------------------------- restore ---

def restore(*, run: Optional[str] = None, names: Iterable[str] = (),
            dry_run: bool = False) -> dict[str, Any]:
    """Put pruned notes back where they were (never over a file now there).

    Selects manifest entries by run id and/or note name (original or trash
    basename, or either full path). The newest prune of a path wins; a path
    whose newest entry is already a restore is skipped.
    """
    names = {n for n in names if n}
    if not run and not names:
        raise PruneError("name a --run or at least one note")
    latest: dict[str, dict[str, Any]] = {}
    for entry in read_manifest():
        key = entry.get("from")
        if isinstance(key, str):
            latest[key] = entry
    out: dict[str, Any] = {"dry_run": dry_run, "restored": [], "skipped": [], "errors": []}
    for src, entry in sorted(latest.items()):
        if entry.get("action") != "prune":
            continue
        wanted = (run and entry.get("run") == run) or any(
            n in (src, entry.get("to"), Path(src).name, Path(entry.get("to") or "").name)
            for n in names)
        if not wanted:
            continue
        trashed, original = Path(entry["to"]), Path(src)
        if not trashed.is_file() or trashed.is_symlink():
            out["skipped"].append({"path": src, "detail": f"{trashed} is no longer in the trash"})
            continue
        if dry_run:
            out["restored"].append({"path": src, "from": str(trashed), "would": True})
            continue
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            used = rename.move_noclobber(trashed, original)
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{src}: {type(exc).__name__}: {exc}")
            continue
        at = sentinel.utcnow()
        item_id = entry.get("item_id")
        if item_id:
            conn = ledger.connect()
            conn.execute("UPDATE transcripts SET pruned_at=NULL, md_path=? WHERE item_id=?",
                         (str(used), item_id))
            ledger.record_transition("transcript", item_id, "pruned", "published", "restored",
                                     f"{trashed} -> {used}")
        for binding_id, doc_id, *prior in entry.get("documents") or []:
            documents.mark_state(binding_id, doc_id, (prior and prior[0]) or "processed")
        _append_manifest({"action": "restore", "run": entry.get("run"), "at": at, "from": src,
                          "to": str(used), "trash": str(trashed), "item_id": item_id})
        out["restored"].append({"path": str(used), "from": str(trashed)})
    return out
