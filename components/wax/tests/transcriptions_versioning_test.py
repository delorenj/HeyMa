import unittest

from wax.transcriptions import versioning as v


class KeyTest(unittest.TestCase):
    def test_keys_for_every_producer_kind(self):
        sha = "ab" * 32
        self.assertEqual(v.transcription_key("4c9f2d894fde1cc5", 2, sha), f"transcription:4c9f2d894fde1cc5:a2:{sha[:16]}")
        self.assertEqual(v.pass_key("91e9ff93-674a-59a7-a3a8-974769713dd8"), "pass:91e9ff93-674a-59a7-a3a8-974769713dd8")
        self.assertEqual(v.skip_key("4c9f2d894fde1cc5", "diarization", 1, 3), "skip:4c9f2d894fde1cc5:diarization:v1:a3")
        self.assertEqual(v.depfail_key("4c9f2d894fde1cc5", "ticketable", 2, 1),
                         "depfail:4c9f2d894fde1cc5:ticketable:v2:a1")
        self.assertEqual(v.backfill_key(sha), f"backfill:{sha}")
        self.assertEqual(v.reconcile_key(sha), f"reconcile:{sha}")
        self.assertEqual(v.reconcile_key(sha, 4), f"reconcile:v4:{sha}")

    def test_pass_snapshot_key(self):
        common = dict(item_id="4c9f2d894fde1cc5", slug="s", pass_version=1, attempt=2)
        self.assertEqual(v.pass_snapshot_key(state="completed", command_id="c", reason_code=None, **common), "pass:c")
        self.assertEqual(v.pass_snapshot_key(state="skipped", command_id=None, reason_code="condition_unmet", **common),
                         "skip:4c9f2d894fde1cc5:s:v1:a2")
        self.assertEqual(v.pass_snapshot_key(state="failed", command_id=None, reason_code="dependency_failed", **common),
                         "depfail:4c9f2d894fde1cc5:s:v1:a2")


class DecideTest(unittest.TestCase):
    head = {"produced_by": "frontmatter-stamp", "pass_version": 1, "content_sha256": "c1"}

    def test_no_head_mints(self):
        for kind in v.PRODUCER_KINDS:
            self.assertEqual(v.decide(None, kind=kind, produced_by=kind, pass_version=None, content_sha256="x"), v.MINT)

    def test_same_slug_version_content_is_no_change(self):
        self.assertEqual(v.decide(self.head, kind="pass", produced_by="frontmatter-stamp", pass_version=1,
                                  content_sha256="c1"), v.NO_CHANGE)

    def test_new_pass_version_or_content_mints(self):
        self.assertEqual(v.decide(self.head, kind="pass", produced_by="frontmatter-stamp", pass_version=2,
                                  content_sha256="c1"), v.MINT)
        self.assertEqual(v.decide(self.head, kind="pass", produced_by="frontmatter-stamp", pass_version=1,
                                  content_sha256="c2"), v.MINT)

    def test_different_slug_same_content_mints(self):
        self.assertEqual(v.decide(self.head, kind="pass", produced_by="clipboard-copy", pass_version=1,
                                  content_sha256="c1"), v.MINT)

    def test_reconcile(self):
        self.assertEqual(v.decide(self.head, kind="reconcile", produced_by="reconcile", pass_version=None,
                                  content_sha256="c1"), v.SKIP)
        self.assertEqual(v.decide(self.head, kind="reconcile", produced_by="reconcile", pass_version=None,
                                  content_sha256="c9"), v.MINT)

    def test_backfill_with_head_skips(self):
        self.assertEqual(v.decide(self.head, kind="backfill", produced_by="backfill", pass_version=None,
                                  content_sha256="c9"), v.SKIP)

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            v.decide(None, kind="nope", produced_by="x", pass_version=None, content_sha256="c")


if __name__ == "__main__":
    unittest.main()
