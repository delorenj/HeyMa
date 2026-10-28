"""Note -> store rows (design B.0 / B.3). PURE: no I/O, no psycopg, no ledger access.

`from_note()` takes the exact note text plus whatever facts the caller already
holds (a ledger snapshot, an asr.json summary, a `.meta.json` sidecar) and
returns every column the projector writes: identity columns (coalesced by the
B.3 source order), version columns, child rows, the four content hashes and the
body statistics. The caller owns reading files and the database.

Invariants enforced here:
  * `frontmatter_block + body == note_text`, using the exact split Wax uses
    (`frontmatter.split`), so every version is restorable byte-for-byte;
  * `body_sha256` is Wax's own definition (`sha256(body.encode())`);
  * `meta_sha256` ignores `wax.passes`, so pass timestamps alone never look
    like a content change;
  * an invalid `classification` fails extraction (mirrors passes.py and the
    CHECK constraint) instead of being silently dropped.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Optional

from .. import frontmatter
from . import chunking

CLASSIFICATIONS = ("monolog", "meeting", "other")
ITEM_ID_RE = re.compile(r"^[0-9a-f]{16}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
UTC = dt.timezone.utc


class ExtractError(ValueError):
    """The note cannot become a version (e.g. an invalid classification)."""


# ---------------------------------------------------------------- helpers --

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_note(text: str) -> tuple[dict[str, Any], str, str]:
    """(frontmatter, frontmatter_block, body) with block + body == text, exactly."""
    fm, body = frontmatter.split(text)
    if not text.endswith(body):
        raise ExtractError("frontmatter split did not yield a suffix body")
    block = text[:len(text) - len(body)]
    if block + body != text:
        raise ExtractError("frontmatter_block + body != note text")
    return fm, block, body


def to_jsonable(value: Any) -> Any:
    """YAML-parsed frontmatter -> JSON-safe (datetimes/dates as ISO strings)."""
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _text(value: Any) -> Optional[str]:
    if _empty(value):
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return canonical_json(value)
    text = str(value).strip()
    return text or None


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or _empty(value):
        return None
    if isinstance(value, int):
        return value            # exact: st_mtime_ns does not survive a float round-trip
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number) or number != int(number):
        return None
    return int(number)


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or _empty(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no"):
        return value.strip().lower() in ("true", "yes")
    return None


def _uuid(value: Any) -> Optional[str]:
    if _empty(value):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _sha(value: Any) -> Optional[str]:
    text = _text(value)
    return text.lower() if text and SHA_RE.match(text.lower()) else None


def _list(value: Any) -> list[str]:
    """list -> as is (stringified); '' -> []; any other string -> [s]; None -> []."""
    if _empty(value):
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if not _empty(v) and str(v).strip()]
    text = _text(value)
    return [text] if text else []


def _first(*values: Any) -> Any:
    for value in values:
        if not _empty(value):
            return value
    return None


def parse_ts(value: Any, *, tz: Optional[dt.tzinfo] = None) -> tuple[Optional[dt.datetime], Optional[str]]:
    """(aware datetime, 'datetime'|'date') or (None, None).

    Naive timestamps are UTC unless `tz` is given (filename / legacy wall clock).
    A bare date becomes midnight with precision 'date'.
    """
    zone = tz or UTC
    if _empty(value) or isinstance(value, bool):
        return None, None
    if isinstance(value, dt.datetime):
        return (value if value.tzinfo else value.replace(tzinfo=zone)), "datetime"
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=zone), "date"
    if not isinstance(value, str):
        return None, None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            day = dt.date.fromisoformat(text)
        except ValueError:
            return None, None
        return dt.datetime(day.year, day.month, day.day, tzinfo=zone), "date"
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        stamp = dt.datetime.fromisoformat(candidate)
    except ValueError:
        return None, None
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=zone)), "datetime"


# filename patterns, in the B.3 order; the first that parses wins
_RECORDED = [
    (re.compile(r"(?<!\d)(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})(?!\d)"), "datetime"),
    # YYYYMMDD-HHMMSS (clip_N__..., ...-rec); the space form is the m4a recorder's (G)
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[- ](\d{2})(\d{2})(\d{2})(?!\d)"), "datetime"),
    (re.compile(r"recording_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?!\d)"), "datetime"),
    (re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2}) (\d{2})\.(\d{2})\.(\d{2})(?!\d)"), "datetime"),
    (re.compile(r"telnyx-(\d{4})-(\d{2})-(\d{2})(?!\d)"), "date"),
]


def parse_recorded_at(name: Optional[str], tz: Optional[dt.tzinfo] = None) -> tuple[Optional[dt.datetime], Optional[str]]:
    """Recording time from the audio NAME (wall clock in `tz`), or (None, None)."""
    if not name:
        return None, None
    zone = tz or _default_tz()
    for pattern, precision in _RECORDED:
        match = pattern.search(name)
        if not match:
            continue
        parts = [int(g) for g in match.groups()]
        try:
            if precision == "date":
                stamp = dt.datetime(parts[0], parts[1], parts[2], tzinfo=zone)
            else:
                stamp = dt.datetime(*parts[:6], tzinfo=zone)
        except ValueError:
            continue
        return stamp, precision
    return None, None


def _default_tz() -> dt.tzinfo:
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(os.environ.get("WAX_TRANSCRIPTS_LOCAL_TZ") or "America/New_York")
    except Exception:  # noqa: BLE001 - a bad zone name must not fail extraction
        return ZoneInfo("America/New_York")


def parse_legacy_duration(value: Optional[str]) -> Optional[float]:
    """'172m 44s' -> 10364.0, '10.4s' -> 10.4, '1h 2m 3s' -> 3723.0."""
    if not value:
        return None
    match = re.fullmatch(r"\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?\s*", value)
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)


def parse_legacy_model(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'large-v3 (faster-whisper)' -> ('large-v3', 'faster-whisper')."""
    if not value:
        return None, None
    match = re.fullmatch(r"\s*(.*?)\s*\(([^()]*)\)\s*", value)
    if match:
        return (match.group(1) or None), (match.group(2).strip() or None)
    return value.strip() or None, None


