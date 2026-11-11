"""The voice registry: every voice Wax has heard, and the notes you fill in to name them.

Two halves, each the owner of one thing:

- **`var/wax.db`** holds the numbers: one row per voice, its exemplars (one
  fingerprint per recording it was confidently heard in) and every sighting
  (which speaker label in which item sounded like which voice, and how much).
- **The vault note `~/d/Voices/<voice-id>.md`** (`WAX_VOICES_DIR`) holds who it
  is. Its frontmatter `name:` is the truth; the database caches it. A new voice
  gets a note with an empty `name:` and a short clip to listen to, and you are
  notified to fill it in. `status: ignore` keeps a voice (a TV host, a hold
  message) matched, so it is not registered again, but never named or relabelled.

The voice-id pass (`config/passes.d/bin/voice-id`) is the only writer of voices
and sightings; `wax voices` reads them, names and merges voices and relabels
old transcripts after a rename.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import frontmatter, ledger, paths, voiceprint

VOICES_DIR = Path(os.environ.get("WAX_VOICES_DIR") or paths.HOME / "d" / "Voices")
LOCK = paths.VAR / "voices.lock"

MAX_EXEMPLARS = 40      # per voice and channel
REDUNDANT_COS = 0.92    # an exemplar this close (raw) to one already kept on its channel adds nothing

STATUS_UNNAMED, STATUS_NAMED, STATUS_IGNORE = "unnamed", "named", "ignore"

SCHEMA = """
CREATE TABLE IF NOT EXISTS voices (
    voice_id    TEXT PRIMARY KEY,
    name        TEXT,
    status      TEXT NOT NULL DEFAULT 'unnamed',
    note_path   TEXT NOT NULL,
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    first_item  TEXT,
    first_label TEXT,
    merged_into TEXT
);
CREATE TABLE IF NOT EXISTS voice_exemplars (
    voice_id  TEXT NOT NULL,
    item_id   TEXT NOT NULL,
    label     TEXT NOT NULL,
    domain    TEXT NOT NULL,
    seconds   REAL NOT NULL,
    vec       BLOB NOT NULL,
    added_at  TEXT NOT NULL,
    PRIMARY KEY (voice_id, item_id, label)
);
CREATE TABLE IF NOT EXISTS voice_sightings (
    item_id   TEXT NOT NULL,
    label     TEXT NOT NULL,
    rendered  TEXT NOT NULL,
    voice_id  TEXT,
    verdict   TEXT NOT NULL,
    score     REAL,
    runner_up REAL,
    domain    TEXT NOT NULL,
    seconds   REAL NOT NULL,
    vec       BLOB,
    model     TEXT NOT NULL,
    at        TEXT NOT NULL,
    PRIMARY KEY (item_id, label)
);
CREATE INDEX IF NOT EXISTS voice_sightings_voice ON voice_sightings(voice_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect():
    conn = ledger.connect()
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def locked():
    """One registry writer at a time, so two passes never register the same stranger twice."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


@dataclass
class Voice:
    voice_id: str
    name: str | None
    status: str
    note_path: Path
    exemplars: list[tuple[str, np.ndarray]] = field(default_factory=list)  # (domain, raw vector)

    @property
    def display(self) -> str:
        return self.name or self.voice_id

    @property
    def link(self) -> str:
        """An Obsidian link to the voice's note, showing the name once there is one."""
        return f"[[{self.voice_id}|{self.name}]]" if self.name else f"[[{self.voice_id}]]"


def note_identity(path: Path) -> tuple[str | None, str]:
    """(name, status) from a voice note's frontmatter; the note is the truth."""
    fm, _ = frontmatter.read(Path(path))
    name = fm.get("name")
    name = str(name).strip() if name not in (None, "") else None
    status = str(fm.get("status") or "").strip().lower()
    if status == STATUS_IGNORE:
        return name, STATUS_IGNORE
    return name, STATUS_NAMED if name else STATUS_UNNAMED


def load(conn, *, refresh_names: bool = True) -> dict[str, Voice]:
    """Every live (not merged) voice with its exemplars, names re-read from the notes."""
    voices: dict[str, Voice] = {}
    for row in conn.execute("SELECT * FROM voices WHERE merged_into IS NULL"):
        name, status = row["name"], row["status"]
        if refresh_names and Path(row["note_path"]).exists():
            name, status = note_identity(Path(row["note_path"]))
            if (name, status) != (row["name"], row["status"]):
                conn.execute("UPDATE voices SET name=?, status=? WHERE voice_id=?", (name, status, row["voice_id"]))
        voices[row["voice_id"]] = Voice(row["voice_id"], name, status, Path(row["note_path"]))
    for row in conn.execute("SELECT voice_id, domain, vec FROM voice_exemplars ORDER BY added_at"):
        voice = voices.get(row["voice_id"])
        if voice is not None:
            voice.exemplars.append((row["domain"], from_blob(row["vec"])))
    return {vid: v for vid, v in voices.items() if v.exemplars}


def add_exemplar(conn, voice: Voice, item_id: str, label: str, domain: str, seconds: float,
                 vec: np.ndarray) -> bool:
    """Keep this recording's fingerprint as an exemplar when it adds something new on its channel."""
    same = [v for d, v in voice.exemplars if d == domain]
    if same and (len(same) >= MAX_EXEMPLARS or float((np.stack(same) @ vec).max()) >= REDUNDANT_COS):
        return False
    conn.execute("INSERT OR REPLACE INTO voice_exemplars VALUES (?,?,?,?,?,?,?)",
                 (voice.voice_id, item_id, label, domain, float(seconds), to_blob(vec), now()))
    voice.exemplars.append((domain, np.asarray(vec, dtype=np.float32)))
    return True


def new_voice_id(conn) -> str:
    while True:
        candidate = f"voice-{secrets.token_hex(3)}"
        if not conn.execute("SELECT 1 FROM voices WHERE voice_id=?", (candidate,)).fetchone():
            return candidate


def register(conn, *, item_id: str, label: str, domain: str, seconds: float, vec: np.ndarray,
             name: str | None = None, note: str, voice_id: str | None = None) -> Voice:
    """A new voice: its row, first exemplar and vault note (never overwriting a note)."""
    voice_id = voice_id or new_voice_id(conn)
    note_path = VOICES_DIR / f"{voice_id}.md"
    status = STATUS_NAMED if name else STATUS_UNNAMED
    conn.execute("INSERT INTO voices (voice_id, name, status, note_path, model, created_at, first_item, first_label) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (voice_id, name, status, str(note_path), voiceprint.MODEL_NAME, now(), item_id, label))
    voice = Voice(voice_id, name, status, note_path)
    add_exemplar(conn, voice, item_id, label, domain, seconds, vec)
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    if not note_path.exists():
        with open(note_path, "x", encoding="utf-8") as fh:
            fh.write(note)
    return voice


def record_sighting(conn, *, item_id: str, label: str, rendered: str, voice_id: str | None, verdict: str,
                    score: float | None, runner_up: float | None, domain: str, seconds: float,
                    vec: np.ndarray | None) -> None:
    conn.execute("INSERT OR REPLACE INTO voice_sightings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (item_id, label, rendered, voice_id, verdict, score, runner_up, domain, float(seconds),
                  to_blob(vec) if vec is not None else None, voiceprint.MODEL_NAME, now()))


def sightings(conn, item_id: str) -> dict[str, dict]:
    """This item's sightings keyed by the label the speaker has in the note NOW."""
    out = {}
    for row in conn.execute("SELECT * FROM voice_sightings WHERE item_id=? AND model=?",
                            (item_id, voiceprint.MODEL_NAME)):
        record = dict(row)
        record["vec"] = from_blob(row["vec"]) if row["vec"] else None
        out[row["rendered"]] = record
    return out


# ---- the vault note -------------------------------------------------------

def _yaml_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def note_text(voice_id: str, *, transcript: Path, title: str | None, label: str, at: str,
              quotes: list[str], clip: str | None, registered: str) -> str:
    stem = transcript.stem
    link = f"[[{stem}|{title}]]" if title else f"[[{stem}]]"
    lines = [
        "---",
        f"voice-id: {voice_id}",
        "name: ",
        "relationship: ",
        "status: unnamed",
        f"first-heard: {_yaml_str(f'[[{stem}]]')}",
        f"first-heard-as: {_yaml_str(label)}",
        f"first-heard-at: {_yaml_str(at)}",
        f"registered: {registered}",
        "tags: [voice]",
        "---",
        "",
        f"# Unnamed voice `{voice_id}`",
        "",
        "Wax heard a voice it did not know. Fill in `name` above (and",
        "`relationship` if you like). From then on Wax labels this voice by name,",
        "and `wax voices sync` relabels the transcripts it was already heard in.",
        "Set `status: ignore` for a voice that should never be named (TV, a hold",
        "message, a recording).",
        "",
    ]
    if clip:
        lines += [f"![[{clip}]]", ""]
    lines += [f"## First heard", "", f"{link}, as **{label}** from {at}:", ""]
    lines += [f"> {q}" for q in quotes] or ["> (no words long enough to quote)"]
    lines.append("")
    return "\n".join(lines)


def write_clip(source_wav: Path, voice_id: str) -> str | None:
    """The voice's listening sample, next to its note, as Opus. None when ffmpeg cannot."""
    if not shutil.which("ffmpeg") or not Path(source_wav).exists():
        return None
    name = f"{voice_id}.ogg"
    target = VOICES_DIR / name
    if target.exists():
        return name
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".ogg.part")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(source_wav),
                        "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", str(tmp)], check=True, timeout=120)
        os.replace(tmp, target)
        return name
    except (OSError, subprocess.SubprocessError):
        tmp.unlink(missing_ok=True)
        return None


