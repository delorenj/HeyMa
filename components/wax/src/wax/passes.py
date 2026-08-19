"""Enrichment Passes: independent, individually tracked, individually traceable.

Independence is the whole design constraint. A pass NEVER gates another pass:
if `wikification` fails, `mem-ops` still runs on the same item, and the failure
is recorded against that one slug rather than stalling the item. There is a
`requires:` field in the registry so the option exists later, but every shipped
pass declares `requires: []` and the runner refuses to honour a non-empty one
without an explicit override — a dependency added by accident is exactly how
"independent passes" quietly becomes a pipeline again.

Traceability: every run mints a DETERMINISTIC command_id
    uuid5(WAX_NS, "ep:<item_id>:<ep_slug>:<attempt>")
issues `bloodbank.cmd.v1.audio.task.start`, and mirrors it as
`...task.requested` carrying that same id. Because Candystore ingests events
only, that mirror is what makes the invoking COMMAND findable at all.

The durable link is **correlationid**, not causationid. Measured: Candystore
persists [actor, cli, correlationid, data, domain, id, producer, project,
service, summary, time, type] and DROPS causationid entirely. We still set
causationid for consumers that keep it, but anything that needs to work against
Candystore must key on correlationid:

    curl 'http://127.0.0.1:8683/events?correlationid=<command_id>'
    -> [task.requested, task.started, task.completed]
"""

import json
import logging
import os
import re
import subprocess
import shutil
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from . import archive, component, events, frontmatter, ledger, rename, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

REGISTRY_DIR = Path(os.environ.get("WAX_PASSES_DIR", component.PASSES))
DEFAULT_TIMEOUT_S = 900
RESULT_VERSION = 1
# A pass that knows why it failed says so on its first stderr line. Exit codes
# cannot carry that: a deleted model and an unreachable provider both exit 1,
# which is how a week of 404s was recorded as an indistinguishable
# "nonzero_exit". Passes that predate the convention simply omit the line.
_REASON_LINE = re.compile(r"^reason_code=([a-z_]+)$")
_PROTECTED_FRONTMATTER = {
    frontmatter.ITEM_KEY,
    frontmatter.WAX_KEY,
    "captured",
    "created_at",
    "source",
    "source-audio",
    "source-s3-key",
    "source-s3-uri",
    "source-sha256",
    "vault-id",
}


class PassError(RuntimeError):
    pass


