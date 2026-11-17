"""Who a phone number is: the contact directory every call label reads.

A call's far party used to be named only by CNAM (`calls.person_name`), which
most mobiles never send, so a call from the operator's wife was transcribed as
`**+1908... (00:02):**`. The phone's own address book never reaches this box
and Telnyx keeps no contacts, so the canonical source is 1Password: every
**Identity** item in vault `DeLoSecrets` (`WAX_CONTACTS_VAULT`) with a phone
field is a contact. The label is the item's `call name` / `nickname` field when
it has one, else its first name, else its title. Add a person to the directory
by giving their Identity item a phone number; nothing else changes.

Precedence for a call's name: this directory, then CNAM, then a CNAM name the
same number carried on an earlier call, then the number itself. The directory
is applied where a name is shown (`calls.remote_name`), never stored in
`calls.remote_name`, so a deleted or renamed contact never lingers.

The directory is read in one `op item list | op item get -` round-trip (well
under a second) and kept in `var/wax.db` (`contacts`) as the last good read:
a 1Password outage keeps the old names instead of reverting calls to numbers.
It is refreshed when older than `WAX_CONTACTS_TTL_S` (15 min) by whichever
reader can afford the round-trip (`wax calls sync` on its Candybar beat, the
call-metadata pass, channel labelling at transcription); `wax contacts sync`
forces it. The Telephony page and `wax calls relabel` read the cache only. No
lookup ever raises: a broken directory costs a name, never a call.

Privacy: the vault's Identity items include the family's. A contact name
reaches the operator's private surfaces (the call notes, the transcripts store,
the Telephony page), the operator's own ntfy line on topic `telephony` and the
bloodbank call.ended event's `data.remote_name` (`call.contact_name` from `wax
calls event`, the operator's choice 2026-11-17). It is never in the stored call
row, so a removed contact stops appearing at once. Logs carry counts only, and `wax contacts list` masks
numbers to their last four digits. No 1Password secret or credential is ever
read: only Identity name and phone fields.

`WAX_CONTACTS=off` disables the directory (the test suite pins this).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from . import ledger, sentinel

log = logging.getLogger("wax.contacts")

# What WAX_CONTACTS means when it is unset; tests/conftest.py sets this to "off" so a
# test that clears os.environ still never reads the real vault.
DEFAULT_SOURCE = "1password"
VAULT = os.environ.get("WAX_CONTACTS_VAULT", "DeLoSecrets")
CATEGORY = "Identity"
OP_TIMEOUT_S = 30.0
SOURCE = "1password"
META_REFRESHED = "contacts.refreshed_at"
META_ERROR = "contacts.error"
META_STATS = "contacts.stats"
META_ERROR_AT = "contacts.error_at"
# After a failed read, wait this long before asking 1Password again (an outage must not cost every lookup 60 s).
RETRY_S = 300.0

# Fields whose value names the person on a call, most specific first.
NAME_LABELS = ("call name", "caller name", "nickname", "display name")
FIRST_NAME_IDS = ("firstname",)
FIRST_NAME_LABELS = ("first name",)
# 1Password's built-in Identity phone fields; any field of type PHONE counts too.
PHONE_IDS = ("defphone", "homephone", "cellphone", "busphone")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    number     TEXT PRIMARY KEY,   -- E.164
    name       TEXT NOT NULL,      -- the label a call shows
    source     TEXT NOT NULL,      -- 1password
    ref        TEXT,               -- the 1Password item id (never its content)
    updated_at TEXT NOT NULL
);
"""


def enabled() -> bool:
    return os.environ.get("WAX_CONTACTS", DEFAULT_SOURCE).strip().lower() not in ("", "0", "off", "false", "no")


def ttl_s() -> float:
    try:
        return max(0.0, float(os.environ.get("WAX_CONTACTS_TTL_S", "900")))
    except ValueError:
        return 900.0


# Connections whose schema is in place. sqlite3.Connection takes no attributes, and
# executescript() COMMITs an open transaction, so it must run once per connection, not per call.
# Keyed by id(); holding the connection keeps its id from being reused. Takes no weakrefs either.
_READY: dict[int, sqlite3.Connection] = {}


def db() -> sqlite3.Connection:
    conn = ledger.connect()
    if _READY.get(id(conn)) is not conn:
        if conn.in_transaction:
            # Never let executescript() commit someone else's transaction.
            conn.execute("CREATE TABLE IF NOT EXISTS contacts (number TEXT PRIMARY KEY, name TEXT NOT NULL, "
                         "source TEXT NOT NULL, ref TEXT, updated_at TEXT NOT NULL)")
        else:
            conn.executescript(SCHEMA)
        _READY[id(conn)] = conn
    return conn


