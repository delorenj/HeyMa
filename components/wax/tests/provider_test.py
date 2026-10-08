import http.client
import io
import json
import os
import subprocess
import threading
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from wax import passes, provider

URL = "https://provider.example/v1/chat/completions"
SECRET = "fake-inference-key-never-printed"
# Shape of a real Jev 400 for an oversize state (2026-10-08), org id included.
MAX_TOKENS_BODY = json.dumps({
    "error": {"message": "HTTP 400: {\"detail\":{\"error_type\":\"max_tokens_exceeded\"}}", "code": 400},
    "user_id": "org_2f9QsecretOrgId",
}).encode()


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, *_args):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def http_error(code: int, body: bytes = b"", headers=None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "status", headers or {}, io.BytesIO(body))


class NotesIsolation(unittest.TestCase):
    def setUp(self):
        provider._NOTES.clear()
        provider._KEY_SOURCES.clear()

    def tearDown(self):
        provider._NOTES.clear()
        provider._KEY_SOURCES.clear()


class PostJsonTest(NotesIsolation):
    def post(self, *outcomes, key=SECRET):
        with patch.object(provider._OPENER, "open", side_effect=list(outcomes)) as urlopen:
            result = provider.post_json(URL, {"question": 1}, key=key, timeout=3,
                                        headers={"X-Title": "wax test"})
        return result, urlopen

    def failure(self, outcome, key=SECRET) -> provider.ProviderError:
        with self.assertRaises(provider.ProviderError) as caught:
            self.post(outcome, key=key)
        self.assertNotIn(key, str(caught.exception))
        return caught.exception

    def test_success_returns_the_object_and_sends_the_documented_headers(self):
        result, urlopen = self.post(FakeResponse(b'{"ok": true}'))
        self.assertEqual(result, {"ok": True})
        request = urlopen.call_args.args[0]
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(headers["authorization"], f"Bearer {SECRET}")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["user-agent"], "HeyMa-Wax/1.0")
        self.assertEqual(headers["x-title"], "wax test")
        self.assertEqual(json.loads(request.data), {"question": 1})
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 3)

    def test_http_statuses_map_to_reason_codes(self):
        cases = [
            (401, b'{"error":{"message":"User not found.","code":401}}', "provider_auth_rejected", None),
            (403, b"forbidden", "provider_auth_rejected", None),
            (402, b"payment required", "provider_quota_exceeded", None),
            (429, b"slow down", "provider_rate_limited", None),
            (400, b'{"error":{"message":"[zod issues]","code":400}}', "provider_bad_request", None),
            (404, b'{"error":{"message":"Model typesafe/jev-9.99 does not exist"}}', "provider_http_error",
             "model_not_found"),
            (500, b"upstream exploded", "provider_http_error", None),
            (503, b"", "provider_http_error", None),
        ]
        for status, body, reason, detail in cases:
            with self.subTest(status=status):
                error = self.failure(http_error(status, body))
                self.assertEqual(error.reason_code, reason)
                self.assertEqual(error.status, status)
                self.assertEqual(error.detail_code, detail)
                self.assertIn(f"HTTP {status}", str(error))

    def test_max_tokens_marker_is_classified_but_the_body_never_reaches_the_message(self):
        error = self.failure(http_error(400, MAX_TOKENS_BODY))
        self.assertEqual(error.reason_code, "provider_bad_request")
        self.assertEqual(error.detail_code, "max_tokens_exceeded")
        for leaked in ("org_2f9QsecretOrgId", "error_type", "detail\\"):
            self.assertNotIn(leaked, str(error))

    def test_error_body_text_is_never_repeated(self):
        error = self.failure(http_error(400, b'{"error":{"message":"transcript says: buy milk"}}'))
        self.assertNotIn("buy milk", str(error))

    def test_rate_limit_names_retry_after(self):
        error = self.failure(http_error(429, b"", {"Retry-After": "7"}))
        self.assertIn("retry after 7s", str(error))

    def test_auth_rejection_names_the_key_source_never_the_key(self):
        with patch.dict(os.environ, {"WAX_TEST_PROVIDER_KEY": SECRET}):
            key = provider.resolve_key(env_var="WAX_TEST_PROVIDER_KEY", op_ref="")
        error = self.failure(http_error(401, b"User not found."), key=key)
        self.assertIn("$WAX_TEST_PROVIDER_KEY", str(error))
        self.assertIn("is_provisioning_key", str(error))

    def test_transport_failures_map_to_timeout_or_unreachable(self):
        cases = [
            (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
            (TimeoutError("read timed out"), "timeout"),
            (urllib.error.URLError(ConnectionRefusedError(111, "refused")), "provider_unreachable"),
            (ConnectionResetError(104, "reset"), "provider_unreachable"),
            (http.client.IncompleteRead(b"partial"), "provider_unreachable"),
        ]
        for outcome, reason in cases:
            with self.subTest(outcome=type(outcome).__name__):
                self.assertEqual(self.failure(outcome).reason_code, reason)

    def test_undecodable_or_non_object_bodies_are_bad_responses(self):
        for body in (b"<html>gateway</html>", b"\xff\xfe\x00", b"[1, 2]", b'"text"'):
            with self.subTest(body=body):
                self.assertEqual(self.failure(FakeResponse(body)).reason_code, "provider_bad_response")

    def test_a_redirect_is_a_failure_and_the_key_never_follows_it(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        try:
            with self.assertRaises(provider.ProviderError) as caught:
                provider.post_json(f"http://127.0.0.1:{server.server_address[1]}/v1/x", {}, key=SECRET, timeout=5)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual((caught.exception.reason_code, caught.exception.status), ("provider_http_error", 302))
        self.assertEqual(seen, [("/v1/x", f"Bearer {SECRET}")])

    def test_invalid_url_is_a_configuration_error(self):
        with self.assertRaises(provider.ProviderError) as caught:
            provider.post_json("not a url", {}, key=SECRET, timeout=1)
        self.assertEqual(caught.exception.reason_code, "run_error")


class ResolveKeyTest(NotesIsolation):
    REF = "op://Vault/item/credential"

    def test_literal_env_var_wins_without_calling_op(self):
        with patch.dict(os.environ, {"WAX_TEST_PROVIDER_KEY": "  sk-env  "}), \
                patch.object(provider.subprocess, "run") as run:
            self.assertEqual(provider.resolve_key(env_var="WAX_TEST_PROVIDER_KEY", op_ref=self.REF), "sk-env")
        run.assert_not_called()

    def test_op_reference_is_read_with_a_timeout(self):
        done = subprocess.CompletedProcess(["op"], 0, stdout="sk-from-op\n", stderr="")
        with patch.dict(os.environ, {"WAX_TEST_PROVIDER_KEY": ""}), \
                patch.object(provider.subprocess, "run", return_value=done) as run:
            key = provider.resolve_key(env_var="WAX_TEST_PROVIDER_KEY", op_ref=self.REF, op_timeout=2.5)
        self.assertEqual(key, "sk-from-op")
        self.assertEqual(run.call_args.args[0], ["op", "read", self.REF])
        self.assertEqual(run.call_args.kwargs["timeout"], 2.5)

    def test_every_op_failure_is_no_api_key(self):
        outcomes = [
            subprocess.CompletedProcess(["op"], 1, stdout="", stderr="[ERROR] item not found\n"),
            subprocess.CompletedProcess(["op"], 0, stdout="  \n", stderr=""),
            subprocess.TimeoutExpired(["op"], 5),
            FileNotFoundError(2, "op"),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__), \
                    patch.dict(os.environ, {"WAX_TEST_PROVIDER_KEY": ""}), \
                    patch.object(provider.subprocess, "run", side_effect=[outcome]):
                with self.assertRaises(provider.ProviderError) as caught:
                    provider.resolve_key(env_var="WAX_TEST_PROVIDER_KEY", op_ref=self.REF)
                self.assertEqual(caught.exception.reason_code, "no_api_key")
                self.assertIn(self.REF, str(caught.exception))

    def test_no_env_and_no_reference_is_no_api_key(self):
        with patch.dict(os.environ, {"WAX_TEST_PROVIDER_KEY": ""}), \
                self.assertRaises(provider.ProviderError) as caught:
            provider.resolve_key(env_var="WAX_TEST_PROVIDER_KEY", op_ref="")
        self.assertEqual(caught.exception.reason_code, "no_api_key")


class ParseJsonObjectTest(unittest.TestCase):
    def test_bare_fenced_and_embedded_objects(self):
        self.assertEqual(provider.parse_json_object('{"a": 1}'), {"a": 1})
        self.assertEqual(provider.parse_json_object('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(provider.parse_json_object('Here you go:\n{"a": 3}\nThanks'), {"a": 3})

    def test_a_fence_inside_a_string_does_not_derail_bare_json(self):
        text = json.dumps({"tickets": [{"description": "Run ```json\n{}\n``` in the shell."}]})
        self.assertEqual(provider.parse_json_object(text)["tickets"][0]["description"],
                         "Run ```json\n{}\n``` in the shell.")

    def test_non_objects_and_garbage_are_bad_responses_without_echoing_content(self):
        for text in ("[1, 2]", "the transcript mentions a secret plan", ""):
            with self.subTest(text=text):
                with self.assertRaises(provider.ProviderError) as caught:
                    provider.parse_json_object(text)
                self.assertEqual(caught.exception.reason_code, "provider_bad_response")
                self.assertNotIn("secret plan", str(caught.exception))


class ChatJsonTest(NotesIsolation):
    SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}},
              "required": ["x"], "additionalProperties": False}

    def call(self, response):
        with patch.object(provider, "post_json", return_value=response) as post:
            result = provider.chat_json(base="https://gw.example/v1/", model="m", key="k", system="sys",
                                        user="usr", schema=self.SCHEMA, schema_name="thing",
                                        max_tokens=8192, timeout=9)
        return result, post

    def test_strict_schema_request_shape(self):
        result, post = self.call({"choices": [{"message": {"content": '{"x": "y"}'}, "finish_reason": "stop"}]})
        self.assertEqual(result, {"x": "y"})
        url, payload = post.call_args.args
        self.assertEqual(url, "https://gw.example/v1/chat/completions")
        self.assertEqual(payload["model"], "m")
        self.assertEqual(payload["max_tokens"], 8192)
        self.assertEqual(payload["temperature"], 0.1)
        self.assertEqual(payload["response_format"], {"type": "json_schema", "json_schema": {
            "name": "thing", "strict": True, "schema": self.SCHEMA}})
        self.assertEqual(payload["messages"], [{"role": "system", "content": "sys"},
                                               {"role": "user", "content": "usr"}])
        self.assertEqual(post.call_args.kwargs["timeout"], 9)

    def test_content_parts_are_joined(self):
        result, _ = self.call({"choices": [{"message": {"content": [{"type": "text", "text": '{"x":'},
                                                                    {"type": "text", "text": ' "z"}'}]}}]})
        self.assertEqual(result, {"x": "z"})

    def test_error_envelope_names_only_the_code(self):
        with self.assertRaises(provider.ProviderError) as caught:
            self.call({"error": {"code": 502, "message": "upstream said something long"}})
        self.assertEqual(caught.exception.reason_code, "provider_bad_response")
        self.assertIn("502", str(caught.exception))
        self.assertNotIn("upstream said", str(caught.exception))

    def test_truncated_output_is_named(self):
        with self.assertRaises(provider.ProviderError) as caught:
            self.call({"choices": [{"message": {"content": '{"x": "unfinis'}, "finish_reason": "length"}]})
        self.assertEqual(caught.exception.detail_code, "output_truncated")

    def test_empty_content_is_a_bad_response(self):
        with self.assertRaises(provider.ProviderError) as caught:
            self.call({"choices": [{"message": {"content": "  "}, "finish_reason": "content_filter"}]})
        self.assertEqual(caught.exception.reason_code, "provider_bad_response")
        self.assertIn("content_filter", str(caught.exception))


class PassOutputTest(NotesIsolation):
    def test_fail_puts_a_runner_readable_reason_first_and_notes_last(self):
        provider.note("note: something worth knowing")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(provider.fail("provider_rate_limited", "HTTP 429 from x"), 1)
        lines = stderr.getvalue().splitlines()
        self.assertEqual(lines, ["reason_code=provider_rate_limited", "HTTP 429 from x",
                                 "note: something worth knowing"])
        self.assertEqual(passes._split_reason(stderr.getvalue())[0], "provider_rate_limited")

    def test_unclassifiable_codes_degrade_to_run_error(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            provider.fail("jev_status_429", "detail")
        self.assertEqual(stderr.getvalue().splitlines()[0], "reason_code=run_error")

    def test_emit_result_is_one_ascii_line_the_runner_parses(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        provider.note("note: after the result")
        result = {"wax_ep_version": 1, "frontmatter": {"projects": ["Café …"]}}
        with redirect_stdout(stdout), redirect_stderr(stderr):
            provider.emit_result(result)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].isascii())
        self.assertEqual(passes._parse_result(stdout.getvalue()), result)
        self.assertEqual(stderr.getvalue().strip(), "note: after the result")

    def test_env_number_falls_back_with_a_note(self):
        with patch.dict(os.environ, {"WAX_TEST_NUMBER": "0.7"}):
            self.assertEqual(provider.env_number("WAX_TEST_NUMBER", 0.6, maximum=1), 0.7)
        for raw in ("high", "1.5", "-1"):
            with self.subTest(raw=raw), patch.dict(os.environ, {"WAX_TEST_NUMBER": raw}):
                self.assertEqual(provider.env_number("WAX_TEST_NUMBER", 0.6, maximum=1), 0.6)
        self.assertEqual(len(provider._NOTES), 3)


class ContextTest(unittest.TestCase):
    def test_document_text_drops_only_the_wax_header(self):
        self.assertEqual(provider.document_text("\n# Transcription: a.ogg\n\n**Speaker 1 (00:00):** hi\n"),
                         "**Speaker 1 (00:00):** hi")
        self.assertEqual(provider.document_text("# Plan\n\nShip it.\n"), "# Plan\n\nShip it.")
        self.assertEqual(provider.document_text("\n# Transcription: a.ogg\n"), "")

    def test_windows_overlap_and_cover_everything(self):
        text = "".join(chr(97 + i % 26) for i in range(1000))
        parts = provider.windows(text, 300, 50)
        self.assertEqual(provider.windows(text[:300], 300, 50), [text[:300]])
        self.assertTrue(all(len(part) <= 300 for part in parts))
        self.assertEqual(parts[0], text[:300])
        self.assertTrue(text.endswith(parts[-1]))
        for left, right in zip(parts, parts[1:]):
            self.assertEqual(left[-50:], right[:50])
        rebuilt = parts[0] + "".join(part[50:] for part in parts[1:])
        self.assertEqual(rebuilt, text)
        with self.assertRaises(ValueError):
            provider.windows(text, 50, 50)


if __name__ == "__main__":
    unittest.main()
