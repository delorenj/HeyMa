import os
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
REPO_ROOT = next(
    parent for parent in COMPONENT_ROOT.parents
    if (parent / ".project.json").is_file()
)


class WaxIntegrationTest(unittest.TestCase):
    @staticmethod
    def _write_test_ogg(path: Path, frequency: int) -> None:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i",
                f"sine=frequency={frequency}:sample_rate=48000:duration=0.6",
                "-ac", "1", "-c:a", "libopus", "-f", "ogg", str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )

    def test_default_runtime_root_is_heyma(self):
        env = os.environ.copy()
        env.pop("WAX_ROOT", None)
        env.pop("WAX_AUDIO_ROOT", None)
        env["PYTHONPATH"] = str(COMPONENT_ROOT / "src")
        result = subprocess.run(
            [
                "python3",
                "-c",
                "from wax.paths import HEYMA; print(HEYMA)",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), str(Path.home() / "HeyMa"))

    def test_wax_root_override_isolated_from_project_data(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            env = os.environ.copy()
            env["WAX_ROOT"] = runtime_root
            result = subprocess.run(
                [
                    str(REPO_ROOT / "bin" / "wax"),
                    "state",
                    "inbox",
                    "--cold",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertIn(result.returncode, {0, 2})
            self.assertIn('"state"', result.stdout)

    def test_root_shim_works_outside_repository(self):
        with tempfile.TemporaryDirectory() as working_dir, tempfile.TemporaryDirectory() as runtime_root:
            env = os.environ.copy()
            env["WAX_ROOT"] = runtime_root
            result = subprocess.run(
                [str(REPO_ROOT / "bin" / "wax"), "state", "stream", "--cold", "--json"],
                cwd=working_dir,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn(json.loads(result.stdout)["state"], {"ready", "not-ready"})

    def test_quiesce_is_an_idempotent_noop_when_idle(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            env = {**os.environ, "WAX_ROOT": runtime_root}
            result = subprocess.run(
                [str(REPO_ROOT / "bin" / "wax"), "rec", "quiesce", "--json"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(json.loads(result.stdout)["action"], "idle")

    def test_quiesce_cleanly_finishes_a_live_encoder(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            root = Path(runtime_root)
            stream = root / "stream"
            inbox = root / "inbox"
            stream.mkdir()
            inbox.mkdir()
            rid = "20260822-133309-quiesce"
            segdir = stream / f"{rid}.segs"
            segdir.mkdir()
            ctl = stream / f"{rid}.ctl"
            os.mkfifo(ctl)
            ctl_fd = os.open(ctl, os.O_RDWR)
            encoder = subprocess.Popen(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                    "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=60",
                    "-ac", "1", "-c:a", "libopus", "-f", "segment",
                    "-segment_time", "0.5", "-segment_format", "ogg",
                    "-reset_timestamps", "1", str(segdir / "seg-%05d.ogg"),
                ],
                stdin=ctl_fd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                proc_stat = Path(f"/proc/{encoder.pid}/stat").read_text()
                starttime = int(proc_stat[proc_stat.rindex(")") + 2:].split()[19])
                (stream / f"{rid}.rec.json").write_text(json.dumps({
                    "rid": rid,
                    "pid": encoder.pid,
                    "starttime": starttime,
                    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "target_name": "session-shutdown.ogg",
                }))
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if len(list(segdir.glob("seg-*.ogg"))) >= 2:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("test encoder did not close two segments")

                env = {**os.environ, "WAX_ROOT": runtime_root}
                result = subprocess.run(
                    [str(REPO_ROOT / "bin" / "wax"), "rec", "quiesce", "--json"],
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=True,
                )
                payload = json.loads(result.stdout)
                self.assertEqual(payload["action"], "stopped")
                self.assertGreater(payload["duration_s"], 0.5)
                self.assertTrue(Path(payload["path"]).is_file())
                self.assertFalse(any(stream.iterdir()))
            finally:
                os.close(ctl_fd)
                if encoder.poll() is None:
                    encoder.terminate()
                encoder.wait(timeout=5)

    def test_disabled_pipeline_with_backlog_is_stopped_not_error(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            inbox = Path(runtime_root) / "inbox"
            inbox.mkdir()
            (inbox / "queued.ogg").write_bytes(b"queued")
            env = os.environ.copy()
            env["WAX_ROOT"] = runtime_root
            result = subprocess.run(
                [
                    str(REPO_ROOT / "bin" / "wax"),
                    "state",
                    "inbox",
                    "--cold",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            state = json.loads(result.stdout)
            self.assertEqual(state["state"], "stopped")
            self.assertEqual(state["pending"], 1)
            self.assertEqual(state["cause_code"], "scheduler_disabled")

    def test_skip_moves_audio_out_of_queue_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            root = Path(runtime_root)
            inbox = root / "inbox"
            inbox.mkdir()
            audio = inbox / "skip-me.ogg"
            audio.write_bytes(b"preserve these bytes")
            env = os.environ.copy()
            env["WAX_ROOT"] = runtime_root
            identify = subprocess.run(
                [
                    "python3", "-c",
                    "from wax import ledger; from pathlib import Path; "
                    "print(ledger.upsert_item(Path(r'" + str(audio) + "')))"
                ],
                cwd=REPO_ROOT,
                env={**env, "PYTHONPATH": str(COMPONENT_ROOT / "src")},
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            result = subprocess.run(
                [str(REPO_ROOT / "bin" / "wax"), "skip", identify, "--json"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)
            skipped = root / "skipped" / audio.name
            self.assertEqual(payload["state"], "skipped")
            self.assertFalse(audio.exists())
            self.assertEqual(skipped.read_bytes(), b"preserve these bytes")

    def test_failed_item_stays_visible_without_blocking_next_pending_item(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            root = Path(runtime_root)
            inbox = root / "inbox"
            inbox.mkdir()
            failed = inbox / "01-failed.ogg"
            pending = inbox / "02-pending.ogg"
            failed.write_bytes(b"failed audio remains")
            pending.write_bytes(b"healthy audio follows")
            env = {**os.environ, "WAX_ROOT": runtime_root,
                   "PYTHONPATH": str(COMPONENT_ROOT / "src")}
            script = (
                "from wax import ledger,worker; from pathlib import Path; "
                f"bad=ledger.upsert_item(Path(r'{failed}')); "
                f"good=ledger.upsert_item(Path(r'{pending}')); "
                "ledger.set_item_state(bad,'failed',cause='test_failure'); "
                "print(worker.next_item()[0]); print(good)"
            )
            result = subprocess.run(
                ["python3", "-c", script], cwd=REPO_ROOT, env=env,
                capture_output=True, text=True, check=True,
            )
            selected, expected = result.stdout.splitlines()
            self.assertEqual(selected, expected)
            self.assertTrue(failed.exists())

    def test_retry_requeues_a_preserved_failed_or_suspect_item(self):
        for failure_state in ("failed", "suspect"):
            with self.subTest(state=failure_state), tempfile.TemporaryDirectory() as runtime_root:
                root = Path(runtime_root)
                inbox = root / "inbox"
                inbox.mkdir()
                failed = inbox / "retry-me.ogg"
                failed.write_bytes(b"failed audio remains")
                env = {**os.environ, "WAX_ROOT": runtime_root,
                       "PYTHONPATH": str(COMPONENT_ROOT / "src")}
                identify = subprocess.run(
                    [
                        "python3", "-c",
                        "from wax import ledger; from pathlib import Path; "
                        f"item=ledger.upsert_item(Path(r'{failed}')); "
                        f"ledger.set_item_state(item,'{failure_state}',cause='archive_failed'); "
                        "print(item)"
                    ],
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()

                result = subprocess.run(
                    [str(REPO_ROOT / "bin" / "wax"), "retry", identify, "--json"],
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                payload = json.loads(result.stdout)
                self.assertEqual(payload["state"], "pending")
                self.assertEqual(payload["previous_cause"], "archive_failed")
                self.assertTrue(failed.exists())

                selected = subprocess.run(
                    [
                        "python3", "-c",
                        "from wax import worker; print(worker.next_item()[0])",
                    ],
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                self.assertEqual(selected, identify)

    def test_salvage_publishes_remux_and_preserves_original_segments(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            root = Path(runtime_root)
            rid = "20260822-133309-test"
            segdir = root / "stream" / f"{rid}.segs"
            segdir.mkdir(parents=True)
            (root / "inbox").mkdir()
            originals = {}
            for number, frequency in enumerate((440, 880)):
                segment = segdir / f"seg-{number:05d}.ogg"
                self._write_test_ogg(segment, frequency)
                originals[segment.name] = segment.read_bytes()

            rec = root / "stream" / f"{rid}.rec.json"
            rec.write_text(json.dumps({
                "rid": rid,
                "pid": 0,
                "starttime": 0,
                "boot_id": "dead-boot",
                "target_name": "interrupted.ogg",
            }))
            env = {**os.environ, "WAX_ROOT": runtime_root}
            result = subprocess.run(
                [str(REPO_ROOT / "bin" / "wax"), "rec", "salvage", rid, "--json"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)

            published = Path(payload["to"])
            evidence = Path(payload["evidence_path"])
            self.assertTrue(payload["published"])
            self.assertTrue(published.is_file())
            self.assertEqual(payload["segments_used"], 2)
            self.assertEqual(payload["segments_skipped"], 0)
            for name, original in originals.items():
                self.assertEqual((evidence / name).read_bytes(), original)
            self.assertTrue((evidence / rec.name).is_file())
            self.assertFalse(any((root / "stream").iterdir()))

    def test_salvage_preserves_unprobeable_legacy_partial(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            root = Path(runtime_root)
            stream = root / "stream"
            stream.mkdir(parents=True)
            (root / "inbox").mkdir()
            rid = "20260822-133309-legacy"
            partial = stream / f"{rid}.ogg.partial"
            original = b"irreplaceable truncated legacy audio"
            partial.write_bytes(original)
            rec = stream / f"{rid}.rec.json"
            rec.write_text(json.dumps({
                "rid": rid,
                "pid": 0,
                "starttime": 0,
                "boot_id": "dead-boot",
                "target_name": "legacy.ogg",
            }))
            env = {**os.environ, "WAX_ROOT": runtime_root}
            result = subprocess.run(
                [str(REPO_ROOT / "bin" / "wax"), "rec", "salvage", rid, "--json"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)
            evidence = Path(payload["evidence_path"])

            self.assertFalse(payload["published"])
            self.assertEqual((evidence / partial.name).read_bytes(), original)
            self.assertTrue((evidence / rec.name).is_file())
            self.assertFalse(any(stream.iterdir()))

    def test_failed_only_inbox_is_not_reported_as_stranded_work(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            root = Path(runtime_root)
            inbox = root / "inbox"
            inbox.mkdir()
            failed = inbox / "broken.ogg"
            failed.write_bytes(b"broken")
            env = {**os.environ, "WAX_ROOT": runtime_root,
                   "PYTHONPATH": str(COMPONENT_ROOT / "src")}
            script = (
                "from wax import ledger,state; from pathlib import Path; "
                "state.QUEUE_GRACE_S=0; state.ENABLED_FLAG.parent.mkdir(parents=True,exist_ok=True); "
                "state.ENABLED_FLAG.touch(); "
                f"item=ledger.upsert_item(Path(r'{failed}')); "
                "ledger.set_item_state(item,'failed',cause='decode_failed'); "
                "snap=ledger.enrich(state.snapshot(run_preflight=False)); "
                "print(snap['inbox']['cause_code']); print(snap['queue']['failed'])"
            )
            result = subprocess.run(
                ["python3", "-c", script], cwd=REPO_ROOT, env=env,
                capture_output=True, text=True, check=True,
            )
            cause, failed_count = result.stdout.splitlines()
            self.assertEqual(cause, "failed_items")
            self.assertEqual(failed_count, "1")

    def test_completed_tray_item_carries_derived_transcript_path(self):
        with tempfile.TemporaryDirectory() as runtime_root:
            root = Path(runtime_root)
            archive = root / "archive"
            vault = root / "vault"
            archive.mkdir()
            vault.mkdir()
            audio = archive / "source.ogg"
            transcript = vault / "derived-transcript.md"
            audio.write_bytes(b"audio")
            transcript.write_text("# transcript\n")
            env = {**os.environ, "WAX_ROOT": runtime_root, "WAX_VAULT": str(vault),
                   "PYTHONPATH": str(COMPONENT_ROOT / "src")}
            script = (
                "from wax import ledger,sentinel; from pathlib import Path; "
                f"item=ledger.upsert_item(Path(r'{audio}'),origin='archive'); "
                "sql='INSERT INTO transcripts(item_id,md_path,created_at) VALUES(?,?,?)'; "
                f"ledger.connect().execute(sql,(item,r'{transcript}',sentinel.utcnow())); "
                "ledger.set_item_state(item,'complete',cause='test'); "
                "row=ledger.tray_items()[0]; print(row['orig_name']); print(row['md_path'])"
            )
            result = subprocess.run(
                ["python3", "-c", script], cwd=REPO_ROOT, env=env,
                capture_output=True, text=True, check=True,
            )
            source_name, md_path = result.stdout.splitlines()
            self.assertEqual(source_name, "source.ogg")
            self.assertEqual(md_path, str(transcript))


if __name__ == "__main__":
    unittest.main()
