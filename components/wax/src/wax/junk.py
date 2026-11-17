"""The junk gate: reject recordings that hold nothing before they cost anything.

A voicemail, a call that is only "Hello? Hello?", a one-word dictation misfire,
a silent clip: none of these is worth a vault note, a row in the transcripts
store, or a single enrichment-pass token. ``wax transcripts prune`` used to
sweep them out nightly, but only after every pass (title-slug, classification,
dictionary-correction, voice-id ...) had already run on them: in the 30 days to
2026-11-17 that was ~117 items and 840k+ input tokens spent on audio that was
then thrown away. The gate stops them at the door instead, for every intake
source, with deterministic rules only (no model, no Jev):

==========  ==================================================================
stage       rule
==========  ==================================================================
pre-ASR     a Telnyx voicemail (``telnyx-vm-*`` name), or audio whose call row
            already says ``outcome = voicemail``. Not even transcribed.
post-ASR    ``prune.classify`` on the fresh transcript, which at this point has
            no title, summary or classification, so only its structural rules
            can fire: no spoken words (``empty``), a voicemail greeting opening
            a call (``voicemail``), a test utterance (``no-content``), fewer
            than ``prune.DEFAULT_MIN_WORDS`` words (``too-short``), a phone call
            with at most ``prune.CALL_NOISE_MAX_WORDS`` words (``call-noise``).
==========  ==================================================================

A rejected item is ``skipped`` with cause ``junk_<reason>``. Backup-first
holds: the audio was archived to S3 before either check, and it is moved (never
deleted) to ``skipped/junk/``; the ASR text stays out of the vault at
``var/logs/<item>/rejected.md``. Nothing is written to the vault, the
``transcripts`` table or the Postgres store, and no pass plan is minted, so no
pass ever runs. The nightly prune stays as the backstop for what only an LLM
title can reveal.

Two guards keep the gate from costing real recordings. A transcript that is
empty from a LONG recording (``EMPTY_LONG_AUDIO_S``) is not junk, it is an ASR
outage, so the adapter fails the item visibly instead. And the test-utterance
rule is anchored: only a note that OPENS with "this is a test" / "let's see if
it works" (``TEST_OPENER_RE``) and stays short is a test, so a real dictation
that merely contains "if it works then ..." survives.

``WAX_JUNK_GATE=0`` turns the gate off for new work in that process (both
stages). A copy of an already-rejected recording that is dropped into the inbox
again is still parked in ``skipped/junk/duplicates/``, because the ledger
already holds the answer for those bytes. To recover a false positive use
``wax retry <item>``: it moves the audio from ``skipped/junk/`` back to the
inbox and records a ``junk_override`` transition, after which neither stage
judges that item again.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, NamedTuple, Optional

from . import paths

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

CAUSE_PREFIX = "junk_"
OVERRIDE_CAUSE = "junk_override"     # the transition `wax retry` writes to release a rejected item
# An empty transcript from audio longer than this is an ASR outage, not a silent clip.
EMPTY_LONG_AUDIO_S = 120.0
# A test utterance opens the note and is short; "if it works then we ship" is a dictation.
TEST_OPENER_RE = re.compile(r"^\W*(?:this is a test|let'?s see if (?:it|this) works|testing[ ,]+(?:one|1|testing))\b",
                            re.IGNORECASE)
TEST_UTTERANCE_MAX_WORDS = 10
# CJK text has no spaces, so prune.speech_words sees one "word" per sentence.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class Rejection(NamedTuple):
    reason: str      # a prune reason: empty | voicemail | no-content | too-short | call-noise
    detail: str
    words: Optional[int] = None

    @property
    def cause(self) -> str:
        return CAUSE_PREFIX + self.reason.replace("-", "_")


def enabled() -> bool:
    return os.environ.get("WAX_JUNK_GATE", "1").strip().lower() not in ("0", "false", "no", "off")


def reject_dir() -> Path:
    return paths.SKIPPED / "junk"


def overridden(item_id: Optional[str]) -> bool:
    """Has the operator released this item from the gate (``wax retry`` on a rejected item)?"""
    if not item_id:
        return False
    try:
        from . import ledger
        return ledger.connect().execute(
            "SELECT 1 FROM transitions WHERE machine='item' AND subject=? AND cause_code=? LIMIT 1",
            (item_id, OVERRIDE_CAUSE)).fetchone() is not None
    except Exception as exc:  # noqa: BLE001 - the gate fails open
        log.debug("junk override lookup for %s failed: %s", item_id, exc)
        return False


def empty_is_an_outage(audio_duration_s: Any) -> bool:
    """An empty transcript from this much audio is not a silent clip: fail it loudly."""
    return isinstance(audio_duration_s, (int, float)) and audio_duration_s > EMPTY_LONG_AUDIO_S


def is_junk_cause(cause: Optional[str]) -> bool:
    return bool(cause) and str(cause).startswith(CAUSE_PREFIX)


def _call_facts(name: str) -> dict[str, Any]:
    """What the call ledger already knows about this file, locally (never a Telnyx GET).

    Fails open: a missing or provisional call row, or any error, is "nothing known".
    """
    try:
        from . import calls
        ident = calls.audio_identity(name)
        if ident is None:
            return {}
        facts: dict[str, Any] = {"call-source": ident.get("source") or "call"}
        try:
            row = calls.call_for_audio(name, fetch=False)
        except Exception:  # noqa: BLE001 - the gate fails open
            row = None
        if row and row.get("outcome"):
            facts["call-outcome"] = row["outcome"]
        return facts
    except Exception as exc:  # noqa: BLE001 - the gate fails open
        log.debug("call lookup for %s failed: %s", name, exc)
        return {}


def before_asr(audio: Path, item_id: Optional[str] = None) -> Optional[Rejection]:
    """Reject audio that is junk by structure alone, before Whisper runs."""
    if not enabled() or overridden(item_id):
        return None
    name = audio.name
    if name.startswith("telnyx-vm-"):
        return Rejection("voicemail", "telnyx voicemail recording")
    facts = _call_facts(name)
    if str(facts.get("call-outcome") or "").lower() == "voicemail":
        return Rejection("voicemail", "call-outcome: voicemail")
    return None


def _spoken_head(body: str) -> str:
    """The first spoken line of a transcript body, speaker label stripped."""
    from . import prune
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or set(s) <= {"-", "*", "_"} or prune._META_LINE_RE.match(s):
            continue
        s = prune._LABEL_RE.sub("", s).strip()
        if s:
            return s
    return ""


def after_asr(md: Path, audio: Path, item_id: Optional[str] = None) -> Optional[Rejection]:
    """Reject a fresh transcript that holds nothing worth a note, before it is published."""
    if not enabled() or overridden(item_id):
        return None
    from . import frontmatter, prune
    try:
        # Not frontmatter.read: it turns an unreadable file into ({}, ""), which would
        # classify as "empty" and throw a real transcript away.
        fm, body = frontmatter.split(md.read_text())
    except Exception as exc:  # noqa: BLE001 - unreadable is not junk; publish and let the gates downstream judge
        log.warning("junk gate could not read %s: %s", md, exc)
        return None
    # Judge structure only: whatever the transcriber put in the frontmatter, plus
    # what the call ledger knows. No title/summary/classification exists yet.
    facts = {k: v for k, v in (fm or {}).items()
             if k in ("source-audio", "call-outcome", "call-source", "call-session-id")}
    facts.update(_call_facts(audio.name))
    verdict = prune.classify(facts, body, audio.name)
    if verdict.reason in (None, "body-lost"):
        return None
    reason, detail, n = verdict.reason, verdict.detail, verdict.words
    if reason == "no-content" and detail.startswith("test utterance"):
        # prune's body rule matches "if it works then" / "count to ten" anywhere in
        # up to 20 words, fine with a title to corroborate it, too loose for a fresh
        # note. Keep it only for a note that opens like a test; otherwise judge the
        # remaining structural rules (too-short, call-noise) as if it had not fired.
        if n <= TEST_UTTERANCE_MAX_WORDS and TEST_OPENER_RE.search(_spoken_head(body)):
            return Rejection(reason, detail, n)
        if n < prune.DEFAULT_MIN_WORDS:
            reason, detail = "too-short", f"{n} spoken word{'s' if n != 1 else ''}"
        elif n <= prune.CALL_NOISE_MAX_WORDS and prune._is_call(facts, audio.name):
            reason, detail = "call-noise", f"phone call with {n} spoken words"
        else:
            return None
    if reason in ("too-short", "call-noise"):
        cjk = len(_CJK_RE.findall(body))
        if cjk and n + cjk // 2 >= prune.DEFAULT_MIN_WORDS:
            return None   # unspaced script: whitespace undercounts; the nightly prune is the backstop
    return Rejection(reason, detail, n)
