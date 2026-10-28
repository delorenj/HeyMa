import datetime as dt
import hashlib
import unittest

from wax import frontmatter
from wax.transcriptions import extract

UTC = dt.timezone.utc

FULL_WAX = """---
schema-version: 1
vault-id: ""
asset-kind: transcript
specialist: transcripts
tags: ""
source: audio-recording
captured: 2026-02-10T15:36:25-05:00
pipeline-status: new
source-audio: /home/delorenj/HeyMa/inbox/2025/05/2025_05_31_17_53_58.mp3
transcribed-at: 2026-10-14 14:40:21
audio-duration-seconds: 1432.921
language: en
engine-model: large-v3
transcription-backend: faster-whisper
diarized: true
duration-seconds: 1400.5
classification: meeting
title: Spouses Disagreeing
summary: A summary.
title-slug: spouses-disagreeing
wax-item-id: 4c9f2d894fde1cc5
wax:
  transcribed_at: 2026-10-14T14:40:22.375Z
  audio_duration_s: 1432.921
  duration_ratio: 1
  source_audio: 2025_05_31_17_53_58.mp3
  diarization_requested: false
  passes:
    classification:
      state: completed
      at: 2026-10-14T14:40:27.302Z
      version: 1
      command_id: 91e9ff93-674a-59a7-a3a8-974769713dd8
      attempt: 1
      reason_code: dependency_failed
      detail: stale
    title-slug:
      state: completed
      at: 2026-10-14T14:41:13.893Z
      version: 3
      command_id: fcbcdbcd-11b0-5b0b-9a3c-1c2d3e4f5a6b
      attempt: 1
      model: automaticai/auto/fast
      served_by: '@cf/zai-org/glm-4.7-flash'
      fell_through: automaticai/personal/glm-5.3-flash=provider_429; automaticai/personal/glm=5.3=timeout
---
# Transcription: 2025_05_31_17_53_58.mp3

**Speaker 2 (01:09):** you on your phone i'm not on the phone

**Speaker 1 (01:19):** where's your phone it's in my pocket
"""

MIGRATED = """---
wax-item-id: 0123456789abcdef
wax:
  transcribed_at: 2026-07-01T10:00:00Z
  audio_duration_s: 12.5
---
# Transcription: record_0014.mp3

just a short memo about groceries
"""

HERMES = """---
pipeline-status: new
source-audio: /tmp/hermes-local-stt-di4k45kj/hermes-desktop-voice-atse9xcg.wav
audio-duration-seconds: 3.2
---
# Transcription: hermes-desktop-voice-atse9xcg.wav

**Speaker 1 (00:00):** Do you have a skill on how to create plugins for yourself?
"""

LEGACY = """---
pipeline-status: new
---
# Transcription: 105044-clip_0055__20260720-105052.mp3

- **Source**: `/home/delorenj/audio/ingest/105044-clip_0055__20260720-105052.mp3`
- **Date**: 2026-07-21 16:45
- **Duration**: 172m 44s
- **Language**: en
- **Model**: large-v3 (faster-whisper)
- **Diarization**: enabled

---

**Speaker 1 (00:00):** want right so if you if you like if you forget a month later

**Speaker 2 (00:16):** once so it's okay it's really interesting
"""

OTTER = """---
pipeline-status: new
source-audio: /home/delorenj/Music/Technical Interview Preparation_otter_ai.mp3
transcribed-at: 2026-07-06T12:07:38
audio-duration-seconds: 1800
language: en
engine-model: large-v3
transcription-backend: faster-whisper
diarized: false
tags:
  - interview
labels: [work]
---
# Transcription: Technical Interview Preparation_otter_ai.mp3

we talked about the system design interview and the questions
"""

NO_FRONTMATTER = "./BRAINDUMP.md -\n"


class SplitTest(unittest.TestCase):
    def test_block_plus_body_is_the_note_for_every_shape(self):
        for text in (FULL_WAX, MIGRATED, HERMES, LEGACY, OTTER, NO_FRONTMATTER, "", "---\nfoo: 1\n---",
                     "---\nnot: [valid\n---\nbody\n", "---\n- a list\n---\nbody\n"):
            with self.subTest(text=text[:30]):
                fm, block, body = extract.split_note(text)
                self.assertEqual(block + body, text)
                e = extract.from_note(text, "n.md")
                self.assertEqual(e.frontmatter_block + e.body, text)

    def test_body_sha_matches_wax_definition(self):
        for text in (FULL_WAX, LEGACY, NO_FRONTMATTER):
            e = extract.from_note(text, "n.md")
            self.assertEqual(e.body_sha256, hashlib.sha256(frontmatter.split(text)[1].encode()).hexdigest())

    def test_seventeen_byte_note(self):
        self.assertEqual(len(NO_FRONTMATTER.encode()), 17)
        e = extract.from_note(NO_FRONTMATTER, "20261022-053245-ai-pair.md")
        self.assertEqual(e.frontmatter_block, "")
        self.assertEqual(e.origin, "test")


