import hashlib
import json
import logging
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import archive, events, frontmatter, ledger, operations, passes, paths, sentinel

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

MAX_ENVELOPE_BYTES = 1024 * 1024 - 4096


def audio_url(bucket, key):
    endpoint = __import__("os").environ.get("WAX_S3_ENDPOINT", "").rstrip("/")
    if not endpoint:
        result = archive._mc_json(["alias", "list", archive.ALIAS, "--json"])
        endpoint = str((result or {}).get("URL") or (result or {}).get("url") or "").rstrip("/")
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    return endpoint + "/" + quote(bucket, safe="") + "/" + quote(key, safe="/")


def pass_event_id(item_id, slug, event_type, key):
    """Content-keyed id of a pass-declared event.

    Deliberately blind to plan_id and attempt: a re-plan (new completion), a
    re-run of the pass, and a manual backfill all describe the SAME ticket, and
    the only thing allowed to make an event "new" is the pass choosing a new key.
    """
    return str(uuid.uuid5(events.WAX_NS, f"ep-event:{item_id}:{slug}:{event_type}:{key}"))


def declared_events(result_json):
    """The well-formed `events` of one recorded pass result, in the pass's order.

    passes._validated_events already refused malformed lists before they could
    be recorded; this is the second line, for a ledger edited by hand or written
    by an older runner. Anything that is not a {type,key,data} object is ignored
    rather than raised, because it is called on the path that parks audio.
    """
    try:
        result = json.loads(result_json or "{}")
    except (TypeError, ValueError):
        return []
    declared = result.get("events") if isinstance(result, dict) else None
    if not isinstance(declared, list):
        return []
    return [e for e in declared if isinstance(e, dict)
            and isinstance(e.get("type"), str) and isinstance(e.get("key"), str)
            and isinstance(e.get("data"), dict)]


def _plan_order(definitions):
    try:
        return passes.ordered(definitions)
    except Exception:  # noqa: BLE001 - ordering is cosmetic here; never block a completion on it
        return sorted(definitions)


def pending_pass_events(conn, item_id, definitions):
    """(slug, event_id, event, command_id) for completed-pass events not yet enqueued.

    Ordered by plan order, then slug, and within a pass by the order the pass
    listed them -- the order they drain in, so ticket 1 is announced before
    ticket 2 and a downstream consumer sees them as the pass numbered them.
    A pass outside the plan (an item completed before the pass existed, then
    backfilled with `wax ep run`) sorts after the planned ones.
    """
    position = {slug: index for index, slug in enumerate(_plan_order(definitions))}
    done = {row["event_id"] for row in conn.execute(
        "SELECT event_id FROM pass_events WHERE item_id=?", (item_id,))}
    rows = conn.execute("SELECT ep_slug, command_id, result FROM passes WHERE item_id=? AND state='completed'",
                        (item_id,)).fetchall()
    pending, seen = [], set()
    for row in sorted(rows, key=lambda r: (position.get(r["ep_slug"], len(position)), r["ep_slug"])):
        for event in declared_events(row["result"]):
            event_id = pass_event_id(item_id, row["ep_slug"], event["type"], event["key"])
            if event_id in done or event_id in seen:
                continue
            seen.add(event_id)
            pending.append((row["ep_slug"], event_id, event, row["command_id"]))
    return pending


def _build_pass_events(pending, item_id, md, completion_id):
    """Envelopes for `pending` as (slug, event_id, command_id, type, subject, encoded) rows.

    Runs BEFORE any transaction and never raises: this is reached from the path
    that parks audio, and an exception here would strand an item whose audio has
    already left the inbox. A pass event that cannot be built or fits no
    envelope is dropped with a warning and counted, never allowed to cost the
    transcript its completion. Returns (rows, dropped).
    """
    rows, dropped = [], 0
    for slug, event_id, event, command_id in pending:
        try:
            entity, action = event["type"].split(".")
            # The runner owns identity and location. They are stamped LAST so a
            # pass cannot point a ticket at a different item or a stale filename;
            # `transcript` is the basename as it exists NOW, after title-slug's rename.
            data = {**event["data"], "item_id": item_id, "transcription_id": item_id,
                    "transcript": md.name, "transcript_uri": md.resolve().as_uri()}
            subject, envelope = events.envelope(
                entity, action, data, event_id=event_id,
                correlationid=completion_id, causationid=completion_id,
                ordering_key=f"transcription:{item_id}")
            encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
            if len(encoded.encode()) > MAX_ENVELOPE_BYTES:
                raise ValueError("envelope exceeds transport policy")
        except Exception as exc:  # noqa: BLE001 - see docstring
            dropped += 1
            log.warning("dropping %s event %s from pass %s for %s: %s: %s",
                        event.get("type"), event.get("key"), slug, item_id, type(exc).__name__, exc)
            continue
        rows.append((slug, event_id, command_id, event["type"], subject, encoded))
    return rows, dropped