def parse_fell_through(value: Any) -> Optional[list[dict[str, str]]]:
    """'m1=r1; m2=r2' -> [{model, reason_code}, ...]; model names may contain '/' or '='."""
    if _empty(value):
        return None
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict) and item.get("model"):
                out.append({"model": str(item.get("model")), "reason_code": str(item.get("reason_code") or "")})
        return out or None
    out = []
    for part in str(value).split("; "):
        part = part.strip()
        if not part:
            continue
        model, sep, reason = part.rpartition("=")
        if not sep:
            model, reason = part, ""
        out.append({"model": model, "reason_code": reason})
    return out or None


def audio_source_hint(name: Optional[str], original: Optional[str] = None) -> Optional[str]:
    low = (name or "").lower()
    path = (original or "").lower()
    if not low and not path:
        return None
    if "otter_ai" in low or "otter.ai" in low:
        return "otter"
    if low.startswith("telnyx-"):
        return "telnyx"
    if re.search(r"(^|[_-])clip_\d+", low):
        return "clip"
    if low.startswith("hermes-") or "hermes" in path:
        return "hermes"
    if re.search(r"-(rec|segs)\.[a-z0-9]+$", low):
        return "rec"
    return None


def _strip_body_replace(evidence: Any) -> Any:
    if isinstance(evidence, dict):
        return {k: _strip_body_replace(v) for k, v in evidence.items() if k != "body_replace"}
    if isinstance(evidence, list):
        return [_strip_body_replace(v) for v in evidence]
    return evidence


def clean_evidence(evidence: Any) -> Optional[Any]:
    if _empty(evidence):
        return None
    return to_jsonable(_strip_body_replace(evidence))


# ---------------------------------------------------------------- columns --

