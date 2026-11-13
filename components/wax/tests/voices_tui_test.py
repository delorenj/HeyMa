"""wax voices tui: clip planning, the key-driven model, the player loop and the relabel worker.

Hermetic like voice_id_test.py: a temporary ledger and voices folder, a generated tone for audio, a
fake player for the model tests, and real subprocesses (`sh`) for the ClipLoop's kill/cycle tests.
The curses view itself is exercised in a real terminal: docs/evidence/heyma-19/.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from wax import ledger, paths, voiceprint, voices, voices_tui as tui

FFMPEG = shutil.which("ffmpeg") is not None
BIN = Path(__file__).resolve().parents[1] / "bin" / "wax"

BODY = ("# Transcription: a.ogg\n\n**Speaker 1 (00:00):** hello\n\n**Speaker 2 (00:02):** voice a talks a while\n\n"
        "**Speaker 1 (00:12):** and then b\n\n**Speaker 2 (00:20):** a again, shorter\n\n**Speaker 1 (00:25):** end\n")


def tone(path: Path, seconds: float, codec: str = "pcm_s16le", fmt: str = "wav") -> Path:
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-c:a", codec, "-f", fmt, str(path)], check=True)
    return path


class FakePlayer:
    """Records what the model asked for; plays nothing."""
    available = True
    reason = ""

    def __init__(self):
        self.calls = []
        self.now = None

    def play(self, voice_id, clips):
        self.calls.append(("play", voice_id, list(clips)))
        self.now = {"voice_id": voice_id, "index": 0, "total": len(clips), "what": clips[0].what, "error": ""}

    def stop(self):
        self.calls.append(("stop",))
        self.now = None

    def skip(self):
        self.calls.append(("skip",))

    def state(self):
        return self.now

    def plays(self):
        return [c[1] for c in self.calls if c[0] == "play"]


class FakeRelabeler:
    def __init__(self):
        self.requests = 0

    def request(self):
        self.requests += 1

    def progress(self):
        return ""


class _Fixture(unittest.TestCase):
    """voice-a unnamed (turns in two recordings), voice-b unnamed (only a listening clip), voice-c named Chase."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [patch.object(paths, "VAR", root / "var"), patch.object(paths, "DB", root / "var/wax.db"),
                        patch.object(voices, "VOICES_DIR", root / "Voices"),
                        patch.object(voices, "LOCK", root / "var/voices.lock")]
        for p in self.patches:
            p.start()
        self._close()
        voiceprint._means = {"call": np.zeros(voiceprint.DIM, np.float32), "room": np.zeros(voiceprint.DIM, np.float32)}
        self.root = root
        self.conn = voices.connect()
        if FFMPEG:
            self.audio1 = tone(root / "a.wav", 40)
            self.audio2 = tone(root / "b.wav", 30)
            voices.VOICES_DIR.mkdir(parents=True)
            tone(voices.VOICES_DIR / "voice-b00002.ogg", 13, codec="libopus", fmt="ogg")
        else:
            self.audio1, self.audio2 = root / "a.wav", root / "b.wav"
        md = root / "a.md"
        md.write_text(BODY)
        self.item("i1", self.audio1, md, 40.0)
        self.item("i2", self.audio2, None, 30.0)
        self.voice("voice-a00001", 100, "i1", ["00:02", "00:20"], seed=1)
        self.sight("i2", "voice-a00001", ["00:05"], 50, label="Speaker 3")
        self.voice("voice-b00002", 60, "i2", None, seed=2, label="Speaker 4")
        self.voice("voice-c00003", 30, "i2", ["00:20"], seed=3, name="Chase", label="Speaker 5")

    def _close(self):
        conn = getattr(ledger._local, "conn", None)
        if conn is not None:
            conn.close()
            del ledger._local.conn

    def tearDown(self):
        self._close()
        voiceprint._means = None
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def item(self, item_id, audio, md, duration):
        self.conn.execute("INSERT INTO items (item_id, sha256, path, first_seen, updated_at) VALUES (?,?,?,?,?)",
                          (item_id, "0" * 64, str(audio), "t", "t"))
        if md is not None:
            self.conn.execute("INSERT INTO transcripts (item_id, md_path, audio_duration, created_at) VALUES (?,?,?,?)",
                              (item_id, str(md), duration, "t"))

    def sight(self, item_id, voice_id, turns, seconds, label="Speaker 2", verdict="matched"):
        voices.record_sighting(self.conn, item_id=item_id, label=label, rendered=label, voice_id=voice_id,
                               verdict=verdict, score=0.9, runner_up=None, domain="room", seconds=seconds,
                               vec=None, turns=turns)

    def voice(self, voice_id, seconds, item_id, turns, *, seed, name=None, label="Speaker 2"):
        note = f"---\nvoice-id: {voice_id}\nname: {name or ''}\nstatus: {'named' if name else 'unnamed'}\n---\n\n# Unnamed voice `{voice_id}`\n"
        voices.register(self.conn, item_id=item_id, label=label, domain="room", seconds=seconds,
                        vec=voiceprint.normalize(np.random.default_rng(seed).standard_normal(voiceprint.DIM).astype(np.float32)),
                        name=name, note=note, voice_id=voice_id)
        if turns is not None:
            self.sight(item_id, voice_id, turns, seconds, label=label)
        else:
            self.sight(item_id, voice_id, None, seconds, label=label)

    def model(self, player=None, relabeler="fake"):
        player = player or FakePlayer()
        relabeler = FakeRelabeler() if relabeler == "fake" else relabeler
        return tui.Model(self.conn, player, relabeler), player, relabeler

    def type(self, model, text):
        for ch in text:
            model.handle_key(ch)


