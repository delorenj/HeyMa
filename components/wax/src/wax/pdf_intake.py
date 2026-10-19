"""PDF intake: the preliminary pass that turns a PDF dropped in a document
binding's folder into a Markdown note beside it, so the binding then picks the
note up as an ordinary new arrival.

Bindings only consider ``.md``/``.markdown`` documents, so a PDF in
``~/d/Inbox/Wax`` used to sit unseen until the vault's Inbox cleanup archived it
to MinIO. Before each post-seed scan this converts every PDF in the folder that
no note there accounts for yet:

- the converter is ``pdf2md`` (the SSOT PDF->Markdown primitive, pymupdf4llm),
  invoked as a CLI; ``WAX_PDF2MD`` overrides the command;
- the note is ``<stem>.md`` beside the PDF (``<stem>-<sha8>.md`` when that name
  is already taken by another note), written through a ``.wax-`` temp and an
  atomic rename so a binding never reads half a note;
- its frontmatter records the source (``source_pdf``, ``source_pdf_sha256``,
  ``source_pdf_pages``) and nothing a pass proposes, so the enrichment pass
  fills the title and summary as it would for any note. ``source_pdf_sha256`` is
  also the contract that tells the Inbox cleanup the PDF is converted and may be
  archived; until then the cleanup holds it;
- the PDF itself is never touched: archiving it stays the cleanup's job;
- a PDF with no text layer (a scan) yields no note — an empty body would be
  deleted by the cleanup as an empty file and then re-converted forever. It
  counts as a failed attempt, and after ``MAX_ATTEMPTS`` this stops trying; the
  cleanup's hold expires and the PDF is archived unconverted.

``WAX_DOCUMENT_PDF_INTAKE=0`` turns the pass off.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from . import frontmatter, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

ENABLED = os.environ.get("WAX_DOCUMENT_PDF_INTAKE", "1") not in ("0", "false", "no", "")
# A PDF still being copied or synced in must not be converted half-written.
MIN_AGE_S = float(os.environ.get("WAX_PDF_INTAKE_MIN_AGE_S", "10"))
TIMEOUT_S = float(os.environ.get("WAX_PDF_INTAKE_TIMEOUT_S", "300"))
MAX_ATTEMPTS = int(os.environ.get("WAX_PDF_INTAKE_MAX_ATTEMPTS", "3"))
SHA_KEY = "source_pdf_sha256"

# sha256 -> failed attempts, for this daemon's lifetime. A restart forgets it,
# which costs at most a few extra attempts on a PDF the cleanup archives anyway.
_failures: dict[str, int] = {}


def converter() -> list[str]:
    cmd = os.environ.get("WAX_PDF2MD")
    if cmd:
        return cmd.split()
    found = shutil.which("pdf2md") or str(Path.home() / ".local/bin/pdf2md")
    return [found]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _converted(folder: Path) -> dict[str, Path]:
    """source_pdf_sha256 -> the note that records it, for every note in the folder."""
    out: dict[str, Path] = {}
    for note in folder.rglob("*.md"):
        fm, _ = frontmatter.read(note)
        sha = fm.get(SHA_KEY) if isinstance(fm, dict) else None
        if isinstance(sha, str):
            out[sha] = note
    return out


def _target(pdf: Path, sha: str) -> Path:
    plain = pdf.with_suffix(".md")
    return plain if not plain.exists() else pdf.with_name(f"{pdf.stem}-{sha[:8]}.md")


def _convert(pdf: Path) -> tuple[str, int]:
    proc = subprocess.run([*converter(), str(pdf), "--json"], capture_output=True,
                          text=True, timeout=TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr.strip().splitlines() or [f"exit {proc.returncode}"])[-1][:300])
    out = json.loads(proc.stdout)
    return out["markdown"], int(out.get("pages") or 0)


def convert_folder(folder: Path, *, now: Optional[float] = None) -> list[dict[str, Any]]:
    """Convert every unaccounted-for PDF under ``folder``; one result per PDF acted on."""
    if not ENABLED or not folder.is_dir():
        return []
    now = time.time() if now is None else now
    pdfs = sorted(p for p in folder.rglob("*")
                  if p.suffix.lower() == ".pdf" and p.is_file()
                  and not any(part.startswith(".") for part in p.relative_to(folder).parts))
    if not pdfs:
        return []
    done = _converted(folder)
    results = []
    for pdf in pdfs:
        try:
            if now - pdf.stat().st_mtime < MIN_AGE_S:
                continue
            sha = _sha256(pdf)
        except OSError:
            continue  # vanished mid-scan: the cleanup took it
        if sha in done or _failures.get(sha, 0) >= MAX_ATTEMPTS:
            continue
        try:
            markdown, pages = _convert(pdf)
            if not markdown.strip():
                raise RuntimeError("no text layer (scanned PDF?); nothing to enrich")
            note = _target(pdf, sha)
            fm = {"source_pdf": pdf.name, SHA_KEY: sha, "source_pdf_pages": pages,
                  "converted_by": "pdf2md", "converted_at": sentinel.utcnow()}
            tmp = note.with_name(f".wax-pdf-{note.name}.tmp")
            tmp.write_text(frontmatter.render(fm, "\n" + markdown.lstrip("\n")))
            os.replace(tmp, note)
        except Exception as exc:  # noqa: BLE001 - one PDF never stops its siblings or the scan
            _failures[sha] = _failures.get(sha, 0) + 1
            error = f"{type(exc).__name__}: {exc}"[:400]
            log.warning("pdf intake could not convert %s (attempt %d/%d): %s",
                        pdf, _failures[sha], MAX_ATTEMPTS, error)
            results.append({"pdf": str(pdf), "converted": False, "attempt": _failures[sha], "error": error})
            continue
        _failures.pop(sha, None)
        done[sha] = note
        log.info("pdf intake converted %s -> %s (%d pages)", pdf.name, note.name, pages)
        results.append({"pdf": str(pdf), "converted": True, "note": str(note), "pages": pages})
    return results
