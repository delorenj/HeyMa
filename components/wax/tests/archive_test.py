import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from wax import archive, paths


class RemoteSizeTest(unittest.TestCase):
    @patch("wax.archive._mc_json")
    def test_uses_stat_when_head_is_allowed(self, mc_json):
        mc_json.return_value = {"status": "success", "size": 42}
        self.assertEqual(archive.remote_size("2026-08-09/abc-recording.mp3"), 42)
        mc_json.assert_called_once()

    @patch("wax.archive._mc_json")
    def test_falls_back_to_exact_key_listing_when_head_is_denied(self, mc_json):
        mc_json.side_effect = [
            None,
            {"status": "success", "type": "file", "key": "abc-recording.mp3", "size": 42},
        ]
        self.assertEqual(archive.remote_size("2026-08-09/abc-recording.mp3"), 42)

    @patch("wax.archive._mc_json")
    def test_rejects_prefix_match_from_listing(self, mc_json):
        mc_json.side_effect = [
            None,
            {"status": "success", "type": "file", "key": "abc-recording.mp3.wax.json", "size": 42},
        ]
        self.assertIsNone(archive.remote_size("2026-08-09/abc-recording.mp3"))


class TranscriptLinkTest(unittest.TestCase):
    @patch("wax.archive._write_sidecar_doc")
    @patch("wax.archive._cat_json")
    def test_idempotent_rearchive_preserves_existing_transcript_projection(self, cat_json, write_doc):
        cat_json.return_value = {
            "archived_at": "original-time",
            "transcript": {"filename": "dated-title.md"},
        }
        archive._write_sidecar(
            "item-id", "2026-08-10/hash-clip.ogg", "a" * 64, 42, Path("clip.ogg"),
        )
        doc = write_doc.call_args.args[0]
        self.assertEqual(doc["transcript"]["filename"], "dated-title.md")
        self.assertEqual(doc["archived_at"], "original-time")

    @patch("wax.archive._tag_audio_object", return_value=None)
    @patch("wax.archive._write_sidecar_doc")
    @patch("wax.archive._cat_json", return_value=None)
    @patch("wax.archive.references")
    def test_links_sidecars_and_tags_without_renaming_audio(
            self, references, _cat_json, write_sidecar, tag_audio):
        references.return_value = [{
            "sha256": "a" * 64,
            "orig_name": "clip-03.ogg",
            "bytes": 42,
            "s3_key": "2026-08-10/aaaaaaaaaaaa-clip-03.ogg",
            "bucket": "recordings",
            "verified_at": "2026-08-10T12:00:00Z",
        }]
        with tempfile.TemporaryDirectory() as directory, patch.object(paths, "VAULT", Path(directory)):
            md = Path(directory) / "20260810-120000-modular-passes.md"
            md.write_text(
                "---\ntitle: Modular Passes\ntitle-slug: modular-passes\n"
                "summary: A summary.\n---\nBody\n"
            )
            result = archive.link_transcript("a" * 16, md)

        doc = write_sidecar.call_args.args[0]
        self.assertEqual(doc["s3_key"], "2026-08-10/aaaaaaaaaaaa-clip-03.ogg")
        self.assertEqual(doc["transcript"]["filename"], md.name)
        self.assertEqual(doc["transcript"]["title_slug"], "modular-passes")
        write_sidecar.assert_called_once_with(doc, strict=True)
        tag_audio.assert_called_once_with(references.return_value[0], md.name, "modular-passes")
        self.assertTrue(result["sidecars_verified"])


if __name__ == "__main__":
    unittest.main()
