"""Local, read-only telephony projection and bounded shadow-event bridge.

The Wax call ledger owns identity and processing. The Telephony component owns only
shadow screening receipts; neither a snapshot nor this bridge calls a provider.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import calls, paths

log = logging.getLogger("wax.telephony")
CORE_CLI = Path(__file__).resolve().parents[4] / "bin" / "telephony"
TIMEOUT_S = 1.5
MAX_OUTPUT = 512 * 1024
MAX_EVENT = 64 * 1024
MAX_LIMIT = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def unavailable(reason: str) -> dict[str, Any]:
    return {"schema": "heyma.telephony.snapshot.v1", "generated_at": _now(),
            "mode": "disabled", "control_enabled": False, "ring_enabled": False,
            "health": "unavailable", "readiness": reason, "calls": []}


def _core_status() -> dict[str, Any]:
    if not CORE_CLI.is_file():
        raise FileNotFoundError("local telephony CLI unavailable")
    proc = subprocess.run([str(CORE_CLI), "--json", "status"], capture_output=True,
                          timeout=TIMEOUT_S, check=False)
    if proc.returncode or len(proc.stdout) > MAX_OUTPUT:
        raise ValueError("local telephony status unavailable")
    data = json.loads(proc.stdout)
    if not isinstance(data, dict) or data.get("schema") != "heyma.telephony.status.v1" \
            or data.get("mode") not in ("disabled", "shadow") or not isinstance(data.get("decisions"), list):
        raise ValueError("local telephony status invalid")
    return data


def shadow_event(raw: Any, *, is_new: bool) -> None:
    """Only fresh successful `wax calls event` deliveries enter local simulation.

    Deliberately no effect on the ingestion result or the call ledger. The core
    itself rejects events when disabled; failures here are diagnostic only.
    """
    if not is_new or not CORE_CLI.is_file():
        return
    try:
        encoded = json.dumps(raw, separators=(",", ":")).encode()
        if len(encoded) > MAX_EVENT:
            log.warning("shadow event not forwarded: envelope exceeds %d bytes", MAX_EVENT)
            return
        proc = subprocess.run([str(CORE_CLI), "--json", "event"], input=encoded,
                              capture_output=True, timeout=TIMEOUT_S, check=False)
        if proc.returncode:
            log.warning("shadow event unavailable (local CLI exit %d)", proc.returncode)
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        log.warning("shadow event unavailable: %s", type(exc).__name__)


def _read_conn() -> sqlite3.Connection:
    # No ledger.connect()/calls.db(): those initialize and migrate tables on a
    # read-only GUI snapshot. A missing ledger must be visibly unavailable.
    conn = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True, timeout=0.3)
    conn.row_factory = sqlite3.Row
    return conn


def _item_processing(conn: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any]:
    item_id = item["item_id"]
    result = {"item_id": item_id, "state": item["state"]}
    plan = conn.execute("SELECT plan_id,definitions FROM processing_plans WHERE item_id=?", (item_id,)).fetchone()
    counts = {"completed": 0, "total": 0, "failed": 0}
    finalized = False
    if plan:
        try:
            definitions = json.loads(plan["definitions"])
            if not isinstance(definitions, dict):
                raise ValueError("invalid definitions")
            counts["total"] = len(definitions)
            states = {row["ep_slug"]: row for row in conn.execute(
                "SELECT ep_slug,state,version FROM passes WHERE item_id=?", (item_id,))}
            for slug, definition in definitions.items():
                row = states.get(slug)
                if row and row["state"] in ("completed", "skipped") and row["version"] == int(definition.get("version") or 1):
                    counts["completed"] += 1
                elif row and row["state"] == "failed":
                    counts["failed"] += 1
            receipt = conn.execute("SELECT 1 FROM completions WHERE item_id=? AND plan_id=? LIMIT 1",
                                   (item_id, plan["plan_id"])).fetchone()
            # The durable completion for the *current* plan is authoritative.
            # A raw item state or all completed pass rows cannot substitute.
            finalized = bool(receipt and counts["completed"] == counts["total"])
        except (ValueError, TypeError, AttributeError):
            result["warning"] = "invalid_processing_plan"
    result["finalized"] = finalized
    result["completed_passes"] = counts["completed"]
    result["total_passes"] = counts["total"]
    result["failed_passes"] = counts["failed"]
    return result


def _recordings_finalized(recordings: list[dict[str, Any]], matched: dict[str, dict[str, Any]]) -> bool:
    """Only exact recording filenames prove that every distinct audio was processed."""
    names = {item["orig_name"] or Path(item["path"]).name for item in matched.values()}
    claimed: set[str] = set()
    for rec in recordings:
        filename = rec["file_name"]
        if not filename and rec["object_name"]:
            # Telnyx custom storage names <leg>-<epoch>.mp3; Wax intake prefixes
            # the recording date. The date may be absent from a historical CDR,
            # so match the complete object suffix, never merely its SIP leg.
            object_stem = Path(rec["object_name"]).stem
            # The intake appends a 64-hex receipt id; older backfills do not.
            candidates = {name for name in names if name.startswith("telnyx-") and
                          (name.endswith("-" + object_stem + ".mp3") or
                           (name.endswith(".mp3") and len(name) >= 69 and
                            name[-69] == "-" and
                            all(c in "0123456789abcdef" for c in name[-68:-4].lower()) and
                            name[:-69].endswith("-" + object_stem)))}
        else:
            candidates = {filename} if filename in names else set()
        if len(candidates) != 1 or candidates & claimed:
            return False
        claimed.update(candidates)
    return bool(recordings)


def _processing(conn: sqlite3.Connection, session: str) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    recordings = [dict(r) for r in conn.execute(
        "SELECT kind,call_leg_id,object_name,file_name,fetch_error FROM call_recordings "
        "WHERE call_session_id=? OR call_leg_id IN (SELECT call_leg_id FROM call_legs WHERE call_session_id=?)",
        (session, session)) if r["kind"] != "record_verb"]
    # Query only Telnyx-named audio. Identity is a parsed leg/session, never an
    # approximate time/phone-number association (redials may share both).
    matched: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT item_id,orig_name,path,state FROM items "
                            "WHERE orig_name LIKE 'telnyx-%' OR path LIKE '%/telnyx-%'"):
        item = dict(row)
        ident = calls.audio_identity(item["orig_name"] or Path(item["path"]).name)
        if not ident or ident.get("record_verb"):
            continue
        ident_session = ident.get("call_session_id") or (
            calls._session_for(conn, ident) if ident.get("call_leg_id") else None)
        if ident_session == session:
            matched[item["item_id"]] = item
    if any(rec["call_leg_id"] and conn.execute(
            "SELECT 1 FROM call_legs WHERE call_leg_id=? AND call_session_id!=?",
            (rec["call_leg_id"], session)).fetchone() for rec in recordings):
        warnings.append("recording_leg_session_conflict")
    if any(not rec["call_leg_id"] and not rec["file_name"] for rec in recordings):
        warnings.append("recording_missing_audio_identity")
    if any(rec["fetch_error"] for rec in recordings):
        warnings.append("recording_fetch_failed")
    items = [_item_processing(conn, item) for item in matched.values()]
    totals = {"completed_passes": sum(item["completed_passes"] for item in items),
              "total_passes": sum(item["total_passes"] for item in items),
              "failed_passes": sum(item["failed_passes"] for item in items)}
    if not items:
        state = "error" if any(rec["fetch_error"] for rec in recordings) else "awaiting_audio"
        if recordings:
            warnings.append("recording_not_in_wax_ledger")
    elif any(item["state"] in ("failed", "suspect") or item["failed_passes"] for item in items):
        state = "error"
    elif any(item["state"] == "skipped" for item in items):
        state = "skipped"
    elif recordings and all(item["finalized"] for item in items) and _recordings_finalized(recordings, matched):
        state = "complete"
    elif any(item["state"] in ("enrichment_pending", "transcribed", "complete") for item in items):
        state = "enriching"
    elif any(item["state"] == "archived" for item in items):
        state = "transcribing"
    elif any(item["state"] == "pending" for item in items):
        state = "queued"
    else:
        state = "unknown"
    if items and not recordings:
        warnings.append("recording_inventory_missing")
    return {"state": state, "items": items, **totals}, warnings


def _call_state(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[str, str | None, str | None]:
    """Do not let a losing SIP fork's hangup terminate a live session."""
    session = row["call_session_id"]
    events = list(conn.execute("SELECT event_type,occurred_at,call_leg_id FROM call_events WHERE call_session_id=?",
                               (session,)))
    legs = list(conn.execute("SELECT call_leg_id,started_at,answered_at,finished_at,completed "
                             "FROM call_legs WHERE call_session_id=?", (session,)))
    terminal_types = {"call.hangup", "call.ended", "call.bridged.hangup"}
    terminal_by_leg: dict[str, datetime] = {}
    session_terminal = False
    for event in events:
        if event["event_type"] not in terminal_types:
            continue
        leg = event["call_leg_id"]
        if not leg:
            session_terminal = True
        elif (at := calls._ts(event["occurred_at"])):
            terminal_by_leg[leg] = max(at, terminal_by_leg.get(leg, at))

    # A derived calls.ended_at is the maximum of *any* leg finish or hangup,
    # not necessarily the end of the session. Check each leg independently.
    active = any(not leg["finished_at"] and not leg["completed"] and
                 (leg["started_at"] or leg["answered_at"] or any(
                     e["call_leg_id"] == leg["call_leg_id"] and
                     e["event_type"] in ("call.initiated", "call.answered") for e in events)) and
                 not any(e["call_leg_id"] == leg["call_leg_id"] and e["event_type"] in terminal_types
                         for e in events)
                 for leg in legs)
    observed_legs = {e["call_leg_id"] for e in events if e["call_leg_id"]}
    observed_legs.update(leg["call_leg_id"] for leg in legs)
    active |= any(not any(e["call_leg_id"] == leg and e["event_type"] in terminal_types for e in events)
                  for leg in observed_legs if leg not in {r["call_leg_id"] for r in legs})
    # Session-wide hangup is authoritative even if an incomplete CDR has not
    # yet populated a leg's finished_at field.
    if session_terminal:
        active = False
    answered = calls._ts(row["answered_at"])
    answers = [calls._ts(e["occurred_at"]) for e in events if e["event_type"] == "call.answered"]
    answered = max((at for at in [answered, *answers] if at), default=None)
    ended = calls._ts(row["ended_at"])
    latest_terminal = max((at for at in [*terminal_by_leg.values(),
                                          *(calls._ts(leg["finished_at"]) for leg in legs)] if at), default=None)
    terminal = session_terminal or bool(row["ended_at"] or terminal_by_leg or latest_terminal)
    # An announced, settled call is final unless there is evidence of a live
    # sibling that started/answered after the last known terminal timestamp.
    if row["announced_at"] and row["ended_at"] and calls.settled(dict(row)) and (
            not active or (answered and ended and ended >= answered and
                           latest_terminal and latest_terminal >= answered and
                           all(calls._ts(e["occurred_at"]) <= latest_terminal
                               for e in events if e["call_leg_id"] and
                               e["event_type"] in ("call.initiated", "call.answered") and
                               calls._ts(e["occurred_at"])))):
        return "ended", None, row["ended_at"]
    if terminal and not active and (not answered or not latest_terminal or latest_terminal >= answered):
        return "ended", None, row["ended_at"] or calls._iso(latest_terminal)
    if answered and (active or not terminal or (latest_terminal and answered > latest_terminal)):
        return "connected", None, None
    if row["direction"] != "inbound":
        return "unknown", None, None
    starts = [calls._ts(e["occurred_at"]) for e in events if e["event_type"] == "call.initiated"]
    starts = [start for start in starts if start is not None]
    if starts and (active or not terminal):
        latest = max(starts)
        age = (datetime.now(timezone.utc) - latest).total_seconds()
        if 0 <= age <= 90:
            return "ringing", latest.isoformat(timespec="seconds").replace("+00:00", "Z"), None
    return "unknown", None, None


