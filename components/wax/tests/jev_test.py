import json
import math
import unittest
from unittest.mock import patch

from wax import jev, provider

DATED_MODEL = "typesafe/jev-1.13-20260917"


def response(answers, **extra):
    body = {"model": DATED_MODEL, "answers": answers,
            "usage": {"input_tokens": 7527, "output_tokens": 639, "cost": 0.000316134},
            "id": "gen-dec-1791496808-test", "provider": "TypeSafe"}
    body.update(extra)
    return body


class FakePost:
    """Stands in for provider.post_json and records every request."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, payload, *, key, timeout, headers=None):
        self.calls.append({"url": url, "payload": json.loads(json.dumps(payload)), "key": key,
                           "timeout": timeout, "headers": headers})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


QUESTIONS = {
    "bb": jev.noul("Does the transcript mention Bloodbank?", "It does.", "It does not."),
    "kind": jev.choice("What kind of note is this?", {"monolog": "one speaker", "meeting": "several"}),
    "difficulty": jev.score("How hard is the work?", ["trivial", "routine", "hard", "frontier"]),
}
GOOD = {
    "bb": {"type": "noul", "noul": 0.97},
    # A one-option choice really answers with an integer probability of 1.
    "kind": {"type": "choice", "choice": "meeting", "probabilities": {"monolog": 0, "meeting": 1},
             "confidence": 1},
    "difficulty": {"type": "score", "score": 1.47},
}


class BuilderTest(unittest.TestCase):
    def test_question_shapes_match_the_wire_format(self):
        self.assertEqual(jev.noul("i", "t", "f"),
                         {"type": "noul", "instructions": "i", "criteria": {"true": "t", "false": "f"}})
        self.assertEqual(jev.choice("i", {"a": "A"}), {"type": "choice", "instructions": "i", "criteria": {"a": "A"}})
        self.assertEqual(jev.score("i", ["low", "high"]),
                         {"type": "score", "instructions": "i", "criteria": ["low", "high"]})
        with self.assertRaises(ValueError):
            jev.choice("i", {})
        with self.assertRaises(ValueError):
            jev.score("i", [])


class DecideTest(unittest.TestCase):
    def setUp(self):
        provider._NOTES.clear()

    def tearDown(self):
        provider._NOTES.clear()

    def test_request_shape_and_provenance(self):
        post = FakePost(response(GOOD))
        decision = jev.decide({"transcript": "body"}, QUESTIONS, key="k", post=post, title="HeyMa Wax test")
        call = post.calls[0]
        self.assertEqual(call["url"], "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(call["payload"], {"model": "typesafe/jev-1.13", "state": {"transcript": "body"},
                                           "questions": json.loads(json.dumps(QUESTIONS))})
        self.assertEqual(call["headers"]["X-Title"], "HeyMa Wax test")
        self.assertEqual(call["timeout"], 20.0)
        self.assertEqual(decision.model, DATED_MODEL)
        self.assertEqual(decision.request_id, "gen-dec-1791496808-test")
        self.assertAlmostEqual(decision.cost, 0.000316134)
        self.assertEqual(decision.noul("bb"), 0.97)
        self.assertEqual(decision.answers["kind"]["choice"], "meeting")

    def test_missing_provenance_falls_back_without_failing(self):
        post = FakePost({"answers": {"bb": {"type": "noul", "noul": 0}}})
        decision = jev.decide({}, {"bb": QUESTIONS["bb"]}, key="k", post=post, model="typesafe/jev-x")
        self.assertEqual((decision.model, decision.cost, decision.request_id), ("typesafe/jev-x", 0.0, ""))

    def test_every_malformed_answer_is_a_bad_response(self):
        cases = {
            "no answers": {"usage": {}},
            "missing answer": {"answers": {k: v for k, v in GOOD.items() if k != "bb"}},
            "wrong type": {"answers": {**GOOD, "bb": {"type": "choice", "noul": 0.5}}},
            "noul above one": {"answers": {**GOOD, "bb": {"type": "noul", "noul": 1.2}}},
            "noul is a bool": {"answers": {**GOOD, "bb": {"type": "noul", "noul": True}}},
            "noul is nan": {"answers": {**GOOD, "bb": {"type": "noul", "noul": math.nan}}},
            "noul is text": {"answers": {**GOOD, "bb": {"type": "noul", "noul": "0.9"}}},
            "unoffered choice": {"answers": {**GOOD, "kind": {**GOOD["kind"], "choice": "other"}}},
            "foreign probability": {"answers": {**GOOD, "kind": {**GOOD["kind"],
                                                                 "probabilities": {"other": 0.5}}}},
            "bad confidence": {"answers": {**GOOD, "kind": {**GOOD["kind"], "confidence": 3}}},
            "score off scale": {"answers": {**GOOD, "difficulty": {"type": "score", "score": 4}}},
            "negative score": {"answers": {**GOOD, "difficulty": {"type": "score", "score": -0.1}}},
        }
        for label, body in cases.items():
            with self.subTest(label), self.assertRaises(provider.ProviderError) as caught:
                jev.decide({}, QUESTIONS, key="k", post=FakePost(body))
            self.assertEqual(caught.exception.reason_code, "provider_bad_response")

    def test_token_budget_rejection_becomes_jev_too_large_without_retry(self):
        too_large = provider.ProviderError("HTTP 400 (max_tokens_exceeded)", "provider_bad_request",
                                           status=400, detail_code="max_tokens_exceeded")
        post = FakePost(too_large, response(GOOD))
        with self.assertRaises(provider.ProviderError) as caught:
            jev.decide({}, QUESTIONS, key="k", post=post)
        self.assertEqual(caught.exception.reason_code, "jev_too_large")
        self.assertEqual(len(post.calls), 1)

    def test_client_errors_are_not_retried(self):
        for error in (provider.ProviderError("400", "provider_bad_request", status=400),
                      provider.ProviderError("401", "provider_auth_rejected", status=401),
                      provider.ProviderError("429", "provider_rate_limited", status=429)):
            post = FakePost(error, response(GOOD))
            with self.subTest(error.reason_code), self.assertRaises(provider.ProviderError) as caught:
                jev.decide({}, QUESTIONS, key="k", post=post)
            self.assertEqual(caught.exception.reason_code, error.reason_code)
            self.assertEqual(len(post.calls), 1)

    def test_transient_failures_are_retried_once(self):
        transient = [provider.ProviderError("t", "timeout"),
                     provider.ProviderError("u", "provider_unreachable"),
                     provider.ProviderError("503", "provider_http_error", status=503)]
        with patch.object(jev, "RETRY_BACKOFF_S", 0):
            for error in transient:
                with self.subTest(error.reason_code):
                    post = FakePost(error, response(GOOD))
                    self.assertEqual(jev.decide({}, QUESTIONS, key="k", post=post).noul("bb"), 0.97)
                    self.assertEqual(len(post.calls), 2)
            post = FakePost(provider.ProviderError("t", "timeout"), provider.ProviderError("t", "timeout"))
            with self.assertRaises(provider.ProviderError) as caught:
                jev.decide({}, QUESTIONS, key="k", post=post)
            self.assertEqual(caught.exception.reason_code, "timeout")
            self.assertEqual(len(post.calls), 2)

    def test_a_request_without_questions_is_refused_locally(self):
        post = FakePost()
        for questions in ({}, {"x": {"type": "maybe"}}):
            with self.subTest(questions=questions), self.assertRaises(provider.ProviderError) as caught:
                jev.decide({}, questions, key="k", post=post)
            self.assertEqual(caught.exception.reason_code, "run_error")
        self.assertEqual(post.calls, [])


if __name__ == "__main__":
    unittest.main()
