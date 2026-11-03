"""A restarting gateway is waited out, not fallen through.

2026-11-03 16:14 UTC: a gateway deploy restarted new-api, and the edge answered
every request with 503 {"error":{"type":"overloaded_error","code":
"gateway_restarting"}} and Retry-After: 15. title-slug asked all six chain
members in one second, every one through the same closed door, and failed.
The chain now waits the Retry-After on the SAME member, within its patience
and its deadline, and never sprays the remaining members at a dead gateway.
"""

import importlib.util
import io
import unittest
import urllib.error
from email.message import Message
from importlib.machinery import SourceFileLoader
from pathlib import Path

from wax import provider

COMPONENT_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
EDGE_BODY = (b'{"type":"error","error":{"type":"overloaded_error","code":"gateway_restarting",'
             b'"message":"The AutomaticAI gateway is restarting. Retry shortly."}}')


def restarting(retry_after=15.0):
    return provider.ProviderError("HTTP 503", "provider_http_error", status=503,
                                  detail_code=provider.GATEWAY_RESTARTING, retry_after=retry_after)


def http_error(status: int, body: bytes, headers: dict) -> urllib.error.HTTPError:
    message = Message()
    for name, value in headers.items():
        message[name] = value
    return urllib.error.HTTPError("https://gw/v1/chat/completions", status, "x", message, io.BytesIO(body))


