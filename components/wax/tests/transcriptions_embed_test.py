import json
import math
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wax.transcriptions import embed


class FakeOllama:
    """Minimal /api/tags + /api/embed on an ephemeral port; records every request."""

    def __init__(self, *, digest=embed.MODEL_DIGEST, dims=embed.DIMS, scale=1.0):
        self.requests = []
        self.digest = digest
        self.dims = dims
        self.scale = scale
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                owner.requests.append(("GET", self.path, None))
                self._send({"models": [{"name": embed.MODEL_NAME, "model": embed.MODEL_NAME, "digest": owner.digest}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("POST", self.path, body))
                if self.path == "/api/generate":
                    self._send({"model": body["model"], "response": "", "done": True, "done_reason": "unload"})
                    return
                vectors = []
                for text in body["input"]:
                    seed = sum(text.encode()) or 1
                    raw = [math.sin(seed + i) for i in range(owner.dims)]
                    norm = math.sqrt(sum(x * x for x in raw))
                    vectors.append([owner.scale * x / norm for x in raw])
                self._send({"model": body["model"], "embeddings": vectors})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class FakeConn:
    """Just enough of a psycopg connection for query_vector/active_model/sweep's preamble."""

    def __init__(self):
        self.statements = []

    class _Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "FROM tx.embedding_model" in sql:
            return self._Result((1, embed.MODEL_NAME, embed.MODEL_DIGEST, embed.DIMS, embed.INSTRUCTION_VERSION,
                                 embed.QUERY_INSTRUCTION))
        if "has_table_privilege" in sql:
            return self._Result((False,))
        if "count(*)" in sql:
            return self._Result((0, None))
        return self._Result(None)


class EmbedTest(unittest.TestCase):
    def test_request_shape_and_batching(self):
        with FakeOllama() as fake:
            embedder = embed.OllamaEmbedder(fake.url, keep_alive="30m", timeout=5)
            vectors = embedder.embed([f"chunk {i}" for i in range(19)])
        self.assertEqual(len(vectors), 19)
        posts = [body for method, path, body in fake.requests if method == "POST"]
        self.assertEqual([len(b["input"]) for b in posts], [8, 8, 3])
        for body in posts:
            self.assertEqual(body["model"], "qwen3-embedding:8b")
            self.assertEqual(body["dimensions"], 2048)
            self.assertIs(body["truncate"], True)
            self.assertEqual(body["options"], {"num_ctx": 2048})
            self.assertEqual(body["keep_alive"], "30m")
            self.assertLessEqual(len(body["input"]), 8)
        self.assertEqual(posts[0]["input"][0], "chunk 0")       # documents carry no prefix

    def test_query_gets_the_instruction_prefix(self):
        with FakeOllama() as fake:
            conn = FakeConn()
            literal = embed.query_vector(conn, "phone quality time", embedder=embed.OllamaEmbedder(fake.url))
        posts = [body for method, _p, body in fake.requests if method == "POST"]
        self.assertEqual(posts[0]["input"], [
            "Instruct: Given a search query, retrieve relevant passages from the user's voice-memo and meeting "
            "transcripts\nQuery: phone quality time"])
        self.assertTrue(literal.startswith("[") and literal.endswith("]"))
        self.assertEqual(len(literal[1:-1].split(",")), 2048)
        self.assertFalse(any("INSERT INTO" in sql for sql, _ in conn.statements))   # no cache write without privilege

    def test_non_unit_vector_is_renormalised_and_wrong_length_raises(self):
        with FakeOllama(scale=3.0) as fake:
            vector = embed.OllamaEmbedder(fake.url).embed(["x"])[0]
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in vector)), 1.0, places=6)
        with FakeOllama(dims=1024) as fake:
            with self.assertRaises(embed.EmbedError) as caught:
                embed.OllamaEmbedder(fake.url).embed(["x"])
        self.assertEqual(caught.exception.reason_code, "bad_dimensions")

    def test_digest_mismatch_refuses_the_sweep(self):
        with FakeOllama(digest="0" * 64) as fake:
            embedder = embed.OllamaEmbedder(fake.url)
            with self.assertRaises(embed.EmbedError) as caught:
                embed.sweep(conn=FakeConn(), embedder=embedder)
        self.assertEqual(caught.exception.reason_code, "digest_mismatch")
        self.assertFalse([r for r in fake.requests if r[0] == "POST"])

    def test_query_digest_mismatch_refuses_and_caches_nothing(self):
        # A re-tagged model must not embed the query into a different space than the stored vectors,
        # nor leave that vector cached under the active model_id.
        with FakeOllama(digest="0" * 64) as fake:
            conn = FakeConn()
            with self.assertRaises(embed.EmbedError) as caught:
                embed.query_vector(conn, "phone quality time", embedder=embed.OllamaEmbedder(fake.url), cache=True)
        self.assertEqual(caught.exception.reason_code, "digest_mismatch")
        self.assertFalse([r for r in fake.requests if r[0] == "POST"])
        self.assertFalse(any("INSERT INTO" in sql for sql, _ in conn.statements))

    def test_unload_asks_ollama_to_drop_the_model(self):
        with FakeOllama() as fake:
            embed.OllamaEmbedder(fake.url).unload()
        self.assertEqual(fake.requests, [("POST", "/api/generate", {"model": "qwen3-embedding:8b", "keep_alive": 0})])

    def test_sweep_stops_before_any_batch_when_the_gpu_is_busy(self):
        with FakeOllama() as fake:
            stats = embed.sweep(conn=FakeConn(), embedder=embed.OllamaEmbedder(fake.url), stop=lambda: True)
        self.assertEqual(stats["stopped"], "gpu_busy")
        self.assertEqual(stats["batches"], 0)
        self.assertFalse([r for r in fake.requests if r[0] == "POST"])

    def test_keep_alive_default_is_short(self):
        # D.9's 30m kept 5.3 GB resident under whisper's 5,000 MB free-VRAM floor (design G.7).
        from wax.transcriptions import config
        with unittest.mock.patch.dict("os.environ", {}, clear=True), \
                unittest.mock.patch.object(config, "UNIT_PATH", config.Path("/nonexistent/unit")):
            self.assertEqual(config.embed_keep_alive(), "2m")
            self.assertEqual(embed.OllamaEmbedder("http://127.0.0.1:9").keep_alive, "2m")

    def test_unreachable_ollama_is_an_embed_error(self):
        with self.assertRaises(embed.EmbedError) as caught:
            embed.OllamaEmbedder("http://127.0.0.1:9", timeout=1).check_digest()
        self.assertEqual(caught.exception.reason_code, "ollama_unreachable")

    def test_halfvec_literal(self):
        self.assertEqual(embed.halfvec_literal([0.1, -2.5e-07, 1.0, 0.333333333]), "[0.1,-2.5e-07,1,0.333333]")

    def test_registry_mismatch_refuses(self):
        class Drifted(FakeConn):
            def execute(self, sql, params=None):
                if "FROM tx.embedding_model" in sql:
                    return self._Result((1, embed.MODEL_NAME, "f" * 64, 2048, 1, embed.QUERY_INSTRUCTION))
                return super().execute(sql, params)
        with self.assertRaises(embed.EmbedError) as caught:
            embed.active_model(Drifted())
        self.assertEqual(caught.exception.reason_code, "model_registry_mismatch")


if __name__ == "__main__":
    unittest.main()