# ------------------------------------------------------------------ numbers --

def normalize(raw: Any) -> Optional[str]:
    """Any written phone number -> E.164 (`+19085551234`); North American by default. None if not a number."""
    if raw is None:
        return None
    text = str(raw).strip()
    # A SIP/tel URI: the user part is the number (`sip:+1908...@host;user=phone`).
    text = re.sub(r"^<?(?:sips?|tel):", "", text, flags=re.I).split("@")[0].split(";")[0]
    if not text or re.search(r"[A-Za-z]", text):
        return None
    plus = text.lstrip().startswith("+")
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10 and not plus:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if plus and 8 <= len(digits) <= 15:
        return "+" + digits
    return None


def mask(number: str) -> str:
    return f"…{number[-4:]}" if number and len(number) >= 4 else "…"


# ------------------------------------------------------------- 1Password --

def _op(args: list[str], stdin: Optional[str] = None) -> str:
    # op's stderr is never passed through: a future op version could echo item content.
    try:
        proc = subprocess.run(["op", *args], input=stdin, capture_output=True, text=True, timeout=OP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"op {args[0]} {args[1]} did not answer within {OP_TIMEOUT_S:g}s") from None
    except OSError as exc:
        raise RuntimeError(f"op could not run: {exc.strerror or exc}") from None
    if proc.returncode:
        raise RuntimeError(f"op {args[0]} {args[1]} exited {proc.returncode}")
    return proc.stdout


def _json_stream(text: str) -> list[Any]:
    """`op item get -` prints one JSON document per item, back to back."""
    decoder, out, i = json.JSONDecoder(), [], 0
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return out
        value, i = decoder.raw_decode(text, i)
        out.extend(value if isinstance(value, list) else [value])


def fetch_items() -> list[dict[str, Any]]:
    """Every Identity item in the vault, with fields (one list call, one bulk get)."""
    listed = _op(["item", "list", "--vault", VAULT, "--categories", CATEGORY, "--format", "json"])
    if not json.loads(listed or "[]"):
        return []
    return [i for i in _json_stream(_op(["item", "get", "-", "--vault", VAULT, "--format", "json"], stdin=listed))
            if isinstance(i, dict)]


def _field(item: dict[str, Any], *, ids: Iterable[str] = (), labels: Iterable[str] = ()) -> Optional[str]:
    ids, labels = set(ids), {label.casefold() for label in labels}
    for f in item.get("fields") or []:
        if not isinstance(f, dict):
            continue
        if f.get("id") in ids or str(f.get("label") or "").strip().casefold() in labels:
            value = str(f.get("value") or "").strip()
            if value:
                return value
    return None


def label_for(item: dict[str, Any]) -> Optional[str]:
    """The name a call shows for this person."""
    name = (_field(item, labels=NAME_LABELS)
            or _field(item, ids=FIRST_NAME_IDS, labels=FIRST_NAME_LABELS)
            or str(item.get("title") or "").strip())
    return name or None


def numbers_of(item: dict[str, Any]) -> set[str]:
    out = set()
    for f in item.get("fields") or []:
        if isinstance(f, dict) and (f.get("type") == "PHONE" or f.get("id") in PHONE_IDS):
            number = normalize(f.get("value"))
            if number:
                out.add(number)
    return out


def directory(items: Iterable[dict[str, Any]], *, own: Iterable[str] = ()) -> tuple[dict[str, tuple[str, str]], dict]:
    """{number: (name, item id)} from 1Password items, and counts.

    An item holding one of our own numbers (`own`: the Telnyx line and the cell) is the
    operator, whose side of a call is already `Me`: skipped. A number two people share
    with different names is ambiguous: neither name is used.
    """
    own = {n for n in (normalize(x) for x in own) if n}
    found: dict[str, tuple[str, str]] = {}
    clash: set[str] = set()
    stats = {"items": 0, "with_phone": 0, "owner_items": 0, "ambiguous": 0, "numbers": 0}
    items = list(items)
    # The operator can have more than one item (an old phone, a device identity): any item named
    # like the one that holds our own numbers is the operator too.
    owner_names = {(label_for(i) or "").casefold() for i in items if numbers_of(i) & own} - {""}
    for item in items:
        stats["items"] += 1
        numbers = numbers_of(item)
        if not numbers:
            continue
        stats["with_phone"] += 1
        name = label_for(item)
        if numbers & own or (name or "").casefold() in owner_names:
            stats["owner_items"] += 1
            continue
        if not name:
            continue
        for number in numbers:
            if number in found and found[number][0].casefold() != name.casefold():
                clash.add(number)
            found.setdefault(number, (name, str(item.get("id") or "")))
    for number in clash:
        found.pop(number, None)
    stats["ambiguous"], stats["numbers"] = len(clash), len(found)
    return found, stats


