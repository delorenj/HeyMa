"""Telephony call identity: one ledger row per phone call, joined to its audio.

The audio of a call reaches Wax on its own (telnyx-s3-intake delivers the native
dual-channel recording; Wax fetches Telnyx voicemail itself, below). What the
audio never carries is WHO and WHICH WAY: caller, direction, forwarded-from,
outcome. This module owns that, keyed by Telnyx's `call_session_id`:

| table             | one row per                    | written from                         |
|-------------------|--------------------------------|--------------------------------------|
| `calls`           | phone call (session)           | derived from the three below          |
| `call_legs`       | Telnyx CDR leg                 | `GET /v2/detail_records` (sync)       |
| `call_recordings` | native recording or voicemail  | `GET /v2/recordings`, voicemail events|
| `call_events`     | Telnyx webhook event (`data.id`)| `wax calls event` (n8n) or sync      |

Two writers, both idempotent, so either may miss without losing a call:

- **push** — n8n receives the Telnyx webhook and pipes the raw envelope into
  `wax calls event` (n8n-nodes-heyma). n8n invokes Wax and owns nothing.
- **pull** — `wax calls sync` reads CDRs, recordings and Telnyx's own
  webhook-delivery log (which retains every event's payload, delivered or not)
  for a window. It is the reconciliation path; a dropped webhook costs nothing.

`calls` is always recomputed from the stored legs and events, never patched,
so the order in which facts arrive does not matter. A call with no audio (missed,
declined, abandoned) is still a row: that is the call log.

Identity of a recording file:
- `telnyx-<UTC date>-<call_leg_id>-<epoch>-<receipt>.mp3` (telnyx-s3-intake)
- `telnyx-vm-<UTC date>-<call_session_id>-<epoch>.wav` (voicemail, this module)
- `rec_<profile>_<name>_<digits>@yymmdd_hhmmss.<ext>` (Groundwire, retired
  2026-11-03; matched by remote number and start time within a few seconds)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import ledger, paths, rename, sentinel

log = logging.getLogger("wax.calls")

API = os.environ.get("WAX_TELNYX_API", "https://api.telnyx.com/v2")
# The "Telnyx Org API Key" item, by UUID (its title is not unique-safe).
KEY_OP = os.environ.get("WAX_TELNYX_API_KEY_OP", "op://DeLoSecrets/74k7nz3xkryqns4lpf65a323ue/credential")
# The Telnyx DID every call reaches us on, and the Verizon cell the outbound
# profile presents as caller ID (ani_override) and that forwards to the DID.
LINE = os.environ.get("WAX_TELNYX_LINE", "+12154498437")
CELL = os.environ.get("WAX_TELNYX_CELL", "+12152083549")
ME = os.environ.get("WAX_CALLS_ME", "Me")
REQUEST_TIMEOUT_S = 20.0
MAX_PAGES = 40

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
TELNYX_TRUNK = re.compile(rf"^telnyx-(\d{{4}}-\d{{2}}-\d{{2}})-({UUID})-(\d+)(?:-[0-9a-f]{{64}})?\.mp3$", re.I)
TELNYX_VM = re.compile(rf"^telnyx-vm-(\d{{4}}-\d{{2}}-\d{{2}})-({UUID})-(\d+)\.wav$", re.I)
GROUNDWIRE = re.compile(r"^rec_[^_]+_(?:(.*)_)?\+?(\d{7,15})@(\d{6})_(\d{6})\.[A-Za-z0-9]+$")
GROUNDWIRE_SKEW_S = 5
VOICEMAIL_LEG = "happy_voicemail_deposit"
US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_session_id TEXT PRIMARY KEY,
    direction       TEXT,            -- inbound | outbound
    line            TEXT,            -- our number the call is on
    remote_number   TEXT,            -- the other party, E.164
    remote_name     TEXT,            -- CNAM / caller_id_name, when it names a person
    forwarded_from  TEXT,            -- SIP Diversion number (the Verizon cell)
    forward_reason  TEXT,            -- Diversion reason: no-answer | user-busy | unconditional ...
    started_at      TEXT,            -- UTC ISO; call start, not file mtime
    answered_at     TEXT,
    ended_at        TEXT,
    talk_seconds    INTEGER,
    outcome         TEXT,            -- answered | voicemail | missed | declined | no_answer | failed
    hangup_cause    TEXT,
    first_seen      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_started ON calls(started_at);
CREATE TABLE IF NOT EXISTS call_legs (
    call_leg_id     TEXT PRIMARY KEY,
    call_session_id TEXT NOT NULL,
    direction       TEXT, cli TEXT, cld TEXT, dest_number TEXT, caller_name TEXT,
    started_at TEXT, answered_at TEXT, finished_at TEXT, call_sec INTEGER,
    hangup_cause TEXT, hangup_details TEXT, completed INTEGER, voicemail INTEGER,
    connection_name TEXT, raw TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS call_legs_session ON call_legs(call_session_id);
CREATE INDEX IF NOT EXISTS call_legs_started ON call_legs(started_at);
CREATE TABLE IF NOT EXISTS call_recordings (
    recording_key   TEXT PRIMARY KEY,  -- Telnyx recording id, or vm:<event id>
    kind            TEXT NOT NULL,     -- trunk | record_verb | voicemail
    call_leg_id     TEXT,
    call_session_id TEXT,
    channels        TEXT,
    started_at TEXT, ended_at TEXT, duration_millis INTEGER,
    object_name     TEXT,              -- <leg>-<epoch>.mp3 in our S3, when custom storage held it
    file_name       TEXT,              -- what Wax put in inbox/ (voicemail)
    raw TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS call_recordings_leg ON call_recordings(call_leg_id);
CREATE INDEX IF NOT EXISTS call_recordings_session ON call_recordings(call_session_id);
CREATE TABLE IF NOT EXISTS call_events (
    event_id        TEXT PRIMARY KEY,  -- Telnyx data.id; deliveries are at-least-once
    event_type      TEXT NOT NULL,
    occurred_at     TEXT,
    call_session_id TEXT,
    call_leg_id     TEXT,
    payload         TEXT NOT NULL,
    via             TEXT NOT NULL,     -- webhook | deliveries
    received_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS call_events_session ON call_events(call_session_id);
"""