class ClassificationTest(unittest.TestCase):
    def test_the_edge_503_is_named_gateway_restarting_with_its_retry_after(self):
        exc = provider._http_error(http_error(503, EDGE_BODY, {"Retry-After": "15"}),
                                   "https://gw/v1/chat/completions", "k")
        self.assertEqual((exc.reason_code, exc.status, exc.detail_code, exc.retry_after),
                         ("provider_http_error", 503, "gateway_restarting", 15.0))
        self.assertTrue(provider.gateway_unavailable(exc))
        self.assertNotIn("Retry shortly", str(exc), "the body never reaches the message")

    def test_a_member_503_is_not_gateway_wide(self):
        body = b'{"error":{"code":"model_not_found","message":"No available channel for model x"}}'
        exc = provider._http_error(http_error(503, body, {}), "https://gw/v1/chat/completions", "k")
        self.assertFalse(provider.gateway_unavailable(exc))
        self.assertIsNone(exc.retry_after)

    def test_retry_after_is_seconds_only_and_capped(self):
        self.assertEqual(provider.retry_after_s({"retry-after": "7"}), 7.0)
        self.assertEqual(provider.retry_after_s({"Retry-After": "99999"}), 300.0)
        self.assertIsNone(provider.retry_after_s({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
        self.assertIsNone(provider.retry_after_s({}))
        self.assertIsNone(provider.retry_after_s(None))

    def test_title_slug_classifies_the_edge_503_the_same_way(self):
        loader = SourceFileLoader("restart_title_slug", str(COMPONENT_ROOT / "config/passes.d/bin/title-slug"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        title = importlib.util.module_from_spec(spec)
        loader.exec_module(title)
        exc = title._http_failure(http_error(503, EDGE_BODY, {"Retry-After": "15"}),
                                  url="https://gw/v1/chat/completions", model="automaticai/auto/fast")
        self.assertEqual((exc.reason_code, exc.detail_code, exc.retry_after),
                         ("provider_http_error", "gateway_restarting", 15.0))


class ChainTest(unittest.TestCase):
    def setUp(self):
        provider._NOTES.clear()
        self.slept: list[float] = []

    def sleep(self, seconds):
        self.slept.append(seconds)

    def test_the_chain_waits_and_asks_the_same_member_again(self):
        asked = []

        def call(model, _timeout):
            asked.append(model)
            if len(asked) <= 2:
                raise restarting()
            return {"title": "ok"}

        result = provider.run_chain(["a", "b", "c"], call, sleep=self.sleep)
        self.assertEqual(asked, ["a", "a", "a"], "b and c were never asked at a closed door")
        self.assertEqual(self.slept, [15.0, 15.0])
        self.assertEqual((result.model, result.value, result.waited), ("a", {"title": "ok"}, 30.0))
        self.assertEqual(result.attempts, [], "a wait is not a fall-through: the member that served "
                                             "must not appear in its own fell_through")
        self.assertEqual(provider.evidence_for(["a", "b", "c"], result.model, result.attempts)["fallbacks"], [])

    def test_past_its_patience_the_chain_stops_without_asking_the_rest(self):
        asked = []

        def call(model, _timeout):
            asked.append(model)
            raise restarting()

        with self.assertRaises(provider.ChainFailed) as caught:
            provider.run_chain(["a", "b", "c"], call, sleep=self.sleep, gateway_patience=40)
        self.assertEqual(asked, ["a"] * 4)
        self.assertEqual(self.slept, [15.0, 15.0, 10.0])
        failure = caught.exception
        self.assertEqual((failure.reason_code, failure.status, failure.detail_code),
                         ("provider_http_error", 503, "gateway_restarting"))
        self.assertTrue(str(failure).startswith("the gateway was down (gateway_restarting); "
                                                "the chain waited 40s"), str(failure))
        skipped = [a for a in failure.attempts if a.get("skipped")]
        self.assertEqual([a["model"] for a in skipped], ["b", "c"])
        self.assertEqual(len([a for a in failure.attempts if a["model"] == "a"]), 1,
                         "one entry for the member, however many waits")
        self.assertEqual(failure.attempts[0]["waited"], 40)

    def test_the_wait_never_eats_the_deadline(self):
        now = [0.0]
        deadline = provider.Deadline(20, clock=lambda: now[0])
        asked = []

        def call(model, _timeout):
            asked.append(model)
            now[0] += 1
            raise restarting()

        def sleep(seconds):
            self.slept.append(seconds)
            now[0] += seconds

        with self.assertRaises(provider.ChainFailed):
            provider.run_chain(["a", "b"], call, sleep=sleep, deadline=deadline)
        # 20s budget: ask (1s), wait 15, ask (1s), then only 3s left: room for no wait.
        self.assertEqual(self.slept, [15.0])
        self.assertEqual(asked, ["a", "a"])
        self.assertGreater(deadline.remaining(), 0, "the chain ended inside its deadline")

    def test_a_capped_wait_leaves_room_for_the_request_despite_overshoot(self):
        now = [0.0]
        deadline = provider.Deadline(12, clock=lambda: now[0])
        asked = []

        def call(model, _timeout):
            asked.append(model)
            if len(asked) == 1:
                raise restarting()
            return "ok"

        def sleep(seconds):
            self.slept.append(seconds)
            now[0] += seconds + 0.01  # a real sleep overshoots

        result = provider.run_chain(["a", "b"], call, sleep=sleep, deadline=deadline)
        self.assertEqual(asked, ["a", "a"], "the capped wait was followed by a real request")
        self.assertEqual(result.model, "a")
        self.assertLess(self.slept[0], 15.0, "the wait was capped by the deadline")

    def test_retry_after_ignores_non_ascii_digits(self):
        self.assertIsNone(provider.retry_after_s({"Retry-After": "\u00b2"}))

    def test_a_missing_retry_after_uses_the_edge_default(self):
        calls = []

        def call(model, _timeout):
            calls.append(model)
            if len(calls) == 1:
                raise restarting(retry_after=None)
            return "ok"

        provider.run_chain(["a"], call, sleep=self.sleep)
        self.assertEqual(self.slept, [provider.GATEWAY_RETRY_DEFAULT_S])

    def test_an_ordinary_503_still_falls_through_without_waiting(self):
        asked = []

        def call(model, _timeout):
            asked.append(model)
            if model == "a":
                raise provider.ProviderError("HTTP 503", "provider_http_error", status=503)
            return "ok"

        result = provider.run_chain(["a", "b"], call, sleep=self.sleep)
        self.assertEqual((asked, self.slept, result.model), (["a", "b"], [], "b"))
        self.assertNotIn("detail_code", result.attempts[0])


if __name__ == "__main__":
    unittest.main()
