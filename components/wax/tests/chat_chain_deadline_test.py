"""The chat-pass model chain against real sockets: truncated bodies, a trickle
that no socket timeout can see, one deadline across the chain, what the
gateway says served, and an Anthropic server refused.

2026-10-22: every chain member answered 503 on the live gateway, and two
gaps showed up behind that outage. http.client.HTTPException is not an
OSError, so a body cut short (IncompleteRead) killed title-slug and
classification with a traceback whose first stderr line was no
`reason_code=`. And a per-socket timeout is not a deadline: the runner's
`timeout_s` could cut a long chain off before it reported any member.

Every server here is a local http.server on 127.0.0.1; nothing reaches the
gateway.
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from wax import frontmatter, ledger, passes, provider

COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
PASSES_DIR = COMPONENT_ROOT / "config" / "passes.d"
NOTE = "---\nlanguage: en\n---\n# Transcript\nDiscussing a modular enrichment pass for audio.\n"
GOOD_CONTENT = {"title": "Modular Enrichment Pass", "summary": "A pass for audio."}

# What a fake gateway does for each requested model.
SHORT, GARBLED, TRICKLE, NO_CHANNEL = "automaticai/x/short", "automaticai/x/garbled", "automaticai/x/trickle", \
    "automaticai/x/no-channel"
GLM = "automaticai/personal/glm-5.3-flash"
SNEAKY = "automaticai/x/sneaky"  # a router name that lands on an Anthropic model


def load_script(name: str):
    loader = SourceFileLoader(f"deadline_{name.replace('-', '_')}", str(PASSES_DIR / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _Gateway(BaseHTTPRequestHandler):
    """POST /v1/chat/completions, scripted by the requested model."""

    content = GOOD_CONTENT

    def log_message(self, *_args):
        pass

    def do_GET(self):  # /v1/models: an empty catalog, so nothing is pre-skipped
        self._send(200, {"data": []})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        model = json.loads(self.rfile.read(length) or b"{}").get("model", "")
        if model == SHORT:
            # Promise 4,000 bytes, send 20, hang up: http.client.IncompleteRead.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "4000")
            self.end_headers()
            self.wfile.write(b'{"choices":[{"messa')
            self.wfile.flush()
            self.close_connection = True
        elif model == GARBLED:
            self.wfile.write(b"NOT-HTTP garbage\r\n\r\n")
            self.wfile.flush()
            self.close_connection = True
        elif model == TRICKLE:
            # A byte every 0.2s: no single recv ever waits long enough for a
            # socket timeout to fire, so only a wall-clock deadline ends it.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "400")
            self.end_headers()
            try:
                for _ in range(400):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                pass
        elif model == NO_CHANNEL:
            self._send(503, {"error": {"message": "No available channel for model automaticai/hypercharm/glm-5.3",
                                       "code": "model_not_found"}},
                       {"X-AutomaticAI-Route": "automaticai/hypercharm/glm-5.3"})
        elif model == SNEAKY:
            self._send(200, self._answer("claude-haiku-4.5"),
                       {"X-AutomaticAI-Route": "automaticai/damien/claude-haiku-4.5"})
        else:
            self._send(200, self._answer(model.rsplit("/", 1)[-1]),
                       {"X-AutomaticAI-Route": model,
                        "X-AutomaticAI-Auto": "role=fast; rung=1; decision=test"})

    def _answer(self, served: str) -> dict:
        return {"model": served, "choices": [{"message": {"content": json.dumps(self.content)}}]}

    def _send(self, status: int, body: dict, headers: dict = None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)


class GatewayCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.title = load_script("title-slug")
        self.title._NOTES.clear()
        provider._NOTES.clear()
        self.addCleanup(self.title._NOTES.clear)
        self.addCleanup(provider._NOTES.clear)

    def run_pass(self, name: str, primary: str, fallbacks: str, *, timeout_s: str = "",
                 request_timeout: str = "10"):
        """The pass executable, end to end, against the fake gateway."""
        with tempfile.TemporaryDirectory() as directory:
            md = Path(directory) / "recording.md"
            md.write_text(NOTE)
            env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
            env.update({"WAX_TITLE_API_BASE": self.base, "WAX_TITLE_API_KEY": "test-key",
                        "WAX_TITLE_MODEL": primary, "WAX_TITLE_MODEL_FALLBACKS": fallbacks,
                        "WAX_TITLE_REQUEST_TIMEOUT_S": request_timeout})
            if timeout_s:
                env["WAX_PASS_TIMEOUT_S"] = timeout_s
            started = time.monotonic()
            done = subprocess.run([str(PASSES_DIR / "bin" / name), str(md), "item"], capture_output=True,
                                  text=True, env=env, timeout=120)
            took = time.monotonic() - started
            self.assertEqual(md.read_text(), NOTE, "a pass never edits its note")
        return done, took


class TruncatedResponseTest(GatewayCase):
    def test_title_slug_maps_a_short_body_to_a_reason_code(self):
        with self.assertRaises(self.title.TitlePassError) as caught:
            self.title._api_request(self.base + "/chat/completions", key="k", model=SHORT, timeout=10,
                                    payload={"model": SHORT})
        self.assertEqual(caught.exception.reason_code, "provider_bad_response")
        self.assertEqual(caught.exception.detail_code, "truncated_response")
        self.assertTrue(provider.falls_through(caught.exception))

    def test_title_slug_maps_a_garbled_status_line_to_a_reason_code(self):
        with self.assertRaises(self.title.TitlePassError) as caught:
            self.title._api_request(self.base + "/chat/completions", key="k", model=GARBLED, timeout=10,
                                    payload={"model": GARBLED})
        self.assertIn(caught.exception.reason_code, ("provider_bad_response", "provider_unreachable"))
        self.assertTrue(provider.falls_through(caught.exception))

    def test_the_shared_provider_maps_a_short_body_too(self):
        with self.assertRaises(provider.ProviderError) as caught:
            provider.post_json(self.base + "/chat/completions", {"model": SHORT}, key="k", timeout=10)
        self.assertEqual((caught.exception.reason_code, caught.exception.detail_code),
                         ("provider_bad_response", "truncated_response"))

    def test_title_slug_falls_through_a_short_body_and_the_glm_route_serves(self):
        done, _ = self.run_pass("title-slug", SHORT, GLM)
        self.assertEqual(done.returncode, 0, done.stderr)
        evidence = json.loads(done.stdout.strip().splitlines()[-1])["evidence"]
        self.assertEqual(evidence["model"], GLM)
        self.assertEqual(evidence["served_by"], "glm-5.3-flash")
        self.assertEqual(evidence["route"], GLM)
        self.assertEqual(evidence["auto"], "role=fast; rung=1; decision=test")
        self.assertEqual(evidence["fallbacks"], [{"model": SHORT, "reason_code": "provider_bad_response"}])

    # classification left the chat chain for Jev in v2 (2026-10-29): its provider failures are
    # covered by classification_jev_test.py, not here.

    def test_a_chain_of_short_bodies_fails_with_reason_code_first(self):
        for name in ("title-slug",):
            with self.subTest(name):
                done, _ = self.run_pass(name, SHORT, GARBLED)
                self.assertEqual(done.returncode, 1)
                lines = done.stderr.splitlines()
                self.assertEqual(lines[0], "reason_code=provider_bad_response", done.stderr)
                self.assertNotIn("Traceback", done.stderr)


class DeadlineTest(GatewayCase):
    def test_a_trickle_is_cut_off_by_the_deadline_and_the_next_member_serves(self):
        # The request timeout (2s) is a per-recv socket timeout and never fires
        # on a byte every 0.2s; the 400-byte body would take 80s. The wall-clock
        # limit ends it at 2.5s and the GLM member serves inside the 10s budget
        # (WAX_PASS_TIMEOUT_S=20 less the 10s reserve).
        done, took = self.run_pass("title-slug", TRICKLE, GLM, timeout_s="20", request_timeout="2")
        self.assertEqual(done.returncode, 0, done.stderr)
        evidence = json.loads(done.stdout.strip().splitlines()[-1])["evidence"]
        self.assertEqual(evidence["fallbacks"][0]["model"], TRICKLE)
        self.assertEqual(evidence["fallbacks"][0]["reason_code"], "timeout")
        self.assertEqual(evidence["model"], GLM)
        self.assertLess(took, 8, "the deadline, not the trickle, ended the request")

    def test_an_exhausted_budget_skips_members_and_still_reports_every_one(self):
        # 14s - 10s reserve = 4s: the trickle eats it (its timeout is capped at
        # what is left), then the GLM member has less than MIN_REQUEST_S left
        # and is skipped, not started and killed.
        done, took = self.run_pass("title-slug", TRICKLE, GLM, timeout_s="14")
        self.assertEqual(done.returncode, 1)
        lines = done.stderr.splitlines()
        self.assertEqual(lines[0], "reason_code=timeout", done.stderr)
        self.assertIn(f"{TRICKLE}=timeout", lines[1])
        self.assertIn(f"{GLM}=deadline_exceeded", lines[1])
        self.assertLess(took, 14 - 5, "the pass reports well before the runner would kill it")

    def test_request_timeouts_shrink_to_what_is_left(self):
        now = [100.0]
        deadline = provider.Deadline(30, clock=lambda: now[0])
        self.assertEqual(deadline.request_timeout(60), 30 - provider.REQUEST_MARGIN_S)
        now[0] += 25
        self.assertEqual(deadline.request_timeout(60), 5 - provider.REQUEST_MARGIN_S)
        self.assertEqual(deadline.request_timeout(2), 2)
        self.assertEqual(deadline.share(2).budget, 2.5)
        seen = []

        def call(model, timeout):
            seen.append((model, timeout))
            now[0] += 3
            raise provider.ProviderError("503", "provider_http_error", status=503)

        with self.assertRaises(provider.ChainFailed) as caught:
            provider.run_chain(["a", "b", "c"], call, deadline=deadline, request_timeout=60)
        self.assertEqual(seen, [("a", 4.0)], "b had 2s left minus the margin: skipped")
        self.assertEqual([a["reason_code"] for a in caught.exception.attempts],
                         ["provider_http_error", "deadline_exceeded", "deadline_exceeded"])
        self.assertEqual(caught.exception.reason_code, "provider_http_error")

    def test_the_runner_timeout_sets_the_budget(self):
        with patch.dict(os.environ, {"WAX_PASS_TIMEOUT_S": "390"}):
            self.assertAlmostEqual(provider.pass_deadline(60).budget, 390 - provider.RESERVE_S)
        with patch.dict(os.environ, {"WAX_PASS_TIMEOUT_S": "bogus"}):
            self.assertAlmostEqual(provider.pass_deadline(60).budget, 60 - provider.RESERVE_S)
        env = {k: v for k, v in os.environ.items() if k != "WAX_PASS_TIMEOUT_S"}
        with patch.dict(os.environ, env, clear=True):
            self.assertAlmostEqual(provider.pass_deadline(390).budget, 390 - provider.RESERVE_S)


class ReasonCodeTest(unittest.TestCase):
    """Item 4: a total failure is named by the first member actually asked."""

    def call(self, failures):
        def call(model, timeout):
            raise failures[model]
        return call

    def test_a_skipped_primary_never_names_the_failure(self):
        failures = {"b": provider.ProviderError("503", "provider_http_error", status=503),
                    "c": provider.ProviderError("slow", "timeout")}
        with self.assertRaises(provider.ChainFailed) as caught:
            provider.run_chain(["a", "b", "c"], self.call(failures), listed=lambda m: m != "a")
        self.assertEqual((caught.exception.reason_code, caught.exception.status), ("provider_http_error", 503))
        self.assertEqual(caught.exception.attempts[0], {"model": "a", "reason_code": "missing_model",
                                                        "skipped": True, "detail": "not listed for this token"})

    def test_missing_model_only_when_every_member_was_skipped(self):
        with self.assertRaises(provider.ChainFailed) as caught:
            provider.run_chain(["a", "b"], self.call({}), listed=lambda m: False)
        self.assertEqual(caught.exception.reason_code, "missing_model")

    def test_the_failed_routes_are_in_the_evidence(self):
        exc = provider.ProviderError("503", "provider_http_error", status=503, route="automaticai/hypercharm/glm-5.3")
        with self.assertRaises(provider.ChainFailed) as caught:
            provider.run_chain(["automaticai/auto/fast"], self.call({"automaticai/auto/fast": exc}))
        self.assertEqual(caught.exception.attempts[0]["route"], "automaticai/hypercharm/glm-5.3")
        self.assertIn("automaticai/auto/fast=provider_http_error/503->automaticai/hypercharm/glm-5.3",
                      str(caught.exception))


class AnthropicServedTest(GatewayCase):
    """Item 3: an Anthropic server fails the pass closed, whoever routed it there."""

    def test_an_anthropic_served_by_fails_closed_without_falling_through(self):
        done, _ = self.run_pass("title-slug", SNEAKY, GLM)
        self.assertEqual(done.returncode, 1)
        lines = done.stderr.splitlines()
        self.assertEqual(lines[0], "reason_code=anthropic_model_served", done.stderr)
        self.assertNotIn(f"{GLM}=", done.stderr, "the next member must not hide the regression")

    def test_the_shared_chat_json_refuses_it(self):
        with self.assertRaises(provider.ProviderError) as caught:
            provider.chat_json(base=self.base, model=SNEAKY, key="k", system="s", user="u",
                               schema={}, schema_name="x", max_tokens=8, timeout=10)
        self.assertEqual(caught.exception.reason_code, provider.ANTHROPIC_SERVED)
        self.assertFalse(provider.falls_through(caught.exception))

    def test_run_chain_refuses_an_anthropic_answer_and_an_anthropic_member(self):
        answer = provider.Answer("t", {"served_by": "claude-sonnet-5.5"})
        with self.assertRaises(provider.ChainFailed) as caught:
            provider.run_chain(["a", "b"], lambda model, timeout: answer)
        self.assertEqual(caught.exception.reason_code, provider.ANTHROPIC_SERVED)
        asked = []
        provider.run_chain(["automaticai/damien/claude-haiku-4.5", "b"],
                           lambda model, timeout: asked.append(model) or "ok")
        self.assertEqual(asked, ["b"], "an Anthropic chain member is never asked")

    def test_provenance_reads_the_gateway_headers(self):
        self.assertEqual(provider.provenance_of(
            {"model": "glm-5.3-flash"},
            {"x-automaticai-route": "automaticai/personal/glm-5.3-flash", "X-AutomaticAI-Auto": "role=fast"}),
            {"served_by": "glm-5.3-flash", "route": "automaticai/personal/glm-5.3-flash", "auto": "role=fast"})
        self.assertEqual(provider.provenance_of({"model": "kimi"}, {"X-AutomaticAI-Free": "pool=nvidia-free"}),
                         {"served_by": "kimi", "auto": "pool=nvidia-free"})
        self.assertEqual(provider.provenance_of({}, None), {})


class TicketableProvenanceTest(GatewayCase):
    def test_ticketable_records_served_by_and_falls_through_a_short_body(self):
        ticketable = load_script("ticketable")
        _Gateway.content = {"tickets": [{"project_id": "bb", "description": "Add the schema"}]}
        self.addCleanup(setattr, _Gateway, "content", GOOD_CONTENT)
        env = {"WAX_TICKETS_API_BASE": self.base, "WAX_TICKETS_API_KEY": "k",
               "WAX_TICKETS_MODEL": SHORT, "WAX_TICKETS_MODEL_FALLBACKS": GLM}
        with patch.dict(os.environ, env):
            raw, _, _, evidence = ticketable.extract("Please add the schema.", fm={}, ids=["bb"], projects=[])
        self.assertEqual(raw, [{"project_id": "bb", "description": "Add the schema"}])
        self.assertEqual((evidence["model"], evidence["served_by"]), (GLM, "glm-5.3-flash"))
        self.assertEqual(evidence["fallbacks"][0]["reason_code"], "provider_bad_response")


from passes_chain_test import ChainCase  # noqa: E402  (a fixture base; it has no tests of its own)


class RunnerTest(ChainCase):
    """The single owner: tells a pass its timeout, refuses an Anthropic-served result."""

    def test_the_runner_tells_the_pass_the_timeout_it_enforces(self):
        script = self.registry_dir / "budget.sh"
        script.write_text("#!/bin/sh\nprintf '{\"wax_ep_version\":1,\"evidence\":{\"budget\":\"%s\"}}\\n' "
                          "\"$WAX_PASS_TIMEOUT_S\"\n")
        script.chmod(0o755)
        self.write_yaml("budget", json.dumps({
            "slug": "budget", "version": 1, "enabled": True, "auto": True, "timeout_s": 45,
            # A YAML can never misstate it.
            "env": {"WAX_PASS_TIMEOUT_S": "9999"}, "command": [str(script), "{md_path}"]}))
        self.assertEqual(passes.run(self.item, "budget")["state"], "completed")
        self.assertEqual(json.loads(self.pass_row("budget")["result"])["evidence"], {"budget": "45"})

    def test_an_anthropic_served_result_is_refused_and_not_applied(self):
        self.add_pass("sneaky", {"frontmatter": {"summary": "Written by the wrong model."}, "evidence": {
            "model": "automaticai/auto/fast", "served_by": "claude-haiku-4.5",
            "chain": ["automaticai/auto/fast"], "fallbacks": []}})
        outcome = passes.run(self.item, "sneaky")
        self.assertEqual((outcome["state"], outcome["reason_code"]), ("failed", provider.ANTHROPIC_SERVED))
        fm, _ = frontmatter.read(self.md)
        self.assertNotIn("summary", fm)
        self.assertEqual(fm["wax"]["passes"]["sneaky"]["reason_code"], provider.ANTHROPIC_SERVED)

    def test_served_by_lands_in_the_note(self):
        self.add_pass("titled", {"frontmatter": {"summary": "A grounded summary."}, "evidence": {
            "model": "automaticai/auto/fast", "served_by": "glm-5.3-flash",
            "route": "automaticai/personal/glm-5.3-flash", "chain": ["automaticai/auto/fast"], "fallbacks": []}})
        self.assertEqual(passes.run(self.item, "titled")["state"], "completed")
        entry = frontmatter.read(self.md)[0]["wax"]["passes"]["titled"]
        self.assertEqual((entry["model"], entry["served_by"], entry["route"]),
                         ("automaticai/auto/fast", "glm-5.3-flash", "automaticai/personal/glm-5.3-flash"))



class ReplanTest(ChainCase):
    """An item minted under a dead chain recovers only by an explicit replan."""

    def stuck_pass(self, chain: str):
        # Like a chat pass: live code, the chain from the (frozen) env.
        script = self.registry_dir / "stuck.sh"
        script.write_text('#!/bin/sh\nif [ "$WAX_TITLE_MODEL_FALLBACKS" = old ]; then\n'
                          '  echo reason_code=provider_http_error >&2; echo "every member 503" >&2; exit 1\nfi\n'
                          'echo \'{"wax_ep_version":1}\'\n')
        script.chmod(0o755)
        self.write_yaml("stuck", json.dumps({"slug": "stuck", "version": 1, "enabled": True, "auto": True,
                                             "env": {"WAX_TITLE_MODEL_FALLBACKS": chain},
                                             "command": [str(script), "{md_path}"]}))

    def mint_with_a_stuck_pass(self):
        self.add_pass("done", {"frontmatter": {"summary": "A grounded summary."}})
        self.stuck_pass("old")
        passes.ensure_plan(self.item)
        self.assertEqual(passes.run(self.item, "done")["state"], "completed")
        self.assertEqual(passes.run(self.item, "stuck")["state"], "failed")
        # The registry is fixed; the item's frozen plan still runs the old env.
        self.stuck_pass("new")
        self.assertEqual(passes.run(self.item, "stuck")["state"], "failed", "a plan never migrates by itself")

    def test_replan_moves_only_the_unfinished_pass_and_then_it_recovers(self):
        self.mint_with_a_stuck_pass()
        dry = passes.replan(self.item)
        self.assertFalse(dry["applied"])
        self.assertEqual(dry["changed"], {"stuck": {"fields": ["env"], "env": ["WAX_TITLE_MODEL_FALLBACKS"]}})
        self.assertEqual(passes.run(self.item, "stuck")["state"], "failed", "a dry run changes nothing")
        self.assertTrue(passes.replan(self.item, apply=True)["applied"])
        outcome = passes.run(self.item, "stuck")
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(self.pass_row("done")["attempt"], 1, "finished passes are untouched")
        audit = ledger.connect().execute(
            "SELECT * FROM transitions WHERE machine='plan' AND subject=?", (self.item,)).fetchall()
        self.assertEqual([row["cause_code"] for row in audit], ["operator_replan"])

    def test_replan_refuses_to_move_a_finished_pass(self):
        self.mint_with_a_stuck_pass()
        self.add_pass("done", {"frontmatter": {"summary": "A different summary."}}, env={"X": "changed"})
        with self.assertRaises(passes.PassError) as caught:
            passes.replan(self.item, apply=True)
        self.assertIn("already finished", str(caught.exception))

    def test_replan_refuses_a_changed_set_of_passes(self):
        self.mint_with_a_stuck_pass()
        self.add_pass("extra", {})
        with self.assertRaises(passes.PassError):
            passes.replan(self.item, apply=True)


if __name__ == "__main__":
    unittest.main()
