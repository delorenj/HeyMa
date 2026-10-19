"""PDF intake: the preliminary pass that turns a PDF dropped in a document
binding's folder into a Markdown note beside it, so the binding then picks the
note up as an ordinary new arrival.

Bindings only consider ``.md``/``.markdown`` documents, so a PDF in
``~/d/Inbox/Wax`` used to sit unseen until the vault's Inbox cleanup archived it
to MinIO. Before each post-seed scan this converts every PDF in the folder that
no note there accounts for yet:

- the converter is ``pdf2md`` (the SSOT PDF->Markdown primitive, pymupdf4llm),
  invoked as a CLI; ``WAX_PDF2MD`` overrides the command;
- the note is ``<stem>.md`` beside the PDF (``<stem>-<sha8>[-n].md`` when that
  name is taken), written to a ``.wax-pdf-`` temp and hard-linked into place, so
  a binding never reads half a note and no existing note is ever overwritten;
- only PDFs that arrived after the binding seeded are converted (new arrivals
  only, like the notes themselves), each PDF content once per binding: the
  conversions are recorded under ``var/pdf-intake/``, so deleting or filing the
  note never brings the PDF back as a second, unenriched note;
- one scan spends at most ``SCAN_BUDGET_S`` converting and defers the rest to
  the next scan, so a heavy PDF never stalls the voice inbox for long; a
  converter past ``TIMEOUT_S`` is killed with its whole process group;
- its frontmatter records the source (``source_pdf``, ``source_pdf_sha256``,
  ``source_pdf_pages``) and nothing a pass proposes, so the enrichment pass
  fills the title and summary as it would for any note. ``source_pdf_sha256`` is
  also the contract that tells the Inbox cleanup the PDF is converted and may be
  archived; until then the cleanup holds it;
- the PDF itself is never touched: archiving it stays the cleanup's job;
- a PDF with no text layer (a scan) yields no note: pymupdf4llm's page
  separators are not text, and a note of nothing would only get an invented
  title. It counts as a failed attempt; after ``MAX_ATTEMPTS`` (recorded, so a
  restart does not reset it) intake gives up loudly, the cleanup's hold expires
  and the PDF is archived unconverted.

``WAX_DOCUMENT_PDF_INTAKE=0`` turns the pass off.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from . import frontmatter, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

ENABLED = os.environ.get("WAX_DOCUMENT_PDF_INTAKE", "1") not in ("0", "false", "no", "")
# A PDF still being copied or synced in must not be converted half-written.
MIN_AGE_S = float(os.environ.get("WAX_PDF_INTAKE_MIN_AGE_S", "60"))
TIMEOUT_S = float(os.environ.get("WAX_PDF_INTAKE_TIMEOUT_S", "300"))
# No new conversion starts once a scan has spent this long converting.
SCAN_BUDGET_S = float(os.environ.get("WAX_PDF_INTAKE_SCAN_BUDGET_S", "60"))
MAX_ATTEMPTS = int(os.environ.get("WAX_PDF_INTAKE_MAX_ATTEMPTS", "3"))
# Fewer word characters than this is not a document worth enriching.
MIN_TEXT_CHARS = 20
SHA_KEY = "source_pdf_sha256"
PAGE_SEPARATOR = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)

# (path, size, mtime_ns) -> sha256, so a PDF left in the folder is hashed once, not every scan.
_hashes: dict[tuple[str, int, int], str] = {}


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


def _cached_sha(path: Path, st: os.stat_result) -> str:
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _hashes:
        _hashes[key] = _sha256(path)
    return _hashes[key]


def _safe(binding_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", binding_id or "unbound")


def _state_path(binding_id: str) -> Path:
    from . import paths
    return paths.VAR / "pdf-intake" / f"{_safe(binding_id)}.json"


def _load(binding_id: str) -> dict[str, Any]:
    try:
        state = json.loads(_state_path(binding_id).read_text())
    except (OSError, ValueError):
        state = {}
    state.setdefault("converted", {})
    state.setdefault("failed", {})
    return state


def _save(binding_id: str, state: dict[str, Any]) -> None:
    path = _state_path(binding_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, path)


def _convert(pdf: Path) -> tuple[str, int]:
    """Run the converter in its own process group: ``pdf2md`` is ``exec uv run``,
    and killing uv alone on a timeout leaves the python converter running."""
    proc = subprocess.Popen([*converter(), str(pdf), "--json"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        raise RuntimeError(f"converter timed out after {TIMEOUT_S:.0f}s")
    if proc.returncode != 0:
        raise RuntimeError((err.strip().splitlines() or [f"exit {proc.returncode}"])[-1][:300])
    data = json.loads(out)
    return data["markdown"], int(data.get("pages") or 0)


def _body(markdown: str) -> str:
    """The converter's text without its page separators; raises when that leaves no document."""
    body = PAGE_SEPARATOR.sub("", markdown)
    body = re.sub(r"\n{3,}", "\n\n", body).strip("\n")
    if len(re.findall(r"\w", body)) < MIN_TEXT_CHARS:
        raise RuntimeError("no text layer (scanned or blank PDF?); nothing to enrich")
    return body + "\n"


