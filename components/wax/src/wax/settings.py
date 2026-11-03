"""Operator settings: the small, durable knobs the HeyMa window turns.

Today that is one knob — which enrichment passes the operator has switched OFF.
It is an OVERLAY, not a registry edit: compiled pipelines freeze their pass
definitions, so flipping `enabled:` in passes.d would change nothing for any
audio or document plan already governed by `transcript-standard` or
`doc-standard`. The runner consults this file instead, at execution time, and
records a switched-off pass as `skipped` with reason_code `operator_disabled`
(see passes.run). A skip, not a failure: finalize completes, dependents are not
wedged behind it, and the note's `wax.passes.<slug>` block says why nothing ran.

Lives at `var/settings.json` (beside the ledger, outside git). Written atomically
under an flock so the window and a CLI call cannot lose each other's update.
Read on every pass run; it is tiny and the file system cache answers.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import paths, sentinel


def path() -> Path:
    # Resolved per call, never at import: tests (and a relocated WAX_ROOT)
    # patch paths.VAR after this module is loaded.
    return paths.VAR / "settings.json"


def load() -> dict[str, Any]:
    """The settings document, or an empty one. Unreadable is never fatal.

    A corrupt settings file must not stop the pipeline — the worst it may do is
    behave as if nothing were switched off, which is the shipped default.
    """
    try:
        doc = json.loads(path().read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


@contextmanager
def _locked() -> Iterator[None]:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target.with_suffix(".lock"), "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write(doc: dict[str, Any]) -> None:
    target = path()
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, target)


def disabled_passes() -> set[str]:
    """Slugs the operator switched off. Empty when unset or unreadable."""
    raw = (load().get("passes") or {}).get("disabled") or []
    return {str(slug) for slug in raw if isinstance(slug, str) and slug}


def pass_enabled(slug: str) -> bool:
    return slug not in disabled_passes()


def set_pass_enabled(slug: str, enabled: bool) -> dict[str, Any]:
    """Switch one pass on or off. Idempotent; returns the resulting state."""
    with _locked():
        doc = load()
        section = doc.get("passes") if isinstance(doc.get("passes"), dict) else {}
        disabled = {str(s) for s in section.get("disabled") or [] if isinstance(s, str)}
        changed = (slug in disabled) == bool(enabled)
        if enabled:
            disabled.discard(slug)
        else:
            disabled.add(slug)
        section["disabled"] = sorted(disabled)
        if changed:
            section["updated_at"] = sentinel.utcnow()
        doc["passes"] = section
        _write(doc)
    return {"slug": slug, "operator_enabled": bool(enabled), "changed": changed,
            "disabled": sorted(disabled)}
