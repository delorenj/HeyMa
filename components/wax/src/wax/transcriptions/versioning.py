"""Version identity and the mint decision (design B.0, D.4 step 5). PURE.

A version is one immutable row per completed pass, per transcription, per
backfill and per detected external edit. Skipped and failed passes are pass
history (`pass_run`) only. These helpers fix
the idempotency keys and decide whether a snapshot mints a version.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

PRODUCER_KINDS = ("transcription", "pass", "backfill", "reconcile")
MINT, NO_CHANGE, SKIP = "mint", "no_change", "skip"


def transcription_key(item_id: str, attempt: int, note_sha256: str) -> str:
    return f"transcription:{item_id}:a{int(attempt)}:{note_sha256[:16]}"


def pass_key(command_id: str) -> str:
    return f"pass:{command_id}"


def skip_key(item_id: str, slug: str, pass_version: int, attempt: int) -> str:
    return f"skip:{item_id}:{slug}:v{int(pass_version)}:a{int(attempt)}"


def depfail_key(item_id: str, slug: str, pass_version: int, attempt: int) -> str:
    return f"depfail:{item_id}:{slug}:v{int(pass_version)}:a{int(attempt)}"


def backfill_key(note_sha256: str) -> str:
    return f"backfill:{note_sha256}"


def reconcile_key(content_sha256: str, head_version_no: Optional[int] = None) -> str:
    """`reconcile:{content_sha}`, scoped to the head it follows (see design G).

    An edit A -> B -> A must mint a third version; a key on content alone would
    collide with the first reconcile and silently leave the head at B.
    """
    if head_version_no is None:
        return f"reconcile:{content_sha256}"
    return f"reconcile:v{int(head_version_no)}:{content_sha256}"


def pass_snapshot_key(*, item_id: str, slug: str, state: str, pass_version: int, attempt: int,
                      command_id: Optional[str], reason_code: Optional[str]) -> str:
    """The key one pass outcome is recorded under, in SQLite and in `pass_run`."""
    if command_id:
        return pass_key(command_id)
    if state == "skipped":
        return skip_key(item_id, slug, pass_version, attempt)
    if reason_code == "dependency_failed":
        return depfail_key(item_id, slug, pass_version, attempt)
    return f"{state}:{item_id}:{slug}:v{int(pass_version)}:a{int(attempt)}"


def decide(head: Optional[Mapping[str, Any]], *, kind: str, produced_by: str,
           pass_version: Optional[int], content_sha256: str) -> str:
    """'mint' | 'no_change' | 'skip' for a snapshot against the current head.

    * no head -> mint
    * backfill with any existing head -> skip (history is never re-imported)
    * reconcile with the same content -> skip (stat refresh only)
    * same produced_by + pass_version + content -> no_change (a retry that changed nothing)
    * anything else -> mint (a different pass on identical content is still an applied pass)
    """
    if kind not in PRODUCER_KINDS:
        raise ValueError(f"unknown producer kind {kind!r}")
    if not head:
        return MINT
    if kind == "backfill":
        return SKIP
    same_content = head.get("content_sha256") == content_sha256
    if kind == "reconcile":
        return SKIP if same_content else MINT
    if (same_content and head.get("produced_by") == produced_by
            and _norm(head.get("pass_version")) == _norm(pass_version)):
        return NO_CHANGE
    return MINT


def _norm(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