class CallsError(RuntimeError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def db() -> sqlite3.Connection:
    conn = ledger.connect()
    if not getattr(conn, "_wax_calls_schema", False):
        conn.executescript(SCHEMA)
        try:
            conn._wax_calls_schema = True  # type: ignore[attr-defined]
        except AttributeError:
            pass
    return conn


# --------------------------------------------------------------------- time --

def _ts(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _iso(stamp: Optional[datetime]) -> Optional[str]:
    return stamp.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if stamp else None


def local_iso(value: Optional[str]) -> Optional[str]:
    """`2026-11-03T00:35:47Z` -> `2026-11-02T19:35:47-05:00` (the vault's style)."""
    stamp = _ts(value)
    return stamp.astimezone().isoformat(timespec="seconds") if stamp else None


# -------------------------------------------------------------------- Telnyx --

def api_key() -> str:
    from .provider import ProviderError, resolve_key
    try:
        return resolve_key(env_var="WAX_TELNYX_API_KEY", op_ref=KEY_OP)
    except ProviderError as exc:
        raise CallsError(str(exc), "no_api_key") from None


def _get(path: str, params: dict[str, Any], key: str) -> dict[str, Any]:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}",
                                                   "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        code = "telnyx_auth_failed" if exc.code in (401, 403) else "telnyx_http_error"
        raise CallsError(f"GET {path}: HTTP {exc.code}", code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CallsError(f"GET {path}: {exc}", "telnyx_unreachable") from None
    except ValueError:
        raise CallsError(f"GET {path}: unparseable body", "telnyx_bad_response") from None
    if not isinstance(data, dict):
        raise CallsError(f"GET {path}: unexpected body", "telnyx_bad_response")
    return data


def _pages(path: str, params: dict[str, Any], key: str, size: int = 250) -> Iterable[dict[str, Any]]:
    for number in range(1, MAX_PAGES + 1):
        page = _get(path, {**params, "page[size]": size, "page[number]": number}, key)
        rows = page.get("data") or []
        yield from (row for row in rows if isinstance(row, dict))
        meta = page.get("meta") or {}
        if not rows or number >= int(meta.get("total_pages") or 1):
            return
    log.warning("calls: %s stopped at %d pages", path, MAX_PAGES)


# ------------------------------------------------------------------- writers --

def _str(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def upsert_leg(conn: sqlite3.Connection, cdr: dict[str, Any]) -> Optional[str]:
    leg, session = _str(cdr.get("id")), _str(cdr.get("telnyx_session_id"))
    if not leg or not session:
        return None
    voicemail = bool(cdr.get("telnyx_voicemail")) or cdr.get("dest_number") == VOICEMAIL_LEG
    conn.execute(
        "INSERT INTO call_legs(call_leg_id,call_session_id,direction,cli,cld,dest_number,caller_name,"
        "started_at,answered_at,finished_at,call_sec,hangup_cause,hangup_details,completed,voicemail,"
        "connection_name,raw,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(call_leg_id) DO UPDATE SET call_session_id=excluded.call_session_id,"
        "direction=excluded.direction,cli=excluded.cli,cld=excluded.cld,dest_number=excluded.dest_number,"
        "caller_name=excluded.caller_name,started_at=excluded.started_at,answered_at=excluded.answered_at,"
        "finished_at=excluded.finished_at,call_sec=excluded.call_sec,hangup_cause=excluded.hangup_cause,"
        "hangup_details=excluded.hangup_details,completed=excluded.completed,voicemail=excluded.voicemail,"
        "connection_name=excluded.connection_name,raw=excluded.raw,updated_at=excluded.updated_at",
        (leg, session, _str(cdr.get("direction")), _str(cdr.get("cli")), _str(cdr.get("cld")),
         _str(cdr.get("dest_number")), _str(cdr.get("caller_name")),
         _iso(_ts(cdr.get("started_at"))), _iso(_ts(cdr.get("answered_at"))), _iso(_ts(cdr.get("finished_at"))),
         int(cdr.get("call_sec") or 0), _str(cdr.get("hangup_cause")), _str(cdr.get("hangup_details")),
         1 if cdr.get("completed") else 0, 1 if voicemail else 0, _str(cdr.get("connection_name")),
         json.dumps(cdr, sort_keys=True), sentinel.utcnow()))
    return session


def upsert_recording(conn: sqlite3.Connection, rec: dict[str, Any]) -> Optional[str]:
    rid = _str(rec.get("id"))
    if not rid:
        return None
    url = ((rec.get("download_urls") or {}).get("mp3") or (rec.get("download_urls") or {}).get("wav") or "")
    object_name = url.rsplit("/", 1)[-1] if url.startswith("s3://") else None
    kind = "record_verb" if rec.get("initiated_by") == "RecordVerb" else "trunk"
    conn.execute(
        "INSERT INTO call_recordings(recording_key,kind,call_leg_id,call_session_id,channels,started_at,"
        "ended_at,duration_millis,object_name,raw,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(recording_key) DO UPDATE SET kind=excluded.kind,call_leg_id=excluded.call_leg_id,"
        "call_session_id=excluded.call_session_id,channels=excluded.channels,started_at=excluded.started_at,"
        "ended_at=excluded.ended_at,duration_millis=excluded.duration_millis,"
        "object_name=COALESCE(excluded.object_name,call_recordings.object_name),"
        "raw=excluded.raw,updated_at=excluded.updated_at",
        (rid, kind, _str(rec.get("call_leg_id")), _str(rec.get("call_session_id")), _str(rec.get("channels")),
         _iso(_ts(rec.get("recording_started_at"))), _iso(_ts(rec.get("recording_ended_at"))),
         int(rec.get("duration_millis") or 0), object_name,
         json.dumps({k: v for k, v in rec.items() if k != "download_urls"} | {"object_name": object_name},
                    sort_keys=True),
         sentinel.utcnow()))
    return _str(rec.get("call_session_id"))


def _envelope(raw: Any) -> Optional[dict[str, Any]]:
    """Accept the Telnyx body ({data:{...}}), its `data`, or a delivery's `webhook`."""
    if not isinstance(raw, dict):
        return None
    for candidate in (raw.get("data"), raw.get("webhook"), raw.get("body"), raw):
        if isinstance(candidate, dict) and isinstance(candidate.get("event_type"), str) \
                and isinstance(candidate.get("payload"), dict):
            return candidate
        if isinstance(candidate, dict) and isinstance(candidate.get("data"), dict) \
                and isinstance(candidate["data"].get("event_type"), str):
            return candidate["data"]
    return None


def record_event(conn: sqlite3.Connection, raw: Any, via: str = "webhook") -> Optional[dict[str, Any]]:
    """Store one webhook event. Returns {event_id, event_type, call_session_id, new} or None."""
    env = _envelope(raw)
    if env is None:
        return None
    payload = env["payload"]
    event_id = _str(env.get("id")) or str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(env, sort_keys=True)))
    session = _str(payload.get("call_session_id"))
    cur = conn.execute(
        "INSERT OR IGNORE INTO call_events(event_id,event_type,occurred_at,call_session_id,call_leg_id,"
        "payload,via,received_at) VALUES(?,?,?,?,?,?,?,?)",
        (event_id, env["event_type"], _iso(_ts(env.get("occurred_at"))), session,
         _str(payload.get("call_leg_id")), json.dumps(payload, sort_keys=True), via, sentinel.utcnow()))
    return {"event_id": event_id, "event_type": env["event_type"], "call_session_id": session,
            "new": cur.rowcount == 1}


# ------------------------------------------------------------------ deriving --

def _diversion(value: str) -> tuple[Optional[str], Optional[str]]:
    number = re.search(r"sip:(\+?\d{7,15})", value or "")
    reason = re.search(r"reason=([A-Za-z_-]+)", value or "")
    return (number.group(1) if number else None), (reason.group(1).lower() if reason else None)


def person_name(name: Optional[str]) -> Optional[str]:
    """CNAM that names a person, title-cased; a location ("FLOURTOWN PA") is not a name."""
    text = re.sub(r"\s+", " ", name or "").strip()
    if not text or text.lower() in {"spam likely", "unknown", "anonymous", "wireless caller", "unavailable"}:
        return None
    tokens = text.split(" ")
    if len(tokens) >= 2 and tokens[-1].upper() in US_STATES and text.isupper():
        return None
    if re.fullmatch(r"\+?[\d\s().-]+", text):
        return None
    return text.title() if text.isupper() else text


def derive(conn: sqlite3.Connection, session: str) -> Optional[dict[str, Any]]:
    """Recompute one `calls` row from every leg and event stored for it."""
    legs = [dict(r) for r in conn.execute(
        "SELECT * FROM call_legs WHERE call_session_id=? ORDER BY started_at", (session,))]
    events = [dict(r) | {"payload": json.loads(r["payload"])} for r in conn.execute(
        "SELECT * FROM call_events WHERE call_session_id=? ORDER BY occurred_at", (session,))]
    if not legs and not events:
        return None
    kinds = {e["event_type"] for e in events}
    first = next((e["payload"] for e in events if e["event_type"] == "call.initiated"), None) \
        or (events[0]["payload"] if events else {})

    if first.get("direction") in ("incoming", "outgoing"):
        direction = "inbound" if first["direction"] == "incoming" else "outbound"
    elif any(leg["direction"] == "inbound" for leg in legs):
        direction = "inbound"
    elif legs:
        direction = "outbound"
    else:
        direction = None
    real = [leg for leg in legs if not leg["voicemail"]]
    voicemail_legs = [leg for leg in legs if leg["voicemail"]]
    lead = next((leg for leg in real if leg["direction"] == direction), real[0] if real else None)

    if direction == "inbound":
        remote = _str(first.get("from")) or (lead and lead["cli"])
        line = _str(first.get("to")) or (lead and lead["cld"]) or LINE
        name = person_name(_str(first.get("caller_id_name")) or (lead and lead["caller_name"]))
    else:
        remote = _str(first.get("to")) or (lead and lead["cld"])
        line = LINE
        name = None
    if name is None and remote:
        # Outbound calls carry no CNAM; the same number calling in did.
        known = conn.execute("SELECT remote_name FROM calls WHERE remote_number=? AND remote_name IS NOT NULL "
                             "AND call_session_id!=? ORDER BY started_at DESC LIMIT 1", (remote, session)).fetchone()
        name = known["remote_name"] if known else None

    forwarded_from = reason = None
    for event in events:
        for header in event["payload"].get("sip_headers") or []:
            if isinstance(header, dict) and str(header.get("name", "")).lower() == "diversion":
                forwarded_from, reason = _diversion(str(header.get("value", "")))
                break
        if forwarded_from:
            break

    stamps = [_ts(leg["started_at"]) for leg in legs] + [_ts(first.get("start_time"))]
    started = min((s for s in stamps if s), default=None)
    answered_legs = [leg for leg in real if leg["answered_at"] and (leg["completed"] or leg["call_sec"])]
    answered = min((_ts(leg["answered_at"]) for leg in answered_legs), default=None)
    if answered is None and kinds & {"call.answered", "call.bridged"} and direction == "outbound":
        answered = min((_ts(e["occurred_at"]) for e in events if e["event_type"] == "call.answered"),
                       default=None)
    ends = [_ts(leg["finished_at"]) for leg in legs] + [
        _ts(e["payload"].get("end_time")) or _ts(e["occurred_at"]) for e in events if e["event_type"] == "call.hangup"]
    ended = max((s for s in ends if s), default=None)
    talk = max((int(leg["call_sec"] or 0) for leg in answered_legs), default=0)
    if not talk and answered and ended:
        talk = max(0, int((ended - answered).total_seconds()))

    hangups = [leg for leg in real if leg["hangup_cause"]]
    cause = (lead and lead["hangup_cause"]) or (hangups[0]["hangup_cause"] if hangups else None) or next(
        (_str(e["payload"].get("hangup_cause")) for e in events if e["event_type"] == "call.hangup"), None)
    if answered is not None:
        outcome = "answered"
    elif "calls.voicemail.completed" in kinds or any(leg["call_sec"] for leg in voicemail_legs):
        outcome = "voicemail"
    elif direction == "inbound":
        refused = any((leg["hangup_details"] or "").endswith("refuse") for leg in real)
        outcome = "declined" if refused else "missed"
    elif direction == "outbound":
        outcome = "no_answer" if (cause or "").upper() in {"ORIGINATOR_CANCEL", "NO_ANSWER", "USER_BUSY",
                                                            "NORMAL_CLEARING"} else "failed"
    else:
        outcome = None
    if ended is None and not legs and "call.hangup" not in kinds and outcome not in ("answered", "voicemail"):
        outcome = "in_progress"

    now = sentinel.utcnow()
    row = {"call_session_id": session, "direction": direction, "line": line, "remote_number": remote,
           "remote_name": name, "forwarded_from": forwarded_from, "forward_reason": reason,
           "started_at": _iso(started), "answered_at": _iso(answered), "ended_at": _iso(ended),
           "talk_seconds": talk, "outcome": outcome, "hangup_cause": cause}
    conn.execute(
        "INSERT INTO calls(call_session_id,direction,line,remote_number,remote_name,forwarded_from,"
        "forward_reason,started_at,answered_at,ended_at,talk_seconds,outcome,hangup_cause,first_seen,updated_at) "
        "VALUES(:call_session_id,:direction,:line,:remote_number,:remote_name,:forwarded_from,:forward_reason,"
        ":started_at,:answered_at,:ended_at,:talk_seconds,:outcome,:hangup_cause,:now,:now) "
        "ON CONFLICT(call_session_id) DO UPDATE SET direction=excluded.direction,line=excluded.line,"
        "remote_number=excluded.remote_number,remote_name=excluded.remote_name,"
        "forwarded_from=excluded.forwarded_from,forward_reason=excluded.forward_reason,"
        "started_at=excluded.started_at,answered_at=excluded.answered_at,ended_at=excluded.ended_at,"
        "talk_seconds=excluded.talk_seconds,outcome=excluded.outcome,hangup_cause=excluded.hangup_cause,"
        "updated_at=excluded.updated_at", row | {"now": now})
    return row


# ----------------------------------------------------------------- voicemail --

def _voicemail_url_ok(url: str) -> bool:
    """Only Telnyx's voicemail bucket (or an operator-listed host) is fetched.

    The webhook is unsigned by the time it reaches Wax, so a URL is a claim; a
    forged event must not be able to pull an arbitrary file into the inbox.
    """
    parsed = urllib.parse.urlparse(url)
    hosts = os.environ.get("WAX_TELNYX_VOICEMAIL_HOSTS", "voice-mail-prod.s3.us-east-1.amazonaws.com")
    allowed = {h.strip().lower() for h in hosts.split(",") if h.strip()}
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in allowed


def _download(url: str, destination: Path, *, trusted: bool = False) -> Path:
    """Stage `url` under inbox/.staging and rename it into place, never over a file.

    `trusted` is for a URL the authenticated Telnyx API itself just handed back; anything
    that came in on a webhook goes through the voicemail host allowlist."""
    if not trusted and not _voicemail_url_ok(url):
        raise CallsError(f"voicemail URL host not allowed: {urllib.parse.urlparse(url).hostname}",
                         "voicemail_untrusted_url")
    staging_dir = paths.INBOX / ".staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging = staging_dir / f"telnyx-vm-{uuid.uuid4().hex}.part"
    request = urllib.request.Request(url, headers={"User-Agent": "wax-calls"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, staging.open("xb") as out:
            while chunk := response.read(1 << 16):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if staging.stat().st_size < 64:
            raise CallsError("voicemail download too small", "voicemail_empty")
        return rename.move_noclobber(staging, destination)
    except urllib.error.HTTPError as exc:
        raise CallsError(f"voicemail download: HTTP {exc.code}", "voicemail_expired" if exc.code == 403
                         else "voicemail_http_error") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CallsError(f"voicemail download: {exc}", "voicemail_unreachable") from None
    finally:
        if staging.exists():
            staging.unlink()


def voicemail_name(session: str, occurred_at: Optional[str]) -> str:
    stamp = _ts(occurred_at) or datetime.now(timezone.utc)
    return f"telnyx-vm-{stamp:%Y-%m-%d}-{session}-{int(stamp.timestamp())}.wav"


def fetch_voicemail(conn: sqlite3.Connection, event_id: str, *, fetch: bool = True) -> Optional[dict[str, Any]]:
    """Put one voicemail's WAV into the inbox exactly once (keyed by the event id)."""
    row = conn.execute("SELECT * FROM call_events WHERE event_id=?", (event_id,)).fetchone()
    if not row or row["event_type"] != "calls.voicemail.completed":
        return None
    key = f"vm:{event_id}"
    done = conn.execute("SELECT file_name FROM call_recordings WHERE recording_key=? AND file_name IS NOT NULL",
                        (key,)).fetchone()
    if done:
        return {"recording_key": key, "file_name": done["file_name"], "fetched": False}
    payload = json.loads(row["payload"])
    url, session = _str(payload.get("recording_url")), row["call_session_id"]
    if not url or not session:
        return None
    name = voicemail_name(session, row["occurred_at"])
    conn.execute(
        "INSERT INTO call_recordings(recording_key,kind,call_session_id,channels,started_at,raw,updated_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(recording_key) DO NOTHING",
        (key, "voicemail", session, "single", row["occurred_at"],
         json.dumps({k: v for k, v in payload.items() if k != "recording_url"}, sort_keys=True), sentinel.utcnow()))
    if not fetch:
        return {"recording_key": key, "file_name": None, "fetched": False}
    placed = _download(url, paths.INBOX / name)
    conn.execute("UPDATE call_recordings SET file_name=?, updated_at=? WHERE recording_key=?",
                 (placed.name, sentinel.utcnow(), key))
    log.info("calls: voicemail for %s fetched into inbox as %s", session, placed.name)
    return {"recording_key": key, "file_name": placed.name, "fetched": True}


# ----------------------------------------------------------------- entrypoints --

def ingest_event(raw: Any, *, via: str = "webhook", fetch: bool = True) -> dict[str, Any]:
    """`wax calls event`: store one Telnyx webhook event and refresh its call."""
    conn = db()
    stored = record_event(conn, raw, via=via)
    if stored is None:
        raise CallsError("not a Telnyx v2 event envelope", "not_an_event")
    result: dict[str, Any] = dict(stored)
    result["ended"] = False
    if stored["call_session_id"]:
        before = conn.execute("SELECT ended_at FROM calls WHERE call_session_id=?",
                              (stored["call_session_id"],)).fetchone()
        result["call"] = derive(conn, stored["call_session_id"])
        # `ended` is true exactly once per call: on the event that first gives it
        # an end. A notifier keys on it, so a multi-leg hangup is one notice.
        call = result["call"] or {}
        result["ended"] = bool(stored["new"] and call.get("ended_at")
                               and call.get("outcome") not in (None, "in_progress")
                               and not (before and before["ended_at"]))
    if stored["event_type"] == "calls.voicemail.completed":
        try:
            result["voicemail"] = fetch_voicemail(conn, stored["event_id"], fetch=fetch)
        except CallsError as exc:
            result["voicemail"] = {"error": exc.code, "detail": str(exc)}
    return result


def sync(since: datetime, *, key: Optional[str] = None, fetch: bool = True,
         deliveries: bool = True) -> dict[str, Any]:
    """`wax calls sync`: reconcile CDRs, recordings and events since `since`."""
    key = key or api_key()
    conn = db()
    since_iso = _iso(since)
    sessions: set[str] = set()
    counts = {"legs": 0, "recordings": 0, "events": 0, "events_new": 0, "voicemail_fetched": 0,
              "voicemail_errors": []}
    for cdr in _pages("/detail_records", {"filter[record_type]": "sip-trunking",
                                          "filter[started_at][gte]": since_iso}, key, size=50):
        session = upsert_leg(conn, cdr)
        if session:
            sessions.add(session)
            counts["legs"] += 1
    for rec in _pages("/recordings", {"filter[created_at][gte]": since_iso}, key):
        session = upsert_recording(conn, rec)
        counts["recordings"] += 1
        if session:
            sessions.add(session)
    voicemails = []
    if deliveries:
        for delivery in _pages("/webhook_deliveries", {}, key):
            hook = delivery.get("webhook") or {}
            occurred = _ts(hook.get("occurred_at"))
            if occurred is None or occurred < since:
                continue
            stored = record_event(conn, hook, via="deliveries")
            if not stored:
                continue
            counts["events"] += 1
            counts["events_new"] += stored["new"]
            if stored["call_session_id"]:
                sessions.add(stored["call_session_id"])
            if stored["event_type"] == "calls.voicemail.completed":
                voicemails.append(stored["event_id"])
    for session in sorted(sessions):
        derive(conn, session)
    for event_id in voicemails:
        try:
            got = fetch_voicemail(conn, event_id, fetch=fetch)
            counts["voicemail_fetched"] += bool(got and got.get("fetched"))
        except CallsError as exc:
            counts["voicemail_errors"].append({"event_id": event_id, "code": exc.code})
    counts["calls"] = len(sessions)
    _set_meta(conn, "calls.last_sync", json.dumps({"at": sentinel.utcnow(), "since": since_iso,
                                                  "calls": len(sessions)}))
    return counts


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))


def last_sync() -> Optional[dict[str, Any]]:
    row = db().execute("SELECT v FROM meta WHERE k='calls.last_sync'").fetchone()
    try:
        return json.loads(row["v"]) if row else None
    except ValueError:
        return None


BACKFILL_MIN_MS = 3000   # Feet-era missed calls left ~2 KB stubs of a second or two


def backfill_recordings(*, dry_run: bool = False, min_ms: int = BACKFILL_MIN_MS,
                        key: Optional[str] = None) -> dict[str, Any]:
    """`wax calls backfill`: pull native trunk recordings that never reached our bucket.

    Before custom storage was attached (2026-10-15T21:18Z) Telnyx kept recordings in its own
    bucket, so telnyx-s3-intake never saw them. Each one gets a fresh presigned link from
    GET /v2/recordings/<id> and lands in the inbox under the intake's own name shape, so the
    call-metadata pass joins it like any other. RecordVerb twins (the Feet-era second copy)
    and sub-`min_ms` stubs are left where they are. Idempotent: `file_name` marks a fetch.
    """
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM call_recordings WHERE kind='trunk' AND object_name IS NULL AND file_name IS NULL "
        "AND recording_key NOT LIKE 'vm:%' ORDER BY started_at")]
    out: dict[str, Any] = {"candidates": len(rows), "fetched": [], "skipped_stub": 0, "errors": [],
                           "dry_run": dry_run}
    for row in rows:
        if (row["duration_millis"] or 0) < min_ms:
            out["skipped_stub"] += 1
            continue
        if dry_run:
            out["fetched"].append({"recording_key": row["recording_key"], "started_at": row["started_at"]})
            continue
        try:
            key = key or api_key()
            rec = (_get(f"/recordings/{row['recording_key']}", {}, key).get("data") or {})
            url = (rec.get("download_urls") or {}).get("mp3") or ""
            if not url.startswith("https://"):
                raise CallsError(f"no https download URL for {row['recording_key']}", "recording_no_url")
            base = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
            if not re.fullmatch(r"[0-9a-f-]{36}-\d{9,13}\.mp3", base):
                raise CallsError(f"unexpected object name {base!r}", "recording_bad_name")
            day = (row["started_at"] or "")[:10] or "unknown"
            placed = _download(url, paths.INBOX / f"telnyx-{day}-{base}", trusted=True)
            conn.execute("UPDATE call_recordings SET file_name=?, updated_at=? WHERE recording_key=?",
                         (placed.name, sentinel.utcnow(), row["recording_key"]))
            out["fetched"].append({"recording_key": row["recording_key"], "file_name": placed.name})
        except CallsError as exc:
            out["errors"].append({"recording_key": row["recording_key"], "code": exc.code, "detail": str(exc)})
    return out


