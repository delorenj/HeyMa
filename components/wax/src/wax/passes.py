"""Enrichment Passes: independent, individually tracked, individually traceable.

Independence is the whole design constraint. A pass never fails because a
SIBLING failed: if `wikification` fails, `mem-ops` still runs on the same item,
and the failure is recorded against that one slug rather than stalling the item.
A malformed definition is equally contained: quarantined() disables exactly the
offending pass (and anything gated on it), never the whole registry.

The one deliberate coupling is the registry's `requires:` list, and it is
honoured unconditionally. ordered() sorts a pass after everything it requires;
run() additionally refuses to execute it until each requirement is `completed`
or `skipped` AT THE LIVE REGISTRY VERSION, and records `dependency_failed`
(ledger row + `wax.passes.<slug>` note entry saying what it is waiting on)
otherwise. Declare `requires` only where a pass consumes another's output — the
dependent really cannot run without it. `after:` only orders: it is a sequencing
preference with no gate, and a failed `after` target never stops its dependent.
Both fields must name ENABLED passes; an `auto` pass may only `require` other
`auto` passes, because a plan holds nothing else. Either violation quarantines
the pass instead of raising the fleet down.

Conditions are declarative and runner-evaluated. `when:` is a list of clauses
({field, <operator>}) checked against the note's frontmatter at run time; the
first unmet clause skips the pass with reason_code condition_unmet and says
which clause failed. `skip_when: {field, equals}` is the legacy skip-polarity
form of the same idea. Routing logic belongs in these fields — where `wax ep
list` and `wax doctor` can see it — not inside pass executables; a pass-reported
skip is reserved for conditions only discoverable at runtime.

Exhaustion is announced, not silent: a pass that fails its final permitted
attempt emits bloodbank.audio.task.exhausted exactly once per (item, slug,
version) — the pipeline's dead-letter signal.

Events are a HOST effect, so a pass only declares them: a result may carry an
`events` list (validated by _validated_events against the registry's `emits`
allowlist AND the Bloodbank schema tree, before any mutation) and finalize.py
publishes them once the pass's own `requires` subgraph completes — a slow
sibling must not hold ready events hostage to the whole plan.

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

from . import archive, component, events, frontmatter, ledger, operations, paths, provider, rename, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

REGISTRY_DIR = Path(os.environ.get("WAX_PASSES_DIR", component.PASSES))
DEFAULT_TIMEOUT_S = 900
RESULT_VERSION = 1
# A pass that knows why it failed says so on its first stderr line. Exit codes
# cannot carry that: a deleted model and an unreachable provider both exit 1,
# which is how a week of 404s was recorded as an indistinguishable
# "nonzero_exit". Passes that predate the convention simply omit the line.
_REASON_LINE = re.compile(r"^reason_code=([a-z_]+)$")

# --- declarative run conditions (`when:`) -----------------------------------
# One clause is {field: <frontmatter key>, <operator>: <operand>}; every clause
# must hold for the pass to run. The runner evaluates them against the note's
# frontmatter at run time, so the routing table lives in the registry — where
# `wax ep list` and doctor can see it — instead of inside pass executables.
_CONDITION_OPS = {
    "equals", "not_equals", "in", "not_in",
    "exists", "absent", "empty", "not_empty",
    "length", "min_length", "max_length",
}
_FLAG_OPS = {"exists", "absent", "empty", "not_empty"}
_SIZE_OPS = {"length", "min_length", "max_length"}

# The sweep bound lives here so the CLI, the worker's scheduled sweep and the
# exhaustion dead-letter all agree on what "final attempt" means.
DEFAULT_MAX_ATTEMPTS = 3

# Fields the runner stamps onto every pass event's data at emission time. A
# pass cannot know them, so the result-time schema check relaxes `required`
# for exactly these (and nothing else); the emission-time check in
# finalize.py validates the fully stamped payload.
RUNNER_STAMPED_EVENT_DATA = {"item_id", "transcription_id", "transcript", "transcript_uri", "project"}
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


def _when_error(when: Any) -> Optional[str]:
    """Why a `when:` block is invalid, or None. Structure only — no values."""
    if when is None:
        return None
    if not isinstance(when, list) or not when:
        return "invalid when: expected a non-empty list of clauses"
    for clause in when:
        if not isinstance(clause, dict):
            return "invalid when: each clause must be an object"
        if not isinstance(clause.get("field"), str):
            return "invalid when clause: missing string field"
        ops = set(clause) - {"field"}
        if len(ops) != 1 or next(iter(ops)) not in _CONDITION_OPS:
            return f"invalid when clause: exactly one operator from {sorted(_CONDITION_OPS)}"
        op = next(iter(ops))
        operand = clause[op]
        if op in ("in", "not_in") and not isinstance(operand, list):
            return f"invalid when clause: {op} expects a list"
        if op in _SIZE_OPS and (not isinstance(operand, int) or isinstance(operand, bool)):
            return f"invalid when clause: {op} expects an integer"
        if op in _FLAG_OPS and operand is not True:
            return f"invalid when clause: {op} takes no operand (write `{op}: true`)"
    return None


def _clause_failure(clause: dict[str, Any], fm: dict[str, Any]) -> Optional[str]:
    """None when the clause holds, else a human-readable reason it did not."""
    field = clause["field"]
    value = fm.get(field)
    op = next(key for key in clause if key != "field")
    operand = clause[op]
    if op == "equals":
        ok = value == operand
    elif op == "not_equals":
        ok = value != operand
    elif op == "in":
        ok = value in operand
    elif op == "not_in":
        ok = value not in operand
    elif op == "exists":
        ok = value is not None
    elif op == "absent":
        ok = value is None
    elif op == "empty":
        # Mirrors _apply_result's no-clobber semantics: 0 and False are real
        # values, not emptiness.
        ok = value is None or value == "" or value == [] or value == {}
    elif op == "not_empty":
        ok = not (value is None or value == "" or value == [] or value == {})
    else:
        size = len(value) if isinstance(value, (list, tuple, dict, str)) else 0
        if op == "length":
            ok = size == operand
        elif op == "min_length":
            ok = size >= operand
        else:
            ok = size <= operand
    return None if ok else f"{field} {op} {operand if op not in _FLAG_OPS else ''} (got {value!r})".replace("  ", " ")


def quarantined(reg: dict[str, dict[str, Any]]) -> dict[str, str]:
    """{slug: reason} for enabled passes that must not run.

    One bad definition used to raise out of ordered() and take the whole EP
    stage down for every item. Now the offending pass — plus anything gated on
    it, which could never run cleanly — is quarantined, and the rest of the
    fleet keeps working. Quarantine is recomputed from the live registry on
    every call, so fixing the YAML lifts it with no restart.
    """
    reasons: dict[str, str] = {}
    for slug, ep in reg.items():
        if not ep.get("enabled"):
            continue
        for field in ("requires", "after"):
            deps = ep.get(field) or []
            if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
                reasons.setdefault(slug, f"invalid {field}: expected a list of pass slugs")
        condition = ep.get("skip_when")
        if condition is not None and (not isinstance(condition, dict)
                                      or set(condition) != {"field", "equals"}
                                      or not isinstance(condition.get("field"), str)):
            reasons.setdefault(slug, "invalid skip_when: expected exactly {field, equals}")
        err = _when_error(ep.get("when"))
        if err:
            reasons.setdefault(slug, err)

    def dependency_problems() -> None:
        for slug, ep in reg.items():
            if not ep.get("enabled") or slug in reasons:
                continue
            for field in ("requires", "after"):
                deps = ep.get(field) or []
                if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
                    continue  # the shape error already quarantined this pass
                for dep in deps:
                    target = reg.get(dep)
                    why = None
                    if target is None:
                        why = f"unknown dependency {dep!r}"
                    elif not target.get("enabled"):
                        why = f"dependency {dep!r} is disabled"
                    elif dep in reasons:
                        why = f"dependency {dep!r} is quarantined"
                    if why:
                        reasons[slug] = why
                        break
                if slug in reasons:
                    break

    def cycles() -> None:
        selected = {s: e for s, e in reg.items() if e.get("enabled") and s not in reasons}
        color: dict[str, int] = {}

        def visit(slug: str, path: list[str]) -> None:
            color[slug] = 1
            for field in ("requires", "after"):
                for dep in selected[slug].get(field) or []:
                    if dep not in selected:
                        continue
                    if color.get(dep) == 1 and dep in path:
                        for member in path[path.index(dep):]:
                            reasons.setdefault(member, "cyclic pass dependency")
                    elif color.get(dep) is None:
                        visit(dep, path + [dep])
            color[slug] = 2

        for slug in sorted(selected):
            if color.get(slug) is None:
                visit(slug, [slug])

    while True:
        before = len(reasons)
        dependency_problems()
        cycles()
        # An auto pass may only require auto passes: a plan holds nothing else.
        for slug, ep in reg.items():
            if not ep.get("enabled") or not ep.get("auto") or slug in reasons:
                continue
            for dep in ep.get("requires") or []:
                target = reg.get(dep)
                if isinstance(target, dict) and target.get("enabled") and not target.get("auto"):
                    reasons.setdefault(slug, f"automatic pass requires manual pass {dep!r}")
        if len(reasons) == before:
            return reasons


def ordered(reg):
    """Enabled, non-quarantined passes in dependency order.

    Never raises on definition content: malformed passes are quarantined (and
    reported by quarantined(), `wax ep list` and doctor) instead of taking the
    whole registry down with them.
    """
    bad = quarantined(reg)
    selected = {slug: ep for slug, ep in reg.items() if ep.get("enabled") and slug not in bad}
    visited, output = set(), []

    def visit(slug):
        if slug in visited:
            return
        visited.add(slug)
        for field in ("requires", "after"):
            for dependency in selected[slug].get(field) or []:
                if dependency in selected:
                    visit(dependency)
        output.append(slug)

    for slug in sorted(selected):
        visit(slug)
    return output


def ensure_plan(item_id):
    conn = ledger.connect()
    row = conn.execute("SELECT definitions FROM processing_plans WHERE item_id=?", (item_id,)).fetchone()
    # Plan authority: with exactly one compiled+enabled transcription pipeline,
    # NEW plans come from its frozen definition; otherwise the live registry
    # stands (legacy fallback). A minted plan NEVER migrates — completed or
    # not. An item's attempts, exhaustion receipts and halt policy all hang
    # off the plan it was given, so it finishes (or is retried) under the
    # graph it started with; adopting a changed authority mid-flight would
    # silently change what runs on an item as a scan side effect. New items,
    # new graph — moving an unfinished item onto a new graph is an explicit
    # operator decision, not something a tick does on its own.
    if row:
        return json.loads(row["definitions"])
    from . import pipelines
    definitions, provenance = pipelines.transcription_authority()
    for slug, ep in definitions.items():
        if any(dependency not in definitions for dependency in ep.get("requires") or []):
            raise PassError(f"automatic pass {slug} requires a manual pass")
    encoded = json.dumps(definitions, sort_keys=True, default=str)
    import hashlib
    plan_id = hashlib.sha256(encoded.encode()).hexdigest()
    conn.execute("INSERT INTO processing_plans(item_id,plan_id,definitions,created_at,source) "
                 "VALUES(?,?,?,?,?) "
                 "ON CONFLICT(item_id) DO UPDATE SET plan_id=excluded.plan_id,"
                 "definitions=excluded.definitions,source=excluded.source",
                 (item_id, plan_id, encoded, sentinel.utcnow(),
                  json.dumps(provenance, sort_keys=True, default=str)))
    return definitions


def replan(item_id: str, *, apply: bool = False) -> dict[str, Any]:
    """Move ONE unfinished item onto the current plan authority. Nothing runs here.

    ensure_plan never migrates a plan on its own; this is the explicit operator
    step it defers to. The case it exists for: 2026-10-22, an item minted under
    transcript-standard v4 froze a model chain whose every member answered 503,
    and `wax ep run` / the sweep rightly re-ran that frozen chain, so the item
    could not recover after v5 fixed the chain.

    Refused when the item already completed (its plan is history), when the
    authority is ambiguous, when the new plan would add or drop a pass, or
    when a pass that already completed or skipped would change definition
    (its result belongs to the plan it ran under). So only unfinished passes
    move; attempt counters and every recorded result are kept. Dry run unless
    `apply`.
    """
    from . import pipelines
    conn = ledger.connect()
    row = conn.execute("SELECT plan_id, definitions, source FROM processing_plans WHERE item_id=?",
                       (item_id,)).fetchone()
    if not row:
        raise PassError(f"{item_id} has no plan; its next run mints one from the current authority")
    if conn.execute("SELECT 1 FROM completions WHERE item_id=?", (item_id,)).fetchone():
        raise PassError(f"{item_id} already completed under its plan; a completed plan is history")
    old = json.loads(row["definitions"])
    new, provenance = pipelines.transcription_authority()
    if provenance.get("fallback_reason"):
        raise PassError(f"the plan authority is ambiguous: {provenance['fallback_reason']}")
    if set(old) != set(new):
        raise PassError(f"the current authority runs {sorted(new)}, the plan runs {sorted(old)}; "
                        "replan moves definitions, never the set of passes")
    recorded = {r["ep_slug"]: dict(r) for r in conn.execute(
        "SELECT ep_slug, state FROM passes WHERE item_id=?", (item_id,))}
    changed = sorted(slug for slug in old
                     if operations.definition_id(old[slug]) != operations.definition_id(new[slug]))
    settled = [slug for slug in changed if (recorded.get(slug) or {}).get("state") in ("completed", "skipped")]
    if settled:
        raise PassError(f"{', '.join(settled)} already finished under the old definition; "
                        "replan only moves unfinished passes")
    diff = {}
    for slug in changed:
        a, b = old[slug], new[slug]
        keys = sorted(k for k in set(a) | set(b) if not k.startswith("_") and a.get(k) != b.get(k))
        env_keys = sorted(k for k in set(a.get("env") or {}) | set(b.get("env") or {})
                          if (a.get("env") or {}).get(k) != (b.get("env") or {}).get(k))
        diff[slug] = {"fields": keys, **({"env": env_keys} if env_keys else {})}
    out: dict[str, Any] = {"item_id": item_id, "applied": False, "changed": diff,
                           "from": json.loads(row["source"] or "{}"), "to": provenance}
    if not changed or not apply:
        return out
    encoded = json.dumps(new, sort_keys=True, default=str)
    import hashlib
    plan_id = hashlib.sha256(encoded.encode()).hexdigest()
    conn.execute("UPDATE processing_plans SET plan_id=?, definitions=?, source=? WHERE item_id=?",
                 (plan_id, encoded, json.dumps(provenance, sort_keys=True, default=str), item_id))
    # Audit on its own machine: the item state machine (pending, archived,
    # ...) is recomputed from its transitions and has no "replanned" state.
    ledger.record_transition("plan", item_id, row["plan_id"][:12], plan_id[:12], "operator_replan",
                             f"{row['plan_id'][:12]} -> {plan_id[:12]} ({provenance.get('name') or provenance.get('kind')}"
                             f" {str(provenance.get('definition_id') or '')[:12]}): {', '.join(changed)}")
    out["applied"] = True
    out["plan_id"] = plan_id
    return out


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
    if "exhausted_at" in {row["name"] for row in conn.execute("PRAGMA table_info(passes)")}:
        # A version bump is a NEW definition with a fresh retry budget, so the
        # dead-letter receipt from the old definition must not suppress the
        # new one's. Same reason sweep stops counting stale-version failures.
        # A non-failed terminal state (completed, skipped) means the pass is no
        # longer dead-lettered, so the receipt goes too; otherwise `wax catalog`
        # and Holocene keep counting a recovered row as exhausted. `running` and
        # `failed` (dependency_failed included) keep it: an in-flight re-run has
        # not recovered yet, and keeping it is what stops a repeat failure past
        # the bound from re-announcing the same exhaustion.
        updates += (", exhausted_at=CASE WHEN passes.version != excluded.version "
                    "OR excluded.state IN ('completed','skipped') "
                    "THEN NULL ELSE passes.exhausted_at END")
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


def _emit_exhausted(item_id: str, slug: str, *, version: int, attempt: int,
                    reason_code: Optional[str], detail: str,
                    command_id: Optional[str]) -> bool:
    """Fire bloodbank.audio.task.exhausted once per (item, slug, version).

    The dead-letter signal: the sweep bound is spent, nothing will re-drive
    this pass automatically, and finalize() keeps withholding the item's
    completion. The exhausted_at receipt is the dedup; _record clears it when
    the registry version changes, so a bumped definition announces afresh on
    its next exhaustion, and when the pass reaches completed or skipped, so a
    recovered pass is no longer counted as exhausted. (The attempt counter is lifetime-per-pair — it is the
    command_id entropy and can never reset.) dependency_failed never exhausts:
    the pass never ran, and its gate lifts when a DIFFERENT pass succeeds.
    """
    conn = ledger.connect()
    if "exhausted_at" not in {row["name"] for row in conn.execute("PRAGMA table_info(passes)")}:
        return False
    row = conn.execute("SELECT exhausted_at FROM passes WHERE item_id=? AND ep_slug=?",
                       (item_id, slug)).fetchone()
    if not row or row["exhausted_at"]:
        return False
    conn.execute("UPDATE passes SET exhausted_at=? WHERE item_id=? AND ep_slug=?",
                 (sentinel.utcnow(), item_id, slug))
    head = (detail or "").strip().splitlines()
    events.emit("task", "exhausted",
                {"item_id": item_id, "ep_slug": slug, "attempt": attempt, "version": version,
                 "reason_code": reason_code,
                 "detail": (head[0][:300] if head else None)},
                correlationid=command_id or item_id, ordering_key=item_id)
    log.warning("%s EXHAUSTED for %s after %d attempt(s) (v%d): %s",
                slug, item_id, attempt, version, reason_code)
    return True


def sweep_exhaustions(max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list[dict[str, Any]]:
    """Dead-letter every failed pass at/past the attempt bound that never said so.

    Covers rows that exhausted before the run()-time hook existed or under a
    different bound. Emission is idempotent via the exhausted_at receipt.
    """
    conn = ledger.connect()
    if "exhausted_at" not in {row["name"] for row in conn.execute("PRAGMA table_info(passes)")}:
        return []
    rows = conn.execute(
        "SELECT item_id, ep_slug, version, attempt, reason_code, detail, command_id "
        "FROM passes WHERE state='failed' AND attempt>=? AND exhausted_at IS NULL "
        "AND COALESCE(reason_code,'') != 'dependency_failed'",
        (max_attempts,)).fetchall()
    fired = []
    for row in rows:
        if _emit_exhausted(row["item_id"], row["ep_slug"],
                           version=int(row["version"] or 1), attempt=int(row["attempt"] or 0),
                           reason_code=row["reason_code"], detail=row["detail"] or "",
                           command_id=row["command_id"]):
            fired.append({"item_id": row["item_id"], "ep_slug": row["ep_slug"],
                          "attempt": int(row["attempt"] or 0)})
    return fired


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
        # Document context: an ordinary Markdown note enriched in place. It has
        # no transcripts row by design (that row asserts audio provenance), so
        # resolution falls through to the documents table — never a fabricated
        # transcript.
        from . import documents
        return documents.md_for(item_id)
    p = Path(row["md_path"])
    return p if p.is_file() else None


def is_document(item_id: str) -> bool:
    """Whether this pass-ledger key names a document rather than an audio item."""
    from . import documents
    return documents.is_document_key(item_id)


def _expand(value: Any, *, item_id: str, md: Path) -> str:
    return (str(value)
            .replace("{md_path}", str(md))
            .replace("{item_id}", item_id)
            .replace("{component_root}", str(component.ROOT))
            .replace("{home}", str(Path.home())))


class _Refused(Exception):
    """A pass result the runner will not apply."""


def _anthropic_served(result: Any) -> Optional[str]:
    """`served_by=<x>` when a chat pass's evidence says an Anthropic model answered.

    Chat-pass evidence is the model-chain shape (it has a `chain`); other
    passes' evidence is not read, so an audio codec named opus is not a model.
    """
    evidence = result.get("evidence") if isinstance(result, dict) else None
    if not isinstance(evidence, dict) or "chain" not in evidence:
        return None
    for field in ("model", "served_by", "route"):
        value = evidence.get(field)
        if isinstance(value, str) and provider.ANTHROPIC_RE.search(value):
            return f"{field}={value[:120]}"
    return None


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


def _event_data_schema(event_type: str) -> dict[str, Any]:
    """The `data` subschema for a pass-declared event type, from the Bloodbank tree.

    Wax publishes straight to NATS and never passes through bb-emit's guard, so
    THIS is the schema enforcement point. Fail-closed is deliberate: the
    platform rule is schema-first, and a pass emitting an unschemed type is a
    contract bug that must fail loudly here rather than drift silently in
    Candystore.
    """
    root = Path(os.environ.get("WAX_BLOODBANK_SCHEMAS")
                or (Path.home() / "code" / "33GOD" / "bloodbank" / "schemas"))
    path = root / "bloodbank" / events.DOMAIN / f"{event_type}.json"
    if not path.is_file():
        raise PassError(f"no Bloodbank schema for {event_type!r} at {path}; "
                        f"author schemas/bloodbank/{events.DOMAIN}/{event_type}.json first (schema-first)")
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PassError(f"unreadable Bloodbank schema {path}: {exc}") from exc
    data = (doc.get("properties") or {}).get("data")
    if not isinstance(data, dict) or data.get("type") != "object":
        raise PassError(f"Bloodbank schema {path} has no object `data` subschema")
    return data


def validate_event_data(event_type: str, data: dict[str, Any], *, relaxed: bool) -> None:
    """jsonschema-check event data against the Bloodbank tree; PassError on violation.

    relaxed=True drops the runner-stamped fields from `required` — used at
    result-apply time, before the runner has stamped them. finalize.py re-checks
    the fully stamped payload with relaxed=False at emission time.
    """
    schema = _event_data_schema(event_type)
    if relaxed:
        schema = {**schema, "required": [r for r in schema.get("required", [])
                                         if r not in RUNNER_STAMPED_EVENT_DATA]}
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import best_match
    error = best_match(Draft202012Validator(schema).iter_errors(data))
    if error is not None:
        raise PassError(f"event data violates the {event_type} schema: {error.message}")


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
        # Shape is not the contract — the Bloodbank schema is. A violation fails
        # this pass with result_apply_failed before the note or ledger moves.
        try:
            validate_event_data(event_type, data, relaxed=True)
        except PassError as exc:
            raise PassError(f"{where}.data: {exc}") from None
    return declared


def _apply_result(item_id: str, md: Path, ep: dict[str, Any], result: dict[str, Any],
                  *, document: bool = False) -> tuple[Path, list[str]]:
    """Apply a pass's declarative mutations and return the current note path.

    ``document=True`` is the ordinary-document context: no transcript identity
    is stamped (a document must never acquire fabricated transcript/audio
    provenance), no archive references exist to link, and the transcript-slug
    rename contract does not apply. Frontmatter proposals keep their full
    no-clobber semantics either way.
    """
    if not result:
        return md, []
    # First, before the note, the ledger or the filesystem is touched: a rejected
    # event list must leave the pass's other proposals unapplied too, or the
    # recorded `failed` would sit next to a half-enriched note.
    if document and result.get("events"):
        # Event envelopes are stamped with audio identity (transcription_id,
        # transcript URI) and validated against audio Bloodbank schemas; a
        # document run cannot satisfy that contract, so it is refused here
        # rather than publishing a fabricated-provenance event.
        raise PassError("document runs cannot declare events")
    _validated_events(ep, result)
    raw_updates = result.get("frontmatter") or {}
    if not isinstance(raw_updates, dict):
        raise PassError("enrichment result frontmatter must be an object")
    forbidden = sorted(set(raw_updates) & _PROTECTED_FRONTMATTER)
    if forbidden:
        raise PassError(f"pass attempted to overwrite provenance/frontmatter ownership: {forbidden}")
    existing, _ = frontmatter.read(md)
    if raw_updates.get("classification") not in (None, *frontmatter.CLASSIFICATIONS):
        raise PassError("invalid classification")
    allowed_clobbers = {str(key) for key in (ep.get("clobber") or [])}
    effective_updates = {
        key: value for key, value in raw_updates.items()
        if (existing.get(key) in (None, "", [], {})
            or existing.get(key) == value
            or key in allowed_clobbers)
    }
    updates = dict(effective_updates)

    if not document:
        # This runner targets transcript artifacts for audio items. Fill known
        # base identity/provenance when older notes predate the metadata-first
        # contract; never replace a non-empty upstream value. `captured` is
        # when the AUDIO WAS RECORDED and is derived from the source's mtime by
        # transcribe_adapter; stamping it with transcribed-at here silently
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
    if document:
        # The date-prefixed filename contract belongs to transcripts; an
        # ordinary document keeps the name its owner gave it.
        requested_slug = None
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
    if replacement is not None and not document:
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


def _capture(item_id: str, slug: str, **kw: Any) -> None:
    """Version the note in the `transcriptions` store. Fail-open: never raises, never fails a pass.

    Called under the item flock (`run` is `@serialized`) right after the note's
    own `wax.passes.<slug>` entry is written, so the snapshot is the note as the
    pass left it. Documents are out of scope (docs/TRANSCRIPTIONS-STORE.md B.0).
    """
    try:
        if is_document(item_id):
            return
        from . import transcriptions
        transcriptions.record_pass(item_id=item_id, slug=slug, **kw)
    except Exception as exc:  # noqa: BLE001 - the store must never cost a pass
        log.warning("transcriptions capture failed for %s/%s: %s: %s", item_id, slug, type(exc).__name__, exc)


def _frozen_plan_entry(item_id: str, slug: str) -> tuple[Optional[dict[str, Any]], Optional[dict[str, int]]]:
    """(the plan's frozen definition of `slug`, the plan's pass versions) for an item, or (None, None) when the item
    has no plan or the plan does not name the pass (legacy history keeps the live-registry path, as in the sweep)."""
    row = ledger.connect().execute(
        "SELECT definitions FROM processing_plans WHERE item_id=?", (item_id,)).fetchone()
    if not row:
        return None, None
    try:
        plan = json.loads(row["definitions"])
    except (TypeError, ValueError):
        return None, None
    if not isinstance(plan, dict) or not isinstance(plan.get(slug), dict):
        return None, None
    try:
        versions = {s: int(p.get("version") or 1) for s, p in plan.items() if isinstance(p, dict)}
    except (TypeError, ValueError):
        return None, None
    return plan[slug], versions


@serialized
def run(item_id: str, slug: str, *, attempt: Optional[int] = None,
         definition=None, dependency_versions: Optional[dict[str, int]] = None,
         live: bool = False) -> dict[str, Any]:
    """Run one pass against one item. Independent of its siblings, gated only by `requires`.

    `attempt` defaults to one past the highest attempt already recorded for
    (item_id, slug) so that a caller which does not track attempts — `wax ep
    run` — cannot mint a command_id that has already been issued.

    `live=True` (`wax ep run --live`) runs the LIVE registry's definition of the
    pass instead of the item's frozen plan entry. It is the operator's tool for
    redoing one pass of a FINISHED item under a newer pass version (a plan is
    history once its completion exists, and `replan` refuses it), e.g. the
    classification v2 reclassification. An unfinished item is refused: its
    finalize compares recorded definitions to its plan, and a live run would
    wedge it in passes_incomplete.
    """
    reg = registry()
    if live and definition is None:
        if ledger.connect().execute(
                "SELECT 1 FROM processing_plans p WHERE p.item_id=? AND NOT EXISTS "
                "(SELECT 1 FROM completions c WHERE c.item_id=p.item_id AND c.plan_id=p.plan_id)",
                (item_id,)).fetchone():
            raise PassError(f"{item_id} has an unfinished plan; --live only redoes a pass of a finished item "
                            "(use `wax ep replan` for an unfinished one)")
    if definition is None and not live:
        # An item with a minted plan runs under that plan's frozen definition no matter who asks: `wax ep run`,
        # the tray's retry and the sweep must agree. A manual retry under the LIVE registry recorded a definition
        # the plan never froze, so finalize refused it (passes_incomplete) even after the pass succeeded.
        frozen, frozen_versions = _frozen_plan_entry(item_id, slug)
        if frozen is not None:
            definition = frozen
            if dependency_versions is None:
                dependency_versions = frozen_versions
    ep = definition or reg.get(slug)
    if ep is None:
        raise PassError(f"unknown pass {slug!r}; known: {sorted(reg)}")
    if definition is None:
        # Live-registry execution consults registry health; a FROZEN compiled
        # definition does not — it was validated at compile and is the thing
        # executing, so a later registry edit cannot wedge a compiled pipeline.
        bad = quarantined(reg)
        if slug in bad:
            raise PassError(f"pass {slug!r} is quarantined: {bad[slug]}")
        if not ep.get("enabled"):
            raise PassError(f"pass {slug!r} is disabled in {ep.get('_path')}")
    attempt = _next_attempt(item_id, slug) if attempt is None else int(attempt)
    for dependency in ep.get("requires") or []:
        row = ledger.connect().execute(
            "SELECT state,version FROM passes WHERE item_id=? AND ep_slug=?", (item_id, dependency)
        ).fetchone()
        if dependency_versions is not None and dependency in dependency_versions:
            wanted = dependency_versions[dependency]
        else:
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
        _capture(item_id, slug, kind="pass_run", state="failed", md_path=md, pass_version=version,
                 attempt=attempt, command_id=None, reason_code="dependency_failed", detail=detail,
                 definition=ep, pass_result=None, changed_fields=[], duration_s=None)
        log.warning("%s dependency_failed for %s (attempt %d): %s", slug, item_id, attempt, detail)
        return {"item_id": item_id, "ep_slug": slug, "version": version, "state": "failed",
                "attempt": attempt, "reason_code": "dependency_failed", "error": detail}

    md = md_for(item_id)
    if md is None:
        raise PassError(f"no transcript recorded for item {item_id}")

    # Document context: a pass that has not declared the `document` capability
    # is VISIBLY inapplicable — recorded and noted as a skip with its reason —
    # never silently omitted and never run against a note it cannot own.
    document = is_document(item_id)
    if document and "document" not in (ep.get("capabilities") or []):
        detail = (f"pass declares capabilities {ep.get('capabilities') or []}; "
                  "document runs require the 'document' capability")
        _record(item_id, slug, "skipped", version=int(ep.get("version") or 1),
                attempt=attempt, reason_code="inapplicable_capability", detail=detail,
                definition=ep)
        try:
            _write_pass_note(md, item_id, slug, {
                "state": "skipped", "at": sentinel.utcnow(),
                "version": int(ep.get("version") or 1),
                "reason_code": "inapplicable_capability", "detail": detail,
            })
        except OSError:
            pass
        return {"item_id": item_id, "ep_slug": slug, "state": "skipped",
                "reason_code": "inapplicable_capability", "detail": detail}

    skip_reason = None
    skip_detail = ""
    if ep.get("kind") == "diarization" and os.environ.get("WAX_DIARIZATION", "").lower() in {"0", "false", "no", "off"}:
        skip_reason = "explicitly_disabled"
    condition = ep.get("skip_when")
    when = ep.get("when")
    if not skip_reason and (condition or when):
        fm, _ = frontmatter.read(md)
        if condition and fm.get(condition["field"]) == condition["equals"]:
            skip_reason = "condition_matched"
        elif when:
            for clause in when:
                failure = _clause_failure(clause, fm)
                if failure:
                    skip_reason = "condition_unmet"
                    skip_detail = failure
                    break
    if skip_reason:
        _record(item_id, slug, "skipped", version=int(ep["version"]), attempt=attempt,
                reason_code=skip_reason, detail=skip_detail,
                result={"skip_reason": skip_reason, **({"detail": skip_detail} if skip_detail else {})},
                definition=ep)
        frontmatter.merge(md, {frontmatter.WAX_KEY: {"passes": {slug: {
            "state": "skipped", "version": int(ep["version"]), "reason_code": skip_reason,
            **({"detail": skip_detail} if skip_detail else {}),
        }}}})
        _capture(item_id, slug, kind="pass_run", state="skipped", md_path=md, pass_version=int(ep["version"]),
                 attempt=attempt, command_id=None, reason_code=skip_reason, detail=skip_detail,
                 definition=ep, pass_result=None, changed_fields=[], duration_s=None)
        return {"item_id": item_id, "ep_slug": slug, "state": "skipped", "reason_code": skip_reason,
                **({"detail": skip_detail} if skip_detail else {})}

    argv = [_expand(a, item_id=item_id, md=md) for a in (ep.get("command") or [])]
    if not argv:
        raise PassError(f"pass {slug!r} has no command")
    version = int(ep.get("version") or 1)
    timeout_s = float(ep.get("timeout_s") or DEFAULT_TIMEOUT_S)
    pass_env = dict(os.environ)
    for key, value in (ep.get("env") or {}).items():
        pass_env[str(key)] = _expand(value, item_id=item_id, md=md)
    # The timeout this runner enforces, after the env block so no YAML can
    # misstate it: a chat pass budgets its whole model chain against it and
    # finishes, with every member's evidence, before the kill below.
    pass_env[provider.PASS_TIMEOUT_ENV] = f"{timeout_s:g}"

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
        r = subprocess.run(argv, capture_output=True, text=True, env=pass_env, timeout=timeout_s)
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
                banned = _anthropic_served(pass_result)
                if banned:
                    # The single owner refuses it too: a pass that forgot the
                    # check must not get an Anthropic answer into a note.
                    raise _Refused(banned)
                current_md, changed_fields = _apply_result(item_id, md, ep, pass_result,
                                                           document=document)
                effective, _ = frontmatter.read(current_md)
                pass_result = {**pass_result, "effective_metadata": {
                    key: effective.get(key) for key in (pass_result.get("frontmatter") or {})
                }}
            except _Refused as exc:
                ok, reason_code = False, provider.ANTHROPIC_SERVED
                err = (f"refused the result: an Anthropic model served it ({exc}); operator order "
                       "2026-10-22, run deploy/provision-inference")
                pass_result = {"evidence": pass_result.get("evidence")}
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
    if state == "failed" and attempt >= DEFAULT_MAX_ATTEMPTS:
        # The final permitted attempt just failed: this is the dead-letter
        # moment. Without an event here the item sits enrichment_pending
        # forever, invisible to everything that isn't watching the tray.
        _emit_exhausted(item_id, slug, version=version, attempt=attempt,
                        reason_code=failure, detail=err, command_id=cid)
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
    else:
        # Chat passes report which model served and which chain members did
        # not; the note carries it so "who wrote this title" never needs the ledger.
        evidence = pass_result.get("evidence") if isinstance(pass_result, dict) else None
        if isinstance(evidence, dict) and isinstance(evidence.get("model"), str):
            note_entry["model"] = evidence["model"][:120]
            # What the gateway says actually answered: a router member
            # (auto/fast) names no model, the response's `model` does.
            if isinstance(evidence.get("served_by"), str):
                note_entry["served_by"] = evidence["served_by"][:120]
            if isinstance(evidence.get("route"), str):
                note_entry["route"] = evidence["route"][:120]
            skipped = [f"{item.get('model')}={item.get('reason_code')}"
                       for item in evidence.get("fallbacks") or [] if isinstance(item, dict)]
            if skipped:
                note_entry["fell_through"] = "; ".join(skipped)[:300]
    if state == "skipped" and ok:
        # A pass-reported skip (no_project, ...) is the answer to "why are there
        # no tickets", and the ledger is not where anyone looks for it.
        note_entry["reason_code"] = failure
    try:
        _write_pass_note(current_md, item_id, slug, note_entry)
    except OSError:
        pass
    _capture(item_id, slug, kind="version" if state == "completed" else "pass_run", state=state,
             md_path=current_md, pass_version=version, attempt=attempt, command_id=cid, reason_code=failure,
             detail=err if not ok else "", definition=ep, pass_result=pass_result,
             changed_fields=changed_fields, duration_s=took, pre_rename_md=md)

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
    """Run every enabled pass. One failing pass never stops the others.

    Quarantined passes are REPORTED (state `quarantined`, with the reason) but
    not run and not given a ledger row: nothing ran, so there is nothing to
    retry — and a row would make the sweep burn attempts on a config error.
    """
    out = []
    reg = registry()
    for slug, why in sorted(quarantined(reg).items()):
        if reg[slug].get("enabled"):
            log.warning("%s quarantined for %s: %s", slug, item_id, why)
            out.append({"item_id": item_id, "ep_slug": slug, "state": "quarantined",
                        "error": why, "reason_code": "quarantined"})
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


def _plan_provenance(item_id: str) -> dict[str, Any]:
    """The stored provenance of an item's plan: which authority minted it and
    the failure policy frozen with it (a compiled pipeline's plan halts or
    runs independent exactly as it was compiled, whatever the registry says
    now)."""
    row = ledger.connect().execute(
        "SELECT source FROM processing_plans WHERE item_id=?", (item_id,)).fetchone()
    if not row:
        return {}
    try:
        value = json.loads(row["source"] or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@serialized
def run_auto(item_id: str, *, hold: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Walk the item's plan, running every pass that is not completed/skipped.

    `hold` names slugs this walk must leave alone even though they are failed:
    a caller that already spent (or deliberately deferred) a pass's attempt in
    this tick uses it so the same walk cannot spend a second one. Held rows
    still count for dependency gating -- they are simply not re-run.
    """
    definitions = ensure_plan(item_id)
    provenance = _plan_provenance(item_id)
    # A plan is FROZEN: dependency gating replays against the plan's stored
    # versions, never the live registry's — bumping a registry dependency
    # after the plan was minted must not strand the plan's dependents behind
    # a version they already satisfied.
    dependency_versions = {slug: int(ep.get("version") or 1)
                           for slug, ep in definitions.items()}
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
        if slug in hold and prior and prior["state"] == "failed":
            out.append({"item_id": item_id, "ep_slug": slug, "version": version,
                        "state": "failed", "skipped": "held: attempt already spent or deferred this tick",
                        "attempt": int(prior["attempt"] or 0)})
            continue
        attempt = int(prior["attempt"] or 0) + 1 if prior else 1
        try:
            result = run(item_id, slug, attempt=attempt, definition=ep,
                         dependency_versions=dependency_versions)
        except PassError as exc:
            _record(item_id, slug, "failed", version=version, attempt=attempt,
                    detail=str(exc), reason_code="run_error", definition=ep)
            result = {"item_id": item_id, "ep_slug": slug, "version": version,
                      "state": "failed", "error": str(exc), "reason_code": "run_error"}
        out.append(result)
        if (provenance.get("failure_policy") == "halt"
                and result.get("state") == "failed"):
            # The plan's compiled policy is halt: stop issuing NEW passes. The
            # un-run tail stays un-run — truthfully — and the item stays
            # enrichment_pending until the failed pass succeeds on retry. No
            # ep_slug on the marker: it describes the walk, not a pass.
            out.append({"item_id": item_id, "state": "halted",
                        "halted_after": slug, "reason_code": "halt_policy"})
            break
    return out


def status(item_id: Optional[str] = None) -> list[dict[str, Any]]:
    conn = ledger.connect()
    if item_id:
        rows = conn.execute("SELECT * FROM passes WHERE item_id=? ORDER BY ep_slug", (item_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM passes ORDER BY updated_at DESC LIMIT 50").fetchall()
    return [dict(r) for r in rows]
