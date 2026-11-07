"""Default-off local shadow contract; all data stays in isolated temporary roots."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
CLI = ROOT / "bin" / "telephony"


def invoke(root: Path, *args: str, payload=None):
    env = {**os.environ, "WAX_ROOT": str(root)}
    return subprocess.run([sys.executable, str(CLI), "--json", *args],
                          input=None if payload is None else json.dumps(payload), text=True,
                          capture_output=True, env=env, timeout=5)


def event(*, event_id="evt-1", number="+12125550123", age=0, kind="call.initiated"):
    return {"data": {"id": event_id, "event_type": kind,
                     "occurred_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
                     "payload": {"call_session_id": "sess-1", "call_leg_id": "leg-1",
                                 "call_control_id": "control-1", "from": number}}}


def result(proc):
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def test_default_status_read_only_and_disabled_event_no_write(tmp_path):
    status = result(invoke(tmp_path, "status"))
    assert status["schema"] == "heyma.telephony.status.v1"
    assert status["mode"] == "disabled"
    assert status["control_enabled"] is False and status["ring_enabled"] is False
    assert status["decisions"] == []
    assert result(invoke(tmp_path, "event", payload=event()))["ignored"] is True
    assert list(tmp_path.iterdir()) == []


def test_shadow_receipt_idempotency_and_fresh_event_gate(tmp_path):
    result(invoke(tmp_path, "config", "--mode", "shadow"))
    result(invoke(tmp_path, "rules", "block", "+1 (212) 555-0123"))
    first = result(invoke(tmp_path, "event", payload=event()))
    assert first["decision"]["decision"] == "would_block"
    assert first["decision"]["enforced"] is False
    assert first["decision"]["verified_ingress"] is False
    second = result(invoke(tmp_path, "event", payload=event()))
    assert second["duplicate"] is True
    assert second["decision"] == first["decision"]
    assert result(invoke(tmp_path, "event", payload=event(event_id="old", age=400)))["ignored"]
    assert result(invoke(tmp_path, "event", payload=event(event_id="answered", kind="call.answered")))["ignored"]
    assert len(result(invoke(tmp_path, "status"))["decisions"]) == 1


def test_rules_conflicts_and_unknown_allow_preview_no_mutation(tmp_path):
    result(invoke(tmp_path, "rules", "allow", "+1 212 555 0123"))
    conflict = invoke(tmp_path, "rules", "block", "+12125550123")
    assert conflict.returncode == 2
    assert "error" in json.loads(conflict.stdout)
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    preview = result(invoke(tmp_path, "preview", payload=event(number="anonymous")))
    assert preview["decision"]["decision"] == "allow"
    assert preview["decision"]["provenance"]["inference"] == "not_used"
    assert before == sorted(str(p) for p in tmp_path.rglob("*"))
    result(invoke(tmp_path, "rules", "remove", "+12125550123"))
    assert result(invoke(tmp_path, "rules", "list"))["rules"]["allow"] == []


def test_provider_time_selects_latest_session_and_keeps_arrival_time(tmp_path):
    result(invoke(tmp_path, "config", "--mode", "shadow"))
    result(invoke(tmp_path, "rules", "block", "+12125550123"))
    newer = event(event_id="newer", number="+12125559999", age=1)
    older = event(event_id="older", age=2)
    result(invoke(tmp_path, "event", payload=newer))
    result(invoke(tmp_path, "event", payload=older))
    decisions = result(invoke(tmp_path, "status"))["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["event_id"] == "newer"
    assert decisions[0]["decision"] == "allow"
    assert decisions[0]["occurred_at"] != decisions[0]["observed_at"]


def test_duplicate_identical_payload_at_same_clock(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "components" / "telephony" / "src"))
    from telephony import core

    monkeypatch.setenv("WAX_ROOT", str(tmp_path))
    args = core._parser().parse_args(["event"])
    fixed = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    payload = event()
    payload["data"]["occurred_at"] = fixed.isoformat()
    monkeypatch.setattr(core, "_read_input", lambda: payload)
    core.execute(core._parser().parse_args(["config", "--mode", "shadow"]))
    first = core.execute(args, clock=lambda: fixed)
    second = core.execute(args, clock=lambda: fixed)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["decision"] == first["decision"]


def test_provider_time_whole_second_vs_fractional_and_legacy_receipts(tmp_path):
    result(invoke(tmp_path, "config", "--mode", "shadow"))
    whole = event(event_id="whole")
    fraction = event(event_id="fraction")
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=1)
    whole["data"]["occurred_at"] = base.isoformat()
    fraction["data"]["occurred_at"] = base.replace(microsecond=500000).isoformat()
    result(invoke(tmp_path, "event", payload=whole))
    result(invoke(tmp_path, "event", payload=fraction))
    assert result(invoke(tmp_path, "status"))["decisions"][0]["event_id"] == "fraction"

    # Old rows stored optional fractions; don't rely on lexical TEXT order for them.
    dbpath = tmp_path / "var" / "telephony" / "receipts.sqlite3"
    with sqlite3.connect(dbpath) as db:
        db.execute("UPDATE receipts SET occurred_at=? WHERE event_id=?",
                   (whole["data"]["occurred_at"].replace("+00:00", "Z"), "whole"))
        db.execute("UPDATE receipts SET occurred_at=? WHERE event_id=?",
                   (fraction["data"]["occurred_at"].replace("+00:00", "Z"), "fraction"))
    assert result(invoke(tmp_path, "status"))["decisions"][0]["event_id"] == "fraction"


def test_missing_exact_leg_cannot_be_assessed(tmp_path):
    result(invoke(tmp_path, "config", "--mode", "shadow"))
    payload = event()
    del payload["data"]["payload"]["call_leg_id"]
    proc = invoke(tmp_path, "event", payload=payload)
    assert proc.returncode == 2
    assert "call_leg_id" in json.loads(proc.stdout)["error"]
    assert not (tmp_path / "var").exists()


def test_status_read_only_with_existing_receipts(tmp_path):
    result(invoke(tmp_path, "config", "--mode", "shadow"))
    result(invoke(tmp_path, "event", payload=event()))
    state = tmp_path / "var" / "telephony"
    before = {p.name: p.stat().st_mtime_ns for p in state.iterdir()}
    result(invoke(tmp_path, "status"))
    assert {p.name: p.stat().st_mtime_ns for p in state.iterdir()} == before


def test_invalid_config_updates_preserve_existing_file(tmp_path):
    result(invoke(tmp_path, "config", "--mode", "shadow", "--ring", "on"))
    config = tmp_path / "config" / "telephony.json"
    original = config.read_bytes()
    for args in (("--mode", "enforce"), ("--ring", "perhaps"),
                 ("--mode", "shadow", "--ring", "perhaps")):
        proc = invoke(tmp_path, "config", *args)
        assert proc.returncode == 2
        assert json.loads(proc.stdout)["schema"] == "heyma.telephony.error.v1"
        assert config.read_bytes() == original


@pytest.mark.parametrize("args", [("config", "--mode", "enforce"), ("rules", "block", "123"),
                                   ("rules", "list", "+12125550123"), ("rules", "allow")])
def test_rejected_operations(tmp_path, args):
    assert invoke(tmp_path, *args).returncode != 0
    assert list(tmp_path.iterdir()) == []


def test_bad_json_and_oversized_event_do_not_write(tmp_path):
    env = {**os.environ, "WAX_ROOT": str(tmp_path)}
    for payload in ("not JSON", "{\"data\":\"" + "x" * (64 * 1024) + "\"}"):
        proc = subprocess.run([sys.executable, str(CLI), "--json", "event"], input=payload,
                              capture_output=True, text=True, env=env, timeout=5)
        assert proc.returncode == 2
        assert json.loads(proc.stdout)["schema"] == "heyma.telephony.error.v1"
    assert list(tmp_path.iterdir()) == []


def test_corrupt_config_fails_closed_without_writing(tmp_path):
    config = tmp_path / "config" / "telephony.json"
    config.parent.mkdir()
    config.write_text('{"mode":"enforce"}')
    proc = invoke(tmp_path, "status")
    assert proc.returncode == 2
    assert "error" in json.loads(proc.stdout)
    assert not (tmp_path / "var").exists()
