"""The junk gate (wax/junk.py): voicemails, "Hello?" calls, one-word misfires and silent clips never reach the
vault, the transcripts table, the store or a single enrichment pass.

The real worker, adapter, ledger and gate run end to end in a temp WAX_ROOT. Only the edges are faked:
- `bin/transcribe` is a small shell script (WAX_TRANSCRIBE) that copies a canned transcript to the --output path
  it is given and logs every invocation, so "the transcriber was never invoked" is a fact, not an inference;
- S3 (archive.archive / remote_size), ffprobe (sanity), the desktop and the finalizer are stubs;
- `passes.run_auto` and `transcriptions.record_transcription` are spies, so "no pass ran" and "nothing reached the
  store" are asserted directly.

Every rejection must also keep the source audio byte for byte (backup-first, never delete).
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from wax import calls, junk, ledger, passes, paths, sanity, state, transcribe_adapter, transcriptions, worker

UUID = "0e1b6c1c-2f64-4f6a-9d3e-0123456789ab"
VM_NAME = f"telnyx-vm-2026-11-16-{UUID}-1763300000.wav"
CALL_NAME = f"telnyx-2026-11-16-{UUID}-1763300000.mp3"
MEMO_NAME = "20261116-120000-memo.ogg"
AUDIO_BYTES = b"OggS-not-really-audio-but-precious-\x00\x01\x02" * 8
SANITY_OK = {"ok": True, "reason_code": None, "audio_duration_s": 10.0, "asr_duration_s": 10.0,
             "duration_ratio": 1.0}

FAKE_TRANSCRIBE = """#!/bin/sh
printf '%s\\n' "$1" >> "$FAKE_TRANSCRIBE_LOG"
audio="$1"; shift
out=""
while [ $# -gt 0 ]; do
  case "$1" in -o|--output) out="$2"; shift ;; esac
  shift
done
if [ -z "$out" ]; then
  stem=$(basename "$audio"); stem="${stem%.*}"
  out="$WAX_VAULT/$stem.md"