def _place(pdf: Path, sha: str, text: str) -> Path:
    """Write the note beside the PDF under the first free name; never replace an existing file."""
    fd, tmp_name = tempfile.mkstemp(prefix=".wax-pdf-", suffix=".tmp", dir=pdf.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        names = [f"{pdf.stem}.md", f"{pdf.stem}-{sha[:8]}.md",
                 *(f"{pdf.stem}-{sha[:8]}-{n}.md" for n in range(2, 100))]
        for name in names:
            try:
                os.link(tmp, pdf.with_name(name))  # atomic, and fails if the name exists
                return pdf.with_name(name)
            except FileExistsError:
                continue
        raise RuntimeError("no free note name beside the PDF")
    finally:
        tmp.unlink(missing_ok=True)


def convert_binding(binding: dict[str, Any], *, now: Optional[float] = None) -> list[dict[str, Any]]:
    """Convert the PDFs that arrived in a seeded binding's folder; one result per PDF acted on."""
    from . import documents  # lazy: documents imports this module on scan

    folder = Path(binding["folder"])
    seeded_at = documents._parse_iso(binding.get("seeded_at"))
    if not ENABLED or seeded_at is None or not folder.is_dir():
        return []
    pdfs = sorted(p for p in folder.rglob("*")
                  if p.suffix.lower() == ".pdf" and p.is_file()
                  and not any(part.startswith(".") for part in p.relative_to(folder).parts))
    if not pdfs:
        return []
    from . import paths
    lock = documents.DocumentRunLock(paths.VAR / "doc-locks" / f"{_safe(binding['binding_id'])}__pdf-intake.lock")
    if not lock.acquire():
        return []  # another scan (waxd or an explicit CLI run) is converting this folder right now
    try:
        return _convert_pending(binding["binding_id"], pdfs, seeded_at,
                                time.time() if now is None else now)
    finally:
        lock.release()


def _convert_pending(binding_id: str, pdfs: list[Path], seeded_at: float, now: float) -> list[dict[str, Any]]:
    state = _load(binding_id)
    results: list[dict[str, Any]] = []
    started = time.monotonic()
    deferred = 0
    for pdf in pdfs:
        try:
            st = pdf.stat()
            if max(st.st_mtime, st.st_ctime) < seeded_at + 1.0:  # seeded_at is ms-truncated
                continue  # in the folder before the binding seeded: history, not an arrival
            if now - st.st_mtime < MIN_AGE_S:
                continue
            sha = _cached_sha(pdf, st)
        except OSError:
            continue  # vanished mid-scan: the cleanup took it
        failed = state["failed"].get(sha, {})
        if sha in state["converted"] or failed.get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        if time.monotonic() - started >= SCAN_BUDGET_S:
            deferred += 1
            continue
        try:
            markdown, pages = _convert(pdf)
            fm = {"source_pdf": pdf.name, SHA_KEY: sha, "source_pdf_pages": pages,
                  "converted_by": "pdf2md", "converted_at": sentinel.utcnow()}
            note = _place(pdf, sha, frontmatter.render(fm, "\n" + _body(markdown)))
        except Exception as exc:  # noqa: BLE001 - one PDF never stops its siblings or the scan
            attempts = failed.get("attempts", 0) + 1
            error = f"{type(exc).__name__}: {exc}"[:400]
            state["failed"][sha] = {"pdf": str(pdf), "attempts": attempts, "error": error,
                                    "at": sentinel.utcnow()}
            _save(binding_id, state)
            if attempts >= MAX_ATTEMPTS:
                log.error("pdf intake gave up on %s after %d attempts (%s); the Inbox cleanup "
                          "will archive it unconverted", pdf, attempts, error)
            else:
                log.warning("pdf intake could not convert %s (attempt %d/%d): %s",
                            pdf, attempts, MAX_ATTEMPTS, error)
            results.append({"pdf": str(pdf), "converted": False, "attempt": attempts,
                            "gave_up": attempts >= MAX_ATTEMPTS, "error": error})
            continue
        state["failed"].pop(sha, None)
        state["converted"][sha] = {"pdf": str(pdf), "note": str(note), "at": sentinel.utcnow()}
        _save(binding_id, state)
        log.info("pdf intake converted %s -> %s (%d pages)", pdf.name, note.name, pages)
        results.append({"pdf": str(pdf), "converted": True, "note": str(note), "pages": pages})
    if deferred:
        log.info("pdf intake deferred %d PDF(s) to the next scan (scan budget %.0fs spent)",
                 deferred, SCAN_BUDGET_S)
        results.append({"deferred": deferred})
    return results
