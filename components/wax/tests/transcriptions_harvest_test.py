"""`px harvest` artifact reading for link-harvest (docs/HOLOCENE-TRANSCRIPTS-TAB.md B.3-B.4).

Pure: no database. The fixture is a real version-2 artifact pilot 0.4.2 wrote for its own
synthetic test note (`px harvest test/fixtures/harvest/20261021-090000-synthetic-sync.md
--dry-run`, 2026-10-29); the cases mutate copies of it. The Postgres side (matching,
idempotency, the view's exclusions) is in transcriptions_pg_test.py.
"""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax.transcriptions import harvest

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "harvest" / "synthetic-sync.actionitems.json"


def fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def published(artifact: dict) -> dict:
    """The fixture as a completed, published harvest: item 1 on a Plane board, item 2 on a --here test
    board, item 3 failed before any write, item 4 held and never filed, item 5 an owner override."""
    a = copy.deepcopy(artifact)
    a["status"] = "complete"
    items = a["items"]
    items[0]["publish"] = {"plane:0711da94-aaaa": {
        "status": "published", "provider": "plane", "project": "pilot", "board": "0711da94-aaaa", "ref": "PX-123",
        "url": "https://plane.delo.sh/33god/browse/PX-123/", "marker": "px-harvest:h-x", "lane": "Todo",
        "at": "2026-10-29T11:00:00Z", "here": False}}
    items[1]["publish"] = {"plane:test-board": {
        "status": "published", "provider": "plane", "project": "pilot", "board": "test-board", "ref": "TEST-21",
        "url": "https://plane.delo.sh/33god/browse/TEST-21/", "lane": "Todo", "at": "2026-10-29T11:00:01Z",
        "here": True}}
    items[2]["publish"] = {"trello:687535e9873b89478afef689": {"status": "failed", "reasons": ["HTTP 400"],
                                                               "project": "intelliforia"}}
    items[3]["gate"] = {"state": "held", "reasons": [{"reason": "disagree"}]}
    items[4]["override"] = {"project": "holocene"}
    items[4]["status"] = "tentative"
    return a


class CheckTest(unittest.TestCase):
    def test_the_fixture_is_a_version_2_dry_run(self):
        a = fixture()
        self.assertEqual((a["kind"], a["version"], a["status"]), (harvest.KIND, 2, "dry-run"))
        self.assertIs(harvest.check(a), a)

    def test_pilots_read_rules(self):
        for mutate, message in (
                (lambda a: a.update(kind="px.other"), "not a px harvest artifact"),
                (lambda a: a.update(version=1), "re-harvest"),
                (lambda a: a.update(version=3), "artifact version 3"),
                (lambda a: a.pop("items"), "no items list"),
                (lambda a: a["items"][0].update(gate={"state": "maybe"}), "invalid gate state (maybe)"),
                (lambda a: a["items"][0].pop("gate"), "invalid gate state (missing)")):
            a = fixture()
            mutate(a)
            with self.subTest(message=message), self.assertRaises(harvest.ArtifactError) as raised:
                harvest.check(a, "x.json")
            self.assertIn(message, str(raised.exception))

    def test_status_and_timestamps_are_required(self):
        for mutate in (lambda a: a.update(status="draft"), lambda a: a.pop("harvest_id"),
                       lambda a: a.pop("created_at")):
            a = fixture()
            mutate(a)
            with self.assertRaises(harvest.ArtifactError):
                harvest.parse(a, path="x.json", sha256="0" * 64)


