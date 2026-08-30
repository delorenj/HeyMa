#!/usr/bin/env python3
"""Render Wax systemd user-unit templates for a fixed Debian install layout."""
from pathlib import Path
import re
import sys


PREFIX = Path("/opt/wax")
WAX_BIN = Path("/usr/bin/wax")
WAXD_BIN = Path("/usr/bin/waxd")
WAX_TRANSCRIBE = Path("/usr/bin/wax-transcribe")
DOCS = PREFIX / "components" / "wax" / "docs" / "WAX-DESIGN.md"


def systemd_quote(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("%", "%%").replace("$", "\\x24")
    return f'"{escaped}"'


def render(template: Path) -> str:
    text = template.read_text()
    text = text.replace(
        "@WAX_DOCUMENTATION_URI@",
        DOCS.as_uri().replace("%", "%%"),
    )
    text = text.replace("@WAX_EXEC_START@", systemd_quote(WAXD_BIN))
    text = text.replace(
        "@WAX_EXEC_QUIESCE@",
        f"{systemd_quote(WAX_BIN)} rec quiesce --json",
    )
    text = text.replace(
        "@WAX_TRANSCRIBE_ENV@",
        systemd_quote(f"WAX_TRANSCRIBE={WAX_TRANSCRIBE}"),
    )
    return text


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <template> <output>", file=sys.stderr)
        return 2
    template = Path(sys.argv[1])
    output = Path(sys.argv[2])
    output.write_text(render(template))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
