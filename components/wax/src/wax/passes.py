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

import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from . import component, events, frontmatter, ledger, paths, sentinel

REGISTRY_DIR = Path(os.environ.get("WAX_PASSES_DIR", component.PASSES))
DEFAULT_TIMEOUT_S = 900


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
        doc.setdefault("requires", [])
        doc.setdefault("timeout_s", DEFAULT_TIMEOUT_S)
        doc["_path"] = str(f)
        out[slug] = doc
    return out


def _record(item_id: str, slug: str, state: str, *, attempt: int = 1,
            command_id: Optional[str] = None, detail: str = "") -> None:
    ledger.connect().execute(
        "INSERT INTO passes(item_id,ep_slug,state,attempt,command_id,updated_at,detail) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(item_id,ep_slug) DO UPDATE SET "
        "state=excluded.state, attempt=excluded.attempt, command_id=excluded.command_id, "
        "updated_at=excluded.updated_at, detail=excluded.detail",
        (item_id, slug, state, attempt, command_id, sentinel.utcnow(), detail[:500]),
    )


def md_for(item_id: str) -> Optional[Path]:
    row = ledger.connect().execute(
        "SELECT md_path FROM transcripts WHERE item_id=?", (item_id,)).fetchone()
    if not row:
        return None
    p = Path(row["md_path"])
    return p if p.is_file() else None


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

    argv = [str(a).replace("{md_path}", str(md)).replace("{item_id}", item_id)
            .replace("{component_root}", str(component.ROOT))
            for a in (ep.get("command") or [])]
    if not argv:
        raise PassError(f"pass {slug!r} has no command")

    cid = events.emit_ep_command(item_id, slug, argv, attempt)
    _record(item_id, slug, "running", attempt=attempt, command_id=cid)
    events.emit("task", "started",
                {"ep_slug": slug, "item_id": item_id, "attempt": attempt,
                 "command_id": cid, "argv": argv},
                correlationid=cid, causationid=cid, ordering_key=item_id)

    started = time.time()
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=float(ep.get("timeout_s") or DEFAULT_TIMEOUT_S))
        ok, rc, err = r.returncode == 0, r.returncode, (r.stderr or "")[-800:]
    except subprocess.TimeoutExpired:
        ok, rc, err = False, None, f"timeout after {ep.get('timeout_s')}s"
    except OSError as e:
        ok, rc, err = False, None, str(e)

    took = round(time.time() - started, 2)
    state = "completed" if ok else "failed"
    _record(item_id, slug, state, attempt=attempt, command_id=cid, detail=err if not ok else "")
    events.emit("task", state,
                {"ep_slug": slug, "item_id": item_id, "attempt": attempt,
                 "command_id": cid, "duration_s": took,
                 **({"reason_code": "nonzero_exit" if rc else "run_error",
                     "returncode": rc, "stderr_tail": err} if not ok else {})},
                correlationid=cid, causationid=cid, ordering_key=item_id)

    # The note records its own history, so the vault is self-describing even if
    # the ledger is lost.
    try:
        frontmatter.merge(md, {
            frontmatter.ITEM_KEY: item_id,
            frontmatter.WAX_KEY: {"passes": {slug: {
                "state": state, "at": sentinel.utcnow(),
                "command_id": cid, "attempt": attempt,
            }}},
        })
    except OSError:
        pass

    return {"item_id": item_id, "ep_slug": slug, "state": state, "command_id": cid,
            "duration_s": took, "returncode": rc, "md_path": str(md),
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
            _record(item_id, slug, "failed", detail=str(e))
            out.append({"item_id": item_id, "ep_slug": slug, "state": "failed", "error": str(e)})
    return out


def status(item_id: Optional[str] = None) -> list[dict[str, Any]]:
    conn = ledger.connect()
    if item_id:
        rows = conn.execute("SELECT * FROM passes WHERE item_id=? ORDER BY ep_slug", (item_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM passes ORDER BY updated_at DESC LIMIT 50").fetchall()
    return [dict(r) for r in rows]
