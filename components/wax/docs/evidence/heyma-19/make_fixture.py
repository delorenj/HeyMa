"""Build the isolated scratch registry the heyma-19 smoke run drives. Usage: make_fixture.py <scratch-dir>

Everything lives under <scratch-dir>: WAX_ROOT (ledger), WAX_VOICES_DIR, WAX_VAULT, generated tone audio and
a fake player that logs the clip it is given and "plays" for 3.737 s. Nothing here touches the real runtime.
"""
import os
import subprocess
import sys
from pathlib import Path

scratch = Path(sys.argv[1]).resolve()
for name, sub in (("WAX_ROOT", "root"), ("WAX_VAULT", "vault"), ("TRANSCRIPTS", "vault"), ("WAX_VOICES_DIR", "voices")):
    os.environ[name] = str(scratch / sub)
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import numpy as np  # noqa: E402

from wax import voiceprint, voices  # noqa: E402

scratch.mkdir(parents=True, exist_ok=True)
(scratch / "voices").mkdir(exist_ok=True)


def tone(path, seconds, freq, codec="pcm_s16le", fmt="wav"):
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}", "-c:a", codec, "-f", fmt, str(path)], check=True)


tone(scratch / "meeting.wav", 60, 330)
tone(scratch / "call.wav", 40, 550)
tone(scratch / "voices" / "voice-b00002.ogg", 13, 880, "libopus", "ogg")
(scratch / "meeting.md").write_text(
    "# Meeting\n\n**Speaker 1 (00:00):** hi\n\n**Speaker 2 (00:03):** a long turn\n\n**Speaker 1 (00:15):** reply\n\n"
    "**Speaker 2 (00:24):** another turn\n\n**Speaker 1 (00:33):** reply\n\n**Speaker 2 (00:40):** third\n\n"
    "**Speaker 1 (00:52):** bye\n")
fake = scratch / "fake-player.sh"
fake.write_text(f'#!/bin/sh\necho "$(date +%T.%N) $$ $1" >> {scratch}/player.log\nexec sleep 3.737\n')
fake.chmod(0o755)

conn = voices.connect()
for item, audio, md, dur in (("item-meeting", "meeting.wav", "meeting.md", 60.0), ("item-call", "call.wav", None, 40.0)):
    conn.execute("INSERT INTO items (item_id, sha256, path, first_seen, updated_at) VALUES (?,?,?,?,?)",
                 (item, "0" * 64, str(scratch / audio), "t", "t"))
    if md:
        conn.execute("INSERT INTO transcripts (item_id, md_path, audio_duration, created_at) VALUES (?,?,?,?)",
                     (item, str(scratch / md), dur, "t"))


def make(vid, seed, seconds, item, label, turns, name=None):
    vec = voiceprint.normalize(np.random.default_rng(seed).standard_normal(voiceprint.DIM).astype(np.float32))
    note = (f"---\nvoice-id: {vid}\nname: {name or ''}\nstatus: {'named' if name else 'unnamed'}\n---\n\n"
            f"# Unnamed voice `{vid}`\n")
    voices.register(conn, item_id=item, label=label, domain="room", seconds=seconds, vec=vec, name=name, note=note,
                    voice_id=vid)
    voices.record_sighting(conn, item_id=item, label=label, rendered=label, voice_id=vid, verdict="matched",
                           score=0.9, runner_up=None, domain="room", seconds=seconds, vec=None, turns=turns)


make("voice-a00001", 1, 4200, "item-meeting", "Speaker 2", ["00:03", "00:24", "00:40"])
make("voice-b00002", 2, 900, "item-call", "Speaker 4", None)
make("voice-c00003", 3, 1800, "item-meeting", "Speaker 7", ["00:15"], name="Chase")
make("voice-d00004", 4, 300, "item-call", "Speaker 8", None)
print("fixture ready:", scratch)
