"""The HeyMa custom dictionary, and the spans of a transcript that may mis-spell it.

Speech recognition spells the speaker's own vocabulary a different way every
time: Skillex comes out "Skillix", "skill X" or "skill ex", DeLoNET comes out
"D low net", a domain comes out "delo dot sh". This module does the part a
human proof-reader does without thinking, and does it deterministically:

1. `load()` reads the dictionary (`$WAX_ROOT/Dictionary.yaml`, override with
   `$WAX_DICTIONARY`): canonical terms, the mis-hearings already seen for
   each, and one line saying what the term is.
2. `candidates()` walks the body, skipping what the speaker did not say
   (the `# Transcription:` header, `**Speaker (mm:ss):**` labels, code, URLs,
   links), and proposes every run of one to four words that SOUNDS like a
   term but is not spelled like it, plus every spoken "dot" between a name and
   a domain or file suffix.
3. `apply()` writes the accepted replacements back, leaving every other byte
   of the body exactly as it was.

Whether a proposal is really a mis-hearing ("skill set" is not Skillex, "hey
ma, pass the salt" is not HeyMa) is not decided here; the pass asks Jev about
each one in context. Matching is deliberately generous for that reason, and
deliberately cheap: a first-sound bucket and a length window keep the fuzzy
comparison to a handful of terms per span.
"""

import difflib
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from . import paths

FILE_NAME = "Dictionary.yaml"
MAX_NGRAM = 4
# A fuzzy match below this never reaches Jev. Measured on the vault's 944
# transcripts (docs/ENRICHMENT-PASSES.md, dictionary-correction).
DEFAULT_MIN_SCORE = 0.82
# A run made only of ordinary English words ("say anything", "him to") needs
# a closer spelling than one containing a non-word ("Skillix", "Delonet").
ENGLISH_RUN_MIN_SCORE = 0.9
SOUND_ALIKE_SCORE = 0.9
# Terms shorter than this (squashed) match only exactly or through a listed
# mis-hearing: "uv" or "px" sound like half the language.
MIN_FUZZY_LEN = 5
# Spoken suffixes after "dot" that make the run a domain or a file name.
DOT_SUFFIXES = frozenset("""
    sh io com net org ai dev app co me us uk ca de tv gg xyz info biz cloud
    md py js ts tsx jsx json yaml yml toml txt env sh csv html css log sql go rs
    """.split())
TLDS = frozenset("sh io com net org ai dev app co me us uk ca de tv gg xyz info biz cloud".split())
LETTER_NAMES = {
    "a": "ay", "b": "bee", "c": "see", "d": "dee", "e": "ee", "f": "ef", "g": "gee",
    "h": "aitch", "i": "eye", "j": "jay", "k": "kay", "l": "el", "m": "em", "n": "en",
    "o": "oh", "p": "pee", "q": "cue", "r": "ar", "s": "es", "t": "tee", "u": "you",
    "v": "vee", "w": "doubleyou", "x": "ex", "y": "why", "z": "zee",
}
WORDLISTS = ("/usr/share/dict/american-english", "/usr/share/dict/words")

