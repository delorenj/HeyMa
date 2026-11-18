"""`heyma` subcommands. No arguments (or `--page`) still opens the GTK4 window."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import paths, registry

WINDOW = paths.ROOT / "components" / "wax" / "bin" / "heyma"
OWN = ("status", "tray", "help", "-h", "--help")


def open_window(argv: list[str]) -> int:
    """Hand over to the existing window launcher (single-instance GTK4)."""
    os.execv(str(WINDOW), [str(WINDOW), *argv])
    return 0  # unreachable; execv replaces the process


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heyma",
        description="HeyMa: Wax, Vinyl, Telephony and Phonograph behind one client. "
                    "With no arguments (or --page settings|telephony|logs) it opens the window.")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser(
        "status", help="one line per product (add --json for the registry); "
                       "exits 0 when every section is ok or off, 2 when any is warn or fail")
    status.add_argument("--json", action="store_true", help="the heyma.shell.status.v1 document")
    sub.add_parser("tray", help="run the one tray (heyma-tray.service does this)")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in OWN:
        return open_window(argv)
    if argv[0] == "help":
        argv = ["--help"]
    args = build_parser().parse_args(argv)
    if args.command == "status":
        doc = registry.collect()
        print(json.dumps(doc, indent=2, sort_keys=True) if args.json else registry.render_text(doc))
        return registry.exit_code(doc)
    if args.command == "tray":
        from . import tray_ui
        return tray_ui.main()
    return 2
