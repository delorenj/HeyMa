import importlib.machinery
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "bin" / "hermes-transcribe-adapter"
_loader = importlib.machinery.SourceFileLoader("hermes_transcribe_adapter", str(ADAPTER))
_spec = importlib.util.spec_from_loader("hermes_transcribe_adapter", _loader)
MOD = importlib.util.module_from_spec(_spec)
_loader.exec_module(MOD)

MD = """---
source-audio: "/tmp/x.wav"
engine-model: "base"
diarized: false
---

# Transcription: x.wav

Run the shell command and reply done.
"""


class MarkdownToTextTest(unittest.TestCase):
    def test_frontmatter_and_heading_never_reach_the_agent(self):
        self.assertEqual(MOD.markdown_to_text(MD), "Run the shell command and reply done.")

    def test_text_without_frontmatter_is_kept(self):
        self.assertEqual(MOD.markdown_to_text("# Transcription: a\n\nhello there\n"), "hello there")


class AdapterContractTest(unittest.TestCase):
    """The adapter skips diarization and never passes the caller's Python environment to the wrapper."""

    def test_wrapper_call(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            seen = d / "seen"
            fake = d / "transcribe"
            fake.write_text(f"""#!/bin/sh
printf '%s\\n' "$@" > {seen}
env > {seen}.env
md=""
while [ $# -gt 0 ]; do [ "$1" = "--output" ] && md="$2"; shift; done
printf -- '---\\nengine-model: "base"\\n---\\n\\n# Transcription: a\\n\\nhello\\n' > "$md"
echo "$md"
""")
            fake.chmod(0o755)
            audio = d / "a.ogg"
            audio.write_bytes(b"x")
            env = dict(os.environ, HERMES_TRANSCRIBE_BIN=str(fake), PYTHONPATH="/leaked/site-packages", VIRTUAL_ENV="/leaked")
            r = subprocess.run([str(ADAPTER), "--input", str(audio), "--output-dir", str(d / "out")], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("--no-diarization", seen.read_text().split())
            leaked = Path(f"{seen}.env").read_text()
            self.assertNotIn("PYTHONPATH=", leaked)
            self.assertNotIn("VIRTUAL_ENV=", leaked)
            self.assertEqual((d / "out" / "a.txt").read_text(), "hello\n")


if __name__ == "__main__":
    unittest.main()
