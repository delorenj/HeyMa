"""Silent audio is detected by MEASUREMENT, for every source, before any GPU time.

Born from three Voice Memos recorded with a dead input (mean -77..-87 dB) that ran a full ASR pass,
came back as an empty transcript, and failed as `transcribe_failed` ("is the ASR down?") with a retry
that could never succeed. The class is keyed on the measured level (sanity.audio_levels), never on a
name, an id or an origin: every intake source converges in worker._process_item.

ffmpeg builds the fixtures (anullsrc / sine); the worker, adapter, ledger and junk gate run for real in a
temp WAX_ROOT (the harness is the junk gate's).
"""

import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import junk_gate_test as jg
from wax import desktop, junk, ledger, sanity, worker

HAVE_FFMPEG = shutil.which("ffmpeg") is not None

SILENT = {"mean_db": -79.5, "max_db": -40.1, "silent": True}
AUDIBLE = {"mean_db": -25.3, "max_db": -3.2, "silent": False}


def ffmpeg_audio(tmp: Path, name: str, graph: str, *, fmt: str = "wav") -> Path:
    """Render a lavfi graph to a real audio file."""
    out = tmp / name
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", graph,
                    str(out.with_suffix("." + fmt))], check=True)
    return out.with_suffix("." + fmt)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
