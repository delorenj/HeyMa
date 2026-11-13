"""`wax split` on a recording that is being processed (HEYMA-18): the fence, the halt, the late result.

A real `worker.process` runs in a thread against a slow fake transcriber (a bash script standing in
for bin/transcribe, WAX_TRANSCRIBE) while `split.run` is called from the test thread, exactly as the
CLI process would call it next to waxd. Only the things that would touch the network, the desktop or
S3 are patched (archive, desktop, passes.run_auto); the ledger, the item flock, the process tracking
and the kill are real.
"""

import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import split_support as sup
from wax import fence, finalize, ledger, operations, passes, paths, procutil, split, transcribe_adapter, worker

PARAMS = split.Params(**sup.SCALED)


def _alive(pid: int) -> bool:
    """A live (not zombie) process with this pid."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return raw[raw.rindex(")") + 2:].split()[0] != "Z"


class FenceBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "root"
        ctx = sup.runtime(self.root)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.marks = self.tmp / "marks"
        self.marks.mkdir()
        # nothing here may reach S3, the desktop, the enrichment models or the real transcript store
        for target, kwargs in (
                (worker.archive, dict(name="archive", side_effect=lambda p, item_id=None: {
                    "s3_key": f"test/{item_id}", "bytes": Path(p).stat().st_size})),
                (worker.archive, dict(name="remote_size", side_effect=lambda key: None)),
                (worker, dict(name="desktop", new=MagicMock())),
                (transcribe_adapter, dict(name="desktop", new=MagicMock())),
                (worker.passes, dict(name="run_auto", return_value=[])),
                (worker.config, dict(name="transcription_size_allowed", return_value=True)),
        ):
            name = kwargs.pop("name")
            p = patch.object(target, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def stage_item(self, fixture: str, name: str) -> tuple[str, Path]:
        dest = paths.INBOX / name
        shutil.copy2(sup.fixture_ogg(fixture), dest)
        return ledger.upsert_item(dest, origin="inbox"), dest

    def slow_transcriber(self, seconds: float, *, writes_note: bool = True) -> Path:
        note = paths.VAULT / "late-note.md"
        body = f"""echo $$ > {self.marks}/pid
