"""`wax voices tui`: name and merge voices full-screen, hearing each one while you do.

A front end over `voices.py`, not a second implementation: naming is `voices.set_name` then
`voices.merge_namesakes`, merging is `voices.merge`, and relabelling transcripts is
`voices.relabel_state` (the voice-id pass), exactly what `wax voices name|merge` run.

Three layers, so the interesting part is testable without a terminal:

- clips: `plan_clips` picks several distinct ~5 s windows of one voice (from the turns its
  sightings recorded, cut out of the item's audio; else slices of the voice's own listening
  clip), `cut_clip` caches them under `var/voice-clips/`, and `ClipLoop` plays them in a loop
  as a killable subprocess. A clip is a throwaway copy: never written to the vault or
  `~/d/Voices`, and `rm -r var/voice-clips` is always safe.
- `Model`: state and key handling (curses-free). `layout` turns it into styled text lines.
- the curses view (`run_curses`): reads keys, draws `layout`. Nothing else.

Relabelling transcripts is slow, so it never runs on the UI thread: a name or merge asks the
`Relabeler` (one background thread, requests coalesce) and the result is summarised at exit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import ledger, paths, voiceprint, voices

CLIP_S = 5.0          # the length of one clip
MIN_CLIP_S = 2.0      # a turn with less speech than this is not worth a clip
TRIM_S = 0.5          # turn edges carry the previous speaker's tail, and `mm:ss` heads are floored
MAX_CLIPS = 8         # per voice; enough variety to recognise someone, short enough to cycle
CONFIDENT = ("matched", "new", "context")   # sightings the pass was sure of; an `uncertain` one may be someone else


# ---- clips -------------------------------------------------------------------------------

@dataclass(frozen=True)
class ClipSpec:
    source: str      # audio file to cut from
    start: float     # seconds into it
    dur: float
    what: str        # for the status line: "<item> 01:23" or "listening clip"


def probe_seconds(path: str | Path) -> float | None:
    """Length of an audio file, or None when ffprobe cannot tell."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(path)], capture_output=True, text=True, timeout=30, check=True).stdout
        return float(out.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _turn_windows(heads: list[str], body: str, duration: float | None) -> list[tuple[float, float]]:
    """(start, end) of each turn head a sighting recorded. A turn ends where the next turn (of
    anyone) starts in the transcript; without the transcript it is assumed to run one clip."""
    starts = sorted({s for _, s in voiceprint.parse_turns(body)}) if body else []
    out = []
    for head in heads:
        try:
            start = voiceprint.parse_timestamp(head)
        except ValueError:
            continue
        later = [s for s in starts if s > start]
        if later:
            end = later[0]
        elif duration and duration > start:
            end = duration
        else:
            end = start + CLIP_S + 2 * TRIM_S
        out.append((start, end))
    return out


def _listening_clip_windows(voice_id: str, note_path: str | None, max_clips: int) -> list[ClipSpec]:
    """Slices of the voice's own `<voice-id>.ogg` listening clip: 5 s windows, a short tail kept
    when it is long enough to hear."""
    candidates = []
    if note_path:
        candidates.append(Path(note_path).parent / f"{voice_id}.ogg")
    candidates.append(voices.VOICES_DIR / f"{voice_id}.ogg")
    source = next((p for p in candidates if p.exists()), None)
    if source is None:
        return []
    total = probe_seconds(source)
    if total is None:
        return [ClipSpec(str(source), 0.0, CLIP_S, "listening clip")]
    clips, at = [], 0.0
    while at < total and len(clips) < max_clips:
        dur = min(CLIP_S, total - at)
        if dur >= MIN_CLIP_S or not clips:
            clips.append(ClipSpec(str(source), round(at, 2), round(dur, 2), f"listening clip {at:.0f}s"))
        at += CLIP_S
    return clips


def plan_clips(conn, voice_id: str, *, max_clips: int = MAX_CLIPS) -> list[ClipSpec]:
    """Distinct ~5 s clips of this voice, spread over the recordings it was confidently heard in.

    Each sighting recorded the turn heads (`mm:ss`) its speaker label owned. A turn's window is
    cut from the item's audio (the ledger's `items.path`) when that file still exists, longest
    turns first, one clip per turn, taking a turn from each recording in rotation. A voice with
    no usable turn (a channel-separated call, audio gone) falls back to slices of its listening
    clip. Empty when there is nothing to hear.
    """
    marks = ",".join("?" for _ in CONFIDENT)
    rows = conn.execute(
        f"SELECT s.item_id, s.turns, i.path, t.md_path, t.audio_duration FROM voice_sightings s "
        f"JOIN items i ON i.item_id = s.item_id LEFT JOIN transcripts t ON t.item_id = s.item_id "
        f"WHERE s.voice_id=? AND s.model=? AND s.turns IS NOT NULL AND s.verdict IN ({marks}) "
        f"ORDER BY s.seconds DESC", (voice_id, voiceprint.MODEL_NAME, *CONFIDENT)).fetchall()
    per_item: list[list[ClipSpec]] = []
    for row in rows:
        if not row["path"] or not Path(row["path"]).exists():
            continue
        body = ""
        if row["md_path"] and Path(row["md_path"]).exists():
            body = Path(row["md_path"]).read_text(encoding="utf-8", errors="replace")
        try:
            heads = json.loads(row["turns"])
        except ValueError:
            continue
        found = []
        for start, end in _turn_windows(heads, body, row["audio_duration"]):
            room = end - start - 2 * TRIM_S
            if room < MIN_CLIP_S:
                continue
            dur = min(CLIP_S, room)
            at = start + TRIM_S + (room - dur) / 2     # centred: the middle of a turn is the surest speech
            stamp = f"{int(start) // 60:02d}:{int(start) % 60:02d}"
            found.append((room, ClipSpec(row["path"], round(at, 2), round(dur, 2), f"{row['item_id']} {stamp}")))
        found.sort(key=lambda pair: -pair[0])
        if found:
            per_item.append([spec for _, spec in found])
    clips: list[ClipSpec] = []
    depth = 0
    while len(clips) < max_clips and any(depth < len(c) for c in per_item):
        for candidates in per_item:
            if depth < len(candidates) and len(clips) < max_clips:
                clips.append(candidates[depth])
        depth += 1
    if clips:
        return clips
    note = conn.execute("SELECT note_path FROM voices WHERE voice_id=?", (voice_id,)).fetchone()
    return _listening_clip_windows(voice_id, note["note_path"] if note else None, max_clips)


def clip_dir() -> Path:
    return paths.VAR / "voice-clips"


def cut_clip(spec: ClipSpec) -> Path | None:
    """The clip as a small mono WAV (every player reads it), cached; None when ffmpeg cannot."""
    if not shutil.which("ffmpeg") or not Path(spec.source).exists():
        return None
    stamp = int(Path(spec.source).stat().st_mtime)
    key = hashlib.sha1(f"{spec.source}|{stamp}|{spec.start}|{spec.dur}".encode()).hexdigest()[:20]
    target = clip_dir() / f"{key}.wav"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".wav.part")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{spec.start}", "-t", f"{spec.dur}",
                        "-i", spec.source, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav",
                        str(tmp)], check=True, timeout=60, stdin=subprocess.DEVNULL)
        os.replace(tmp, target)
        return target
    except (OSError, subprocess.SubprocessError):
        tmp.unlink(missing_ok=True)
        return None


