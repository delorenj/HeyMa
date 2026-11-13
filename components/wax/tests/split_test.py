"""Real CLI + AF_UNIX + ffmpeg evidence in a fully isolated native waxd harness.

Only external integrations (display, store, events, device and S3 transport) are
replaced. Capture, worker, backup verification, item locks, passes/finalize,
socket handler, media analysis/extraction and CLI are the production code.
"""
import array
import hashlib
import json
import math
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import wave
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parents[1]
CLI = COMPONENT / "bin" / "wax"
SRC = COMPONENT / "src"
PYTHON = "/usr/bin/python3"


def wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.025)
    raise AssertionError("fixture condition did not become true before deadline")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixture_audio(path, kind="meeting", stereo=False, duration=190):
    """8 kHz 16-bit; modulated multi-tone syllables, ordinary pauses, low noise.

    Meeting activity 0..60s (53 accumulated active seconds), quiet 60..190s.
    Expected 100ms-window quiet start=60s; padded cut=62s. Phase stereo is L=-R.
    Internal case has >120s quiet followed by resumed activity; never split it.
    """
    rate = 8000
    channels = 2 if stereo else 1
    pauses = {10, 11, 25, 26, 27, 44, 45}
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        for second in range(duration):
            active = (kind == "continuous" or
                      kind == "meeting" and second < 60 and second not in pauses or
                      kind == "pauses" and second % 12 not in (9, 10) or
                      kind == "internal" and (second < 45 or second >= 175))
            samples = array.array("h")
            for i in range(rate):
                t = second + i / rate
                value = (int(6000 * (0.55 + 0.4 * math.sin(2 * math.pi * 3.2 * t)) *
                             (math.sin(2 * math.pi * 183 * t) + 0.25 * math.sin(2 * math.pi * 337 * t)))
                         if active else (i % 5 - 2))
                samples.append(value)
                if stereo:
                    samples.append(-value)
            output.writeframes(samples.tobytes())
    return {"kind": kind, "sample_rate": rate, "channels": channels, "duration": duration,
            "activity_end_seconds": 60 if kind == "meeting" else None,
            "expected_meeting_cut_seconds": 62 if kind == "meeting" else None,
            "sha256": sha(path)}


MC = r'''#!/usr/bin/python3
import hashlib,json,os,shutil,sys
from pathlib import Path
root=Path(os.environ["WAX_ROOT"])
args=sys.argv[1:]; command=args[0]
def remote(target):
    p=root/"fake-s3"/target
    assert p.resolve().is_relative_to((root/"fake-s3").resolve())
    return p
if command=="cp":
    source,destination=Path(args[-2]),remote(args[-1]); destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,destination)
    sys.exit(0)
if command in ("stat","ls"):
    p=remote(args[-1])
    if not p.is_file(): sys.exit(1)
    size=p.stat().st_size
    if (root/"bad-backup").exists(): size+=1
    print(json.dumps(dict(status="success",type="file",key=p.name,size=size,etag=hashlib.md5(p.read_bytes()).hexdigest())))
elif command=="cat":
    p=remote(args[-1]); print(p.read_text() if p.exists() else "null")
elif command=="tag": pass
else: sys.exit(1)
'''

