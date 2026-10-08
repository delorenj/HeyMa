import hashlib
import json
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import archive, events, frontmatter, ledger, operations, paths, sentinel

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
            return {"finalized": True, "unchanged": True, "event_id": prior["event_id"]}
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
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute("INSERT INTO outbox(subject,envelope,created_at) VALUES(?,?,?)",
                                  (subject, encoded, sentinel.utcnow()))
            conn.execute("INSERT INTO completions VALUES(?,?,?,?,?,?)",
                         (item_id, plan["plan_id"], event_id, cursor.lastrowid,
                          hashlib.sha256(raw).hexdigest(), sentinel.utcnow()))
            ledger.set_item_state(item_id, "complete", cause="finalized", evidence=event_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"finalized": True, "event_id": event_id, "outbox_id": cursor.lastrowid}
