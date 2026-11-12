"""voice-id: speaker turns, channel-aware matching, the registry and the pass's decisions.

Hermetic: a temporary ledger and voices folder, synthetic 192-number
fingerprints (no GPU, no audio). The GPU half (`voiceprint.fingerprint_item`)
is replaced by a function returning the fingerprints a test chose.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from wax import jev, ledger, paths, provider, voice_context, voice_id_pass, voiceprint, voices

RNG = np.random.default_rng(7)
T = voiceprint.Thresholds()


def unit(seed: int, dim: int = voiceprint.DIM) -> np.ndarray:
    return voiceprint.normalize(np.random.default_rng(seed).standard_normal(dim).astype(np.float32))


def near(vec: np.ndarray, noise: float, seed: int) -> np.ndarray:
    """The same voice in another recording: cosine to `vec` is about 1 / sqrt(1 + noise**2)."""
    return voiceprint.normalize(vec + noise * unit(10_000 + seed, len(vec)))


class TurnsTest(unittest.TestCase):
    BODY = ("# Transcription: x.ogg\n\n**Speaker 1 (00:00):** hello there how are you doing today\n\n"
            "**Speaker 2 (00:52):** fine thanks\n\n**Speaker 1 (1:02:03):** good\n\n"
            "not a turn **Speaker 3 (00:10):** inline\n")

    def test_turns_and_their_regions(self):
        turns = voiceprint.parse_turns(self.BODY)
        self.assertEqual(turns, [("Speaker 1", 0.0), ("Speaker 2", 52.0), ("Speaker 1", 3723.0)])
        regions = voiceprint.turn_regions(turns, 3800.0)
        self.assertEqual(regions, {"Speaker 1": [(0.0, 52.0), (3723.0, 3800.0)], "Speaker 2": [(52.0, 3723.0)]})

    def test_windows_trim_split_and_drop_short(self):
        spans = voiceprint.windows([(0.0, 1.2), (10.0, 35.0)])
        self.assertTrue(all(b - a <= voiceprint.WINDOW_MAX_S for a, b in spans))
        self.assertEqual(len(spans), 3)
        self.assertAlmostEqual(spans[0][0], 10.15)
        self.assertAlmostEqual(spans[-1][1], 34.85)

    def test_segment_regions_by_channel(self):
        segs = [{"speaker": 0, "start": 0, "end": 2}, {"speaker": 1, "start": 2, "end": 5},
                {"speaker": None, "start": 5, "end": 6}, {"speaker": 0, "start": 7, "end": 7}]
        self.assertEqual(voiceprint.segment_regions(segs), {0: [(0.0, 2.0)], 1: [(2.0, 5.0)]})

    def test_relabel_touches_only_turn_heads(self):
        body = "**Speaker 2 (00:52):** Speaker 2 said **Speaker 2 (00:53):**\n\n**Speaker 21 (01:00):** x\n"
        out = voice_id_pass.relabel(body, {"Speaker 2": "Damian"})
        self.assertEqual(out, "**Damian (00:52):** Speaker 2 said **Speaker 2 (00:53):**\n\n"
                              "**Speaker 21 (01:00):** x\n")

    def test_quotes_skip_fragments(self):
        self.assertEqual(voice_id_pass.quotes_for(self.BODY, "Speaker 1"), ["hello there how are you doing today"])

    def test_regions_for_each_shape(self):
        asr = {"result": {"channel_separated": True, "channel_labels": ["Me", "Damian Miller"],
                          "segments": [{"speaker": 0, "start": 0, "end": 3}, {"speaker": 1, "start": 3, "end": 9}]}}
        mode, regions = voice_id_pass.regions_for("", asr, 9)
        self.assertEqual(mode, "channels")
        self.assertEqual(regions["Damian Miller"], {"channel": 1, "spans": [(3.0, 9.0)]})
        mode, regions = voice_id_pass.regions_for(self.BODY, {"result": {"segments": []}}, 3800)
        self.assertEqual((mode, sorted(regions)), ("turns", ["Speaker 1", "Speaker 2"]))
        mode, regions = voice_id_pass.regions_for("just words", {"result": {"segments": [{"start": 1, "end": 4}]}}, 4)
        self.assertEqual((mode, regions), ("whole", {voice_id_pass.WHOLE: {"channel": None, "spans": [(1, 4)]}}))
        self.assertEqual(voice_id_pass.regions_for("just words", None, 4),
                         ("whole", {voice_id_pass.WHOLE: {"channel": None, "spans": [(0.0, 4.0)]}}))
        self.assertEqual(voice_id_pass.regions_for("", None, 4), ("none", {}))


class MatchTest(unittest.TestCase):
    def setUp(self):
        voiceprint._means = {"call": np.zeros(voiceprint.DIM, np.float32), "room": np.zeros(voiceprint.DIM, np.float32)}

    def tearDown(self):
        voiceprint._means = None

    def test_centroid_drops_the_window_that_is_someone_else(self):
        a = unit(1)
        windows = np.stack([near(a, 0.3, s) for s in range(6)] + [unit(99)])
        vec, kept = voiceprint.centroid(windows)
        self.assertEqual(kept, 6)
        self.assertGreater(float(vec @ a), 0.95)

    def test_known_voice_matches_and_a_stranger_is_new(self):
        a, b = unit(1), unit(2)
        registry = {"voice-a": [("room", a), ("room", near(a, 0.2, 3))], "voice-b": [("room", b)]}
        m = voiceprint.match(near(a, 0.25, 4), "room", registry)
        self.assertEqual((m.voice_id, m.cross, m.clear), ("voice-a", False, True))
        self.assertEqual(voiceprint.classify(m, 30, 0.8), "matched")
        stranger = voiceprint.match(unit(3), "room", registry)
        self.assertTrue(stranger.stranger)
        self.assertEqual(voiceprint.classify(stranger, 30, 0.8), "new")

    def test_short_or_scattered_speech_is_never_registered(self):
        m = voiceprint.match(unit(3), "room", {"voice-a": [("room", unit(1))]})
        self.assertEqual(voiceprint.classify(m, 3, 0.9), "too_short")
        self.assertEqual(voiceprint.classify(m, 10, 0.9), "too_short")   # under min_new_s
        self.assertEqual(voiceprint.classify(m, 30, 0.2), "too_short")   # windows disagree

    def test_two_close_voices_are_uncertain(self):
        a = unit(1)
        registry = {"voice-a": [("room", near(a, 0.1, 1))], "voice-a2": [("room", near(a, 0.1, 2))]}
        m = voiceprint.match(near(a, 0.1, 3), "room", registry)
        self.assertFalse(m.clear)
        self.assertEqual(voiceprint.classify(m, 30, 0.8), "uncertain")

    def test_a_voice_heard_only_on_the_other_channel_uses_its_own_bars(self):
        a = unit(1)
        orth = unit(2) - float(unit(2) @ a) * a
        orth /= np.linalg.norm(orth)
        cos = (T.match_cross + T.match) / 2      # between the two bars
        probe = cos * a + np.sqrt(1 - cos ** 2) * orth
        same = voiceprint.match(probe, "room", {"voice-a": [("room", a)]})
        cross = voiceprint.match(probe, "call", {"voice-a": [("room", a)]})
        self.assertEqual((same.cross, cross.cross), (False, True))
        self.assertAlmostEqual(same.score, cos, places=4)
        self.assertEqual(voiceprint.classify(same, 30, 0.8), "uncertain")   # under the same-channel bar
        self.assertEqual(voiceprint.classify(cross, 30, 0.8), "matched")    # over the other-channel bar

    def test_one_odd_same_channel_exemplar_does_not_outvote_the_other_channel(self):
        a = unit(1)
        registry = {"voice-a": [("call", near(a, 0.2, s)) for s in range(5)] + [("room", unit(77))]}
        m = voiceprint.match(near(a, 0.3, 9), "room", registry)
        self.assertTrue(m.cross)
        self.assertGreater(m.score, T.match_cross)
        self.assertEqual(voiceprint.classify(m, 30, 0.8), "matched")

    def test_same_channel_exemplars_win_over_the_other_channel(self):
        a = unit(1)
        registry = {"voice-a": [("room", unit(50)), ("call", near(a, 0.2, 6))]}
        m = voiceprint.match(near(a, 0.2, 7), "call", registry)
        self.assertFalse(m.cross)
        self.assertGreater(m.score, T.match)

    def test_centring_removes_the_shared_channel_colour(self):
        colour = unit(42) * 1.5
        heard = [voiceprint.normalize(unit(seed) + colour) for seed in range(100, 160)]
        voiceprint._means = {"call": np.mean(heard, axis=0), "room": np.zeros(voiceprint.DIM, np.float32)}
        x, y = voiceprint.normalize(unit(1) + colour), voiceprint.normalize(unit(2) + colour)
        self.assertGreater(float(x @ y), 0.6)
        self.assertLess(abs(float(voiceprint.center(x, "call") @ voiceprint.center(y, "call"))), 0.3)

    def test_the_shipped_domain_means_load(self):
        voiceprint._means = None
        means = voiceprint.domain_means()
        self.assertEqual(sorted(means), ["call", "room"])
        self.assertEqual(means["call"].shape, (voiceprint.DIM,))


class _Registry(unittest.TestCase):
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


class RegistryTest(_Registry):
    def test_register_writes_a_note_to_fill_in_and_never_overwrites_one(self):
        transcript = self.root / "20260903-092132-chat.md"
        note = voices.note_text("voice-abc123", transcript=transcript, title="Chat", label="Speaker 2", at="00:52",
                                quotes=["another show at like one"], clip="voice-abc123.ogg", registered="now")
        voice = voices.register(self.conn, item_id="i1", label="Speaker 2", domain="room", seconds=30,
                                vec=unit(1), note=note, voice_id="voice-abc123")
        text = voice.note_path.read_text()
        self.assertIn("name: \n", text)
        self.assertIn("![[voice-abc123.ogg]]", text)
        self.assertIn("[[20260903-092132-chat|Chat]]", text)
        voice.note_path.write_text(text.replace("name: \n", "name: Damian\n"))
        self.assertEqual(voices.note_identity(voice.note_path), ("Damian", "named"))
        loaded = voices.load(self.conn)
        self.assertEqual((loaded["voice-abc123"].name, loaded["voice-abc123"].link),
                         ("Damian", "[[voice-abc123|Damian]]"))
        with self.assertRaises(Exception):
            voices.register(self.conn, item_id="i2", label="Speaker 1", domain="room", seconds=30, vec=unit(2),
                            note="x", voice_id="voice-abc123")
        voices.set_name(self.conn, "voice-abc123", "Damian Miller")   # the heading follows the name
        self.assertIn("# Damian Miller (`voice-abc123`)", voice.note_path.read_text())
        self.assertNotIn("Unnamed voice", voice.note_path.read_text())

    def test_ignore_status_and_exemplar_diversity(self):
        voice = voices.register(self.conn, item_id="i1", label="Speaker 1", domain="room", seconds=30,
                                vec=unit(1), note="---\nname: \nstatus: ignore\n---\n", voice_id="voice-tv0001")
        self.assertEqual(voices.note_identity(voice.note_path), (None, "ignore"))
        self.assertFalse(voices.add_exemplar(self.conn, voice, "i2", "Speaker 1", "room", 30, unit(1)))
        self.assertTrue(voices.add_exemplar(self.conn, voice, "i2", "Speaker 1", "call", 30, unit(1)))
        self.assertTrue(voices.add_exemplar(self.conn, voice, "i3", "Speaker 1", "room", 30, near(unit(1), 0.6, 1)))

    def test_merge_moves_sightings_and_keeps_the_note(self):
        for vid, seed in (("voice-keep01", 1), ("voice-drop01", 2)):
            voices.register(self.conn, item_id=f"i{seed}", label="Speaker 1", domain="room", seconds=30,
                            vec=unit(seed), note="---\nname: \n---\n", voice_id=vid)
            voices.record_sighting(self.conn, item_id=f"i{seed}", label="Speaker 1", rendered="Speaker 1",
                                   voice_id=vid, verdict="new", score=None, runner_up=None, domain="room",
                                   seconds=30, vec=unit(seed))
        self.assertEqual(voices.merge(self.conn, "voice-keep01", "voice-drop01"), 1)
        self.assertEqual(sorted(voices.load(self.conn)), ["voice-keep01"])
        self.assertEqual(len(voices.load(self.conn)["voice-keep01"].exemplars), 2)
        drop_note = voices.VOICES_DIR / "voice-drop01.md"
        self.assertIn("merged-into", drop_note.read_text())
        voices.set_name(self.conn, "voice-keep01", "Chase")
        self.assertEqual(voices.needs_relabel(self.conn), ["i1", "i2"])


class PassTest(_Registry):
    """The pass end to end, with the GPU half replaced."""

    def note(self, name: str, body: str, *, item: str, asr: dict | None = None, duration: float = 600) -> Path:
        audio = self.root / f"{item}.ogg"
        audio.write_bytes(b"x")
        md = self.root / name
        md.write_text("---\ntitle: A chat\n---\n" + body)
        asr_path = None
        if asr is not None:
            asr_path = self.root / f"{item}.asr.json"
            asr_path.write_text(json.dumps(asr))
        conn = ledger.connect()
        conn.execute("INSERT INTO items (item_id, path, sha256, state, first_seen, updated_at) "
                     "VALUES (?,?,?,?,?,?)", (item, str(audio), item * 4, "complete", "now", "now"))
        conn.execute("INSERT INTO transcripts (item_id, md_path, audio_duration, asr_path, created_at) "
                     "VALUES (?,?,?,?,?)", (item, str(md), duration, str(asr_path) if asr_path else None, "now"))
        return md

    def run_pass(self, md, item, prints, owner="Jarad"):
        def fake(audio, regions, clip_dir):
            self.asked = sorted(regions)
            out = {}
            for label in regions:
                vec, seconds = prints[label]
                out[label] = {"vec": [float(x) for x in vec], "seconds": seconds, "windows": 5, "kept": 5,
                              "consistency": 0.8, "clip": None}
            return out
        self.asked = []
        env = {"WAX_VOICE_OWNER": owner}
        with patch.object(voice_id_pass, "fingerprint", fake), patch.dict("os.environ", env):
            return voice_id_pass.main(md, item)

    BODY = "**Speaker 1 (00:00):** I gave him a drink at like one in the morning\n\n" \
           "**Speaker 2 (00:52):** another show at like one that is too late\n\n**Speaker 3 (02:03):** so\n"

    def test_first_sight_registers_strangers_then_a_name_relabels_them(self):
        md = self.note("chat.md", self.BODY, item="a1")
        jarad, wife = unit(1), unit(2)
        prints = {"Speaker 1": (jarad, 300.0), "Speaker 2": (wife, 200.0), "Speaker 3": (unit(3), 2.0)}
        result = self.run_pass(md, "a1", prints)
        speakers = result["evidence"]["speakers"]
        self.assertEqual([speakers[s]["verdict"] for s in ("Speaker 1", "Speaker 2", "Speaker 3")],
                         ["new", "new", "too_short"])
        self.assertEqual(len(result["evidence"]["registered"]), 2)
        self.assertNotIn("body_replace", result)   # nobody has a name yet
        self.assertEqual(len(result["frontmatter"]["voices"]), 2)
        # Rerun: same fingerprints from the cache, no GPU, no new voices.
        again = self.run_pass(md, "a1", prints)
        self.assertEqual(self.asked, [])
        self.assertEqual(again["evidence"]["registered"], [])
        self.assertEqual({v["verdict"] for v in again["evidence"]["speakers"].values()}, {"matched", "too_short"})
        # Name Speaker 2's voice; the rerun relabels the turns.
        voice_id = again["evidence"]["speakers"]["Speaker 2"]["voice"]
        voices.set_name(self.conn, voice_id, "Mom")
        self.assertEqual(voices.needs_relabel(self.conn), ["a1"])
        named = self.run_pass(md, "a1", prints)
        self.assertIn("**Mom (00:52):**", named["body_replace"]["text"])
        self.assertIn("**Speaker 1 (00:00):**", named["body_replace"]["text"])
        self.assertIn(f"[[{voice_id}|Mom]]", named["frontmatter"]["voices"])
        md.write_text("---\ntitle: A chat\n---\n" + named["body_replace"]["text"])
        self.assertEqual(voices.needs_relabel(self.conn), [])
        # A rename later follows the label the pass itself wrote.
        voices.set_name(self.conn, voice_id, "Mama")
        renamed = self.run_pass(md, "a1", prints)
        self.assertIn("**Mama (00:52):**", renamed["body_replace"]["text"])

    def test_a_known_voice_in_a_new_recording_is_named_and_learned(self):
        jarad = unit(1)
        md1 = self.note("one.md", self.BODY, item="b1")
        first = self.run_pass(md1, "b1", {"Speaker 1": (jarad, 300.0), "Speaker 2": (unit(2), 200.0),
                                          "Speaker 3": (unit(3), 1.0)})
        voices.set_name(self.conn, first["evidence"]["speakers"]["Speaker 1"]["voice"], "Jarad")
        md2 = self.note("two.md", "**Speaker 1 (00:00):** a different talk entirely here\n", item="b2")
        second = self.run_pass(md2, "b2", {"Speaker 1": (near(jarad, 0.2, 9), 120.0)})
        self.assertEqual(second["evidence"]["speakers"]["Speaker 1"]["verdict"], "matched")
        self.assertIn("**Jarad (00:00):**", second["body_replace"]["text"])

    def test_a_call_names_the_owner_on_channel_a_and_never_relabels(self):
        asr = {"result": {"channel_separated": True, "channel_labels": ["Me", "+15551234567"],
                          "segments": [{"speaker": 0, "start": 0, "end": 30}, {"speaker": 1, "start": 30, "end": 90}]}}
        md = self.note("call.md", "**Me (00:00):** hi\n\n**+15551234567 (00:30):** hello\n", item="c1", asr=asr)
        result = self.run_pass(md, "c1", {"Me": (unit(1), 30.0), "+15551234567": (unit(5), 60.0)})
        speakers = result["evidence"]["speakers"]
        self.assertEqual((speakers["Me"]["verdict"], speakers["Me"]["name"]), ("owner_new", "Jarad"))
        self.assertEqual(speakers["+15551234567"]["verdict"], "new")
        self.assertNotIn("body_replace", result)
        self.assertEqual(result["evidence"]["domain"], "call")
        owner = voices.load(self.conn)[speakers["Me"]["voice"]]
        self.assertEqual(owner.status, "named")
        self.assertIn("name: Jarad", owner.note_path.read_text())

    def test_ignored_voice_is_matched_but_never_named(self):
        md = self.note("tv.md", self.BODY, item="d1")
        tv = unit(4)
        first = self.run_pass(md, "d1", {"Speaker 1": (tv, 100.0), "Speaker 2": (unit(6), 1.0),
                                         "Speaker 3": (unit(7), 1.0)})
        note = voices.VOICES_DIR / f"{first['evidence']['speakers']['Speaker 1']['voice']}.md"
        note.write_text(note.read_text().replace("name: \n", "name: News anchor\n").replace("status: unnamed",
                                                                                            "status: ignore"))
        md2 = self.note("tv2.md", "**Speaker 1 (00:00):** and in other news tonight the weather\n", item="d2")
        second = self.run_pass(md2, "d2", {"Speaker 1": (near(tv, 0.2, 1), 100.0)})
        self.assertEqual(second["evidence"]["speakers"]["Speaker 1"]["verdict"], "matched")
        self.assertNotIn("body_replace", second)
        self.assertEqual(second["frontmatter"]["voices"], [])

    def test_no_local_audio_is_a_classified_failure(self):
        md = self.note("gone.md", self.BODY, item="e1")
        (self.root / "e1.ogg").unlink()
        with self.assertRaises(voice_id_pass.AudioMissing):
            self.run_pass(md, "e1", {})

    CALL = {"result": {"channel_separated": True, "channel_labels": ["Me", "Damian Miller"],
                       "segments": [{"speaker": 0, "start": 0, "end": 30}, {"speaker": 1, "start": 30, "end": 90}]}}

    def test_caller_id_names_the_far_voice_and_learns_every_phone(self):
        md = self.note("c1.md", "**Me (00:00):** hi\n\n**Damian Miller (00:30):** hello\n", item="f1", asr=self.CALL)
        damian = unit(5)
        first = self.run_pass(md, "f1", {"Me": (unit(1), 30.0), "Damian Miller": (damian, 60.0)})
        speaker = first["evidence"]["speakers"]["Damian Miller"]
        self.assertEqual((speaker["verdict"], speaker["name"]), ("caller_id_new", "Damian Miller"))
        self.assertEqual(first["evidence"]["registered"], [])   # named already: nothing to fill in
        note = voices.VOICES_DIR / f"{speaker['voice']}.md"
        self.assertIn("name: Damian Miller", note.read_text())
        self.assertIn("caller ID", note.read_text())
        # His car phone sounds like nobody we know, but the line says it is him.
        md2 = self.note("c2.md", "**Me (00:00):** yo\n\n**Damian Miller (00:30):** from the car\n", item="f2",
                        asr=self.CALL)
        second = self.run_pass(md2, "f2", {"Me": (near(unit(1), 0.2, 1), 30.0),
                                           "Damian Miller": (unit(6), 60.0)})
        self.assertEqual(second["evidence"]["speakers"]["Damian Miller"]["verdict"], "caller_id")
        self.assertEqual(len(voices.load(self.conn)[speaker["voice"]].exemplars), 2)

    def test_a_voice_memo_teaches_the_owner_his_desk_voice(self):
        asr = {"result": {"channel_separated": True, "channel_labels": ["Me", "+15551234567"],
                          "segments": [{"speaker": 0, "start": 0, "end": 30}, {"speaker": 1, "start": 30, "end": 90}]}}
        md = self.note("call.md", "**Me (00:00):** hi\n", item="g1", asr=asr)
        jarad = unit(1)
        call = self.run_pass(md, "g1", {"Me": (jarad, 30.0), "+15551234567": (unit(5), 2.0)})
        owner_id = call["evidence"]["speakers"]["Me"]["voice"]
        memo = self.note("memo.md", "remember to call the plumber about the leak",
                         item="g2", asr={"result": {"segments": [{"start": 0, "end": 40}]}})
        desk = near(jarad, 1.8, 3)   # cosine about 0.42 to his phone voice: over the solo bar, under the match bar
        result = self.run_pass(memo, "g2", {voice_id_pass.WHOLE: (desk, 40.0)})
        speaker = result["evidence"]["speakers"][voice_id_pass.WHOLE]
        self.assertEqual((speaker["verdict"], speaker["voice"]), ("owner", owner_id))
        self.assertEqual(sorted(d for d, _ in voices.load(self.conn)[owner_id].exemplars), ["call", "room"])

    def test_naming_a_second_voice_after_someone_known_merges_them(self):
        for vid, seed in (("voice-aaaaaa", 1), ("voice-bbbbbb", 2)):
            voices.register(self.conn, item_id=f"i{seed}", label="Speaker 1", domain="room", seconds=30,
                            vec=unit(seed), note="---\nname: \n---\n", voice_id=vid)
        voices.set_name(self.conn, "voice-aaaaaa", "Cait")
        voices.set_name(self.conn, "voice-bbbbbb", "cait")
        self.assertEqual(voices.merge_namesakes(self.conn), [("voice-aaaaaa", "voice-bbbbbb")])
        self.assertEqual(sorted(voices.load(self.conn)), ["voice-aaaaaa"])


class UnfoldTest(PassTest):
    """A name the pass wrote comes back off exactly the turns it was written on."""

    FOLD = ("**Speaker 1 (00:00):** I gave him a drink at like one in the morning\n\n"
            "**Speaker 2 (00:52):** another show at like one that is too late\n\n"
            "**Speaker 1 (01:30):** fine, one more and then bed for real\n")

    def write(self, md, result):
        md.write_text("---\ntitle: A chat\n---\n" + result["body_replace"]["text"])

    def test_two_speakers_folded_into_one_name_come_apart(self):
        jarad = unit(1)
        voices.register(self.conn, item_id="seed", label="Speaker 1", domain="room", seconds=60, vec=jarad,
                        note="---\nname: Jarad\nstatus: named\n---\n", voice_id="voice-jarad1")
        wife = near(jarad, 1.5, 4)   # over his match bar, under learn: taken for him until she is known
        md = self.note("fold.md", self.FOLD, item="u1")
        prints = {"Speaker 1": (near(jarad, 0.05, 3), 300.0), "Speaker 2": (wife, 200.0)}
        first = self.run_pass(md, "u1", prints)
        self.assertEqual(first["body_replace"]["text"].count("**Jarad ("), 3)   # folded
        self.write(md, first)
        rows = {r["label"]: r for r in voices.sighting_rows(self.conn, "u1")}
        self.assertEqual(rows["Speaker 1"]["turns"], ["00:00", "01:30"])
        self.assertEqual(rows["Speaker 2"]["turns"], ["00:52"])
        # She becomes known; the rerun parts the two and names her turn alone.
        voices.register(self.conn, item_id="seed2", label="Speaker 1", domain="room", seconds=60, vec=wife,
                        note="---\nname: Laura\nstatus: named\n---\n", voice_id="voice-laura1")
        again = self.run_pass(md, "u1", prints)
        self.assertEqual(self.asked, [])   # each label's own cached fingerprint, no GPU
        text = again["body_replace"]["text"]
        self.assertIn("**Jarad (00:00):**", text)
        self.assertIn("**Laura (00:52):**", text)
        self.assertIn("**Jarad (01:30):**", text)
        self.assertEqual(again["evidence"]["unfolded"], ["Speaker 1", "Speaker 2"])
        self.write(md, again)
        # A name the pass no longer stands behind is withdrawn; a hand edit is left alone.
        md.write_text(md.read_text().replace("**Jarad (01:30):**", "**Dad (01:30):**"))
        self.conn.execute("UPDATE voices SET status='ignore' WHERE voice_id='voice-laura1'")
        Path(voices.load(self.conn)["voice-laura1"].note_path).write_text(
            "---\nname: Laura\nstatus: ignore\n---\n")
        third = self.run_pass(md, "u1", {**prints, "Dad": prints["Speaker 1"]})
        text = third["body_replace"]["text"]
        self.assertIn("**Speaker 2 (00:52):**", text)
        self.assertIn("**Jarad (00:00):**", text)
        self.assertIn("**Dad (01:30):**", text)

    def test_a_fold_with_no_turn_map_is_never_put_to_the_context(self):
        rows = [{"label": "Speaker 1", "rendered": "Jarad", "turns": None},
                {"label": "Speaker 2", "rendered": "Jarad", "turns": None},
                {"label": "Speaker 3", "rendered": "Laura", "turns": None}]
        self.assertEqual(voice_id_pass._folded(rows, set()), frozenset({"Jarad"}))
        self.assertEqual(voice_id_pass._folded(rows, {"Speaker 1", "Speaker 2"}), frozenset())

    def test_a_telephone_recording_on_one_track_is_call_audio(self):
        self.assertTrue(voice_id_pass.telephone("/x/archive/2026/10/telnyx-2026-10-30-57a0.mp3"))
        self.assertTrue(voice_id_pass.telephone("/x/telnyx-vm-2026-11-11-f523.wav"))
        self.assertFalse(voice_id_pass.telephone("/x/20260909-094902.m4a"))
        self.assertFalse(voice_id_pass.telephone(None))


class DecideTest(unittest.TestCase):
    def setUp(self):
        voiceprint._means = {"call": np.zeros(voiceprint.DIM, np.float32), "room": np.zeros(voiceprint.DIM, np.float32)}

    def tearDown(self):
        voiceprint._means = None

    def decide(self, **kw):
        base = dict(label="Speaker 1", mode="turns", channel=None, vec=unit(9), seconds=60.0, consistency=0.8,
                    domain="room", voices={}, names={}, owner="Jarad")
        return voiceprint.decide(**{**base, **kw})

    def test_caller_names(self):
        for label in ("+19083392545", "Channel B", "Unassigned", "Unknown caller", "Me", "Speaker 2"):
            self.assertIsNone(voiceprint.caller_name(label), label)
        self.assertEqual(voiceprint.caller_name("Dariusz Czaplic"), "Dariusz Czaplic")

    def test_the_owner_calling_from_another_phone_is_the_owner(self):
        d = self.decide(label="Jarad DeLorenzo", mode="channels", channel=1, domain="call",
                        voices={"voice-j": [("call", unit(1))]}, names={"voice-j": "Jarad"})
        self.assertEqual((d.verdict, d.voice_id, d.learn), ("owner", "voice-j", True))

    def test_someone_else_on_a_known_line_is_who_they_sound_like(self):
        wife = unit(3)
        d = self.decide(label="Damian Miller", mode="channels", channel=1, domain="call", vec=near(wife, 0.1, 1),
                        voices={"voice-d": [("call", unit(5))], "voice-w": [("call", wife)]},
                        names={"voice-d": "Damian Miller", "voice-w": "Wendy"})
        self.assertEqual((d.verdict, d.voice_id), ("matched", "voice-w"))

    def test_a_memo_in_someone_elses_voice_is_not_the_owner(self):
        d = self.decide(mode="whole", label=voice_id_pass.WHOLE, vec=unit(9),
                        voices={"voice-j": [("room", unit(1))]}, names={"voice-j": "Jarad"})
        self.assertEqual(d.verdict, "new")
        self.assertEqual(d.register_as, "")


if __name__ == "__main__":
    unittest.main()


class ContextTest(_Registry):
    """voice_context: the transcript names a speaker the voice could not settle (Jev faked at the wire)."""

    note = PassTest.note
    run_pass = PassTest.run_pass

    BODY = ("**Speaker 1 (00:00):** Laura can you pass the salt please, it is right next to you\n\n"
            "**Speaker 2 (00:10):** here you go, and do not forget we have the school meeting at four\n")

    def setUp(self):
        super().setUp()
        self.laura = unit(5)
        voices.register(self.conn, item_id="seed", label="Speaker 1", domain="room", seconds=60, vec=self.laura,
                        note="---\nname: Laura\nrelationship: wife\nstatus: named\n---\n", voice_id="voice-laura1")
        self.calls = []

    def jev(self, verdicts):
        """A Jev ingress answering each speaker's choice with `verdicts[label]` ({name or someone_else: p})."""
        def post(url, payload, *, key, timeout, response_headers=None):
            self.calls.append(payload)
            answers = {}
            for qname, q in payload["questions"].items():
                label = q["instructions"].split("Who is ", 1)[1].split(" in this recording")[0]
                wanted = verdicts.get(label, {})
                probs = {}
                for option, meaning in q["criteria"].items():
                    who = voice_context.SOMEONE_ELSE if option == voice_context.SOMEONE_ELSE else meaning.split(" is ", 1)[1].split(" (")[0]
                    probs[option] = wanted.get(who, 0.0)
                rest = 1.0 - sum(probs.values())
                probs[voice_context.SOMEONE_ELSE] += max(0.0, rest)
                answers[qname] = {"type": "choice", "choice": max(probs, key=probs.get), "probabilities": probs}
            return {"answers": answers, "model": "jev-1.13.0"}
        return post

    def run_ctx(self, md, item, prints, verdicts, env=None):
        environ = {"WAX_VOICE_CONTEXT": "1", **(env or {})}
        with patch.object(provider, "resolve_key", lambda **kw: "k"), \
                patch.object(provider, "post_json", self.jev(verdicts)), patch.dict("os.environ", environ):
            return self.run_pass(md, item, prints)

    def uncertain_laura(self, seed):
        return near(self.laura, 1.6, seed)   # cosine about 0.53: under the match bar, over `new`

    def test_the_transcript_names_an_uncertain_voice(self):
        md = self.note("salt.md", self.BODY, item="k1")
        result = self.run_ctx(md, "k1", {"Speaker 1": (unit(1), 30.0), "Speaker 2": (self.uncertain_laura(1), 40.0)},
                              {"Speaker 2": {"Laura": 0.7}})
        speaker = result["evidence"]["speakers"]["Speaker 2"]
        self.assertEqual((speaker["verdict"], speaker["name"]), ("context", "Laura"))
        self.assertGreaterEqual(speaker["context"]["posterior"], 0.85)
        self.assertIn("**Laura (00:10):**", result["body_replace"]["text"])
        self.assertIn("[[voice-laura1|Laura]]", result["frontmatter"]["voices"])
        self.assertEqual(len(voices.load(self.conn)["voice-laura1"].exemplars), 1)   # context never teaches the voice
        state = self.calls[0]["state"]
        self.assertEqual(state["people"]["voice-laura1"]["relationship"], "wife")
        self.assertTrue(any("THIS SPEAKER" in line for line in state["unidentified_speakers"]["Speaker 2"]))
        self.assertNotIn("score", json.dumps(state))   # Jev never sees the voice evidence

    def test_a_context_that_points_nowhere_names_nobody(self):
        md = self.note("salt2.md", self.BODY, item="k2")
        result = self.run_ctx(md, "k2", {"Speaker 1": (unit(1), 30.0), "Speaker 2": (self.uncertain_laura(2), 40.0)},
                              {"Speaker 2": {"Laura": 0.2}})
        speaker = result["evidence"]["speakers"]["Speaker 2"]
        self.assertEqual(speaker["verdict"], "uncertain")
        self.assertIsNone(speaker["context"]["named"])
        self.assertNotIn("body_replace", result)

    def test_context_names_a_would_be_stranger_instead_of_registering_it(self):
        md = self.note("salt3.md", self.BODY, item="k3")
        faint = near(self.laura, 2.3, 3)   # cosine about 0.40: the voice alone would register a stranger
        self.assertEqual(voiceprint.classify(voiceprint.match(faint, "room", {"v": [("room", self.laura)]}), 40.0, 0.8),
                         "new")
        result = self.run_ctx(md, "k3", {"Speaker 1": (unit(1), 30.0), "Speaker 2": (faint, 40.0)},
                              {"Speaker 2": {"Laura": 0.95}})
        speaker = result["evidence"]["speakers"]["Speaker 2"]
        self.assertEqual((speaker["verdict"], speaker["voice"]), ("context", "voice-laura1"))
        # Only Speaker 1, whom no named voice resembles, is registered.
        self.assertEqual(result["evidence"]["registered"], [result["evidence"]["speakers"]["Speaker 1"]["voice"]])

    def test_a_failed_jev_call_leaves_the_voice_verdict_and_says_so(self):
        md = self.note("salt4.md", self.BODY, item="k4")

        def down(*a, **kw):
            raise provider.ProviderError("connection refused", "provider_unreachable")
        with patch.object(provider, "resolve_key", lambda **kw: "k"), patch.object(provider, "post_json", down), \
                patch.object(jev, "RETRY_BACKOFF_S", 0), patch.dict("os.environ", {"WAX_VOICE_CONTEXT": "1"}):
            result = self.run_pass(md, "k4", {"Speaker 1": (unit(1), 30.0),
                                              "Speaker 2": (self.uncertain_laura(4), 40.0)})
        self.assertEqual(result["evidence"]["speakers"]["Speaker 2"]["verdict"], "uncertain")
        self.assertIn("provider_unreachable", result["evidence"]["context"]["error"])

    def test_jev_sees_the_diarizer_label_not_a_name_this_pass_wrote(self):
        md = self.note("salt7.md", self.BODY, item="k7")
        first = self.run_ctx(md, "k7", {"Speaker 1": (unit(1), 30.0), "Speaker 2": (self.laura, 40.0)}, {})
        self.assertIn("**Laura (00:10):**", first["body_replace"]["text"])
        md.write_text("---\ntitle: A chat\n---\n" + first["body_replace"]["text"])
        # A second named voice too close to hers: the voice can no longer tell the two apart.
        voices.register(self.conn, item_id="seed3", label="Speaker 1", domain="room", seconds=60,
                        vec=near(self.laura, 0.15, 8), note="---\nname: Chaya\nstatus: named\n---\n",
                        voice_id="voice-chaya1")
        again = self.run_ctx(md, "k7", {}, {"Speaker 2": {"Laura": 0.8}})
        state = self.calls[-1]["state"]
        self.assertIn("Speaker 2", state["unidentified_speakers"])
        self.assertNotIn("Laura", state["unidentified_speakers"])
        self.assertEqual(again["evidence"]["speakers"]["Speaker 2"]["verdict"], "context")
        self.assertNotIn("body_replace", again)   # still Laura, now on the context's word

    def test_off_switch_and_no_candidates_never_call_jev(self):
        md = self.note("salt5.md", self.BODY, item="k5")
        self.run_ctx(md, "k5", {"Speaker 1": (unit(1), 2.0), "Speaker 2": (self.uncertain_laura(5), 40.0)}, {},
                     env={"WAX_VOICE_CONTEXT": "0"})
        md2 = self.note("salt6.md", self.BODY, item="k6")
        # Nobody named sounds remotely like either speaker.
        self.run_ctx(md2, "k6", {"Speaker 1": (unit(1), 2.0), "Speaker 2": (unit(77), 40.0)}, {})
        self.assertEqual(self.calls, [])