@unittest.skipUnless(FFMPEG, "ffmpeg")
class ClipPlanTest(_Fixture):
    def test_clips_come_from_turns_longest_first_one_recording_at_a_time(self):
        clips = tui.plan_clips(self.conn, "voice-a00001")
        self.assertEqual([c.what for c in clips], ["i1 00:02", "i2 00:05", "i1 00:20"])
        first = clips[0]
        # turn 00:02..00:12, trimmed 0.5 each side leaves 9 s; a 5 s clip is centred in it
        self.assertEqual((first.source, first.start, first.dur), (str(self.audio1), 4.5, 5.0))
        self.assertAlmostEqual(clips[2].dur, 4.0)           # 00:20..00:25 leaves 4 s
        self.assertEqual(clips[1].source, str(self.audio2))  # no transcript: the turn is assumed one clip long
        self.assertTrue(all(tui.MIN_CLIP_S <= c.dur <= tui.CLIP_S for c in clips))

    def test_max_clips_caps_the_plan(self):
        self.assertEqual(len(tui.plan_clips(self.conn, "voice-a00001", max_clips=2)), 2)

    def test_cut_clip_makes_a_cached_wav_under_var_never_the_vault(self):
        spec = tui.plan_clips(self.conn, "voice-a00001")[0]
        wav = tui.cut_clip(spec)
        self.assertIsNotNone(wav)
        self.assertEqual(wav.parent, paths.VAR / "voice-clips")
        self.assertFalse(str(wav).startswith(str(voices.VOICES_DIR)))
        self.assertAlmostEqual(tui.probe_seconds(wav), 5.0, delta=0.1)
        stamp = wav.stat().st_mtime_ns
        self.assertEqual(tui.cut_clip(spec), wav)
        self.assertEqual(wav.stat().st_mtime_ns, stamp)      # second cut is the cache
        self.assertEqual([p.name for p in voices.VOICES_DIR.glob("*.wav")], [])   # nothing in the vault

    def test_a_voice_without_turns_falls_back_to_slices_of_its_listening_clip(self):
        clips = tui.plan_clips(self.conn, "voice-b00002")
        self.assertEqual([(c.start, round(c.dur)) for c in clips], [(0.0, 5), (5.0, 5), (10.0, 3)])
        self.assertTrue(all(c.source.endswith("voice-b00002.ogg") for c in clips))
        wavs = [tui.cut_clip(c) for c in clips]
        self.assertTrue(all(wavs))
        self.assertAlmostEqual(tui.probe_seconds(wavs[2]), 3.0, delta=0.2)

    def test_turns_whose_audio_is_gone_fall_back_then_nothing(self):
        self.audio2.unlink()
        self.audio1.unlink()
        self.assertEqual(tui.plan_clips(self.conn, "voice-a00001"), [])        # no turns audio, no ogg
        tone(voices.VOICES_DIR / "voice-a00001.ogg", 7, codec="libopus", fmt="ogg")
        self.assertEqual([(c.start, round(c.dur)) for c in tui.plan_clips(self.conn, "voice-a00001")],
                         [(0.0, 5), (5.0, 2)])

    def test_an_uncertain_sighting_is_never_played_as_the_voice(self):
        self.conn.execute("UPDATE voice_sightings SET verdict='uncertain' WHERE voice_id='voice-a00001'")
        self.assertEqual(tui.plan_clips(self.conn, "voice-a00001"), [])


