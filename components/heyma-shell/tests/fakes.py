"""A Context whose CLIs, files and HTTP endpoint are all fakes."""

import json
from datetime import datetime, timezone
from pathlib import Path

from heyma_shell.adapters import Context, Run

NOW = 1_800_000_000.0


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def wax_snapshot(stream="ready", inbox="ready", age=1.0, **extra):
    snap = {"stream": {"state": stream}, "inbox": {"state": inbox, "pending": 0},
            "queue": {"failed": 0}, "passes": {}, "diarization": {}, "updated_at": iso(NOW - age)}
    snap.update(extra)
    return snap


class FakeWorld:
    """Records every call so tests can assert on the exact argv and timeout."""

    def __init__(self, root="/repo", home="/home/u", environ=None, present=(), cli=None,
                 files=None, http=None):
        self.calls = []
        self.root, self.home = Path(root), Path(home)
        self.present = {Path(p) for p in present}
        self.cli = cli or {}        # tuple(argv) -> Run
        self.files = files or {}    # str(path) -> object
        self.http = http or {}      # url -> (status, body)
        self.environ = dict(environ or {})

    def run(self, argv, timeout=10.0):
        self.calls.append((tuple(argv), timeout))
        return self.cli.get(tuple(argv), Run(127, "", f"not found: {argv[0]}"))

    def http_get(self, url, timeout=4.0):
        self.calls.append((url, timeout))
        return self.http.get(url, (0, ""))

    def read_json(self, path):
        return self.files.get(str(path))

    def exists(self, path):
        return Path(path) in self.present

    def context(self) -> Context:
        return Context(root=self.root, home=self.home, environ=self.environ, run=self.run,
                       http_get=self.http_get, read_json=self.read_json, exists=self.exists,
                       now=lambda: NOW)


def ok(payload) -> Run:
    return Run(0, json.dumps(payload), "")