_TOKEN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9'’.-]*[A-Za-z0-9])?")
_POSSESSIVE = re.compile(r"(['’]s)$", re.IGNORECASE)
# What a speaker did not say, or what a correction must never edit.
_PROTECTED = [
    re.compile(r"^# Transcription:.*$", re.MULTILINE),
    re.compile(r"^\s*\*\*[^*\n]{1,120}\*\*", re.MULTILINE),     # **Speaker 1 (00:12):**
    re.compile(r"```.*?(?:```|\Z)", re.DOTALL),
    re.compile(r"`[^`\n]*`"),
    re.compile(r"\[\[[^\]\n]*\]\]"),                             # [[wiki links]]
    re.compile(r"\]\([^)\n]*\)"),                                # ](link targets)
    re.compile(r"\b(?:https?|ftp|file)://\S+", re.IGNORECASE),
    re.compile(r"<[^>\n]+>"),                                    # <html> / <autolinks>
]
# Separators allowed inside one candidate run: spaces or a hyphen, never a
# comma or a sentence break ("skill. X" is two thoughts, not one word).
_JOINABLE = re.compile(r"[ \t]+|[ \t]*-[ \t]*")
# Words a mis-hearing never begins or ends with unless the speaker listed it
# that way: "Dami and" is a name and a conjunction, "in Telephoria" keeps its
# "in", "automatic and I've" is not AutomaticAI (labelled sample, 2026-11-04).
FUNCTION_WORDS = frozenset("""
a an the and or but nor so if as at by for from in into of off on onto out to up with
i i'm i've i'd i'll me my we us our you your he him his she her it its they them their
is am are was were be been do did doesn't don't didn't can't won't isn't it's that's
this that these those there here not no
""".split())
_SPELLED_LABEL = re.compile(r"^[A-Za-z](?:-[A-Za-z0-9])+$")


class DictionaryError(Exception):
    """reason_code: dictionary_missing | dictionary_invalid"""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class Term:
    term: str
    heard: tuple[str, ...] = ()
    about: str = ""
    # every heard form as written, including ones spelled like the term itself
    # ("automatic a i"): they say which edge words a run may keep
    listed: tuple[str, ...] = field(default=(), compare=False, repr=False)


@dataclass
class Dictionary:
    terms: list[Term]
    path: Path
    sha256: str
    # squashed mis-hearing -> term, and squashed canonical -> term
    heard_index: dict[str, Term] = field(default_factory=dict)
    exact_index: dict[str, Term] = field(default_factory=dict)
    # first sound -> [(term, keys)]
    buckets: dict[str, list[tuple[Term, tuple[str, ...]]]] = field(default_factory=dict)
    # every term and heard form, word-normalised ("automatic a i")
    spoken_forms: frozenset[str] = frozenset()


@dataclass
class Candidate:
    start: int
    end: int
    said: str
    replacement: str
    term: Optional[Term]       # None for the spoken-dot rule
    source: str                # heard | exact | case | fuzzy | dot
    score: float


def default_path() -> Path:
    override = os.environ.get("WAX_DICTIONARY", "").strip()
    return Path(override).expanduser() if override else paths.HEYMA / FILE_NAME


def squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _expanded(text: str) -> str:
    """Single letters as their spoken names: "skill X" -> "skillex", "D low" -> "deelow"."""
    words = re.findall(r"[A-Za-z0-9]+", text)
    return "".join(LETTER_NAMES.get(w.casefold(), w.casefold()) if len(w) == 1 else w.casefold()
                   for w in words)


def phonetic(text: str) -> str:
    """A crude sound key: vowel runs (with y and a trailing w/h) collapse to one vowel.

    "Haema", "Hayma", "Haima" and "HeyMa" all key to "hama"; "Skillix" and
    "Skillex" to "skalax". Crude is fine: it only proposes, Jev decides.
    """
    s = squash(text)
    for a, b in (("ph", "f"), ("ck", "k"), ("qu", "kw"), ("wh", "w"), ("z", "s"), ("c", "k")):
        s = s.replace(a, b)
    s = re.sub(r"[aeiouy]+[wh]?(?![aeiouy])", "a", s)
    s = re.sub(r"(.)\1+", r"\1", s)
    return s


def _keys(text: str) -> tuple[str, ...]:
    return (squash(text), _expanded(text), phonetic(text), phonetic(_expanded(text)))