def registry() -> dict[str, dict[str, Any]]:
    """Load every EP definition from passes.d/*.yaml."""
    out: dict[str, dict[str, Any]] = {}
    if not REGISTRY_DIR.is_dir():
        return out
    for f in sorted(REGISTRY_DIR.glob("*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError as e:
            out[f.stem] = {"slug": f.stem, "enabled": False, "error": f"unparseable: {e}"}
            continue
        slug = doc.get("slug") or f.stem
        doc.setdefault("slug", slug)
        doc.setdefault("enabled", False)
        doc.setdefault("auto", False)
        doc.setdefault("version", 1)
        doc.setdefault("clobber", [])
        doc.setdefault("requires", [])
        doc.setdefault("timeout_s", DEFAULT_TIMEOUT_S)
        doc["_path"] = str(f)
        out[slug] = doc
    return out


def _record(item_id: str, slug: str, state: str, *, version: int = 1, attempt: int = 1,
            command_id: Optional[str] = None, detail: str = "",
            reason_code: Optional[str] = None) -> None:
    conn = ledger.connect()
    columns = ["item_id", "ep_slug", "version", "state", "attempt", "command_id", "updated_at", "detail"]
    values: list[Any] = [item_id, slug, version, state, attempt, command_id, sentinel.utcnow(), detail[:500]]
    # ledger.py owns this table. A ledger that predates the reason_code column
    # still has to say WHY a pass failed, so fall back to the head of detail
    # rather than dropping the one field an operator triages on.
    if "reason_code" in {row["name"] for row in conn.execute("PRAGMA table_info(passes)")}:
        columns.append("reason_code")
        values.append(reason_code)
    elif reason_code:
        values[columns.index("detail")] = f"reason_code={reason_code}\n{detail}"[:500]
    updates = ", ".join(f"{c}=excluded.{c}" for c in columns[2:])
    conn.execute(
        f"INSERT INTO passes({','.join(columns)}) VALUES({','.join('?' * len(columns))}) "
        f"ON CONFLICT(item_id,ep_slug) DO UPDATE SET {updates}",
        values,
    )


def _next_attempt(item_id: str, slug: str) -> int:
    """Attempt is the ONLY entropy in the command_id uuid5, so reusing one
    replays a spent idempotency key. `wax ep run` passed no attempt at all,
    which meant every manual re-run minted the command_id of attempt 1."""
    row = ledger.connect().execute(
        "SELECT MAX(attempt) AS attempt FROM passes WHERE item_id=? AND ep_slug=?",
        (item_id, slug),
    ).fetchone()
    return int((row["attempt"] if row else None) or 0) + 1


def _split_reason(stderr: str) -> tuple[Optional[str], str]:
    """Split a pass's machine-readable `reason_code=<code>` header off stderr.

    Tolerant by design: a pass that does not emit the header keeps its stderr
    verbatim and is classified from its exit code as before.
    """
    head, _, rest = (stderr or "").partition("\n")
    match = _REASON_LINE.match(head.strip())
    if not match:
        return None, stderr or ""
    return match.group(1), rest


def _first_line(detail: str) -> str:
    """One log line per failure: the tail of a traceback is for the ledger."""
    lines = (detail or "").strip().splitlines()
    return lines[0].strip() if lines else "no detail"


def md_for(item_id: str) -> Optional[Path]:
    row = ledger.connect().execute(
        "SELECT md_path FROM transcripts WHERE item_id=?", (item_id,)).fetchone()
    if not row:
        return None
    p = Path(row["md_path"])
    return p if p.is_file() else None


def _expand(value: Any, *, item_id: str, md: Path) -> str:
    return (str(value)
            .replace("{md_path}", str(md))
            .replace("{item_id}", item_id)
            .replace("{component_root}", str(component.ROOT))
            .replace("{home}", str(Path.home())))


def _parse_result(stdout: str) -> dict[str, Any]:
    """Return the last wax.ep.v1 object; ordinary text remains legacy output."""
    for line in reversed((stdout or "").splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or "wax_ep_version" not in value:
            continue
        if value.get("wax_ep_version") != RESULT_VERSION:
            raise PassError(
                f"unsupported enrichment result version {value.get('wax_ep_version')!r}; "
                f"expected {RESULT_VERSION}"
            )
        return value
    return {}


def _frontmatters_command() -> Path:
    configured = os.environ.get("WAX_FRONTMATTERS", "").strip()
    candidates = [Path(configured).expanduser()] if configured else []
    # The uv-tool launcher on this host can outlive its installed package. The
    # source checkout's venv is the known-good installation used by the vault.
    candidates.append(Path.home() / "code" / "frontmatters" / ".venv" / "bin" / "frontmatters")
    discovered = shutil.which("frontmatters")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise PassError("frontmatters editor not found; set WAX_FRONTMATTERS to a working executable")


def _run_frontmatters(args: list[str]) -> None:
    command = _frontmatters_command()
    try:
        result = subprocess.run(
            [str(command), *args], capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PassError(f"frontmatters failed to start: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error")[-800:]
        raise PassError(f"frontmatters exited {result.returncode}: {detail}")


def _apply_base_schema(ep: dict[str, Any], item_id: str, md: Path) -> None:
    """Stamp the vault's base taxonomy onto the note. Best-effort, never fatal.

    This is a purely local, deterministic scaffold, but it used to ride inside
    _apply_result, which is only reached when the child exits 0 — so the
    week-long title-slug LLM outage also withheld a stamp that never needed an
    LLM. It now runs ahead of the child and downgrades every failure to a
    warning: the base schema must not be able to fail a pass either.
    """
    schema = ep.get("frontmatter_schema")
    if not schema:
        return
    expanded = _expand(schema, item_id=item_id, md=md)
    schema_path = Path(os.path.expandvars(os.path.expanduser(expanded)))
    if not schema_path.is_file():
        log.warning("%s: frontmatter schema missing, skipping base stamp: %s",
                    ep.get("slug"), schema_path)
        return
    try:
        _run_frontmatters(["apply-base", str(md), "--schema", str(schema_path)])
    except PassError as exc:
        log.warning("%s: base frontmatter stamp failed: %s", ep.get("slug"), exc)


def _apply_frontmatter(md: Path, updates: dict[str, Any]) -> None:
    """Batch every grounded value from one pass into a single frontmatters set."""
    if not updates:
        return
    invalid = sorted(k for k in updates if not re.fullmatch(r"[A-Za-z0-9_-]+", str(k)))
    if invalid:
        raise PassError(f"invalid frontmatter keys from pass: {invalid}")
    pairs = [f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in updates.items()]
    _run_frontmatters(["set", str(md), *pairs])


def _date_prefix(md: Path, item_id: str) -> str:
    match = re.match(r"^(\d{8}-\d{6})(?:-|$)", md.stem)
    if match:
        return match.group(1)
    row = ledger.connect().execute(
        "SELECT orig_name,first_seen FROM items WHERE item_id=?", (item_id,),
    ).fetchone()
    if row:
        match = re.match(r"^(\d{8}-\d{6})(?:-|$)", row["orig_name"] or "")
        if match:
            return match.group(1)
        try:
            from datetime import datetime
            return datetime.fromisoformat(row["first_seen"].replace("Z", "+00:00")).astimezone().strftime(
                "%Y%m%d-%H%M%S"
            )
        except (TypeError, ValueError):
            pass
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(md.stat().st_mtime))


def _normalise_slug(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    slug = slug[:80].rstrip("-")
    if not slug:
        raise PassError("pass returned an empty transcript slug")
    return slug


def _rename_transcript(md: Path, item_id: str, slug: str) -> Path:
    target = md.with_name(f"{_date_prefix(md, item_id)}-{_normalise_slug(slug)}.md")
    if target == md:
        return md
    final = rename.move_noclobber(md, target)
    ledger.connect().execute(
        "UPDATE transcripts SET md_path=? WHERE item_id=?", (str(final), item_id),
    )
    return final


def _apply_result(item_id: str, md: Path, ep: dict[str, Any], result: dict[str, Any]) -> tuple[Path, list[str]]:
    """Apply a pass's declarative mutations and return the current note path."""
    if not result:
        return md, []
    raw_updates = result.get("frontmatter") or {}
    if not isinstance(raw_updates, dict):
        raise PassError("enrichment result frontmatter must be an object")
    forbidden = sorted(set(raw_updates) & _PROTECTED_FRONTMATTER)
    if forbidden:
        raise PassError(f"pass attempted to overwrite provenance/frontmatter ownership: {forbidden}")
    existing, _ = frontmatter.read(md)
    allowed_clobbers = {str(key) for key in (ep.get("clobber") or [])}
    effective_updates = {
        key: value for key, value in raw_updates.items()
        if (existing.get(key) in (None, "", [], {})
            or existing.get(key) == value
            or key in allowed_clobbers)
    }
    updates = dict(effective_updates)

    # This runner currently targets transcript artifacts only. Fill known base
    # identity/provenance when older notes predate the metadata-first contract;
    # never replace a non-empty upstream value.
    # `captured` is when the AUDIO WAS RECORDED and is derived from the source's
    # mtime by transcribe_adapter; stamping it with transcribed-at here silently
    # rewrote a recording's date to whenever Whisper happened to get to it.
    base_values = {
        "schema-version": 1,
        "asset-kind": "transcript",
        "specialist": "transcripts",
        "source": "audio-recording",
    }
    for key, value in base_values.items():
        if value not in (None, "") and not existing.get(key):
            updates[key] = value

    refs = archive.references(item_id)
    if refs:
        primary = refs[0]
        updates["source-sha256"] = primary["sha256"]
        updates["source-s3-key"] = primary["s3_key"]
        updates["source-s3-uri"] = f"s3://{primary['bucket']}/{primary['s3_key']}"

    # The base schema is stamped by _apply_base_schema before the pass runs, so
    # this stays a pure write of what THIS pass produced.
    _apply_frontmatter(md, updates)

    transcript = result.get("transcript") or {}
    if not isinstance(transcript, dict):
        raise PassError("enrichment result transcript must be an object")
    requested_slug = transcript.get("slug")
    existing_slug = existing.get("title-slug")
    if (existing_slug and requested_slug and existing_slug != requested_slug
            and "title-slug" not in allowed_clobbers):
        requested_slug = existing_slug
    current = _rename_transcript(md, item_id, requested_slug) if requested_slug else md

    # No archive.link_transcript here any more. Linking used to be gated on this
    # pass returning a slug, so when title-slug 404'd the sidecar projection and
    # the S3 tags went with it — the audio<->transcript half of the archive
    # design switched off by an LLM outage. worker.process() now links on the
    # evidence that matters (a transcript row for a backed-up item), after the
    # passes run, so a rename here is still picked up.

    changed = [f"frontmatter.{key}" for key in effective_updates]
    if current != md:
        changed.append("transcript.filename")
    return current, changed


def run(item_id: str, slug: str, *, attempt: Optional[int] = None,
        allow_requires: bool = False) -> dict[str, Any]:
    """Run one pass against one item. Independent of every other pass.

    `attempt` defaults to one past the highest attempt already recorded for
    (item_id, slug) so that a caller which does not track attempts — `wax ep
    run` — cannot mint a command_id that has already been issued.
    """
    reg = registry()
    ep = reg.get(slug)
    if ep is None:
        raise PassError(f"unknown pass {slug!r}; known: {sorted(reg)}")
    if not ep.get("enabled"):
        raise PassError(f"pass {slug!r} is disabled in {ep.get('_path')}")
    if ep.get("requires") and not allow_requires:
        raise PassError(
            f"pass {slug!r} declares requires={ep['requires']}, but passes are "
            "independent by contract; pass allow_requires=True to override")

    attempt = _next_attempt(item_id, slug) if attempt is None else int(attempt)

    md = md_for(item_id)
    if md is None:
        raise PassError(f"no transcript recorded for item {item_id}")

    argv = [_expand(a, item_id=item_id, md=md) for a in (ep.get("command") or [])]
    if not argv:
        raise PassError(f"pass {slug!r} has no command")
    version = int(ep.get("version") or 1)
    pass_env = dict(os.environ)
    for key, value in (ep.get("env") or {}).items():
        pass_env[str(key)] = _expand(value, item_id=item_id, md=md)

    cid = events.emit_ep_command(item_id, slug, argv, attempt)
    _record(item_id, slug, "running", version=version, attempt=attempt, command_id=cid)
    events.emit("task", "started",
                {"ep_slug": slug, "item_id": item_id, "attempt": attempt,
                 "pass_version": version, "command_id": cid, "argv": argv},
                correlationid=cid, causationid=cid, ordering_key=item_id)

    started = time.time()
    _apply_base_schema(ep, item_id, md)
    current_md = md
    changed_fields: list[str] = []
    reason_code: Optional[str] = None
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           env=pass_env, timeout=float(ep.get("timeout_s") or DEFAULT_TIMEOUT_S))
        # Read the reason off the FULL stderr: truncating first would cut away
        # the very end the header is on. Which end to KEEP then depends on who
        # wrote the stderr — a pass that emits the header authored the lines
        # right after it, while an unannotated pass is being read for the tail
        # of a traceback.
        reason_code, stderr_body = _split_reason(r.stderr or "")
        ok, rc = r.returncode == 0, r.returncode
        err = stderr_body[:800] if reason_code else stderr_body[-800:]
        if ok:
            reason_code = None
            try:
                current_md, changed_fields = _apply_result(
                    item_id, md, ep, _parse_result(r.stdout or ""),
                )
            except (archive.ArchiveError, PassError, OSError) as exc:
                ok, reason_code = False, "result_apply_failed"
                err = str(exc)[-800:]
                current_md = md_for(item_id) or md
    except subprocess.TimeoutExpired:
        ok, rc, reason_code = False, None, "timeout"
        err = f"timeout after {ep.get('timeout_s')}s"
    except OSError as e:
        ok, rc, reason_code, err = False, None, "run_error", str(e)

    took = round(time.time() - started, 2)
    state = "completed" if ok else "failed"
    # Exit code alone only ever separated "the process ran" from "it did not";
    # a code the pass reported about itself always wins over that guess.
    failure = None if ok else (reason_code or ("nonzero_exit" if rc is not None else "run_error"))
    _record(item_id, slug, state, version=version, attempt=attempt,
            command_id=cid, detail=err if not ok else "", reason_code=failure)
    events.emit("task", state,
                {"ep_slug": slug, "item_id": item_id, "attempt": attempt,
                 "pass_version": version, "command_id": cid, "duration_s": took,
                 "changed_fields": changed_fields,
                 **({"reason_code": failure, "returncode": rc, "stderr_tail": err}
                    if not ok else {})},
                correlationid=cid, causationid=cid, ordering_key=item_id)

    # The note records its own history, so the vault is self-describing even if
    # the ledger is lost. A bare `title-slug: {state: failed}` was self-describing
    # in name only — the reason lived in the ledger, which nobody opens.
    note_entry: dict[str, Any] = {
        "state": state, "at": sentinel.utcnow(),
        "version": version, "command_id": cid, "attempt": attempt,
    }
    if not ok:
        note_entry["reason_code"] = failure
        note_entry["detail"] = err[:300]
    try:
        frontmatter.merge(current_md, {
            frontmatter.ITEM_KEY: item_id,
            frontmatter.WAX_KEY: {"passes": {slug: note_entry}},
        })
    except OSError:
        pass

    if ok:
        log.info("%s completed for %s in %ss (attempt %d, %d field(s) changed)",
                 slug, item_id, took, attempt, len(changed_fields))
    else:
        log.warning("%s %s for %s (attempt %d, rc=%s): %s",
                    slug, failure, item_id, attempt, rc, _first_line(err))

    return {"item_id": item_id, "ep_slug": slug, "version": version,
            "state": state, "command_id": cid, "duration_s": took,
            "returncode": rc, "md_path": str(current_md), "changed_fields": changed_fields,
            **({"error": err, "reason_code": failure} if not ok else {})}


def run_all(item_id: str) -> list[dict[str, Any]]:
    """Run every enabled pass. One failing pass never stops the others."""
    out = []
    for slug, ep in sorted(registry().items()):
        if not ep.get("enabled"):
            continue
        try:
            out.append(run(item_id, slug))
        except PassError as e:
            # PassError escapes run() only before the child starts: a misconfigured
            # or unrunnable definition, never a failure of the pass's own work.
            _record(item_id, slug, "failed", version=int(ep.get("version") or 1),
                    detail=str(e), reason_code="run_error")
            log.warning("%s run_error for %s: %s", slug, item_id, _first_line(str(e)))
            out.append({"item_id": item_id, "ep_slug": slug, "state": "failed",
                        "error": str(e), "reason_code": "run_error"})
    return out


def run_auto(item_id: str) -> list[dict[str, Any]]:
    """Run enabled auto passes once per version; failures never stop siblings."""
    previous = {
        row["ep_slug"]: dict(row)
        for row in ledger.connect().execute(
            "SELECT * FROM passes WHERE item_id=?", (item_id,),
        ).fetchall()
    }
    out: list[dict[str, Any]] = []
    for slug, ep in sorted(registry().items()):
        if not ep.get("enabled") or not ep.get("auto"):
            continue
        version = int(ep.get("version") or 1)
        prior = previous.get(slug)
        if prior and prior["state"] == "completed" and int(prior.get("version") or 1) >= version:
            out.append({
                "item_id": item_id, "ep_slug": slug, "version": version,
                "state": "completed", "skipped": "already completed at this version",
            })
            continue
        attempt = int(prior["attempt"] or 0) + 1 if prior else 1
        try:
            out.append(run(item_id, slug, attempt=attempt))
        except PassError as exc:
            _record(item_id, slug, "failed", version=version, attempt=attempt,
                    detail=str(exc), reason_code="run_error")
            log.warning("%s run_error for %s (attempt %d): %s",
                        slug, item_id, attempt, _first_line(str(exc)))
            out.append({
                "item_id": item_id, "ep_slug": slug, "version": version,
                "state": "failed", "error": str(exc), "reason_code": "run_error",
            })
    return out


def status(item_id: Optional[str] = None) -> list[dict[str, Any]]:
    conn = ledger.connect()
    if item_id:
        rows = conn.execute("SELECT * FROM passes WHERE item_id=? ORDER BY ep_slug", (item_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM passes ORDER BY updated_at DESC LIMIT 50").fetchall()
    return [dict(r) for r in rows]
