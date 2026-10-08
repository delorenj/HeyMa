import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from wax import component, pjregistry


def record(pid, name, description="", repo="", status="active", **extra):
    return {"project_id": pid, "slug": pid, "name": name, "description": description,
            "repo_path": repo or f"/home/u/code/{pid}", "status": status, "template": {},
            "source_artifacts": [], "agents": {"pm": {"role": "pm"}}, "created_at": "", "updated_at": "",
            "ticket_provider": {"type": "plane", "workspace": "33god", "identifier": pid[:4].upper(),
                                "board_id": f"board-{pid}", "state": "active"}, **extra}


REGISTRY = {
    "schema_version": 1,
    "notebook": {},
    "projects": {
        "bb": record("bb", "Bloodbank", "Event bus for the 33god development pipeline",
                     repo="/home/u/code/33GOD/bloodbank", status="planned"),
        "intelliforia": record("intelliforia", "IntelliForia", "EMR compliance guardian",
                               secrets={"materialize_env": "do-not-forward"}),
        "keepy-money": record("keepy-money", "keepy-money", repo="/home/u/code/KeepyMoney"),
        "tower-of-dumb-things": record("tower-of-dumb-things", "Tower Of Dumb Things",
                                       repo="/home/u/code/pile-of-dumb-things"),
        "old": record("old", "Old", status="archived"),
        "stale": record("stale", "Stale"),
        "Bad_ID": record("Bad_ID", "Bad"),
        "mismatch": record("other-id", "Mismatch"),
    },
    "__registry_status": {"stale": {"status": "missing"}},
}