def _parse_entry(name: Any, value: Any, where: Path) -> Term:
    if not isinstance(name, str) or not name.strip():
        raise DictionaryError("dictionary_invalid", f"{where}: a term must be a non-empty string, got {name!r}")
    heard: Any = []
    about: Any = ""
    if value is None:
        pass
    elif isinstance(value, str):
        about = value
    elif isinstance(value, list):
        heard = value
    elif isinstance(value, dict):
        extra = set(value) - {"heard", "about"}
        if extra:
            raise DictionaryError("dictionary_invalid", f"{where}: {name}: unknown keys {sorted(extra)}")
        heard = value.get("heard") or []
        about = value.get("about") or ""
    else:
        raise DictionaryError("dictionary_invalid", f"{where}: {name}: expected nothing, a list of "
                              "mis-hearings, a description, or {heard, about}")
    if not isinstance(heard, list) or any(not isinstance(h, (str, int)) or not str(h).strip() for h in heard):
        raise DictionaryError("dictionary_invalid", f"{where}: {name}: heard must be a list of strings")
    if not isinstance(about, str):
        raise DictionaryError("dictionary_invalid", f"{where}: {name}: about must be a string")
    term = name.strip()
    if not squash(term):
        raise DictionaryError("dictionary_invalid", f"{where}: {term!r} has no letters or digits")
    forms = tuple(dict.fromkeys(str(h).strip() for h in heard if squash(str(h)) != squash(term)))
    listed = tuple(dict.fromkeys(str(h).strip() for h in heard))
    return Term(term=term, heard=forms, about=" ".join(about.split()), listed=listed)