# frontmatter key -> (version column, kind)
VERSION_FIELDS: dict[str, tuple[str, str]] = {
    "schema-version": ("schema_version", "int"),
    "asset-kind": ("asset_kind", "text"),
    "specialist": ("specialist", "text"),
    "source": ("source", "text"),
    "title": ("title", "text"),
    "title-slug": ("title_slug", "text"),
    "summary": ("summary", "text"),
    "diarized": ("diarized", "bool"),
    "diarization-requested": ("diarization_requested", "bool"),
    "diarization-device-requested": ("diarization_device_requested", "text"),
    "diarization-device": ("diarization_device", "text"),
    "duration-seconds": ("stamped_duration_s", "num"),
    "word-count": ("stamped_word_count", "int"),
    "language": ("fm_language", "text"),
    "engine-model": ("fm_engine_model", "text"),
    "transcription-backend": ("fm_transcription_backend", "text"),
    "audio-duration-seconds": ("fm_audio_duration_s", "num"),
    "transcribed-at": ("fm_transcribed_at", "ts"),
    "captured": ("fm_captured_at", "ts"),
    "source-audio": ("fm_source_audio", "text"),
    "source-sha256": ("fm_source_sha256", "text"),
    "source-s3-key": ("fm_source_s3_key", "text"),
    "source-s3-uri": ("fm_source_s3_uri", "text"),
    "vault-id": ("vault_id", "text"),
    "content-role": ("content_role", "text"),
    "metadata-quality": ("metadata_quality", "text"),
    "pillar": ("pillar", "text"),
    "triage-release": ("triage_release", "text"),
    "wiki-candidate": ("wiki_candidate", "text"),
    "wiki-action": ("wiki_action", "text"),
    "decision-id": ("decision_id", "text"),
    "domain": ("domain", "text"),
    "subdomain": ("subdomain", "text"),
    "category": ("category", "text"),
    "subcategory": ("subcategory", "text"),
    "route-path": ("route_path", "text"),
    "route-confidence": ("route_confidence", "text"),
    "route-reason": ("route_reason", "text"),
    "hitl-reason": ("hitl_reason", "text"),
    "description": ("description", "text"),
    "modified": ("modified_at", "ts"),
    "last_processed": ("last_processed_at", "ts"),
    "triage-pass": ("triage_pass", "date"),
    "last_triage_pass": ("last_triage_pass", "text"),
}
LIST_FIELDS = {"tags": "tags", "labels": "labels", "project-ids": "project_ids", "projects": "projects",
               "obsidian-note-status": "obsidian_note_status", "pipeline-status": "pipeline_status"}
# keys represented elsewhere (identity, pass state, children), so not in vault_extra
HANDLED_KEYS = set(VERSION_FIELDS) | set(LIST_FIELDS) | {"classification", "wax-item-id", "wax", "ticketable"}

TRANSCRIPT_BODY_COLUMNS = (
    "body_sha256", "body", "char_count", "byte_count", "word_count", "token_estimate", "h1_source_name",
    "has_speaker_labels", "speaker_count", "speaker_turn_count", "has_summary_section", "legacy_source_path",
    "legacy_date_local", "legacy_duration_s", "legacy_language", "legacy_model", "legacy_backend",
    "legacy_diarization")

# Identity columns whose NEWEST observation wins (ledger/disk observed); every
# other identity column keeps its first non-null value (a transcribe-time fact).
OBSERVED_IDENTITY = ("origin", "audio_source_hint", "ledger_origin", "ledger_state", "audio_local_path",
                     "backup_verified_at", "backup_method", "audio_s3_bucket", "audio_s3_key",
                     "audio_bytes", "note_size", "note_mtime_ns")
FIRST_IDENTITY = ("ledger_created_at", "source_sha256", "audio_format", "source_audio_original_path",
                  "source_audio_name", "recorded_at", "recorded_precision", "captured_at",
                  "captured_precision", "transcribed_at", "audio_duration_s", "asr_duration_s",
                  "duration_ratio", "language", "language_probability", "engine_model",
                  "transcription_backend", "asr_word_count", "asr_segment_count", "asr_path",
                  "processing_seconds", "diarization_requested_at_transcribe", "diarization_degraded",
                  "diarization_device_requested_at_transcribe", "diarization_device_at_transcribe")


