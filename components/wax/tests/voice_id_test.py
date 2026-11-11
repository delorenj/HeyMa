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

from wax import ledger, paths, voice_id_pass, voiceprint, voices

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
        self.assertEqual(voice_id_pass.regions_for("just words", None, 4), ("none", {}))


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