TRANSCRIBE = r'''#!/usr/bin/python3
import json,os,sqlite3,subprocess,sys,time
from pathlib import Path
root=Path(os.environ["WAX_ROOT"]); source=Path(sys.argv[1])
conn=sqlite3.connect(root/"var/wax.db"); conn.row_factory=sqlite3.Row
row=conn.execute("SELECT item_id FROM files_seen WHERE path=?",(str(source),)).fetchone(); item=row["item_id"]
backup=conn.execute("SELECT * FROM backups WHERE item_id=? AND verified_at IS NOT NULL",(item,)).fetchone()
# Direct adapter tests may opt in to starting without a worker backup; normal
# harness transcription MUST prove the real worker passed its verification gate.
if not backup and not (root/"direct-adapter-test").exists(): sys.exit(91)
(root/(item+".started")).write_text("verified-backup="+str(bool(backup)))
if (root/"block-child").exists() and source.name.startswith("a-"):
    deadline=time.monotonic()+60
    while not (root/(item+".release")).exists():
        if time.monotonic()>deadline: sys.exit(92)
        time.sleep(.02)
duration=float(subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","default=nw=1:nk=1",str(source)],text=True))
md=Path(os.environ["WAX_VAULT"])/(item+".md")
md.write_text("---\ntitle: Fixture meeting\n---\nHistorical source transcript.\n")
if "--asr-output" in sys.argv:
    Path(sys.argv[sys.argv.index("--asr-output")+1]).write_text(json.dumps({"segments":[],"duration":duration}))
print(md)
print("Transcription-Metadata: "+json.dumps({"duration_seconds":duration,"word_count":4,"model":"controlled-fixture","diarized":False}),file=sys.stderr)
(root/(item+".child-finished")).write_text(str(time.time_ns()))
'''

HARNESS = r'''
import fcntl,json,os,runpy,sys,threading,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from wax import archive,capture,desktop,documents,events,operations,paths,split,state,transcriptions,worker
root=paths.AUDIO
archive.MC=str(root/"fake-mc"); archive.ATTEMPTS=1
for name in ("ding","notify","notify_stage_failure","clear_stage_failure"):
    setattr(desktop,name,lambda *a,**k:None)
events.drain=lambda:None
transcriptions.tick=lambda **k:None
transcriptions.yield_gpu=lambda:None
documents.scan=lambda:{}
state.preflight=lambda:{"ok":True,"source":"isolated-fixture","problems":[]}
worker.POLL_S=.05
module=runpy.run_path(sys.argv[2],run_name="waxd_fixture")
class Tray:
    available=False; reason="isolated fixture"
    def poll(self): pass
    def stop(self): pass
module["tray_process"].TrayProcess=Tray
Daemon=module["Daemon"]
Daemon._schedule_telephony=lambda self:{}
original=split._extract
def extract(*args):
    if (root/"fail-extract").exists() and args[-1]=="tail":
        raise RuntimeError("injected tail extraction failure")
    if (root/"pause-extract").exists():
        (root/"extract-entered").touch()
        deadline=time.monotonic()+30
        while not (root/"release-extract").exists():
            if time.monotonic()>deadline: raise TimeoutError("test extraction barrier")
            time.sleep(.02)
    return original(*args)
split._extract=extract
daemon=Daemon()
if (root/"capture-fixture.wav").exists():
    def argv(source,out,rid,**kwargs):
        return ["ffmpeg","-hide_banner","-loglevel","error","-readrate","30",
                "-i",str(root/"capture-fixture.wav"),"-c:a","libopus","-b:a","32k",
                "-f","segment","-segment_time","60","-segment_format","ogg",
                "-reset_timestamps","1",str(out)]
    capture._ffmpeg_argv=argv
    with paths.LOCK.open("a") as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        rec=capture.start(trigger="isolated-test")
    (root/"capture-rid").write_text(rec["rid"])
(root/"daemon-ready").touch()
try:
    while not (root/"stop-harness").exists():
        daemon._tick()
        time.sleep(.1)
finally:
    daemon.worker.stop()
    for rid in capture.sentinel.list_captures():
        if capture.sentinel.encoder_alive(capture.sentinel.read_json(paths.rec_path(rid)) or {}):
            try: capture.stop(rid,for_split=True)
            except capture.CaptureError: pass
    deadline=time.monotonic()+10
    while daemon.worker._liveness_snapshot()[1] and time.monotonic()<deadline:
        time.sleep(.02)
    daemon.quit()
'''


