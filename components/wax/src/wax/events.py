"""Bloodbank envelopes + a durable outbox.

Two rules, both learned from what the old pipeline got wrong:

1. **Publishing is fail-open.** A NATS outage must never break a recording. The
   event is written to the outbox in the same breath as the state change, and a
   drainer publishes it later. Recording does not depend on the bus being up.

2. **Delivered means acked.** A row is marked published only on a JetStream
   PubAck. "The socket accepted our bytes" is not delivery, and marking those
   rows published is how an audit trail quietly develops holes.

Subjects follow the live contract in
bloodbank/services/agent-hooks/core/validate.py:
    type    = bloodbank.v1.<domain>.<entity>.<action>
    subject = bloodbank.<evt|cmd>.v1.<domain>.<entity>.<action>
Every entity used here (session, file, transcription, task, status, heartbeat)
is already in ALLOWED_ENTITIES, so nothing here is blocked on a schema PR.

NOTE on commands: Candystore subscribes to `bloodbank.evt.v1.>` ONLY, and the
command stream is a workqueue with a short max_age. A raw command is therefore
invisible in Candystore and gone within a day. So every command Wax issues is
ALSO mirrored as `...task.requested` carrying the same command_id — that mirror
is what makes "find the command that invoked this EP" answerable at all.
"""

import json
import os
import socket
import uuid
from typing import Any, Optional

from . import ledger, natsclient, sentinel

DOMAIN = "audio"
PRODUCER = "wax"
SERVICE = "wax"
PROJECT = "wax"
SOURCE = f"//{socket.gethostname()}/wax"

# Deterministic namespace so a command_id can be recomputed rather than stored.
WAX_NS = uuid.UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    subject      TEXT NOT NULL,
    envelope     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    published_at TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT
);
CREATE INDEX IF NOT EXISTS outbox_unpublished ON outbox(published_at, id);
"""


def _ensure() -> None:
    ledger.connect().executescript(OUTBOX_SCHEMA)


def subject_for(ce_type: str, kind: str) -> str:
    vendor, version, domain, entity, action = ce_type.split(".", 4)
    marker = {"event": "evt", "command": "cmd", "reply": "rpy"}[kind]
    return f"{vendor}.{marker}.{version}.{domain}.{entity}.{action}"


def envelope(entity: str, action: str, data: dict[str, Any], *,
             kind: str = "event", correlationid: Optional[str] = None,
             causationid: Optional[str] = None,
             command_id: Optional[str] = None,
             ordering_key: Optional[str] = None) -> tuple[str, dict[str, Any]]:
    ce_type = f"bloodbank.v1.{DOMAIN}.{entity}.{action}"
    subject = subject_for(ce_type, kind)
    eid = str(uuid.uuid4())
    env: dict[str, Any] = {
        "specversion": "1.0",
        "id": eid,
        "source": SOURCE,
        "type": ce_type,
        "subject": subject,
        "time": sentinel.utcnow(),
        "correlationid": correlationid or eid,
        "producer": PRODUCER,
        "service": SERVICE,
        "domain": DOMAIN,
        "kind": kind,
        # Candystore buckets by actor+project; without both, events render as
        # unknown/unknown and drop out of /summary/by-project entirely.
        "actor": {"type": "service", "agent_id": "service:wax"},
        "project": PROJECT,
        # Candystore buckets on data.project, NOT the top-level field —
        # measured: an envelope with only top-level project still renders as
        # "unknown" and drops out of /summary/by-project. Set both.
        "data": {**data, "project": PROJECT},
    }
    if causationid:
        env["causationid"] = causationid
    if kind == "event":
        env["ordering_key"] = ordering_key or data.get("item_id") or data.get("capture_id") or eid
    if kind == "command":
        env["command_id"] = command_id or eid
        env["idempotency_key"] = command_id or eid
        env["delivery"] = "single_consumer"
    return subject, env


def emit(entity: str, action: str, data: dict[str, Any], **kw: Any) -> int:
    """Queue an event. Returns the outbox row id. NEVER raises on bus trouble."""
    _ensure()
    subject, env = envelope(entity, action, data, **kw)
    cur = ledger.connect().execute(
        "INSERT INTO outbox(subject,envelope,created_at) VALUES(?,?,?)",
        (subject, json.dumps(env, sort_keys=True), sentinel.utcnow()),
    )
    return cur.lastrowid


def command_id_for(item_id: str, ep_slug: str, attempt: int = 1) -> str:
    """Deterministic, so the invoking command is findable without storing it."""
    return str(uuid.uuid5(WAX_NS, f"ep:{item_id}:{ep_slug}:{attempt}"))


def emit_ep_command(item_id: str, ep_slug: str, argv: list[str], attempt: int = 1) -> str:
    """Issue an EP command AND mirror it as an event so Candystore can see it."""
    cid = command_id_for(item_id, ep_slug, attempt)
    payload = {"ep_slug": ep_slug, "item_id": item_id, "attempt": attempt, "argv": argv}
    emit("task", "start", payload, kind="command", command_id=cid, correlationid=cid)
    emit("task", "requested",
         {**payload, "command_id": cid,
          "command_subject": "bloodbank.cmd.v1.audio.task.start",
          "idempotency_key": cid, "invoked_by": "wax"},
         correlationid=cid, causationid=cid)
    return cid


# ------------------------------------------------------------------ drain ----

def backlog() -> int:
    _ensure()
    return ledger.connect().execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE published_at IS NULL").fetchone()["n"]


def drain(limit: int = 50) -> dict[str, Any]:
    """Publish queued events. A row is marked published ONLY on a PubAck."""
    _ensure()
    conn = ledger.connect()
    rows = conn.execute(
        "SELECT id, subject, envelope FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return {"published": 0, "failed": 0, "backlog": 0}

    published = failed = 0
    try:
        with natsclient.Nats() as n:
            for r in rows:
                try:
                    n.publish_ack(r["subject"], json.loads(r["envelope"]))
                    conn.execute(
                        "UPDATE outbox SET published_at=?, attempts=attempts+1, last_error=NULL WHERE id=?",
                        (sentinel.utcnow(), r["id"]),
                    )
                    published += 1
                except natsclient.NatsError as e:
                    conn.execute(
                        "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                        (str(e)[:400], r["id"]),
                    )
                    failed += 1
                    break  # connection is suspect; retry the rest next pass
    except (OSError, natsclient.NatsError) as e:
        conn.execute(
            "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?",
            (f"connect: {str(e)[:380]}", rows[0]["id"]),
        )
        failed += 1

    return {"published": published, "failed": failed, "backlog": backlog()}
