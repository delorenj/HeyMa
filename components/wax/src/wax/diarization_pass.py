import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

from . import component, frontmatter, ledger


def build_result(md, item_id):
    row = ledger.connect().execute(
        "SELECT i.path,i.sha256,t.asr_path,t.body_sha256 FROM items i JOIN transcripts t USING(item_id) WHERE item_id=?",
        (item_id,),
    ).fetchone()
    if not row or not row["asr_path"]:
        raise RuntimeError("timed ASR is unavailable; do not repeat Whisper to repair diarization")
    asr = json.loads(Path(row["asr_path"]).read_text())
    if asr.get("version") != 1 or asr.get("source_sha256") != row["sha256"]:
        raise RuntimeError("timed ASR source identity mismatch")
    _, body = frontmatter.read(md)
    digest = hashlib.sha256(body.encode()).hexdigest()
    if digest != row["body_sha256"]:
        return {"wax_ep_version": 1, "state": "skipped", "reason_code": "human_edits"}
    script = component.ROOT.parents[1] / "scripts" / "transcribe.py"
    spec = importlib.util.spec_from_file_location("wax_asr_renderer", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    turns, device = module.diarize_local(row["path"], "cuda")
    if not turns or not device or not device.startswith("cuda"):
        raise RuntimeError("Sortformer did not produce CUDA speaker turns")
    rendered = module.to_markdown(asr["result"], row["path"], asr.get("timestamps", False), turns, True, "cuda", device)
    _, replacement = frontmatter.split(rendered)
    return {"wax_ep_version": 1,
            "frontmatter": {"diarized": True, "diarization-requested": True,
                            "diarization-device-requested": "cuda", "diarization-device": device},
            "body_replace": {"sha256": digest, "text": replacement}}


def main(md, item_id):
    python = Path(os.environ.get("DIARIZATION_PYTHON") or
                  (component.ROOT.parents[1] / ".venv-diarization/bin/python"))
    source = component.ROOT / "src"
    command = [str(python), "-c",
               "import sys,json; sys.path.insert(0,sys.argv[1]); "
               "from pathlib import Path; from wax.diarization_pass import build_result; "
               "print(json.dumps(build_result(Path(sys.argv[2]),sys.argv[3])))",
               str(source), str(md), item_id]
    result = subprocess.run(command, text=True, capture_output=True, timeout=86400)
    if result.returncode:
        raise RuntimeError((result.stderr or "diarization child failed")[-800:])
    return json.loads(result.stdout.splitlines()[-1])