def health(days: int = 7) -> dict[str, Any]:
    """What `wax doctor` and `wax status` ask of the call log, from the ledger alone."""
    conn = db()
    since = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    stranded = [r["event_id"] for r in conn.execute(
        "SELECT e.event_id FROM call_events e LEFT JOIN call_recordings r "
        "ON r.recording_key = 'vm:' || e.event_id "
        "WHERE e.event_type='calls.voicemail.completed' AND e.occurred_at >= ? AND r.file_name IS NULL",
        (since,))]
    webhook = conn.execute("SELECT MAX(received_at) AS at FROM call_events WHERE via='webhook'").fetchone()
    counts = {r["outcome"] or "unknown": r["n"] for r in conn.execute(
        "SELECT outcome, COUNT(*) AS n FROM calls WHERE started_at >= ? GROUP BY outcome", (since,))}
    return {"last_sync": last_sync(), "last_webhook_at": webhook["at"] if webhook else None,
            "voicemail_unfetched": stranded, "outcomes": counts, "days": days}


# ------------------------------------------------------------ audio <-> call --

def audio_identity(name: str) -> Optional[dict[str, Any]]:
    """What a file name says about its call, without touching the ledger."""
    base = Path(name).name
    if m := TELNYX_VM.match(base):
        return {"source": "telnyx-voicemail", "call_session_id": m.group(2).lower(),
                "epoch": int(m.group(3))}
    if m := TELNYX_TRUNK.match(base):
        epoch = int(m.group(3))
        # RecordVerb names carry a microsecond epoch (16 digits) — Feet-era duplicates. A
        # few early trunk objects carry milliseconds (13 digits); those are not duplicates.
        record_verb = epoch > 10**15
        return {"source": "telnyx", "call_leg_id": m.group(2).lower(),
                "epoch": epoch // 1_000_000 if record_verb else epoch // 1000 if epoch > 10**12 else epoch,
                "record_verb": record_verb}
    if m := GROUNDWIRE.match(base):
        try:
            local = datetime.strptime(m.group(3) + m.group(4), "%y%m%d%H%M%S").astimezone()
        except ValueError:
            return None
        return {"source": "groundwire", "remote_digits": m.group(2), "contact": m.group(1),
                "started_at": _iso(local)}
    return None


