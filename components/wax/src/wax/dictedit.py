"""`wax dict`: add a word to the HeyMa custom dictionary without hand-editing YAML.

The dictionary (`Dictionary.yaml`, read by the `dictionary-correction` pass on
every transcript) is a file people edit by hand, so this module edits it the
way a person would: one line per term, comments and sections left alone. Every
change is checked three ways before the file is replaced, atomically:

1. the new file still loads (`dictionary.load`, the same check the pass makes);
2. its parsed `terms` equal the old ones plus exactly the intended change, so a
   line edit can never quietly damage a neighbouring entry;
3. a `heard:` form is linted against what went wrong in calibration
   (docs/ENRICHMENT-PASSES.md, dictionary-correction): ordinary English words
   and fragments of the term ("jangler" for pjangler) are refused without
   `force`, because they turn every ordinary use of the word into a candidate.

The user does not have to invent mis-hearings. `scan()` reads the vault and
reports, for one term, how often it is already spelled right, what the pass
would already catch (and therefore needs no `heard:` entry), and the spans that
sound close but fall below the pass's bar. Those near misses are the only
mis-hearings worth listing, and they come from the speaker's real transcripts
rather than a guess. `fix_plan()` and `fix_one()` then re-run the pass on just
the notes that contain the term, so past transcripts are corrected too.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import multiprocessing
import os
import re
import signal
import tempfile
import threading
import unicodedata
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import yaml

from . import dictionary, frontmatter, paths

DEFAULT_SECTION = "Added with wax dict"
# A sound-alike between this and the pass's own bar is reported as a near miss:
# not corrected unless listed, worth a look. Below it is noise.
NEAR_MISS_MIN_SCORE = 0.72
MAX_EXAMPLES = 3
WORKERS = max(1, min(8, (os.cpu_count() or 2) // 4))

_HEADER = re.compile(r"^\s*#\s*--\s*(?P<name>.*?)\s*-*\s*$")
_ENTRY_KEY = re.compile(r"""^  (?P<key>"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s#"'].*?):(?:[ \t]|$)""")
_PLAIN_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*(?: [A-Za-z0-9._+-]+)*$")
_PLAIN_FORM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .'’_-]*$")


class DictEditError(Exception):
    """A refused change. `hint` says what to do instead."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# ----------------------------------------------------------------- the file --

@dataclass
class Entry:
    key: str
    line: int          # 0-based index of the `  Key: ...` line
    end: int           # one past its last line (continuation lines included)


@dataclass
class Layout:
    lines: list[str]
    terms_line: int
    entries: dict[str, Entry] = field(default_factory=dict)      # by key
    sections: list[tuple[str, int]] = field(default_factory=list)  # (name, header line)


def _parse_key(text: str) -> str:
    try:
        doc = yaml.safe_load(f"{text}: x")
    except yaml.YAMLError:
        return text
    if isinstance(doc, dict) and len(doc) == 1:
        return str(next(iter(doc)))
    return text


def layout(text: str) -> Layout:
    lines = text.split("\n")
    terms_line = next((i for i, line in enumerate(lines) if re.match(r"^terms:\s*(#.*)?$", line)), -1)
    if terms_line < 0:
        raise DictEditError("the dictionary has no top-level `terms:` line",
                            "fix Dictionary.yaml by hand; `wax doctor` shows the parse error")
    out = Layout(lines=lines, terms_line=terms_line)
    i = terms_line + 1
    while i < len(lines):
        line = lines[i]
        if line and not line[0].isspace() and not line.startswith("#"):
            break                                    # the next top-level key
        header = _HEADER.match(line)
        if header:
            out.sections.append((header.group("name"), i))
        m = _ENTRY_KEY.match(line)
        if m and not line.lstrip().startswith("#"):
            j = i + 1
            while j < len(lines) and lines[j].strip() and not lines[j].lstrip().startswith("#") \
                    and (len(lines[j]) - len(lines[j].lstrip()) > 2 or lines[j].startswith("  - ")):
                j += 1
            key = _parse_key(m.group("key"))
            out.entries[key] = Entry(key=key, line=i, end=j)
            i = j
            continue
        i += 1
    return out


def _quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _yaml_plain_ok(text: str) -> bool:
    try:
        return yaml.safe_load(text) == text
    except yaml.YAMLError:
        return False


def _key(term: str) -> str:
    return term if _PLAIN_KEY.match(term) and _yaml_plain_ok(term) else _quote(term)


def _form(text: str) -> str:
    plain = _PLAIN_FORM.match(text) and _yaml_plain_ok(text) and ": " not in text and " #" not in text
    return text if plain else _quote(text)


def render(term: str, heard: list[str], about: str) -> str:
    """One `  Term: ...` line, in the file's own style."""
    if heard and about:
        value = f"{{heard: [{', '.join(_form(h) for h in heard)}], about: {_quote(about)}}}"
    elif heard:
        value = f"[{', '.join(_form(h) for h in heard)}]"
    elif about:
        value = _quote(about)
    else:
        value = ""
    for text in (term, about, *heard):
        bad = next((ch for ch in text if unicodedata.category(ch) in ("Cc", "Cs", "Cn", "Co")), None)
        if bad is not None:
            raise DictEditError(f"{text!r} contains the control character U+{ord(bad):04X}",
                                "retype it without the stray character")
    line = f"  {_key(term)}:" + (f" {value}" if value else "")
    expected = {term: _entry_value(heard, about)}
    if _safe_load(line.strip()) != expected:
        # A form the quick style check let through as plain YAML read back as
        # something else: quote everything rather than guess again.
        line = f"  {_quote(term)}: " + json.dumps(_entry_value(heard, about), ensure_ascii=False)
        if _safe_load(line.strip()) != expected:
            raise DictEditError(f"cannot write {term!r} as one line of YAML")
    return line


def _safe_load(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return _UNPARSEABLE


_UNPARSEABLE = object()


def _entry_value(heard: list[str], about: str) -> Any:
    if heard and about:
        return {"heard": list(heard), "about": about}
    if heard:
        return list(heard)
    return about or None


def _terms(text: str) -> dict[str, Any]:
    doc = yaml.safe_load(text) or {}
    return dict((doc.get("terms") or {}) if isinstance(doc, dict) else {})


def _normalised(value: Any) -> tuple[list[str], str]:
    """(heard, about) of one entry value, as `dictionary._parse_entry` reads it."""
    if value is None:
        return [], ""
    if isinstance(value, str):
        return [], value
    if isinstance(value, list):
        return [str(v) for v in value], ""
    return [str(v) for v in (value.get("heard") or [])], str(value.get("about") or "")


def _section_end(lay: Layout, header: int) -> int:
    """Line after the last entry in the section that starts at `header`."""
    nxt = [ln for _, ln in lay.sections if ln > header]
    stop = min(nxt) if nxt else len(lay.lines)
    last = header
    for entry in lay.entries.values():
        if header < entry.line < stop:
            last = max(last, entry.end - 1)
    return last + 1


def _find_section(lay: Layout, name: str) -> Optional[int]:
    """Exact title, else the one title it starts, else the one title containing it."""
    want = " ".join(name.split()).casefold()
    if not want:
        return None
    for test in (lambda t: t == want, lambda t: t.startswith(want), lambda t: want in t):
        hits = [(title, line) for title, line in lay.sections if test(title.casefold())]
        if len(hits) == 1:
            return hits[0][1]
        if len(hits) > 1:
            raise DictEditError(f"{name!r} matches several sections",
                                "say which: " + "; ".join(t for t, _ in hits))
    return None


def _insert(text: str, line: str, section: Optional[str]) -> str:
    lay = layout(text)
    lines = list(lay.lines)
    name = " ".join((section or "").split()) or DEFAULT_SECTION
    header = _find_section(lay, name)
    if header is not None:
        at = _section_end(lay, header)
        lines[at:at] = [line]
        return "\n".join(lines)
    if name.casefold() != DEFAULT_SECTION.casefold():
        known = ", ".join(t for t, _ in lay.sections) or "none"
        raise DictEditError(f"no section matching {section!r}", f"sections: {known}")
    # A new trailing section, after the last entry of the terms mapping.
    tail = max([e.end for e in lay.entries.values()] or [lay.terms_line + 1])
    while tail > lay.terms_line + 1 and not lines[tail - 1].strip():
        tail -= 1
    width = max((len(lines[ln]) for _, ln in lay.sections), default=80)
    rule = "-" * max(3, width - len(f"  # -- {name} "))
    block = ["", f"  # -- {name} {rule}", line]
    lines[tail:tail] = block
    return "\n".join(lines)


def _replace_line(text: str, term: str, line: Optional[str]) -> str:
    lay = layout(text)
    entry = lay.entries.get(term)
    if entry is None:
        raise DictEditError(f"{term!r} is not in the dictionary")
    if entry.end - entry.line > 1:
        raise DictEditError(f"{term!r} is written over several lines",
                            "edit that entry in Dictionary.yaml by hand")
    lines = list(lay.lines)
    if line is not None:
        line += _trailing_comment(lines[entry.line])
    lines[entry.line:entry.end] = [line] if line is not None else []
    return "\n".join(lines)


def _trailing_comment(line: str) -> str:
    """`   # note` at the end of an entry line (a `#` inside quotes is not one), or ""."""
    whole = _safe_load(line.strip())
    for m in re.finditer(r"[ \t]+#", line):
        head = line[:m.start()]
        if head.strip() and _safe_load(head.strip()) == whole:
            return line[m.start():]
    return ""


# ------------------------------------------------------------------ linting --

_COMMON: Optional[frozenset[str]] = None


def common_words() -> frozenset[str]:
    """Ordinary lower-case English words (proper nouns like "Jared" are not)."""
    global _COMMON
    if _COMMON is None:
        words: set[str] = set()
        for candidate in dictionary.WORDLISTS:
            try:
                with open(candidate, encoding="utf-8", errors="ignore") as handle:
                    words = {w for w in (line.strip() for line in handle) if w.isalpha() and w.islower()}
                break
            except OSError:
                continue
        _COMMON = frozenset(words)
    return _COMMON


@dataclass
class Lint:
    errors: list[str] = field(default_factory=list)     # never written
    refusals: list[str] = field(default_factory=list)   # written only with force
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def extend(self, other: "Lint") -> None:
        self.errors += other.errors
        self.refusals += other.refusals
        self.warnings += other.warnings
        self.notes += other.notes


def lint_term(term: str, book: dictionary.Dictionary) -> Lint:
    out = Lint()
    sq = dictionary.squash(term)
    if not sq:
        out.errors.append(f"{term!r} has no letters or digits")
        return out
    for t in book.terms:
        if t.term.casefold() == term.casefold():
            out.errors.append(f"{t.term!r} is already in the dictionary")
        elif dictionary.squash(t.term) == sq:
            out.errors.append(f"{term!r} is spelled like the existing term {t.term!r}")
    owner = book.heard_index.get(sq)
    if owner is not None:
        out.errors.append(f"{term!r} is listed as a mis-hearing of {owner.term!r}")
    if term.casefold() in common_words() and term != term.casefold():
        out.notes.append(f"lower-case {term.casefold()!r} is an ordinary word: the pass will offer "
                         f"every one as {term!r} and Jev must be fairly sure (0.65) to capitalise it")
    return out


def redundant(term: str, form: str, book: dictionary.Dictionary) -> bool:
    """`form` adds nothing: it IS the term, or the pass already offers it as the term unlisted.

    Decided by the pass's own scan on a one-line sentence, not by spelling: a
    form spelled like the term can still need listing ("automatic a i" ends on
    function words, which the scan only crosses for a listed form)."""
    form = " ".join(form.split())
    if form == term:
        return True
    return offered(term, form, book) in ("exact", "case")


def offered(term: str, form: str, book: dictionary.Dictionary) -> Optional[str]:
    """How the pass would offer `form` as `term` today (heard/exact/case/fuzzy), or None."""
    form = " ".join(form.split())
    for c in dictionary.candidates(f"we said {form} there", book):
        if c.term is not None and c.term.term == term and c.said.casefold() == form.casefold() \
                and c.replacement == term:
            return c.source
    return None


def lint_heard(term: str, form: str, book: dictionary.Dictionary, *, others_only: bool = False) -> Lint:
    """What is wrong with listing `form` as a mis-hearing of `term`."""
    out = Lint()
    sq, tsq = dictionary.squash(form), dictionary.squash(term)
    words = dictionary._norm(form).split()
    if not sq:
        out.errors.append(f"{form!r} has no letters or digits")
        return out
    owner = book.heard_index.get(sq) or book.heard_index.get(dictionary._expanded(form))
    if owner is not None and owner.term != term:
        out.errors.append(f"{form!r} is already a mis-hearing of {owner.term!r}")
    other = book.exact_index.get(sq)
    if other is not None and other.term != term:
        out.errors.append(f"{form!r} is the term {other.term!r}")
    if others_only:
        return out
    how = offered(term, form, book)
    if how in ("exact", "case") or form == term:
        out.notes.append(f"{form!r} is already corrected to {term} without being listed, so it was not added")
        return out
    if how == "fuzzy":
        out.notes.append(f"{form!r} is already offered to Jev by sound; listing it lowers Jev's bar for it "
                         "from 0.5 to 0.4")
    if sq in tsq and sq != tsq:
        out.refusals.append(
            f"{form!r} is a piece of {term}: every {form!r} would become {term}, even beside the rest "
            f"of the word (\"jangler\" once turned \"P jangler\" into \"P pjangler\")")
    if len(words) == 1 and words[0] in common_words():
        out.refusals.append(
            f"{form!r} is an ordinary English word: listing it puts every {form!r} in every transcript "
            "in front of Jev at a lower bar. The pass already finds real sound-alikes by itself")
    elif words and all(w in common_words() or w in dictionary.FUNCTION_WORDS for w in words):
        out.warnings.append(
            f"{form!r} is made of ordinary words; it is right to list only if people really say "
            f"{term} that way (\"blood bank\" for Bloodbank is; \"candy bar\" for Candybar caught a snack)")
    if words and (words[0] in dictionary.FUNCTION_WORDS or words[-1] in dictionary.FUNCTION_WORDS) \
            and len(words) > 1:
        out.warnings.append(f"{form!r} begins or ends with {words[0] if words[0] in dictionary.FUNCTION_WORDS else words[-1]!r};"
                            " a correction will swallow that word too")
    return out


# ------------------------------------------------------------------- writes --

@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock = paths.VAR / "dictionary.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


_REMOVE = object()


def _verify(old: str, new: str, expect: dict[str, Any], path: Path) -> None:
    """The new text loads, and its terms are the old ones with exactly `expect` applied."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tmp:
        tmp.write(new)
    try:
        dictionary.load(Path(tmp.name))
    except dictionary.DictionaryError as exc:
        raise DictEditError(f"the change would break the dictionary: {exc}") from None
    finally:
        os.unlink(tmp.name)
    want = _terms(old)
    for k, v in expect.items():
        if v is _REMOVE:
            want.pop(k, None)
        else:
            want[k] = v
    if _terms(new) != want:
        raise DictEditError(f"refusing to write {path.name}: the edit changed more than {sorted(expect)}")




def _write(path: Path, old: str, new: str) -> None:
    current = path.read_text(encoding="utf-8")
    if current != old:
        raise DictEditError(f"{path} changed while this command ran", "run it again")
    fd, tmp = tempfile.mkstemp(prefix=".Dictionary.", suffix=".yaml", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@dataclass
class Change:
    action: str                  # added | updated | removed | unchanged
    term: str
    line: Optional[str]
    heard: list[str]
    about: str
    lint: Lint
    path: Path
    written: bool


def _target(path: Optional[Path]) -> Path:
    """The real file (through any symlink), which must exist: an edit never creates it."""
    target = Path(path if path else dictionary.default_path()).expanduser().resolve()
    if not target.is_file():
        raise DictEditError(f"no dictionary at {target}",
                            "WAX_DICTIONARY names another file; the shipped one is ~/HeyMa/Dictionary.yaml")
    return target


def _book(path: Path) -> dictionary.Dictionary:
    try:
        return dictionary.load(path)
    except dictionary.DictionaryError as exc:
        raise DictEditError(str(exc), "fix Dictionary.yaml by hand first; `wax doctor` checks it") from None


def add(term: str, *, about: str = "", heard: tuple[str, ...] = (), section: Optional[str] = None,
        update: bool = False, force: bool = False, dry_run: bool = False,
        path: Optional[Path] = None) -> Change:
    """Add a term, or with `update` amend one (new `about` replaces, new `heard` forms append)."""
    target = _target(path)
    term = " ".join(term.split())
    about = " ".join(about.split())
    with _locked(target):
        old = target.read_text(encoding="utf-8")
        book = _book(target)
        lay = layout(old)
        existing = next((k for k in lay.entries if k.casefold() == term.casefold()), None)
        lint = Lint()
        if existing is not None:
            if not update:
                raise DictEditError(f"{existing!r} is already in the dictionary",
                                    f"`wax dict heard {existing} \"...\"` adds a mis-hearing; "
                                    f"`wax dict add {existing} --update --about \"...\"` replaces its description")
            term = existing
            cur_heard, cur_about = _normalised(_terms(old).get(term))
        else:
            lint = lint_term(term, book)
            cur_heard, cur_about = [], ""
        new_heard = list(cur_heard)
        for form in heard:
            form = " ".join(str(form).split())
            if not form:
                continue
            listed = next((h for h in new_heard if form.casefold() == h.casefold()), None)
            if listed is not None:
                lint.notes.append(f"{form!r} is already listed for {term}"
                                  + ("" if listed == form else f" (as {listed!r}; case does not matter)"))
                continue
            lint.extend(lint_heard(term, form, book))
            if not redundant(term, form, book):  # a no-op form is reported, not written
                new_heard.append(form)
        new_about = about or cur_about
        if lint.errors:
            raise DictEditError("; ".join(lint.errors))
        if lint.refusals and not force:
            raise DictEditError("; ".join(lint.refusals), "add --force to list it anyway")
        if existing is not None and new_heard == cur_heard and new_about == cur_about:
            return Change("unchanged", term, None, new_heard, new_about, lint, target, False)
        line = render(term, new_heard, new_about)
        new = _replace_line(old, term, line) if existing is not None else _insert(old, line, section)
        _verify(old, new, {term: _entry_value(new_heard, new_about)}, target)
        if not dry_run:
            _write(target, old, new)
        return Change("updated" if existing is not None else "added", term, line, new_heard,
                      new_about, lint, target, not dry_run)


def remove(term: str, *, heard: tuple[str, ...] = (), dry_run: bool = False,
           path: Optional[Path] = None) -> Change:
    """Remove a term, or only some of its `heard` forms."""
    target = _target(path)
    with _locked(target):
        old = target.read_text(encoding="utf-8")
        _book(target)
        lay = layout(old)
        existing = next((k for k in lay.entries if k.casefold() == term.casefold()), None)
        if existing is None:
            raise DictEditError(f"{term!r} is not in the dictionary", "`wax dict list` shows every term")
        cur_heard, cur_about = _normalised(_terms(old).get(existing))
        if not heard:
            new = _replace_line(old, existing, None)
            _verify(old, new, {existing: _REMOVE}, target)
            if not dry_run:
                _write(target, old, new)
            return Change("removed", existing, None, [], "", Lint(), target, not dry_run)
        drop = {" ".join(h.split()).casefold() for h in heard}
        missing = sorted(drop - {h.casefold() for h in cur_heard})
        if missing:
            raise DictEditError(f"{existing} has no mis-hearing {', '.join(repr(m) for m in missing)}",
                                f"it lists: {', '.join(cur_heard) or 'none'}")
        new_heard = [h for h in cur_heard if h.casefold() not in drop]
        line = render(existing, new_heard, cur_about)
        new = _replace_line(old, existing, line)
        _verify(old, new, {existing: _entry_value(new_heard, cur_about)}, target)
        if not dry_run:
            _write(target, old, new)
        return Change("updated", existing, line, new_heard, cur_about, Lint(), target, not dry_run)


def lookup(term: str, path: Optional[Path] = None) -> Optional[dictionary.Term]:
    book = _book(Path(path) if path else dictionary.default_path())
    return next((t for t in book.terms if t.term.casefold() == term.casefold()), None)


def listing(path: Optional[Path] = None) -> list[dict[str, Any]]:
    target = Path(path) if path else dictionary.default_path()
    book = _book(target)
    lay = layout(target.read_text(encoding="utf-8"))
    by_line = sorted(lay.sections, key=lambda s: s[1])

    def section_of(term: str) -> str:
        entry = lay.entries.get(term)
        name = ""
        for title, line in by_line:
            if entry is not None and line < entry.line:
                name = title
        return name

    return [{"term": t.term, "heard": list(t.listed), "about": t.about, "section": section_of(t.term)}
            for t in book.terms]


# --------------------------------------------------------------------- scan --

def notes(vault: Optional[Path] = None) -> list[Path]:
    root = Path(vault) if vault else paths.VAULT
    return sorted(p for p in root.rglob("*.md")
                  if not any(part.startswith(".") for part in p.relative_to(root).parts))


_SCAN: dict[str, Any] = {}


@contextlib.contextmanager
def _sigint_ignored() -> Iterator[None]:
    """SIGINT ignored for the block (only the main thread may change handlers; elsewhere, a no-op)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _scan_init(book: dictionary.Dictionary, term: str, min_score: float, worker: bool = False) -> None:
    if worker:
        # Ctrl-C belongs to the parent, which stops the pool; a worker that died of
        # it too would leave the pool waiting on a result that never comes.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    found = next(t for t in book.terms if t.term == term)
    _SCAN.update(book=book, term=term, found=found, min_score=min_score, memo={}, loose_memo={},
                 first_look={})


def _scan_note(path_str: str) -> dict[str, Any]:
    book, term = _SCAN["book"], _SCAN["term"]
    try:
        _fm, body = frontmatter.split(Path(path_str).read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return {"path": path_str, "caught": [], "near": [], "right": 0}
    mask = dictionary.protected_mask(body)
    right = 0
    for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", body):
        if not any(mask[m.start():m.end()]):
            right += 1
    # Most notes never come near the term: a look that sounds out each run against
    # this one term (not all of them) clears those, and is never wrong about the
    # rest (dictionary.may_offer). Only what it lets through gets the full match.
    if not dictionary.may_offer(body, book, _SCAN["found"],
                                min_score=min(_SCAN["min_score"], NEAR_MISS_MIN_SCORE),
                                memo=_SCAN["first_look"]):
        return {"path": path_str, "caught": [], "near": [], "right": right}
    caught = [(c.said, c.source, c.score, dictionary.excerpt(body, c.start, c.end, 70, mask))
              for c in dictionary.candidates(body, book, min_score=_SCAN["min_score"],
                                             memo=_SCAN["memo"])
              if c.term is not None and c.term.term == term]
    spans = {s for s, *_ in caught}
    near = []
    if _SCAN["min_score"] > NEAR_MISS_MIN_SCORE:
        for c in dictionary.candidates(body, book, min_score=NEAR_MISS_MIN_SCORE, spoken_dot=False,
                                       memo=_SCAN["loose_memo"]):
            if c.term is not None and c.term.term == term and c.source == "fuzzy" \
                    and c.score < _SCAN["min_score"] and c.said not in spans:
                near.append((c.said, c.source, c.score, dictionary.excerpt(body, c.start, c.end, 70, mask)))
    return {"path": path_str, "caught": caught, "near": near, "right": right}


def scan(term: str, *, book: Optional[dictionary.Dictionary] = None, vault: Optional[Path] = None,
         min_score: Optional[float] = None, workers: int = WORKERS,
         progress: Optional[Callable[[int, int], None]] = None) -> dict[str, Any]:
    """How `term` fares across the vault: spelled right, caught by the pass, and near misses.

    `progress(done, total)` is called as notes finish. Interrupting (Ctrl-C)
    stops the worker processes at once and re-raises KeyboardInterrupt.
    """
    book = book or _book(dictionary.default_path())
    found = next((t for t in book.terms if t.term.casefold() == term.casefold()), None)
    if found is None:
        raise DictEditError(f"{term!r} is not in the dictionary")
    term = found.term
    if min_score is None:
        min_score = float(os.environ.get("WAX_DICTIONARY_MIN_SCORE") or dictionary.DEFAULT_MIN_SCORE)
    files = [str(p) for p in notes(vault)]
    results: list[dict[str, Any]] = []

    def tick() -> None:
        if progress is not None:
            progress(len(results), len(files))

    tick()
    if workers > 1 and len(files) > 50:
        # Workers fork with SIGINT ignored, so a Ctrl-C reaches only this process,
        # which then stops them. ProcessPoolExecutor (not multiprocessing.Pool) so a
        # worker killed outright (OOM) fails the scan instead of hanging it.
        with _sigint_ignored():
            pool = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("fork"),
                                       initializer=_scan_init, initargs=(book, term, min_score, True))
            mapped = pool.map(_scan_note, files, chunksize=8)
        try:
            for r in mapped:
                results.append(r)
                tick()
        except BaseException:
            with _sigint_ignored():  # a second Ctrl-C must not leave the pool half stopped
                workers_left = list((getattr(pool, "_processes", None) or {}).values())
                pool.shutdown(wait=False, cancel_futures=True)
                for proc in workers_left:
                    proc.terminate()
                for proc in workers_left:
                    proc.join(5)
            raise
        pool.shutdown()
    else:
        _scan_init(book, term, min_score)
        for f in files:
            results.append(_scan_note(f))
            tick()
    return summarise(term, results, len(files), min_score)


def summarise(term: str, results: list[dict[str, Any]], scanned: int, min_score: float) -> dict[str, Any]:
    def group(kind: str) -> list[dict[str, Any]]:
        counts: Counter[str] = Counter()
        in_notes: dict[str, set[str]] = {}
        meta: dict[str, tuple[str, float]] = {}
        examples: dict[str, list[str]] = {}
        for r in results:
            for said, source, score, ctx in r[kind]:
                key = said.casefold()
                counts[key] += 1
                in_notes.setdefault(key, set()).add(r["path"])
                meta.setdefault(key, (source, score))
                if len(examples.setdefault(key, [])) < MAX_EXAMPLES:
                    examples[key].append(ctx)
        return [{"said": key, "count": n, "notes": len(in_notes[key]), "source": meta[key][0],
                 "score": meta[key][1], "examples": examples[key]}
                for key, n in counts.most_common()]

    caught = group("caught")
    return {
        "term": term,
        "notes_scanned": scanned,
        "min_score": min_score,
        "near_miss_min_score": NEAR_MISS_MIN_SCORE,
        "spelled_right": sum(r["right"] for r in results),
        "spelled_right_notes": sum(1 for r in results if r["right"]),
        "caught": caught,
        "caught_total": sum(c["count"] for c in caught),
        "near": group("near"),
        "notes_to_fix": sorted(r["path"] for r in results if r["caught"]),
    }


# ---------------------------------------------------------------------- fix --

def fix_plan(note_paths: list[str]) -> list[dict[str, Any]]:
    """How each note gets the pass: its own Wax item, a document run, or not at all."""
    from . import ledger
    db = ledger.connect()
    plan = []
    for p in note_paths:
        path = Path(p)
        try:
            fm, _ = frontmatter.split(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError as exc:
            plan.append({"path": p, "how": "skip", "why": f"unreadable: {exc}"})
            continue
        item = str(fm.get(frontmatter.ITEM_KEY) or "").strip()
        if item and not item.startswith("doc-"):
            row = db.execute("SELECT md_path FROM transcripts WHERE item_id=?", (item,)).fetchone()
            if row is not None and row["md_path"] and Path(row["md_path"]) == path:
                plan.append({"path": p, "how": "item", "item_id": item})
            elif row is not None:
                plan.append({"path": p, "how": "skip",
                             "why": f"carries item {item}, whose transcript is {row['md_path']}"})
            else:
                plan.append({"path": p, "how": "skip", "why": f"item {item} is not in the ledger"})
        else:
            plan.append({"path": p, "how": "document"})
    return plan


def _first(text: Any) -> str:
    return str(text or "").strip().splitlines()[0][:200] if str(text or "").strip() else ""


def count_term(path: Path, term: str) -> int:
    try:
        _fm, body = frontmatter.split(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return 0
    return len(re.findall(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", body))


def fix_one(step: dict[str, Any], term: str) -> dict[str, Any]:
    """Run dictionary-correction on one planned note; report how many `term`s it gained."""
    from . import finalize, ledger, passes, pipelines
    path = Path(step["path"])
    before = count_term(path, term)
    out: dict[str, Any] = {"path": step["path"], "how": step["how"]}
    try:
        if step["how"] == "item":
            result = passes.run(step["item_id"], "dictionary-correction", operator_toggle=False)
            finalize.publish_pass_events(step["item_id"])
            finalize.finalize(step["item_id"])
            out["state"] = result.get("state") or "completed"
            if result.get("reason_code") or result.get("error"):
                out["error"] = f"{result.get('reason_code') or ''} {_first(result.get('error'))}".strip()
            current = ledger.connect().execute(
                "SELECT md_path FROM transcripts WHERE item_id=?", (step["item_id"],)).fetchone()
            if current is not None and current["md_path"]:
                path = Path(current["md_path"])
        else:
            result = pipelines.run_document("dictionary-only", path, binding_id="", explicit=True)
            states = sorted({str(r.get("state")) for r in result.get("results") or []})
            out["state"] = "failed" if result.get("failed") else (",".join(states) or
                                                                  result.get("skipped") or "completed")
            bad = next((r for r in result.get("results") or [] if r.get("state") in ("failed", "skipped")), None)
            if bad is not None and (bad.get("reason_code") or bad.get("error")):
                out["error"] = f"{bad.get('reason_code') or ''} {_first(bad.get('error'))}".strip()
    except Exception as exc:  # noqa: BLE001 - one note never stops the rest
        out["state"] = "failed"
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    out["gained"] = count_term(path, term) - before
    return out