# ----------------------------------------------------------------- refresh --

def _age_s(stamp: Optional[str]) -> Optional[float]:
    if not stamp:
        return None
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds()


def refresh(*, force: bool = False, fetch=None) -> dict[str, Any]:
    """Re-read the directory from 1Password when stale (or `force`). Never raises.

    Returns {"state": fresh|refreshed|error|off, "changed": [numbers whose name changed], ...}.
    """
    if not enabled():
        return {"state": "off", "changed": []}
    try:
        return _refresh(force=force, fetch=fetch)
    except Exception as exc:  # noqa: BLE001 - the directory must never cost a call (or calls sync) anything
        log.warning("contacts: refresh failed (%s)", type(exc).__name__)
        return {"state": "error", "detail": f"{type(exc).__name__}: {exc}", "changed": []}


def _refresh(*, force: bool, fetch) -> dict[str, Any]:
    conn = db()
    if conn.in_transaction:
        # A caller inside a ledger transaction reads the cache; the next reader outside one refreshes.
        return {"state": "deferred", "changed": []}
    age = _age_s(ledger.meta_get(META_REFRESHED))
    if not force and age is not None and age < ttl_s():
        return {"state": "fresh", "changed": [], "age_s": int(age)}
    failed = _age_s(ledger.meta_get(META_ERROR_AT))
    if not force and failed is not None and failed < RETRY_S:
        return {"state": "backoff", "changed": [], "detail": ledger.meta_get(META_ERROR)}
    from . import calls  # the owner's own numbers live there; imported late (calls imports this module)
    try:
        found, stats = directory((fetch or fetch_items)(), own=(calls.LINE, calls.CELL))
    except Exception as exc:  # noqa: BLE001 - the directory must never cost a call its processing
        detail = f"{type(exc).__name__}: {exc}"
        ledger.meta_set(META_ERROR, detail)
        ledger.meta_set(META_ERROR_AT, sentinel.utcnow())
        log.warning("contacts: 1Password read failed, keeping the last good directory (%s)", detail)
        return {"state": "error", "detail": detail, "changed": []}
    now = sentinel.utcnow()
    before = {r["number"]: r["name"] for r in conn.execute("SELECT number, name FROM contacts WHERE source=?",
                                                            (SOURCE,))}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM contacts WHERE source=?", (SOURCE,))
        conn.executemany("INSERT INTO contacts(number,name,source,ref,updated_at) VALUES(?,?,?,?,?)",
                         [(n, name, SOURCE, ref, now) for n, (name, ref) in found.items()])
        conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (META_REFRESHED, now))
        conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (META_STATS, json.dumps(stats)))
        conn.execute("DELETE FROM meta WHERE k IN (?,?)", (META_ERROR, META_ERROR_AT))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    changed = sorted(n for n in set(before) | set(found)
                     if before.get(n) != (found[n][0] if n in found else None))
    log.info("contacts: %d number(s) from %d item(s); %d changed", len(found), stats["items"], len(changed))
    return {"state": "refreshed", "changed": changed, **stats}


def name_for(number: Any, *, refresh_stale: bool = True) -> Optional[str]:
    """The contact name for a phone number, or None. Never raises."""
    if not enabled():
        return None
    e164 = normalize(number)
    if not e164:
        return None
    try:
        if refresh_stale:
            refresh()
        row = db().execute("SELECT name FROM contacts WHERE number=?", (e164,)).fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("contacts: lookup failed (%s: %s)", type(exc).__name__, exc)
        return None
    return row["name"] if row else None


def status() -> dict[str, Any]:
    conn = db()
    count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    at = ledger.meta_get(META_REFRESHED)
    try:
        stats = json.loads(ledger.meta_get(META_STATS) or "{}")
    except ValueError:
        stats = {}
    return {"enabled": enabled(), "vault": VAULT, "numbers": count, "refreshed_at": at,
            "age_s": None if _age_s(at) is None else int(_age_s(at)),
            "error": ledger.meta_get(META_ERROR), "stats": stats}


def listing() -> list[dict[str, Any]]:
    """The directory with numbers masked to their last four digits."""
    return [{"name": r["name"], "number": mask(r["number"]), "source": r["source"]}
            for r in db().execute("SELECT * FROM contacts ORDER BY name COLLATE NOCASE, number")]