def _session_for(conn: sqlite3.Connection, ident: dict[str, Any]) -> Optional[str]:
    if ident.get("call_session_id"):
        return ident["call_session_id"]
    if leg := ident.get("call_leg_id"):
        for sql in ("SELECT call_session_id FROM call_recordings WHERE call_leg_id=? AND call_session_id IS NOT NULL",
                    "SELECT call_session_id FROM call_legs WHERE call_leg_id=?",
                    "SELECT call_session_id FROM call_events WHERE call_leg_id=? AND call_session_id IS NOT NULL"):
            row = conn.execute(sql, (leg,)).fetchone()
            if row:
                return row[0]
        return None
    if ident.get("source") == "groundwire":
        started = _ts(ident["started_at"])
        digits = ident["remote_digits"][-10:]
        lo, hi = _iso(started - timedelta(seconds=GROUNDWIRE_SKEW_S)), _iso(started + timedelta(seconds=GROUNDWIRE_SKEW_S))
        # Several attempts can start within seconds of each other (redials);
        # the recording belongs to the one that was answered, nearest in time.
        best = None
        for row in conn.execute("SELECT call_session_id, cli, cld, started_at, completed FROM call_legs "
                                "WHERE started_at BETWEEN ? AND ?", (lo, hi)):
            if (row["cli"] or "").endswith(digits) or (row["cld"] or "").endswith(digits):
                rank = (0 if row["completed"] else 1, abs((_ts(row["started_at"]) - started).total_seconds()))
                if best is None or rank < best[0]:
                    best = (rank, row["call_session_id"])
        return best[1] if best else None
    return None