def _screening(decisions: list[dict[str, Any]], session: str, legs: set[str]) -> dict[str, Any]:
    # Exact identity only. Receipt arrival order cannot replace newer provider evidence.
    matched = [d for d in decisions if isinstance(d, dict) and
               (d.get("session_id") == session or
                (d.get("session_id") in (None, "") and d.get("call_leg_id") in legs and d.get("call_leg_id")))
               and d.get("mode") == "shadow" and d.get("enforced") is False
               and d.get("decision") in ("allow", "would_block")
               and calls._ts(d.get("occurred_at")) is not None]
    matched.sort(key=lambda d: (calls._ts(d["occurred_at"]), str(d.get("event_id") or "")))
    chosen = matched[-1] if matched else None
    return {"decision": chosen["decision"] if chosen else "unassessed",
            "reason": chosen.get("reason") if chosen else None, "enforced": False}


def snapshot(limit: int = 30) -> dict[str, Any]:
    """No provider fetch, inference or ledger writes; failures are not empty success."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    try:
        status = _core_status()
    except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return unavailable(f"Local telephony status unavailable ({type(exc).__name__}); live enforcement unavailable")
    result = {"schema": "heyma.telephony.snapshot.v1", "generated_at": _now(),
              "mode": status["mode"], "control_enabled": False,
              "ring_enabled": bool(status.get("ring_enabled", False)),
              "health": "ok", "readiness": status.get("readiness") or "Live enforcement unavailable",
              "calls": []}
    try:
        with _read_conn() as conn:
            rows = conn.execute("SELECT * FROM calls ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
            for row in rows:
                session = row["call_session_id"]
                call_state, ring_event_at, ended_at = _call_state(conn, row)
                processing, warnings = _processing(conn, session)
                legs = {r[0] for r in conn.execute(
                    "SELECT call_leg_id FROM call_legs WHERE call_session_id=?", (session,))}
                legs.update(r[0] for r in conn.execute(
                    "SELECT call_leg_id FROM call_events WHERE call_session_id=? AND call_leg_id IS NOT NULL", (session,)))
                screening = _screening(status["decisions"], session, legs)
                if status["mode"] == "shadow" and screening["decision"] == "unassessed":
                    warnings.append("no_shadow_assessment_receipt")
                direction = row["direction"]
                if direction not in ("inbound", "outbound"):
                    warnings.append("call_direction_unknown")
                result["calls"].append({
                    "session_id": session, "direction": direction if direction in ("inbound", "outbound") else None,
                    "from_number": row["remote_number"] if direction == "inbound" else row["line"],
                    "to_number": row["line"] if direction == "inbound" else row["remote_number"],
                    "remote_name": row["remote_name"], "started_at": row["started_at"],
                    "ring_event_at": ring_event_at,
                    "answered_at": row["answered_at"], "ended_at": ended_at,
                    "call_state": call_state, "screening": screening,
                    "processing": processing, **({"warnings": warnings} if warnings else {})})
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        result["health"] = "unavailable"
        result["readiness"] = f"Call ledger unavailable ({type(exc).__name__}); live enforcement unavailable"
        result["calls"] = []
    return result