@dataclass
class Extraction:
    relpath: str
    note_text: str
    frontmatter_block: str
    body: str
    fm: dict[str, Any]
    frontmatter_json: dict[str, Any]
    note_sha256: str
    note_bytes: int
    body_sha256: str
    meta_sha256: str
    content_sha256: str
    origin: str
    wax_item_id: Optional[str]
    identity: dict[str, Any]
    version: dict[str, Any]
    body_row: dict[str, Any]
    speakers: list[dict[str, Any]]
    tags: list[str]
    labels: list[str]
    tag_rows: list[tuple[str, int, str]]
    project_rows: list[tuple[int, str, Optional[str], Optional[float]]]
    ticket_rows: list[tuple[int, Optional[str], str, Optional[str]]]
    pass_states: list[dict[str, Any]]
    display_title: str
    doc_input: Optional[str]
    doc_input_sha256: Optional[str]
    current_tags: list[str]
    word_count: int
    parsed: chunking.Parsed = field(repr=False)

    @property
    def searchable(self) -> bool:
        return self.origin != "test"

    def chunks(self) -> list[chunking.Chunk]:
        return chunking.chunk(self.body, self.parsed)


def meta_hash(fm: dict[str, Any]) -> str:
    """sha256 of the canonical frontmatter with `wax.passes` removed."""
    clone = copy.deepcopy(fm) if fm else {}
    wax = clone.get(frontmatter.WAX_KEY)
    if isinstance(wax, dict):
        wax.pop("passes", None)
    return hashlib.sha256(canonical_json(clone).encode("utf-8")).hexdigest()


def content_hash(body_sha: str, meta_sha: str) -> str:
    return hashlib.sha256(f"{body_sha}:{meta_sha}".encode("utf-8")).hexdigest()