def _fetch_for(ident: dict[str, Any], key: Optional[str] = None) -> None:
    """Pull just enough from Telnyx to resolve one file's call."""
    key = key or api_key()
    conn = db()
    if leg := ident.get("call_leg_id"):
        for rec in _pages("/recordings", {"filter[call_leg_id]": leg}, key, size=20):
            upsert_recording(conn, rec)
    session = _session_for(conn, ident)
    if session:
        for cdr in _pages("/detail_records", {"filter[record_type]": "sip-trunking",
                                              "filter[telnyx_session_id][eq]": session}, key, size=50):
            upsert_leg(conn, cdr)
        derive(conn, session)
    elif ident.get("epoch") or ident.get("started_at"):
        around = (datetime.fromtimestamp(ident["epoch"], timezone.utc) if ident.get("epoch")
                  else _ts(ident["started_at"]))
        sync(around - timedelta(minutes=10), key=key, fetch=False, deliveries=False)


def call_for_audio(name: str, *, fetch: bool = False) -> Optional[dict[str, Any]]:
    """The `calls` row for a recording file name (fetching from Telnyx if asked)."""
    ident = audio_identity(name)
    if ident is None:
        return None
    conn = db()
    session = _session_for(conn, ident)
    if session:
        row = conn.execute("SELECT * FROM calls WHERE call_session_id=?", (session,)).fetchone()
        if row is None or (fetch and row["ended_at"] is None):
            row = None
    else:
        row = None
    if row is None and fetch:
        _fetch_for(ident)
        session = _session_for(conn, ident)
        row = conn.execute("SELECT * FROM calls WHERE call_session_id=?", (session,)).fetchone() if session else None
    if row is None:
        return None
    return dict(row) | {"source": ident["source"], "call_leg_id": ident.get("call_leg_id"),
                        "record_verb": ident.get("record_verb", False)}