class TimestampTest(unittest.TestCase):
    def test_naive_is_utc_aware_keeps_offset_bare_date_is_date_precision(self):
        e = extract.from_note(FULL_WAX, "n.md")
        self.assertEqual(e.version["fm_transcribed_at"], dt.datetime(2026, 10, 14, 14, 40, 21, tzinfo=UTC))
        captured = e.identity["captured_at"]
        self.assertEqual(captured.utcoffset(), dt.timedelta(hours=-5))
        self.assertEqual(e.identity["captured_precision"], "datetime")
        dated = extract.from_note("---\ncaptured: 2026-02-10\n---\nbody words here\n", "n.md")
        self.assertEqual(dated.identity["captured_precision"], "date")
        self.assertEqual(dated.identity["captured_at"], dt.datetime(2026, 2, 10, tzinfo=UTC))
        self.assertEqual(extract.parse_ts("2026-10-14T14:40:22.375Z")[0].tzinfo, UTC)

    def test_recorded_at_patterns(self):
        tz = extract._default_tz()
        cases = {
            "2025_05_31_17_53_58.mp3": (dt.datetime(2025, 5, 31, 17, 53, 58), "datetime"),
            "clip_0055__20260720-105052.mp3": (dt.datetime(2026, 7, 20, 10, 50, 52), "datetime"),
            "20261022-050452-rec.ogg": (dt.datetime(2026, 10, 22, 5, 4, 52), "datetime"),
            "recording_20260817_115013.wav": (dt.datetime(2026, 8, 17, 11, 50, 13), "datetime"),
            "2026-07-13 17.36.30.ogg": (dt.datetime(2026, 7, 13, 17, 36, 30), "datetime"),
            "20260728 125905.m4a": (dt.datetime(2026, 7, 28, 12, 59, 5), "datetime"),
            "telnyx-2026-10-01-e252ecd2-c0de-11f1-a1ae-ba5ac364b654-1791219893-000102dc.mp3":
                (dt.datetime(2026, 10, 1), "date"),
        }
        for name, (wall, precision) in cases.items():
            with self.subTest(name=name):
                stamp, got = extract.parse_recorded_at(name, tz)
                self.assertEqual(got, precision)
                self.assertEqual(stamp, wall.replace(tzinfo=tz))
        self.assertEqual(extract.parse_recorded_at("peepeedance.mp3", tz), (None, None))
        self.assertEqual(extract.parse_recorded_at("2026_13_40_99_99_99.mp3", tz), (None, None))

    def test_legacy_bullets(self):
        e = extract.from_note(LEGACY, "105044-clip_0055__20260720-105052.md")
        self.assertEqual(e.body_row["legacy_duration_s"], 10364)
        self.assertEqual(e.body_row["legacy_model"], "large-v3")
        self.assertEqual(e.body_row["legacy_backend"], "faster-whisper")
        self.assertEqual(e.body_row["legacy_language"], "en")
        self.assertEqual(e.body_row["legacy_diarization"], "enabled")
        self.assertEqual(e.body_row["legacy_source_path"],
                         "/home/delorenj/audio/ingest/105044-clip_0055__20260720-105052.mp3")
        self.assertEqual(e.body_row["legacy_date_local"].utcoffset(), dt.timedelta(hours=-4))
        self.assertEqual(e.identity["engine_model"], "large-v3")
        self.assertEqual(e.identity["transcription_backend"], "faster-whisper")
        self.assertEqual(e.identity["asr_duration_s"], 10364)
        self.assertEqual(extract.parse_legacy_duration("10.4s"), 10.4)
        self.assertEqual(extract.parse_legacy_duration("1h 2m 3s"), 3723)


