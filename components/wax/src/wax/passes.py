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
import os
import re
import subprocess
import shutil
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from . import archive, component, events, frontmatter, ledger, rename, sentinel

REGISTRY_DIR = Path(os.environ.get("WAX_PASSES_DIR", component.PASSES))
DEFAULT_TIMEOUT_S = 900
RESULT_VERSION = 1
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
            command_id: Optional[str] = None, detail: str = "") -> None:
    ledger.connect().execute(
        "INSERT INTO passes(item_id,ep_slug,version,state,attempt,command_id,updated_at,detail) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(item_id,ep_slug) DO UPDATE SET "
        "version=excluded.version, state=excluded.state, attempt=excluded.attempt, command_id=excluded.command_id, "
        "updated_at=excluded.updated_at, detail=excluded.detail",
        (item_id, slug, version, state, attempt, command_id, sentinel.utcnow(), detail[:500]),
    )


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


def _apply_frontmatter(md: Path, updates: dict[str, Any], schema: Optional[str]) -> None:
    """Apply the vault base first, then batch all grounded values in one set."""
    if schema:
        schema_path = Path(os.path.expandvars(os.path.expanduser(schema)))
        if not schema_path.is_file():
            raise PassError(f"frontmatter schema does not exist: {schema_path}")
        _run_frontmatters(["apply-base", str(md), "--schema", str(schema_path)])
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
    base_values = {
        "schema-version": 1,
        "asset-kind": "transcript",
        "specialist": "transcripts",
        "source": "audio-recording",
        "captured": str(existing.get("transcribed-at") or ""),
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

    schema = ep.get("frontmatter_schema")
    expanded_schema = _expand(schema, item_id=item_id, md=md) if schema else None
    _apply_frontmatter(md, updates, expanded_schema)

    transcript = result.get("transcript") or {}
    if not isinstance(transcript, dict):
        raise PassError("enrichment result transcript must be an object")
    requested_slug = transcript.get("slug")
    existing_slug = existing.get("title-slug")
    if (existing_slug and requested_slug and existing_slug != requested_slug
            and "title-slug" not in allowed_clobbers):
        requested_slug = existing_slug
    current = _rename_transcript(md, item_id, requested_slug) if requested_slug else md

    if refs and (requested_slug or result.get("link_audio")):
        archive.link_transcript(item_id, current)

    changed = [f"frontmatter.{key}" for key in effective_updates]
    if current != md:
        changed.append("transcript.filename")
    if refs and (requested_slug or result.get("link_audio")):
        changed.append("audio.transcript-link")
    return current, changed


def run(item_id: str, slug: str, *, attempt: int = 1,
        allow_requires: bool = False) -> dict[str, Any]:
    """Run one pass against one item. Independent of every other pass."""
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
    current_md = md
    changed_fields: list[str] = []
    reason_code: Optional[str] = None
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           env=pass_env, timeout=float(ep.get("timeout_s") or DEFAULT_TIMEOUT_S))
        ok, rc, err = r.returncode == 0, r.returncode, (r.stderr or "")[-800:]
        if ok:
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
    _record(item_id, slug, state, version=version, attempt=attempt,
            command_id=cid, detail=err if not ok else "")
    events.emit("task", state,
                {"ep_slug": slug, "item_id": item_id, "attempt": attempt,
                 "pass_version": version, "command_id": cid, "duration_s": took,
                 "changed_fields": changed_fields,
                 **({"reason_code": reason_code or ("nonzero_exit" if rc is not None else "run_error"),
                     "returncode": rc, "stderr_tail": err} if not ok else {})},
                correlationid=cid, causationid=cid, ordering_key=item_id)

    # The note records its own history, so the vault is self-describing even if
    # the ledger is lost.
    try:
        frontmatter.merge(current_md, {
            frontmatter.ITEM_KEY: item_id,
            frontmatter.WAX_KEY: {"passes": {slug: {
                "state": state, "at": sentinel.utcnow(),
                "version": version, "command_id": cid, "attempt": attempt,
            }}},
        })
    except OSError:
        pass

    return {"item_id": item_id, "ep_slug": slug, "version": version,
            "state": state, "command_id": cid, "duration_s": took,
            "returncode": rc, "md_path": str(current_md), "changed_fields": changed_fields,
            **({"error": err} if not ok else {})}


def run_all(item_id: str) -> list[dict[str, Any]]:
    """Run every enabled pass. One failing pass never stops the others."""
    out = []
    for slug, ep in sorted(registry().items()):
        if not ep.get("enabled"):
            continue
        try:
            out.append(run(item_id, slug))
        except PassError as e:
            _record(item_id, slug, "failed", version=int(ep.get("version") or 1), detail=str(e))
            out.append({"item_id": item_id, "ep_slug": slug, "state": "failed", "error": str(e)})
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
            _record(item_id, slug, "failed", version=version, attempt=attempt, detail=str(exc))
            out.append({
                "item_id": item_id, "ep_slug": slug, "version": version,
                "state": "failed", "error": str(exc),
            })
    return out


def status(item_id: Optional[str] = None) -> list[dict[str, Any]]:
    conn = ledger.connect()
    if item_id:
        rows = conn.execute("SELECT * FROM passes WHERE item_id=? ORDER BY ep_slug", (item_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM passes ORDER BY updated_at DESC LIMIT 50").fetchall()
    return [dict(r) for r in rows]
