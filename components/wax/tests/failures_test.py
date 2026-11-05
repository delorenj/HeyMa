"""`wax failures`: the collector, grouping, the ticket dedupe and the px sweep.

Hermetic: a temporary ledger, a fixed pass registry, and a fake `px` (a tmp
executable that records argv and answers with canned JSON shaped like pilot's
`--json` output). Nothing here can reach a real board.
"""

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from wax import calls, events, failures, ledger, passes, paths
from wax.transcriptions import outbox as snapshots

COMPONENT = Path(__file__).resolve().parents[1]

REGISTRY = {
    "title-slug": {"slug": "title-slug", "enabled": True, "version": 3},
    "classification": {"slug": "classification", "enabled": True, "version": 2},
    "diarization": {"slug": "diarization", "enabled": True, "version": 1},
    "summarize": {"slug": "summarize", "enabled": False, "version": 1},
    "clipboard-copy": {"slug": "clipboard-copy", "enabled": True, "version": 1},
    "call-metadata": {"slug": "call-metadata", "enabled": True, "version": 1},
}

# A stand-in for pilot's px. State lives in FAKE_PX_DIR: markers -> refs (so a
# repeated marker is ADOPTED, as px does), each ticket's state group, comments.
# `mode` switches failure shapes: fail (ok:false, exit 1), hang (never answers),
# lost (creates the ticket on the "board", then never answers).
FAKE_PX = r'''#!{python}
import json, os, sys, time
d = os.environ["FAKE_PX_DIR"]
state_path = os.path.join(d, "state.json")
state = json.load(open(state_path)) if os.path.exists(state_path) else {"seq": 0, "markers": {}, "groups": {}, "comments": []}
with open(os.path.join(d, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
mode_path = os.path.join(d, "mode")
mode = open(mode_path).read().strip() if os.path.exists(mode_path) else "ok"
def save():
    json.dump(state, open(state_path, "w"))
def answer(doc, code=0):
    save()
    print(json.dumps(doc, indent=2))
    sys.exit(code)
args = sys.argv[1:]
if mode == "hang":
    time.sleep(20)
if mode == "fail":
    sys.stderr.write("pilot: plane is down\n")
    answer({"ok": False, "error": "plane is down"}, 1)
if args[:2] == ["task", "create"]:
    mark = args[args.index("--marker") + 1]
    if mark in state["markers"]:
        ref = state["markers"][mark]
        ticket = {"id": "id-" + ref, "ref": ref, "created": False, "adopted": True, "marker": "[" + mark + "]"}
        answer({"ok": True, "created": ticket, "ref": ref, "id": "id-" + ref, "adopted": True, "was_created": False})
    state["seq"] += 1
    ref = "HEYMA-%d" % (100 + state["seq"])
    state["markers"][mark] = ref
    state["groups"][ref] = "unstarted"
    if mode == "lost":
        save()
        time.sleep(20)
    ticket = {"id": "id-" + ref, "sequence_id": 100 + state["seq"], "name": args[2], "state": "Todo",
              "priority": "urgent", "url": "https://plane.example/" + ref, "ref": ref,
              "marker": "[" + mark + "]", "created": True, "adopted": False}
    answer({"ok": True, "created": ticket, "ref": ref, "id": "id-" + ref, "adopted": False, "was_created": True})
if args[0] == "show":
    ref = args[1]
    answer({"ok": True, "ticket": {"ref": ref, "state_group": state["groups"].get(ref, "unstarted"),
                                   "state_name": "Todo"}})
if args[0] == "comment":
    state["comments"].append({"ref": args[1], "message": args[args.index("-m") + 1]})
    answer({"ok": True, "commented": {"ref": args[1], "comment_id": "c1"}})
answer({"ok": False, "error": "unknown command"}, 1)
'''


def ago(seconds: float) -> str:
    return failures._iso(time.time() - seconds)


