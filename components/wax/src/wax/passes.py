"""Enrichment Passes: independent, individually tracked, individually traceable.

Independence is the whole design constraint. A pass never fails because a
SIBLING failed: if `wikification` fails, `mem-ops` still runs on the same item,
and the failure is recorded against that one slug rather than stalling the item.

The one deliberate coupling is the registry's `requires:` list, and it is
honoured unconditionally. ordered() sorts a pass after everything it requires;
run() additionally refuses to execute it until each requirement is `completed`
or `skipped` AT THE LIVE REGISTRY VERSION, and records `dependency_failed`
(ledger row + `wax.passes.<slug>` note entry saying what it is waiting on)
otherwise. Declare `requires` only where a pass consumes another's output — the
dependent really cannot run without it. `after:` only orders: it is a sequencing
preference with no gate, and a failed `after` target never stops its dependent.
Both fields must name ENABLED passes; an `auto` pass may only `require` other
`auto` passes (ensure_plan), because a plan holds nothing else.

Events are a HOST effect, so a pass only declares them: a result may carry an
`events` list (validated by _validated_events against the registry's `emits`
allowlist, before any mutation) and finalize.py publishes them after
`transcription.completed`. A pass cannot emit "after completed" itself — it runs
before completion exists — and an event published from inside a pass that later
fails would announce work that never happened.

Traceability: every run mints a DETERMINISTIC command_id
    uuid5(WAX_NS, "ep:<item_id>:<ep_slug>:<attempt>")
issues `bloodbank.cmd.audio.task.start`, and mirrors it as
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

import tempfile
from functools import wraps

from . import archive, component, events, frontmatter, ledger, operations, paths, rename, sentinel

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


# Pass-declared events (result key `events`). Limits are enforced on the way IN,
# in _apply_result, so a violation fails that one pass with result_apply_failed
# before anything touches the note — finalize() never has to defend against them.
_EVENT_TYPE = re.compile(r"^[a-z][a-z_]*\.[a-z][a-z_]*$")
_EVENT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")
MAX_EVENTS_PER_RESULT = 50
MAX_EVENT_DATA_BYTES = 32 * 1024


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


def serialized(function):
    @wraps(function)
    def call(item_id, *args, **kwargs):
        with operations.item_lock(item_id):
            return function(item_id, *args, **kwargs)
    return call


def ordered(reg):
    selected = {slug: ep for slug, ep in reg.items() if ep.get("enabled")}
    visiting, visited, output = set(), set(), []

    def visit(slug):
        if slug in visited:
            return
        if slug in visiting:
            raise PassError(f"cyclic pass dependency: {slug}")
        visiting.add(slug)
        ep = selected[slug]
        for field in ("requires", "after"):
            dependencies = ep.get(field) or []
            if not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies):
                raise PassError(f"invalid {field} for {slug}")
            for dependency in dependencies:
                if dependency not in selected:
                    raise PassError(f"missing enabled dependency {dependency} for {slug}")
                visit(dependency)
        condition = ep.get("skip_when")
        if condition and (not isinstance(condition, dict) or set(condition) != {"field", "equals"}
                          or not isinstance(condition["field"], str)):
            raise PassError(f"invalid skip condition for {slug}")
        visiting.remove(slug)
        visited.add(slug)
        output.append(slug)

    for slug in sorted(selected):
        visit(slug)
    return output


def ensure_plan(item_id):
    conn = ledger.connect()
    row = conn.execute("SELECT definitions FROM processing_plans WHERE item_id=?", (item_id,)).fetchone()
    reg = registry()
    order = ordered(reg)
    definitions = {slug: reg[slug] for slug in order if reg[slug].get("auto")}
    if row:
        receipt = conn.execute("SELECT 1 FROM completions WHERE item_id=?", (item_id,)).fetchone()
        if receipt:
            return json.loads(row["definitions"])
        if json.loads(row["definitions"]) == definitions:
            return definitions
    for slug, ep in definitions.items():
        if any(dependency not in definitions for dependency in ep.get("requires") or []):
            raise PassError(f"automatic pass {slug} requires a manual pass")
    encoded = json.dumps(definitions, sort_keys=True, default=str)
    import hashlib
    plan_id = hashlib.sha256(encoded.encode()).hexdigest()
    conn.execute("INSERT INTO processing_plans(item_id,plan_id,definitions,created_at) VALUES(?,?,?,?) "
                 "ON CONFLICT(item_id) DO UPDATE SET plan_id=excluded.plan_id,definitions=excluded.definitions",
                 (item_id, plan_id, encoded, sentinel.utcnow()))
    return definitions


def _record(item_id: str, slug: str, state: str, *, version: int = 1, attempt: int = 1,
             command_id: Optional[str] = None, detail: str = "",
             reason_code: Optional[str] = None, result=None, definition=None) -> None:
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
    columns.extend(["result", "definition_id"])
    values.extend([json.dumps(result or {}, ensure_ascii=False, default=str),
                   operations.definition_id(definition) if definition else None])
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


def _validated_events(ep: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate a result's optional `events` list; return it, or raise PassError.

    Events are published later by finalize() under a content-keyed id, so the
    only moment a malformed one can be refused cheaply — and cost the pass
    nothing but its own state — is here. The registry `emits` allowlist is
    checked lazily, per result, so a typo in one pass's YAML fails that pass's
    runs rather than raising out of ordered() and taking down every item.

    Messages carry indexes and shapes, never `data` content: a transcript-derived
    description does not belong in a ledger `detail` or a journal line.
    """
    slug = ep.get("slug") or "?"
    emits = ep.get("emits")
    if emits is not None and (not isinstance(emits, list)
                              or any(not isinstance(t, str) or not _EVENT_TYPE.match(t) for t in emits)):
        raise PassError(f"invalid emits for {slug}: expected a list of '<entity>.<action>' strings")
    declared = result.get("events")
    if declared is None:
        return []
    if not isinstance(declared, list):
        raise PassError("enrichment result events must be a list")
    if not declared:
        return []
    if len(declared) > MAX_EVENTS_PER_RESULT:
        raise PassError(f"enrichment result declares {len(declared)} events; limit is {MAX_EVENTS_PER_RESULT}")
    if not emits:
        raise PassError(f"pass {slug} returned events but its registry entry declares no emits")
    seen: set[str] = set()
    for index, event in enumerate(declared):
        where = f"events[{index}]"
        if not isinstance(event, dict):
            raise PassError(f"{where} must be an object")
        if set(event) != {"type", "key", "data"}:
            raise PassError(f"{where} must have exactly the keys type, key, data")
        event_type, key, data = event["type"], event["key"], event["data"]
        if not isinstance(event_type, str) or not _EVENT_TYPE.match(event_type):
            raise PassError(f"{where}.type must match <entity>.<action> in lowercase")
        if event_type not in emits:
            raise PassError(f"{where}.type {event_type} is not declared in {slug} emits")
        if not isinstance(key, str) or not _EVENT_KEY.match(key):
            raise PassError(f"{where}.key must match {_EVENT_KEY.pattern}")
        if key in seen:
            raise PassError(f"{where}.key duplicates an earlier event key")
        seen.add(key)
        if not isinstance(data, dict):
            raise PassError(f"{where}.data must be an object")
        if "project" in data:
            raise PassError(f"{where}.data must not contain 'project': the envelope owns it")
        try:
            encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise PassError(f"{where}.data is not JSON-serializable") from None
        if len(encoded.encode("utf-8")) > MAX_EVENT_DATA_BYTES:
            raise PassError(f"{where}.data exceeds {MAX_EVENT_DATA_BYTES} bytes")
    return declared