class Runtime:
    def __init__(self, root):
        self.root = root
        root.mkdir()
        self.env = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            self.env.pop(name, None)
        self.env.update(WAX_ROOT=str(root), WAX_VAULT=str(root / "vault"),
                        TRANSCRIPTS=str(root / "vault"), WAX_PASSES_DIR=str(root / "passes"),
                        WAX_TRANSCRIBE=str(root / "fake-transcribe"), WAX_DIARIZATION="0",
                        WAX_S3_ENDPOINT="http://isolated.invalid", WAX_MIN_FREE_BYTES="0")
        for name in ("vault", "passes", "inbox"):
            (root / name).mkdir()
        for name, text in (("fake-mc", MC), ("fake-transcribe", TRANSCRIBE), ("harness.py", HARNESS)):
            file = root / name
            file.write_text(text)
            file.chmod(0o700)
        self.log = (root / "daemon.log").open("w")
        self.process = None

    def start(self):
        self.process = subprocess.Popen([PYTHON, str(self.root / "harness.py"), str(SRC), str(COMPONENT / "bin/waxd")],
                                        env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        wait_for(lambda: (self.root / "daemon-ready").exists() or self.process.poll() is not None)
        assert self.process.poll() is None, (self.root / "daemon.log").read_text()
        return self

    def close(self):
        for started in self.root.glob("*.started"):
            started.with_suffix(".release").touch()
        (self.root / "release-extract").touch()
        (self.root / "stop-harness").touch()
        if self.process:
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(5)
                raise AssertionError("isolated daemon failed to stop")
            assert self.process.returncode == 0, (self.root / "daemon.log").read_text()
        self.log.close()

    def cli(self, *args, check=None):
        result = subprocess.run([PYTHON, str(CLI), *map(str, args)], env=self.env,
                                capture_output=True, text=True, timeout=90)
        with (self.root / "cli-evidence.jsonl").open("a") as log:
            log.write(json.dumps({"argv": list(map(str, args)), "exit": result.returncode,
                                  "stdout": result.stdout, "stderr": result.stderr}) + "\n")
        if check is not None:
            assert result.returncode == check, result.stdout + result.stderr
        return result

    def evaluate(self, code, background=False):
        argv = [PYTHON, "-c", "import sys;sys.path.insert(0," + repr(str(SRC)) + ");" + code]
        if background:
            return subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    def rows(self, query, args=()):
        try:
            with sqlite3.connect(self.root / "var/wax.db") as conn:
                conn.row_factory = sqlite3.Row
                return [dict(row) for row in conn.execute(query, args)]
        except sqlite3.OperationalError:
            return []

    def enable(self):
        (self.root / "var/pipeline.enabled").touch()

    def split(self, target, *args, check=0):
        return json.loads(self.cli("split", target, *args, "--json", check=check).stdout)


@pytest.fixture
def runtime(tmp_path):
    run = Runtime(tmp_path / "runtime")
    try:
        yield run
    finally:
        run.close()


def assert_coverage(result, source):
    assert result["source"]["sha256"] == sha(source)
    assert result["source_held"]
    assert len(result["segments"]) == 2
    first, second = result["segments"]
    assert first["start_sample"] == 0
    assert first["end_sample"] == second["start_sample"]
    assert second["end_sample"] == result["analysis"]["total_samples"]
    decoded = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(source), "-f", "f64le", "-c:a", "pcm_f64le", "-"])
    outputs = []
    for segment in result["segments"]:
        assert sha(segment["path"]) == segment["sha256"]
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", segment["path"], "-f", "f64le", "-c:a", "pcm_f64le", "-"])
        assert hashlib.sha256(raw).hexdigest() == segment["decoded_sha256"]
        outputs.append(raw)
    assert b"".join(outputs) == decoded
    assert Path(result["manifest_path"]).is_file()


@pytest.mark.parametrize("stereo", [False, True], ids=["modulated-meeting", "phase-opposed-stereo"])
def test_real_cli_waveform_and_idempotency(runtime, stereo):
    source = runtime.root / "inbox/meeting.wav"
    characteristics = fixture_audio(source, stereo=stereo)
    (runtime.root / "fixture-characteristics.json").write_text(json.dumps(characteristics))
    original = source.read_bytes()
    runtime.start()
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(runtime.root / "var/waxd.sock"))
        assert json.loads(client.recv(65536))  # old send-and-close status client
    result = runtime.split(source)
    assert result["analysis"]["cut_sample"] == 62 * 8000
    assert result["segments"][0]["end_seconds"] == 62
    assert result["segments"][1]["end_seconds"] == 190
    assert result["submission"]["disposition"] == "held_not_submitted"
    assert_coverage(result, source)
    assert source.read_bytes() == original
    assert not list(runtime.root.glob("*.started"))  # no ASR/LLM prerequisite
    repeated = runtime.split(result["source"]["item_id"])
    assert repeated["reused"] and repeated["segments"] == result["segments"]
    assert len(list((Path(result["manifest_path"]).parent).glob("attempt-*"))) == 1


