#!/usr/bin/env python3
"""Fresh HEYMA-18 acceptance evidence; run through the recorded scratch clean-env.

All runtime paths must be beneath the PM profile scratch directory. Reuses the
committed audio/systemd/transcriber/pass seams, NOT a production microphone or S3.
The local mc seam exercises archive verification but is explicitly not S3 proof;
the 2026-11-13 committed MinIO trace supplies that independent evidence.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[6]
COMPONENT = REPO / "components/wax"
RUN = Path(os.environ["TMPDIR"]).parent.resolve()
SCRATCH = Path("/home/delorenj/.hermes/profiles/heyma-pm/cache/scratch")
assert RUN.is_relative_to(SCRATCH), RUN
V = RUN / "integration"
ROOT = V / "root"
HISTORICAL = COMPONENT / "docs/evidence/heyma-18/validation-2026-11-13/harness"
for directory in ("home", "tmp", "root", "vault", "voices", "logs", "fixtures",
                  "stubbin", "passes.d", "pipelines.d", "local-object-seam"):
    (V / directory).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(COMPONENT / "tests"))
sys.path.insert(0, str(COMPONENT / "src"))
import split_fixtures


def save(name, obj):
    (V / "logs" / name).write_text(json.dumps(obj, indent=2, default=str) + "\n")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def wait(predicate, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("condition timed out")


def rows(sql, args=()):
    with sqlite3.connect(f"file:{ROOT}/var/wax.db?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute(sql, args)]


def proc(pid):
    path = Path(f"/proc/{pid}")
    try:
        stat = (path / "stat").read_text()
        fields = stat[stat.rindex(")") + 2:].split()
        return {"pid": pid, "ppid": int(fields[1]), "state": fields[0],
                "starttick": fields[19],
                "argv": (path / "cmdline").read_bytes().replace(b"\0", b" ").decode()}
    except FileNotFoundError:
        return {"pid": pid, "exited": True}


for tool in ("ffprobe", "nice", "bash", "sh", "sleep", "cat", "date", "env",
             "mkdir", "mv", "cp", "kill", "grep", "sed", "awk", "sha256sum",
             "basename", "dirname", "tr", "head", "tail", "wc", "touch", "tee",
             "readlink", "seq", "ls", "id", "true", "false", "test", "flock",
             "stat", "ln", "chmod", "uname", "hostname"):
    location = shutil.which(tool)
    if location:
        (V / "stubbin" / tool).symlink_to(location)
(V / "stubbin/python3").symlink_to(sys.executable)
for tool in ("ffmpeg", "systemd-run", "pactl", "notify-send", "canberra-gtk-play"):
    dest = V / f"stub-{tool}"
    dest.write_text((HISTORICAL / f"stub-{tool}").read_text().replace(
        "/tmp/heyma-validate-hm18", str(V)))
    dest.chmod(0o755)
    (V / "stubbin" / tool).symlink_to(dest)
for tool in ("fake-transcribe", "fx-stamp", "stub-frontmatters"):
    dest = V / tool
    dest.write_text((HISTORICAL / tool).read_text().replace(
        "/tmp/heyma-validate-hm18", str(V)))
    dest.chmod(0o755)
(V / "passes.d/fx-stamp.yaml").write_text(
    (HISTORICAL / "passes.d/fx-stamp.yaml").read_text().replace("@V@", str(V)))

env = {
    "PATH": str(V / "stubbin"), "HOME": str(V / "home"), "TMPDIR": str(V / "tmp"),
    "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "HEYMA_VALIDATE": str(V),
    "WAX_ROOT": str(ROOT), "WAX_VAULT": str(V / "vault"), "TRANSCRIPTS": str(V / "vault"),
    "WAX_VOICES_DIR": str(V / "voices"), "WAX_VOICE_NOTIFY": "0", "WAX_VOICE_CONTEXT": "0",
    "WAX_VOICE_NTFY_ALERT": str(V / "no-notify"), "WAX_NATS_HOST": "127.0.0.1",
    "WAX_NATS_PORT": "1", "WAX_S3_ENDPOINT": "http://127.0.0.1:1",
    "MC_CONFIG_DIR": str(V / "mc"), "WAX_TRANSCRIPTS_UNIT_FILE": str(V / "no-unit"),
    "WAX_HARVEST_DIR": "off", "WAX_FRONTMATTERS": str(V / "stub-frontmatters"),
    "WAX_TRANSCRIBE": str(V / "fake-transcribe"), "WAX_PASSES_DIR": str(V / "passes.d"),
    "WAX_PIPELINES_DIR": str(V / "pipelines.d"), "WAX_DICTIONARY": str(ROOT / "Dictionary.yaml"),
    "WAX_DOC_HYGIENE_FOLDERS": str(V / "hygiene"), "WAX_DOCUMENT_PDF_INTAKE": "0",
    "WAX_DIARIZATION": "0", "WAX_SEGMENT_SECONDS": "10", "WAX_MIN_FREE_BYTES": "1",
    "TELEPHONY_STATE_DIR": str(V / "telephony"), "TELEPHONY_CONFIG": str(V / "telephony.json"),
    "TRANSCRIBE_STASH_DIR": str(V / "stash"), "TRANSCRIBE_LOCK_FILE": str(V / "transcribe.lock"),
    "TRANSCRIBE_FAST_LOCK_FILE": str(V / "fast.lock"),
}
save("00-environment.json", env)
assert all(not env.get(key) for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"))
for name in ("HOME", "TMPDIR", "WAX_ROOT", "WAX_VAULT", "WAX_VOICES_DIR"):
    assert Path(env[name]).is_relative_to(RUN)


def cli(label, *args, expected=0):
    argv = [sys.executable, str(COMPONENT / "bin/wax"), "--json", *args]
    result = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=180)
    record = {"command": shlex.join(argv), "exit": result.returncode,
              "stdout": result.stdout, "stderr": result.stderr}
    save(label + ".json", record)
    print(label, "exit", result.returncode, flush=True)
    assert result.returncode == expected, record
    return json.loads(result.stdout)


specs = {}
for name in ("meeting_overrun_quiet", "continuous_activity", "all_quiet", "meeting_with_pauses"):
    cached = V / "fixtures" / f"{name}.fixture.json"
    specs[name] = (json.loads(cached.read_text()) if cached.exists()
                   else split_fixtures.build(name, V / "fixtures"))
    specs[name]["sha256"] = sha(V / "fixtures" / f"{name}.ogg")
save("01-fixtures.json", specs)
for name in specs:
    audio = V / "fixtures" / f"{name}.ogg"
    original = sha(audio)
    # The CLI resolves registered items or unregistered files inside inbox,
    # not arbitrary external paths. Register these retained source fixtures
    # as complete before analysis; no worker or transcription is involved.
    register = [
        sys.executable, "-c",
        f"import sys;sys.path.insert(0,{str(COMPONENT / 'src')!r});"
        "from wax import ledger;"
        "i=ledger.upsert_item(__import__('pathlib').Path(sys.argv[1]),origin='acceptance_fixture');"
        "ledger.set_item_state(i,'complete',cause='acceptance_fixture',evidence='waveform-only fixture')",
        str(audio),
    ]
    subprocess.run(register, env=env, check=True)
    if name == "meeting_overrun_quiet":
        proposed = cli("02-quiet-dry-run", "split", str(audio), "--dry-run")
        cut = cli("03-quiet-split", "split", str(audio))
        manifest = json.loads(Path(cut["manifest"]).read_text())
        assert proposed["detection"]["boundary"]["cut_s"] == 640
        assert cut["detection"]["boundary"]["cut_s"] == 640
        assert manifest["source"]["sha256"] == original
        assert all(sha(Path(cut["held_dir"]) / p["file"]) == p["sha256"]
                   for p in manifest["segments"])
    else:
        manifests = sorted(ROOT.glob("skipped/split/*/split.json"))
        result = cli("04-" + name, "split", str(audio), expected=3)
        assert result["outcome"] == "no_boundary" and result["changes"] == "none"
        assert sorted(ROOT.glob("skipped/split/*/split.json")) == manifests
    assert sha(audio) == original
assert not list((V / "vault").glob("*.md"))
assert not (V / "logs/transcriber.log").exists()
assert not (V / "logs/pass.log").exists()
save("05-waveform-checks.json", {"checks": "all assertions passed", "detector_inputs": "waveform only",
                                "transcriber_started": False, "passes_started": False})

# Explicitly labelled transport seam; archive.py still computes and verifies
# size and single-part MD5, and records its normal backups/state transitions.
mc = V / "local-mc-seam"
mc.write_text("""#!""" + sys.executable + """
import hashlib,json,os,shutil,sys,time
from pathlib import Path
v=Path(os.environ["HEYMA_VALIDATE"]); args=sys.argv[1:]
with (v/"logs/local-object-seam.jsonl").open("a") as f:
 f.write(json.dumps({"time_ns":time.time_ns(),"argv":args})+"\\n")
