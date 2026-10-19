"""PDF intake: a PDF in a bound folder becomes a note the same scan enriches.

A fake ``pdf2md`` stands in for the converter; the binding, registry and
pipeline are the document scenario's temp fixtures.
"""

import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from documents_test import _Scenario
from wax import documents, frontmatter, pdf_intake


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
        pdf_intake._failures.clear()
        documents.scan(pipeline="doc")  # seed the empty folder
        return self

    def __exit__(self, *a):
        self.env.stop()
        pdf_intake._failures.clear()
        return super().__exit__(*a)

    def pdf(self, name: str, data: bytes = b"%PDF-1.7 fake", age: float = 60) -> Path:
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
            s.markdown.write_text("  \n")
            s.pdf("scan.pdf")
            for _ in range(pdf_intake.MAX_ATTEMPTS + 2):
                scan = documents.scan(pipeline="doc")["bindings"][0]
            self.assertFalse((s.docs / "scan.md").exists())
            self.assertEqual(s.conversions(), pdf_intake.MAX_ATTEMPTS)
            self.assertNotIn("pdf_intake", scan, "an exhausted PDF is no longer reported every scan")

    def test_a_converter_failure_never_stops_the_binding_scan(self):
        with _Intake() as s:
            s.pdf("broken.pdf")
            s.drop("plain.md")
            with patch.dict(os.environ, {"WAX_PDF2MD": "/bin/false"}):
                scan = documents.scan(pipeline="doc")["bindings"][0]
            self.assertEqual(scan["pdf_intake"][0]["converted"], False)
            self.assertEqual(scan["processed"], 1, "the ordinary note still ran")

    def test_intake_can_be_turned_off(self):
        with _Intake() as s, patch.object(pdf_intake, "ENABLED", False):
            s.pdf("report.pdf")
            documents.scan(pipeline="doc")
            self.assertEqual(s.conversions(), 0)


if __name__ == "__main__":
    unittest.main()