sleep {seconds} &
echo $! > {self.marks}/sleeper
wait
{f'echo late transcript > {note}' if writes_note else ''}
echo {note}"""
        script = sup.fake_transcriber(self.tmp, body)
        env = patch.dict(os.environ, {"WAX_TRANSCRIBE": str(script)})
        env.start()
        self.addCleanup(env.stop)
        return note

    def run_worker(self, item_id: str, path: Path) -> dict:
        out: dict = {}

        def target():
            try:
                out["result"] = worker.process(item_id, path)
            except BaseException as exc:  # noqa: BLE001 - surfaced to the test
                out["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        out["thread"] = thread
        return out

    def wait_for(self, mark: str, timeout: float = 20.0) -> str:
        deadline = time.monotonic() + timeout
        path = self.marks / mark
        while time.monotonic() < deadline:
            if path.exists() and path.read_text().strip():
                return path.read_text().strip()
            time.sleep(0.05)
        self.fail(f"the fake transcriber never wrote {mark}")

    def state_of(self, item_id: str) -> str:
        return ledger.connect().execute("SELECT state FROM items WHERE item_id=?", (item_id,)).fetchone()["state"]


class SplitWhileProcessingTest(FenceBase):
    def test_halts_the_item_cuts_it_and_leaves_the_queue_alone(self):
        note = self.slow_transcriber(60)
        item_id, audio = self.stage_item("meeting_overrun_quiet", "meeting.ogg")
        other_id, other = self.stage_item("continuous_activity", "bystander.ogg")
        other_sha = ledger.sha256_file(other)
        before_state = {}
        run = self.run_worker(item_id, audio)
        pid = int(self.wait_for("pid"))
        sleeper = int(self.wait_for("sleeper"))
        before_state["item"] = self.state_of(item_id)
        self.assertEqual(before_state["item"], "archived", "in processing: archived, transcribing")
        self.assertTrue(_alive(pid) and _alive(sleeper))
        tracked = ledger.connect().execute("SELECT argv0 FROM item_children WHERE item_id=?", (item_id,)).fetchall()
        self.assertEqual(len(tracked), 1, "the transcriber is tracked against exactly this item")

        started = time.monotonic()
        report = split.run(item_id[:10], PARAMS, halt_timeout_s=30)
        halted_in = time.monotonic() - started

        self.assertEqual(report["outcome"], "split")
        self.assertLess(halted_in, 20, "the halt killed the child instead of waiting out its 60 s")
        self.assertTrue(report["halted_children"], "the report names what was halted")
        self.assertFalse(_alive(pid), "the transcriber is gone")
        self.assertFalse(_alive(sleeper), "and so is its whole process tree")
        run["thread"].join(15)
        self.assertFalse(run["thread"].is_alive())
        self.assertNotIn("error", run, run.get("error"))
        self.assertIn("fenced", run["result"], "the worker noticed and stopped; it did not park or transcribe")
        self.assertNotIn("transcribe", run["result"])
        # no late result anywhere
        self.assertFalse(note.exists())
        self.assertIsNone(ledger.connect().execute("SELECT 1 FROM transcripts WHERE item_id=?",
                                                   (item_id,)).fetchone())
        self.assertEqual(self.state_of(item_id), "skipped")
        last = ledger.connect().execute("SELECT cause_code FROM transitions WHERE machine='item' AND subject=? ORDER BY seq DESC LIMIT 1",
                                        (item_id,)).fetchone()
        self.assertEqual(last["cause_code"], "split")
        # no audio lost: original and both parts exist, the original is byte-identical
        hold = Path(report["held_dir"])
        self.assertEqual(ledger.sha256_file(hold / "meeting.ogg"), report["source_sha256"])
        self.assertEqual(len(report["segments"]), 2)
        for seg in report["segments"]:
            self.assertEqual(ledger.sha256_file(hold / seg["file"]), seg["sha256"])
        self.assertEqual(report["processing"]["state_before"], "archived")
        self.assertEqual(report["processing"]["state_after"], "skipped")
        self.assertTrue(report["processing"]["fenced"])
        # unrelated queue item: not fenced, not moved, not changed, still pending
        self.assertEqual(self.state_of(other_id), "pending")
        self.assertTrue(other.is_file())
        self.assertEqual(ledger.sha256_file(other), other_sha)
        self.assertIsNone(ledger.fence_of(other_id))
        self.assertEqual(worker.next_item()[0], other_id, "the worker's next pick is the bystander")

    def test_a_late_result_after_the_fence_is_parked_not_published(self):
        # Worst case: the halt does not stop the child (patched out), so the transcriber finishes
        # AFTER the fence is up and has already written its note into the vault.
        note = self.slow_transcriber(2)
        item_id, audio = self.stage_item("meeting_overrun_quiet", "meeting.ogg")
        run = self.run_worker(item_id, audio)
        self.wait_for("pid")
        with patch.object(fence, "halt_children", return_value=[]):
            report = split.run(item_id[:10], PARAMS, halt_timeout_s=30)
        self.assertEqual(report["outcome"], "split")
        run["thread"].join(15)
        self.assertIn("fenced", run["result"])
        self.assertFalse(note.exists(), "the late transcript is out of the vault")
        parked = paths.RECOVERED / "split-late" / note.name
        self.assertEqual(parked.read_text().strip(), "late transcript", "preserved, never deleted")
        self.assertIsNone(ledger.connect().execute("SELECT 1 FROM transcripts WHERE item_id=?", (item_id,)).fetchone())
        self.assertEqual(self.state_of(item_id), "skipped", "the late worker did not move the item")
        self.assertEqual(len(list(Path(report["held_dir"]).glob("*part*.ogg"))), 2, "no duplicate outcome")
        self.assertEqual(len(ledger.connect().execute("SELECT 1 FROM split_segments").fetchall()), 2)

    def test_no_boundary_leaves_the_processing_item_running(self):
        self.slow_transcriber(3, writes_note=False)
        item_id, audio = self.stage_item("continuous_activity", "talk.ogg")
        run = self.run_worker(item_id, audio)
        pid = int(self.wait_for("pid"))
        report = split.run(item_id[:10], PARAMS, halt_timeout_s=30)
        self.assertEqual(report["outcome"], "no_boundary")
        self.assertIsNone(ledger.fence_of(item_id), "never fenced: analysis comes first")
        self.assertTrue(_alive(pid), "the transcriber was not touched")
        self.assertEqual(ledger.connect().execute("SELECT COUNT(*) c FROM item_children").fetchone()["c"], 1)
        self.assertFalse(split.split_root().exists())
        run["thread"].join(15)           # lets the 3 s fake finish; its fake output is not a real note

    def test_a_halt_that_cannot_settle_lifts_the_fence_and_cuts_nothing(self):
        item_id, audio = self.stage_item("meeting_overrun_quiet", "meeting.ogg")
        sha = ledger.sha256_file(audio)
        release = threading.Event()
        held = threading.Event()

        def hold_lock():       # an untracked holder: nothing the split can signal
            with operations.item_lock(item_id):
                held.set()
                release.wait(30)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        self.addCleanup(holder.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        with self.assertRaises(split.HaltTimeout):
            split.run(item_id[:10], PARAMS, halt_timeout_s=1.0)
        self.assertIsNone(ledger.fence_of(item_id), "the fence is lifted: the item resumes as it was")
        self.assertEqual(self.state_of(item_id), "pending")
        self.assertEqual(ledger.sha256_file(audio), sha)
        self.assertFalse(split.split_root().exists(), "nothing was cut")
        self.assertEqual(split.EXIT_NO_BOUNDARY, 3)


class FenceGuardTest(FenceBase):
    """Every commit point refuses a fenced item. Unit-level, so each guard is pinned on its own."""

    def setUp(self):
        super().setUp()
        self.item_id, self.audio = self.stage_item("meeting_overrun_quiet", "meeting.ogg")
        self.assertTrue(fence.raise_fence(self.item_id, "split-test"))

    def test_raise_is_idempotent_and_lift_removes_it(self):
        self.assertFalse(fence.raise_fence(self.item_id, "split-test"))
        fence.lift_fence(self.item_id, "split-test")
        self.assertIsNone(ledger.fence_of(self.item_id))

    def test_the_ledger_refuses_state_changes_except_the_splits_own(self):
        with self.assertRaises(fence.Fenced):
            ledger.set_item_state(self.item_id, "transcribed", cause="late_worker")
        ledger.set_item_state(self.item_id, "skipped", cause="split")
        self.assertEqual(self.state_of(self.item_id), "skipped")

    def test_the_worker_does_not_pick_up_a_fenced_item(self):
        self.assertIsNone(worker.next_item())

    def test_process_returns_fenced_without_archiving(self):
        result = worker.process(self.item_id, self.audio)
        self.assertIn("fenced", result)
        worker.archive.archive.assert_not_called()

    def test_a_pass_is_cancelled_not_run_and_not_recorded_as_failed(self):
        with patch.object(passes.fence, "run_tracked") as run_tracked:
            result = passes.run(self.item_id, "title-slug")
        run_tracked.assert_not_called()
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["reason_code"], "item_split")
        self.assertIsNone(ledger.connect().execute("SELECT 1 FROM passes WHERE item_id=? AND state='failed'",
                                                   (self.item_id,)).fetchone())

    def test_completion_and_pass_events_are_withheld(self):
        self.assertEqual(finalize.finalize(self.item_id), {"finalized": False, "reason_code": "item_split"})
        self.assertEqual(finalize.publish_pass_events(self.item_id)["pass_events_reason"], "item_split")

    def test_the_failed_pass_sweep_skips_a_fenced_item(self):
        conn = ledger.connect()
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(passes)")]
        self.assertIn("item_id", cols)
        self.assertEqual([r for r in ledger.failed_passes_for_sweep(5) if r[0] == self.item_id], [])

    def test_transcribe_refuses_to_start(self):
        with self.assertRaises(fence.Fenced):
            transcribe_adapter.transcribe(self.audio, item_id=self.item_id)


class ProcessTrackingTest(FenceBase):
    def test_run_tracked_registers_and_unregisters_and_kills_the_tree_on_halt(self):
        item_id, _ = self.stage_item("meeting_overrun_quiet", "meeting.ogg")
        script = sup.fake_transcriber(self.tmp, f"echo $$ > {self.marks}/pid\nsleep 60 &\necho $! > {self.marks}/sleeper\nwait")
        box: dict = {}

        def target():
            box["r"] = fence.run_tracked(item_id, [str(script)], timeout=120)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 15)
        pid, sleeper = int(self.wait_for("pid")), int(self.wait_for("sleeper"))
        rows = ledger.connect().execute("SELECT * FROM item_children WHERE item_id=?", (item_id,)).fetchall()
        self.assertEqual([r["pid"] for r in rows], [pid])
        halted = fence.halt_children(item_id)
        thread.join(15)
        self.assertEqual([h["pid"] for h in halted], [pid])
        self.assertFalse(_alive(pid) or _alive(sleeper))
        self.assertNotEqual(box["r"].returncode, 0)
        self.assertEqual(ledger.connect().execute("SELECT COUNT(*) c FROM item_children").fetchone()["c"], 0)

    def test_halt_ignores_a_recycled_pid(self):
        item_id, _ = self.stage_item("meeting_overrun_quiet", "meeting.ogg")
        bystander = __import__("subprocess").Popen(["sleep", "30"])
        self.addCleanup(bystander.kill)
        ledger.connect().execute(
            "INSERT INTO item_children(item_id,pid,starttime,boot_id,argv0,started_at) VALUES(?,?,?,?,?,?)",
            (item_id, bystander.pid, procutil.proc_starttime(bystander.pid) + 12345, procutil.boot_id(), "sleep", "now"))
        self.assertEqual(fence.halt_children(item_id), [])
        self.assertIsNone(bystander.poll(), "a process that merely reuses the pid is never signalled")


if __name__ == "__main__":
    unittest.main()
