#!/usr/bin/python3
"""Configure an immutable, flat Syncthing audio feed. Never touches audio bytes.

Receive-only does not remember deletions across remote edits or Revert. Retire
each completed filename immediately, while its local copy remains available.
The durable ignore rules then prevent a second download under that name.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


SUFFIXES = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".aac",
            ".mov", ".mp4", ".mkv", ".webm", ".wma", ".aiff"}
BEGIN = "// BEGIN HeyMa audio feed policy"
END = "// END HeyMa audio feed policy"
FLAT_BEGIN = "// BEGIN HeyMa flat audio policy"
FLAT_END = "// END HeyMa flat audio policy"


def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".audio-policy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(value)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def literal(name):
    # This file runs on the Linux hub: Syncthing 1.x uses backslash escaping.
    if (not isinstance(name, str) or not name or name in (".", "..") or name.startswith(".")
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or "/" in name or "\\" in name or name != name.strip()):
        raise ValueError(f"Cannot safely retire filename {name!r}")
    return "/" + "".join("\\" + c if c in "\\*?[]{}" else c for c in name)


def patterns(retired, existing):
    custom = []
    managed = False
    for line in existing:
        if line in (BEGIN, FLAT_BEGIN):
            if managed:
                raise ValueError("Nested managed ignore block")
            managed = True
        elif line in (END, FLAT_END):
            if not managed:
                raise ValueError("Unmatched managed ignore block")
            managed = False
        elif not managed:
            custom.append(line)
    if managed:
        raise ValueError("Unclosed managed ignore block")
    return [BEGIN, "// Received filenames are permanent exclusions; keep local audio.",
            *[literal(name) for name in sorted(retired)], END,
            *custom, FLAT_BEGIN, "// Only root-level audio files may arrive.",
            *[f"(?i)!/*{suffix}" for suffix in sorted(SUFFIXES)], "*", FLAT_END]


class API:
    def __init__(self, config):
        gui = ET.parse(config).getroot().find("gui")
        if gui.get("tls") == "true":
            raise ValueError("Use the hub's loopback HTTP API")
        address = gui.findtext("address")
        _, port = address.rsplit(":", 1)
        # Credentials only travel to loopback, even if GUI listens on all hosts.
        self.base = f"http://127.0.0.1:{port}/rest/"
        self.key = gui.findtext("apikey")

    def call(self, endpoint, body=None):
        request = urllib.request.Request(
            self.base + endpoint,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-API-Key": self.key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read()
            return json.loads(payload) if payload else None


def reconcile(api, root, state, folder):
    config = api.call("config/folders/" + urllib.parse.quote(folder, safe=""))
    actual = Path(config["path"]).expanduser().resolve()
    if actual != root.resolve() or config["type"] != "receiveonly":
        raise ValueError(f"{folder} must be receiveonly at {root}; found {config['type']} at {actual}")
    if not (root / config["markerName"]).is_dir():
        raise ValueError("Syncthing folder marker is missing; refusing to seed an empty folder")
    retired = set(json.loads(state.read_text()) if state.exists() else [])
    # Syncthing's temporary downloads are hidden. Only final, closed files have
    # their public name; neither a size-settle heuristic nor an audio write.
    retired.update(p.name for p in root.iterdir()
                   if p.is_file() and not p.is_symlink() and not p.name.startswith(".")
                   and p.suffix.lower() in SUFFIXES)
    for name in retired:
        literal(name)
    state_value = json.dumps(sorted(retired), ensure_ascii=False, indent=2) + "\n"
    ignore_file = root / ".stignore"
    existing = ignore_file.read_text().splitlines() if ignore_file.exists() else []
    desired = patterns(retired, existing)
    endpoint = "db/ignores?" + urllib.parse.urlencode({"folder": folder})
    if desired != existing:
        api.call(endpoint, {"ignore": desired})
    acknowledged = api.call(endpoint)
    if not isinstance(acknowledged, dict) or acknowledged.get("ignore") != desired:
        raise RuntimeError("Syncthing did not acknowledge permanent filename exclusions")
    if not state.exists() or state.read_text() != state_value:
        atomic_write(state, state_value)
        print(f"{folder}: {len(retired)} received filenames permanently excluded", flush=True)
    return len(retired)


def main():
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home()/".local/state/syncthing/config.xml")
    parser.add_argument("--root", type=Path, default=repo/"dropoff")
    parser.add_argument("--state", type=Path, default=repo/"var/syncthing-audio/received.json")
    parser.add_argument("--folder", default="audio")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        api = API(args.config)
        while True:
            reconcile(api, args.root, args.state, args.folder)
            if args.once:
                return
            time.sleep(1)


if __name__ == "__main__":
    main()
