"""PDF intake: a PDF in a bound folder becomes a note the same scan enriches.

A fake ``pdf2md`` stands in for the converter; the binding, registry and
pipeline are the document scenario's temp fixtures.
"""

import os
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from documents_test import _Scenario
from wax import documents, frontmatter, paths, pdf_intake


class _Intake(_Scenario):
    """The document scenario plus a fake pdf2md whose output each test sets."""

    def __enter__(self):
        super().__enter__()
        self.calls = self.root / "pdf2md.calls"
        self.markdown = self.root / "pdf2md.md"
        self.markdown.write_text("# Quarterly Report\n\nRevenue went up.\n")
        fake = self.root / "pdf2md"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"open({str(self.calls)!r}, 'a').write(sys.argv[1] + '\\n')\n"
            f"md = open({str(self.markdown)!r}).read()\n"
            "print(json.dumps({'markdown': md, 'chars': len(md), 'pages': 2, 'source': sys.argv[1]}))\n"
        )
        fake.chmod(0o755)
        self.env = patch.dict(os.environ, {"WAX_PDF2MD": str(fake)})
        self.env.start()
        pdf_intake._hashes.clear()
        documents.scan(pipeline="doc")  # seed the empty folder
        return self

    def __exit__(self, *a):
        self.env.stop()
        pdf_intake._hashes.clear()
        return super().__exit__(*a)

    def pdf(self, name: str, data: bytes = b"%PDF-1.7 fake", age: float = 120) -> Path:
        path = self.docs / name
        path.write_bytes(data)
        then = time.time() - age
        os.utime(path, (then, then))
        return path

    def conversions(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0


class PdfIntakeTest(unittest.TestCase):
    def test_a_pdf_becomes_a_note_the_same_scan_enriches(self):
        with _Intake() as s:
            pdf = s.pdf("report.pdf")
            scan = documents.scan(pipeline="doc")["bindings"][0]
            note = s.docs / "report.md"
            self.assertTrue(note.is_file())
            self.assertEqual(scan["pdf_intake"][0]["converted"], True)
            self.assertEqual(scan["arrivals"], 1, "the note is an arrival in the scan that wrote it")
            self.assertEqual(scan["processed"], 1)
            fm, body = frontmatter.read(note)
            self.assertEqual(fm["source_pdf"], "report.pdf")
            self.assertEqual(fm["source_pdf_sha256"], pdf_intake._sha256(pdf))
            # the vault Inbox cleanup releases the PDF on exactly this line (inbox_cleanup.pdf_is_held)
            self.assertRegex(note.read_text(), rf"(?m)^source_pdf_sha256:\s*['\"]?{pdf_intake._sha256(pdf)}\b")
            self.assertEqual(fm["source_pdf_pages"], 2)
            self.assertEqual(fm["title"], "A Grounded Title", "the pass, not intake, names the note")
            self.assertIn("Revenue went up.", body)
            self.assertEqual(pdf.read_bytes(), b"%PDF-1.7 fake", "the PDF is left for the cleanup to archive")
            self.assertEqual([p.name for p in s.docs.iterdir() if p.name.startswith(".")], [], "no temp left")

    def test_a_converted_pdf_is_never_converted_again(self):
        with _Intake() as s:
            s.pdf("report.pdf")
            documents.scan(pipeline="doc")
            again = documents.scan(pipeline="doc")["bindings"][0]
            self.assertEqual(s.conversions(), 1)
            self.assertNotIn("pdf_intake", again)
            self.assertEqual(again["arrivals"], 0)

    def test_a_taken_name_gets_a_hash_suffix_and_leaves_the_other_note_alone(self):
        with _Intake() as s:
            mine = s.drop("report.md", "---\ntitle: Mine\n---\nHand written.\n")
            pdf = s.pdf("report.pdf")
            documents.scan(pipeline="doc")
            sha = pdf_intake._sha256(pdf)
            converted = s.docs / f"report-{sha[:8]}.md"
            self.assertTrue(converted.is_file())
            self.assertEqual(frontmatter.read(converted)[0]["source_pdf_sha256"], sha)
            self.assertIn("Hand written.", mine.read_text())

    def test_a_pdf_still_arriving_waits(self):
        with _Intake() as s:
            s.pdf("busy.pdf", age=0)
            documents.scan(pipeline="doc")
            self.assertEqual(s.conversions(), 0)
            self.assertFalse((s.docs / "busy.md").exists())

    def test_a_pdf_with_no_text_writes_no_note_and_stops_after_its_budget(self):
        with _Intake() as s:
            s.markdown.write_text("-----\n\n-----\n\n-----\n\n")  # what pymupdf4llm gives a scan
            s.pdf("scan.pdf")
            for _ in range(pdf_intake.MAX_ATTEMPTS + 2):
                scan = documents.scan(pipeline="doc")["bindings"][0]
            self.assertFalse((s.docs / "scan.md").exists())
            self.assertEqual(s.conversions(), pdf_intake.MAX_ATTEMPTS)
            self.assertNotIn("pdf_intake", scan, "an exhausted PDF is no longer reported every scan")
            pdf_intake._hashes.clear()  # a daemon restart keeps the budget: it is on disk
            documents.scan(pipeline="doc")
            self.assertEqual(s.conversions(), pdf_intake.MAX_ATTEMPTS)

    def test_a_converter_failure_never_stops_the_binding_scan(self):
        with _Intake() as s:
            s.pdf("broken.pdf")
            s.drop("plain.md")
            with patch.dict(os.environ, {"WAX_PDF2MD": "/bin/false"}):
                scan = documents.scan(pipeline="doc")["bindings"][0]
            self.assertEqual(scan["pdf_intake"][0]["converted"], False)
            self.assertEqual(scan["processed"], 1, "the ordinary note still ran")

    def test_page_separators_are_not_kept_as_text(self):
        with _Intake() as s:
            s.markdown.write_text("# Report\n\nRevenue went up this quarter.\n\n-----\n\nPage two text here.\n\n-----\n\n")
            s.pdf("report.pdf")
            documents.scan(pipeline="doc")
            body = frontmatter.read(s.docs / "report.md")[1]
            self.assertNotIn("-----", body)
            self.assertIn("Page two text here.", body)

    def test_a_pdf_already_there_at_seed_is_history_not_an_arrival(self):
        with _Scenario() as s, patch.dict(os.environ, {"WAX_PDF2MD": "/bin/false"}):
            old = s.docs / "old.pdf"
            old.write_bytes(b"%PDF old")
            then = time.time() - 3600
            os.utime(old, (then, then))
            documents.scan(pipeline="doc")  # seeds
            later = documents.scan(pipeline="doc")["bindings"][0]
            self.assertNotIn("pdf_intake", later)

    def test_a_deleted_or_filed_note_never_brings_its_pdf_back(self):
        with _Intake() as s:
            s.pdf("report.pdf")
            documents.scan(pipeline="doc")
            (s.docs / "report.md").unlink()
            documents.scan(pipeline="doc")
            self.assertEqual(s.conversions(), 1)
            self.assertFalse((s.docs / "report.md").exists())

    def test_a_taken_suffixed_name_is_never_overwritten(self):
        with _Intake() as s:
            pdf = s.pdf("report.pdf")
            sha = pdf_intake._sha256(pdf)
            s.drop("report.md", "Plain.\n")
            taken = s.drop(f"report-{sha[:8]}.md", "Precious.\n")
            documents.scan(pipeline="doc")
            self.assertEqual(frontmatter.read(taken)[1], "Precious.\n")
            self.assertNotIn("source_pdf_sha256", frontmatter.read(taken)[0])
            self.assertTrue((s.docs / f"report-{sha[:8]}-2.md").is_file())

    def test_a_folder_another_scan_is_converting_is_left_to_it(self):
        with _Intake() as s:
            s.pdf("report.pdf")
            binding = documents.bindings(pipeline="doc")[0]
            lock = documents.DocumentRunLock(
                paths.VAR / "doc-locks" / f"{pdf_intake._safe(binding['binding_id'])}__pdf-intake.lock")
            self.assertTrue(lock.acquire())
            try:
                documents.scan(pipeline="doc")
            finally:
                lock.release()
            self.assertEqual(s.conversions(), 0)

    def test_the_scan_budget_defers_the_rest(self):
        with _Intake() as s, patch.object(pdf_intake, "SCAN_BUDGET_S", 0):
            s.pdf("a.pdf", b"%PDF a")
            s.pdf("b.pdf", b"%PDF b")
            scan = documents.scan(pipeline="doc")["bindings"][0]
            self.assertEqual(s.conversions(), 0)
            self.assertEqual(scan["pdf_intake"], [{"deferred": 2}])

    def test_a_timed_out_converter_is_killed_with_its_children(self):
        with _Intake() as s, patch.object(pdf_intake, "TIMEOUT_S", 1):
            pidfile = s.root / "child.pid"
            hang = s.root / "hang"
            hang.write_text("#!/bin/sh\nsleep 60 &\necho $! > " + str(pidfile) + "\nwait\n")
            hang.chmod(0o755)
            s.pdf("slow.pdf")
            with patch.dict(os.environ, {"WAX_PDF2MD": str(hang)}):
                scan = documents.scan(pipeline="doc")["bindings"][0]
            self.assertIn("timed out", scan["pdf_intake"][0]["error"])
            child = int(pidfile.read_text())
            time.sleep(0.2)
            stat = Path(f"/proc/{child}/stat")
            self.assertTrue(not stat.exists() or stat.read_text().split()[2] == "Z", "the grandchild died too")

    def test_intake_can_be_turned_off(self):
        with _Intake() as s, patch.object(pdf_intake, "ENABLED", False):
            s.pdf("report.pdf")
            documents.scan(pipeline="doc")
            self.assertEqual(s.conversions(), 0)


if __name__ == "__main__":
    unittest.main()