def path(target):
 p=v/"local-object-seam"/target
 assert p.resolve().is_relative_to(v/"local-object-seam")
 return p
if args[0]=="cp":
 src=Path(args[-2]); dst=path(args[-1]); dst.parent.mkdir(parents=True,exist_ok=True)
 shutil.copy2(src,dst)
elif args[0] in ("stat","ls"):
 p=path(args[-1])
 if not p.is_file(): sys.exit(1)
 print(json.dumps({"status":"success","type":"file","key":p.name,"size":p.stat().st_size,
 "etag":hashlib.md5(p.read_bytes(),usedforsecurity=False).hexdigest()}))
elif args[0]=="cat":
 p=path(args[-1])
 if not p.is_file(): sys.exit(1)
 print(p.read_text())
elif args[0]!="tag": sys.exit(2)
""")
mc.chmod(0o755)
launcher = V / "launch-waxd.py"
launcher.write_text(
    "import runpy,sys\n"
    f"sys.path.insert(0, {str(COMPONENT / 'src')!r})\n"
    "from wax import archive\n"
    f"archive.MC = {str(mc)!r}  # LOCAL TRANSPORT SEAM, not S3\n"
    f"runpy.run_path({str(COMPONENT / 'bin/waxd')!r}, run_name='__main__')\n")
daemon_log = (V / "logs/waxd.txt").open("w")
daemon = subprocess.Popen([sys.executable, str(launcher)], env=env, stdout=daemon_log,
                          stderr=subprocess.STDOUT)
encoder = None
try:
    wait(lambda: (ROOT / "var/state.json").exists())
    status = wait(lambda: json.loads((ROOT / "var/state.json").read_text()).get("daemon"))
    assert status["pid"] == daemon.pid and "item-fence/1" in status["capabilities"]
    save("10-daemon-owner.json", {"daemon": proc(daemon.pid), "advertisement": status})
    cli("10-pipeline-enable", "pipeline", "enable")
    source = V / "fixtures/meeting_overrun_quiet.ogg"
    for prefix in ("slow", "stubborn", "passslow"):
        # Distinct encoded identities, same deterministic waveform/timeline.
        dest = ROOT / "inbox" / f"{prefix}-meeting.ogg"
        pending = V / "fixtures" / f"{prefix}-meeting.ogg"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-c", "copy",
                        "-metadata", f"comment={prefix}", str(pending)], check=True, env=env)
        digest = sha(pending)
        shutil.copy2(pending, dest)
        item_id = wait(lambda: (rows("SELECT item_id FROM items WHERE sha256=?", (digest,)) or [{}])[0].get("item_id"))
        child = wait(lambda: next((r for r in rows("SELECT * FROM item_children WHERE item_id=?", (item_id,))
                                   if (r["argv0"] == "fx-stamp") == (prefix == "passslow")), None))
        before = rows("SELECT * FROM items WHERE item_id=?", (item_id,))
        owner = proc(child["pid"])
        assert owner["ppid"] == daemon.pid, owner
        if prefix == "slow":
            unrelated = ROOT / "inbox/unrelated-talk.ogg"
            # A new content identity, not the previously registered waveform
            # fixture (otherwise the ordinary duplicate policy parks it).
            subprocess.run(["ffmpeg", "-v", "error", "-i",
                            str(V / "fixtures/continuous_activity.ogg"), "-c", "copy",
                            "-metadata", "comment=unrelated", str(unrelated)],
                           check=True, env=env)
        result = cli(f"11-{prefix}-split", "split", item_id[:12])
        assert result["processing"]["state_after"] == "skipped"
        assert result["processing"]["fenced"] and result["halted_children"]
        assert sha(result["processing"]["original"]) == digest
        assert not rows("SELECT * FROM item_children WHERE item_id=?", (item_id,))
        assert rows("SELECT state FROM items WHERE item_id=?", (item_id,))[0]["state"] == "skipped"
        assert not rows("SELECT * FROM completions WHERE item_id=?", (item_id,))
        if prefix == "slow":
            wait(lambda: rows("SELECT item_id FROM items WHERE orig_name='unrelated-talk.ogg' AND state='complete'"))
        if prefix == "stubborn":
            assert list((ROOT / "recovered/split-late").rglob("*stubborn*"))
            assert not list((V / "vault").glob("*stubborn*"))
        save(f"12-{prefix}-verified.json", {"before": before, "child_owner": owner,
             "child_after": proc(child["pid"]), "after": rows("SELECT * FROM items WHERE item_id=?", (item_id,)),
             "fence": rows("SELECT * FROM item_fences WHERE item_id=?", (item_id,)),
             "passes": rows("SELECT * FROM passes WHERE item_id=?", (item_id,)),
             "transitions": rows("SELECT * FROM transitions WHERE machine='item' AND subject=?", (item_id,)),
             "original_sha256": digest, "all_assertions": "passed"})

    # The real capture CLI + encoder; only input device and systemd scope are seams.
    shutil.copy2(source, V / "fixtures/live-source.ogg")
    started = cli("20-live-start", "rec", "start", "--label", "acceptance-live")
    encoder = started["pid"]
    save("21-live-encoder-owner.json", proc(encoder))
    wait(lambda: len(list((ROOT / "stream" / f"{started['started']}.segs").glob("*.ogg"))) >= 12)
    early = cli("22-live-no-boundary", "split", expected=3)
    assert early["capture"]["state_after"] == "recording"
    wait(lambda: len(list((ROOT / "stream" / f"{started['started']}.segs").glob("*.ogg"))) >= 110)
    preview = cli("23-live-dry-run", "split", "--dry-run")
    assert preview["capture"]["state_after"] == "recording"
    result = cli("24-live-split", "split")
    assert result["capture"]["state_before"] == "recording"
    assert result["capture"]["state_after"] == "ready"
    assert not list((ROOT / "stream").glob("*.rec.json"))
    manifest = json.loads(Path(result["manifest"]).read_text())
    assert sha(manifest["original_path"]) == manifest["source"]["sha256"]
    assert all(sha(Path(result["held_dir"]) / p["file"]) == p["sha256"]
               for p in manifest["segments"])
    assert not list((ROOT / "inbox").glob("*acceptance-live*"))
    assert not list((V / "vault").glob("*acceptance-live*"))
    save("25-live-verified.json", {"encoder_after": proc(encoder), "manifest": manifest,
         "capture_not_restarted": True, "all_assertions": "passed"})
    save("30-final-ledger.json", {"items": rows("SELECT * FROM items"),
         "fences": rows("SELECT * FROM item_fences"), "segments": rows("SELECT * FROM split_segments"),
         "backups": rows("SELECT * FROM backups"), "children": rows("SELECT * FROM item_children")})
    save("31-result.json", {"assertions": "passed", "S3": "not contacted; local mc seam",
         "production": "not contacted", "daemon": proc(daemon.pid)})
finally:
    if encoder:
        # Only this scratch capture, via the normal orderly finalizer if needed.
        if list((ROOT / "stream").glob("*.rec.json")):
            cli("99-cleanup-capture", "rec", "stop")
    daemon.terminate()
    daemon.wait(timeout=30)
    daemon_log.close()
    save("99-daemon-exit.json", {"exit": daemon.returncode})
print("ACCEPTANCE_ASSERTIONS_PASSED", flush=True)
