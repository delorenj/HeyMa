"""Embedding gates (design D.5). Reads the Wax ledger only.

A transcript's chunks are embedded once its body has SETTLED: no
body-mutating pass in its frozen plan (`body_mutation` set in
`processing_plans.definitions`) is still non-terminal. Terminal means
completed or skipped, or failed with `exhausted_at` set or with
`attempt >= passes.DEFAULT_MAX_ATTEMPTS`. Embedding a body that diarization is
about to replace would throw the vectors away minutes later.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .. import ledger

DEFAULT_MAX_ATTEMPTS = 3   # passes.DEFAULT_MAX_ATTEMPTS; not imported to keep this module light


def _max_attempts() -> int:
    try:
        from .. import passes
        return int(passes.DEFAULT_MAX_ATTEMPTS)
    except Exception:  # noqa: BLE001
        return DEFAULT_MAX_ATTEMPTS


def body_mutating_passes(item_id: str) -> list[str]:
    row = ledger.connect().execute("SELECT definitions FROM processing_plans WHERE item_id=?",
                                   (item_id,)).fetchone()
    if not row:
        return []
    try:
        plan = json.loads(row["definitions"])
    except (TypeError, ValueError):
        return []
    if not isinstance(plan, dict):
        return []
    return sorted(slug for slug, ep in plan.items() if isinstance(ep, dict) and ep.get("body_mutation"))


def _terminal(row: Optional[Any], max_attempts: int) -> bool:
    if row is None:
        return False
    state = row["state"]
    if state in ("completed", "skipped"):
        return True
    if state == "failed":
        keys = row.keys()
        if "exhausted_at" in keys and row["exhausted_at"]:
            return True
        return int(row["attempt"] or 0) >= max_attempts
    return False


def body_settled(item_id: Optional[str]) -> bool:
    """True when nothing in the item's frozen plan can still replace its body."""
    if not item_id:
        return True      # Hermes / legacy notes have no plan; their body is what it is
    slugs = body_mutating_passes(item_id)
    if not slugs:
        return True
    conn = ledger.connect()
    max_attempts = _max_attempts()
    for slug in slugs:
        row = conn.execute("SELECT * FROM passes WHERE item_id=? AND ep_slug=?", (item_id, slug)).fetchone()
        if not _terminal(row, max_attempts):
            return False
    return True


def settled_map(item_ids: list[str]) -> dict[str, bool]:
    out = {}
    for item_id in item_ids:
        try:
            out[item_id] = body_settled(item_id)
        except Exception:  # noqa: BLE001 - an unreadable plan never blocks embedding forever
            out[item_id] = True
    return out