class AudioLevelsTest(unittest.TestCase):
    """sanity.audio_levels against real ffmpeg output."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix="levels-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_digital_silence_is_silent(self):
        f = ffmpeg_audio(self.dir, "null", "anullsrc=r=16000:cl=mono:d=3")
        levels = sanity.audio_levels(f)
        self.assertTrue(levels["silent"])
        self.assertLessEqual(levels["mean_db"], sanity.SILENT_MEAN_DB)
        self.assertLessEqual(levels["max_db"], sanity.SILENT_MAX_DB)

    def test_a_tone_is_audible(self):
        f = ffmpeg_audio(self.dir, "tone", "sine=frequency=440:duration=3")
        levels = sanity.audio_levels(f)
        self.assertFalse(levels["silent"])
        self.assertGreater(levels["mean_db"], -40)
        self.assertGreater(levels["max_db"], sanity.SILENT_MAX_DB)

    def test_a_very_quiet_recording_with_a_real_peak_is_not_silent(self):
        # mean below the floor, but one loud moment: someone did speak. Both bounds must hold.
        with patch.object(sanity.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, "", "[Parsed_volumedetect_0 @ 0x1] mean_volume: -71.2 dB\n"
                           "[Parsed_volumedetect_0 @ 0x1] max_volume: -12.0 dB\n")
            levels = sanity.audio_levels(Path("x.ogg"))
        self.assertEqual(levels, {"mean_db": -71.2, "max_db": -12.0, "silent": False})

    def test_inf_levels_parse_as_the_floor(self):
        with patch.object(sanity.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, "", "mean_volume: -inf dB\nmax_volume: -inf dB\n")
            levels = sanity.audio_levels(Path("x.ogg"))
        self.assertTrue(levels["silent"])
        self.assertEqual(levels["mean_db"], -120.0)

    def test_stereo_with_one_dead_channel_is_judged_as_one_mix_and_is_audible(self):
        # a Telnyx call: our side speaks, the other leg is digital silence
        f = ffmpeg_audio(self.dir, "onesided",
                         "sine=frequency=440:duration=3[a];anullsrc=r=44100:cl=mono:d=3[b];[a][b]amerge=inputs=2")
        self.assertFalse(sanity.audio_levels(f)["silent"])

    def test_stereo_with_both_channels_dead_is_silent(self):
        f = ffmpeg_audio(self.dir, "bothdead", "anullsrc=r=44100:cl=stereo:d=3")
        self.assertTrue(sanity.audio_levels(f)["silent"])

    def test_fails_open_when_the_file_is_not_audio(self):
        junk_file = self.dir / "garbage.ogg"
        junk_file.write_bytes(b"not audio at all")
        self.assertIsNone(sanity.audio_levels(junk_file))

    def test_fails_open_when_ffmpeg_is_missing_or_times_out(self):
        for exc in (FileNotFoundError("ffmpeg"), subprocess.TimeoutExpired("ffmpeg", 1)):
            with self.subTest(exc=type(exc).__name__), patch.object(sanity.subprocess, "run", side_effect=exc):
                self.assertIsNone(sanity.audio_levels(Path("x.ogg")))


class FormatDurationTest(unittest.TestCase):
    def test_reads_like_a_person_says_it(self):
        self.assertEqual(sanity.format_duration(4748.2), "79m 8s")
        self.assertEqual(sanity.format_duration(42), "42s")
        self.assertEqual(sanity.format_duration(None), "unknown length")
        self.assertEqual(sanity.format_duration(3600 * 4 + 120), "4h 02m")


class SilentParkingTest(jg.JunkGateHarness):
    """Hard silence: archived, then parked in skipped/silent/ with no ASR run."""

    def silent(self, levels=SILENT, duration=4748.2):
        return (patch.object(sanity, "audio_levels", return_value=levels),
                patch.object(sanity, "probe_duration", return_value=duration))

    def run_with(self, levels=SILENT, duration=4748.2):
        a, b = self.silent(levels, duration)
        with a, b:
            return self.run_once()

    def test_silent_audio_is_archived_then_parked_and_never_transcribed(self):
        audio = self.drop(jg.MEMO_NAME, jg.words(30))
        result = self.run_with()
        self.assertEqual(result["skipped"]["reason"], "junk_silent")
        parked = self.root / "skipped" / "silent" / audio.name
        self.assertEqual(parked.read_bytes(), jg.AUDIO_BYTES, "audio survives byte for byte")
        self.assertFalse(audio.exists())
        self.assertFalse((self.root / "skipped" / "junk").exists())
        self.assertEqual(self.state_of(), ("skipped", "junk_silent"))
        self.assertEqual(self.transcriber_calls(), [], "no GPU run for silent audio")
        self.assert_nothing_published()
        # backup-first: S3 was verified (archived transition) before the park
        causes = [r["cause_code"] for r in ledger.connect().execute(
            "SELECT cause_code FROM transitions WHERE machine='item' AND subject=? ORDER BY seq", (self.item,))]
        self.assertLess(causes.index("s3_verified"), causes.index("junk_silent"))
        row = ledger.connect().execute("SELECT * FROM items WHERE item_id=?", (self.item,)).fetchone()
        self.assertEqual((row["audio_mean_db"], row["audio_max_db"]), (-79.5, -40.1))
        self.assertEqual(row["path"], str(parked))

    def test_evidence_names_the_levels_the_cause_and_the_next_step(self):
        self.drop(jg.MEMO_NAME, jg.words(30))
        self.run_with()
        evidence = ledger.describe_failure(self.item)["evidence"]
        self.assertIn("silent audio: mean -79.5 dB, max -40.1 dB over 79m 8s", evidence)
        self.assertIn("speech is about -25 dB", evidence)
        self.assertIn("check the mic/input", evidence)
        self.assertIn("parked at ", evidence)
        self.assertIn("skipped/silent/", evidence)
        self.assertIn(f"wax retry {self.item} forces transcription", evidence)

    def test_the_operator_is_told_once_per_source_in_plain_words(self):
        self.drop(jg.MEMO_NAME, jg.words(30))
        self.run_with()
        notify = worker.desktop.notify_silent_recording
        notify.assert_called_once()
        source, body = notify.call_args.args
        self.assertEqual(source, "manual")
        for needle in (jg.MEMO_NAME, "79m 8s", "-79.5 dB", "-40.1 dB", "Check the input", "skipped/silent/"):
            self.assertIn(needle, body)
        worker.desktop.ding.assert_not_called()      # not a Wax fault: no error chime
        worker.desktop.notify.assert_not_called()

    def test_wax_retry_releases_it_and_the_silence_gate_stays_off_for_that_item(self):
        audio = self.drop(jg.MEMO_NAME, jg.words(30))
        self.run_with()
        out = worker.retry_item(self.item)
        self.assertEqual((out["state"], out["previous_cause"], out["junk_override"]),
                         ("pending", "junk_silent", True))
        self.assertTrue(audio.is_file(), "audio is back in the inbox")
        self.assertEqual(audio.read_bytes(), jg.AUDIO_BYTES)
        self.assertFalse((self.root / "skipped/silent" / audio.name).exists())
        self.assertEqual(self.state_of(), ("pending", "junk_override"))
        result = self.run_with()         # still measures silent: the override wins
        self.assertNotIn("skipped", result)
        self.assertEqual(len(self.transcriber_calls()), 1, "the operator forced transcription")
        self.assertEqual(len(self.vault_files()), 1)

    def test_silence_gate_respects_the_global_junk_switch(self):
        self.drop(jg.MEMO_NAME, jg.words(30))
        with patch.dict("os.environ", {"WAX_JUNK_GATE": "0"}):
            result = self.run_with()
        self.assertNotIn("skipped", result)
        self.assertEqual(len(self.transcriber_calls()), 1)

    def test_audible_audio_is_untouched_and_rearms_the_alarm(self):
        self.drop(jg.MEMO_NAME, jg.words(30))
        result = self.run_with(AUDIBLE, 600.0)
        self.assertNotIn("skipped", result)
        self.assertEqual(len(self.transcriber_calls()), 1)
        worker.desktop.clear_silent_source.assert_called_once_with("manual")
        worker.desktop.notify_silent_recording.assert_not_called()

    def test_unmeasurable_audio_fails_open_to_the_normal_path(self):
        self.drop(jg.MEMO_NAME, jg.words(30))
        result = self.run_with(None)
        self.assertNotIn("skipped", result)
        self.assertEqual(len(self.transcriber_calls()), 1)
        row = ledger.connect().execute("SELECT audio_mean_db FROM items WHERE item_id=?", (self.item,)).fetchone()
        self.assertIsNone(row["audio_mean_db"])

    def test_before_asr_junk_rules_still_come_first(self):
        # a silent Telnyx voicemail is a voicemail: the cheap structural rule answers before ffmpeg runs
        self.drop(jg.VM_NAME, jg.words(5))
        a, b = self.silent()
        with a as levels, b:
            result = self.run_once()
        self.assertEqual(result["skipped"]["reason"], "junk_voicemail")
        levels.assert_not_called()

    def test_a_redropped_copy_of_silent_audio_is_parked_beside_it(self):
        self.drop(jg.MEMO_NAME, jg.words(30))
        self.run_with()
        copy = self.root / "inbox" / "again.ogg"
        copy.write_bytes(jg.AUDIO_BYTES)
        result = self.run_with()
        self.assertEqual(result["reason"], "junk_redropped")
        self.assertEqual(Path(result["parked"]), self.root / "skipped/silent/duplicates/again.ogg")
        self.assertFalse((self.root / "skipped/junk").exists())
        # and the item is still releasable as what it was
        self.assertEqual(worker.retry_item(self.item)["previous_cause"], "junk_silent")

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
    def test_end_to_end_with_real_ffmpeg_audio(self):
        silent_wav = ffmpeg_audio(self.root, "src", "anullsrc=r=16000:cl=mono:d=2").read_bytes()
        audio = self.drop(jg.MEMO_NAME, jg.words(30), payload=silent_wav)
        result = self.run_once()
        self.assertEqual(result["skipped"]["reason"], "junk_silent")
        self.assertEqual((self.root / "skipped/silent" / audio.name).read_bytes(), silent_wav)
        self.assertEqual(self.transcriber_calls(), [])

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
    def test_telnyx_call_with_one_silent_channel_is_not_silent(self):
        onesided = ffmpeg_audio(
            self.root, "call", "sine=frequency=440:duration=2[a];anullsrc=r=44100:cl=mono:d=2[b];[a][b]amerge=inputs=2",
            fmt="wav").read_bytes()
        self.drop(jg.CALL_NAME, jg.words(60), payload=onesided)
        result = self.run_once()
        self.assertNotIn("skipped", result)
        self.assertEqual(len(self.transcriber_calls()), 1)

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
    def test_telnyx_call_with_both_channels_silent_is_parked(self):
        dead = ffmpeg_audio(self.root, "dead", "anullsrc=r=44100:cl=stereo:d=2").read_bytes()
        audio = self.drop(jg.CALL_NAME, jg.words(60), payload=dead)
        result = self.run_once()
        self.assertEqual(result["skipped"]["reason"], "junk_silent")
        self.assertTrue((self.root / "skipped/silent" / audio.name).is_file())


class EmptyTranscriptTest(jg.JunkGateHarness):
    """Post-ASR: an empty transcript is decided by level; duration is only the fallback."""

    def levels(self, *values):
        return patch.object(sanity, "audio_levels", side_effect=list(values))

    def test_empty_transcript_on_audible_audio_is_a_visible_asr_failure_even_for_a_short_clip(self):
        self.sanity_verdict = {**jg.SANITY_OK, "audio_duration_s": 5.0, "asr_duration_s": 5.0}
        audio = self.drop(jg.MEMO_NAME, "")
        with self.levels(AUDIBLE, AUDIBLE):
            result = self.run_once()
        self.assertNotIn("skipped", result)
        self.assertEqual(self.state_of(), ("failed", "asr_empty_output"))
        self.assertIn("EMPTY TRANSCRIPT", result["error"])
        self.assertIn("audible", result["error"])
        self.assertTrue(audio.is_file(), "the audio stays in the inbox, visible")
        self.assertEqual(list((self.root / "skipped").glob("*/*")), [], "nothing was parked")
        self.assert_nothing_published()
        evidence = ledger.describe_failure(self.item)["evidence"]
        self.assertIn(f"next: wax retry {self.item} | wax skip {self.item}", evidence)

    def test_an_empty_transcript_the_pre_asr_gate_missed_is_still_parked_as_silent(self):
        audio = self.drop(jg.MEMO_NAME, "")
        with self.levels(None, SILENT):       # unmeasurable before ASR, measurable after
            result = self.run_once()
        self.assertEqual(result["skipped"]["reason"], "junk_silent")
        self.assertTrue((self.root / "skipped/silent" / audio.name).is_file())
        self.assertEqual(self.state_of(), ("skipped", "junk_silent"))
        self.assertEqual(len(self.transcriber_calls()), 1)
        self.assert_nothing_published()

    def test_unmeasurable_level_falls_back_to_duration(self):
        self.sanity_verdict = {**jg.SANITY_OK, "audio_duration_s": 600.0, "asr_duration_s": 600.0}
        self.drop(jg.MEMO_NAME, "")
        with self.levels(None, None):
            self.run_once()
        self.assertEqual(self.state_of(), ("failed", "asr_empty_output"))

    def test_a_short_audible_empty_phone_call_is_still_call_noise(self):
        self.sanity_verdict = {**jg.SANITY_OK, "audio_duration_s": 8.0, "asr_duration_s": 8.0}
        audio = self.drop(jg.CALL_NAME, "")
        with self.levels(AUDIBLE, AUDIBLE):
            self.run_once()
        self.assert_rejected(audio, "junk_empty", asr=True)


class SilentSourceTest(jg.JunkGateHarness):
    """The snapshot facts `wax status`, state.json and waxd's source_silent condition share."""

    def park(self, name, *, origin="dropoff", levels=SILENT):
        audio = self.root / "inbox" / name
        audio.write_bytes(jg.AUDIO_BYTES + name.encode())
        self.transcript.write_text(jg.note(jg.words(30), name))
        self.item = ledger.upsert_item(audio, origin=origin)
        with patch.object(sanity, "audio_levels", return_value=levels):
            return worker.run_once()

    def test_one_silent_recording_is_visible_but_is_not_yet_a_source_problem(self):
        self.park("a.ogg")
        health = ledger.silent_health()
        self.assertEqual(health["count"], 1)
        self.assertEqual(health["recent"][0]["orig_name"], "a.ogg")
        self.assertEqual(health["recent"][0]["source"], "dropoff")
        self.assertEqual(health["sources"], [])

    def test_two_silent_in_a_row_from_one_origin_raise_source_silent(self):
        self.park("a.ogg")
        self.park("b.ogg")
        self.park("c.ogg", origin="inbox")        # another source: not part of the run
        sources = ledger.silent_health()["sources"]
        self.assertEqual([(s["source"], s["count"]) for s in sources], [("dropoff", 2)])
        self.assertEqual(sources[0]["names"], ["a.ogg", "b.ogg"])

    def test_the_next_audible_item_from_that_origin_clears_it(self):
        self.park("a.ogg")
        self.park("b.ogg")
        self.park("c.ogg", levels=AUDIBLE)
        self.assertEqual(ledger.silent_health()["sources"], [])
        self.assertEqual(ledger.silent_health()["count"], 2, "the parked ones stay listed")
        self.park("d.ogg")
        self.assertEqual(ledger.silent_health()["sources"], [], "one silent after audible is not a run")

    def test_enrich_puts_it_in_the_snapshot(self):
        self.park("a.ogg")
        snap = ledger.enrich({"inbox": {}, "stream": {}})
        self.assertEqual(snap["silent"]["count"], 1)

    def test_telnyx_calls_are_their_own_source(self):
        self.assertEqual(ledger.source_of("inbox", jg.CALL_NAME), "telnyx")
        self.assertEqual(ledger.source_of("dropoff", "20261118 092137.m4a"), "dropoff")
        self.assertEqual(ledger.source_of(None, None), "unknown")