def _apply_result(item_id: str, md: Path, ep: dict[str, Any], result: dict[str, Any]) -> tuple[Path, list[str]]:
    """Apply a pass's declarative mutations and return the current note path."""
    if not result:
        return md, []
    # First, before the note, the ledger or the filesystem is touched: a rejected
    # event list must leave the pass's other proposals unapplied too, or the
    # recorded `failed` would sit next to a half-enriched note.
    _validated_events(ep, result)
    raw_updates = result.get("frontmatter") or {}
    if not isinstance(raw_updates, dict):
        raise PassError("enrichment result frontmatter must be an object")
    forbidden = sorted(set(raw_updates) & _PROTECTED_FRONTMATTER)
    if forbidden:
        raise PassError(f"pass attempted to overwrite provenance/frontmatter ownership: {forbidden}")
    existing, _ = frontmatter.read(md)
    if raw_updates.get("classification") not in (None, "monolog", "meeting", "other"):
        raise PassError("invalid classification")
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

    transcript = result.get("transcript") or {}
    if not isinstance(transcript, dict):
        raise PassError("enrichment result transcript must be an object")
    requested_slug = transcript.get("slug")
    existing_slug = existing.get("title-slug")
    if existing_slug and requested_slug and existing_slug != requested_slug and "title-slug" not in allowed_clobbers:
        requested_slug = existing_slug
    if requested_slug:
        requested_slug = _normalise_slug(requested_slug)
    original = md.read_bytes()
    _, body = frontmatter.split(original.decode("utf-8"))
    replacement = result.get("body_replace")
    if replacement is not None:
        import hashlib
        if ep.get("body_mutation") != "compare-and-replace":
            raise PassError("pass is not authorized to replace the transcript body")
        if not isinstance(replacement, dict) or set(replacement) != {"sha256", "text"}:
            raise PassError("invalid body replacement intent")
        if not isinstance(replacement["text"], str) or len(replacement["text"].encode()) > 16 * 1024 * 1024:
            raise PassError("body replacement exceeds policy")
        if hashlib.sha256(body.encode()).hexdigest() != replacement["sha256"]:
            raise PassError("transcript body changed; preserving human edits")
    fd, name = tempfile.mkstemp(prefix=".wax-ep-", suffix=".md", dir=md.parent)
    staging = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(original)
        _apply_frontmatter(staging, updates)
        if replacement is not None:
            staged_fm, _ = frontmatter.read(staging)
            staging.write_text(frontmatter.render(staged_fm, replacement["text"]), encoding="utf-8")
        if md.read_bytes() != original:
            raise PassError("document changed during pass application; preserving human edits")
        os.chmod(staging, md.stat().st_mode)
        with staging.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(staging, md)
        current = _rename_transcript(md, item_id, requested_slug) if requested_slug else md
    finally:
        staging.unlink(missing_ok=True)
    if replacement is not None:
        ledger.connect().execute("UPDATE transcripts SET diarized=1 WHERE item_id=?", (item_id,))

    changed = [f"frontmatter.{key}" for key in effective_updates]
    if replacement is not None:
        changed.append("transcript.body")
    if current != md:
        changed.append("transcript.filename")
    return current, changed