def _prepare_pass_events(conn, item_id, definitions, md, completion_id):
    """pending -> envelopes for a NEW completion; (rows, dropped), never raises.

    A failure here defers the tickets, it does not lose them: they stay absent
    from pass_events, so the next finalize() backfills them against this
    completion. That is strictly better than raising after the audio is parked.
    """
    try:
        return _build_pass_events(pending_pass_events(conn, item_id, definitions),
                                  item_id, md, completion_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("pass events for %s deferred: %s: %s", item_id, type(exc).__name__, exc)
        return [], 0


def _enqueue_pass_events(conn, item_id, rows):
    """Insert outbox + pass_events rows inside the CALLER's open transaction."""
    inserted = 0
    for slug, event_id, command_id, event_type, subject, encoded in rows:
        if conn.execute("SELECT 1 FROM pass_events WHERE event_id=?", (event_id,)).fetchone():
            continue
        cursor = conn.execute("INSERT INTO outbox(subject,envelope,created_at) VALUES(?,?,?)",
                              (subject, encoded, sentinel.utcnow()))
        conn.execute("INSERT INTO pass_events(event_id,item_id,ep_slug,command_id,type,outbox_id,created_at) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (event_id, item_id, slug, command_id, event_type, cursor.lastrowid, sentinel.utcnow()))
        inserted += 1
    return inserted


def _backfill_pass_events(conn, item_id, plan, prior):
    """Enqueue events from passes that completed AFTER the item was finalized.

    A completed item keeps its stored plan, so a pass added later (or a manual
    `wax ep run`) never changes plan_id and finalize() used to answer "unchanged"
    without looking. Its tickets are correlated to the completion that already
    went out. Never raises: the item is already complete, nothing is lost by
    trying again on the next finalize(), and this must not break the caller.
    """
    try:
        definitions = json.loads(plan["definitions"])
        pending = pending_pass_events(conn, item_id, definitions)
        if not pending:
            return {"pass_events": 0}
        transcript = conn.execute("SELECT md_path FROM transcripts WHERE item_id=?", (item_id,)).fetchone()
        md = Path(transcript["md_path"]) if transcript else None
        if md is None or not md.is_file():
            return {"pass_events": 0, "pass_events_reason": "missing_transcript",
                    "pass_events_pending": len(pending)}
        rows, dropped = _build_pass_events(pending, item_id, md, prior["event_id"])
        inserted = 0
        if rows:
            conn.execute("BEGIN IMMEDIATE")
            try:
                inserted = _enqueue_pass_events(conn, item_id, rows)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {"pass_events": inserted, **({"pass_events_dropped": dropped} if dropped else {})}
    except Exception as exc:  # noqa: BLE001
        log.warning("pass events for %s not enqueued: %s: %s", item_id, type(exc).__name__, exc)
        return {"pass_events": 0, "pass_events_reason": type(exc).__name__}


def finalize(item_id):
    with operations.item_lock(item_id):
        conn = ledger.connect()
        events._ensure()
        plan = conn.execute("SELECT * FROM processing_plans WHERE item_id=?", (item_id,)).fetchone()
        if not plan:
            return {"finalized": False, "reason_code": "missing_plan"}
        prior = conn.execute("SELECT * FROM completions WHERE item_id=? AND plan_id=?",
                             (item_id, plan["plan_id"])).fetchone()
        if prior:
            return {"finalized": True, "unchanged": True, "event_id": prior["event_id"],
                    **_backfill_pass_events(conn, item_id, plan, prior)}
        item = conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
        transcript = conn.execute("SELECT * FROM transcripts WHERE item_id=?", (item_id,)).fetchone()
        if not item or not transcript:
            return {"finalized": False, "reason_code": "missing_artifacts"}
        audio = Path(item["path"])
        try:
            audio.resolve().relative_to(paths.ARCHIVE.resolve())
        except ValueError:
            return {"finalized": False, "reason_code": "audio_not_parked"}
        if not audio.is_file():
            return {"finalized": False, "reason_code": "missing_audio"}
        definitions = json.loads(plan["definitions"])
        results = {row["ep_slug"]: dict(row) for row in conn.execute(
            "SELECT * FROM passes WHERE item_id=?", (item_id,)).fetchall()}
        for slug, ep in definitions.items():
            result = results.get(slug)
            if (not result or result["state"] not in ("completed", "skipped")
                    or result["version"] != int(ep.get("version") or 1)
                    or result.get("definition_id") != operations.definition_id(ep)):
                return {"finalized": False, "reason_code": "passes_incomplete", "ep_slug": slug}
        refs = [ref for ref in archive.references(item_id) if ref["verified_at"]]
        if not refs:
            return {"finalized": False, "reason_code": "unverified_archive"}
        md = Path(transcript["md_path"])
        if not md.is_file():
            return {"finalized": False, "reason_code": "missing_transcript"}
        raw = md.read_bytes()
        fm, body = frontmatter.split(raw.decode("utf-8"))
        if fm.get(frontmatter.ITEM_KEY) != item_id:
            return {"finalized": False, "reason_code": "artifact_identity_mismatch"}
        metadata = json.loads(json.dumps(fm, default=str, ensure_ascii=False))
        pass_results = {slug: {
            "state": result["state"], "version": result["version"],
            "reason_code": result["reason_code"], "result": json.loads(result["result"] or "{}"),
        } for slug, result in results.items() if slug in definitions or result["state"] in ("completed", "skipped")}
        for result in pass_results.values():
            result["result"].pop("body_replace", None)
            # Declared events are published as their own envelopes after this
            # one. Left inline they would travel twice and bloat the completion.
            result["result"].pop("events", None)
        primary = refs[0]
        s3_uri = f"s3://{primary['bucket']}/{primary['s3_key']}"
        data = {
            "item_id": item_id, "transcription_id": item_id,
            "file_path": str(audio), "md_path": str(md), "transcript_uri": md.resolve().as_uri(),
            "transcript_text": body, "transcript_inline": True,
            "transcript_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "transcript_bytes": len(body.encode()), "finalized": True, "plan_id": plan["plan_id"],
            "title": fm.get("title"), "summary": fm.get("summary"),
            "title_slug": fm.get("title-slug"), "classification": fm.get("classification"),
            "duration_seconds": transcript["audio_duration"],
            "processing_seconds": transcript["processing_seconds"],
            "word_count": transcript["word_count"], "engine_model": transcript["engine_model"],
            "engine": fm.get("transcription-backend"), "language_detected": fm.get("language"),
            "s3_uri": s3_uri, "s3_url": audio_url(primary["bucket"], primary["s3_key"]),
            "archive_references": refs, "metadata": metadata, "pass_results": pass_results,
        }
        event_id = str(uuid.uuid5(events.WAX_NS, f"completion:{item_id}:{plan['plan_id']}"))
        subject, envelope = events.envelope("transcription", "completed", data,
                                            event_id=event_id,
                                            ordering_key=f"transcription:{item_id}")
        encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode()) > MAX_ENVELOPE_BYTES:
            envelope["data"].update(transcript_text="", transcript_inline=False,
                                     copy_warning="Full transcript exceeds event transport policy; use canonical URI.")
            encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode()) > MAX_ENVELOPE_BYTES:
            return {"finalized": False, "reason_code": "metadata_exceeds_transport_policy"}
        # Everything that can fail for a reason of its own is built before the
        # transaction, so the only things that can roll a completion back are
        # the ledger writes themselves.
        pass_rows, dropped = _prepare_pass_events(conn, item_id, definitions, md, event_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute("INSERT INTO outbox(subject,envelope,created_at) VALUES(?,?,?)",
                                  (subject, encoded, sentinel.utcnow()))
            # Right after the completion row: the outbox drains in id order, so
            # consumers see `completed` first and then the events that hang off it.
            inserted = _enqueue_pass_events(conn, item_id, pass_rows)
            conn.execute("INSERT INTO completions VALUES(?,?,?,?,?,?)",
                         (item_id, plan["plan_id"], event_id, cursor.lastrowid,
                          hashlib.sha256(raw).hexdigest(), sentinel.utcnow()))
            ledger.set_item_state(item_id, "complete", cause="finalized", evidence=event_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"finalized": True, "event_id": event_id, "outbox_id": cursor.lastrowid,
                "pass_events": inserted, **({"pass_events_dropped": dropped} if dropped else {})}