class StatusRenderTest(unittest.TestCase):
    """`wax status` says silent recordings were parked, and names a silent source."""

    def render(self, silent):
        import contextlib
        import io
        import cli_honesty_test as ch
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ch.load_cli().render_status({"stream": {"state": "idle"}, "inbox": {"state": "idle"},
                                         "silent": silent})
        return buf.getvalue()

    def test_silent_line_names_the_recordings_and_the_next_step(self):
        out = self.render({"count": 2, "more": 0, "sources": [], "recent": [
            {"orig_name": "20261118 092137.m4a", "source": "dropoff"},
            {"orig_name": "20261118 101502.m4a", "source": "dropoff"}]})
        self.assertIn("2 silent recording(s) parked (not transcribed): "
                      "20261118 092137.m4a (dropoff), 20261118 101502.m4a (dropoff) -> check the mic/input", out)
        self.assertNotIn("SOURCE SILENT", out)

    def test_a_silent_source_is_called_out(self):
        out = self.render({"count": 2, "more": 0, "recent": [{"orig_name": "a.m4a", "source": "dropoff"}],
                           "sources": [{"source": "dropoff", "count": 2, "names": ["a", "b"], "last_at": "x"}]})
        self.assertIn("SOURCE SILENT dropoff: 2 in a row", out)

    def test_nothing_silent_prints_nothing(self):
        self.assertNotIn("silent", self.render({"count": 0, "recent": [], "more": 0, "sources": []}))