class Server:
    """A loopback registry whose answers a test scripts per request."""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                server.requests.append(self.path)
                status, body, delay = server.responder(len(server.requests))
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client already timed out, which is the point

            def log_message(self, *_args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        return self

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def ok(body=None):
    return 200, json.dumps(REGISTRY if body is None else body).encode(), 0


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FetchRegistryTest(unittest.TestCase):
    def fetch(self, server, **kwargs):
        kwargs.setdefault("backoff", 0)
        return pjregistry.fetch_registry(server.url, **kwargs)

    def failure(self, responder, reason, *, requests, **kwargs):
        with Server(responder) as server:
            with self.assertRaises(pjregistry.RegistryError) as caught:
                self.fetch(server, **kwargs)
        self.assertEqual(caught.exception.reason_code, reason)
        self.assertEqual(len(server.requests), requests)
        return caught.exception

    def test_reads_the_snapshot_endpoint(self):
        with Server(lambda n: ok()) as server:
            data = self.fetch(server)
        self.assertEqual(server.requests, ["/v1/registry"])
        self.assertEqual(set(data["projects"]), set(REGISTRY["projects"]))

    def test_never_routes_loopback_through_an_environment_proxy(self):
        dead = f"http://127.0.0.1:{closed_port()}"
        with Server(lambda n: ok()) as server, \
                patch.dict(os.environ, {"http_proxy": dead, "HTTP_PROXY": dead, "no_proxy": "", "NO_PROXY": ""}):
            self.assertEqual(self.fetch(server)["schema_version"], 1)

    def test_server_errors_are_retried_then_unavailable(self):
        with Server(lambda n: (503, b'{"error":"db","code":"registry_unavailable"}', 0) if n == 1 else ok()) as server:
            self.assertEqual(self.fetch(server)["schema_version"], 1)
        self.assertEqual(len(server.requests), 2)
        self.failure(lambda n: (503, b"{}", 0), "registry_unavailable", requests=3)

    def test_client_errors_fail_fast(self):
        error = self.failure(lambda n: (404, b'{"error":"Unknown registry endpoint","code":"not_found"}', 0),
                             "registry_unavailable", requests=1)
        self.assertIn("HTTP 404", str(error))
        self.assertIn("(not_found)", str(error))
        self.assertNotIn("Unknown registry endpoint", str(error))

    def test_refused_connection_is_unavailable(self):
        started = time.monotonic()
        with self.assertRaises(pjregistry.RegistryError) as caught:
            pjregistry.fetch_registry(f"http://127.0.0.1:{closed_port()}", attempts=2, backoff=0)
        self.assertEqual(caught.exception.reason_code, "registry_unavailable")
        self.assertLess(time.monotonic() - started, 5)

    def test_slow_registry_times_out_as_unavailable(self):
        self.failure(lambda n: (200, b"{}", 0.5), "registry_unavailable", requests=1, attempts=1, timeout=0.1)

    def test_bad_json_and_bad_shape_are_invalid(self):
        self.failure(lambda n: (200, b"<html>", 0), "registry_invalid", requests=1)
        self.failure(lambda n: ok({**REGISTRY, "schema_version": 2}), "registry_invalid", requests=1)
        self.failure(lambda n: ok({"schema_version": 1, "projects": []}), "registry_invalid", requests=1)

    def test_location_follows_pjangler_precedence(self):
        with patch.dict(os.environ, {"PJ_PROJECT_REGISTRY": "", "PJ_REGISTRY_URL": ""}):
            self.assertEqual(pjregistry.registry_url(), "http://127.0.0.1:8764")
        with patch.dict(os.environ, {"PJ_PROJECT_REGISTRY": "http://a:1/", "PJ_REGISTRY_URL": "http://b:2"}):
            self.assertEqual(pjregistry.registry_url(), "http://a:1")
        with patch.dict(os.environ, {"PJ_PROJECT_REGISTRY": "", "PJ_REGISTRY_URL": "http://b:2"}):
            self.assertEqual(pjregistry.registry_url(), "http://b:2")
        with patch.dict(os.environ, {"PJ_PROJECT_REGISTRY": "/tmp/fixture.yaml"}), \
                self.assertRaises(pjregistry.RegistryError) as caught:
            pjregistry.fetch_registry()
        self.assertEqual(caught.exception.reason_code, "registry_misconfigured")


class CandidatesTest(unittest.TestCase):
    def test_compact_sorted_projection_of_live_projects(self):
        candidates = pjregistry.candidates(REGISTRY)
        self.assertEqual([c["project_id"] for c in candidates],
                         ["bb", "intelliforia", "keepy-money", "tower-of-dumb-things"])
        bb = candidates[0]
        self.assertEqual(set(bb), {"project_id", "name", "description", "repo_path", "status", "provider",
                                   "workspace", "identifier", "board_id", "board_state"})
        self.assertEqual((bb["status"], bb["provider"], bb["board_id"]), ("planned", "plane", "board-bb"))
        self.assertNotIn("do-not-forward", json.dumps(candidates))
        archived = pjregistry.candidates(REGISTRY, include_archived=True)
        self.assertIn("old", [c["project_id"] for c in archived])


class AliasesTest(unittest.TestCase):
    def write(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "aliases.yaml"
        path.write_text(text)
        return path

    def test_valid_file_missing_file_and_env_override(self):
        path = self.write("projects:\n  bb:\n    aliases: [blood bank]\n  vinyl:\n    about: '  Live   dictation. '\n")
        self.assertEqual(pjregistry.load_aliases(path), {
            "bb": {"aliases": ["blood bank"], "about": ""},
            "vinyl": {"aliases": [], "about": "Live dictation."},
        })
        self.assertIsNone(pjregistry.load_aliases(path.with_name("absent.yaml")))
        with patch.dict(os.environ, {"WAX_PROJECT_ALIASES": str(path)}):
            self.assertIn("bb", pjregistry.load_aliases())

    def test_malformed_files_are_configuration_errors(self):
        for text in ("projects: [", "projects: []\n", "projects:\n  Bad_ID: {}\n",
                     "projects:\n  bb:\n    alias: [x]\n", "projects:\n  bb:\n    aliases: blood bank\n",
                     "projects:\n  bb:\n    aliases: ['']\n"):
            with self.subTest(text=text), self.assertRaises(pjregistry.RegistryError) as caught:
                pjregistry.load_aliases(self.write(text))
            self.assertEqual(caught.exception.reason_code, "aliases_invalid")

    def test_shipped_hint_file_is_valid_and_specific(self):
        hints = pjregistry.load_aliases(component.CONFIG / "project-aliases.yaml")
        self.assertIn("transcription-queue", hints)
        self.assertIn("client-portal", hints)
        generic = {"infra", "docker", "the vault", "vault", "gateway", "app", "website", "pipeline"}
        for pid, hint in hints.items():
            for alias in hint["aliases"]:
                self.assertNotIn(alias.casefold(), generic, f"{pid}: {alias!r} is too generic")

    def test_spoken_forms_survive_and_repo_names_only_when_distinct(self):
        hints = {"bb": {"aliases": ["blood bank", "BLOODBANK"], "about": ""},
                 "tower-of-dumb-things": {"aliases": ["Tower of Lost Things"], "about": "A junk tower game."}}
        by_id = {c["project_id"]: c for c in pjregistry.candidates(REGISTRY)}
        bb = pjregistry.hinted(by_id["bb"], hints)
        self.assertEqual(bb["aliases"], ["blood bank"])
        self.assertEqual(bb["about"], "Event bus for the 33god development pipeline.")
        tower = pjregistry.hinted(by_id["tower-of-dumb-things"], hints)
        self.assertEqual(tower["aliases"], ["Tower of Lost Things", "pile-of-dumb-things"])
        self.assertEqual(tower["about"], "A junk tower game.")
        self.assertEqual(pjregistry.hinted(by_id["keepy-money"], None)["aliases"], [])


if __name__ == "__main__":
    unittest.main()
