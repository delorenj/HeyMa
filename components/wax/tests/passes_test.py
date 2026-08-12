import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wax import events, frontmatter, ledger, passes, paths, sentinel


def _close_test_connection() -> None:
    connection = getattr(ledger._local, "conn", None)
    if connection is not None:
        connection.close()
        del ledger._local.conn


class EnrichmentPassContractTest(unittest.TestCase):
    def test_result_renames_note_updates_frontmatter_and_is_version_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            vault = root / "vault"
            registry = root / "passes.d"
            runtime.mkdir()
            vault.mkdir()
            registry.mkdir()
            audio = runtime / "source.ogg"
            audio.write_bytes(b"audio bytes")
            md = vault / "20260810-120000-rec.md"
            md.write_text("---\ntranscribed-at: 2026-08-10T12:00:00-04:00\n---\n# Transcript\nBody\n")
            command = registry / "title-pass"
            command.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '" + json.dumps({
                    "wax_ep_version": 1,
                    "frontmatter": {
                        "title": "Modular Enrichment Pass",
                        "summary": "A grounded summary.",
                        "title-slug": "modular-enrichment-pass",
                    },
                    "transcript": {"slug": "modular-enrichment-pass"},
                }) + "'\n"
            )
            command.chmod(0o755)
            (registry / "title-slug.yaml").write_text(
                "slug: title-slug\nversion: 2\nenabled: true\nauto: true\n"
                f"command: [{json.dumps(str(command))}, \"{{md_path}}\"]\n"
            )

            def fake_frontmatters(args):
                if args[0] != "set":
                    return
                note = Path(args[1])
                updates = {}
                for pair in args[2:]:
                    key, raw = pair.split("=", 1)
                    updates[key] = json.loads(raw)
                frontmatter.merge(note, updates)

            with patch.object(paths, "DB", runtime / "wax.db"), \
                    patch.object(paths, "VAR", runtime), \
                    patch.object(paths, "VAULT", vault), \
                    patch.object(passes, "REGISTRY_DIR", registry), \
                    patch.object(passes, "_run_frontmatters", side_effect=fake_frontmatters), \
                    patch.object(events, "emit_ep_command", return_value="command-id"), \
                    patch.object(events, "emit"):
                _close_test_connection()
                try:
                    item_id = ledger.upsert_item(audio)
                    ledger.connect().execute(
                        "INSERT INTO transcripts(item_id,md_path,created_at) VALUES(?,?,?)",
                        (item_id, str(md), sentinel.utcnow()),
                    )
                    result = passes.run_auto(item_id)
                    renamed = vault / "20260810-120000-modular-enrichment-pass.md"
                    self.assertEqual(result[0]["state"], "completed")
                    self.assertEqual(result[0]["version"], 2)
                    self.assertTrue(renamed.is_file())
                    self.assertFalse(md.exists())
                    row = ledger.connect().execute(
                        "SELECT md_path FROM transcripts WHERE item_id=?", (item_id,),
                    ).fetchone()
                    self.assertEqual(row["md_path"], str(renamed))
                    fm, _ = frontmatter.read(renamed)
                    self.assertEqual(fm["title"], "Modular Enrichment Pass")
                    self.assertEqual(fm["asset-kind"], "transcript")
                    self.assertEqual(fm["wax"]["passes"]["title-slug"]["version"], 2)

                    replacement = {
                        "wax_ep_version": 1,
                        "frontmatter": {
                            "title": "A Different Generated Title",
                            "summary": "A replacement summary.",
                            "title-slug": "a-different-generated-title",
                        },
                        "transcript": {"slug": "a-different-generated-title"},
                    }
                    command.write_text(
                        "#!/bin/sh\nprintf '%s\\n' '" + json.dumps(replacement) + "'\n"
                    )
                    command.chmod(0o755)
                    passes.run(item_id, "title-slug", attempt=2)
                    fm, _ = frontmatter.read(renamed)
                    self.assertEqual(fm["title"], "Modular Enrichment Pass")
                    self.assertTrue(renamed.is_file())

                    rerun = passes.run_auto(item_id)
                    self.assertEqual(rerun[0]["skipped"], "already completed at this version")
                finally:
                    _close_test_connection()


if __name__ == "__main__":
    unittest.main()
