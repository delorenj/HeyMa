"""`heyma` subcommands. No arguments (or `--page`) still opens the GTK4 window."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import paths, registry

WINDOW = paths.ROOT / "components" / "wax" / "bin" / "heyma"
OWN = ("status", "tray", "setup", "help", "-h", "--help")


def open_window(argv: list[str]) -> int:
    """Hand over to the existing window launcher (single-instance GTK4)."""
    os.execv(str(WINDOW), [str(WINDOW), *argv])
    return 0  # unreachable; execv replaces the process


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heyma",
        description="HeyMa: Wax, Vinyl, Telephony, Phonograph and Voxxy behind one client. "
                    "With no arguments (or --page settings|telephony|logs) it opens the window.")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser(
        "status", help="one line per product (add --json for the registry); "
                       "exits 0 when every section is ok or off, 2 when any is warn or fail")
    status.add_argument("--json", action="store_true", help="the heyma.shell.status.v1 document")
    sub.add_parser("tray", help="run the one tray (heyma-tray.service does this)")
    setup = sub.add_parser(
        "setup", help="set up products on this host and record them in ~/.config/heyma/host.toml "
                      "(each step calls the product's own setup; exits 0 ready, 2 degraded, 3 failed)")
    setup.add_argument("products", nargs="*", help="which steps (default: all). Steps: voxxy")
    setup.add_argument("--url", help="voxxy: the Voxxy server (default: keep vox.delo.sh or the configured one)")
    setup.add_argument("--key-ref", help="voxxy: op:// reference to this host's API key "
                                         "(default: $HEYMA_VOXXY_KEY_REF, else keep)")
    speak = setup.add_mutually_exclusive_group()
    speak.add_argument("--speak-here", dest="speak_here", action="store_true", default=None,
                       help="voxxy: make this desk a speaker (Speak on this desk)")
    speak.add_argument("--no-speak-here", dest="speak_here", action="store_false",
                       help="voxxy: stop this desk being a speaker")
    setup.add_argument("--json", action="store_true", help="the heyma.setup.v1 document")
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
    if args.command == "setup":
        from . import setup as host_setup
        try:
            doc = host_setup.run(args.products, host_setup.Options(
                url=args.url, key_ref=args.key_ref, speak_here=args.speak_here))
        except ValueError as exc:
            print(f"heyma setup: {exc}", file=sys.stderr)
            return 64
        print(json.dumps(doc, indent=2) if args.json else host_setup.render_text(doc))
        return doc["exit"]
    if args.command == "tray":
        from . import tray_ui
        return tray_ui.main()
    return 2