class CombineTest(unittest.TestCase):
    def cand(self, vid, lr):
        return voice_context.Candidate(vid, vid.title(), 0.5, False, lr)

    def test_likelihood_ratio_rises_with_the_score_and_is_capped(self):
        lrs = [voice_context.likelihood_ratio(s, False) for s in (0.0, 0.3, 0.45, 0.6, 0.95)]
        self.assertEqual(lrs, sorted(lrs))
        self.assertEqual((lrs[0], lrs[-1]), (voice_context.LR_MIN, voice_context.LR_MAX))
        self.assertGreater(voice_context.likelihood_ratio(0.3, True), voice_context.likelihood_ratio(0.3, False))

    def test_context_must_point_and_the_voice_must_not_argue(self):
        s = voice_context.Settings()
        both = voice_context.combine({"laura": 0.6, "someone_else": 0.4}, [self.cand("laura", 7.0)], s)
        self.assertEqual(both.voice_id, "laura")
        weak_context = voice_context.combine({"laura": 0.2, "someone_else": 0.8}, [self.cand("laura", 20.0)], s)
        self.assertIsNone(weak_context.voice_id)   # posterior 0.83: the voice carries it, not the transcript
        weak_voice = voice_context.combine({"laura": 0.9, "someone_else": 0.1}, [self.cand("laura", 0.3)], s)
        self.assertIsNone(weak_voice.voice_id)
        split = voice_context.combine({"laura": 0.45, "chaya": 0.45, "someone_else": 0.1},
                                      [self.cand("laura", 5.0), self.cand("chaya", 5.0)], s)
        self.assertIsNone(split.voice_id)

    def test_excerpt_puts_name_mentions_first_and_marks_the_speaker(self):
        body = ("**Speaker 1 (00:00):** hello\n\n**Speaker 2 (00:05):** weather is nice\n\n"
                "**Speaker 1 (00:09):** Laura, the kids are home\n\n**Speaker 2 (00:12):** great\n")
        lines = voice_context.excerpt(body, "Speaker 2", {"Speaker 1": "Jarad"}, ["Laura"])
        self.assertIn("[00:09] Jarad: Laura, the kids are home", lines)
        self.assertIn("[00:12] THIS SPEAKER: great", lines)
        self.assertEqual(voice_context.excerpt(body, "Speaker 2", {}, [], limit=70),
                         ["[00:00] Speaker 1: hello", "[00:05] THIS SPEAKER: weather is nice"])
