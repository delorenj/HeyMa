"""Real-Postgres tests for the transcriptions store (design E.2).

Run with the interpreter that has psycopg (system Python):

    /usr/bin/python3 -m pytest tests/transcriptions_pg_test.py -q

Skips cleanly when psycopg is missing (mise Python), when `docker info` fails,
or when `pgvector/pgvector:0.8.0-pg17` cannot be started within 60 s. The
container uses a password generated here; no real credential is touched.
"""

import contextlib
import hashlib
import json
import math
import os
import random
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import psycopg
    from psycopg import errors as pg_errors
    HAVE_PSYCOPG = True
except ImportError:  # mise Python has no psycopg: the whole module skips
    HAVE_PSYCOPG = False

from wax import frontmatter, ledger, paths, sentinel
from wax import transcriptions
from wax.transcriptions import config, db, embed, health, migrate, outbox, reconcile, search, store

IMAGE = "pgvector/pgvector:0.8.0-pg17"
STATE: dict = {}


def _docker(*args, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def setUpModule():  # noqa: N802 - unittest hook
    if not HAVE_PSYCOPG:
        raise unittest.SkipTest("psycopg is not importable (run under /usr/bin/python3)")
    if shutil.which("docker") is None:
        raise unittest.SkipTest("docker is not installed")
    try:
        if _docker("info", timeout=20).returncode != 0:
            raise unittest.SkipTest("docker info failed")
    except subprocess.TimeoutExpired:
        raise unittest.SkipTest("docker info timed out") from None
    superpw = secrets.token_hex(16)
    try:
        run = _docker("run", "-d", "--rm", "-e", f"POSTGRES_PASSWORD={superpw}", "-p", "127.0.0.1::5432", IMAGE)
    except subprocess.TimeoutExpired:
        raise unittest.SkipTest(f"docker run {IMAGE} timed out") from None
    if run.returncode != 0:
        raise unittest.SkipTest(f"docker run {IMAGE} failed: {run.stderr.strip()[:200]}")
    cid = run.stdout.strip()
    STATE["container"] = cid
    port = None
    deadline = time.time() + 60
    while time.time() < deadline and port is None:
        out = _docker("port", cid, "5432/tcp", timeout=10).stdout.strip().splitlines()
        if out:
            port = int(out[0].rsplit(":", 1)[1])
        else:
            time.sleep(0.5)
    if port is None:
        tearDownModule()
        raise unittest.SkipTest("container exposed no port")
    STATE["port"] = port
    superdsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    while time.time() < deadline:
        try:
            with psycopg.connect(superdsn, password=superpw, connect_timeout=2) as c:
                c.execute("SELECT 1")
            break
        except psycopg.Error:
            time.sleep(0.5)
    else:
        tearDownModule()
        raise unittest.SkipTest("postgres in the container did not become ready within 60 s")
    pw = {role: secrets.token_hex(16) for role in ("owner", "app", "ro")}
    STATE["pw"] = pw
    STATE["superdsn"], STATE["superpw"] = superdsn, superpw
    with psycopg.connect(superdsn, password=superpw, autocommit=True) as c:
        for role in ("owner", "app", "ro"):
            c.execute(f"CREATE ROLE transcriptions_{role} LOGIN PASSWORD '{pw[role]}'")
        c.execute("CREATE DATABASE transcriptions OWNER transcriptions_owner TEMPLATE template0 ENCODING 'UTF8'")
        c.execute("REVOKE ALL ON DATABASE transcriptions FROM PUBLIC")
        c.execute("GRANT CONNECT ON DATABASE transcriptions TO transcriptions_app, transcriptions_ro")
        for role in ("owner", "app", "ro"):
            c.execute(f"ALTER ROLE transcriptions_{role} IN DATABASE transcriptions SET search_path = tx, public")
    with psycopg.connect(f"postgresql://postgres@127.0.0.1:{port}/transcriptions", password=superpw,
                         autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        c.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def tearDownModule():  # noqa: N802
    cid = STATE.get("container")
    if cid:
        _docker("unpause", cid, timeout=30)
        _docker("stop", "-t", "2", cid, timeout=60)


# ---------------------------------------------------------------- fixtures --

class FakeEmbedder:
    """Deterministic unit vectors seeded from sha256(input); counts calls like OllamaEmbedder."""
    batch = 8

    def __init__(self):
        self.calls = 0
        self.inputs = 0
        self.texts = []

    def check_digest(self):
        return None

    @staticmethod
    def vector(text):
        rng = random.Random(int(hashlib.sha256(text.encode()).hexdigest(), 16))
        raw = [rng.gauss(0, 1) for _ in range(embed.DIMS)]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]

    def embed(self, texts):
        out = []
        for start in range(0, len(texts), self.batch):
            chunk = list(texts[start:start + self.batch])
            self.calls += 1
            self.inputs += len(chunk)
            self.texts.extend(chunk)
            out.extend(self.vector(t) for t in chunk)
        return out


TOPICS = ["you were on your phone again instead of spending quality time with the kids",
          "the garage door is broken and the weather is cold this morning",
          "we need to practice chess openings before the tournament on saturday",
          "dinner tonight is egg rolls and a science documentary about volcanoes",
          "the job search is hard but the interview with the legal firm went well"]


def make_body(name, turns=36, words=48, tail=None):
    rng = random.Random(name)
    vocab = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa "
             "quebec romeo sierra tango uniform victor whiskey xray yankee zulu").split()
    parts = [f"# Transcription: {name}", ""]
    for i in range(turns):
        minutes, seconds = divmod(i * 41, 60)
        text = TOPICS[i % len(TOPICS)] + " " + " ".join(rng.choice(vocab) for _ in range(words))
        if tail is not None and i == turns - 1:
            text = tail
        parts.append(f"**Speaker {1 + i % 2} ({minutes:02d}:{seconds:02d}):** {text}")
        parts.append("")
    return "\n".join(parts)