class SilentNotificationTest(unittest.TestCase):
    def setUp(self):
        desktop.clear_silent_source()
        self.addCleanup(desktop.clear_silent_source)

    def test_title_urgency_and_dedupe_per_source(self):
        with patch.object(desktop, "notify", return_value=True) as notify:
            self.assertTrue(desktop.notify_silent_recording("dropoff", "a.m4a, 79m 8s, mean -79.5 dB. Check the input"))
            self.assertFalse(desktop.notify_silent_recording("dropoff", "b.m4a"), "a muted Mac chimes once")
            self.assertTrue(desktop.notify_silent_recording("inbox", "c.ogg"), "another source is news")
        self.assertEqual(notify.call_args_list[0].args[0], "Wax: recording is silent")
        self.assertEqual(notify.call_args_list[0].kwargs["urgency"], "normal")
        self.assertEqual(notify.call_count, 2)

    def test_an_audible_item_rearms_the_source(self):
        with patch.object(desktop, "notify", return_value=True) as notify:
            desktop.notify_silent_recording("dropoff", "a")
            desktop.clear_silent_source("dropoff")
            desktop.notify_silent_recording("dropoff", "b")
        self.assertEqual(notify.call_count, 2)


class FailureNotificationTest(jg.JunkGateHarness):
    def test_item_failure_notifications_end_with_the_next_step(self):
        self.sanity_verdict = {**jg.SANITY_OK, "audio_duration_s": 600.0, "asr_duration_s": 600.0}
        self.drop(jg.MEMO_NAME, "")
        with patch.object(sanity, "audio_levels", return_value=AUDIBLE):
            self.run_once()
        title, body = worker.desktop.notify.call_args.args
        self.assertEqual(title, "Wax: item failed (asr_empty_output)")
        self.assertIn(f"next: wax retry {self.item} | wax skip {self.item}", body)


class DocsMatchBehaviourTest(unittest.TestCase):
    """The hub names the new causes, so an operator can look them up."""

    def test_reason_codes_document_the_new_causes(self):
        root = Path(__file__).resolve().parents[3]
        text = (root / "skills/heyma/subskills/doctor/references/reason-codes.md").read_text()
        for cause in ("junk_silent", "asr_empty_output"):
            self.assertIn(cause, text)
        self.assertIn(junk.SILENT_CAUSE, text)


if __name__ == "__main__":
    unittest.main()