class ModelTest(_Fixture):
    def test_opens_on_the_most_heard_unnamed_voice_and_starts_its_clips(self):
        model, player, _ = self.model()
        self.assertEqual([r["voice_id"] for r in model.rows], ["voice-a00001", "voice-b00002", "voice-c00003"])
        self.assertEqual((model.cursor, model.focus), (0, "name"))
        self.assertEqual(player.plays(), ["voice-a00001"])
        clips = player.calls[-1][2]
        self.assertGreaterEqual(len(clips), 3 if FFMPEG else 0)   # a loop of several clips, not one

    def test_moving_away_stops_the_player_first_then_plays_the_new_row(self):
        model, player, _ = self.model()
        player.calls.clear()
        model.handle_key("DOWN")
        self.assertEqual([c[0] for c in player.calls], ["stop", "play"])
        self.assertEqual(player.plays(), ["voice-b00002"])
        player.calls.clear()
        model.handle_key("DOWN")                      # onto the named voice: stop, and nothing plays
        self.assertEqual(player.calls, [("stop",)])
        self.assertEqual((model.focus, model.cursor), ("list", 2))
        model.handle_key("k")                         # list focus: k moves
        self.assertEqual(model.cursor, 1)

    def test_typing_goes_to_the_name_field_so_jk_and_q_are_letters_there(self):
        model, player, _ = self.model()
        self.type(model, "jakq")
        self.assertEqual((model.cursor, model.buffer, model.quit), (0, "jakq", False))
        model.handle_key("BACKSPACE")
        self.assertEqual(model.buffer, "jak")
        model.handle_key("CTRL_U")
        self.assertEqual(model.buffer, "")
        self.type(model, "ab cd")
        model.handle_key("CTRL_W")
        self.assertEqual(model.buffer, "ab ")

    def test_a_half_typed_name_survives_a_trip_to_another_row(self):
        model, _, _ = self.model()
        self.type(model, "Dam")
        model.handle_key("DOWN")
        self.assertEqual(model.buffer, "")
        model.handle_key("UP")
        self.assertEqual(model.buffer, "Dam")

    def test_enter_locks_in_the_name_through_voices_set_name_and_moves_on(self):
        model, player, relabeler = self.model()
        with patch.object(voices, "set_name", wraps=voices.set_name) as set_name:
            self.type(model, "Damian Miller")
            model.handle_key("ENTER")
        set_name.assert_called_once_with(self.conn, "voice-a00001", "Damian Miller")
        note = (voices.VOICES_DIR / "voice-a00001.md").read_text()
        self.assertIn("name: Damian Miller", note)
        self.assertIn("# Damian Miller (`voice-a00001`)", note)
        self.assertEqual(voices.note_identity(voices.VOICES_DIR / "voice-a00001.md"), ("Damian Miller", "named"))
        self.assertIn("voice-a00001 is Damian Miller", model.status)
        self.assertEqual(relabeler.requests, 1)
        row = model.rows[0]
        self.assertEqual((row["name"], row["status"]), ("Damian Miller", "named"))
        self.assertEqual(model.row["voice_id"], "voice-b00002")   # on to the next unnamed voice
        self.assertEqual(player.plays()[-1], "voice-b00002")

    def test_enter_on_an_empty_name_names_nothing(self):
        model, _, relabeler = self.model()
        model.handle_key("ENTER")
        self.assertIn("type a name", model.status)
        self.assertEqual(relabeler.requests, 0)

    def test_naming_a_voice_after_a_known_one_folds_it_into_the_namesake(self):
        model, _, relabeler = self.model()
        self.type(model, "chase")
        model.handle_key("ENTER")
        # merge_namesakes keeps the OLDEST voice of the name (voice-a was registered first)
        self.assertIn("voice-c00003 merged into voice-a00001, already named chase", model.status)
        self.assertEqual(self.conn.execute("SELECT merged_into FROM voices WHERE voice_id='voice-c00003'").fetchone()[0],
                         "voice-a00001")

    def test_no_sync_names_without_relabelling(self):
        model, _, _ = self.model(relabeler=None)
        self.type(model, "Zed")
        model.handle_key("ENTER")
        self.assertIn("--no-sync", model.status)

    def test_a_named_voice_can_be_renamed_with_enter(self):
        model, player, _ = self.model()
        model.handle_key("END")
        model.handle_key("ENTER")
        self.assertEqual((model.focus, model.buffer), ("name", "Chase"))
        model.handle_key("CTRL_U")
        self.type(model, "Chase D")
        model.handle_key("ENTER")
        self.assertEqual(voices.note_identity(voices.VOICES_DIR / "voice-c00003.md"), ("Chase D", "named"))

    def test_space_toggles_playback_on_a_named_row(self):
        model, player, _ = self.model()
        model.handle_key("END")
        player.calls.clear()
        model.handle_key(" ")
        self.assertEqual(player.plays(), ["voice-c00003"] if FFMPEG else [])
        if FFMPEG:
            model.handle_key(" ")
            self.assertEqual(player.calls[-1], ("stop",))

    # -- merge
    def pick_target(self, model, voice_id):
        while model.pick["voice_id"] != voice_id:
            model.handle_key("DOWN" if model.merge_cursor < [r["voice_id"] for r in model.rows].index(voice_id) else "UP")

    def test_alt_m_picks_a_target_and_confirming_calls_merge_keep_picked_drop_original(self):
        model, player, relabeler = self.model()
        model.handle_key("ALT_M")
        self.assertEqual(model.mode, tui.MERGE_PICK)
        self.assertEqual(model.pick["voice_id"], "voice-c00003")      # starts on the named voice
        self.pick_target(model, "voice-c00003")
        model.handle_key("ENTER")
        self.assertEqual(model.mode, tui.MERGE_CONFIRM)
        self.assertIn("fold voice-a00001 into voice-c00003 (Chase)? [y/n]", model.status)
        with patch.object(voices, "merge", wraps=voices.merge) as merge:
            model.handle_key("y")
        merge.assert_called_once_with(self.conn, "voice-c00003", "voice-a00001")
        self.assertEqual(model.mode, tui.BROWSE)
        self.assertIn("voice-a00001 folded into voice-c00003", model.status)
        self.assertEqual(relabeler.requests, 1)
        merged = {r["voice_id"]: r["merged_into"] for r in model.rows}
        self.assertEqual(merged["voice-a00001"], "voice-c00003")
        self.assertEqual(model.row["voice_id"], "voice-b00002")

    def test_esc_cancels_the_selector_and_the_original_row_plays_again(self):
        model, player, relabeler = self.model()
        model.handle_key("ALT_M")
        player.calls.clear()
        with patch.object(voices, "merge") as merge:
            model.handle_key("ESC")
            model.handle_key("ALT_M")
            model.handle_key("ENTER")
            model.handle_key("n")                       # back from the confirmation
            self.assertEqual(model.mode, tui.MERGE_PICK)
            model.handle_key("ESC")
        merge.assert_not_called()
        self.assertEqual((model.mode, relabeler.requests), (tui.BROWSE, 0))
        self.assertEqual(player.plays()[0], "voice-a00001")
        self.assertEqual(model.row["voice_id"], "voice-a00001")

    def test_a_voice_cannot_be_merged_into_itself_or_into_a_merged_voice(self):
        voices.merge(self.conn, "voice-c00003", "voice-b00002")
        model, _, _ = self.model()
        model.handle_key("ALT_M")
        with patch.object(voices, "merge") as merge:
            model.handle_key("UP")
            model.handle_key("UP")                     # onto the original itself
            self.assertEqual(model.pick["voice_id"], "voice-a00001")
            model.handle_key("ENTER")
            self.assertEqual(model.mode, tui.MERGE_PICK)
            self.assertIn("itself", model.status)
            model.handle_key("DOWN")                   # voice-c (live), then voice-b (merged)
            model.handle_key("DOWN")
            self.assertEqual(model.pick["voice_id"], "voice-b00002")
            model.handle_key("ENTER")
            self.assertEqual(model.mode, tui.MERGE_PICK)
            self.assertIn("merged into voice-c00003", model.status)
        merge.assert_not_called()
        with self.assertRaises(ValueError):
            voices.merge(self.conn, "voice-a00001", "voice-a00001")

    def test_a_merged_row_cannot_start_a_merge_or_be_named(self):
        voices.merge(self.conn, "voice-c00003", "voice-b00002")
        model, _, _ = self.model()
        model.handle_key("END")
        self.assertEqual(model.row["voice_id"], "voice-b00002")
        model.handle_key("ALT_M")
        self.assertEqual(model.mode, tui.BROWSE)
        self.assertIn("already merged", model.status)
        model.handle_key("ENTER")
        self.assertIn("name that one", model.status)

    def test_ctrl_c_quits_from_every_mode_and_shutdown_stops_the_player(self):
        for keys in ([], ["ALT_M"], ["ALT_M", "ENTER"]):
            model, player, _ = self.model()
            for key in keys:
                model.handle_key(key)
            model.handle_key("CTRL_C")
            self.assertTrue(model.quit)
            player.calls.clear()
            model.shutdown()
            self.assertEqual(player.calls, [("stop",)])

    def test_without_a_player_the_status_says_so_and_naming_still_works(self):
        reason = "no audio player found (pw-play, paplay, ffplay, aplay; or set WAX_VOICES_PLAYER)"
        model, _, _ = self.model(player=tui.NullPlayer(reason))
        self.assertEqual(model.status, reason)
        self.type(model, "Quiet")
        model.handle_key("ENTER")
        self.assertEqual(voices.note_identity(voices.VOICES_DIR / "voice-a00001.md"), ("Quiet", "named"))

    def test_make_player_without_players_or_with_no_play(self):
        with patch.dict(os.environ, {"WAX_VOICES_PLAYER": "", "PATH": "/nonexistent"}):
            self.assertIsNone(tui.player_command())
            self.assertFalse(tui.make_player().available)
        self.assertFalse(tui.make_player(no_play=True).available)
        self.assertEqual(tui.player_command({"WAX_VOICES_PLAYER": "mpv --no-video"}), ["mpv", "--no-video"])

    def test_refresh_keeps_order_and_picks_up_a_new_voice(self):
        model, _, _ = self.model()
        self.voice("voice-d00004", 5, "i1", None, seed=9, label="Speaker 9")
        model.refresh()
        self.assertEqual([r["voice_id"] for r in model.rows][-1], "voice-d00004")
        self.assertEqual([r["voice_id"] for r in model.rows][:3], ["voice-a00001", "voice-b00002", "voice-c00003"])

    def test_empty_registry_is_quiet(self):
        self.conn.execute("DELETE FROM voices")
        model, player, _ = self.model()
        for key in ("DOWN", "ENTER", "ALT_M", "q"):
            model.handle_key(key)
        self.assertTrue(model.quit)
        self.assertTrue(tui.layout(model, 40, 10))