def notify_new(found: list[dict]) -> None:
    """Tell the operator there are voices to name: desktop and phone. Never raises."""
    if not found or os.environ.get("WAX_VOICE_NOTIFY", "1") in ("0", "false", "no"):
        return
    from . import desktop

    lines = [f"{f['label']} in “{f['title']}” → Voices/{f['voice_id']}.md" for f in found]
    title = "Wax heard a new voice" if len(found) == 1 else f"Wax heard {len(found)} new voices"
    body = "Name it: " + "; ".join(lines) if len(found) == 1 else "Name them:\n" + "\n".join(lines)
    desktop.notify(title, body, urgency="normal")
    alert = Path(os.environ.get("WAX_VOICE_NTFY_ALERT") or paths.HOME / ".local/bin/ntfy-alert")
    if alert.exists():
        env = dict(os.environ)
        if os.environ.get("WAX_VOICE_NTFY_URL"):
            env["NTFY_URL"] = os.environ["WAX_VOICE_NTFY_URL"]
        try:
            subprocess.run([str(alert), "--title", title, "--priority", "default", "--tags", "wax,voice", body],
                           env=env, timeout=30, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


GENERIC_LABEL = re.compile(r"^Speaker \d+$")


# ---- operator commands (wax voices) ----------------------------------------------

def listing(conn) -> list[dict]:
    """Every voice, most heard first, with where and how much it was heard."""
    load(conn)  # refresh names from the notes
    rows = conn.execute("""
        SELECT v.voice_id, v.name, v.status, v.note_path, v.created_at, v.merged_into,
               COUNT(DISTINCT s.item_id) AS items, COALESCE(SUM(s.seconds), 0) AS seconds, MAX(s.at) AS last_heard,
               (SELECT COUNT(*) FROM voice_exemplars e WHERE e.voice_id = v.voice_id) AS exemplars
        FROM voices v LEFT JOIN voice_sightings s ON s.voice_id = v.voice_id
        GROUP BY v.voice_id ORDER BY v.merged_into IS NOT NULL, seconds DESC""").fetchall()
    return [dict(r) for r in rows]


def resolve(conn, ref: str) -> dict | None:
    """A voice by id, unique id prefix, or (case-insensitive) name."""
    rows = conn.execute("SELECT * FROM voices WHERE voice_id=? OR voice_id LIKE ? OR lower(name)=lower(?)",
                        (ref, f"{ref}%" if ref.startswith("voice-") else ref, ref)).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def heard_in(conn, voice_id: str) -> list[dict]:
    rows = conn.execute("""SELECT s.item_id, s.label, s.rendered, s.verdict, s.score, s.seconds, t.md_path
                           FROM voice_sightings s LEFT JOIN transcripts t USING(item_id)
                           WHERE s.voice_id=? ORDER BY s.at DESC""", (voice_id,)).fetchall()
    return [dict(r) for r in rows]


def set_name(conn, voice_id: str, name: str) -> None:
    """Name a voice: in its note (the truth) and the cache."""
    row = conn.execute("SELECT note_path FROM voices WHERE voice_id=?", (voice_id,)).fetchone()
    if row is None:
        raise KeyError(voice_id)
    note = Path(row["note_path"])
    if note.exists():
        frontmatter.merge(note, {"name": name, "status": STATUS_NAMED})
    conn.execute("UPDATE voices SET name=?, status=? WHERE voice_id=?", (name, STATUS_NAMED, voice_id))


def merge(conn, keep: str, drop: str) -> int:
    """Fold voice `drop` into `keep` (the same person registered twice). Nothing is deleted."""
    if keep == drop:
        raise ValueError("a voice cannot be merged into itself")
    with locked():
        conn.execute("BEGIN IMMEDIATE")
        try:
            moved = conn.execute("UPDATE voice_sightings SET voice_id=? WHERE voice_id=?", (keep, drop)).rowcount
            conn.execute("INSERT OR IGNORE INTO voice_exemplars SELECT ?, item_id, label, domain, seconds, vec, "
                         "added_at FROM voice_exemplars WHERE voice_id=?", (keep, drop))
            conn.execute("DELETE FROM voice_exemplars WHERE voice_id=?", (drop,))
            conn.execute("UPDATE voices SET merged_into=? WHERE voice_id=?", (keep, drop))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    row = conn.execute("SELECT note_path FROM voices WHERE voice_id=?", (drop,)).fetchone()
    if row and Path(row["note_path"]).exists():
        frontmatter.merge(Path(row["note_path"]), {"status": "merged", "merged-into": f"[[{keep}]]"})
    return moved


def merge_namesakes(conn) -> list[tuple[str, str]]:
    """Fold voices that share a name (you named a second voice after someone already known) into the oldest."""
    live = load(conn)
    by_name: dict[str, list[str]] = {}
    for row in conn.execute("SELECT voice_id, created_at FROM voices WHERE merged_into IS NULL ORDER BY created_at"):
        voice = live.get(row["voice_id"])
        if voice is not None and voice.name and voice.status != STATUS_IGNORE:
            by_name.setdefault(voice.name.casefold(), []).append(voice.voice_id)
    merged = []
    for ids in by_name.values():
        for drop in ids[1:]:
            merge(conn, ids[0], drop)
            merged.append((ids[0], drop))
    return merged


def needs_relabel(conn) -> list[str]:
    """Items whose transcript still shows an old label for a voice that has (or changed) a name."""
    voices_now = {v.voice_id: v for v in load(conn).values()}
    stale = set()
    for row in conn.execute("SELECT item_id, label, rendered, voice_id FROM voice_sightings "
                            "WHERE voice_id IS NOT NULL AND verdict IN ('matched','new')"):
        voice = voices_now.get(row["voice_id"])
        if voice is None or voice.status == STATUS_IGNORE or not voice.name:
            continue
        if GENERIC_LABEL.match(row["label"]) and row["rendered"] != voice.name:
            stale.add(row["item_id"])
    return sorted(stale)
