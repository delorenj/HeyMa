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
meeting that happens to mention a voicemail is never touched. Structure
(``call-outcome: voicemail``, a ``telnyx-vm-*`` source, a call that opens on a
voicemail greeting) marks a voicemail at any length, except a long one
(over ``LONG_VOICEMAIL_MIN_WORDS``) whose title says what the message is about
(``meaningful_title``): that is a real person's message and is kept. Rules,
first hit wins:

==============  ============================================================
``empty``       the body has no spoken words (header only, metadata only, a
                lone URL)
``voicemail``   a voicemail by structure (``call-outcome: voicemail``, a
                ``telnyx-vm-*`` source), by title, or by summary on a short
                note: greetings, carrier intercepts and left messages alike,
                except a long message with a meaningful title (kept) that is
                not a loan or debt pitch (``LOAN_PITCH_RE``)
``no-content``  the title or summary says there is nothing there ("empty",
                "no spoken content", "unable to hear", "repeated hello",
                "automated system message"), or Jev classified it
                ``no_content``, on a short note
``too-short``   fewer than ``min_words`` spoken words
``call-noise``  a phone call with at most ``CALL_NOISE_MAX_WORDS`` spoken words
``body-lost``   (reported, not pruned by default, checked first) a body far
                smaller than the word count recorded for it (the note's
                ``word-count`` or the ledger's): a clobbered transcript
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
CALL_NOISE_MAX_WORDS = 20        # a call with no more than this said is connection noise
SHORT_MAX_WORDS = 60             # title/summary rules only judge notes this short...
VOICEMAIL_SUMMARY_MAX_WORDS = 300  # ...except a voicemail named by its summary
LONG_VOICEMAIL_MIN_WORDS = SHORT_MAX_WORDS  # a structural voicemail longer than this keeps a meaningful title
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
    r"|\bmenu prompt\b"
    r"|\bheyma probe\b|\bprobe (?:call|test)\b|\bphone answering\b"
    r"|\btesting (?:voice note|message)\b"
    r"|\b(?:message|voice note|button|audio quality|quality comparison)\b.{0,30}\btest\b"
    r"|\btest\b.{0,30}\b(?:delivery|transmission)\b",
    re.IGNORECASE)

_LABEL_RE = re.compile(r"^\*\*[^*\n]{1,80}?\*\*:?\s*")   # **Speaker 1 (00:00):**
_META_LINE_RE = re.compile(r"^[-*] \*\*[^*\n]{1,40}\*\*:")   # - **Duration**: 2.8s (legacy header)
# A voicemail greeting that opens the recording: the call rolled to voicemail
# and whatever follows is the message. Searched in the first lines of a call.
VM_GREETING_RE = re.compile(
    r"\byou'?ve reached\b[^.\n]{0,40}[.!]\s*(?:please\s+)?leave (?:a |your )?message\b", re.IGNORECASE)
# A call title naming an automated phone system: believed up to the long fence.
AUTOMATED_CALL_TITLE_RE = re.compile(
    r"^automated\b.*\b(?:menu|greeting|prompt|system|announcement)\b", re.IGNORECASE)
# Untitled test utterances ("let's see if this works").
TEST_BODY_RE = re.compile(
    r"\b(?:this is a test|let'?s see if (?:it|this) works|if it works,? then|count to (?:three|four|ten))\b",
    re.IGNORECASE)
TEST_BODY_MAX_WORDS = 20
# Titles ``meaningful_title`` refuses on a long voicemail. A title that is
# just an audio file name ("20261005-rec.ogg"):
AUDIO_NAME_RE = re.compile(
    r"\.(?:mp3|mp4|ogg|oga|opus|wav|m4a|aac|aiff?|caf|3gp|wma|flac|webm|amr)$", re.IGNORECASE)
# ...an audio stem ("audio_2d86e0cd8d19", "telnyx-vm-abc123", "20260903-092132-rec"):
FILE_STEM_TITLE_RE = re.compile(
    r"^(?:audio|telnyx|rec|recording|clip|voice|vm)?[\w-]*?(?:[0-9a-f]{8,}|\d{6,}[\w-]*)$|^\S*[_-]\S*\d\S*$",
    re.IGNORECASE)
# ...a generic label that says nothing about the message:
GENERIC_TITLE_RE = re.compile(
    r"^(?:(?:my|jarad'?s|personal|standard|default|custom|carrier|phone|outgoing|new|unknown|"
    r"incoming|brief|short|recorded|voice|audio|missed|untitled|greeting)\s+)*"
    r"(?:greeting|message|voice ?message|missed call|call|call ?back|caller|recording|note|memo|"
    r"untitled(?: note)?|unknown caller)s?\.?$",
    re.IGNORECASE)