def _waiting_on(dependency: str, row, wanted) -> str:
    """Why a `requires` entry is unmet, in the shape an operator can act on."""
    if not row:
        return f"waiting on {dependency} (missing)"
    seen = f"{row['state']}@v{row['version']}"
    if row["state"] in ("completed", "skipped") and wanted is not None:
        # Satisfied except for the version: a bumped dependency invalidates
        # every dependent's earlier run, and "completed@v1" alone reads as fine.
        seen += f", need v{wanted}"
    return f"waiting on {dependency} ({seen})"


def _write_pass_note(md: Path, item_id: str, slug: str, entry: dict[str, Any]) -> None:
    """Replace `wax.passes.<slug>` in the note WHOLESALE.

    frontmatter.merge deep-merges, which is wrong for a history entry: a pass
    that failed with `reason_code`/`detail` and later completed kept both keys,
    so its note said "completed" next to "dependency_failed: waiting on …".
    """
    fm, body = frontmatter.read(md)
    fm[frontmatter.ITEM_KEY] = item_id
    block = fm.get(frontmatter.WAX_KEY)
    if not isinstance(block, dict):
        block = fm[frontmatter.WAX_KEY] = {}
    history = block.get("passes")
    if not isinstance(history, dict):
        history = block["passes"] = {}
    history[slug] = entry
    tmp = md.with_suffix(md.suffix + ".tmp")
    tmp.write_text(frontmatter.render(fm, body))
    os.replace(tmp, md)