@pytest.mark.parametrize("kind", ["continuous", "quiet", "pauses", "internal"])
def test_explicit_no_boundary(runtime, kind):
    source = runtime.root / "inbox" / (kind + ".wav")
    fixture_audio(source, kind)
    before = sha(source)
    runtime.start()
    result = runtime.split(source, check=2)
    assert result["status"] == "no_boundary"
    assert result["source_held"] and Path(result["hold_path"]).is_file()
    assert "segments" not in result and sha(source) == before
    assert not list(runtime.root.rglob("manifest.json"))
    runtime.enable()
    time.sleep(.2)
    assert source.is_file() and not list(runtime.root.glob("*.started"))


def test_active_capture_stops_to_hold_preserves_segments(runtime):
    fixture_audio(runtime.root / "capture-fixture.wav", duration=600)
    runtime.start()
    rid = (runtime.root / "capture-rid").read_text()
    segments = runtime.root / "stream" / (rid + ".segs")
    wait_for(lambda: len(list(segments.glob("seg-*.ogg"))) >= 4, timeout=20)
    prior = {p.name: sha(p) for p in sorted(segments.glob("seg-*.ogg"))[:-1]}
    result = runtime.split(rid)
    assert result["capture"]["capture_state"] == "stopped"
    assert not result["capture"]["auto_restart"]
    source = Path(result["source"]["path"])
    assert source.is_relative_to(runtime.root / "recovered/split-captures")
    assert not list((runtime.root / "inbox").iterdir())
    evidence = Path(result["capture"]["evidence_path"])
    for name, digest in prior.items():
        assert sha(evidence / name) == digest
    for suffix in ("rec.json", "stop", "fin.json"):
        assert (evidence / (rid + "." + suffix)).exists()
    assembly = json.loads(Path(result["capture"]["assembly_path"]).read_text())
    assert assembly["total_samples"] == result["analysis"]["total_samples"]
    decoded = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(source),
                                       "-c:a", "pcm_f64le", "-f", "f64le", "-"])
    decoded_segments = b"".join(subprocess.check_output(
        ["ffmpeg", "-v", "error", "-i", str(p), "-c:a", "pcm_f64le", "-f", "f64le", "-"])
        for p in sorted(evidence.glob("seg-*.ogg")))
    assert decoded == decoded_segments
    assert not segments.exists()
    assert_coverage(result, source)
    again = runtime.split(rid)
    assert again["reused"] and again["source"] == result["source"]
    assert not list(runtime.root.glob("*.started"))


def test_active_capture_invalid_tail_never_claims_success(runtime):
    fixture_audio(runtime.root / "capture-fixture.wav", duration=600)
    runtime.start()
    rid = (runtime.root / "capture-rid").read_text()
    segments = runtime.root / "stream" / (rid + ".segs")
    wait_for(lambda: len(list(segments.glob("seg-*.ogg"))) >= 2)
    invalid = segments / "seg-99999.ogg"
    invalid.write_bytes(b"invalid tail: preserve me")
    result = runtime.split(rid, check=2)
    assert result["status"] == "failed" and "invalid segment" in result["error"]
    assert result["capture_hold"] and invalid.read_bytes() == b"invalid tail: preserve me"
    assert not list((runtime.root / "inbox").iterdir())
    assert not list(runtime.root.rglob("manifest.json"))
    assert (runtime.root / "stream" / (rid + ".rec.json")).exists()


