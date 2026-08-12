#!/usr/bin/env python3
"""Scaffold a side-effect-free document enrichment pass and YAML registry entry."""

from __future__ import annotations

import argparse
import re
import stat
import sys
from pathlib import Path


SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


PASS_TEMPLATE = '''#!/usr/bin/env python3
"""{description}

This executable is intentionally side-effect free. The host runner owns all
metadata writes, renames, state changes, and external links.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


RESULT_VERSION = 1


class EnrichmentError(RuntimeError):
    pass


def document_body(text: str) -> str:
    """Remove a leading Markdown frontmatter block without parsing its values."""
    if not text.startswith("---"):
        return text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[index + 1 :])
    return text


def enrich_document(body: str, *, document: Path, item_id: str = "") -> dict[str, Any]:
    """Return content-derived frontmatter proposals for any text document."""
    if not body.strip():
        raise EnrichmentError(f"document has no body: {{document}}")
    # Implement this pass's grounded enrichment here. Do not mutate `document`
    # or call host services from this function.
    return {{}}


def build_result(document: Path, *, item_id: str = "") -> dict[str, Any]:
    try:
        text = document.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise EnrichmentError(f"document is not UTF-8 text: {{document}}") from exc
    updates = enrich_document(document_body(text), document=document, item_id=item_id)
    if not isinstance(updates, dict):
        raise EnrichmentError("enrich_document must return an object")
    return {{"wax_ep_version": RESULT_VERSION, "frontmatter": updates}}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    parser.add_argument("item_id", nargs="?", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    document = args.document.expanduser().resolve()
    if not document.is_file():
        print(f"not a document file: {{document}}", file=sys.stderr)
        return 2
    try:
        result = build_result(document, item_id=args.item_id)
    except (OSError, EnrichmentError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def registry_template(slug: str, description: str, host: str) -> str:
    if host == "wax":
        executable = f"{{component_root}}/config/passes.d/bin/{slug}"
        document = "{md_path}"
    else:
        executable = f"bin/{slug}"
        document = "{document_path}"
    return f'''slug: {slug}
version: 1
description: "{description.replace(chr(34), chr(39))}"
enabled: false
auto: false
requires: []
clobber: []
timeout_s: 300
command: ["{executable}", "{document}", "{{item_id}}"]
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="lowercase kebab-case pass identifier")
    parser.add_argument("--registry-dir", required=True, type=Path)
    parser.add_argument(
        "--host", choices=("generic", "wax"), default="generic",
        help="select registry placeholders; the executable remains document-neutral",
    )
    parser.add_argument(
        "--description", default="Derive grounded metadata from any text document",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not SLUG_RE.fullmatch(args.slug):
        print("slug must be lowercase kebab-case", file=sys.stderr)
        return 2

    registry_dir = args.registry_dir.expanduser().resolve()
    manifest = registry_dir / f"{args.slug}.yaml"
    executable = registry_dir / "bin" / args.slug
    collisions = [path for path in (manifest, executable) if path.exists()]
    if collisions:
        print("refusing to overwrite: " + ", ".join(map(str, collisions)), file=sys.stderr)
        return 1

    executable.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        registry_template(args.slug, args.description, args.host), encoding="utf-8",
    )
    executable.write_text(
        PASS_TEMPLATE.format(description=args.description), encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(manifest)
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