def write_note(path, fm, body):
    path.write_text(frontmatter.render(fm, body))


def _close_ledger():
    conn = getattr(ledger._local, "conn", None)
    if conn is not None:
        conn.close()
        del ledger._local.conn


# ---------------------------------------------------------------- the cases --

@unittest.skipUnless(HAVE_PSYCOPG, "psycopg is not importable (run under /usr/bin/python3)")
class TranscriptionsStoreTest(unittest.TestCase):
    """Ordered: each case builds on the store the previous one left (E.2 1-13)."""

    ITEM = "4c9f2d894fde1cc5"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.var, cls.vault = root / "var", root / "vault"
        cls.var.mkdir()
        cls.vault.mkdir()
        port = STATE["port"]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("WAX_TRANSCRIPTS", "WAX_EMBED", "WAX_S3"))}
        env.update({
            "WAX_TRANSCRIPTS_DSN": f"postgresql://transcriptions_app@127.0.0.1:{port}/transcriptions",
            "WAX_TRANSCRIPTS_PASSWORD": STATE["pw"]["app"],
            "WAX_TRANSCRIPTS_OWNER_DSN": f"postgresql://transcriptions_owner@127.0.0.1:{port}/transcriptions",
            "WAX_TRANSCRIPTS_OWNER_PASSWORD": STATE["pw"]["owner"],
            "WAX_EMBED_URL": "http://127.0.0.1:9",
        })
        _close_ledger()
        cls.patches = [patch.object(paths, "VAR", cls.var), patch.object(paths, "DB", cls.var / "wax.db"),
                       patch.object(paths, "VAULT", cls.vault),
                       patch.object(config, "UNIT_PATH", root / "no-unit.service"),
                       patch.dict(os.environ, env, clear=True)]
        for p in cls.patches:
            p.start()
        db.forget_passwords()
        cls.embedder = FakeEmbedder()

    @classmethod
    def tearDownClass(cls):
        _close_ledger()
        for p in reversed(cls.patches):
            p.stop()
        cls.tmp.cleanup()

    # -------------------------------------------------------------- helpers

    def app(self):
        return db.connect("app", autocommit=True)

    def owner(self):
        return db.connect("owner", autocommit=True)

    def one(self, sql, params=()):
        with contextlib.closing(self.app()) as c:
            return c.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        with contextlib.closing(self.app()) as c:
            return c.execute(sql, params).fetchall()

    def sweep(self):
        before = (self.embedder.calls, self.embedder.inputs)
        stats = embed.sweep(until_empty=True, embedder=self.embedder, settled=lambda item: True)
        return self.embedder.calls - before[0], self.embedder.inputs - before[1], stats

    def ledger_rows(self, item_id, md, *, origin="dropoff"):
        conn = ledger.connect()
        now = sentinel.utcnow()
        conn.execute("INSERT OR REPLACE INTO items(item_id,sha256,path,orig_name,bytes,duration_s,origin,state,"
                     "first_seen,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (item_id, item_id + "0" * 48, f"/archive/{item_id}.ogg", f"{item_id}.ogg", 1234, 300.0,
                      origin, "complete", now, now))
        conn.execute("INSERT OR REPLACE INTO transcripts(item_id,md_path,audio_duration,asr_duration,duration_ratio,"
                     "word_count,diarized,engine_model,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (item_id, str(md), 300.0, 299.5, 0.998, 900, 0, "large-v3", now))
        conn.execute("INSERT OR REPLACE INTO backups(item_id,s3_key,bucket,bytes,verified_at,method) "
                     "VALUES(?,?,?,?,?,?)", (item_id, f"2026-10-27/{item_id[:12]}-x.ogg", "recordings", 1234, now, "etag"))

    def record(self, md, slug, *, attempt=1, version=1, state="completed", kind="version", cid=None,
               changed=None, reason=None):
        transcriptions.record_pass(
            item_id=self.ITEM, slug=slug, kind=kind, state=state, md_path=md, pass_version=version,
            attempt=attempt, command_id=cid or f"00000000-0000-5000-8000-{abs(hash((slug, attempt))) % 10**12:012d}",
            reason_code=reason, detail="", definition={"slug": slug, "version": version},
            pass_result={"evidence": {"model": "automaticai/auto/fast", "served_by": "glm",
                                      "fallbacks": [{"model": "a/b", "reason_code": "provider_429"}]}},
            changed_fields=changed or [], duration_s=1.5)

    def pass_entry(self, fm, slug, attempt=1, version=1):
        fm.setdefault("wax", {}).setdefault("passes", {})[slug] = {
            "state": "completed", "at": sentinel.utcnow(), "version": version, "attempt": attempt,
            "command_id": f"00000000-0000-5000-8000-{abs(hash((slug, attempt))) % 10**12:012d}"}

    def head(self, item=None):
        return self.one("SELECT t.transcript_id, t.current_version_id, c.version_id, c.version_no, c.produced_by, "
                        "c.content_sha256, c.body_sha256, t.note_relpath FROM tx.transcript t "
                        "JOIN tx.transcript_current c USING (transcript_id) WHERE t.wax_item_id = %s",
                        (item or self.ITEM,))

    # -------------------------------------------------------------- 1

    def test_01_migrations(self):
        first = migrate.apply()
        self.assertEqual(first["applied"], ["0001_core", "0002_search"])
        second = migrate.apply()
        self.assertEqual(second["applied"], [])
        with tempfile.TemporaryDirectory() as edited:
            for f in migrate.MIGRATIONS.glob("*.sql"):
                shutil.copy(f, edited)
            with open(Path(edited) / "0001_core.sql", "a") as handle:
                handle.write("\n-- an edit\n")
            with self.assertRaises(migrate.MigrationError):
                migrate.apply(directory=Path(edited))
        with contextlib.closing(self.app()) as c:
            self.assertTrue(migrate.status(c)["current"])
            self.assertEqual(c.execute("SELECT count(*) FROM tx.check_invariants()").fetchone()[0], 0)

    # -------------------------------------------------------------- 3

    def test_02_transcription_snapshot_is_v1(self):
        cls = type(self)
        cls.md = self.vault / "20261027-221920-rec.md"
        cls.fm = {"schema-version": 1, "asset-kind": "transcript", "source": "audio-recording",
                  "captured": "2026-02-10T15:36:25-05:00", "language": "en", "engine-model": "large-v3",
                  "audio-duration-seconds": 300.0, "classification": "meeting", "tags": ["family"],
                  "wax-item-id": self.ITEM,
                  "wax": {"transcribed_at": "2026-10-27T22:19:20Z", "audio_duration_s": 300.0,
                          "duration_ratio": 0.998, "source_audio": "memo-a.ogg"}}
        cls.body = make_body("memo-a.ogg")
        write_note(cls.md, cls.fm, cls.body)
        self.ledger_rows(self.ITEM, cls.md)
        asr = self.var / "asr.json"
        asr.write_text(json.dumps({"source_sha256": "a" * 64, "result": {"language": "en", "language_probability": 0.98,
                                   "segments": [{}] * 36, "duration": 299.5, "model": "large-v3",
                                   "backend": "faster-whisper"}}))
        transcriptions.record_transcription(item_id=self.ITEM, md_path=cls.md, meta={"model": "large-v3"},
                                            verdict={"ok": True}, asr_path=asr, attempt=1, processing_seconds=11.0)
        self.assertEqual(outbox.pending(self.ITEM), [], "the synchronous projection drained the queue")
        tid, current, head_version, version_no, produced_by, _c, _b, _p = self.head()
        self.assertEqual(current, head_version)
        self.assertEqual((version_no, produced_by), (1, "transcription"))
        cls.tid = tid
        chunks = self.one("SELECT count(*) FROM tx.chunk_current WHERE transcript_id = %s", (tid,))[0]
        self.assertGreater(chunks, 3)
        hits = self.one("SELECT count(*) FROM tx.chunk_current WHERE transcript_id = %s "
                        "AND tsv @@ plainto_tsquery('english', 'garage door weather')", (tid,))[0]
        self.assertGreater(hits, 0, "FTS finds new chunks before any embedding")
        self.assertEqual(self.one("SELECT count(*) FROM tx.chunk_vector_current")[0], 0)
        ident = self.one("SELECT origin, language_probability, asr_segment_count, audio_s3_bucket, source_sha256, "
                         "ledger_state FROM tx.transcript WHERE transcript_id = %s", (tid,))
        self.assertEqual(ident[0], "wax")
        self.assertAlmostEqual(ident[1], 0.98, places=3)
        self.assertEqual(ident[2:4], (36, "recordings"))
        calls, inputs, _ = self.sweep()
        unique = self.one("SELECT count(DISTINCT search_sha256) FROM tx.chunk_current")[0]
        # every distinct chunk text, plus one doc vector for doc_input 'Tags: family'
        self.assertEqual(inputs, unique + 1)
        self.assertEqual(calls, math.ceil(unique / 8) + 1)
        self.assertEqual(self.one("SELECT count(*) FROM tx.chunk_vector_current")[0], chunks)
        self.assertEqual(self.one("SELECT count(*) FROM tx.doc_vector")[0], 1)
        restored = self.one("SELECT v.frontmatter_block || b.body = %s FROM tx.transcript_version v "
                            "JOIN tx.transcript_body b USING (body_sha256) WHERE v.version_id = %s",
                            (cls.md.read_text(), head_version))[0]
        self.assertTrue(restored, "every version is restorable byte-for-byte")

    # -------------------------------------------------------------- 2

    def test_03_versions_are_immutable(self):
        with contextlib.closing(self.app()) as c:
            with self.assertRaises(pg_errors.InsufficientPrivilege):
                c.execute("UPDATE tx.transcript_version SET produced_by = 'x'")
            with self.assertRaises(pg_errors.InsufficientPrivilege):
                c.execute("DELETE FROM tx.transcript_version")
            with self.assertRaises(pg_errors.InsufficientPrivilege):
                c.execute("UPDATE tx.transcript_body SET body = ''")
        with contextlib.closing(self.owner()) as c:
            with self.assertRaises(pg_errors.InsufficientPrivilege) as caught:
                c.execute("UPDATE tx.transcript_version SET produced_by = 'x'")
            self.assertIn("append-only", str(caught.exception))
            with self.assertRaises(pg_errors.InsufficientPrivilege):
                c.execute("DELETE FROM tx.transcript_body")

    # -------------------------------------------------------------- 4

    def test_04_frontmatter_stamp_is_v2_with_zero_embeddings(self):
        cls = type(self)
        before_chunks = [r[0] for r in self.all("SELECT chunk_id FROM tx.chunk_current WHERE transcript_id = %s "
                                                "ORDER BY ordinal", (cls.tid,))]
        before_cache = self.one("SELECT count(*) FROM tx.embedding_cache")[0]
        before_content = self.head()[5]
        cls.fm.update({"duration-seconds": 300.0, "word-count": 900})
        self.pass_entry(cls.fm, "frontmatter-stamp")
        write_note(cls.md, cls.fm, cls.body)
        self.record(cls.md, "frontmatter-stamp", changed=["frontmatter.duration-seconds"])
        _t, _cur, _v, version_no, produced_by, content, _b, _p = self.head()
        self.assertEqual((version_no, produced_by), (2, "frontmatter-stamp"))
        self.assertNotEqual(content, before_content)
        after_chunks = [r[0] for r in self.all("SELECT chunk_id FROM tx.chunk_current WHERE transcript_id = %s "
                                               "ORDER BY ordinal", (cls.tid,))]
        self.assertEqual(after_chunks, before_chunks)
        calls, _inputs, _ = self.sweep()
        self.assertEqual(calls, 0)
        self.assertEqual(self.one("SELECT count(*) FROM tx.embedding_cache")[0], before_cache)
        run = self.one("SELECT outcome, source, model, fell_through FROM tx.pass_run WHERE slug = 'frontmatter-stamp'")
        self.assertEqual(run[:3], ("version", "hook", "automaticai/auto/fast"))
        self.assertEqual(run[3], [{"model": "a/b", "reason_code": "provider_429"}])

    # -------------------------------------------------------------- 5

    def test_05_title_slug_rename_is_v3_with_one_doc_embedding(self):
        cls = type(self)
        old = cls.md
        cls.fm.update({"title": "Spouses Disagreeing Over Phone Time", "summary": "A couple argues about phones.",
                       "title-slug": "spouses-disagreeing-over-phone-time"})
        self.pass_entry(cls.fm, "title-slug", version=3)
        new = self.vault / "20261027-221920-spouses-disagreeing-over-phone-time.md"
        write_note(new, cls.fm, cls.body)
        old.unlink()
        cls.md = new
        self.record(new, "title-slug", version=3, changed=["frontmatter.title", "transcript.filename"])
        tid, _cur, _v, version_no, produced_by, _c, _b, relpath = self.head()
        self.assertEqual((version_no, produced_by, relpath), (3, "title-slug", new.name))
        paths_ = {r[0]: r[1] for r in self.all("SELECT note_relpath, role FROM tx.transcript_path "
                                               "WHERE transcript_id = %s", (tid,))}
        self.assertEqual(paths_.get(old.name), "canonical")
        self.assertEqual(paths_.get(new.name), "canonical")
        calls, inputs, _ = self.sweep()
        self.assertEqual((calls, inputs), (1, 1))
        self.assertEqual(self.embedder.texts[-1], "Title: Spouses Disagreeing Over Phone Time\n"
                                                  "Summary: A couple argues about phones.\nTags: family")
        self.assertEqual(self.one("SELECT count(*) FROM tx.doc_vector")[0], 1)

    # -------------------------------------------------------------- 6

    def test_06_diarization_body_is_v4_reusing_cached_vectors(self):
        cls = type(self)
        before = {r[0] for r in self.all("SELECT search_sha256 FROM tx.chunk_current WHERE transcript_id = %s",
                                         (cls.tid,))}
        before_ids = {r[0] for r in self.all("SELECT chunk_id FROM tx.chunk_current WHERE transcript_id = %s",
                                             (cls.tid,))}
        cls.body = make_body("memo-a.ogg", tail="and that is the final word on the phone argument tonight")
        cls.fm.update({"diarized": True})
        self.pass_entry(cls.fm, "diarization")
        write_note(cls.md, cls.fm, cls.body)
        cls.diar_cid = "00000000-0000-5000-8000-0000000d1a01"
        self.record(cls.md, "diarization", cid=cls.diar_cid, changed=["transcript.body"])
        _t, _cur, _v, version_no, produced_by, _c, body_sha, _p = self.head()
        self.assertEqual((version_no, produced_by), (4, "diarization"))
        rows = self.all("SELECT chunk_id, search_sha256 FROM tx.chunk_current WHERE transcript_id = %s", (cls.tid,))
        self.assertFalse(before_ids & {r[0] for r in rows}, "chunks were swapped")
        after = {r[1] for r in rows}
        new_texts = after - before
        self.assertTrue(new_texts and (after & before), "some chunk texts are new, some unchanged")
        prefilled = self.one("SELECT count(*) FROM tx.chunk_vector_current WHERE transcript_id = %s", (cls.tid,))[0]
        self.assertEqual(prefilled, len([r for r in rows if r[1] in before]), "cached texts reuse their vectors")
        calls, inputs, _ = self.sweep()
        self.assertEqual(inputs, len(new_texts))
        self.assertEqual(calls, math.ceil(len(new_texts) / 8))
        self.assertEqual(self.one("SELECT count(*) FROM tx.chunk_current c JOIN tx.transcript_current h "
                                  "USING (transcript_id) WHERE c.body_sha256 <> h.body_sha256")[0], 0)

    # -------------------------------------------------------------- 7

    def test_07_redelivery_is_a_noop(self):
        versions = self.one("SELECT count(*) FROM tx.transcript_version")[0]
        runs = self.one("SELECT count(*) FROM tx.pass_run")[0]
        self.record(type(self).md, "diarization", cid=type(self).diar_cid)      # SQLite INSERT OR IGNORE
        snap = ledger.connect().execute("SELECT * FROM transcript_snapshots WHERE idempotency_key = ?",
                                        (f"pass:{type(self).diar_cid}",)).fetchone()
        snap = dict(snap)
        snap["payload"] = json.loads(snap["payload"])
        with contextlib.closing(self.app()) as c:
            with c.transaction():
                outcome = store.apply_snapshot(c, snap)
        self.assertIn(outcome.decision, ("no_change", "duplicate"))
        self.assertEqual(self.one("SELECT count(*) FROM tx.transcript_version")[0], versions)
        self.assertEqual(self.one("SELECT count(*) FROM tx.pass_run")[0], runs)

    # -------------------------------------------------------------- 8

    def test_08_same_version_retry_without_change_is_no_change(self):
        cls = type(self)
        versions = self.one("SELECT count(*) FROM tx.transcript_version")[0]
        self.pass_entry(cls.fm, "diarization", attempt=2)          # only wax.passes moves
        write_note(cls.md, cls.fm, cls.body)
        self.record(cls.md, "diarization", attempt=2, cid="00000000-0000-5000-8000-0000000d1a02")
        self.assertEqual(self.one("SELECT count(*) FROM tx.transcript_version")[0], versions)
        run = self.one("SELECT outcome, version_id FROM tx.pass_run WHERE idempotency_key = %s",
                       ("pass:00000000-0000-5000-8000-0000000d1a02",))
        self.assertEqual(run[0], "no_change")
        failed = "00000000-0000-5000-8000-0000000fa11d"
        self.record(cls.md, "classification", kind="pass_run", state="failed", cid=failed, reason="timeout")
        self.assertEqual(self.one("SELECT outcome, state, version_id FROM tx.pass_run WHERE idempotency_key = %s",
                                  (f"pass:{failed}",)), ("not_applied", "failed", None))
        self.assertEqual(self.one("SELECT count(*) FROM tx.transcript_version")[0], versions)

    # -------------------------------------------------------------- 9

    def test_09_version_without_head_upsert_fails_at_commit(self):
        cls = type(self)
        with contextlib.closing(self.app()) as c:
            row = c.execute("SELECT * FROM tx.transcript_version WHERE transcript_id = %s ORDER BY version_no DESC "
                            "LIMIT 1", (cls.tid,))
            names = [d.name for d in row.description]
            values = dict(zip(names, row.fetchone()))
            for drop in ("version_id", "version_no", "created_at"):
                values.pop(drop)
            values["idempotency_key"] = "manual:no-head"
            values["frontmatter"] = psycopg.types.json.Jsonb(values["frontmatter"])
            values["vault_extra"] = psycopg.types.json.Jsonb(values["vault_extra"])
            for k in ("fell_through", "evidence"):
                if values.get(k) is not None:
                    values[k] = psycopg.types.json.Jsonb(values[k])
            cols = list(values)
            with self.assertRaises(pg_errors.ForeignKeyViolation):
                with c.transaction():
                    c.execute(f"INSERT INTO tx.transcript_version ({', '.join(cols)}) "
                              f"VALUES ({', '.join(['%s'] * len(cols))})", [values[k] for k in cols])
        self.assertEqual(self.head()[3], 4, "the head is untouched")

    # -------------------------------------------------------------- 10

    def _extra_transcript(self, item, name, *, classification, tags, captured, title, body_topic):
        md = self.vault / name
        fm = {"captured": captured, "classification": classification, "tags": tags, "title": title,
              "wax-item-id": item, "audio-duration-seconds": 120.0,
              "wax": {"audio_duration_s": 120.0, "source_audio": name.replace(".md", ".ogg")}}
        body = "# Transcription: x\n\n" + "\n\n".join(
            f"**Speaker 1 ({i:02d}:00):** {body_topic} {' '.join(['memo'] * 10)}" for i in range(4)) + "\n"
        write_note(md, fm, body)
        self.ledger_rows(item, md)
        transcriptions.record_transcription(item_id=item, md_path=md, meta={}, verdict={}, asr_path=self.var / "x",
                                            attempt=1, processing_seconds=1.0)
        return md

    def test_10_search(self):
        cls = type(self)
        self._extra_transcript("1111111111111111", "monolog-b.md", classification="monolog", tags=["work"],
                               captured="2026-10-01T09:00:00-04:00", title="Chess Openings Practice",
                               body_topic="practicing the sicilian defense and queen gambit chess openings alone")
        self._extra_transcript("2222222222222222", "other-c.md", classification="other", tags=["home"],
                               captured="2026-06-01T09:00:00-04:00", title="Volcano Documentary Dinner",
                               body_topic="egg rolls for dinner then a volcano documentary on television")
        test_md = self.vault / "z-test-note.md"
        test_md.write_text("---\ntitle: Chess Openings Practice\n---\nchess\n")
        with contextlib.closing(self.app()) as c:
            with c.transaction():
                store.apply_snapshot(c, {"seq": None, "item_id": None, "kind": "version", "idempotency_key": "",
                                         "md_path": str(test_md), "note_text": test_md.read_text(),
                                         "created_at": sentinel.utcnow(),
                                         "payload": {"producer_kind": "backfill", "produced_by": "backfill",
                                                     "item_is_wax": False}})
        self.sweep()
        with contextlib.closing(self.owner()) as c:
            c.execute("ANALYZE")
        e = self.embedder
        hybrid = search.run("phone quality time with the kids", embedder=e)
        self.assertEqual(hybrid["mode"], "hybrid")
        top = hybrid["results"][0]
        self.assertEqual(top["wax_item_id"], self.ITEM)
        self.assertEqual(top["version_no"], 4)
        self.assertEqual(top["version_id"], self.head()[1], "search returns the head version")
        self.assertTrue(top["snippet"])
        self.assertEqual({r["wax_item_id"] for r in search.run("chess", classifications=["monolog"],
                                                                 embedder=e)["results"]}, {"1111111111111111"})
        self.assertEqual({r["wax_item_id"] for r in search.run("dinner", tags=["home"], embedder=e)["results"]},
                         {"2222222222222222"})
        dated = search.run("memo", since="2026-09-01", until="2026-11-01", embedder=e)["results"]
        self.assertEqual({r["wax_item_id"] for r in dated}, {"1111111111111111"})
        self.assertEqual(search.run("memo", origins=["legacy"], embedder=e)["results"], [])
        exact = search.run("phone kids chess", origins=["wax"], limit=3, embedder=e)["results"]
        approx = search.run("phone kids chess", origins=["wax"], limit=3, exact_below=0, embedder=e)["results"]
        self.assertEqual([r["transcript_id"] for r in exact], [r["transcript_id"] for r in approx])
        lexical = search.run("Volcano Documentary", lexical=True)
        self.assertEqual(lexical["mode"], "lexical")
        self.assertEqual(lexical["results"][0]["wax_item_id"], "2222222222222222")
        self.assertIsNone(lexical["results"][0]["rank_chunk_vec"])
        titles = search.run("Chess Openings Practice", embedder=e)["results"]
        self.assertNotIn("test", {r["origin"] for r in titles}, "G.1: test notes never surface")
        for leg in ("rank_doc_lex", "rank_doc_vec"):
            self.assertTrue(any(r[leg] for r in titles))
        with contextlib.closing(self.app()) as c:
            with c.transaction():
                c.execute("SET LOCAL enable_seqscan = off")   # a 30-row table: prove the index is usable
                ok, names = health.ann_uses_hnsw(c)
                self.assertEqual(health.search_vector_path(c), "hnsw")
        self.assertTrue(ok, names)
        with contextlib.closing(self.app()) as c:
            # At ~30 rows and tx.search's ef_search=200 the planner prefers the exact scan (G.6).
            self.assertEqual(health.search_vector_path(c), "exact")
        shown = search.show(self.ITEM, version=1, raw=True)
        self.assertEqual(shown["version"]["version_no"], 1)
        self.assertTrue(shown["raw"].startswith("---\n"))
        hist = search.history(cls.md.name)
        self.assertEqual([v["version_no"] for v in hist["versions"]], [1, 2, 3, 4])
        self.assertEqual([v["body_changed"] for v in hist["versions"]], [True, False, False, True])
        self.assertTrue(any(r["outcome"] == "not_applied" for r in hist["pass_runs"]))
        with contextlib.closing(self.app()) as c:
            self.assertEqual(search.resolve(c, "spouses-disagreeing-over-phone"), cls.tid)

    # -------------------------------------------------------------- 11

    def test_11_reconcile_backfill(self):
        vault = Path(self.tmp.name) / "vault2"
        vault.mkdir()
        wax_item = "3333333333333333"
        notes = {
            "legacy-clip.md": "---\npipeline-status: new\n---\n# Transcription: clip_0059.mp3\n\n- **Source**: `/a/clip_0059.mp3`\n"
                              "- **Duration**: 2m 9s\n\n---\n\n**Speaker 1 (00:00):** the garage door weather report\n",
            "hermes.md": "---\nsource-audio: /tmp/hermes-local-stt-x/hermes-desktop-voice-1.wav\n---\n# Transcription: h.wav\n\n"
                         "remind me to buy milk and eggs tomorrow\n",
            "otter.md": "---\nsource-audio: /home/u/Music/Interview_otter_ai.mp3\ntags: [work]\n---\n"
                        "we discussed the interview loop and the system design round\n",
            "20261020-101010-wax-note.md": f"---\nwax-item-id: {wax_item}\nwax:\n  audio_duration_s: 60\n---\n"
                                           "# Transcription: 2026_10_20_10_10_10.mp3\n\nthe wax note body has words\n",
            "20261020-101010-wax-note 1.md": f"---\nwax-item-id: {wax_item}\nwax:\n  audio_duration_s: 60\n---\n"
                                             "# Transcription: 2026_10_20_10_10_10.mp3\n\nthe wax note body has words\n",
            "a-first.ogg.md": "---\nwax-item-id: 2d89c76e10c61b6d\n---\n\nbody\n",
            "braindump.md": "./BRAINDUMP.md -\n",
            "notes-on-chess.md": "---\ntitle: Chess Notes\n---\nlong list of chess openings to practice this week\n",
        }
        for name, text in notes.items():
            (vault / name).write_text(text)
        self.ledger_rows(wax_item, vault / "20261020-101010-wax-note.md")
        self.ledger_rows("4444444444444444", vault / "gone-forever.md")     # ledger-only: its note is missing
        from wax import events
        events._ensure()
        lconn = ledger.connect()
        cid = "00000000-0000-5000-8000-00000000aaaa"
        for subject in ("bloodbank.evt.audio.task.completed", "bloodbank.evt.v1.audio.task.completed"):
            env = {"type": "x", "time": "2026-10-20T10:20:00.000Z",
                   "data": {"item_id": wax_item, "ep_slug": "classification", "pass_version": 1, "attempt": 1,
                            "command_id": cid if "v1" not in subject else cid[:-4] + "bbbb",
                            "duration_s": 2.0, "changed_fields": ["frontmatter.classification"]}}
            lconn.execute("INSERT INTO outbox(subject, envelope, created_at) VALUES(?,?,?)",
                          (subject, json.dumps(env), sentinel.utcnow()))
        lconn.execute("INSERT INTO passes(item_id,ep_slug,version,state,attempt,command_id,updated_at,detail,result) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", (wax_item, "classification", 1, "completed", 1, cid,
                                                   "2026-10-20T10:20:00.000Z", "",
                                                   json.dumps({"evidence": {"model": "m1"}})))
        with patch.object(paths, "VAULT", vault):
            first = reconcile.sweep()
            self.assertEqual(first["errors"], [])
            self.assertEqual(first["backfill_versions"], 7)
            self.assertEqual(first["aliases"], 1)
            self.assertEqual(first["ledger_only_created"], 1)
            self.assertEqual(first["history_outbox_imported"], 2)
            origins = dict(self.all("SELECT origin, count(*) FROM tx.transcript t WHERE note_missing_at IS NULL "
                                    "GROUP BY origin"))
            self.assertEqual(origins, {"legacy": 3, "hermes-stt": 1, "wax": 1, "test": 2})
            versions = self.one("SELECT count(*) FROM tx.transcript_version")[0]
            again = reconcile.sweep()
            self.assertEqual((again["backfill_versions"], again["reconcile_versions"], again["identities_created"],
                              again["history_outbox_imported"], again["history_ledger_imported"]), (0, 0, 0, 0, 0))
            self.assertEqual(self.one("SELECT count(*) FROM tx.transcript_version")[0], versions)
            alias = self.one("SELECT role FROM tx.transcript_path WHERE note_relpath = %s",
                             ("20261020-101010-wax-note 1.md",))
            self.assertEqual(alias[0], "alias")
            run = self.one("SELECT source, evidence FROM tx.pass_run WHERE idempotency_key = %s", (f"pass:{cid}",))
            self.assertEqual(run, ("outbox", {"model": "m1"}))
            missing = self.one("SELECT note_missing_at IS NOT NULL, current_version_id FROM tx.transcript "
                               "WHERE wax_item_id = '4444444444444444'")
            self.assertEqual(missing, (True, None))

            edited = vault / "otter.md"
            time.sleep(0.01)
            edited.write_text(notes["otter.md"].replace("system design round", "system design round and pay"))
            third = reconcile.sweep()
            self.assertEqual(third["reconcile_versions"], 1)
            kinds = self.one("SELECT array_agg(producer_kind ORDER BY version_no) FROM tx.transcript_version v "
                             "JOIN tx.transcript t USING (transcript_id) WHERE t.note_relpath = 'otter.md'")[0]
            self.assertEqual(kinds, ["backfill", "reconcile"])

            (vault / "hermes.md").unlink()
            fourth = reconcile.sweep()
            self.assertGreaterEqual(fourth["vanished_marked"], 1)
            kept = self.one("SELECT note_missing_at IS NOT NULL, current_version_id IS NOT NULL FROM tx.transcript "
                            "WHERE note_relpath = 'hermes.md'")
            self.assertEqual(kept, (True, True))

            self.sweep()
            parity = reconcile.sweep(check=True)
            self.assertEqual(parity["aliases"], 1)
            self.assertEqual(parity["canonical_notes"], 6)
            self.assertEqual(parity["identities_with_note"], 6)
            self.assertEqual(parity["invariant_violations"], 0)
            self.assertFalse(parity["drift"], parity)

    # -------------------------------------------------------------- 12

    def test_12_outage_keeps_snapshots_pending_then_drains_in_order(self):
        cls = type(self)
        cid = STATE["container"]
        start_no = self.head()[3]
        self.assertEqual(_docker("pause", cid, timeout=30).returncode, 0)
        try:
            outbox.reset_breaker()
            cls.fm.update({"title": "Paused One"})
            self.pass_entry(cls.fm, "title-slug", version=4)
            write_note(cls.md, cls.fm, cls.body)
            started = time.time()
            self.record(cls.md, "title-slug", version=4, cid="00000000-0000-5000-8000-0000000ff001")
            self.assertLess(time.time() - started, 10, "connect_timeout bounds the hook")
            self.assertTrue(outbox.breaker_open())
            cls.fm.update({"classification": "other"})
            self.pass_entry(cls.fm, "classification", version=2)
            write_note(cls.md, cls.fm, cls.body)
            self.record(cls.md, "classification", version=2, cid="00000000-0000-5000-8000-0000000ff002")
            self.assertEqual(len(outbox.pending(self.ITEM)), 2)
            self.assertEqual(health.snapshot()["queued"], 2)
        finally:
            _docker("unpause", cid, timeout=30)
        deadline = time.time() + 30
        while True:
            try:
                drained = store.project_pending()
                break
            except db.StoreUnavailable:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        self.assertEqual(drained["projected"], 2)
        self.assertEqual(outbox.pending(self.ITEM), [])
        rows = self.all("SELECT version_no, produced_by FROM tx.transcript_version WHERE transcript_id = %s "
                        "AND version_no > %s ORDER BY version_no", (cls.tid, start_no))
        self.assertEqual(rows, [(start_no + 1, "title-slug"), (start_no + 2, "classification")])

    # -------------------------------------------------------------- 13

    def test_13_invariants_hold_and_probes_answer(self):
        self.assertEqual(self.all("SELECT * FROM tx.check_invariants()"), [])
        rows = health.probes(embedder=self.embedder)
        self.assertEqual(len(rows), 7)
        by_name = {r["probe"]: r for r in rows}
        self.assertEqual(by_name["transcripts store"]["status"], "pass")
        self.assertEqual(by_name["transcripts invariants"]["status"], "pass")
        self.assertEqual(by_name["transcripts embedder"]["status"], "pass")
        tick = transcriptions.tick(gpu_busy=lambda: True)
        self.assertEqual(tick["state"], "ok")
        self.assertEqual(tick["embed"]["deferred"], "gpu_busy")
        self.assertIs(tick["gpu_yielded"], False)       # it asked Ollama (dead port here) to drop the model
        self.assertEqual(health.snapshot()["state"], "ok")

    # -------------------------------------------------------------- review (2026-10-28, design G.7)

    def test_14_every_snapshot_statement_is_bounded_at_5s_even_on_a_60s_connection(self):
        with contextlib.closing(db.connect("app", statement_timeout_ms=60000, autocommit=True)) as c:
            self.assertEqual(c.execute("SHOW statement_timeout").fetchone()[0], "1min")
            with c.transaction():
                store.bound_statements(c)
                self.assertEqual(c.execute("SHOW statement_timeout").fetchone()[0], "5s")
            self.assertEqual(c.execute("SHOW statement_timeout").fetchone()[0], "1min")   # transaction-local

    def test_15_search_falls_back_to_lexical_on_a_digest_mismatch_and_caches_nothing(self):
        class Retagged(FakeEmbedder):
            def check_digest(self):
                raise embed.EmbedError("ollama serves another digest", "digest_mismatch")

        before = self.one("SELECT count(*) FROM tx.embedding_cache WHERE input_kind = 'query'")[0]
        out = transcriptions.search.run("a query nobody has embedded yet about volcanoes", embedder=Retagged())
        self.assertEqual(out["mode"], "lexical")
        self.assertTrue(out["degraded"].startswith("digest_mismatch"))
        self.assertEqual(self.one("SELECT count(*) FROM tx.embedding_cache WHERE input_kind = 'query'")[0], before)


if __name__ == "__main__":
    unittest.main()
