import os
import sys
import tempfile
import subprocess
import importlib.util
import shutil
from types import SimpleNamespace
from importlib.machinery import SourceFileLoader
import unittest
from pathlib import Path
from unittest.mock import patch


COMPONENT_ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
sys.path.insert(0, str(COMPONENT_ROOT / "src"))

from wax import transcribe_adapter


def load_systemd_installer():
    path = COMPONENT_ROOT / "deploy/install-systemd-user"
    loader = SourceFileLoader("wax_systemd_installer", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PortabilityTest(unittest.TestCase):
    def test_transcriber_uses_configured_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "custom-transcribe"
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
            with patch.dict(os.environ, {"WAX_TRANSCRIBE": str(executable)}):
                self.assertEqual(transcribe_adapter.transcribe_command(), executable.resolve())

    def test_transcriber_falls_back_to_path(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            transcribe_adapter.shutil, "which", return_value="/bin/sh"
        ):
            self.assertEqual(transcribe_adapter.transcribe_command(), Path("/bin/sh").resolve())

    def test_transcriber_reports_missing_command(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            transcribe_adapter.shutil, "which", return_value=None
        ):
            with self.assertRaisesRegex(transcribe_adapter.TranscribeError, "WAX_TRANSCRIBE"):
                transcribe_adapter.transcribe_command()

    def test_pipeline_enables_diarization_by_default_with_explicit_opt_out(self):
        with patch.dict(os.environ, {}, clear=True):
            env = transcribe_adapter.transcribe_env(Path("/tmp/wax-test.log"))
            self.assertNotIn("DIARIZATION_VENV", env)
        with patch.dict(os.environ, {"WAX_DIARIZATION": "0", "DIARIZATION_VENV": "/opt/diar"}, clear=True):
            env = transcribe_adapter.transcribe_env(Path("/tmp/wax-test.log"))
            self.assertIn(".diarization-disabled", env["DIARIZATION_VENV"])

    def test_in_band_transcription_metadata(self):
        stderr = 'noise\nTranscription-Metadata: {"duration_seconds": 12.5, "diarized": true}\n'
        self.assertEqual(transcribe_adapter.parse_metadata(stderr)["duration_seconds"], 12.5)

    def test_progress_parser_does_not_misreport_diarization_as_asr_99(self):
        legacy = "  [ 99%] 02:58:19 / 180m 0s\n  Running diarization (cpu)...\n"
        self.assertEqual(transcribe_adapter.parse_progress(legacy), {"stage": "diarize"})
        structured = (
            'Transcription-Progress: {"stage":"diarize","percent":37,'
            '"chunks_done":370,"chunks_total":1000}\n'
        )
        self.assertEqual(
            transcribe_adapter.parse_progress(structured),
            {"stage": "diarize", "progress_pct": 37},
        )

    def test_adapter_dings_at_process_start_and_successful_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "audio.ogg"
            md = root / "derived.md"
            audio.write_bytes(b"audio")
            md.write_text("# transcript\n")
            metadata = (
                'Transcription-Metadata: {"duration_seconds": 1, "word_count": 1, '
                '"diarized": true, "model": "large-v3"}\n'
            )
            connection = SimpleNamespace(execute=lambda *_args, **_kwargs: None)
            verdict = {"ok": True, "reason_code": None, "audio_duration_s": 1.0,
                       "asr_duration_s": 1.0, "duration_ratio": 1.0}
            with patch.object(transcribe_adapter, "transcribe_command", return_value=Path("/bin/true")), \
                    patch.object(transcribe_adapter.subprocess, "run", return_value=SimpleNamespace(
                        returncode=0, stdout=str(md) + "\n", stderr=metadata)), \
                    patch.object(transcribe_adapter.sanity, "check", return_value=verdict), \
                    patch.object(transcribe_adapter.sanity, "source_unchanged", return_value=True), \
                    patch.object(transcribe_adapter, "vault_name", return_value=md.name), \
                    patch.object(transcribe_adapter.paths, "VAULT", root), \
                    patch.object(transcribe_adapter.paths, "LOGS", root / "logs"), \
                    patch.object(transcribe_adapter.ledger, "connect", return_value=connection), \
                    patch.object(transcribe_adapter.ledger, "set_item_state"), \
                    patch.object(transcribe_adapter.desktop, "ding") as ding:
                transcribe_adapter.transcribe(audio, item_id="item")
            self.assertEqual([call.args[0] for call in ding.call_args_list], ["start", "complete"])

    def test_systemd_template_is_relocation_safe(self):
        template = (COMPONENT_ROOT / "deploy/systemd/user/waxd.service").read_text()
        self.assertNotIn("%h/HeyMa", template)
        self.assertIn("@WAX_EXEC_START@", template)
        self.assertIn("@WAX_DOCUMENTATION_URI@", template)

    def test_systemd_installer_renders_current_checkout(self):
        installer = COMPONENT_ROOT / "deploy/install-systemd-user"
        result = subprocess.run(
            [str(installer), "--dry-run"],
            cwd="/tmp",
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn(str(COMPONENT_ROOT.parents[1] / "bin/waxd"), result.stdout)
        self.assertIn(str(COMPONENT_ROOT / "docs/WAX-DESIGN.md"), result.stdout)
        self.assertNotIn("@WAX_", result.stdout)

    def test_systemd_render_handles_spaces_and_special_characters(self):
        installer = load_systemd_installer()
        with tempfile.TemporaryDirectory(prefix="Hey Ma #100% $") as directory:
            repo = Path(directory)
            component = repo / "components/wax"
            template_dir = component / "deploy/systemd/user"
            template_dir.mkdir(parents=True)
            source = COMPONENT_ROOT / "deploy/systemd/user/waxd.service"
            (template_dir / "waxd.service").write_text(source.read_text())
            executable = repo / "bin/waxd"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            rendered = installer.render(component, repo)
            uri = (component / "docs/WAX-DESIGN.md").as_uri()
            self.assertIn(f'Documentation={uri.replace("%", "%%")}', rendered)
            self.assertIn("%%20", rendered)
            self.assertIn("%%23", rendered)
            self.assertIn("%%25", rendered)
            self.assertIn('ExecStart="', rendered)
            self.assertIn("Hey Ma #100%% \\x24", rendered)
            self.assertNotIn("@WAX_", rendered)
            verifier = shutil.which("systemd-analyze")
            if verifier:
                unit = repo / "waxd.service"
                unit.write_text(rendered)
                result = subprocess.run(
                    [verifier, "--user", "verify", str(unit)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