# ...or a title that says the recording is empty. Narrower than NO_CONTENT_RE,
# which judges short notes and would refuse "Empty Storefront Lease Inquiry".
LONG_NO_CONTENT_TITLE_RE = re.compile(
    r"\bno (?:spoken|speech|content|audio|dialogue|words|message)\b"
    r"|\b(?:can'?t|cannot|could not|unable to) hear\b"
    r"|\binsufficient (?:transcript|content|audio|information|text)\b"
    r"|\b(?:empty|blank|silent) (?:transmission|recording|file|message|audio|call|note|transcript)\b"
    r"|\b(?:dead air|silence only|only silence)\b",
    re.IGNORECASE)
# An unsolicited loan or debt pitch, matched against a voicemail's title and
# summary only: a long titled voicemail is kept unless it is one of these
# (operator order 2026-11-10: "add a spam rule for long loan pitches"). Never
# applied outside a voicemail, so a real call about a loan payoff is untouched.
LOAN_PITCH_RE = re.compile(
    r"\bpre-?\s?(?:approv\w*|select\w*|qualif\w*|screen\w*)\b"
    r"|\bdebt (?:consolidation|relief|settlement|reduction)\b"
    r"|\b(?:personal|business|hardship|unsecured|installment) loans?\b"
    r"|\bloan (?:offers?|options?|opportunit\w*|eligibility)\b"
    r"|\b(?:funding|lending) (?:group|offers?|options?|solutions?)\b"
    r"|\btax (?:relief|debt|resolution)\b"
    r"|\bcredit card (?:debt|relief|consolidation)\b",
    re.IGNORECASE)
# Jev's classification floor tags anything under 15 words and 10 s no_content
# without reading it; believed alone only for fragments this short.
FLOOR_NO_CONTENT_MAX_WORDS = 7
# A body this many times smaller than the word count recorded for it was
# clobbered, not short.
BODY_LOST_RATIO = 4


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
            if "://" in tok or not any(ch.isalnum() for ch in tok):
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


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def body_lost_claim(fm: dict[str, Any], words: int, recorded_words: int = 0) -> int:
    """The word count a note claims, when its body holds far less speech than that; else 0.

    The one definition of "body lost", shared by ``classify`` (prune) and
    ``damage`` (``wax doctor``): the larger of the note's own ``word-count`` and
    the ledger's ``transcripts.word_count`` must exceed ``SHORT_MAX_WORDS`` and
    ``BODY_LOST_RATIO`` times the ``words`` of speech now in the body.
    """
    claimed = max(_int(fm.get("word-count")), _int(recorded_words))
    if claimed > SHORT_MAX_WORDS and claimed > BODY_LOST_RATIO * max(words, 1):
        return claimed
    return 0


def meaningful_title(title: str, name: str = "") -> bool:
    """A title that says what a message is about, not that it is a voicemail or empty.

    Not meaningful: no title; a placeholder nobody chose (``hygiene.placeholder_title``:
    ``Untitled N``, ``Transcription: <file>.ogg``); the note's or audio's own
    file name or stem; a generic label (``GENERIC_TITLE_RE``: "Personal
    Greeting", "Missed Call", "Voice Message"); fewer than two words; or a title
    that names a voicemail, carrier intercept, automated system or an empty
    recording (``VOICEMAIL_RE``, ``AUTOMATED_CALL_TITLE_RE``,
    ``LONG_NO_CONTENT_TITLE_RE``). The short-note ``NO_CONTENT_RE`` is not
    used: on a long message "Empty Storefront Lease Inquiry" is a topic.
    """
    t = title.strip()
    if not t or hygiene.placeholder_title(t) or AUDIO_NAME_RE.search(t) or GENERIC_TITLE_RE.match(t):
        return False
    stem = Path(name).stem.lower() if name else ""
    if stem and t.lower() in (stem, Path(name).name.lower()):
        return False
    if FILE_STEM_TITLE_RE.match(t):
        return False
    tokens = [w for w in re.split(r"[\s_]+", t) if any(ch.isalnum() for ch in w)]
    wide = any(ch.isalpha() and ord(ch) >= 0x2E80 for ch in t)   # CJK: no spaces between words
    if not any(ch.isalpha() for ch in t) or (len(tokens) < 2 and not (wide and len(t) >= 4)):
        return False
    vm = VOICEMAIL_RE.search(t)
    if vm and vm.group(0).lower() != "mailbox":   # "Mailbox Delivery Complaint" is a topic
        return False
    return not (AUTOMATED_CALL_TITLE_RE.search(t) or LONG_NO_CONTENT_TITLE_RE.search(t))


