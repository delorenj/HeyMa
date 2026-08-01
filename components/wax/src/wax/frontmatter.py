"""Read and merge YAML frontmatter without trampling anyone else's keys.

Wax owns exactly two keys: `wax-item-id` and the `wax:` block. Everything else
in a note's frontmatter belongs to somebody else and is preserved byte-for-byte
where possible.

In particular Wax does NOT own `pipeline-status`. The Obsidian note-status
plugin blanket-stamps `pipeline-status: new` on ~97% of the vault, so that key
carries no information about whether *this* pipeline touched a file — which is
why "a .md without a state property hasn't been processed" needs its own key
rather than reusing that one.

Writes go through a temp file + atomic rename. The vault has no git and an
active weekly cleanup job; a half-written note is not recoverable.
"""

import os
from pathlib import Path
from typing import Any, Optional

import yaml

DELIM = "---"
ITEM_KEY = "wax-item-id"
WAX_KEY = "wax"


def split(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter, body). Missing/!invalid frontmatter -> ({}, text)."""
    if not text.startswith(DELIM):
        return {}, text
    lines = text.split("\n")
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == DELIM:
            end = i
            break
    if end is None:
        return {}, text
    raw = "\n".join(lines[1:end])
    try:
        fm = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(fm, dict):
        return {}, text
    return fm, "\n".join(lines[end + 1:])


def render(fm: dict[str, Any], body: str) -> str:
    if not fm:
        return body
    dumped = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False).rstrip()
    return f"{DELIM}\n{dumped}\n{DELIM}\n{body.lstrip(chr(10))}"


def read(path: Path) -> tuple[dict[str, Any], str]:
    try:
        return split(path.read_text())
    except OSError:
        return {}, ""


def merge(path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge `updates` into the note's frontmatter, atomically."""
    fm, body = read(path)
    _deep_merge(fm, updates)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(render(fm, body))
    os.replace(tmp, path)
    return fm


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def is_unprocessed(path: Path) -> bool:
    """True if this pipeline has demonstrably never touched the note.

    Deliberately keyed on `wax-item-id`, not on any generic `state`/`status`
    field, because those are written by other tools for other reasons.
    """
    fm, _ = read(path)
    return not fm or not fm.get(ITEM_KEY)


def pass_states(path: Path) -> dict[str, Any]:
    fm, _ = read(path)
    return ((fm.get(WAX_KEY) or {}).get("passes") or {})