def party_label(call: dict[str, Any]) -> str:
    return call.get("remote_name") or call.get("remote_number") or "Caller"


def channel_parties(audio_name: str, *, fetch: bool = True) -> Optional[tuple[str, str]]:
    """Speaker labels for channel 0 and channel 1 of a dual-channel recording.

    Telnyx dual recording puts the FIRST leg on channel A. On an outbound call
    from Groundwire that is our SIP leg (verified 2026-11-02 against a known
    voicemail greeting). On an inbound call the first leg is the PSTN caller's
    — inferred from the docs, not yet verified with a real answered inbound call.
    """
    try:
        call = call_for_audio(audio_name, fetch=fetch)
    except CallsError as exc:
        log.warning("calls: no party labels for %s (%s)", audio_name, exc.code)
        return None
    if not call or not call.get("direction"):
        return None
    other = party_label(call)
    return (ME, other) if call["direction"] == "outbound" else (other, ME)


def frontmatter_for(call: dict[str, Any]) -> dict[str, Any]:
    """The note's `call-*` keys. Unknown values are absent, never null."""
    fields = {
        "call-direction": call.get("direction"),
        "call-outcome": call.get("outcome"),
        "call-remote-number": call.get("remote_number"),
        "call-remote-name": call.get("remote_name"),
        "call-line": call.get("line"),
        "call-forwarded-from": call.get("forwarded_from"),
        "call-forward-reason": call.get("forward_reason"),
        "call-started": local_iso(call.get("started_at")),
        "call-answered": local_iso(call.get("answered_at")),
        "call-ended": local_iso(call.get("ended_at")),
        "call-talk-seconds": call.get("talk_seconds") if call.get("answered_at") else None,
        "call-session-id": call.get("call_session_id"),
        "call-leg-id": call.get("call_leg_id"),
        "call-source": call.get("source"),
    }
    return {k: v for k, v in fields.items() if v not in (None, "")}