def classify(fm: dict[str, Any], body: str, name: str = "", *,
             min_words: int = DEFAULT_MIN_WORDS, recorded_words: int = 0) -> Verdict:
    """Decide whether one note is junk. Pure: no I/O.

    ``recorded_words`` is the word count Wax recorded for this transcript when
    it was written (``transcripts.word_count``); with the note's own
    ``word-count`` it is the evidence that a short body was clobbered.
    """
    words = speech_words(body)
    title, summary = _text(fm.get("title")), _text(fm.get("summary"))
    is_call = _is_call(fm, name)

    claimed = body_lost_claim(fm, words, recorded_words)
    if claimed:
        return Verdict("body-lost", f"body has {words} spoken words but {claimed} were recorded", words)

    if words == 0:
        return Verdict("empty", "no spoken words in the body", words)

    long_titled = words > LONG_VOICEMAIL_MIN_WORDS and meaningful_title(title, name)
    pitch = next((f"{field} says {m.group(0)!r}" for field, text in (("title", title), ("summary", summary))
                  for m in [LOAN_PITCH_RE.search(text)] if m), "")
    structural = None
    if _text(fm.get("call-outcome")).lower() == "voicemail":
        structural = "call-outcome: voicemail"
    elif "telnyx-vm-" in _source_names(fm, body, name):
        structural = "telnyx voicemail recording"
    elif is_call and VM_GREETING_RE.search(body[:700]):
        structural = "voicemail greeting opens the call"
    if structural:
        # A long message whose title names what it is about is someone's real
        # message (a press-conference invitation, a recruiting pitch), not a
        # greeting: believe the title and keep it (operator order 2026-11-10),
        # unless it is a loan pitch.
        if long_titled and pitch:
            return Verdict("voicemail", f"{structural}; loan pitch ({pitch})", words)
        if long_titled:
            return Verdict(None, f"{structural}, but a long message titled {title!r}", words)
        return Verdict("voicemail", structural, words)
    if words <= VOICEMAIL_SUMMARY_MAX_WORDS:
        for field, text in (("title", title), ("summary", summary)):
            m = VOICEMAIL_RE.search(text)
            if m:
                if field == "summary" and long_titled and pitch:
                    return Verdict("voicemail", f"summary says {m.group(0)!r}; loan pitch ({pitch})", words)
                if field == "summary" and long_titled:
                    # "Damian leaves a voicemail about ..." under a title that
                    # says what: the same long titled message, kept.
                    return Verdict(None, f"summary says {m.group(0)!r}, but a long message "
                                         f"titled {title!r}", words)
                return Verdict("voicemail", f"{field} says {m.group(0)!r}", words)
        m = AUTOMATED_CALL_TITLE_RE.search(title)
        if m and is_call:
            return Verdict("no-content", f"title says {m.group(0)!r}", words)

    if words <= SHORT_MAX_WORDS:
        for field, text in (("title", title), ("summary", summary)):
            m = NO_CONTENT_RE.search(text)
            if m:
                return Verdict("no-content", f"{field} says {m.group(0)!r}", words)
        if _text(fm.get("classification-detail")) == "no_content" and (
                _text(fm.get("classification-source")) != "rule:floor"
                or words <= FLOOR_NO_CONTENT_MAX_WORDS):
            return Verdict("no-content", "classification-detail: no_content", words)
    if words <= TEST_BODY_MAX_WORDS:
        m = TEST_BODY_RE.search(body)
        if m:
            return Verdict("no-content", f"test utterance {m.group(0)!r}", words)

    if words < min_words:
        return Verdict("too-short", f"{words} spoken word{'s' if words != 1 else ''}", words)

    if words <= CALL_NOISE_MAX_WORDS and is_call:
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
        # *.suspect.md is a sanity-gate quarantine: the evidence behind a
        # `suspect` item, not a transcript (reconcile.list_notes skips it too).
        if (path.suffix == ".md" and not path.name.startswith(".")
                and not path.name.endswith(".suspect.md")):
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


def judge(path: Path, text: str, *, min_words: int,
          recorded_words: int = 0) -> tuple[dict[str, Any], Verdict]:
    fm, body = frontmatter.split(text)
    if not fm and text.startswith(frontmatter.DELIM):
        # Frontmatter that does not parse: we cannot believe a title we cannot read.
        return fm, Verdict(None, "unparseable frontmatter", speech_words(body))
    return fm, classify(fm, body, path.name, min_words=min_words, recorded_words=recorded_words)