class ListAndExtraTest(unittest.TestCase):
    def test_tags_and_labels(self):
        self.assertEqual(extract.from_note(FULL_WAX, "n.md").version["tags"], [])
        one = extract.from_note("---\ntags: meeting\n---\nsome body words\n", "n.md")
        self.assertEqual(one.version["tags"], ["meeting"])
        otter = extract.from_note(OTTER, "n.md")
        self.assertEqual(otter.version["tags"], ["interview"])
        self.assertEqual(otter.version["labels"], ["work"])
        self.assertEqual(otter.tag_rows, [("tags", 0, "interview"), ("labels", 0, "work")])
        self.assertEqual(otter.current_tags, ["interview", "work"])

    def test_empty_router_strings_are_null_and_unknown_keys_land_in_vault_extra(self):
        e = extract.from_note("---\nvault-id: ''\ndomain: ''\nmystery: 42\nnested: {a: [1]}\n---\nbody text words\n",
                              "n.md")
        self.assertIsNone(e.version["vault_id"])
        self.assertIsNone(e.version["domain"])
        self.assertEqual(e.version["vault_extra"], {"mystery": 42, "nested": {"a": [1]}})
        self.assertEqual(e.frontmatter_json["mystery"], 42)

    def test_pass_state_rows(self):
        e = extract.from_note(FULL_WAX, "n.md")
        states = {p["slug"]: p for p in e.pass_states}
        self.assertIsNone(states["classification"]["reason_code"])   # completed: stale keys dropped
        self.assertIsNone(states["classification"]["detail"])
        self.assertEqual(states["title-slug"]["pass_version"], 3)
        self.assertEqual(states["title-slug"]["fell_through"], [
            {"model": "automaticai/personal/glm-5.3-flash", "reason_code": "provider_429"},
            {"model": "automaticai/personal/glm=5.3", "reason_code": "timeout"}])
        self.assertEqual(states["title-slug"]["served_by"], "@cf/zai-org/glm-4.7-flash")

    def test_invalid_classification_fails_extraction(self):
        with self.assertRaises(extract.ExtractError):
            extract.from_note("---\nclassification: Meeting\n---\nbody\n", "n.md")
        ok = extract.from_note("---\nclassification: ''\n---\nbody\n", "n.md")
        self.assertIsNone(ok.version["classification"])


class OriginTest(unittest.TestCase):
    def test_origins(self):
        first = "---\nwax-item-id: 2d89c76e10c61b6d\npipeline-status: new\n---\n\nbody\n"
        self.assertEqual(extract.from_note(first, "a-first.ogg.md").origin, "test")
        self.assertEqual(extract.from_note(HERMES, "h.md").origin, "hermes-stt")
        clip = "---\npipeline-status: new\n---\n# Transcription: clip_0059.mp3\n\nsome words were said here\n"
        self.assertEqual(extract.from_note(clip, "clip.md").origin, "legacy")
        self.assertEqual(extract.from_note(MIGRATED, "m.md").origin, "wax")
        self.assertEqual(extract.from_note(clip, "clip.md", item_id="0123456789abcdef").origin, "wax")
        self.assertEqual(extract.from_note(clip, "clip.md", ledger_maps_note=True).origin, "wax")
        # a short body WITH duration evidence is not a test note
        self.assertEqual(extract.from_note("---\naudio-duration-seconds: 2.0\n---\nhi\n", "x.md").origin, "legacy")
        self.assertFalse(extract.from_note(first, "a-first.ogg.md").searchable)

    def test_hermes_legacy_bullet_source(self):
        note = ("---\npipeline-status: new\n---\n# Transcription: audio_32eab340bc12.wav\n\n"
                "- **Source**: `/tmp/hermes-local-stt-di4k45kj/audio_32eab340bc12.wav`\n- **Duration**: 10.4s\n\n---\n\n"
                "**Speaker 1 (00:00):** Do you have a skill?\n")
        e = extract.from_note(note, "audio_32eab340bc12.md")
        self.assertEqual(e.origin, "hermes-stt")
        self.assertEqual(e.identity["source_audio_name"], "audio_32eab340bc12.wav")