class LayoutAndKeysTest(_Fixture):
    def test_layout_is_safe_and_bounded_at_every_size(self):
        model, _, _ = self.model()
        for mode_keys in ([], ["ALT_M"], ["ALT_M", "ENTER"]):
            m, _, _ = self.model()
            for key in mode_keys:
                m.handle_key(key)
            for width in (1, 5, 11, 12, 15, 20, 30, 40, 60, 120):
                for height in (1, 2, 3, 4, 5, 6, 8, 24):
                    lines = tui.layout(m, width, height)
                    self.assertEqual(len(lines), height, (width, height))
                    for text, _style in lines:
                        self.assertLessEqual(len(text), max(1, width - 1), (width, height, text))

    def test_the_name_field_and_merge_markers_show(self):
        model, _, _ = self.model()
        self.type(model, "Dam")
        text = [t for t, _ in tui.layout(model, 100, 12)]
        self.assertTrue(any(line.startswith(">") and "Dam_" in line for line in text))
        self.assertTrue(any(line.startswith("NAME> Dam_") for line in text))
        model.handle_key("ALT_M")
        text = [t for t, s in tui.layout(model, 100, 12)]
        self.assertTrue(any(line.startswith("- ") and "voice-a00001" in line for line in text))
        self.assertTrue(any(line.startswith("+ ") and "voice-c00003" in line for line in text))

    def test_scrolling_keeps_the_cursor_row_visible(self):
        for n in range(30):
            self.voice(f"voice-e{n:05d}", 1 + n, "i1", None, seed=100 + n, label=f"Speaker {10 + n}")
        model, _, _ = self.model()
        model.handle_key("END")
        text = [t for t, _ in tui.layout(model, 80, 12)]
        self.assertTrue(any(model.row["voice_id"] in line for line in text))

    def test_alt_m_is_escape_then_m_and_a_lone_escape_is_cancel(self):
        def feed(*chars):
            pending = list(chars)
            return tui.decode_key(pending.pop(0), lambda: pending.pop(0) if pending else None)

        self.assertEqual(feed("\x1b"), "ESC")
        self.assertEqual(feed("\x1b", "m"), "ALT_M")
        self.assertEqual(feed("\x1b", "M"), "ALT_M")
        self.assertIsNone(feed("\x1b", "x"))
        self.assertEqual(feed("\n"), "ENTER")
        self.assertEqual(feed("\x7f"), "BACKSPACE")
        self.assertEqual(feed("\x15"), "CTRL_U")
        self.assertEqual(feed("\x03"), "CTRL_C")
        self.assertEqual(feed("é"), "é")
        self.assertIsNone(feed("\x01"))
        import curses
        self.assertEqual(feed(curses.KEY_UP), "UP")
        self.assertEqual(feed(curses.KEY_RESIZE), "RESIZE")


class ClipLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log, self.pids = self.dir / "log", self.dir / "pids"
        self.clips = [tui.ClipSpec(str(self.dir / f"c{i}.wav"), 0.0, 5.0, f"clip {i}") for i in (1, 2, 3)]

    def tearDown(self):
        self.tmp.cleanup()

    def loop(self, script):
        # `sh -c SCRIPT sh FILE`: the script sees the clip path as $1
        return tui.ClipLoop(["sh", "-c", script, "sh"], cut=lambda spec: Path(spec.source))

    def wait_for(self, predicate, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            threading.Event().wait(0.02)
        return False

    def lines(self):
        return self.log.read_text().split() if self.log.exists() else []

    def test_cycles_clip_1_2_3_then_back_to_1(self):
        loop = self.loop(f'echo "$1" >> {self.log}; exec sleep 0.05')
        loop.play("voice-a", self.clips)
        self.assertTrue(self.wait_for(lambda: len(self.lines()) >= 5))
        loop.stop()
        names = [Path(p).name for p in self.lines()]
        self.assertEqual(names[:5], ["c1.wav", "c2.wav", "c3.wav", "c1.wav", "c2.wav"])

    def test_stop_kills_the_player_process_at_once(self):
        loop = self.loop(f'echo $$ >> {self.pids}; exec sleep 30')
        loop.play("voice-a", self.clips)
        self.assertTrue(self.wait_for(self.pids.exists))
        self.assertTrue(self.wait_for(lambda: self.pids.read_text().strip() != ""))
        pid = int(self.pids.read_text().split()[0])
        os.kill(pid, 0)
        started = time.monotonic()
        loop.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(self.wait_for(lambda: not Path(f"/proc/{pid}").exists()), "player still running after stop")
        self.assertIsNone(loop.state())
        self.assertEqual(len(self.pids.read_text().split()), 1)    # and nothing started after the stop

    def test_play_replaces_the_previous_loop_and_skip_moves_on(self):
        loop = self.loop(f'echo $$ >> {self.pids}; echo "$1" >> {self.log}; exec sleep 30')
        loop.play("voice-a", self.clips)
        self.assertTrue(self.wait_for(lambda: len(self.lines()) >= 1))
        first_pid = int(self.pids.read_text().split()[0])
        loop.skip()
        self.assertTrue(self.wait_for(lambda: len(self.lines()) >= 2))
        self.assertEqual([Path(p).name for p in self.lines()[:2]], ["c1.wav", "c2.wav"])
        self.assertEqual(loop.state()["index"], 1)
        loop.play("voice-b", self.clips[:1])
        self.assertTrue(self.wait_for(lambda: not Path(f"/proc/{first_pid}").exists()))
        self.assertEqual(loop.state()["voice_id"], "voice-b")
        loop.stop()

    def test_a_player_that_keeps_failing_ends_the_loop_with_an_error(self):
        loop = tui.ClipLoop(["false"], cut=lambda spec: Path(spec.source))
        loop.play("voice-a", self.clips)
        self.assertTrue(self.wait_for(lambda: (loop.state() or {}).get("error")))
        self.assertIn("keeps failing", loop.state()["error"])
        loop.stop()

    def test_a_missing_player_binary_is_reported_not_raised(self):
        loop = tui.ClipLoop(["/nonexistent/player"], cut=lambda spec: Path(spec.source))
        loop.play("voice-a", self.clips)
        self.assertTrue(self.wait_for(lambda: (loop.state() or {}).get("error")))
        self.assertIn("cannot start", loop.state()["error"])
        loop.stop()

    def test_clips_that_cannot_be_cut_end_the_loop_with_an_error(self):
        loop = tui.ClipLoop(["true"], cut=lambda spec: None)
        loop.play("voice-a", self.clips)
        self.assertTrue(self.wait_for(lambda: (loop.state() or {}).get("error")))
        loop.stop()


class RelabelerTest(unittest.TestCase):
    def test_runs_in_the_background_and_summarises(self):
        gate, entered = threading.Event(), threading.Event()
        done = []

        def run_one(item):
            gate.wait(5)
            done.append(item)
            return "completed" if item != "bad" else "failed"

        relabeler = tui.Relabeler(run_one=lambda item: (entered.set(), run_one(item))[1], items=lambda: ["a", "bad", "c"])
        relabeler.request()
        self.assertTrue(entered.wait(5))
        self.assertTrue(relabeler.running)                 # request() returned while work is blocked
        self.assertIn("relabelling 0/3", relabeler.progress())
        gate.set()
        self.assertTrue(relabeler.wait(5))
        self.assertEqual(done, ["a", "bad", "c"])
        self.assertEqual(relabeler.summary(), ["relabelled 2 of 3 transcript(s)", "  bad  failed",
                                               "  rerun the failures with: wax voices sync"])
        self.assertEqual(relabeler.failed(), [("bad", "failed")])
        self.assertEqual(relabeler.progress(), "")

    def test_a_request_during_a_sweep_makes_it_look_again(self):
        gate, entered, calls = threading.Event(), threading.Event(), []

        def items():
            calls.append(1)
            return ["x"] if len(calls) == 1 else []

        relabeler = tui.Relabeler(run_one=lambda item: (entered.set(), gate.wait(5), "completed")[2], items=items)
        relabeler.request()
        self.assertTrue(entered.wait(5))                   # a sweep is now running
        relabeler.request()
        relabeler.request()
        gate.set()
        self.assertTrue(relabeler.wait(5))
        self.assertEqual(len(calls), 2)                    # two requests mid-sweep, one more look

    def test_errors_are_results_not_crashes(self):
        def boom(item):
            raise RuntimeError("gpu on fire")

        relabeler = tui.Relabeler(run_one=boom, items=lambda: ["a"])
        relabeler.request()
        self.assertTrue(relabeler.wait(5))
        self.assertIn("gpu on fire", relabeler.failed()[0][1])

    def test_finish_prints_the_summary_and_the_exit_code(self):
        relabeler = tui.Relabeler(run_one=lambda item: "failed", items=lambda: ["a"])
        relabeler.request()
        printed = []
        self.assertEqual(tui.finish(relabeler, printed.append), 1)
        self.assertIn("relabelled 0 of 1 transcript(s)", printed)
        self.assertEqual(tui.finish(None), 0)

    def test_relabel_state_reports_a_refused_pass(self):
        from wax import passes

        with patch.object(passes, "run", side_effect=passes.PassError("nope")):
            self.assertEqual(voices.relabel_state("i1"), "refused: nope")
        with patch.object(passes, "run", return_value={"state": "completed"}):
            self.assertEqual(voices.relabel_state("i1"), "completed")


class CliTest(unittest.TestCase):
    def test_tui_without_a_terminal_refuses_and_changes_nothing(self):
        proc = subprocess.run([sys.executable, str(BIN), "voices", "tui"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("needs a terminal", proc.stderr)

    def test_tui_flags_parse(self):
        proc = subprocess.run([sys.executable, str(BIN), "voices", "tui", "--help"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--no-sync", proc.stdout)
        self.assertIn("--no-play", proc.stdout)


if __name__ == "__main__":
    unittest.main()