# ---- the player --------------------------------------------------------------------------

PLAYERS = (["pw-play"], ["paplay"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"], ["aplay", "-q"])


def player_command(environ=None) -> list[str] | None:
    """The command that plays one WAV file when given its path: `$WAX_VOICES_PLAYER`
    (shell-quoted words) or the first of pw-play, paplay, ffplay, aplay on PATH."""
    environ = os.environ if environ is None else environ
    override = (environ.get("WAX_VOICES_PLAYER") or "").strip()
    if override:
        return shlex.split(override)
    for argv in PLAYERS:
        if shutil.which(argv[0]):
            return list(argv)
    return None


class NullPlayer:
    """No audio: the TUI still names and merges, and the status line says why nothing plays."""
    available = False

    def __init__(self, reason: str):
        self.reason = reason

    def play(self, voice_id, clips): ...
    def stop(self): ...
    def skip(self): ...

    def state(self):
        return None


class ClipLoop:
    """Plays a voice's clips one after another, 1 → 2 → … → 1, as one player subprocess at a time.

    `stop()` kills the running player at once. The thread blocks on the player process itself
    (no polling), and a generation event keeps a stale thread from starting a player after a stop.
    """
    available = True
    reason = ""
    GIVE_UP_AFTER = 3     # consecutive clips that failed to cut or play

    def __init__(self, argv: list[str], cut: Callable[[ClipSpec], Path | None] = cut_clip):
        self.argv, self.cut = argv, cut
        self._lock = threading.Lock()
        self._halt: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._skip = False
        self._state: dict | None = None

    def play(self, voice_id: str, clips: list[ClipSpec]) -> None:
        self.stop()
        if not clips:
            return
        halt = threading.Event()
        state = {"voice_id": voice_id, "index": 0, "total": len(clips), "what": clips[0].what, "error": ""}
        with self._lock:
            self._halt, self._state = halt, state
            self._thread = threading.Thread(target=self._run, args=(halt, state, clips), daemon=True)
            self._thread.start()

    def _run(self, halt: threading.Event, state: dict, clips: list[ClipSpec]) -> None:
        index, failures = 0, 0
        while not halt.is_set():
            spec = clips[index % len(clips)]
            state["index"], state["what"] = index % len(clips), spec.what
            path = self.cut(spec)
            ok = False
            if path is not None and not halt.is_set():
                with self._lock:
                    if halt.is_set():
                        break
                    try:
                        proc = subprocess.Popen([*self.argv, str(path)], stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except OSError as exc:
                        state["error"] = f"cannot start {self.argv[0]}: {exc}"
                        break
                    self._proc = proc
                rc = proc.wait()
                with self._lock:
                    self._proc = None
                    skipped, self._skip = self._skip, False
                ok = rc == 0 or skipped
            elif path is None:
                state["error"] = "cannot cut a clip (ffmpeg?)"
            failures = 0 if ok else failures + 1
            if failures >= self.GIVE_UP_AFTER:
                state["error"] = state["error"] or f"{self.argv[0]} keeps failing"
                break
            index += 1

    def skip(self) -> None:
        """Jump to the next clip now."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._skip = True
                self._proc.kill()

    def stop(self) -> None:
        with self._lock:
            halt, thread, proc = self._halt, self._thread, self._proc
            if halt is not None:
                halt.set()
            if proc is not None and proc.poll() is None:
                proc.kill()
            self._halt = self._thread = self._state = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)

    def state(self) -> dict | None:
        """{voice_id, index, total, what, error} while a loop is set up, else None."""
        with self._lock:
            return dict(self._state) if self._state else None


def make_player(*, no_play: bool = False):
    if no_play:
        return NullPlayer("audio is off (--no-play)")
    argv = player_command()
    if argv is None:
        return NullPlayer("no audio player found (pw-play, paplay, ffplay, aplay; or set WAX_VOICES_PLAYER)")
    return ClipLoop(argv)


# ---- relabelling, off the UI thread ---------------------------------------------------------

class Relabeler:
    """Reruns voice-id on every transcript showing a stale name, in one background thread.

    `request()` is cheap and may be called after every name or merge: requests that arrive while a
    sweep runs make it look again when it finishes, so nothing waits and nothing runs twice at once.
    """

    def __init__(self, run_one: Callable[[str], str] = voices.relabel_state,
                 items: Callable[[], list[str]] | None = None):
        self.run_one = run_one
        self.items = items or (lambda: voices.needs_relabel(voices.connect()))
        self._lock = threading.Lock()
        self._dirty = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.total = self.done = 0
        self.current = ""
        self.results: list[tuple[str, str]] = []

    def request(self) -> None:
        with self._lock:
            self._dirty = True
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._dirty or self._stop.is_set():
                    self._thread = None
                    self.current = ""
                    return
                self._dirty = False
            try:
                todo = self.items()
            except Exception as exc:  # the registry is the owner of its errors; show, do not die
                with self._lock:
                    self.results.append(("(find stale transcripts)", f"error: {exc}"))
                continue
            with self._lock:
                self.total += len(todo)
            for item_id in todo:
                if self._stop.is_set():      # asked to stop: the transcript in flight finished, start no more
                    break
                with self._lock:
                    self.current = item_id
                try:
                    state = self.run_one(item_id)
                except Exception as exc:
                    state = f"error: {exc}"
                with self._lock:
                    self.done += 1
                    self.results.append((item_id, state))

    def stop(self) -> None:
        """Finish the transcript in flight, start no more. Never kills a pass mid-write."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def remaining(self) -> int:
        with self._lock:
            return max(0, self.total - self.done)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None

    def progress(self) -> str:
        with self._lock:
            if self._thread is None:
                return ""
            return f"relabelling {self.done}/{self.total}" + (f" ({self.current})" if self.current else "")

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self.running

    def failed(self) -> list[tuple[str, str]]:
        with self._lock:
            return [r for r in self.results if r[1] not in voices.RELABEL_OK]

    def summary(self) -> list[str]:
        with self._lock:
            results = list(self.results)
        if not results:
            return []
        bad = [r for r in results if r[1] not in voices.RELABEL_OK]
        lines = [f"relabelled {len(results) - len(bad)} of {len(results)} transcript(s)"]
        lines += [f"  {item_id}  {state}" for item_id, state in bad]
        if self.stopped and self.remaining():
            lines.append(f"  stopped early: {self.remaining()} transcript(s) not started")
        if bad or (self.stopped and self.remaining()):
            lines.append("  rerun the failures with: wax voices sync" if not self.stopped
                         else "  rerun the failures and the rest with: wax voices sync")
        return lines


# ---- registry changes, off the UI thread ------------------------------------------------------------

class InlineWorker:
    """Runs a change on the spot, on the model's own connection. Tests, and nothing else."""
    busy = False
    label = ""

    def __init__(self, conn):
        self.conn = conn

    def submit(self, label: str, fn: Callable, then: Callable) -> None:
        try:
            result, error = fn(self.conn), None
        except Exception as exc:
            result, error = None, exc
        then(result, error)

    def poll(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool:
        return True


class ThreadWorker:
    """Runs one registry change (name, merge, reload) at a time on its own thread and connection.

    `voices.merge` waits on the registry flock, which a voice-id pass holds across its LLM call (tens
    of seconds), and the ledger waits up to 10 s for a writer. On the UI thread either would freeze
    the screen and Ctrl+C. Here the screen keeps drawing and reading keys; `poll()`, called from the
    UI loop, delivers the result to `then(result, error)` on the UI thread, where the model may touch
    its own state and connection.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._done: tuple | None = None
        self.label = ""

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def submit(self, label: str, fn: Callable, then: Callable) -> None:
        def run():
            try:
                outcome = (fn(voices.connect()), None)     # this thread's own ledger connection
            except Exception as exc:
                outcome = (None, exc)
            with self._lock:
                self._done = (then, *outcome)

        self.label = label
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def poll(self) -> None:
        with self._lock:
            done, self._done = self._done, None
        if done is not None:
            self._thread = None
            then, result, error = done
            then(result, error)

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not (thread is not None and thread.is_alive())


# ---- the model: state, keys, actions ----------------------------------------------------------

BROWSE, MERGE_PICK, MERGE_CONFIRM = "browse", "merge-pick", "merge-confirm"
NAME_MAX = 80


def _live(row: dict) -> bool:
    return not row["merged_into"]


def _unnamed(row: dict) -> bool:
    return _live(row) and row["status"] == voices.STATUS_UNNAMED


def sort_rows(rows: list[dict]) -> list[dict]:
    """Unnamed first (most heard first), then named, then ignored, merged voices last."""
    rank = {voices.STATUS_UNNAMED: 0, voices.STATUS_NAMED: 1, voices.STATUS_IGNORE: 2}
    return sorted(rows, key=lambda r: (not _live(r), rank.get(r["status"], 1), -float(r["seconds"] or 0), r["voice_id"]))


class Model:
    """Everything the screen shows and every key's effect, with no curses in it.

    Keys arrive as tokens: a printable character, or one of UP DOWN PGUP PGDN HOME END ENTER ESC TAB
    BACKSPACE ALT_M CTRL_C CTRL_Q CTRL_R CTRL_U CTRL_W CTRL_N. `focus` says where typing goes: "name"
    (the NAME field of the highlighted row; the default on an unnamed voice) or "list".
    """

    def __init__(self, conn, player, relabeler: Relabeler | None, *, planner=plan_clips, rows: list[dict] | None = None,
                 worker=None):
        self.conn, self.player, self.relabeler, self.planner = conn, player, relabeler, planner
        self.worker = worker or InlineWorker(conn)
        self.rows = sort_rows(rows if rows is not None else voices.listing(conn))
        self.cursor = 0
        self.merge_cursor = 0
        self.mode = BROWSE
        self.focus = "list"
        self.buffer = ""
        self.drafts: dict[str, str] = {}
        self.editing_named = False
        self.status = ""
        self.top = 0                 # first visible row; kept by `layout`
        self.quit = False
        self.arrive()

    # -- queries
    @property
    def row(self) -> dict | None:
        return self.rows[self.cursor] if self.rows else None

    @property
    def pick(self) -> dict | None:
        return self.rows[self.merge_cursor] if self.rows else None

    @property
    def busy(self) -> bool:
        return self.worker.busy

    def tick(self) -> None:
        """Called by the screen loop: hand a finished registry change back to the model."""
        try:
            self.worker.poll()
        except Exception as exc:
            self.status = f"error: {exc}"

    def drain(self) -> None:
        """After the screen is gone: wait for a registry change in flight, then apply it (it may queue a relabel)."""
        while not self.worker.wait(0.5):
            pass
        self.tick()

    def unnamed_count(self) -> int:
        return sum(1 for r in self.rows if _unnamed(r))

    def confirm_prompt(self) -> str:
        keep, drop = self.pick, self.row
        who = keep["name"] or "unnamed"
        text = f"fold {drop['voice_id']} into {keep['voice_id']} ({who})? [y/n]"
        if drop["name"] and not keep["name"]:
            text += f"  note: {keep['voice_id']} is unnamed and will not take the name {drop['name']!r}"
        return text

    # -- arriving on a row
    def arrive(self) -> None:
        """The highlight is on a new row: stop whatever played, set the focus, play it when unnamed."""
        self.player.stop()
        row = self.row
        self.editing_named = False
        if row is None:
            self.focus = "list"
            return
        if _unnamed(row):
            self.focus = "name"
            self.buffer = self.drafts.get(row["voice_id"], "")
            self.play(row)
        else:
            self.focus = "list"
            self.buffer = ""

    def play(self, row: dict) -> None:
        if not self.player.available:
            self.status = self.player.reason
            return
        clips = self.planner(self.conn, row["voice_id"])
        if not clips:
            self.status = f"nothing to play for {row['voice_id']} (no audio left for its sightings, no listening clip)"
            return
        self.player.play(row["voice_id"], clips)
        self.status = f"playing {len(clips)} clip(s) of {row['voice_id']}"

    def _stash_draft(self) -> None:
        row = self.row
        if row is not None and self.focus == "name" and not self.editing_named:
            if self.buffer:
                self.drafts[row["voice_id"]] = self.buffer
            else:
                self.drafts.pop(row["voice_id"], None)

    def move(self, to: int) -> None:
        if not self.rows:
            return
        to = max(0, min(len(self.rows) - 1, to))
        if self.mode == MERGE_PICK:
            self.merge_cursor = to
            return
        if to != self.cursor:
            self._stash_draft()
            self.cursor = to
            self.arrive()

    def _step(self, key: str) -> int:
        page = 10
        return {"UP": -1, "DOWN": 1, "PGUP": -page, "PGDN": page}[key]

    def refresh(self) -> None:
        """Re-read every row's numbers and names (off the UI thread); order is kept, new voices join at the end."""
        def done(rows, error):
            if error:
                self.status = f"reload failed: {error}"
            else:
                self._apply(rows)

        self.worker.submit("reloading", lambda conn: voices.listing(conn), done)

    def _apply(self, listing: list[dict]) -> None:
        fresh = {r["voice_id"]: r for r in listing}
        known = [fresh.pop(r["voice_id"]) for r in self.rows if r["voice_id"] in fresh]
        self.rows = known + sort_rows(list(fresh.values()))
        self.cursor = min(self.cursor, max(0, len(self.rows) - 1))
        self.merge_cursor = min(self.merge_cursor, max(0, len(self.rows) - 1))

    def next_unnamed(self, after: int) -> int | None:
        for offset in range(1, len(self.rows) + 1):
            index = (after + offset) % len(self.rows)
            if _unnamed(self.rows[index]):
                return index
        return None

    def advance(self) -> None:
        """After a name or a merge, go to the next voice that still waits for one."""
        target = self.next_unnamed(self.cursor)
        if target is not None and target != self.cursor:
            self.cursor = target
        self.arrive()

    def _relabel(self) -> str:
        if self.relabeler is None:
            return " (not relabelled: --no-sync; run wax voices sync)"
        self.relabeler.request()
        return ""

    # -- keys
    def handle_key(self, key: str | None) -> None:
        if key is None:
            return
        if key in ("CTRL_C", "CTRL_Q"):
            self.quit = True
            return
        if self.busy:
            self.status = f"{self.worker.label}: waiting on the voice registry (a relabel may hold it); Ctrl+C quits"
            return
        try:
            self._dispatch(key)
        except Exception as exc:     # a locked ledger or a vanished file must not end the session
            self.status = f"error: {exc}"

    def _dispatch(self, key: str) -> None:
        if self.mode == MERGE_CONFIRM:
            return self._key_confirm(key)
        if self.mode == MERGE_PICK:
            return self._key_pick(key)
        self._key_browse(key)

    def _key_browse(self, key: str) -> None:
        row = self.row
        if row is None:
            if key in ("q", "ESC"):
                self.quit = True
            return
        if key in ("UP", "DOWN", "PGUP", "PGDN"):
            return self.move(self.cursor + self._step(key))
        if key == "HOME":
            return self.move(0)
        if key == "END":
            return self.move(len(self.rows) - 1)
        if key == "ALT_M":
            return self.start_merge()
        if key == "CTRL_R":
            return self.toggle_play(restart=True)
        if key == "CTRL_N":
            return self.player.skip()
        if self.focus == "name":
            return self._key_name(key)
        if key == "q":
            self.quit = True
        elif key in ("j", "k"):
            self.move(self.cursor + (1 if key == "j" else -1))
        elif key in (" ", "p"):
            self.toggle_play()
        elif key == "m":
            self.start_merge()
        elif key == "r":
            self.status = "reloading"
            self.refresh()
        elif key in ("ENTER", "TAB", "e", "i"):
            self.begin_name()

    def begin_name(self) -> None:
        row = self.row
        if not _live(row):
            self.status = f"{row['voice_id']} is merged into {row['merged_into']}; name that one"
            return
        self.focus = "name"
        if _unnamed(row):
            self.buffer = self.drafts.get(row["voice_id"], "")
        else:
            self.editing_named = True
            self.buffer = row["name"] or ""
        self.status = "type a name, Enter locks it in"

    def _key_name(self, key: str) -> None:
        if key == "ENTER":
            return self.commit_name()
        if key == "BACKSPACE":
            self.buffer = self.buffer[:-1]
        elif key == "CTRL_U":
            self.buffer = ""
        elif key == "CTRL_W":
            self.buffer = re.sub(r"\S*\s*$", "", self.buffer)
        elif key == "ESC":
            if self.editing_named:
                self.editing_named, self.focus, self.buffer = False, "list", ""
                self.status = "rename cancelled"
            elif self.buffer:
                self.buffer = ""
        elif key == "TAB":
            if self.editing_named:
                self.editing_named, self.focus, self.buffer = False, "list", ""
            else:
                self._stash_draft()
                self.focus = "list"
                self.status = "list focus: j/k move, q quits, Tab/Enter returns to NAME"
        elif len(key) == 1 and key.isprintable() and len(self.buffer) < NAME_MAX:
            self.buffer += key
        return

    def toggle_play(self, restart: bool = False) -> None:
        row = self.pick if self.mode == MERGE_PICK else self.row
        if row is None:
            return
        state = self.player.state()
        if state and state["voice_id"] == row["voice_id"] and not restart:
            self.player.stop()
            self.status = "stopped"
            return
        if not _live(row):
            self.status = f"{row['voice_id']} is merged; nothing to play"
            return
        self.play(row)

    def commit_name(self) -> None:
        row = self.row
        name = " ".join(self.buffer.split())
        if not name:
            self.status = "type a name first (Ctrl+C quits)"
            return
        if name == row["name"]:
            self.editing_named, self.focus, self.buffer = False, "list", ""
            self.status = f"{row['voice_id']} is already {name}"
            return
        vid = row["voice_id"]

        def job(conn):
            voices.set_name(conn, vid, name)
            return voices.merge_namesakes(conn), voices.listing(conn)

        def done(result, error):
            if error:
                self.status = f"could not name {vid}: {error} (r reloads)"
                return
            folded, rows = result
            self.drafts.pop(vid, None)
            note = "".join(f"; {drop} merged into {keep}, already named {name}" for keep, drop in folded)
            message = f"{vid} is {name}{note}" + self._relabel()
            if self.quit:                  # quit while the registry was busy: the change landed, the screen is gone
                return
            self._apply(rows)
            self.advance()
            self.status = message          # after advance: arriving on the next voice must not hide the confirmation

        self.status = f"naming {vid}..."
        self.worker.submit(f"naming {vid}", job, done)

    # -- merge
    def start_merge(self) -> None:
        row = self.row
        if row is None:
            return
        if not _live(row):
            self.status = f"{row['voice_id']} is already merged into {row['merged_into']}"
            return
        self._stash_draft()
        self.mode = MERGE_PICK
        # Start on the most heard other live voice that already has a name, else the next live one.
        order = [i for i, r in enumerate(self.rows) if _live(r) and i != self.cursor]
        named = [i for i in order if self.rows[i]["status"] == voices.STATUS_NAMED]
        self.merge_cursor = (named or order or [self.cursor])[0]
        self.status = (f"MERGE: fold {row['voice_id']} into the voice you pick; "
                       "up/down choose, Enter confirms, Esc cancels, Space plays it")

    def _key_pick(self, key: str) -> None:
        if key in ("UP", "DOWN", "PGUP", "PGDN"):
            return self.move(self.merge_cursor + self._step(key))
        if key in ("k", "j"):
            return self.move(self.merge_cursor + (1 if key == "j" else -1))
        if key == "HOME":
            return self.move(0)
        if key == "END":
            return self.move(len(self.rows) - 1)
        if key in (" ", "p"):
            return self.toggle_play()
        if key == "ESC":
            self.cancel_merge("merge cancelled")
        elif key == "ENTER":
            keep, drop = self.pick, self.row
            if keep["voice_id"] == drop["voice_id"]:
                self.status = "a voice cannot be merged into itself; pick another"
            elif not _live(keep):
                self.status = f"{keep['voice_id']} is merged into {keep['merged_into']}; pick that one"
            else:
                self.player.stop()
                self.mode = MERGE_CONFIRM
                self.status = self.confirm_prompt()

    def _key_confirm(self, key: str) -> None:
        if key in ("y", "Y"):
            self.do_merge()
        elif key in ("n", "N", "ESC"):
            self.mode = MERGE_PICK
            self.status = "not merged; pick again or Esc to cancel"

    def cancel_merge(self, why: str) -> None:
        self.mode = BROWSE
        self.arrive()
        self.status = why

    def do_merge(self) -> None:
        keep, drop = self.pick, self.row
        if keep["voice_id"] == drop["voice_id"] or not _live(keep) or not _live(drop):
            return self.cancel_merge("nothing merged: both voices must be live and different")
        keep_id, drop_id = keep["voice_id"], drop["voice_id"]

        def job(conn):
            return voices.merge(conn, keep_id, drop_id), voices.listing(conn)

        def done(result, error):
            if error:
                self.mode = MERGE_PICK
                self.status = f"merge failed: {error}"
                return
            moved, rows = result
            self.drafts.pop(drop_id, None)
            message = f"{drop_id} folded into {keep_id} ({moved} sightings moved)" + self._relabel()
            self.mode = BROWSE
            if self.quit:
                return
            self._apply(rows)
            self.advance()
            self.status = message

        self.status = f"merging {drop_id} into {keep_id}..."
        self.worker.submit(f"merging {drop_id}", job, done)

    def shutdown(self) -> None:
        self.player.stop()


# ---- layout: the model as styled text (still no curses) -------------------------------------------

COLUMNS = (("VOICE", 14, "<"), ("NAME", 22, "<"), ("STATUS", 9, "<"), ("ITEMS", 6, ">"), ("MINUTES", 8, ">"))
HELP = {
    ("browse", "name"): "type name  Enter lock in  Up/Down move  Alt+M merge  Tab list  ^R replay  ^N next clip  ^C quit",
    ("browse", "list"): "j/k move  Enter/e name  Space play  Alt+M/m merge  r reload  q quit",
    ("merge-pick", ""): "MERGE  Up/Down pick target  Enter confirm  Space play  Esc cancel",
    ("merge-confirm", ""): "y fold it in  n/Esc back",
}


def fit(text: str, width: int, align: str = "<") -> str:
    text = text.replace("\n", " ")
    if len(text) > width:
        text = text[:max(0, width - 1)] + "…" if width > 1 else text[:width]
    return f"{text:{align}{width}}"


def _columns_for(usable: int) -> list[tuple[str, int, str]]:
    """The columns that fit `usable` characters (after the 2-wide marker), as (name, width, align).
    MINUTES, ITEMS and STATUS drop out first; then NAME and VOICE shrink; NAME gets whatever is spare."""
    cols = [list(c) for c in COLUMNS]
    taken = lambda: sum(w + 1 for _, w, _ in cols)
    while len(cols) > 2 and taken() > usable:
        cols.pop()
    for index in (1, 0):                    # NAME, then VOICE, down to 4 characters each
        cols[index][1] = max(4, cols[index][1] - max(0, taken() - usable))
    if taken() > usable:
        cols = [["NAME", max(1, usable - 1), "<"]]
    else:
        for col in cols:
            if col[0] == "NAME":
                col[1] += usable - taken()
    return [tuple(c) for c in cols]


def layout(model: Model, width: int, height: int) -> list[tuple[str, str]]:
    """`height` lines of (text, style), each at most `width - 1` wide. Styles: header, row, cursor,
    pick, dim, edit, play, status, help. Safe at any size: a tiny terminal gets fewer lines, never an error."""
    width, height = max(1, width), max(1, height)
    if width < 12 or height < 3:
        return [(fit("terminal too small", width - 1 or 1), "status")] + [("", "row")] * (height - 1)
    cols = _columns_for(width - 1 - 2)

    def line(marker: str, cells: dict) -> str:
        parts = [fit(str(cells.get(n, "")), w, a) for n, w, a in cols]
        return (marker + " " + " ".join(parts))[:width - 1]

    footer = min(3, max(1, height - 4))      # a short terminal keeps table rows and gives up help, then the play line
    body_rows = max(0, height - 2 - footer)
    out: list[tuple[str, str]] = [
        (fit(f"wax voices   {len(model.rows)} voices, {model.unnamed_count()} waiting for a name", width - 1), "header"),
        (line("  ", {n: n for n, _, _ in cols}), "header"),
    ]
    active = model.merge_cursor if model.mode != BROWSE else model.cursor
    if active < model.top:
        model.top = active
    if body_rows and active >= model.top + body_rows:
        model.top = active - body_rows + 1
    for index in range(model.top, min(len(model.rows), model.top + body_rows)):
        row = model.rows[index]
        status = "merged" if row["merged_into"] else row["status"]
        name = row["name"] or "-"
        style, marker = ("dim" if row["merged_into"] else "row"), "  "
        if index == model.cursor:
            marker = "- " if model.mode != BROWSE else "> "
            style = "cursor"
            if model.mode == BROWSE and model.focus == "name":
                name, style = model.buffer + "_", "edit"
        if model.mode != BROWSE and index == model.merge_cursor:
            marker, style = "+ ", "pick"
        out.append((line(marker, {"VOICE": row["voice_id"], "NAME": name, "STATUS": status,
                                  "ITEMS": row["items"], "MINUTES": f"{(row['seconds'] or 0) / 60:.1f}"}), style))
    while len(out) < 2 + body_rows:
        out.append(("", "row"))
    key = (model.mode, model.focus if model.mode == BROWSE else "")
    tail = [(fit(_play_line(model), width - 1), "play"), (fit(model.status, width - 1), "status"),
            (fit(HELP.get(key, ""), width - 1), "help")]
    out += [tail[1]] if footer == 1 else tail[:2] if footer == 2 else tail
    return out[:height]


def _play_line(model: Model) -> str:
    if model.mode == BROWSE and model.focus == "name" and not model.editing_named:
        head = f"NAME> {model.buffer}_"
    elif model.mode == BROWSE and model.editing_named:
        head = f"RENAME> {model.buffer}_"
    elif model.mode == MERGE_PICK:
        head = f"fold {model.row['voice_id']} into {model.pick['voice_id']}?"
    else:
        head = ""
    state = model.player.state()
    if state:
        now = f"[clip {state['index'] + 1}/{state['total']} of {state['voice_id']}: {state['what']}]"
        if state["error"]:
            now = f"[player: {state['error']}]"
    else:
        now = ""
    relabel = model.relabeler.progress() if model.relabeler else ""
    working = f"[{model.worker.label}...]" if model.busy else ""
    return "  ".join(part for part in (head, working, now, relabel) if part)


# ---- keys: curses codes to tokens (pure, so Alt+M is testable) ---------------------------------------

CHAR_KEYS = {"\n": "ENTER", "\r": "ENTER", "\t": "TAB", "\x7f": "BACKSPACE", "\b": "BACKSPACE",
             "\x15": "CTRL_U", "\x17": "CTRL_W", "\x03": "CTRL_C", "\x11": "CTRL_Q", "\x12": "CTRL_R",
             "\x0e": "CTRL_N"}


def decode_key(first, read_more: Callable[[], object]) -> str | None:
    """A token from what curses returned. `first` is a str (character) or int (curses key code);
    `read_more()` returns the next pending character or None, and is only asked after ESC: ESC alone
    is cancel, ESC then `m` is Alt+M, and any other Alt chord is ignored."""
    import curses

    if isinstance(first, int):
        return {curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN", curses.KEY_PPAGE: "PGUP", curses.KEY_NPAGE: "PGDN",
                curses.KEY_HOME: "HOME", curses.KEY_END: "END", curses.KEY_BACKSPACE: "BACKSPACE",
                curses.KEY_ENTER: "ENTER", curses.KEY_RESIZE: "RESIZE"}.get(first)
    if first == "\x1b":
        more = read_more()
        if more is None:
            return "ESC"
        if more in ("[", "O"):
            # The start of an arrow/function key whose tail was slow (laggy ssh/mosh): ncurses gave us a
            # bare ESC. Swallow the rest, or its final letter would be typed into the name.
            for _ in range(8):
                tail = read_more()
                if not isinstance(tail, str) or not tail:      # nothing more pending, or a curses key code
                    break
                if more == "O" or ("@" <= tail <= "~" and tail != "["):   # SS3 is one byte; CSI ends at its final byte
                    break
            return None
        return "ALT_M" if more in ("m", "M") else None
    if first in CHAR_KEYS:
        return CHAR_KEYS[first]
    return first if len(first) == 1 and first.isprintable() else None


# ---- the curses view -------------------------------------------------------------------------------

class StderrCapture:
    """While the screen is up, file descriptor 2 goes to an anonymous temp file; `text` has what was written.

    The relabel runs the real passes in this process, and a pass that fails logs a warning to stderr. Printed
    onto a curses screen it scribbles over the table and stays there (curses only repaints what it thinks changed).
    Redirecting fd 2, not `sys.stderr`, also catches `logging.lastResort` and anything a child inherits."""

    def __init__(self):
        self.text = ""
        self._saved = None
        self._file = None

    def __enter__(self):
        try:
            self._file = tempfile.TemporaryFile(mode="w+b")
            sys.stderr.flush()
            self._saved = os.dup(2)
            os.dup2(self._file.fileno(), 2)
        except OSError:
            self._saved = None            # no capture: the screen may get scribbled on, but the tool still works
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            try:
                sys.stderr.flush()
            except (OSError, ValueError):
                pass
            os.dup2(self._saved, 2)
            os.close(self._saved)
            self._saved = None
            self._file.seek(0)
            self.text = self._file.read().decode("utf-8", errors="replace")
        if self._file is not None:
            self._file.close()
        return False

    def lines(self, limit: int = 40) -> list[str]:
        lines = [line for line in self.text.splitlines() if line.strip()]
        return lines[-limit:]


class EscapeReader:
    """`read_more` for `decode_key`: how long to wait for the byte after an ESC, and for each byte after that.

    ESC alone is cancel, so the first wait is the price of a lone Esc (this plus ncurses' ESCDELAY). Once
    ESC is followed by `[` or `O` the key is known to be an arrow/function key whose tail is merely late, so
    the waits for its remaining bytes can be longer. Measured through a pty (docs/evidence/heyma-19/
    validation-2026-11-13): with 80 ms for both, a gap of 100 ms between the bytes of ESC [ B typed the B into
    the NAME field."""
    FIRST_MS = 150      # after ESC: Alt+M's `m`, or the `[`/`O` of a late arrow key
    TAIL_MS = 250       # after that: each later byte of a late arrow/function key
    IDLE_MS = 100       # the screen loop's own wake-up

    def __init__(self, stdscr, error):
        self.stdscr, self.error, self.calls = stdscr, error, 0

    def reset(self) -> None:
        """A new key starts: the next wait is the first one again."""
        self.calls = 0

    def __call__(self):
        self.stdscr.timeout(self.FIRST_MS if self.calls == 0 else self.TAIL_MS)
        self.calls += 1
        try:
            return self.stdscr.get_wch()
        except self.error:
            return None
        finally:
            self.stdscr.timeout(self.IDLE_MS)


def run_curses(model: Model) -> None:
    import curses

    os.environ["ESCDELAY"] = "50"    # ncurses waits this long (ms) after ESC to see whether it starts a key sequence

    def main(stdscr):
        curses.raw()                 # Ctrl+C, Ctrl+Q arrive as keys: we stop the player and restore the terminal ourselves
        curses.curs_set(0)
        stdscr.keypad(True)
        attrs = {"row": 0, "header": curses.A_BOLD | curses.A_UNDERLINE, "cursor": curses.A_REVERSE,
                 "edit": curses.A_REVERSE | curses.A_BOLD, "pick": curses.A_REVERSE | curses.A_BOLD,
                 "dim": curses.A_DIM, "play": curses.A_BOLD, "status": 0, "help": curses.A_DIM}

        more = EscapeReader(stdscr, curses.error)

        stdscr.timeout(100)          # wakes the screen ten times a second: clip counter, relabel progress, finished registry changes
        while not model.quit:
            model.tick()
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            for y, (text, style) in enumerate(layout(model, width, height)):
                try:
                    stdscr.addstr(y, 0, text[:max(0, width - 1)], attrs.get(style, 0))
                except curses.error:
                    pass             # a corner write on a tiny terminal; never worth dying for
            stdscr.refresh()
            try:
                first = stdscr.get_wch()
            except curses.error:
                continue             # timeout: redraw
            except KeyboardInterrupt:
                model.quit = True
                break
            more.reset()
            key = decode_key(first, more)
            if key == "RESIZE":
                continue             # the next pass reads the new size
            model.handle_key(key)

    capture = StderrCapture()
    try:
        with capture:
            curses.wrapper(main)
    finally:
        model.shutdown()
        shown = capture.lines()
        if shown:       # the terminal is restored: say what the passes printed while the screen was up
            print("messages written while the screen was up:", file=sys.stderr)
            for line in shown:
                print(f"  {line}", file=sys.stderr)


def finish(relabeler: Relabeler | None, out=print, model: Model | None = None) -> int:
    """After the screen is gone: let a registry change in flight land, let a running relabel finish, print
    its summary. Ctrl+C asks the relabel to stop after the transcript in flight (a second one abandons that
    transcript too); whatever was not done is left for `wax voices sync`. 1 when anything failed or was left."""
    if model is not None:
        try:
            if model.busy:
                out(f"{model.worker.label} is waiting on the voice registry; letting it land (Ctrl+C abandons it)")
            model.drain()
        except KeyboardInterrupt:
            out("a name or merge was still in flight and is abandoned; check `wax voices list`, then `wax voices sync`")
            if relabeler is not None:
                relabeler.stop()
            return 1
    if relabeler is None:
        return 0
    try:
        shown = ""
        while not relabeler.wait(0.5):
            now = relabeler.progress()
            if now != shown:
                out(f"  {now}   (Ctrl+C stops after the current transcript)")
                shown = now
    except KeyboardInterrupt:
        relabeler.stop()
        out("stopping after the transcript in flight (Ctrl+C again abandons it)")
        try:
            while not relabeler.wait(0.5):
                pass
        except KeyboardInterrupt:
            out("abandoned the transcript in flight; rerun it with `wax voices sync`")
            return 1
    for text in relabeler.summary():
        out(text)
    return 1 if relabeler.failed() or (relabeler.stopped and relabeler.remaining()) else 0


def main(*, no_sync: bool = False, no_play: bool = False) -> int:
    import sys

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("wax voices tui needs a terminal (use `wax voices list|name|merge` in scripts)", file=sys.stderr)
        return 2
    conn = voices.connect()
    relabeler = None if no_sync else Relabeler()
    model = Model(conn, make_player(no_play=no_play), relabeler, worker=ThreadWorker())
    crashed = 0
    try:
        run_curses(model)
    except Exception as exc:         # the terminal is already restored by curses.wrapper; still land what is in flight
        print(f"wax voices tui: the screen failed: {exc!r}", file=sys.stderr)
        crashed = 1
    return finish(relabeler, model=model) or crashed