def test_processing_settlement_timeout_late_claim_and_unrelated_progress(runtime):
    source = runtime.root / "inbox/a-meeting.wav"
    other = runtime.root / "inbox/b-unrelated.wav"
    fixture_audio(source)
    fixture_audio(other, "continuous", duration=2)
    before = sha(source)
    (runtime.root / "block-child").touch()
    runtime.start()
    runtime.enable()
    wait_for(lambda: list(runtime.root.glob("*.started")))
    item = runtime.rows("SELECT item_id FROM items WHERE orig_name='a-meeting.wav'")[0]["item_id"]
    timeout = runtime.split(item, "--wait-seconds", ".05", check=2)
    assert timeout["status"] == "pending" and timeout["source_held"]
    assert sha(source) == before and not list(runtime.root.rglob("manifest.json"))
    # A stale claimant queues AFTER the durable fence, before active child exits.
    stale = runtime.evaluate("from wax import worker;from pathlib import Path;import json;"
                             f"print(json.dumps(worker.process({item!r},Path({str(source)!r}))))", background=True)
    (runtime.root / "pause-extract").touch()
    split_process = subprocess.Popen([PYTHON, str(CLI), "split", item, "--json"], env=runtime.env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    (runtime.root / (item + ".release")).touch()
    wait_for(lambda: (runtime.root / "extract-entered").exists())
    # Original worker settles all nested adapter/pass/finalize work before the
    # split obtains the lock; it can park and change canonical items.path.
    row = runtime.rows("SELECT path,state FROM items WHERE item_id=?", (item,))[0]
    assert row["state"] == "complete" and Path(row["path"]).is_relative_to(runtime.root / "archive")
    note = Path(runtime.rows("SELECT md_path FROM transcripts WHERE item_id=?", (item,))[0]["md_path"])
    historical = note.read_bytes()
    wait_for(lambda: runtime.rows("SELECT item_id FROM items WHERE orig_name='b-unrelated.wav' AND state='complete'"))
    assert not list(runtime.root.rglob("manifest.json"))  # unrelated advances DURING split
    (runtime.root / "release-extract").touch()
    stdout, stderr = split_process.communicate(timeout=30)
    assert split_process.returncode == 0, stdout + stderr
    result = json.loads(stdout)
    (runtime.root / "settlement-cli.json").write_text(stdout)
    stale_stdout, stale_stderr = stale.communicate(timeout=10)
    assert stale.returncode == 0, stale_stderr
    assert json.loads(stale_stdout)["reason_code"] == "split_hold"
    assert result["processing"]["historical_transcript"] == str(note)
    assert result["source"]["path"] == row["path"]
    assert sha(row["path"]) == before and note.read_bytes() == historical
    assert_coverage(result, row["path"])
    completions = runtime.rows("SELECT * FROM completions WHERE item_id=?", (item,))
    assert len(completions) == 1
    runtime.cli("transcribe", row["path"], check=2)
    guard = json.loads(runtime.evaluate("from wax import passes,finalize;import json;"
                                        f"print(json.dumps([passes.run({item!r},'nonexistent'),passes.run_auto({item!r}),"
                                        f"finalize.finalize({item!r}),finalize.publish_pass_events({item!r})]))"))
    assert guard[0]["reason_code"] == guard[2]["reason_code"] == guard[3]["reason_code"] == "split_hold"
    assert guard[1] == []
    assert note.read_bytes() == historical
    assert runtime.rows("SELECT * FROM completions WHERE item_id=?", (item,)) == completions


def test_extraction_failure_is_recoverable_retry_is_atomic(runtime):
    source = runtime.root / "inbox/meeting.wav"
    fixture_audio(source)
    before = sha(source)
    runtime.start()
    (runtime.root / "fail-extract").touch()
    failed = runtime.split(source, check=2)
    assert failed["status"] == "failed" and "injected" in failed["error"]
    assert failed["source_held"] and not list(runtime.root.rglob("manifest.json"))
    partial = Path(failed["attempt_path"]) / "prefix.wav"
    assert partial.exists() and sha(source) == before
    prefix_hash = sha(partial)
    (runtime.root / "fail-extract").rename(runtime.root / "failure-injection-finished")
    result = runtime.split(source)
    assert_coverage(result, source)
    assert partial.exists() and sha(partial) == prefix_hash
    assert len(list(Path(result["manifest_path"]).parent.glob("attempt-*"))) == 2


def test_explicit_submit_is_idempotent_and_verified_backup_gated(runtime):
    source = runtime.root / "inbox/meeting.wav"
    fixture_audio(source)
    runtime.start()
    result = runtime.split(source, "--submit")
    submitted = result["submission"]["segments"]
    assert len(submitted) == 2
    for entry in submitted.values():
        assert entry["admitted"] and Path(entry["path"]).is_file()
    # Fake S3 accepts uploads but reports wrong size: actual verifier refuses
    # ASR, even though the held derivations and queued copies are all intact.
    (runtime.root / "bad-backup").touch()
    runtime.enable()
    wait_for(lambda: len(runtime.rows("SELECT * FROM items WHERE state='failed'")) == 2)
    assert not list(runtime.root.glob("*.started"))
    assert not runtime.rows("SELECT * FROM backups")
    repeated = runtime.split(source, "--submit")
    assert repeated["reused"] and repeated["submission"] == result["submission"]
    assert len(list((runtime.root / "inbox").glob("split-*.wav"))) == 2
    (runtime.root / "bad-backup").rename(runtime.root / "bad-backup-finished")
    for entry in submitted.values():
        runtime.cli("retry", entry["item_id"], check=0)
    wait_for(lambda: len(runtime.rows("SELECT * FROM items WHERE state='complete'")) == 2)
    assert len(list(runtime.root.glob("*.started"))) == 2
    for row in runtime.rows("SELECT * FROM backups"):
        assert row["verified_at"] and "size+md5" in row["method"]
    # Queued copies have now moved. Repeating submission still never copies
    # them again or resets a completed identity, and held outputs remain.
    final = runtime.split(source, "--submit")
    assert final["submission"] == result["submission"]
    assert len(runtime.rows("SELECT * FROM items WHERE state='complete'")) == 2
    assert not list((runtime.root / "inbox").glob("split-*.wav"))
    assert_coverage(final, source)


def test_direct_adapter_also_settles_under_shared_item_lock(runtime):
    source = runtime.root / "inbox/a-direct.wav"
    fixture_audio(source)
    runtime.start()
    runtime.evaluate(f"from wax import ledger;from pathlib import Path;ledger.upsert_item(Path({str(source)!r}))")
    item = runtime.rows("SELECT item_id FROM items")[0]["item_id"]
    (runtime.root / "block-child").touch()
    (runtime.root / "direct-adapter-test").touch()
    direct = runtime.evaluate("from wax import transcribe_adapter;from pathlib import Path;"
                              f"print(transcribe_adapter.transcribe(Path({str(source)!r})))", background=True)
    wait_for(lambda: (runtime.root / (item + ".started")).exists())
    pending = runtime.split(item, "--wait-seconds", "0", check=2)
    assert pending["status"] == "pending"
    (runtime.root / (item + ".release")).touch()
    stdout, stderr = direct.communicate(timeout=15)
    assert direct.returncode == 0, stdout + stderr
    result = runtime.split(item)
    assert result["processing"]["historical_transcript"]
    runtime.cli("transcribe", source, check=2)


def test_cli_offline_and_invalid_targets_never_become_worker(runtime):
    offline = runtime.split("unavailable", check=2)
    assert "unavailable" in offline["error"]
    runtime.start()
    for target in ("/tmp/not-owned.wav", str(runtime.root / "dropoff/audio.wav"),
                   str(runtime.root / "stream/seg-00000.ogg")):
        refused = runtime.split(target, check=2)
        assert refused["status"] == "failed" and "owned audio" in refused["error"]
    for wait in ("-1", "nan", "inf", "3601"):
        assert runtime.split("unavailable", "--wait-seconds", wait, check=2)["status"] == "failed"