class _Ledger(unittest.TestCase):
    """A temp runtime root with a fixed registry; helpers write raw ledger rows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        for name, value in {"VAR": self.root / "var", "DB": self.root / "var/wax.db",
                            "INBOX": self.root / "inbox", "ARCHIVE": self.root / "archive",
                            "VAULT": self.root / "vault", "HEYMA": self.root, "AUDIO": self.root}.items():
            self.stack.enter_context(patch.object(paths, name, value))
        for directory in (paths.VAR, paths.INBOX, paths.ARCHIVE, paths.VAULT):
            directory.mkdir(parents=True)
        self.stack.enter_context(patch.object(passes, "registry", side_effect=lambda: dict(REGISTRY)))
        self._close()
        self.conn = ledger.connect()
        self.px_dir = self.root / "px"
        self.px_dir.mkdir()
        self.px_exe = self.px_dir / "px"
        self.px_exe.write_text(FAKE_PX.replace("{python}", sys.executable))
        self.px_exe.chmod(self.px_exe.stat().st_mode | stat.S_IXUSR)
        self.stack.enter_context(patch.dict(os.environ, {"FAKE_PX_DIR": str(self.px_dir),
                                                         "WAX_PX": str(self.px_exe)}))

    def _close(self):
        conn = getattr(ledger._local, "conn", None)
        if conn is not None:
            conn.close()
            del ledger._local.conn

    def tearDown(self):
        self._close()
        self.stack.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------ fake px --
    def px(self, timeout: float = 10.0) -> failures.Px:
        return failures.Px(exe=self.px_exe, cwd=self.root, timeout=timeout)

    def px_mode(self, mode: str) -> None:
        (self.px_dir / "mode").write_text(mode)

    def px_calls(self) -> list[dict]:
        log = self.px_dir / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def px_state(self) -> dict:
        return json.loads((self.px_dir / "state.json").read_text())

    def close_ticket(self, ref: str, group: str = "completed") -> None:
        state = self.px_state()
        state["groups"][ref] = group
        (self.px_dir / "state.json").write_text(json.dumps(state))

    # ------------------------------------------------------------- writers --
    def item(self, item_id, state="complete", name=None, path=None, updated=None):
        self.conn.execute(
            "INSERT INTO items(item_id,sha256,path,orig_name,state,first_seen,updated_at) VALUES(?,?,?,?,?,?,?)",
            (item_id, item_id * 4, str(path or paths.ARCHIVE / f"{item_id}.ogg"), name or f"{item_id}.ogg",
             state, updated or ago(86400), updated or ago(86400)))

    def transition(self, machine, subject, to_state, cause, at, evidence=""):
        cur = self.conn.execute(
            "INSERT INTO transitions(machine,subject,from_state,to_state,cause_code,evidence,at,generation) "
            "VALUES(?,?,?,?,?,?,?,0)", (machine, subject, None, to_state, cause, evidence or None, at))
        return cur.lastrowid

    def pass_row(self, key, slug, *, reason="provider_bad_response", version=None, attempt=1,
                 updated=None, exhausted=None, state="failed", detail="no model in the chain answered"):
        self.conn.execute(
            "INSERT INTO passes(item_id,ep_slug,version,state,attempt,updated_at,detail,reason_code,exhausted_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (key, slug, version or REGISTRY[slug]["version"], state, attempt, updated or ago(7200), detail,
             reason, exhausted))

    def document(self, binding, doc_id, path, state="failed", first_seen=None):
        self.conn.execute(
            "INSERT INTO documents(binding_id,doc_id,path,sha256,state,first_seen,updated_at) VALUES(?,?,?,?,?,?,?)",
            (binding, doc_id, str(path), doc_id * 2, state, first_seen or ago(86400), ago(3600)))

    def receipt(self, binding, doc_id, pipeline="doc-standard", *, state="failed", reason="run_error",
                attempts=3, last=None, exhausted=None, error="RuntimeError: boom"):
        self.conn.execute(
            "INSERT INTO document_receipts(binding_id,doc_id,pipeline,definition_id,state,attempts,"
            "last_attempt_at,exhausted_at,last_error,reason_code) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (binding, doc_id, pipeline, "d" * 64, state, attempts, last or ago(7200), exhausted, error, reason))

    def outbox_row(self, created, *, published=None, suppressed=None, error=None):
        events._ensure()
        cur = self.conn.execute(
            "INSERT INTO outbox(subject,envelope,created_at,published_at,attempts,last_error,suppressed_reason) "
            "VALUES(?,?,?,?,?,?,?)", ("bloodbank.evt.audio.file.recorded", "{}", created, published,
                                      1 if error else 0, error, suppressed))
        return cur.lastrowid

    def snapshot(self, key, created, *, projected=None, error=None, attempts=0):
        snapshots._ensure()
        cur = self.conn.execute(
            "INSERT INTO transcript_snapshots(item_id,kind,idempotency_key,md_path,note_text,payload,created_at,"
            "projected_at,attempts,last_error) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("aaaa000000000001", "version", key, None, "note body must never reach a ticket", "{}", created,
             projected, attempts, error))
        return cur.lastrowid

    # --------------------------------------------------------- the world --
    def seed_world(self):
        """One row of every kind, plus one row of every exclusion."""
        # pass: exhausted (final) + failing past the grace, same signature; one still young
        for key in ("aaaa000000000001", "aaaa000000000002", "aaaa000000000003"):
            self.item(key)
        self.pass_row("aaaa000000000001", "title-slug", attempt=3, exhausted=ago(9000), updated=ago(7200))
        self.pass_row("aaaa000000000002", "title-slug", attempt=1, updated=ago(5400))
        self.pass_row("aaaa000000000003", "title-slug", attempt=1, updated=ago(300))
        # exclusions: dependency_failed, registry-disabled, old version, operator-disabled, too old
        self.pass_row("aaaa000000000001", "diarization", reason="dependency_failed")
        self.pass_row("aaaa000000000002", "summarize")
        self.pass_row("aaaa000000000002", "classification", version=1, reason="empty_document")
        self.pass_row("aaaa000000000003", "clipboard-copy", reason="clipboard_unavailable")
        (paths.VAR / "settings.json").write_text(json.dumps({"passes": {"disabled": ["clipboard-copy"]}}))
        self.pass_row("aaaa000000000003", "call-metadata", reason="call_not_found", updated=ago(10 * 86400))
        # documents: a vanished doc's pass, a superseded doc's pass, and a pipeline-level receipt
        self.document("b1", "doc-1111111111111111", self.root / "vault/Untitled.md", state="gone")
        self.pass_row("doc-1111111111111111@b1", "classification", reason="empty_document")
        self.document("b1", "doc-2222222222222222", self.root / "vault/out.md", state="failed",
                      first_seen=ago(86400))
        self.document("b1", "doc-3333333333333333", self.root / "vault/out.md", state="processed",
                      first_seen=ago(43200))
        self.pass_row("doc-2222222222222222@b1", "title-slug", reason="superseded_failure")
        self.document("b1", "doc-8888888888888888", self.root / "vault/live.md", state="failed")
        self.pass_row("doc-8888888888888888@b1", "title-slug", reason="provider_http_error")
        self.document("b1", "doc-4444444444444444", self.root / "vault/crash.md")
        self.receipt("b1", "doc-4444444444444444", exhausted=ago(7200))
        # receipts that must NOT surface: blamed on a failed pass, processed, gone
        self.document("b1", "doc-5555555555555555", self.root / "vault/blamed.md")
        self.receipt("b1", "doc-5555555555555555", reason="provider_bad_response")
        self.pass_row("doc-5555555555555555@b1", "title-slug")
        self.document("b1", "doc-6666666666666666", self.root / "vault/ok.md", state="processed")
        self.receipt("b1", "doc-6666666666666666")
        self.document("b1", "doc-7777777777777777", self.root / "vault/gone.md", state="gone")
        self.receipt("b1", "doc-7777777777777777")
        # items: failed + suspect; one failed item outside the window
        self.item("bbbb000000000001", state="failed", name="rec-1.ogg", path=paths.INBOX / "rec-1.ogg")
        (paths.INBOX / "rec-1.ogg").write_bytes(b"audio")
        self.transition("item", "bbbb000000000001", "failed", "transcribe_failed", ago(10800),
                        "transcribe exited 143; log retained: /tmp/var/logs/bbbb000000000001/transcription.1.log")
        self.item("bbbb000000000002", state="suspect", name="rec-2.ogg")
        self.transition("item", "bbbb000000000002", "suspect", "duration_mismatch", ago(10000), "asr 3s of 600s")
        self.item("bbbb000000000003", state="failed", name="old.ogg")
        self.transition("item", "bbbb000000000003", "failed", "archive_failed", ago(20 * 86400), "old")
        # recordings: terminal fetch_error; voicemail_empty and CALL_REJECTED are by design
        conn = calls.db()
        for key, err in (("rec-bad", "recording_bad_name"), ("vm:empty", "voicemail_empty")):
            conn.execute("INSERT INTO call_recordings(recording_key,kind,call_session_id,fetch_error,raw,updated_at) "
                         "VALUES(?,?,?,?,?,?)", (key, "trunk", "sess-1", err, "{}", ago(7200)))
        conn.execute("INSERT INTO calls(call_session_id,outcome,hangup_cause,started_at,first_seen,updated_at) "
                     "VALUES(?,?,?,?,?,?)", ("sess-rejected", "failed", "CALL_REJECTED", ago(7200), ago(7200),
                                             ago(7200)))
        conn.execute("INSERT INTO call_events(event_id,event_type,occurred_at,call_session_id,payload,via,"
                     "received_at) VALUES(?,?,?,?,?,?,?)",
                     ("ev-vm-1", "calls.voicemail.completed", ago(7200), "sess-vm",
                      json.dumps({"recording_url": "https://telnyx.example/vm.wav"}), "webhook", ago(7200)))
        # outbox: stale -> filed; fresh, suppressed and published -> not
        self.stale_outbox = self.outbox_row(ago(1200), error="connect: nats down")
        self.outbox_row(ago(60))
        self.outbox_row(ago(7200), suppressed="legacy_premature_completion")
        self.outbox_row(ago(7200), published=ago(7100))
        # snapshots: rejected (final), failing past the grace, and a healed one
        self.snapshot("k1", ago(7200), projected=ago(7000), error="rejected: no frontmatter")
        self.snapshot("k2", ago(7200), error="OperationalError: connection refused", attempts=4)
        self.snapshot("k3", ago(7200), projected=ago(7000), error="OperationalError: transient")
        # hygiene: the empty sweep errored; the untitled one did not
        ledger.meta_set("hygiene_empty_sweep", json.dumps({"kind": "empty", "last_run_at": ago(600),
                                                           "last_error": "PermissionError: [Errno 13] vault",
                                                           "folders": ["/vault/Inbox"]}))
        ledger.meta_set("hygiene_untitled_sweep", json.dumps({"kind": "untitled", "last_run_at": ago(600),
                                                              "last_error": None}))
        # worker: a crash; a stranded gap that healed in 10 s; one that lasted two hours
        self.crash = self.transition("inbox", None, "error", "worker_exception", ago(5000), "KeyError: 'x'")
        self.transition("inbox", None, "error", "stranded_work", ago(4800), "1 item(s) in inbox")
        self.transition("inbox", None, "ready-and-active", None, ago(4790))
        self.long_stranding = self.transition("inbox", None, "error", "stranded_work", ago(4700), "2 item(s)")
        self.transition("inbox", None, "ready-and-active", None, ago(4700 - 7300))
        self.transition("inbox", None, "ready-and-waiting", None, ago(60))


class CollectorTest(_Ledger):
    def test_every_kind_is_collected_and_every_exclusion_holds(self):
        self.seed_world()
        errors = []
        rows = failures.collect(errors=errors)
        self.assertEqual(errors, [])
        got = {(r["kind"], r["slug"], r["reason_code"], r["subject"]): r for r in rows}
        keys = {k[:3] for k in got}

        # every kind
        self.assertIn(("pass", "title-slug", "provider_bad_response"), keys)
        self.assertIn(("pass", "title-slug", "provider_http_error"), keys)       # a document's pass row
        self.assertIn(("document", "doc-standard", "run_error"), keys)
        self.assertIn(("item", "failed", "transcribe_failed"), keys)
        self.assertIn(("item", "suspect", "duration_mismatch"), keys)
        self.assertIn(("recording", "trunk", "recording_bad_name"), keys)
        self.assertIn(("recording", "voicemail", "voicemail_unfetched"), keys)
        self.assertIn(("outbox", "bloodbank", "outbox_stale"), keys)
        self.assertIn(("snapshot", "transcriptions", "snapshot_rejected"), keys)
        self.assertIn(("snapshot", "transcriptions", "projection_failed"), keys)
        self.assertIn(("hygiene", "empty", "sweep_error"), keys)
        self.assertIn(("worker", "inbox", "worker_exception"), keys)
        self.assertIn(("worker", "inbox", "stranded_work"), keys)

        # every exclusion
        reasons = {r["reason_code"] for r in rows}
        self.assertNotIn("dependency_failed", reasons)
        self.assertNotIn("voicemail_empty", reasons)
        self.assertNotIn("CALL_REJECTED", reasons)
        self.assertFalse(any("sess-rejected" in json.dumps(r) for r in rows))
        slugs = {r["slug"] for r in rows if r["kind"] == "pass"}
        self.assertNotIn("summarize", slugs)          # disabled in the registry
        self.assertNotIn("clipboard-copy", slugs)     # switched off by the operator
        self.assertNotIn("classification", slugs)     # v1 failure under a v2 registry; the gone doc's too
        self.assertNotIn("call-metadata", slugs)      # outside --since
        subjects = {r["subject"] for r in rows}
        self.assertNotIn("doc-1111111111111111@b1/classification", subjects)   # vanished document
        self.assertNotIn("doc-2222222222222222@b1/title-slug", subjects)        # superseded document
        self.assertNotIn("superseded_failure", reasons)
        for doc in ("5555", "6666", "7777"):                                   # blamed, processed, gone
            self.assertFalse(any(r["kind"] == "document" and doc * 4 in r["subject"] for r in rows), doc)
        self.assertNotIn("bbbb000000000003", subjects)                         # outside --since
        outbox = [r for r in rows if r["kind"] == "outbox"]
        self.assertEqual([r["subject"] for r in outbox], [str(self.stale_outbox)])
        self.assertEqual(len([r for r in rows if r["kind"] == "snapshot"]), 2)
        self.assertFalse(any(r["kind"] == "hygiene" and r["slug"] == "untitled" for r in rows))
        stranded = [r for r in rows if r["reason_code"] == "stranded_work"]
        self.assertEqual([r["subject"] for r in stranded], [str(self.long_stranding)])
        self.assertTrue(stranded[0]["final"])

    def test_ticketable_is_final_or_past_the_grace(self):
        self.seed_world()
        rows = {r["subject"]: r for r in failures.collect() if r["kind"] == "pass"}
        exhausted = rows["aaaa000000000001/title-slug"]
        self.assertTrue(exhausted["final"] and exhausted["ticketable"])
        old = rows["aaaa000000000002/title-slug"]
        self.assertFalse(old["final"])
        self.assertTrue(old["ticketable"])
        young = rows["aaaa000000000003/title-slug"]
        self.assertFalse(young["ticketable"])
        # a longer grace keeps the non-final one waiting too
        rows = {r["subject"]: r for r in failures.collect(grace_s=6 * 3600) if r["kind"] == "pass"}
        self.assertFalse(rows["aaaa000000000002/title-slug"]["ticketable"])
        self.assertTrue(rows["aaaa000000000001/title-slug"]["ticketable"])

    def test_an_ongoing_stranding_counts_only_with_actionable_work(self):
        self.transition("inbox", None, "error", "stranded_work", ago(7200), "1 item(s) in inbox")
        self.item("cccc000000000001", state="failed", path=paths.INBOX / "failed.ogg")
        (paths.INBOX / "failed.ogg").write_bytes(b"x")
        self.assertEqual([r for r in failures.collect() if r["kind"] == "worker"], [])
        self.item("cccc000000000002", state="pending", path=paths.INBOX / "queued.ogg")
        (paths.INBOX / "queued.ogg").write_bytes(b"y")
        rows = [r for r in failures.collect() if r["kind"] == "worker"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["final"])
        self.assertTrue(rows[0]["ticketable"])

    def test_a_broken_registry_is_a_collector_error_not_silence(self):
        self.item("aaaa000000000001")
        self.pass_row("aaaa000000000001", "title-slug", exhausted=ago(60))
        self.item("bbbb000000000001", state="failed")
        self.transition("item", "bbbb000000000001", "failed", "transcribe_failed", ago(600))
        with patch.object(passes, "registry", side_effect=OSError("passes.d unreadable")):
            errors = []
            rows = failures.collect(errors=errors)
            self.assertTrue(errors and errors[0].startswith("pass: OSError"))
            self.assertEqual({r["kind"] for r in rows}, {"item"})      # the other kinds still report
            with self.assertRaises(OSError):
                failures.collect()

    def test_details_are_one_scrubbed_line(self):
        fake = "sk" + "-" + "abcdefghijklmnop" + "0123"      # assembled: not a secret-shaped literal
        self.item("aaaa000000000001")
        self.pass_row("aaaa000000000001", "title-slug", exhausted=ago(60),
                      detail=f"HTTP 401 Authorization: Bearer {fake} rejected\nTraceback ...\n"
                             "  secret tail line")
        (row,) = [r for r in failures.collect() if r["kind"] == "pass"]
        self.assertNotIn(fake, row["detail"])
        self.assertNotIn("Traceback", row["detail"])
        self.assertIn("HTTP 401", row["detail"])
        self.assertLessEqual(len(row["detail"]), failures.DETAIL_CHARS)


class GroupingTest(_Ledger):
    def test_one_signature_per_kind_slug_reason_newest_first(self):
        self.seed_world()
        sigs = failures.group(failures.collect())
        keys = [(s["kind"], s["slug"], s["reason_code"]) for s in sigs]
        self.assertEqual(len(keys), len(set(keys)))
        title_slug = next(s for s in sigs if s["slug"] == "title-slug" and s["reason_code"] == "provider_bad_response")
        # the young row is not ticket-worthy yet; the blamed document's pass joins the same signature
        self.assertEqual([r["subject"] for r in title_slug["subjects"]],
                         ["aaaa000000000002/title-slug", "aaaa000000000001/title-slug",
                          "doc-5555555555555555@b1/title-slug"])
        self.assertEqual(title_slug["affected"], 3)
        self.assertEqual(title_slug["final"], 1)
        self.assertEqual(title_slug["first_seen"][:16], ago(9000)[:16])    # the exhaustion, not the last try
        stamps = [failures._epoch(s["last_seen"]) for s in sigs]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_fingerprint_is_stable(self):
        self.assertEqual(failures.fingerprint("pass", "title-slug", "provider_bad_response"),
                         failures.fingerprint("pass", "title-slug", "provider_bad_response"))
        self.assertEqual(failures.fingerprint("pass", "title-slug", "provider_bad_response"),
                         __import__("hashlib").sha256(b"pass|title-slug|provider_bad_response").hexdigest()[:12])
        self.assertNotEqual(failures.fingerprint("pass", "title-slug", "x"), failures.fingerprint("pass", "title-slu", "gx"))
        self.assertEqual(failures.marker("abc123def456", 2), "wax-failure:abc123def456.2")

    def test_ticket_body_is_pointer_rich_and_capped(self):
        for n in range(25):
            key = f"dddd{n:012d}"
            self.item(key, name=f"rec-{n}.ogg")
            self.pass_row(key, "title-slug", exhausted=ago(600 + n), updated=ago(600 + n))
        (sig,) = failures.group(failures.collect())
        text = failures.body(sig, generation=1, marker_text=failures.marker(sig["fp"], 1))
        title = failures.title(sig)
        self.assertEqual(title, "[wax] pass title-slug: provider_bad_response (25 affected)")
        self.assertLess(len(title), 120)
        self.assertIn("heyma-pipeline-doctor", text)
        self.assertIn("no LLM", text)
        self.assertIn("## Acceptance criteria", text)
        self.assertIn("`wax ep status dddd000000000000`", text)
        self.assertIn("wax ep run title-slug", text)
        self.assertIn("journalctl --user -u waxd.service --since", text)
        self.assertIn("... and 5 more not shown", text)
        self.assertEqual(text.count("\n20. "), 1)
        self.assertNotIn("\n21. ", text)
        self.assertTrue(text.endswith(f"\n[wax-failure:{sig['fp']}.1]"))
        self.assertEqual(text.count("[wax-failure:"), 1)

    def test_a_long_title_stays_under_120(self):
        sig = {"kind": "document", "slug": "x" * 90, "reason_code": "y" * 60, "affected": 3}
        self.assertLess(len(failures.title(sig)), 120)
        self.assertTrue(failures.title(sig).endswith("(3 affected)"))


class SweepTest(_Ledger):
    def _two_signatures(self):
        self.item("aaaa000000000001")
        self.item("aaaa000000000002")
        self.pass_row("aaaa000000000001", "title-slug", exhausted=ago(7000), updated=ago(7000))
        self.item("bbbb000000000001", state="failed", name="rec-1.ogg")
        self.transition("item", "bbbb000000000001", "failed", "transcribe_failed", ago(3000))

    def test_files_once_then_makes_zero_px_calls(self):
        self._two_signatures()
        first = failures.sweep(px=self.px())
        self.assertEqual(first["px_errors"], 0)
        self.assertEqual(first["counts"], {"created": 2})
        creates = [c["argv"] for c in self.px_calls()]
        self.assertEqual(len(creates), 2)
        argv = creates[0]
        self.assertEqual(argv[:3], ["task", "create", argv[2]])
        self.assertTrue(argv[2].startswith("[wax] "))
        for flag, value in (("--state", "Todo"), ("--priority", "urgent"), ("-l", "bug,wax-failure")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--json", argv)
        mark = argv[argv.index("--marker") + 1]
        self.assertRegex(mark, r"^wax-failure:[0-9a-f]{12}\.1$")
        self.assertIn(f"[{mark}]", argv[argv.index("-d") + 1])
        self.assertEqual(self.px_calls()[0]["cwd"], str(self.root))
        stored = failures.tickets()
        self.assertEqual({r["status"] for r in stored.values()}, {"filed"})
        self.assertEqual(sorted(r["ticket_ref"] for r in stored.values()), ["HEYMA-101", "HEYMA-102"])

        second = failures.sweep(px=self.px())
        self.assertEqual(second["counts"], {"unchanged": 2})
        self.assertEqual(len(self.px_calls()), 2)          # no px call at all on the second run
        self.assertEqual(second["px_calls"], 0)

    def test_a_new_subject_is_a_comment_and_is_then_known(self):
        self._two_signatures()
        failures.sweep(px=self.px())
        self.pass_row("aaaa000000000002", "title-slug", exhausted=ago(100), updated=ago(100))
        res = failures.sweep(px=self.px())
        self.assertEqual(res["counts"], {"commented": 1, "unchanged": 1})
        verbs = [c["argv"][0] for c in self.px_calls()[2:]]
        self.assertEqual(verbs, ["show", "comment"])
        (comment,) = self.px_state()["comments"]
        self.assertTrue(comment["message"].startswith("1 more affected"))
        self.assertIn("aaaa000000000002", comment["message"])
        self.assertNotIn("[wax-failure:", comment["message"])
        fp = failures.fingerprint("pass", "title-slug", "provider_bad_response")
        self.assertEqual(sorted(failures.tickets()[fp]["subjects"]),
                         ["aaaa000000000001/title-slug", "aaaa000000000002/title-slug"])
        before = len(self.px_calls())
        failures.sweep(px=self.px())
        self.assertEqual(len(self.px_calls()), before)

    def test_a_closed_ticket_with_new_subjects_files_a_new_generation(self):
        self._two_signatures()
        failures.sweep(px=self.px())
        fp = failures.fingerprint("pass", "title-slug", "provider_bad_response")
        old_ref = failures.tickets()[fp]["ticket_ref"]
        self.close_ticket(old_ref, "completed")
        self.pass_row("aaaa000000000002", "title-slug", exhausted=ago(100), updated=ago(100))
        res = failures.sweep(px=self.px())
        rec = next(r for r in res["signatures"] if r["fp"] == fp)
        self.assertEqual(rec["action"], "refiled")
        self.assertEqual(rec["generation"], 2)
        row = failures.tickets()[fp]
        self.assertEqual((row["generation"], row["status"]), (2, "filed"))
        self.assertNotEqual(row["ticket_ref"], old_ref)
        create = self.px_calls()[-1]["argv"]
        self.assertEqual(create[create.index("--marker") + 1], f"wax-failure:{fp}.2")
        body = create[create.index("-d") + 1]
        self.assertIn(f"Regression: {old_ref} was closed", body)
        self.assertIn("aaaa000000000001", body)        # the new generation covers every current subject
        self.assertIn("aaaa000000000002", body)

    def test_a_cancelled_ticket_regresses_too(self):
        self._two_signatures()
        failures.sweep(px=self.px())
        fp = failures.fingerprint("item", "failed", "transcribe_failed")
        self.close_ticket(failures.tickets()[fp]["ticket_ref"], "cancelled")
        self.item("bbbb000000000002", state="failed")
        self.transition("item", "bbbb000000000002", "failed", "transcribe_failed", ago(100))
        res = failures.sweep(px=self.px())
        self.assertEqual(next(r for r in res["signatures"] if r["fp"] == fp)["action"], "refiled")

    def test_px_failure_leaves_uncertain_then_the_next_run_adopts(self):
        self._two_signatures()
        self.px_mode("lost")              # the ticket lands on the board, the answer never comes back
        res = failures.sweep(px=self.px(timeout=1.5))
        self.assertEqual(res["px_errors"], 2)
        self.assertEqual({r["action"] for r in res["signatures"]}, {"error"})
        self.assertTrue(all("timed out" in r["error"] for r in res["signatures"]))
        stored = failures.tickets()
        self.assertEqual({r["status"] for r in stored.values()}, {"uncertain"})
        self.assertTrue(all(r["last_error"] for r in stored.values()))
        markers_on_board = dict(self.px_state()["markers"])
        self.assertEqual(len(markers_on_board), 2)

        self.px_mode("ok")
        res = failures.sweep(px=self.px())
        self.assertEqual(res["counts"], {"adopted": 2})
        self.assertEqual(len(self.px_state()["markers"]), 2)    # no twin was filed
        stored = failures.tickets()
        self.assertEqual({r["status"] for r in stored.values()}, {"filed"})
        self.assertEqual(sorted(r["ticket_ref"] for r in stored.values()), sorted(markers_on_board.values()))
        self.assertTrue(all(r["last_error"] is None for r in stored.values()))

    def test_px_refusal_is_recorded_and_the_run_continues(self):
        self._two_signatures()
        self.px_mode("fail")
        res = failures.sweep(px=self.px())
        self.assertEqual(res["px_errors"], 2)
        self.assertTrue(all("plane is down" in r["error"] for r in res["signatures"]))
        self.assertEqual({r["status"] for r in failures.tickets().values()}, {"uncertain"})
        self.px_mode("ok")
        res = failures.sweep(px=self.px())
        self.assertEqual(res["counts"], {"created": 2})

    def test_a_missing_or_relative_px_is_an_error_not_a_path_lookup(self):
        self._two_signatures()
        with patch.dict(os.environ, {"WAX_PX": "px"}):
            res = failures.sweep()
        self.assertEqual(res["px_errors"], 2)
        self.assertIn("absolute", res["signatures"][0]["error"])
        res = failures.sweep(px=failures.Px(exe=self.root / "missing-px", cwd=self.root))
        self.assertIn("not an executable", res["signatures"][0]["error"])

    def test_the_cap_is_logged_and_the_rest_file_next_run(self):
        for n in range(4):
            self.item(f"eeee{n:012d}", state="failed")
            self.transition("item", f"eeee{n:012d}", "failed", f"cause_{n}", ago(1000 + n))
        with self.assertLogs("wax.failures", level="WARNING") as logs:
            res = failures.sweep(px=self.px(), max_new=3)
        self.assertEqual(res["counts"], {"created": 3, "capped": 1})
        self.assertEqual([c["reason_code"] for c in res["capped"]], ["cause_3"])   # newest first: oldest waits
        self.assertTrue(any("--max 3 reached" in line for line in logs.output))
        self.assertIn("--max 3 reached", failures.render_sweep(res))
        res = failures.sweep(px=self.px(), max_new=3)
        self.assertEqual(res["counts"], {"created": 1, "unchanged": 3})

    def test_dry_run_writes_nothing_and_calls_no_px(self):
        self._two_signatures()
        res = failures.sweep(px=self.px(), dry_run=True)
        self.assertEqual(res["counts"], {"would_create": 2})
        self.assertEqual(self.px_calls(), [])
        self.assertNotIn("failure_tickets", failures._tables(self.conn))
        self.assertTrue(all(r["title"] and r["body"] for r in res["signatures"]))
        text = failures.render_sweep(res)
        self.assertIn("[wax-failure:", text)
        self.assertIn("## Acceptance criteria", text)
        # with tickets on record, a dry run still neither calls px nor writes
        failures.sweep(px=self.px())
        self.pass_row("aaaa000000000002", "title-slug", exhausted=ago(10), updated=ago(10))
        calls_before, rows_before = len(self.px_calls()), failures.tickets()
        res = failures.sweep(px=self.px(), dry_run=True)
        self.assertEqual(len(self.px_calls()), calls_before)
        self.assertEqual(failures.tickets(), rows_before)
        self.assertIn("would_check", res["counts"])

    def test_report_shows_what_is_ticketed_and_never_calls_px(self):
        self._two_signatures()
        rep = failures.report()
        self.assertEqual([s["ticket"] for s in rep["signatures"]], [None, None])
        failures.sweep(px=self.px())
        self.pass_row("aaaa000000000002", "title-slug", exhausted=ago(10), updated=ago(10))
        calls_before = len(self.px_calls())
        rep = failures.report()
        self.assertEqual(len(self.px_calls()), calls_before)
        tickets = {s["slug"]: s["ticket"] for s in rep["signatures"]}
        self.assertEqual(tickets["title-slug"]["untracked"], 1)
        self.assertEqual(tickets["failed"]["untracked"], 0)
        self.assertIn("filed +1 new", failures.render_list(rep))

    def test_created_from_reads_both_shapes(self):
        nested = {"ok": True, "created": {"ref": "HEYMA-9", "id": "u9", "created": True, "adopted": False}}
        self.assertEqual(failures.created_from(nested)["ref"], "HEYMA-9")
        self.assertTrue(failures.created_from(nested)["created"])
        flat = {"ok": True, "ref": "HEYMA-8", "id": "u8", "created": False, "adopted": True}
        self.assertEqual(failures.created_from(flat), {"ref": "HEYMA-8", "id": "u8", "created": False,
                                                       "adopted": True, "url": None})
        with self.assertRaises(failures.PxError):
            failures.created_from({"ok": True})
        self.assertEqual(failures._parse_json('warning: x\n{\n  "ok": true\n}\n'), {"ok": True})

    def test_parse_duration(self):
        self.assertEqual(failures.parse_duration("30m"), 1800)
        self.assertEqual(failures.parse_duration("1h"), 3600)
        self.assertEqual(failures.parse_duration("7d"), 7 * 86400)
        self.assertEqual(failures.parse_duration("2w"), 14 * 86400)
        self.assertEqual(failures.parse_duration("90"), 90)
        with self.assertRaises(ValueError):
            failures.parse_duration("soon")


def _load_wax():
    loader = SourceFileLoader("wax_cli_failures", str(COMPONENT / "bin" / "wax"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FailuresCliTest(_Ledger):
    @classmethod
    def setUpClass(cls):
        cls.wax = _load_wax()

    def main(self, *argv):
        out = io.StringIO()
        with patch.object(sys, "argv", ["wax", *argv]), redirect_stdout(out), \
                patch.object(failures, "repo_root", return_value=self.root):
            code = self.wax.main()
        return code, out.getvalue()

    def seed(self):
        self.item("aaaa000000000001")
        self.pass_row("aaaa000000000001", "title-slug", exhausted=ago(7000), updated=ago(7000))

    def test_list_is_the_default_and_reads_only(self):
        self.seed()
        code, out = self.main("failures")
        self.assertEqual(code, 0)
        self.assertIn("not filed", out)
        self.assertIn("pass title-slug: provider_bad_response", out)
        code, out = self.main("failures", "list", "--json", "--since", "2w", "--grace", "30m")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["since_s"], 14 * 86400)
        self.assertEqual(doc["grace_s"], 1800)
        self.assertEqual(doc["signatures"][0]["slug"], "title-slug")
        self.assertEqual(self.px_calls(), [])
        self.assertNotIn("failure_tickets", failures._tables(self.conn))

    def test_since_applies_before_and_after_the_verb(self):
        self.seed()
        _, out = self.main("--json", "failures", "--since", "30m")
        self.assertEqual(json.loads(out)["signatures"], [])
        _, out = self.main("failures", "sweep", "--since", "30m", "--dry-run", "--json")
        self.assertEqual(json.loads(out)["signatures"], [])

    def test_sweep_dry_run_then_sweep_through_the_fake_px(self):
        self.seed()
        code, out = self.main("failures", "sweep", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would_create", out)
        self.assertIn("[wax-failure:", out)
        self.assertEqual(self.px_calls(), [])
        code, out = self.main("failures", "sweep", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["counts"], {"created": 1})
        self.assertEqual(len(self.px_calls()), 1)
        code, out = self.main("failures", "sweep")
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)
        self.assertEqual(len(self.px_calls()), 1)

    def test_a_px_failure_exits_2(self):
        self.seed()
        self.px_mode("fail")
        code, out = self.main("failures", "sweep")
        self.assertEqual(code, 2)
        self.assertIn("plane is down", out)

    def test_max_flag_reaches_the_sweep(self):
        for n in range(3):
            self.item(f"eeee{n:012d}", state="failed")
            self.transition("item", f"eeee{n:012d}", "failed", f"cause_{n}", ago(1000 + n))
        with self.assertLogs("wax.failures", level="WARNING"):
            code, out = self.main("failures", "sweep", "--max", "1", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["counts"], {"created": 1, "capped": 2})

    def test_a_bad_duration_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught, patch("sys.stderr", io.StringIO()):
            self.main("failures", "--since", "soon")
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