def _newest_backup(backups: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    rows = [b for b in backups or [] if isinstance(b, dict) and b.get("s3_key")]
    if not rows:
        return None
    return sorted(rows, key=lambda b: (str(b.get("verified_at") or ""), str(b.get("s3_key"))))[-1]


def _s3_from_uri(uri: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not uri or not str(uri).startswith("s3://"):
        return None, None
    rest = str(uri)[5:]
    bucket, _, key = rest.partition("/")
    return (bucket or None), (key or None)


def from_note(note_text: str, relpath: str, *, ledger: Optional[dict[str, Any]] = None,
              asr: Optional[dict[str, Any]] = None, sidecar: Optional[dict[str, Any]] = None,
              item_id: Optional[str] = None, evidence: Optional[dict[str, Any]] = None,
              stat: Optional[dict[str, Any]] = None, vault_root: str = "",
              ledger_maps_note: bool = False, tz: Optional[dt.tzinfo] = None) -> Extraction:
    """Everything the projector writes for one note snapshot.

    `ledger` = {"item": items row, "transcript": transcripts row, "backups": [rows]};
    `ledger_maps_note` = the ledger's `transcripts.md_path` is this note;
    `asr` = asr.json summary {language, language_probability, segment_count, duration, model,
    backend, source_sha256}; `sidecar` = the `.meta.json` beside a legacy note.
    """
    if "\x00" in note_text:
        raise ExtractError("note contains a NUL byte, which Postgres text cannot store")
    zone = tz or _default_tz()
    fm, block, body = split_note(note_text)
    ledger = ledger or {}
    item = ledger.get("item") or {}
    trow = ledger.get("transcript") or {}
    backups = ledger.get("backups") or []
    asr = asr or {}
    sidecar = sidecar or {}
    wax = fm.get(frontmatter.WAX_KEY) if isinstance(fm.get(frontmatter.WAX_KEY), dict) else {}

    classification = fm.get("classification")
    if not _empty(classification) and classification not in CLASSIFICATIONS:
        raise ExtractError(f"invalid classification {classification!r}; expected one of {CLASSIFICATIONS}")
    classification = None if _empty(classification) else classification

    parsed = chunking.parse(body)
    legacy = parsed.legacy
    legacy_model, legacy_backend = parse_legacy_model(legacy.get("Model"))
    legacy_source = (legacy.get("Source") or "").strip().strip("`").strip() or None
    legacy_date, _ = parse_ts(legacy.get("Date"), tz=zone)
    legacy_duration = parse_legacy_duration(legacy.get("Duration"))

    note_bytes = len(note_text.encode("utf-8"))
    note_sha = sha256_text(note_text)
    body_sha = sha256_text(body)
    meta_sha = meta_hash(fm)
    content_sha = content_hash(body_sha, meta_sha)

    # ---------------------------------------------------------- body stats
    turns = parsed.turns
    speakers: dict[str, dict[str, Any]] = {}
    for seg in turns:
        row = speakers.setdefault(seg.label, {"speaker_label": seg.label, "turn_count": 0, "word_count": 0,
                                              "first_start_s": None, "last_start_s": None})
        row["turn_count"] += 1
        row["word_count"] += len(seg.words)
        if seg.ts is not None:
            if row["first_start_s"] is None:
                row["first_start_s"] = seg.ts
            row["last_start_s"] = seg.ts
    word_count = parsed.word_count
    body_row = {
        "body_sha256": body_sha, "body": body, "char_count": len(body),
        "byte_count": len(body.encode("utf-8")), "word_count": word_count,
        "token_estimate": chunking.token_estimate(body) if body else 0,
        "h1_source_name": parsed.h1_name, "has_speaker_labels": bool(turns),
        "speaker_count": len(speakers), "speaker_turn_count": len(turns),
        "has_summary_section": parsed.summary_span is not None,
        "legacy_source_path": legacy_source, "legacy_date_local": legacy_date,
        "legacy_duration_s": legacy_duration, "legacy_language": legacy.get("Language") or None,
        "legacy_model": legacy_model, "legacy_backend": legacy_backend,
        "legacy_diarization": legacy.get("Diarization") or None,
    }

    # ---------------------------------------------------------- identity
    fm_item = _text(fm.get(frontmatter.ITEM_KEY))
    fm_item = fm_item if fm_item and ITEM_ID_RE.match(fm_item) else None
    wax_item_id = item_id if item_id and ITEM_ID_RE.match(item_id) else fm_item

    source_audio = _text(fm.get("source-audio"))
    original_path = _first(source_audio, legacy_source)
    name = _first(_text(wax.get("source_audio")),
                  os.path.basename(source_audio) if source_audio else None,
                  _text(item.get("orig_name")),
                  os.path.basename(legacy_source) if legacy_source else None,
                  parsed.h1_name)
    audio_format = None
    if name and "." in name:
        audio_format = name.rsplit(".", 1)[1].lower() or None

    backup = _newest_backup(backups)
    bucket = key = None
    if backup:
        bucket, key = _text(backup.get("bucket")), _text(backup.get("s3_key"))
    if not (bucket and key):
        bucket, key = _s3_from_uri(_text(fm.get("source-s3-uri")))
    if not (bucket and key):
        raw_key = _text(fm.get("source-s3-key"))
        if raw_key:
            bucket, key = "recordings", raw_key[len("recordings/"):] if raw_key.startswith("recordings/") else raw_key

    recorded_at, recorded_precision = parse_recorded_at(name, zone)
    captured_at, captured_precision = parse_ts(fm.get("captured"))
    transcribed_at = None
    for candidate in (fm.get("transcribed-at"), wax.get("transcribed_at"), trow.get("created_at")):
        transcribed_at, _ = parse_ts(candidate)
        if transcribed_at:
            break
    if not transcribed_at:
        transcribed_at = legacy_date

    duration_evidence = any(not _empty(v) for v in (fm.get("audio-duration-seconds"), wax.get("audio_duration_s"))) \
        or bool(trow)
    if word_count < 3 and not duration_evidence:
        origin = "test"
    elif fm_item or ledger_maps_note or (item_id and ITEM_ID_RE.match(item_id or "")):
        origin = "wax"
    elif original_path and (str(original_path).startswith("/tmp/hermes-local-stt-")
                            or "/.claude/tmp/hermes_voice" in str(original_path)
                            or "/.hermes/" in str(original_path)):
        origin = "hermes-stt"
    else:
        origin = "legacy"

    identity = {
        "wax_item_id": wax_item_id,
        "origin": origin,
        "audio_source_hint": audio_source_hint(name, original_path),
        "ledger_origin": _text(item.get("origin")),
        "ledger_state": _text(item.get("state")),
        "ledger_created_at": parse_ts(trow.get("created_at"))[0],
        "source_sha256": _first(_sha(fm.get("source-sha256")), _sha(item.get("sha256")), _sha(asr.get("source_sha256"))),
        "audio_s3_bucket": bucket,
        "audio_s3_key": key,
        "audio_bytes": _int(_first(backup.get("bytes") if backup else None, item.get("bytes"))),
        "audio_format": audio_format,
        "backup_verified_at": parse_ts(backup.get("verified_at"))[0] if backup else None,
        "backup_method": _text(backup.get("method")) if backup else None,
        "source_audio_original_path": original_path,
        "source_audio_name": name,
        "audio_local_path": _text(item.get("path")),
        "recorded_at": recorded_at,
        "recorded_precision": recorded_precision,
        "captured_at": captured_at,
        "captured_precision": captured_precision,
        "transcribed_at": transcribed_at,
        "audio_duration_s": _num(_first(wax.get("audio_duration_s"), trow.get("audio_duration"),
                                        item.get("duration_s"), fm.get("duration-seconds"))),
        "asr_duration_s": _num(_first(fm.get("audio-duration-seconds"), trow.get("asr_duration"),
                                      asr.get("duration"), legacy_duration)),
        "duration_ratio": _num(_first(wax.get("duration_ratio"), trow.get("duration_ratio"))),
        "language": _text(_first(fm.get("language"), asr.get("language"), legacy.get("Language"),
                                 sidecar.get("language"))),
        "language_probability": _num(asr.get("language_probability")),
        "engine_model": _text(_first(fm.get("engine-model"), trow.get("engine_model"), legacy_model,
                                     sidecar.get("model"))),
        "transcription_backend": _text(_first(fm.get("transcription-backend"), asr.get("backend"),
                                              legacy_backend, sidecar.get("backend"))),
        "asr_word_count": _int(_first(trow.get("word_count"), fm.get("word-count"), sidecar.get("word_count"))),
        "asr_segment_count": _int(_first(asr.get("segment_count"), sidecar.get("segment_count"))),
        "asr_path": _text(trow.get("asr_path")),
        "processing_seconds": _num(trow.get("processing_seconds")),
        "diarization_requested_at_transcribe": _bool(wax.get("diarization_requested")),
        "diarization_degraded": _bool(wax.get("diarization_degraded")),
        "diarization_device_requested_at_transcribe": _text(wax.get("diarization_device_requested")),
        "diarization_device_at_transcribe": _text(wax.get("diarization_device")),
        "vault_root": vault_root,
        "note_relpath": relpath,
        "note_size": _int((stat or {}).get("size")),
        "note_mtime_ns": _int((stat or {}).get("mtime_ns")),
    }

    # ---------------------------------------------------------- version columns
    version: dict[str, Any] = {}
    for key_name, (column, kind) in VERSION_FIELDS.items():
        raw = fm.get(key_name)
        if kind == "int":
            version[column] = _int(raw)
        elif kind == "num":
            version[column] = _num(raw)
        elif kind == "bool":
            version[column] = _bool(raw)
        elif kind == "ts":
            version[column] = parse_ts(raw)[0]
        elif kind == "date":
            stamp, _ = parse_ts(raw)
            version[column] = stamp.date() if stamp else None
        else:
            version[column] = _text(raw)
    if version.get("schema_version") is not None and not (-32768 <= version["schema_version"] <= 32767):
        version["schema_version"] = None
    for key_name, column in LIST_FIELDS.items():
        version[column] = _list(fm.get(key_name))
    version["classification"] = classification
    version["fm_wax_item_id"] = fm_item
    version["vault_extra"] = {k: to_jsonable(v) for k, v in fm.items() if str(k) not in HANDLED_KEYS}

    tags, labels = version["tags"], version["labels"]
    tag_rows = [("tags", i, t) for i, t in enumerate(tags)] + [("labels", i, t) for i, t in enumerate(labels)]
    names = version["projects"]
    probabilities = {}
    if isinstance(evidence, dict) and isinstance(evidence.get("probabilities"), dict):
        probabilities = evidence["probabilities"]
    project_rows = [(i, pid, names[i] if i < len(names) else None, _num(probabilities.get(pid)))
                    for i, pid in enumerate(version["project_ids"])]
    ticket_rows = []
    tickets = fm.get("ticketable")
    if isinstance(tickets, list):
        for i, ticket in enumerate(tickets):
            if isinstance(ticket, dict) and _text(ticket.get("description")):
                ticket_rows.append((i, _text(ticket.get("project_id")), _text(ticket.get("description")),
                                    _text(_first(ticket.get("transcript_excerpt"), ticket.get("excerpt")))))

    pass_states = []
    passes = wax.get("passes") if isinstance(wax.get("passes"), dict) else {}
    for slug, entry in passes.items():
        if not isinstance(entry, dict):
            continue
        state = _text(entry.get("state")) or "unknown"
        completed = state == "completed"
        pass_states.append({
            "slug": str(slug), "state": state, "at": parse_ts(entry.get("at"))[0],
            "pass_version": _int(entry.get("version")), "attempt": _int(entry.get("attempt")),
            "command_id": _uuid(entry.get("command_id")),
            "reason_code": None if completed else _text(entry.get("reason_code")),
            "detail": None if completed else _text(entry.get("detail")),
            "model": _text(entry.get("model")), "served_by": _text(entry.get("served_by")),
            "route": _text(entry.get("route")), "fell_through": parse_fell_through(entry.get("fell_through")),
        })

    stem = PurePosixPath(relpath).name
    stem = stem[:-3] if stem.endswith(".md") else stem
    display_title = version.get("title") or stem or relpath
    current_tags = list(dict.fromkeys(tags + labels))
    doc_parts = []
    if version.get("title"):
        doc_parts.append(f"Title: {version['title']}")
    if version.get("summary"):
        doc_parts.append(f"Summary: {version['summary']}")
    if current_tags:
        doc_parts.append(f"Tags: {', '.join(current_tags)}")
    doc_input = "\n".join(doc_parts) if doc_parts else None

    return Extraction(
        relpath=relpath, note_text=note_text, frontmatter_block=block, body=body, fm=fm,
        frontmatter_json=to_jsonable(fm), note_sha256=note_sha, note_bytes=note_bytes,
        body_sha256=body_sha, meta_sha256=meta_sha, content_sha256=content_sha, origin=origin,
        wax_item_id=wax_item_id, identity=identity, version=version, body_row=body_row,
        speakers=list(speakers.values()), tags=tags, labels=labels, tag_rows=tag_rows,
        project_rows=project_rows, ticket_rows=ticket_rows, pass_states=pass_states,
        display_title=display_title, doc_input=doc_input,
        doc_input_sha256=sha256_text(doc_input) if doc_input else None,
        current_tags=current_tags, word_count=word_count, parsed=parsed)


def asr_summary(doc: Any) -> dict[str, Any]:
    """The few asr.json facts the store keeps (never the segments themselves)."""
    if not isinstance(doc, dict):
        return {}
    result = doc.get("result") if isinstance(doc.get("result"), dict) else {}
    segments = result.get("segments")
    return {k: v for k, v in {
        "language": result.get("language"),
        "language_probability": result.get("language_probability"),
        "segment_count": len(segments) if isinstance(segments, list) else None,
        "duration": result.get("duration"),
        "model": result.get("model"),
        "backend": result.get("backend"),
        "source_sha256": doc.get("source_sha256"),
    }.items() if v is not None}