def load(path: Optional[Path] = None) -> Dictionary:
    """Parse the dictionary; missing or malformed is an error, never an empty dictionary.

    A typo that silently emptied the dictionary would turn the pass into a
    no-op that still reports success, which is exactly how Wax fails.
    """
    target = Path(path) if path else default_path()
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        raise DictionaryError("dictionary_missing", f"{target} does not exist") from None
    except OSError as exc:
        raise DictionaryError("dictionary_missing", f"{target}: {exc}") from None
    try:
        doc = yaml.safe_load(raw.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DictionaryError("dictionary_invalid", f"{target}: unparseable ({exc})") from None
    entries = doc.get("terms") if isinstance(doc, dict) else None
    if entries is None:
        entries = {}
    if not isinstance(entries, dict):
        raise DictionaryError("dictionary_invalid", f"{target}: expected a top-level 'terms' mapping")
    terms = [_parse_entry(name, value, target) for name, value in entries.items()]
    seen: dict[str, str] = {}
    for t in terms:
        if t.term.casefold() in seen:
            raise DictionaryError("dictionary_invalid", f"{target}: {t.term!r} is listed twice")
        seen[t.term.casefold()] = t.term
    book = Dictionary(terms=terms, path=target, sha256=hashlib.sha256(raw).hexdigest())
    for t in terms:
        book.exact_index.setdefault(squash(t.term), t)
    for t in terms:
        for h in t.heard:
            key = squash(h)
            other = book.heard_index.get(key)
            if other is not None and other.term != t.term:
                raise DictionaryError("dictionary_invalid",
                                      f"{target}: {h!r} is listed as a mis-hearing of both {other.term!r} and {t.term!r}")
            if key in book.exact_index and book.exact_index[key].term != t.term:
                raise DictionaryError("dictionary_invalid",
                                      f"{target}: {h!r} (heard for {t.term!r}) is itself the term "
                                      f"{book.exact_index[key].term!r}")
            book.heard_index[key] = t
        keys = _keys(t.term)
        if len(keys[0]) >= MIN_FUZZY_LEN:
            for first in {k[0] for k in keys if k}:
                book.buckets.setdefault(first, []).append((t, keys))
    book.spoken_forms = frozenset(_norm(f) for t in terms for f in (t.term, *t.listed))
    return book


_ENGLISH: Optional[frozenset[str]] = None


def english() -> frozenset[str]:
    """Lower-cased system word list, or empty when the machine has none."""
    global _ENGLISH
    if _ENGLISH is None:
        words: set[str] = set()
        for candidate in WORDLISTS:
            try:
                with open(candidate, encoding="utf-8", errors="ignore") as handle:
                    words = {line.strip().casefold() for line in handle if line.strip().isalpha()}
                break
            except OSError:
                continue
        _ENGLISH = frozenset(words)
    return _ENGLISH


def protected_mask(body: str) -> bytearray:
    mask = bytearray(len(body))
    for pattern in _PROTECTED:
        for m in pattern.finditer(body):
            mask[m.start():m.end()] = b"\x01" * (m.end() - m.start())
    return mask


_MATCHERS: dict[str, difflib.SequenceMatcher] = {}


def _ratio(a: str, b: str, floor: float = 0.6) -> float:
    """difflib's ratio, or 0.0 as soon as it provably cannot reach `floor`.

    `b` is always a term key, so its matcher (whose index of `b` is the
    expensive part) is built once per term and reused for every span.
    """
    if not a or not b:
        return 0.0
    la, lb = len(a), len(b)
    if 2.0 * min(la, lb) / (la + lb) < floor:
        return 0.0
    matcher = _MATCHERS.get(b)
    if matcher is None:
        matcher = _MATCHERS[b] = difflib.SequenceMatcher(None, "", b, autojunk=False)
    matcher.set_seq1(a)
    if matcher.quick_ratio() < floor:
        return 0.0
    return matcher.ratio()


def _score(span_keys: tuple[str, ...], term_keys: tuple[str, ...], *, sound: bool = True,
           floor: float = 0.0) -> float:
    """How much a span looks like a term: its spelling, or a sound-alike that is nearly a spelling.

    A sound key alone is too loose (with vowels collapsed, "him to" is one
    letter from "HeyMa"), so it only counts when the keys are IDENTICAL, long
    enough to mean something, and the spelling is already more than half there,
    and never for a run of ordinary English words ("him a" keys like HeyMa).
    A result below `floor` may be reported as 0.0.
    """
    s_sq, s_ex, s_ph, s_phx = span_keys
    t_sq, _t_ex, t_ph, _ = term_keys
    if sound and len(t_ph) >= 4 and t_ph in (s_ph, s_phx):
        spelled = max(_ratio(s_sq, t_sq), _ratio(s_ex, t_sq))
        return max(spelled, SOUND_ALIKE_SCORE) if spelled >= 0.6 else spelled
    cut = max(floor, 0.6)
    return max(_ratio(s_sq, t_sq, cut), _ratio(s_ex, t_sq, cut))


def _tokens(body: str, mask: bytearray) -> list[tuple[int, int]]:
    out = []
    for m in _TOKEN.finditer(body):
        start, end = m.start(), m.end()
        if any(mask[start:end]):
            continue
        out.append((start, end))
    return out


def _possessive_split(body: str, start: int, end: int) -> int:
    """End of the span minus a trailing 's, so "skill X's" corrects to "Skillex's"."""
    m = _POSSESSIVE.search(body[start:end])
    return end - len(m.group(1)) if m and end - start > len(m.group(1)) + 1 else end


def _spelled_out(said: str) -> bool:
    """"J-A-R-A-D" or "d e l o" is the speaker spelling a word aloud, on purpose."""
    parts = re.findall(r"[A-Za-z0-9]+", said)
    return len(parts) >= 3 and all(len(p) == 1 for p in parts)


def _match(said: str, book: Dictionary, words: frozenset[str],
           min_score: float) -> Optional[tuple[Term, str, float]]:
    """(term, source, score) for one run of words, or None."""
    keys = _keys(said)
    sq = keys[0]
    if not sq or _spelled_out(said):
        return None
    # 1. The speaker's own list of mis-hearings.
    if _function_edge(said, book):
        return None
    term = book.heard_index.get(sq) or book.heard_index.get(keys[1])
    if term is not None:
        return (term, "heard", 1.0) if said != term.term else None
    # 2. Same letters, wrong case or spacing ("Delo net", "delonet").
    term = book.exact_index.get(sq) or book.exact_index.get(keys[1])
    if term is not None:
        if said == term.term or _sentence_case(said, term.term):
            return None
        # Only the case differs and the word is ordinary English ("plane",
        # "wax", "in hindsight"): usually right as written.
        source = "case" if said.casefold() == term.term.casefold() and sq in words else "exact"
        return term, source, 0.99
    # 3. Sounds like a term.
    if len(sq) < MIN_FUZZY_LEN:
        return None
    if "dot" in _norm(said).split():
        # A spoken "dot" is the dot rule's to read ("delo dot sh" is delo.sh, not DeLoDocs).
        return None
    if sq[-1].isdigit():
        # "GoHighLevel2 plumbers", "Vox CPM2": a number the term does not end in
        # is another word (or another version), never the term mis-heard.
        return None
    parts = [squash(w) for w in re.findall(r"[A-Za-z0-9]+", said)]
    all_english = bool(words) and all(p in words for p in parts)
    if all_english and len(parts) == 1:
        # One ordinary English word ("skills", "deli") is not a mis-hearing by
        # sound alone; a listed mis-hearing or a respelling still is.
        return None
    floor = max(min_score, ENGLISH_RUN_MIN_SCORE) if all_english else min_score
    pool: dict[str, tuple[Term, tuple[str, ...]]] = {}
    for first in {k[0] for k in keys if k}:
        for t, tkeys in book.buckets.get(first, ()):
            pool[t.term] = (t, tkeys)
    best: Optional[tuple[float, Term]] = None
    for t, tkeys in pool.values():
        tl = len(tkeys[0])
        if not (0.6 * tl <= len(sq) <= 1.5 * tl + 2) or not _shape_fits(said, t.term) \
                or (sq[0].isdigit() != tkeys[0][0].isdigit()):
            continue
        score = _score(keys, tkeys, sound=not all_english, floor=floor)
        if score >= floor and (best is None or score > best[0]):
            best = (score, t)
    return (best[1], "fuzzy", round(best[0], 3)) if best is not None else None


def candidates(body: str, book: Dictionary, *, min_score: float = DEFAULT_MIN_SCORE,
               spoken_dot: bool = True) -> list[Candidate]:
    """Every span that may be a mis-spelling, best match per span, overlaps kept.

    Overlaps are resolved after Jev answers (`resolve`), because which of two
    overlapping readings is right is exactly the question Jev is asked.
    """
    mask = protected_mask(body)
    tokens = _tokens(body, mask)
    words = english()
    found: dict[tuple[int, int], Candidate] = {}
    # The same few thousand word runs recur all through a long meeting.
    memo: dict[str, Optional[tuple[Term, str, float]]] = {}

    def offer(c: Candidate) -> None:
        key = (c.start, c.end)
        if key not in found or c.score > found[key].score:
            found[key] = c

    for i in range(len(tokens)):
        for n in range(1, MAX_NGRAM + 1):
            if i + n > len(tokens):
                break
            if n > 1:
                gap = body[tokens[i + n - 2][1]:tokens[i + n - 1][0]]
                if not _JOINABLE.fullmatch(gap) or "\n" in gap:
                    break
            start = tokens[i][0]
            end = _possessive_split(body, start, tokens[i + n - 1][1])
            said = body[start:end]
            if said not in memo:
                memo[said] = _match(said, book, words, min_score)
            hit = memo[said]
            suffix = ""
            if (hit is None or hit[1] == "fuzzy") and len(said) > 4 and said.endswith("s"):
                # "gorilla desks" is GorillaDesk's plural; the replacement keeps the "s".
                stem = said[:-1]
                if stem not in memo:
                    memo[stem] = _match(stem, book, words, min_score)
                if memo[stem] is not None and memo[stem][1] in ("heard", "exact"):
                    hit, suffix = memo[stem], "s"
            if hit is not None:
                term, source, score = hit
                offer(Candidate(start, end, said, term.term + suffix, term, source, score))

    out = _dominant(list(found.values()))
    if spoken_dot:
        claimed = [(c.start, c.end) for c in out if c.source in ("heard", "exact")]
        # "foul dot ai" listed as heard for fal.ai beats the generic "foul.ai".
        out.extend(d for d in dot_candidates(body, mask, book, min_score)
                   if not any(lo <= d.start and d.end <= hi for lo, hi in claimed))
    out.sort(key=lambda c: (c.start, c.end))
    return out


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9'’]+", text.casefold()))


def _function_edge(said: str, book: Dictionary) -> bool:
    """A multi-word run that begins or ends with a function word, not as listed.

    Allowed only when the run is spelled exactly like a term or a `heard` form,
    so "automatic a i" (listed) still matches while "in telephoria" does not.
    """
    words = _norm(said).split()
    if len(words) < 2 or not (words[0] in FUNCTION_WORDS or words[-1] in FUNCTION_WORDS):
        return False
    return _norm(said) not in book.spoken_forms


def _sentence_case(said: str, term: str) -> bool:
    """"Pjangler" at the start of a sentence is the term, capitalised, not a mis-spelling."""
    return said == term[:1].upper() + term[1:] and term[:1].islower()


def _labels(text: str) -> list[str]:
    return [part for part in text.split(".") if part]


def _shape_fits(said: str, term: str) -> bool:
    """A fuzzy match must have the term's shape where shape is meaningful.

    A domain term needs its suffix to have been heard ("dlo sh" and "dilo.sh"
    are delo.sh; "delos" and "de los" are not), and a span already written
    as a dotted name must have as many labels as the term ("api.automaticai.io"
    is a different host from automaticai.io, not a mis-spelling of it).
    """
    if "." in said.strip("."):
        if len(_labels(said)) != len(_labels(term)):
            return False
    if "." in term.strip("."):
        suffix = squash(_labels(term)[-1])
        last = re.findall(r"[A-Za-z0-9]+", said)
        if not last or not squash(last[-1]).endswith(suffix):
            return False
    return True


def _dominant(cands: list[Candidate]) -> list[Candidate]:
    """Drop a fuzzy span that only matches because of words at its edges.

    "Skillex is" sounds 0.88 like Skillex, but "Skillex" alone is exact; "automatic
    ai oh" sounds like automaticai.io, but "automatic ai" is exactly AutomaticAI.
    A span loses to a strictly shorter span inside it that matches the same term at
    least as well, or that is a heard/exact match for any term; a fuzzy span
    containing a term spelled right is never a mis-hearing; and any span strictly
    inside a heard/exact span for the same term gives way to it.
    """
    keep = []
    for c in cands:
        beaten = c.source == "fuzzy" and any(
            o is not c and o.start >= c.start and o.end <= c.end and (o.end - o.start) < (c.end - c.start)
            and ((o.replacement == c.replacement and o.score >= c.score) or o.source in ("heard", "exact", "case"))
            for o in cands)
        inner_exact = c.source == "fuzzy" and any(
            w.casefold() == t.casefold()
            for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]|[A-Za-z0-9]", c.said)
            for t in ([c.term.term] if c.term is not None else []))
        # "automatic a" inside "automatic a I", "jangler" inside "P jangler": the
        # longer listed reading owns those words; the fragment would leave a stray.
        enclosed = any(
            o is not c and o.source in ("heard", "exact") and o.term is not None and o.term == c.term
            and o.start <= c.start and c.end <= o.end and (o.end - o.start) > (c.end - c.start)
            for o in cands)
        if not beaten and not inner_exact and not enclosed:
            keep.append(c)
    return keep


