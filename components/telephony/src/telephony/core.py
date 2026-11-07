"""Local, default-off Telephony shadow policy. No provider command transport exists here."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any, Callable, Iterator

SCHEMA = "heyma.telephony.status.v1"
MAX_INPUT = 64 * 1024
MAX_RECEIPTS = 50
E164 = re.compile(r"\+[1-9][0-9]{1,14}\Z")
ID = re.compile(r"[A-Za-z0-9_:.\-]{1,128}\Z")
READINESS = "Shadow simulation only; live enforcement and verified HTTP ingress are unavailable. Calls still ring and record normally."


class TelephonyError(Exception):
    pass


def _root() -> Path:
    return Path(os.environ.get("WAX_ROOT", str(Path.home() / "HeyMa"))).expanduser()


def _paths() -> tuple[Path, Path]:
    return (Path(os.environ["TELEPHONY_CONFIG"]).expanduser() if os.environ.get("TELEPHONY_CONFIG")
            else _root() / "config" / "telephony.json",
            Path(os.environ["TELEPHONY_STATE_DIR"]).expanduser() if os.environ.get("TELEPHONY_STATE_DIR")
            else _root() / "var" / "telephony")


def _number(value: Any) -> str:
    if not isinstance(value, str):
        raise TelephonyError("number must be an E.164 string")
    # Only visual separators are accepted; never guess country codes.
    normalized = re.sub(r"[ ()\-.]", "", value)
    if not E164.fullmatch(normalized):
        raise TelephonyError("number must be E.164 (+ followed by 2–15 digits)")
    return normalized


def _id(value: Any, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise TelephonyError(f"{name} must be a bounded identifier")
    return value


def _config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mode": "disabled", "ring_enabled": False, "rules": {"allow": [], "block": []}}
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise TelephonyError("config is too large")
        data = json.loads(raw)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise TelephonyError("invalid telephony config") from exc
    if not isinstance(data, dict) or set(data) - {"mode", "ring_enabled", "rules"}:
        raise TelephonyError("invalid telephony config schema")
    mode = data.get("mode", "disabled")
    if mode not in ("disabled", "shadow"):
        raise TelephonyError("enforcement is unavailable; mode must be disabled or shadow")
    ring = data.get("ring_enabled", False)
    if not isinstance(ring, bool):
        raise TelephonyError("ring_enabled must be boolean")
    rules = data.get("rules", {"allow": [], "block": []})
    if not isinstance(rules, dict) or set(rules) != {"allow", "block"}:
        raise TelephonyError("rules must contain allow and block lists")
    for kind in ("allow", "block"):
        values = rules[kind]
        if not isinstance(values, list) or len(values) > 10000 or any(
            not isinstance(n, str) or not E164.fullmatch(n) for n in values
        ) or len(values) != len(set(values)):
            raise TelephonyError("rules must contain unique E.164 numbers")
    if set(rules["allow"]) & set(rules["block"]):
        raise TelephonyError("conflicting allow and block rules")
    return {"mode": mode, "ring_enabled": ring, "rules": rules}


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".telephony-", delete=False) as stream:
            name = stream.name
            json.dump(data, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def _read_input() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise TelephonyError("input exceeds 64 KiB")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise TelephonyError("stdin must contain a JSON object") from exc
    if not isinstance(value, dict):
        raise TelephonyError("stdin must contain a JSON object")
    return value


def _event(raw: dict[str, Any]) -> dict[str, Any]:
    envelope: Any = raw.get("data", raw)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
        raise TelephonyError("expected Telnyx event envelope with data.payload")
    kind = envelope.get("event_type")
    if not isinstance(kind, str) or len(kind) > 80 or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9._-]*", kind):
        raise TelephonyError("invalid event_type")
    event_id = _id(envelope.get("id"), "event_id")
    payload = envelope["payload"]
    session = _id(payload.get("call_session_id"), "call_session_id")
    leg = _id(payload.get("call_leg_id"), "call_leg_id", optional=True)
    control = _id(payload.get("call_control_id"), "call_control_id", optional=True)
    occurred = envelope.get("occurred_at")
    if not isinstance(occurred, str) or len(occurred) > 40:
        raise TelephonyError("event requires occurred_at timestamp")
    try:
        timestamp = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TelephonyError("invalid occurred_at timestamp") from exc
    if timestamp.tzinfo is None:
        raise TelephonyError("occurred_at must include timezone")
    return {"event_id": event_id, "event_type": kind, "session_id": session,
            "call_leg_id": leg, "control_id": control,
            "occurred_at": timestamp.astimezone(timezone.utc), "payload": payload}


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _legacy_timestamp_key(value: str) -> str:
    # Existing receipts can use optional fractional seconds or a UTC offset.
    return _utc_timestamp(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _decision(event: dict[str, Any], config: dict[str, Any], now: datetime) -> dict[str, Any]:
    payload = event["payload"]
    value = next((payload[k] for k in ("from", "from_number", "caller_id_number")
                  if isinstance(payload.get(k), str) and payload[k]), None)
    try:
        caller = _number(value)
    except TelephonyError:
        caller = None
    rules = config["rules"]
    if caller in rules["allow"]:
        decision, reason, source = "allow", "explicit_allow_rule", "number_allowlist"
    elif caller in rules["block"]:
        decision, reason, source = "would_block", "explicit_block_rule", "number_blocklist"
    else:
        decision, reason, source = "allow", "no_explicit_block_evidence", "default_allow"
    return {"session_id": event["session_id"], "call_leg_id": event["call_leg_id"],
            "control_id": event["control_id"], "event_id": event["event_id"],
            "observed_at": _utc_timestamp(now),
            "occurred_at": _utc_timestamp(event["occurred_at"]),
            "decision": decision, "reason": reason, "mode": "shadow", "enforced": False,
            "verified_ingress": False,
            "evidence": {"caller_number": caller, "rule": source if source != "default_allow" else None},
            "provenance": {"source": source, "inference": "not_used", "ingress": "local_unverified_simulation"}}


def _database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=3)
    conn.execute("CREATE TABLE IF NOT EXISTS receipts (event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, occurred_at TEXT NOT NULL, observed_at TEXT NOT NULL, receipt TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS receipts_recent ON receipts(session_id, occurred_at DESC)")
    return conn


def _recent(path: Path) -> list[dict[str, Any]]:
    try:
        if not path.is_file():
            return []
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)) as conn:
            conn.create_function("telephony_utc_key", 1, _legacy_timestamp_key, deterministic=True)
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT receipt FROM (SELECT receipt, session_id, event_id, "
                "telephony_utc_key(occurred_at) AS time_key, "
                "ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY telephony_utc_key(occurred_at) DESC, event_id DESC) AS rank "
                "FROM receipts) WHERE rank=1 ORDER BY time_key DESC, event_id DESC LIMIT ?", (MAX_RECEIPTS,))]
    except (sqlite3.Error, ValueError) as exc:
        raise TelephonyError("decision receipts unavailable") from exc


def status(config: dict[str, Any], state: Path) -> dict[str, Any]:
    return {"schema": SCHEMA, "mode": config["mode"], "control_enabled": False,
            "ring_enabled": config["ring_enabled"], "decisions": _recent(state / "receipts.sqlite3"),
            "readiness": READINESS, "verified_ingress": False}


def execute(args: argparse.Namespace, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict[str, Any]:
    config_path, state_dir = _paths()
    if args.command == "config" and (args.mode is not None or args.ring is not None):
        with _locked(config_path):
            config = _config(config_path)
            if args.mode is not None:
                config["mode"] = args.mode
            if args.ring is not None:
                config["ring_enabled"] = args.ring == "on"
            _save(config_path, config)
        return {"mode": config["mode"], "ring_enabled": config["ring_enabled"], "rules": config["rules"]}
    if args.command == "rules" and args.operation != "list":
        number = _number(args.number)
        with _locked(config_path):
            config = _config(config_path)
            rules = config["rules"]
            if args.operation in ("allow", "block"):
                opposite = "block" if args.operation == "allow" else "allow"
                if number in rules[opposite]:
                    raise TelephonyError("conflicting rule; remove opposite rule first")
                rules[args.operation] = sorted(set(rules[args.operation]) | {number})
            else:
                rules["allow"] = [n for n in rules["allow"] if n != number]
                rules["block"] = [n for n in rules["block"] if n != number]
            _save(config_path, config)
        return {"rules": rules}
    config = _config(config_path)
    if args.command == "status":
        return status(config, state_dir)
    if args.command == "config":
        return {"mode": config["mode"], "ring_enabled": config["ring_enabled"], "rules": config["rules"]}
    if args.command == "rules":
        return {"rules": config["rules"]}
    if args.command in ("event", "preview"):
        event = _event(_read_input())
        if event["event_type"] != "call.initiated":
            return {"schema": "heyma.telephony.event.v1", "mode": config["mode"], "ignored": True,
                    "reason": "not_an_initiated_event", "verified_ingress": False}
        if event["call_leg_id"] is None:
            raise TelephonyError("call.initiated requires exact call_leg_id")
        if event["payload"].get("direction") not in (None, "incoming", "inbound"):
            return {"schema": "heyma.telephony.event.v1", "mode": config["mode"], "ignored": True,
                    "reason": "not_an_inbound_event", "verified_ingress": False}
        now = clock().astimezone(timezone.utc)
        if args.command == "preview":
            return {"schema": "heyma.telephony.preview.v1", "simulation": True,
                    "decision": _decision(event, config, now)}
        if config["mode"] == "disabled":
            return {"schema": "heyma.telephony.event.v1", "mode": "disabled", "ignored": True,
                    "reason": "policy_disabled", "verified_ingress": False}
        age = (now - event["occurred_at"]).total_seconds()
        if not 0 <= age <= 300:
            return {"schema": "heyma.telephony.event.v1", "mode": "shadow", "ignored": True,
                    "reason": "not_fresh_initiated_event", "verified_ingress": False}
        receipt = _decision(event, config, now)
        dbpath = state_dir / "receipts.sqlite3"
        try:
            with closing(_database(dbpath)) as db, db:
                inserted = db.execute("INSERT OR IGNORE INTO receipts(event_id,session_id,occurred_at,observed_at,receipt) VALUES(?,?,?,?,?)",
                                      (receipt["event_id"], receipt["session_id"], receipt["occurred_at"],
                                       receipt["observed_at"], json.dumps(receipt, sort_keys=True))).rowcount == 1
                row = db.execute("SELECT receipt FROM receipts WHERE event_id=?", (receipt["event_id"],)).fetchone()
                saved = json.loads(row[0])
        except sqlite3.Error as exc:
            raise TelephonyError("decision receipt unavailable") from exc
        return {"schema": "heyma.telephony.event.v1", "mode": "shadow", "ignored": False,
                "duplicate": not inserted, "verified_ingress": False, "decision": saved}
    raise TelephonyError("unsupported command")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local-only, default-off Telephony shadow simulator")
    parser.add_argument("--json", action="store_true", help="machine JSON output")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    config = sub.add_parser("config")
    config.add_argument("--mode")
    config.add_argument("--ring")
    rules = sub.add_parser("rules")
    rules.add_argument("operation", choices=("list", "allow", "block", "remove"))
    rules.add_argument("number", nargs="?")
    sub.add_parser("preview")
    sub.add_parser("event")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "config" and args.mode is not None and args.mode not in ("disabled", "shadow"):
            raise TelephonyError("enforcement is unavailable; mode must be disabled or shadow")
        if args.command == "config" and args.ring is not None and args.ring not in ("on", "off"):
            raise TelephonyError("ring must be on or off")
        if args.command == "rules" and ((args.operation == "list") != (args.number is None)):
            raise TelephonyError("rules list takes no number; mutations require one number")
        result = execute(args)
    except (TelephonyError, OSError) as exc:
        # Avoid leaking filesystem paths, raw payloads, or provider data in errors.
        message = str(exc) if isinstance(exc, TelephonyError) else "local telephony storage unavailable"
        print(json.dumps({"error": message, "schema": "heyma.telephony.error.v1"}), file=sys.stdout if args.json else sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0