@serialized
def run(item_id: str, slug: str, *, attempt: Optional[int] = None,
         definition=None) -> dict[str, Any]:
    """Run one pass against one item. Independent of its siblings, gated only by `requires`.

    `attempt` defaults to one past the highest attempt already recorded for
    (item_id, slug) so that a caller which does not track attempts — `wax ep
    run` — cannot mint a command_id that has already been issued.
    """
    reg = registry()
    ordered(reg)
    ep = definition or reg.get(slug)
    if ep is None:
        raise PassError(f"unknown pass {slug!r}; known: {sorted(reg)}")
    if not ep.get("enabled"):
        raise PassError(f"pass {slug!r} is disabled in {ep.get('_path')}")
    attempt = _next_attempt(item_id, slug) if attempt is None else int(attempt)
    for dependency in ep.get("requires") or []:
        row = ledger.connect().execute(
            "SELECT state,version FROM passes WHERE item_id=? AND ep_slug=?", (item_id, dependency)
        ).fetchone()
        wanted = (reg.get(dependency) or {}).get("version")
        if row and row["state"] in ("completed", "skipped") and row["version"] == wanted:
            continue
        # A gate that fires is a real attempt. It used to be recorded as attempt 1
        # every time, which reset the counter that bounds the sweep AND let the
        # dependent's next real run reuse a command_id already spent. It still
        # emits no task events (no command was issued) but must say, in the note
        # where people look, why this pass has not produced anything.
        version = int(ep.get("version") or 1)
        detail = _waiting_on(dependency, row, wanted)
        _record(item_id, slug, "failed", version=version, attempt=attempt,
                detail=detail, reason_code="dependency_failed", definition=ep)
        md = md_for(item_id)
        if md is not None:
            try:
                _write_pass_note(md, item_id, slug, {
                    "state": "failed", "at": sentinel.utcnow(), "version": version,
                    "attempt": attempt, "reason_code": "dependency_failed", "detail": detail,
                })
            except OSError:
                pass
        log.warning("%s dependency_failed for %s (attempt %d): %s", slug, item_id, attempt, detail)
        return {"item_id": item_id, "ep_slug": slug, "version": version, "state": "failed",
                "attempt": attempt, "reason_code": "dependency_failed", "error": detail}

    md = md_for(item_id)
    if md is None:
        raise PassError(f"no transcript recorded for item {item_id}")

    skip_reason = None
    if ep.get("kind") == "diarization" and os.environ.get("WAX_DIARIZATION", "").lower() in {"0", "false", "no", "off"}:
        skip_reason = "explicitly_disabled"
    condition = ep.get("skip_when")
    if condition:
        fm, _ = frontmatter.read(md)
        if fm.get(condition["field"]) == condition["equals"]:
            skip_reason = "condition_matched"
    if skip_reason:
        _record(item_id, slug, "skipped", version=int(ep["version"]), attempt=attempt,
                reason_code=skip_reason, result={"skip_reason": skip_reason}, definition=ep)
        frontmatter.merge(md, {frontmatter.WAX_KEY: {"passes": {slug: {
            "state": "skipped", "version": int(ep["version"]), "reason_code": skip_reason,
        }}}})
        return {"item_id": item_id, "ep_slug": slug, "state": "skipped", "reason_code": skip_reason}

    argv = [_expand(a, item_id=item_id, md=md) for a in (ep.get("command") or [])]
    if not argv:
        raise PassError(f"pass {slug!r} has no command")
    version = int(ep.get("version") or 1)
    pass_env = dict(os.environ)
    for key, value in (ep.get("env") or {}).items():
        pass_env[str(key)] = _expand(value, item_id=item_id, md=md)

    cid = events.emit_ep_command(item_id, slug, argv, attempt)
    _record(item_id, slug, "running", version=version, attempt=attempt, command_id=cid, definition=ep)
    events.emit("task", "started",
                {"ep_slug": slug, "item_id": item_id, "attempt": attempt,
                 "pass_version": version, "command_id": cid, "argv": argv},
                correlationid=cid, causationid=cid, ordering_key=item_id)

    started = time.time()
    _apply_base_schema(ep, item_id, md)
    current_md = md
    changed_fields: list[str] = []
    reason_code: Optional[str] = None
    pass_result = {}
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
                pass_result = _parse_result(r.stdout or "")
                current_md, changed_fields = _apply_result(item_id, md, ep, pass_result)
                effective, _ = frontmatter.read(current_md)
                pass_result = {**pass_result, "effective_metadata": {
                    key: effective.get(key) for key in (pass_result.get("frontmatter") or {})
                }}
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
    if ok and pass_result.get("state") == "skipped":
        state = "skipped"
        reason_code = pass_result.get("reason_code") or "skipped"
    
    # Exit code alone only ever separated "the process ran" from "it did not";
    # a code the pass reported about itself always wins over that guess.
    failure = reason_code if state == "skipped" else (None if ok else (reason_code or ("nonzero_exit" if rc is not None else "run_error")))
    _record(item_id, slug, state, version=version, attempt=attempt,
            command_id=cid, detail=err if not ok else "", reason_code=failure,
            result=pass_result, definition=ep)
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
    elif state == "skipped":
        # A pass-reported skip (no_project, ...) is the answer to "why are there
        # no tickets", and the ledger is not where anyone looks for it.
        note_entry["reason_code"] = failure
    try:
        _write_pass_note(current_md, item_id, slug, note_entry)
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