class CoalesceTest(unittest.TestCase):
    def test_audio_duration_order(self):
        ledger = {"transcript": {"audio_duration": 3.0, "created_at": "2026-10-14T14:40:22.000Z"},
                  "item": {"duration_s": 4.0}}
        both = extract.from_note("---\nwax:\n  audio_duration_s: 1.0\nduration-seconds: 2.0\n---\nwords words words\n",
                                 "n.md", ledger=ledger)
        self.assertEqual(both.identity["audio_duration_s"], 1.0)
        stamped = extract.from_note("---\nduration-seconds: 2.0\n---\nwords words words\n", "n.md")
        self.assertEqual(stamped.identity["audio_duration_s"], 2.0)
        led = extract.from_note("---\nduration-seconds: 2.0\n---\nwords words words\n", "n.md", ledger=ledger)
        self.assertEqual(led.identity["audio_duration_s"], 3.0)

    def test_s3_and_source_sha_order(self):
        sha = "e" * 64
        note = (f"---\nsource-sha256: {sha}\nsource-s3-uri: s3://recordings/2026-10-27/abc-x.mp3\n"
                "source-s3-key: recordings/2026-10-27/abc-x.mp3\n---\nwords words words\n")
        e = extract.from_note(note, "n.md")
        self.assertEqual((e.identity["audio_s3_bucket"], e.identity["audio_s3_key"]),
                         ("recordings", "2026-10-27/abc-x.mp3"))
        self.assertEqual(e.identity["source_sha256"], sha)
        led = extract.from_note(note, "n.md", ledger={"backups": [
            {"bucket": "b1", "s3_key": "old", "verified_at": "2026-01-01T00:00:00Z", "bytes": 1, "method": "etag"},
            {"bucket": "b2", "s3_key": "new", "verified_at": "2026-02-01T00:00:00Z", "bytes": 2, "method": "size"}]})
        self.assertEqual((led.identity["audio_s3_bucket"], led.identity["audio_s3_key"]), ("b2", "new"))
        self.assertEqual(led.identity["backup_method"], "size")
        key_only = extract.from_note("---\nsource-s3-key: recordings/2026/x.mp3\n---\nwords words words\n", "n.md")
        self.assertEqual((key_only.identity["audio_s3_bucket"], key_only.identity["audio_s3_key"]),
                         ("recordings", "2026/x.mp3"))

    def test_transcribed_at_order(self):
        wax_only = extract.from_note("---\nwax:\n  transcribed_at: 2026-10-14T14:40:22Z\n---\nwords words words\n",
                                     "n.md", ledger={"transcript": {"created_at": "2026-10-15T00:00:00Z"}})
        self.assertEqual(wax_only.identity["transcribed_at"], dt.datetime(2026, 10, 14, 14, 40, 22, tzinfo=UTC))
        legacy = extract.from_note(LEGACY, "n.md")
        self.assertEqual(legacy.identity["transcribed_at"], legacy.body_row["legacy_date_local"])


class HashTest(unittest.TestCase):
    def test_pass_timestamps_do_not_change_content(self):
        base = extract.from_note(FULL_WAX, "n.md")
        bumped = extract.from_note(FULL_WAX.replace("at: 2026-10-14T14:40:27.302Z", "at: 2026-10-15T09:00:00.000Z"),
                                   "n.md")
        self.assertNotEqual(base.note_sha256, bumped.note_sha256)
        self.assertEqual(base.content_sha256, bumped.content_sha256)
        retitled = extract.from_note(FULL_WAX.replace("title: Spouses Disagreeing", "title: Other"), "n.md")
        self.assertNotEqual(base.content_sha256, retitled.content_sha256)
        self.assertEqual(base.body_sha256, retitled.body_sha256)

    def test_doc_input(self):
        e = extract.from_note(FULL_WAX, "n.md")
        self.assertEqual(e.doc_input, "Title: Spouses Disagreeing\nSummary: A summary.")
        self.assertEqual(e.doc_input_sha256, hashlib.sha256(e.doc_input.encode()).hexdigest())
        self.assertIsNone(extract.from_note(HERMES, "h.md").doc_input)
        self.assertEqual(extract.from_note(HERMES, "hermes-note.md").display_title, "hermes-note")

    def test_body_stats(self):
        e = extract.from_note(FULL_WAX, "n.md")
        self.assertTrue(e.body_row["has_speaker_labels"])
        self.assertEqual(e.body_row["speaker_count"], 2)
        self.assertEqual(e.body_row["speaker_turn_count"], 2)
        self.assertEqual(e.body_row["h1_source_name"], "2025_05_31_17_53_58.mp3")
        speakers = {s["speaker_label"]: s for s in e.speakers}
        self.assertEqual(speakers["Speaker 2"]["first_start_s"], 69.0)

    def test_nul_byte_is_refused(self):
        with self.assertRaises(extract.ExtractError):
            extract.from_note("body\x00", "n.md")


if __name__ == "__main__":
    unittest.main()