_LEAD_WORDS = frozenset("a an the my your our their his her its this that of in on at to for and or with".split())
_DOT_RUN = re.compile(
    r"(?<![\w.])([A-Za-z0-9][A-Za-z0-9-]*)((?:[ \t]+dot[ \t]+[A-Za-z0-9][A-Za-z0-9-]*)+)(?!\w|\.\w)",
    re.IGNORECASE)


def dot_candidates(body: str, mask: bytearray, book: Dictionary,
                   min_score: float = DEFAULT_MIN_SCORE) -> list[Candidate]:
    """"delo dot sh" -> "delo.sh"; "drive dot dlo dot sh" -> "drive.delo.sh".

    The run must end in a domain or file suffix. Domains are lower-cased; a
    dictionary domain that the trailing labels sound like supplies its spelling.
    """
    domains = [t for t in book.terms if "." in t.term.strip(".")]
    out = []
    for m in _DOT_RUN.finditer(body):
        if any(mask[m.start():m.end()]):
            continue
        parts = [m.group(1)] + re.findall(r"dot[ \t]+([A-Za-z0-9][A-Za-z0-9-]*)", m.group(2), re.IGNORECASE)
        if len(parts) < 2 or parts[-1].casefold() not in DOT_SUFFIXES:
            continue
        if any(_SPELLED_LABEL.match(p) for p in parts) or (
                len(parts[0]) == 1 and re.search(r"(?:^|\s)[A-Za-z][ \t]+$", body[:m.start()])):
            # "C-A-L dot D-E-L-O dot SH", "D E L o dot sh": the speaker is spelling
            # an address letter by letter; a one-label rewrite would garble it.
            continue
        start, said = m.start(), m.group(0)
        if parts[0].casefold() in _LEAD_WORDS:
            # "a dot env file" is "a .env file", not "a.env": the article stays a word.
            lead = len(m.group(1))
            start += lead + (len(said[lead:]) - len(said[lead:].lstrip()))
            said, parts = body[start:m.end()], [""] + parts[1:]
        if parts[-1].casefold() in TLDS:
            parts = [p.casefold() for p in parts]
        else:
            parts[-1] = parts[-1].casefold()
        known = None
        for t in domains:
            labels = _labels(t.term)
            if len(labels) > len(parts):
                continue
            tail = ".".join(parts[-len(labels):])
            if tail.casefold() == t.term.casefold() or (
                    squash(labels[-1]) == squash(parts[-1])
                    and _ratio(squash(tail), squash(t.term)) >= min_score):
                known = t
                parts = parts[:-len(labels)] + [t.term]
                break
        joined = ".".join(parts)
        out.append(Candidate(start, m.end(), said, joined, known, "dot", 0.9))
    return out