fi
cp "$FAKE_TRANSCRIPT" "$out"
echo "Written: $out" >&2
echo 'Transcription-Metadata: {"duration_seconds": 10, "word_count": 1, "diarized": false, "model": "fake"}' >&2
echo "$out"
"""


def words(n: int) -> str:
    return " ".join(f"word{i}" for i in range(n))


def note(spoken: str, name: str = "x.ogg") -> str:
    return f"# Transcription: {name}\n\n**Speaker 1 (00:00):** {spoken}\n" if spoken else f"# Transcription: {name}\n\n"


def _close_test_connection() -> None:
    connection = getattr(ledger._local, "conn", None)
    if connection is not None:
        connection.close()
        del ledger._local.conn


class JunkGateHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="junkgate-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name)) / "waxroot"
        self.vault = Path(os.path.realpath(self.tmp.name)) / "vault"
        self.vault.mkdir(parents=True)
        for d in ("inbox", "archive", "skipped", "var", "var/logs"):
            (self.root / d).mkdir(parents=True)
        self.transcribe_log = self.root / "transcribe-calls.log"
        self.transcript = self.root / "canned-transcript.md"
        self.fake = self.root / "fake-transcribe"
        self.fake.write_text(FAKE_TRANSCRIBE)
        self.fake.chmod(0o755)

        self.run_auto = MagicMock(return_value=[])
        self.record = MagicMock()
        self.sanity_verdict = dict(SANITY_OK)
        _close_test_connection()
        env = {"WAX_TRANSCRIBE": str(self.fake), "FAKE_TRANSCRIBE_LOG": str(self.transcribe_log),
               "FAKE_TRANSCRIPT": str(self.transcript), "WAX_VAULT": str(self.vault)}
        self.env = patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop("WAX_JUNK_GATE", None)
        self.patches = [
            patch.object(paths, "AUDIO", self.root), patch.object(paths, "INBOX", self.root / "inbox"),
            patch.object(paths, "ARCHIVE", self.root / "archive"),
            patch.object(paths, "SKIPPED", self.root / "skipped"),
            patch.object(paths, "RECOVERED", self.root / "recovered"),
            patch.object(paths, "VAR", self.root / "var"), patch.object(paths, "DB", self.root / "var/wax.db"),
            patch.object(paths, "LOGS", self.root / "var/logs"), patch.object(paths, "VAULT", self.vault),
            patch.object(state, "CLAIM_FILE", self.root / "var/inbox.claim"),
            patch.object(worker.dropoff, "copy_received"),
            patch.object(worker, "desktop"), patch.object(transcribe_adapter, "desktop"),
            patch.object(worker.archive, "archive", side_effect=lambda p, item_id=None: {
                "s3_key": f"2026-11-16/{p.name}", "bytes": p.stat().st_size}),
            patch.object(worker.archive, "remote_size", side_effect=lambda key: next(
                (p.stat().st_size for p in (self.root / "inbox").rglob("*") if p.is_file()), 0)),
            patch.object(transcribe_adapter.archive, "references", return_value=[]),
            patch.object(sanity, "probe_duration", return_value=None),
            patch.object(sanity, "check", side_effect=lambda audio, meta: dict(self.sanity_verdict)),
            patch.object(passes, "run_auto", self.run_auto),
            patch.object(transcriptions, "record_transcription", self.record),
            patch.object(worker.finalize, "publish_pass_events", return_value={}),
            patch.object(worker.finalize, "finalize", return_value={"finalized": True}),
            patch.object(worker, "_link_archive", return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.env.stop()
        _close_test_connection()

    # -- helpers
    def drop(self, name: str, spoken_or_note: str, *, raw: bool = False, payload: bytes = AUDIO_BYTES) -> Path:
        """Put `name` in the inbox and set what the fake transcriber will 'hear'.

        Items are content-addressed: a second drop of the same bytes IS a re-drop (RedropTest), so a test that
        drops several different recordings gives each its own payload.
        """
        audio = self.root / "inbox" / name
        audio.write_bytes(payload)
        self.transcript.write_text(spoken_or_note if raw else note(spoken_or_note, name))
        self.item = ledger.upsert_item(audio)
        self.assertRegex(self.item, r"^[0-9a-f]{16}$")
        return audio

    def run_once(self):
        return worker.run_once()

    def transcriber_calls(self) -> list[str]:
        return self.transcribe_log.read_text().splitlines() if self.transcribe_log.exists() else []

    def state_of(self, item=None) -> tuple[str, str]:
        item = item or self.item
        st = ledger.connect().execute("SELECT state FROM items WHERE item_id=?", (item,)).fetchone()["state"]
        t = ledger.connect().execute(
            "SELECT cause_code FROM transitions WHERE machine='item' AND subject=? ORDER BY seq DESC LIMIT 1",
            (item,)).fetchone()
        return st, t["cause_code"]

    def transcript_rows(self) -> int:
        return ledger.connect().execute("SELECT COUNT(*) c FROM transcripts").fetchone()["c"]

    def vault_files(self) -> list[str]:
        return sorted(p.name for p in self.vault.rglob("*") if p.is_file())

    def assert_nothing_published(self):
        self.assertEqual(self.vault_files(), [])
        self.assertEqual(self.transcript_rows(), 0)
        self.record.assert_not_called()
        self.run_auto.assert_not_called()

    def assert_rejected(self, audio: Path, cause: str, *, asr: bool):
        """The shared postcondition of every rejection."""
        parked = self.root / "skipped" / "junk" / audio.name
        self.assertTrue(parked.is_file(), "the audio must be parked in skipped/junk/")
        self.assertEqual(parked.read_bytes(), AUDIO_BYTES, "source audio must survive byte for byte")
        self.assertFalse(audio.exists(), "and must leave the inbox")
        self.assertEqual(self.state_of(), ("skipped", cause))
        row = ledger.connect().execute("SELECT path FROM items WHERE item_id=?", (self.item,)).fetchone()
        self.assertEqual(row["path"], str(parked))
        self.assert_nothing_published()
        rejected = self.root / "var/logs" / self.item / "rejected.md"
        self.assertEqual(rejected.is_file(), asr, "rejected.md exists exactly when ASR ran")
        self.assertEqual(len(self.transcriber_calls()), 1 if asr else 0)
        # No staged note is left behind either.
        self.assertEqual([p.name for p in (self.root / "var/logs" / self.item).glob("*.md")] if asr else [],
                         ["rejected.md"] if asr else [])


class RejectionTest(JunkGateHarness):
    def test_telnyx_voicemail_is_rejected_before_asr(self):
        audio = self.drop(VM_NAME, words(80))     # even a long voicemail: structure, not length
        result = self.run_once()
        self.assertEqual(result["skipped"]["reason"], "junk_voicemail")
        self.assertEqual(result["skipped"]["stage"], "before ASR")
        self.assert_rejected(audio, "junk_voicemail", asr=False)
        self.assertEqual(self.transcriber_calls(), [])

    def test_call_row_saying_voicemail_is_rejected_before_asr(self):
        audio = self.drop(CALL_NAME, words(80))
        with patch.object(calls, "call_for_audio", return_value={"outcome": "voicemail"}):
            result = self.run_once()
        self.assertEqual(result["skipped"]["stage"], "before ASR")
        self.assert_rejected(audio, "junk_voicemail", asr=False)

    def test_one_word_dictation_is_rejected_after_asr(self):
        audio = self.drop(MEMO_NAME, "Hello?")
        result = self.run_once()
        self.assertEqual(result["skipped"]["reason"], "junk_too_short")
        self.assertEqual(result["skipped"]["stage"], "after ASR")
        self.assertEqual(result["skipped"]["words"], 1)
        self.assert_rejected(audio, "junk_too_short", asr=True)
        self.assertIn("Hello?", (self.root / "var/logs" / self.item / "rejected.md").read_text())
        self.assertNotIn("transcribe", result)    # no transcribe result, so nothing downstream saw one

    def test_short_telnyx_call_is_call_noise(self):
        audio = self.drop(CALL_NAME, words(12))
        result = self.run_once()
        self.assertEqual(result["skipped"]["reason"], "junk_call_noise")
        self.assert_rejected(audio, "junk_call_noise", asr=True)

    def test_call_noise_boundary_is_twenty_words(self):
        audio = self.drop(CALL_NAME, words(20))
        self.run_once()
        self.assert_rejected(audio, "junk_call_noise", asr=True)

    def test_twenty_one_word_call_passes(self):
        self.drop(CALL_NAME, words(21))
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.run_auto.assert_called_once_with(self.item)
        self.assertEqual(len(self.vault_files()), 1)

    def test_telnyx_call_opening_on_a_voicemail_greeting_is_rejected_at_any_length(self):
        greeting = "You've reached Jarad. Please leave a message after the tone. "
        audio = self.drop(CALL_NAME, greeting + words(60))      # 70 words: not call-noise, only the greeting
        result = self.run_once()
        self.assertEqual(result["skipped"]["reason"], "junk_voicemail")
        self.assertEqual(result["skipped"]["stage"], "after ASR")
        self.assert_rejected(audio, "junk_voicemail", asr=True)

    def test_a_dictation_quoting_a_greeting_is_not_a_voicemail(self):
        quote = "Note to self, the greeting says: You've reached Jarad. Please leave a message after the tone. "
        self.drop(MEMO_NAME, quote + words(30))
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.run_auto.assert_called_once()

    def test_zero_word_note_is_empty(self):
        audio = self.drop(MEMO_NAME, "")
        result = self.run_once()
        self.assertEqual(result["skipped"]["reason"], "junk_empty")
        self.assert_rejected(audio, "junk_empty", asr=True)

    def test_speaker_label_alone_is_empty(self):
        audio = self.drop(MEMO_NAME, "# Transcription: x.ogg\n\n**Speaker 1 (00:00):**\n\n---\n", raw=True)
        self.run_once()
        self.assert_rejected(audio, "junk_empty", asr=True)

    def test_a_test_utterance_is_rejected(self):
        audio = self.drop(MEMO_NAME, "this is a test of the microphone")
        result = self.run_once()
        self.assertEqual(result["skipped"]["reason"], "junk_no_content")
        self.assert_rejected(audio, "junk_no_content", asr=True)

    def test_every_rejection_keeps_the_audio_byte_identical(self):
        cases = [(VM_NAME, words(5), "junk_voicemail", False), (MEMO_NAME, "Hello?", "junk_too_short", True),
                 (CALL_NAME, words(9), "junk_call_noise", True)]
        for i, (name, spoken, cause, asr) in enumerate(cases):
            with self.subTest(name=name):
                self.transcribe_log.unlink(missing_ok=True)
                payload = AUDIO_BYTES + name.encode()
                audio = self.drop(name, spoken, payload=payload)
                self.record.reset_mock()
                self.run_once()
                self.assertEqual((self.root / "skipped/junk" / audio.name).read_bytes(), payload)
                self.assertEqual(self.state_of(), ("skipped", cause))
        self.assertEqual(self.vault_files(), [])
        self.run_auto.assert_not_called()


class PassThroughTest(JunkGateHarness):
    def test_five_word_dictation_passes_and_is_enriched(self):
        audio = self.drop(MEMO_NAME, "remember to buy more coffee")
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.assertEqual(len(self.vault_files()), 1)
        self.assertTrue(self.vault_files()[0].endswith("-memo.md"))
        note_path = Path(result["transcribe"]["md_path"])
        self.assertEqual(note_path.parent, self.vault)
        self.assertIn("remember to buy more coffee", note_path.read_text())
        self.assertEqual(self.transcript_rows(), 1)
        self.record.assert_called_once()
        self.run_auto.assert_called_once_with(self.item)
        self.assertFalse(audio.exists())
        self.assertTrue(Path(result["parked"]).is_file())
        self.assertEqual(Path(result["parked"]).read_bytes(), AUDIO_BYTES)
        self.assertNotEqual(self.state_of()[0], "skipped")
        # Staged note was moved, not copied: nothing but the logs stays under var/logs.
        self.assertEqual([p.name for p in (self.root / "var/logs" / self.item).glob("*.md")], [])
        self.assertFalse((self.root / "skipped" / "junk").exists())

    def test_four_word_dictation_is_the_shortest_that_passes(self):
        self.drop(MEMO_NAME, "call mom after lunch")
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.run_auto.assert_called_once()

    def test_long_phone_call_passes(self):
        self.drop(CALL_NAME, words(400))
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.assertEqual(len(self.vault_files()), 1)
        self.run_auto.assert_called_once_with(self.item)
        self.assertEqual(len(self.transcriber_calls()), 1)

    def test_a_long_note_that_mentions_voicemail_is_untouched(self):
        self.drop(CALL_NAME, "we should talk about the voicemail greeting and the mailbox settings " + words(60))
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.run_auto.assert_called_once()

    def test_call_row_with_a_real_outcome_does_not_block(self):
        self.drop(CALL_NAME, words(60))
        with patch.object(calls, "call_for_audio", return_value={"outcome": "answered"}):
            result = self.run_once()
        self.assertNotIn("skipped", result)
        self.run_auto.assert_called_once()

    def test_gate_off_lets_a_one_word_item_through(self):
        self.drop(MEMO_NAME, "Hello?")
        with patch.dict(os.environ, {"WAX_JUNK_GATE": "0"}):
            result = self.run_once()
        self.assertNotIn("skipped", result)
        self.assertEqual(len(self.vault_files()), 1)
        self.run_auto.assert_called_once()
        self.assertEqual(self.transcript_rows(), 1)

    def test_gate_off_lets_a_voicemail_name_be_transcribed(self):
        self.drop(VM_NAME, words(30))
        for off in ("0", "false", "no", "off", " OFF "):
            with self.subTest(off=off), patch.dict(os.environ, {"WAX_JUNK_GATE": off}):
                self.assertFalse(junk.enabled())
        with patch.dict(os.environ, {"WAX_JUNK_GATE": "off"}):
            self.assertFalse(junk.enabled())
            result = self.run_once()
        self.assertNotIn("skipped", result)
        self.assertEqual(len(self.transcriber_calls()), 1)
        self.assertEqual(len(self.vault_files()), 1)

    def test_gate_on_by_default_and_for_unrecognised_values(self):
        self.assertTrue(junk.enabled())
        with patch.dict(os.environ, {"WAX_JUNK_GATE": "1"}):
            self.assertTrue(junk.enabled())
        with patch.dict(os.environ, {"WAX_JUNK_GATE": ""}):
            self.assertTrue(junk.enabled())


class SanityGateOrderTest(JunkGateHarness):
    def test_a_sanity_failure_stays_suspect_not_junk(self):
        self.sanity_verdict = {**SANITY_OK, "ok": False, "reason_code": "duration_mismatch",
                               "asr_duration_s": 1.0, "duration_ratio": 0.1}
        audio = self.drop(MEMO_NAME, "Hello?")      # would be junk, but the sanity gate speaks first
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.assertIn("SANITY GATE", result["error"])
        self.assertEqual(self.vault_files(), [])
        suspects = list((self.root / "var/logs" / self.item).glob("output-*/transcript.suspect.md"))
        self.assertEqual(len(suspects), 1)
        self.assertEqual(self.state_of(), ("suspect", "duration_mismatch"))
        self.assertTrue(audio.is_file(), "a suspect item's audio stays visible in the inbox")
        self.assertEqual(audio.read_bytes(), AUDIO_BYTES)
        self.assertFalse((self.root / "var/logs" / self.item / "rejected.md").exists())
        self.assertFalse((self.root / "skipped" / "junk").exists())
        self.assertEqual(self.transcript_rows(), 0)
        self.record.assert_not_called()
        self.run_auto.assert_not_called()

    def test_a_suspect_note_with_a_real_body_is_quarantined_in_the_vault_too(self):
        self.sanity_verdict = {**SANITY_OK, "ok": False, "reason_code": "audio_unprobeable"}
        self.drop(MEMO_NAME, words(50))
        self.run_once()
        self.assertEqual(self.vault_files(), [])
        self.assertEqual(len(list((self.root / "var/logs" / self.item).glob("output-*/transcript.suspect.md"))), 1)
        self.assertEqual(self.state_of()[0], "suspect")


class RedropTest(JunkGateHarness):
    def redrop(self, name: str, content: bytes = AUDIO_BYTES) -> Path:
        copy = self.root / "inbox" / name
        copy.write_bytes(content)
        return copy

    def test_redropped_copy_of_a_pre_asr_rejection_is_parked_and_never_reprocessed(self):
        self.drop(VM_NAME, words(5))
        self.run_once()
        self.assertEqual(self.state_of(), ("skipped", "junk_voicemail"))
        copy = self.redrop(VM_NAME)
        self.assertEqual(worker.next_item(), (self.item, copy, "park-junk"))
        result = self.run_once()
        self.assertEqual(result["reason"], "junk_redropped")
        dup = self.root / "skipped/junk/duplicates" / VM_NAME
        self.assertEqual(Path(result["parked"]), dup)
        self.assertEqual(dup.read_bytes(), AUDIO_BYTES)
        self.assertFalse(copy.exists())
        self.assertEqual((self.root / "skipped/junk" / VM_NAME).read_bytes(), AUDIO_BYTES)  # the original too
        self.assertEqual(self.transcriber_calls(), [])
        self.assertEqual(self.state_of(), ("skipped", "junk_redropped"))
        self.assert_nothing_published()
        self.assertIsNone(self.run_once())          # and the inbox is clean: nothing left to do

    def test_redropped_copy_of_an_after_asr_rejection_is_not_transcribed_again(self):
        self.drop(MEMO_NAME, "Hello?")
        self.run_once()
        self.assertEqual(len(self.transcriber_calls()), 1)
        self.redrop("20261116-130000-memo-again.ogg")           # same bytes, another name
        result = self.run_once()
        self.assertEqual(result["reason"], "junk_redropped")
        self.assertEqual(len(self.transcriber_calls()), 1)
        self.assertTrue((self.root / "skipped/junk/duplicates/20261116-130000-memo-again.ogg").is_file())
        self.assert_nothing_published()

    def test_repeated_redrops_never_clobber_each_other(self):
        self.drop(VM_NAME, words(5))
        self.run_once()
        for _ in range(3):
            self.redrop(VM_NAME)
            self.assertEqual(self.run_once()["reason"], "junk_redropped")
        parked = sorted(p.name for p in (self.root / "skipped/junk/duplicates").iterdir())
        self.assertEqual(len(parked), 3)
        for p in (self.root / "skipped/junk/duplicates").iterdir():
            self.assertEqual(p.read_bytes(), AUDIO_BYTES)

    def test_a_skipped_item_that_is_not_junk_is_left_alone(self):
        audio = self.drop(MEMO_NAME, words(30))
        ledger.set_item_state(self.item, "skipped", cause="file_too_large_for_transcription", evidence="test")
        # Another copy of the same bytes: the oversize policy owns this item, the junk gate must not touch it.
        other = self.root / "elsewhere.ogg"
        other.write_bytes(AUDIO_BYTES)
        ledger.connect().execute("UPDATE items SET path=? WHERE item_id=?", (str(other), self.item))
        self.assertIsNone(worker.next_item())
        self.assertTrue(audio.is_file())

    def test_a_rejected_item_still_in_its_own_place_is_not_its_own_duplicate(self):
        audio = self.drop(MEMO_NAME, words(30))
        ledger.set_item_state(self.item, "skipped", cause="junk_too_short", evidence="test")
        # items.path == the inbox file itself: this is the item, not a re-dropped copy.
        self.assertIsNone(worker.next_item())
        self.assertTrue(audio.is_file())


class FailOpenTest(JunkGateHarness):
    def test_calls_lookup_raising_does_not_block_a_real_call(self):
        self.drop(CALL_NAME, words(80))
        with patch.object(calls, "call_for_audio", side_effect=RuntimeError("ledger locked")):
            result = self.run_once()
        self.assertNotIn("skipped", result)
        self.assertEqual(len(self.vault_files()), 1)
        self.run_auto.assert_called_once()

    def test_audio_identity_raising_does_not_block(self):
        self.drop(MEMO_NAME, words(40))
        with patch.object(calls, "audio_identity", side_effect=RuntimeError("boom")):
            result = self.run_once()
        self.assertNotIn("skipped", result)
        self.run_auto.assert_called_once()

    def test_the_structural_name_rule_does_not_need_the_calls_ledger(self):
        audio = self.drop(VM_NAME, words(3))
        with patch.object(calls, "call_for_audio", side_effect=RuntimeError("ledger locked")), \
                patch.object(calls, "audio_identity", side_effect=RuntimeError("boom")):
            self.run_once()
        self.assert_rejected(audio, "junk_voicemail", asr=False)

    def test_unreadable_or_missing_note_is_not_junk(self):
        audio = self.root / "inbox" / MEMO_NAME
        audio.write_bytes(AUDIO_BYTES)
        self.assertIsNone(junk.after_asr(self.root / "no-such-note.md", audio))
        note_path = self.root / "unreadable.md"
        note_path.write_bytes(b"\xff\xfe not utf-8")
        self.assertIsNone(junk.after_asr(note_path, audio))
        with patch("wax.frontmatter.split", side_effect=ValueError("bad yaml")):
            self.assertIsNone(junk.after_asr(note_path, audio))

    def test_gate_off_short_circuits_both_stages(self):
        md = self.root / "n.md"
        md.write_text(note(""))
        with patch.dict(os.environ, {"WAX_JUNK_GATE": "0"}):
            self.assertIsNone(junk.before_asr(Path(VM_NAME)))
            self.assertIsNone(junk.after_asr(md, Path(MEMO_NAME)))
        self.assertIsNotNone(junk.before_asr(Path(VM_NAME)))
        self.assertEqual(junk.after_asr(md, Path(MEMO_NAME)).cause, "junk_empty")

    def test_only_a_leading_telnyx_vm_prefix_is_structural(self):
        self.assertIsNone(junk.before_asr(Path("20261116-memo-about-telnyx-vm-setup.ogg")))
        self.assertIsNone(junk.before_asr(Path(CALL_NAME)))


class StagingContractTest(JunkGateHarness):
    def test_waxd_stages_the_note_in_a_private_per_attempt_directory(self):
        self.drop(MEMO_NAME, "remember to buy more coffee")
        with patch.object(transcribe_adapter.fence, "run_tracked", wraps=transcribe_adapter.fence.run_tracked) as run:
            self.run_once()
        cmd = run.call_args.args[1]
        staged = Path(cmd[cmd.index("--output") + 1])
        self.assertEqual(staged.name, "transcript.md")
        self.assertTrue(staged.parent.name.startswith("output-"))
        self.assertEqual(staged.parent.parent, self.root / "var/logs" / self.item)
        self.assertEqual(run.call_args.kwargs["env"]["TRANSCRIPTS"], str(staged.parent))
        self.assertEqual(run.call_args.kwargs["env"]["WAX_VAULT"], str(staged.parent))

    def test_a_caller_supplied_output_cannot_escape_private_output(self):
        audio = self.root / "inbox" / MEMO_NAME
        audio.write_bytes(AUDIO_BYTES)
        explicit = self.root / "elsewhere" / "mine.md"
        with patch.object(transcribe_adapter.fence, "run_tracked") as run:
            with self.assertRaisesRegex(transcribe_adapter.TranscribeError, "caller-supplied output"):
                transcribe_adapter.transcribe(audio, item_id="0123456789abcdef",
                                               extra=["--output", str(explicit)])
        run.assert_not_called()
        self.assertFalse(explicit.exists())

    def test_has_output_arg_spellings(self):
        for args in (["-o", "x"], ["--output", "x"], ["--output=x"], ["-ox"]):
            self.assertTrue(transcribe_adapter._has_output_arg(args), args)
        for args in ([], ["--asr-output", "x"], ["--no-diarization"]):
            self.assertFalse(transcribe_adapter._has_output_arg(args), args)

    def test_move_out_never_overwrites(self):
        src, dst = self.root / "a.md", self.vault / "a.md"
        dst.write_text("older note")
        src.write_text("newer note")
        landed = transcribe_adapter._move_out(src, dst)
        self.assertEqual(dst.read_text(), "older note")
        self.assertEqual(landed.read_text(), "newer note")
        self.assertNotEqual(landed, dst)

    def test_move_out_copy_move_fallback_across_filesystems(self):
        from wax import rename as rn
        src, dst = self.root / "b.md", self.vault / "b.md"
        src.write_text("note")
        with patch.object(rn, "move_noclobber", side_effect=OSError(18, "Invalid cross-device link")):
            landed = transcribe_adapter._move_out(src, dst)
        self.assertEqual(landed, dst)
        self.assertEqual(dst.read_text(), "note")
        self.assertFalse(src.exists())


class ReviewFixesTest(JunkGateHarness):
    """Regressions for the adversarial reviews of the staging move and the gate rules."""

    # D1: a failed vault publish must fail the item, not record a note that sits in var/logs.
    def test_failed_vault_publish_fails_the_item_and_keeps_the_staged_note(self):
        audio = self.drop(MEMO_NAME, words(30))
        with patch.object(transcribe_adapter, "_move_out", side_effect=OSError(28, "No space left on device")):
            result = self.run_once()
        self.assertIn("could not publish", result["error"])
        self.assertEqual(self.state_of(), ("failed", "transcribe_failed"))
        self.assertEqual(self.vault_files(), [])
        self.assertEqual(self.transcript_rows(), 0)
        self.record.assert_not_called()
        self.run_auto.assert_not_called()
        self.assertTrue(audio.is_file(), "audio stays in the inbox so wax retry works")
        staged = list((self.root / "var/logs" / self.item).glob("output-*/transcript.md"))
        self.assertEqual(len(staged), 1)
        self.assertIn("word0", staged[0].read_text())
        # and the operator's retry works once the vault is writable again
        worker.retry_item(self.item)
        result = self.run_once()
        self.assertNotIn("error", result)
        self.assertEqual(len(self.vault_files()), 1)
        self.assertEqual(self.transcript_rows(), 1)

    def test_a_caller_supplied_vault_output_is_refused_before_spawning(self):
        audio = self.root / "inbox" / MEMO_NAME
        audio.write_bytes(AUDIO_BYTES)
        inside = self.vault / "mine.md"
        with patch.object(transcribe_adapter.fence, "run_tracked") as run:
            with self.assertRaisesRegex(transcribe_adapter.TranscribeError, "caller-supplied output"):
                transcribe_adapter.transcribe(audio, item_id="0123456789abcdef", extra=["-o", str(inside)])
        run.assert_not_called()
        self.assertFalse(inside.exists())

    # D2 (data safety): a repeat sanity failure must stay a *.suspect.md, never read as a note.
    def test_a_second_sanity_failure_of_the_same_stem_is_still_a_suspect_note(self):
        self.sanity_verdict = {**SANITY_OK, "ok": False, "reason_code": "audio_unprobeable"}
        self.drop(MEMO_NAME, words(30))
        self.run_once()
        worker.retry_item(self.item)
        self.run_once()
        self.assertEqual(self.vault_files(), [])
        suspects = list((self.root / "var/logs" / self.item).glob("output-*/transcript.suspect.md"))
        self.assertEqual(len(suspects), 2)
        self.assertTrue(all(p.read_text() for p in suspects))

    # D2 (ops): empty text from a long recording is an outage, not junk.
    def test_empty_transcript_from_long_audio_fails_visibly_instead_of_being_skipped(self):
        self.sanity_verdict = {**SANITY_OK, "audio_duration_s": 600.0, "asr_duration_s": 600.0}
        audio = self.drop(MEMO_NAME, "")
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.assertIn("EMPTY TRANSCRIPT", result["error"])
        self.assertEqual(self.state_of(), ("failed", "transcribe_failed"))
        self.assertTrue(audio.is_file())
        self.assertEqual(audio.read_bytes(), AUDIO_BYTES)
        self.assertFalse((self.root / "skipped" / "junk").exists())
        self.assert_nothing_published()

    def test_empty_transcript_from_a_short_clip_is_still_junk(self):
        self.sanity_verdict = {**SANITY_OK, "audio_duration_s": 5.0, "asr_duration_s": 5.0}
        audio = self.drop(MEMO_NAME, "")
        self.run_once()
        self.assert_rejected(audio, "junk_empty", asr=True)

    # D3: a false positive can be recovered, and is not judged again.
    def test_wax_retry_releases_a_junk_rejection_and_the_gate_stays_off_for_it(self):
        audio = self.drop(MEMO_NAME, "Hello?")
        self.run_once()
        self.assertEqual(self.state_of(), ("skipped", "junk_too_short"))
        out = worker.retry_item(self.item)
        self.assertEqual(out["state"], "pending")
        self.assertTrue(audio.is_file(), "audio is back in the inbox")
        self.assertEqual(audio.read_bytes(), AUDIO_BYTES)
        self.assertFalse((self.root / "skipped/junk" / audio.name).exists())
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.assertEqual(len(self.vault_files()), 1)
        self.assertEqual(self.transcript_rows(), 1)
        self.run_auto.assert_called_once_with(self.item)

    def test_wax_retry_releases_a_pre_asr_voicemail_rejection_too(self):
        self.drop(VM_NAME, words(30))
        self.run_once()
        self.assertEqual(self.transcriber_calls(), [])
        worker.retry_item(self.item)
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.assertEqual(len(self.transcriber_calls()), 1)

    def test_retry_still_refuses_a_skipped_item_that_is_not_junk(self):
        self.drop(MEMO_NAME, words(30))
        ledger.set_item_state(self.item, "skipped", cause="file_too_large_for_transcription", evidence="test")
        with self.assertRaises(RuntimeError):
            worker.retry_item(self.item)

    # D4: the test-utterance rule must not eat real dictations.
    def test_dictations_that_merely_contain_a_test_phrase_survive(self):
        for i, text in enumerate(("If it works then we ship tomorrow morning",
                                  "I need to count to ten before I reply to him")):
            with self.subTest(text=text):
                self.run_auto.reset_mock()
                self.drop(f"2026111{i}-120000-memo.ogg", text, payload=AUDIO_BYTES + bytes([i]))
                result = self.run_once()
                self.assertNotIn("skipped", result)
                self.run_auto.assert_called_once_with(self.item)

    def test_a_test_phrase_that_leaves_too_few_words_is_still_too_short(self):
        audio = self.drop(MEMO_NAME, "count to ten")
        self.run_once()
        self.assert_rejected(audio, "junk_too_short", asr=True)

    def test_a_short_call_with_a_test_phrase_is_call_noise(self):
        audio = self.drop(CALL_NAME, "if it works then we ship tomorrow morning")
        self.run_once()
        self.assert_rejected(audio, "junk_call_noise", asr=True)

    def test_a_test_opener_is_still_rejected(self):
        for i, text in enumerate(("This is a test", "let's see if this works", "this is a test of the mic")):
            with self.subTest(text=text):
                audio = self.drop(f"2026111{i}-130000-memo.ogg", text, payload=AUDIO_BYTES + bytes([i]))
                self.run_once()
                self.assertEqual(ledger.connect().execute(
                    "SELECT state FROM items WHERE item_id=?", (self.item,)).fetchone()["state"], "skipped")
                self.assertTrue((self.root / "skipped/junk" / audio.name).is_file())

    # D5: unspaced scripts are not one word.
    def test_unspaced_cjk_dictation_is_not_too_short(self):
        self.drop(MEMO_NAME, "明日の朝九時に会議があるので資料を準備しておいてください")
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.run_auto.assert_called_once()

    def test_a_tiny_cjk_utterance_is_still_junk(self):
        audio = self.drop(MEMO_NAME, "こんにちは")
        self.run_once()
        self.assert_rejected(audio, "junk_too_short", asr=True)


if __name__ == "__main__":
    unittest.main()
