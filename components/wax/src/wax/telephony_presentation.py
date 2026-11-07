"""GI-free Telephony presentation and the tray's opt-in ringtone state machine.

Only the waxd snapshot mirror is read by the tray. This module never reads the
ledger or contacts a provider; callers inject time and playback in tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any, Callable

from . import component

TELEPHONY_CLI = component.ROOT.parents[1] / "bin" / "telephony"
SNAPSHOT_SCHEMA = "heyma.telephony.snapshot.v1"
SCHEMA = SNAPSHOT_SCHEMA
STALE_SECONDS = 10
RING_MAX_AGE_SECONDS = 15
RING_PLAYBACK_SECONDS = 4
RING_REPEAT_SECONDS = 4
RING_SOUND = Path("/usr/share/sounds/freedesktop/stereo/phone-incoming-call.oga")
RING_BACKEND = Path("/usr/bin/paplay")


def playback_unavailable(sound: Path = RING_SOUND, backend: Path = RING_BACKEND) -> str:
    """Explain why an opted-in ringtone cannot make sound on this desktop."""
    if not sound.is_file():
        return f"ringtone file missing: {sound}"
    if not backend.is_file():
        return f"playback backend missing: {backend}"
    return ""


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def snapshot_fresh(snapshot: Any, now: datetime) -> bool:
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        return False
    stamp = _timestamp(snapshot.get("generated_at"))
    return bool(stamp and -2 <= (now - stamp).total_seconds() <= STALE_SECONDS
                and snapshot.get("health") == "ok")


def presentation(snapshot: Any, now: datetime) -> dict[str, Any]:
    """Shape the mirror/CLI response; bad or stale data is never an empty success."""
    if not snapshot_fresh(snapshot, now):
        return {"banner": "Telephony snapshot unavailable or stale — no live call status",
                "available": False, "ring_enabled": False, "calls": []}
    mode = snapshot.get("mode")
    banner = ("Shadow only — decisions are not enforced; calls still ring and record"
              if mode == "shadow" else "Disabled — no call screening or ringtone")
    calls = []
    for call in snapshot.get("calls", []) if isinstance(snapshot.get("calls"), list) else []:
        if not isinstance(call, dict):
            continue
        screening = call.get("screening") or {}
        processing = call.get("processing") or {}
        if not isinstance(screening, dict):
            screening = {}
        if not isinstance(processing, dict):
            processing = {}
        decision = screening.get("decision")
        verdict = ("Would block — not enforced" if decision == "would_block"
                   else "Would allow — not enforced" if decision == "allow" else "Not assessed")
        call_state = call.get("call_state")
        phone = {"ringing": "Ringing", "connected": "Connected", "ended": "Ended"}.get(
            call_state, "Call state unknown")
        process = str(processing.get("state") or "unknown").replace("_", " ").capitalize()
        progress = f"{processing.get('completed_passes') or 0}/{processing.get('total_passes') or 0} passes"
        failed = processing.get("failed_passes") or 0
        if failed:
            progress += f" · {failed} failed"
        warnings = [str(w) for w in (call.get("warnings") or []) if w]
        if decision not in ("allow", "would_block"):
            warnings.append("No matched shadow screening receipt for this session")
        calls.append({"session_id": str(call.get("session_id") or ""),
                      "title": str(call.get("remote_name") or call.get("from_number") or "Unknown caller"),
                      "direction": str(call.get("direction") or "unknown"),
                      "from_number": str(call.get("from_number") or "unknown"),
                      "to_number": str(call.get("to_number") or "unknown"),
                      "phone": phone, "screening": verdict,
                      "reason": str(screening.get("reason") or ""),
                      "processing": f"{process} · {progress}",
                      "warnings": warnings})
    return {"banner": banner, "available": True,
            "ring_enabled": bool(snapshot.get("ring_enabled")) if mode == "shadow" else False,
            "calls": calls}


class RingPlayer:
    """At most one bounded playback child, stopped on any loss of eligibility."""

    def __init__(self, sound: Path = RING_SOUND, popen: Callable = subprocess.Popen):
        self.sound = sound
        self.popen = popen
        self.child = None
        self.started = None

    def tick(self, now: datetime, active: bool) -> None:
        if self.child is not None:
            try:
                if not active or (now - self.started).total_seconds() >= RING_PLAYBACK_SECONDS:
                    if self.child.poll() is None:
                        self.child.terminate()
                        try:
                            self.child.wait(timeout=0.2)
                        except subprocess.TimeoutExpired:
                            self.child.kill()
                            try:
                                self.child.wait(timeout=0.2)
                            except subprocess.TimeoutExpired:
                                pass
                    self.child = None
                elif self.child.poll() is not None:
                    self.child = None
            except (OSError, subprocess.SubprocessError):
                self.child = None
        if active and self.child is None and self.sound.is_file():
            try:
                self.child = self.popen(["/usr/bin/paplay", str(self.sound)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.started = now
            except (OSError, subprocess.SubprocessError):
                self.child = None

    def stop(self) -> None:
        self.tick(datetime.now(timezone.utc), False)


class Ringer:
    """One tray-owned alert; startup/history/replay cannot turn into a new ring."""

    def __init__(self, player: RingPlayer, now: datetime):
        self.player = player
        self.launched_at = now
        self.initialized = False
        self.seen: set[str] = set()
        self.active_id: str | None = None
        self.last_sound: datetime | None = None
        self.silenced: set[str] = set()

    def silence(self) -> None:
        if self.active_id:
            self.silenced.add(self.active_id)
            self.seen.add(self.active_id)
        self.active_id = None
        self.player.stop()

    def tick(self, snapshot: Any, now: datetime) -> bool:
        calls = snapshot.get("calls") if isinstance(snapshot, dict) else None
        if not self.initialized:
            if isinstance(calls, list) and snapshot_fresh(snapshot, now):
                self.seen.update(str(c.get("session_id")) for c in calls if isinstance(c, dict))
                self.initialized = True
            self.player.tick(now, False)
            return False
        eligible = (snapshot_fresh(snapshot, now) and snapshot.get("mode") == "shadow"
                    and snapshot.get("ring_enabled") is True and isinstance(calls, list))
        if not eligible:
            if isinstance(calls, list):
                self.seen.update(str(c.get("session_id")) for c in calls
                                 if isinstance(c, dict) and c.get("session_id"))
            if self.active_id:
                self.seen.add(self.active_id)
            self.active_id = None
            self.player.tick(now, False)
            return False
        ringing = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            sid = call.get("session_id")
            if not isinstance(sid, str) or not sid:
                continue
            if call.get("call_state") != "ringing" or call.get("answered_at") or call.get("ended_at"):
                self.seen.add(sid)
                continue
            event_at = _timestamp(call.get("ring_event_at"))
            age = (now - event_at).total_seconds() if event_at else float("inf")
            if (call.get("direction") == "inbound" and sid not in self.seen
                    and event_at and event_at > self.launched_at
                    and -2 <= age <= RING_MAX_AGE_SECONDS):
                ringing.append(sid)
            elif sid != self.active_id:
                self.seen.add(sid)
        if self.active_id and self.active_id not in ringing:
            self.seen.add(self.active_id)
            self.player.tick(now, False)
            self.active_id = None
        if not self.active_id and ringing:
            self.active_id = ringing[0]
            self.last_sound = None
        active = bool(self.active_id and self.active_id not in self.silenced)
        if active and (self.last_sound is None or
                       (now - self.last_sound).total_seconds() >= RING_REPEAT_SECONDS):
            self.player.tick(now, True)
            self.last_sound = now
        elif not active:
            self.player.tick(now, False)
        return active