def recent(limit: int = 50, outcome: Optional[str] = None) -> list[dict[str, Any]]:
    conn = db()
    sql = "SELECT * FROM calls"
    args: list[Any] = []
    if outcome:
        sql += " WHERE outcome=?"
        args.append(outcome)
    sql += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)
    rows = [dict(r) for r in conn.execute(sql, args)]
    for row in rows:
        files = [r["object_name"] or r["file_name"] for r in conn.execute(
            "SELECT object_name, file_name FROM call_recordings WHERE call_session_id=? AND kind!='record_verb'",
            (row["call_session_id"],)) if r["object_name"] or r["file_name"]]
        row["recordings"] = files
    return rows


def show(ref: str) -> Optional[dict[str, Any]]:
    conn = db()
    session = None
    if re.fullmatch(UUID, ref, re.I):
        ref = ref.lower()
        if conn.execute("SELECT 1 FROM calls WHERE call_session_id=?", (ref,)).fetchone():
            session = ref
        else:
            session = _session_for(conn, {"call_leg_id": ref})
    else:
        item = conn.execute("SELECT orig_name, path FROM items WHERE item_id=?", (ref,)).fetchone()
        name = (item["orig_name"] or Path(item["path"]).name) if item else ref
        ident = audio_identity(name)
        session = _session_for(conn, ident) if ident else None
    if not session:
        return None
    call = conn.execute("SELECT * FROM calls WHERE call_session_id=?", (session,)).fetchone()
    return {
        "call": dict(call) if call else None,
        "legs": [{k: v for k, v in dict(r).items() if k != "raw"} for r in conn.execute(
            "SELECT * FROM call_legs WHERE call_session_id=? ORDER BY started_at", (session,))],
        "recordings": [{k: v for k, v in dict(r).items() if k != "raw"} for r in conn.execute(
            "SELECT * FROM call_recordings WHERE call_session_id=?", (session,))],
        "events": [{"event_type": r["event_type"], "occurred_at": r["occurred_at"], "via": r["via"]}
                   for r in conn.execute("SELECT event_type, occurred_at, via FROM call_events "
                                         "WHERE call_session_id=? ORDER BY occurred_at", (session,))],
    }