# ----------------------------------------------------------------- ledger ----

def _transcript_item(path: Path, fm: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The ledger transcript this note is the output of, if Wax recorded one.

    Matched on the recorded ``md_path`` (exact, then same real file), with the
    note's own ``wax-item-id`` as a tie-breaker: a note is never pinned to an
    item whose recorded note is some other file.
    """
    conn = ledger.connect()
    rows = conn.execute(
        "SELECT t.item_id, t.md_path, t.pruned_at, t.word_count, i.state FROM transcripts t "
        "LEFT JOIN items i USING(item_id) WHERE t.md_path=?", (str(path),)).fetchall()
    if not rows:
        claimed = fm.get(frontmatter.ITEM_KEY)
        if isinstance(claimed, str) and claimed:
            rows = conn.execute(
                "SELECT t.item_id, t.md_path, t.pruned_at, t.word_count, i.state FROM transcripts t "
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


# -------------------------------------------------------------- integrity ----
# `wax doctor` asks a different question than prune: not "is this note junk?"
# but "did something outside Wax destroy a note Wax published?". Two real notes
# lost their bodies that way (a symlink written through, a manual paste) and
# nothing noticed for weeks. Same evidence as the body-lost rule, no new thresholds.

DAMAGE_KINDS = ("body-lost", "frontmatter-lost")
INTEGRITY_BUDGET_S = 10.0       # a doctor probe never hangs, however big the vault grows
INTEGRITY_LIST_MAX = 10         # offenders named in full; the total is always counted


class Damage(NamedTuple):
    kinds: tuple[str, ...]      # from DAMAGE_KINDS, in that order
    claimed: int                # words recorded for the note (its word-count, the ledger's); 0 when none
    words: int                  # spoken words in the body now
    detail: str


def damage(text: str, *, recorded_words: int = 0) -> Optional[Damage]:
    """How a note Wax published was damaged outside Wax, or None. Pure: no I/O.

    ``body-lost`` is prune's rule (``body_lost_claim``). ``frontmatter-lost``:
    the note has no frontmatter block, or one that does not parse. Wax stamps
    ``wax-item-id`` and the ``wax:`` block into every transcript it publishes,
    so a published note without them was overwritten or pasted over.
    """
    try:
        fm, body = frontmatter.split(text)
    except Exception:  # noqa: BLE001 - e.g. `captured: 2026-02-30`: yaml's timestamp constructor raises ValueError, not YAMLError
        # A block Wax cannot parse is a block Wax cannot read, whatever the reason. One such note must never abort
        # the scan (and with it every other finding): judge it exactly like frontmatter that is not valid YAML.
        fm, body = {}, text
    words = speech_words(body)
    claimed = body_lost_claim(fm, words, recorded_words)
    kinds: list[str] = []
    parts: list[str] = []
    if claimed:
        kinds.append("body-lost")
        parts.append(f"body has {words} spoken words but {claimed} were recorded")
    if not fm:
        kinds.append("frontmatter-lost")
        parts.append("frontmatter does not parse" if text.startswith(frontmatter.DELIM)
                     else "no frontmatter")
    if not kinds:
        return None
    return Damage(tuple(kinds), max(claimed, _int(recorded_words)), words, "; ".join(parts))


def _read_note(path: Path) -> tuple[Optional[str], str]:
    """(text, "ok"), or (None, why): missing | too_large | unreadable. Bounded, and a FIFO or
    device can never block it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "unreadable"
        if st.st_size > MAX_NOTE_BYTES:
            return None, "too_large"
        data = b""
        while len(data) <= st.st_size:
            block = os.read(fd, st.st_size - len(data) + 1)
            if not block:
                break
            data += block
    except OSError:
        return None, "unreadable"
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8"), "ok"
    except UnicodeDecodeError:
        return None, "unreadable"


def scan_integrity(*, limit: int = INTEGRITY_LIST_MAX,
                   budget_s: float = INTEGRITY_BUDGET_S) -> dict[str, Any]:
    """Look for damaged notes among the ledger's published transcripts. Never writes.

    One pass over the ``transcripts`` rows (newest first, ``pruned_at`` rows
    skipped: their note is in the trash on purpose), each existing note read
    once and judged by ``damage``. ``complete`` is False when ``budget_s`` ran
    out first, so a partial scan can never read as a clean one. Up to ``limit``
    offenders are listed with their restore sources; ``damaged_total`` counts all.
    A note that cannot be read (not UTF-8 text, not a regular file) cannot be
    judged either way, so it is counted and listed apart (``unreadable_notes``).
    """
    rows = ledger.connect().execute(
        "SELECT item_id, md_path, word_count, asr_path FROM transcripts "
        "WHERE pruned_at IS NULL AND md_path IS NOT NULL AND md_path != '' "
        "ORDER BY created_at DESC, item_id").fetchall()
    out: dict[str, Any] = {"ledger_rows": len(rows), "checked": 0, "missing": 0, "unreadable": 0,
                           "too_large": 0, "damaged_total": 0, "damaged": [], "unreadable_notes": [],
                           "complete": True}
    deadline = time.monotonic() + budget_s
    for row in rows:
        if time.monotonic() > deadline:
            out["complete"] = False
            break
        path = Path(os.path.expanduser(row["md_path"]))
        text, why = _read_note(path)
        if text is None:
            out[why] += 1
            if why == "unreadable" and len(out["unreadable_notes"]) < limit:
                out["unreadable_notes"].append({"item_id": row["item_id"], "path": str(path), "name": path.name})
            continue
        out["checked"] += 1
        found = damage(text, recorded_words=_int(row["word_count"]))
        if found is None:
            continue
        out["damaged_total"] += 1
        if len(out["damaged"]) < limit:
            item_id = row["item_id"]
            asr = row["asr_path"] or str(paths.LOGS / item_id / "asr.json")
            out["damaged"].append({
                "item_id": item_id, "path": str(path), "name": path.name,
                "kinds": list(found.kinds), "claimed": found.claimed, "words": found.words,
                "detail": found.detail,
                "restore": {"store_history": f"wax transcripts history {item_id}",
                            "store_show": f"wax transcripts show {item_id} --version N --raw",
                            "asr_json": asr, "asr_json_exists": os.path.isfile(asr)},
            })
    return out


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
    fm, _ = frontmatter.split(first.text)
    item = _transcript_item(path, fm)
    recorded = _int((item or {}).get("word_count"))
    fm, verdict = judge(path, first.text, min_words=min_words, recorded_words=recorded)
    base = {"path": str(path), "name": path.name, "title": frontmatter_title(fm),
            "reason": verdict.reason, "detail": verdict.detail, "words": verdict.words}
    if item is not None:
        base["item_id"] = item["item_id"]
    if verdict.reason is None or verdict.reason not in reasons:
        return {**base, "outcome": "keep"}
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
        _, again = judge(path, second.text, min_words=min_words, recorded_words=recorded)
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
        # The manifest is the only undo path: write it before anything else
        # can fail, so a note in the trash is always findable by unprune.
        _append_manifest({"action": "prune", "run": run_id, "at": at, "from": str(path),
                          "to": str(used), "reason": verdict.reason, "detail": verdict.detail,
                          "words": verdict.words, "item_id": item and item["item_id"],
                          "md_path": item and item["md_path"],
                          "documents": [[r["binding_id"], r["doc_id"], r.get("state")] for r in rows]})
        try:
            if item is not None:
                ledger.connect().execute("UPDATE transcripts SET pruned_at=? WHERE item_id=?",
                                         (at, item["item_id"]))
                ledger.record_transition("transcript", item["item_id"], "published", "pruned",
                                         verdict.reason or "", f"{path} -> {used}")
            for row in rows:
                documents.mark_state(row["binding_id"], row["doc_id"], "trashed")
        except Exception as exc:
            raise PruneError(f"moved to {used} (run {run_id}) but the ledger update failed: "
                             f"{type(exc).__name__}: {exc}") from exc
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
    return {"ok": not out.get("errors"), "vanished_marked": out.get("vanished_marked"),
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
            # Keep the ledger's own spelling of the path (~/d/... rather than
            # the resolved vault path) unless no-clobber had to rename it.
            recorded = entry.get("md_path") or str(used)
            md_path = recorded if used == original else str(Path(recorded).with_name(used.name))
            conn.execute("UPDATE transcripts SET pruned_at=NULL, md_path=? WHERE item_id=?",
                         (md_path, item_id))
            ledger.record_transition("transcript", item_id, "pruned", "published", "restored",
                                     f"{trashed} -> {used}")
        for binding_id, doc_id, *prior in entry.get("documents") or []:
            documents.mark_state(binding_id, doc_id, (prior and prior[0]) or "processed")
        _append_manifest({"action": "restore", "run": entry.get("run"), "at": at, "from": src,
                          "to": str(used), "trash": str(trashed), "item_id": item_id})
        out["restored"].append({"path": str(used), "from": str(trashed)})
    return out