def resolve(accepted: Iterable[tuple[Candidate, float]]) -> list[Candidate]:
    """Non-overlapping replacements: most probable first, then the longer span."""
    chosen: list[Candidate] = []
    for c, _p in sorted(accepted, key=lambda cp: (-cp[1], -(cp[0].end - cp[0].start), cp[0].start)):
        if all(c.end <= o.start or c.start >= o.end for o in chosen):
            chosen.append(c)
    return sorted(chosen, key=lambda c: c.start)


def apply(body: str, chosen: list[Candidate]) -> str:
    out, cursor = [], 0
    for c in sorted(chosen, key=lambda c: c.start):
        if c.start < cursor:
            raise ValueError("overlapping replacements")
        out.append(body[cursor:c.start])
        out.append(c.replacement)
        cursor = c.end
    out.append(body[cursor:])
    return "".join(out)


def excerpt(body: str, start: int, end: int, radius: int = 160, mask: Optional[bytearray] = None) -> str:
    """The words around a span, on word boundaries, with what the speaker did not say blanked.

    The `# Transcription:` header, speaker labels, code and links are not
    speech; shown to Jev they only add noise.
    """
    lo, hi = max(0, start - radius), min(len(body), end + radius)
    while lo > 0 and not body[lo - 1].isspace():
        lo -= 1
    while hi < len(body) and not body[hi].isspace():
        hi += 1
    if mask is None:
        mask = protected_mask(body)
    text = "".join(" " if mask[i] else body[i] for i in range(lo, hi))
    return " ".join(text.split())