class ParseTest(unittest.TestCase):
    def test_dry_run_rows(self):
        rows = harvest.parse(fixture(), path="/a/actionitems.json", sha256="a" * 64, size=10, mtime_ns=20)
        run = rows["run"]
        self.assertEqual(run["harvest_id"], "hv-8c7d55405a73")
        self.assertEqual(run["status"], "dry-run")
        self.assertEqual(run["wax_item_id"], "0123456789abcdef")
        self.assertEqual(run["body_sha256"], "8c7d5582bc31145f4eaba61ddaa17d66f39cbb0e49e8098879256a5cd4795e25")
        self.assertIsNone(run["s3_uri"])
        self.assertEqual((run["item_count"], run["clear_count"], run["held_count"]), (5, 5, 0))
        self.assertEqual((run["artifact_bytes"], run["artifact_mtime_ns"]), (10, 20))
        self.assertEqual(run["created_at"].isoformat(), "2026-10-29T10:41:44+00:00")
        self.assertEqual(rows["tickets"], [])
        first = rows["items"][0]
        self.assertEqual(first, {"item_id": "h-ba8cf792a194", "ordinal": 1,
                                 "title": "Send Tom a reply about the renewal quote by Friday", "category": "action",
                                 "status": "firm", "gate_state": "clear", "project_id": None,
                                 "group_kind": "general", "quote": "I'll send Tom a reply about the renewal quote by Friday."})

    def test_publish_blocks_become_tickets(self):
        rows = harvest.parse(published(fixture()), path="p", sha256="b" * 64)
        self.assertEqual(rows["run"]["status"], "complete")
        self.assertEqual((rows["run"]["clear_count"], rows["run"]["held_count"]), (4, 1))
        tickets = {t["item_id"]: t for t in rows["tickets"]}
        self.assertEqual(len(tickets), 3)
        real = tickets["h-ba8cf792a194"]
        self.assertEqual((real["provider"], real["board_id"], real["publish_status"], real["ticket_ref"],
                          real["lane"], real["project_id"], real["is_test"]),
                         ("plane", "0711da94-aaaa", "published", "PX-123", "Todo", "pilot", False))
        self.assertEqual(real["published_at"].isoformat(), "2026-10-29T11:00:00+00:00")
        self.assertTrue(tickets["h-a3a2847ece66"]["is_test"])
        failed = tickets["h-a0a8e48b1b95"]
        # A failed/blocked block carries no provider/board: both come from the publish key.
        self.assertEqual((failed["provider"], failed["board_id"], failed["publish_status"], failed["ticket_ref"],
                          failed["published_at"]), ("trello", "687535e9873b89478afef689", "failed", None, None))
        items = {i["item_id"]: i for i in rows["items"]}
        self.assertEqual(items["h-eaf1959dabb7"]["gate_state"], "held")
        self.assertEqual(items["h-dbb20219c102"]["project_id"], "holocene")       # override wins
        self.assertEqual(items["h-dbb20219c102"]["status"], "tentative")

    def test_quote_is_truncated_and_unknown_publish_states_are_skipped(self):
        a = published(fixture())
        a["items"][0]["evidence"]["spans"][0]["quote"] = "q" * 1000
        a["items"][1]["publish"]["plane:test-board"]["status"] = "teleported"
        a["items"][2]["status"] = "maybe"
        rows = harvest.parse(a, path="p", sha256="c" * 64)
        self.assertEqual(len(rows["items"][0]["quote"]), harvest.QUOTE_CHARS)
        self.assertNotIn("h-a3a2847ece66", {t["item_id"] for t in rows["tickets"]})
        self.assertTrue(any("teleported" in w for w in rows["warnings"]))
        self.assertIsNone(rows["items"][2]["status"])

    def test_classifier_project_and_v1_quote_fallback(self):
        a = fixture()
        a["items"][0]["classification"] = {"status": "one", "project": "pilot"}
        a["items"][1]["evidence"] = {"quote": "an old-shape quote"}
        rows = harvest.parse(a, path="p", sha256="d" * 64)
        self.assertEqual(rows["items"][0]["project_id"], "pilot")
        self.assertEqual(rows["items"][1]["quote"], "an old-shape quote")

    def test_a_bad_document_identity_is_dropped_not_trusted(self):
        a = fixture()
        a["document"]["wax_item_id"] = "not-an-id"
        a["document"]["body_sha256"] = "zz"
        run = harvest.parse(a, path="p", sha256="e" * 64)["run"]
        self.assertIsNone(run["wax_item_id"])
        self.assertIsNone(run["body_sha256"])


class DiscoverTest(unittest.TestCase):
    def test_directory_scan_finds_only_actionitems_and_off_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "b-20261029-101010").mkdir()
            (root / "a" / "actionitems.json").write_text("{}")
            (root / "b-20261029-101010" / "actionitems.json").write_text("{}")
            (root / "a" / "actionitems.json.lock").mkdir()
            (root / "notes.json").write_text("{}")
            with patch.dict(os.environ, {harvest.DIR_ENV: str(root)}):
                files, scanned = harvest.discover()
                self.assertEqual(scanned, root)
                self.assertEqual([p.parent.name for p in files], ["a", "b-20261029-101010"])
                one, scanned = harvest.discover([root / "notes.json"])
                self.assertEqual((one, scanned), ([root / "notes.json"], None))
            with patch.dict(os.environ, {harvest.DIR_ENV: "off"}):
                self.assertEqual(harvest.discover(), ([], None))
            with patch.dict(os.environ, {harvest.DIR_ENV: str(root / "missing")}):
                self.assertEqual(harvest.discover()[0], [])

    def test_read_hashes_the_exact_bytes(self):
        rows, facts = harvest.read(FIXTURE)
        self.assertEqual(facts["sha256"], hashlib.sha256(FIXTURE.read_bytes()).hexdigest())
        self.assertEqual(rows["run"]["artifact_sha256"], facts["sha256"])
        self.assertEqual(rows["run"]["artifact_path"], str(FIXTURE.resolve()))
        with tempfile.NamedTemporaryFile("w", suffix=".json") as bad:
            bad.write("not json")
            bad.flush()
            with self.assertRaises(harvest.ArtifactError):
                harvest.read(Path(bad.name))


if __name__ == "__main__":
    unittest.main()