@serialized
def run_all(item_id: str) -> list[dict[str, Any]]:
    """Run every enabled pass. One failing pass never stops the others."""
    out = []
    reg = registry()
    for slug in ordered(reg):
        ep = reg[slug]
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


@serialized
def run_auto(item_id: str) -> list[dict[str, Any]]:
    definitions = ensure_plan(item_id)
    previous = {row["ep_slug"]: dict(row) for row in ledger.connect().execute(
        "SELECT * FROM passes WHERE item_id=?", (item_id,)).fetchall()}
    out = []
    for slug in ordered(definitions):
        ep = definitions[slug]
        version = int(ep.get("version") or 1)
        prior = previous.get(slug)
        if (prior and prior["state"] in ("completed", "skipped") and prior["version"] == version
                and prior.get("definition_id") in (None, operations.definition_id(ep))):
            out.append({"item_id": item_id, "ep_slug": slug, "version": version,
                        "state": prior["state"], "skipped": "already completed at this version"})
            continue
        attempt = int(prior["attempt"] or 0) + 1 if prior else 1
        try:
            out.append(run(item_id, slug, attempt=attempt, definition=ep))
        except PassError as exc:
            _record(item_id, slug, "failed", version=version, attempt=attempt,
                    detail=str(exc), reason_code="run_error", definition=ep)
            out.append({"item_id": item_id, "ep_slug": slug, "version": version,
                        "state": "failed", "error": str(exc), "reason_code": "run_error"})
    return out


def status(item_id: Optional[str] = None) -> list[dict[str, Any]]:
    conn = ledger.connect()
    if item_id:
        rows = conn.execute("SELECT * FROM passes WHERE item_id=? ORDER BY ep_slug", (item_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM passes ORDER BY updated_at DESC LIMIT 50").fetchall()
    return [dict(r) for r in rows]
