"""Best-effort desktop feedback for long-running transcription work.

Also the home of the *failure* channel, and deliberately so: tray.py imports
`gi` at module scope and mise's python3 (3.11.13, first on PATH) has no gi, so a
worker thread that wanted to raise an alarm "through the tray" could not import
the module outside the daemon. Nothing below touches a display server.
"""

import logging
import shutil
import subprocess
import threading
import time

log = logging.getLogger("wax." + __name__.rsplit(".", 1)[-1])

# `failed` is the sound the machine owed you for a week. `complete` fires at the
# end of transcription — BEFORE the enrichment passes run — so eleven consecutive
# title-slug failures were each announced with an affirmative chime. dialog-error
# is present in /usr/share/sounds/freedesktop/stereo on this box (verified) and
# is the freedesktop-standard negative event.
SOUNDS = {"start": "message", "complete": "complete", "failed": "dialog-error"}

# A dead provider fails on EVERY item, so the interesting event is the FIRST
# failure of a given kind, not the fortieth. Notify once per distinct
# (slug, reason_code) and stay quiet until clear_stage_failure() re-arms it.
_NOTIFIED: set[tuple[str, str]] = set()
_NOTIFY_LOCK = threading.Lock()


ACTION_TIMEOUT_S = 120.0
# Ledger-only actions answer in well under a second; 120 s means "hung". Rerun
# passes runs the passes synchronously (ticketable allows 1800 s, wikification
# 900 s), so it is never killed: the tray just waits for the verdict.
LONG_ACTIONS = ("ep",)
_JSON_REASON_KEYS = ("evidence", "error", "reason", "detail", "message", "cause_code", "action")


def action_timeout(args) -> float | None:
    """Seconds before a `wax <args>` tray action counts as hung; None = never kill it."""
    return None if args and str(args[0]) in LONG_ACTIONS else ACTION_TIMEOUT_S


def _json_reason(text: str) -> str:
    import json
    try:
        doc = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(doc, dict):
        return ""
    for key in _JSON_REASON_KEYS:
        value = doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _action_error(result) -> str:
    """The line worth reading: stderr before stdout (stdout may be a JSON body).

    A traceback yields its LAST line (the exception), not its first `File …`
    frame; a JSON body yields its evidence/error field, not a lone `{`.
    """
    for text in (getattr(result, "stderr", "") or "", getattr(result, "stdout", "") or ""):
        text = text.strip()
        if not text:
            continue
        reason = _json_reason(text)
        if reason:
            return reason.splitlines()[0][:200]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if any(line.startswith("Traceback") for line in lines):
            return lines[-1][:200]
        for line in lines:
            if line.strip("{}[](),") and not line.startswith("Traceback"):
                return line[:200]
    return f"exit {getattr(result, 'returncode', '?')}"


def run_reporting(argv: list[str], *, what: str, on_failure=None, app: str = "Wax",
                  timeout: float = ACTION_TIMEOUT_S, runner=None,
                  notifier=None, wait: bool = False, shown=None) -> threading.Thread:
    """Run a tray action's CLI off the UI thread and SAY when it fails.

    Every tray menu action (Retry, Skip, Transcribe anyway, rerun passes, Clear
    completed) used to be a fire-and-forget Popen with its output discarded, so
    a refused `wax skip` or a crashed `wax retry` left a greyed row and nothing
    else. A non-zero exit, a timeout or a missing binary now raises a desktop
    notification naming the action and the CLI's own first error line, logs it,
    and calls `on_failure(message)` so the host can re-enable the row.
    """
    runner = runner or subprocess.run
    notifier = notifier or notify

    def work():
        try:
            result = runner(argv, capture_output=True, text=True, timeout=timeout,
                            stdin=subprocess.DEVNULL)
            if result.returncode == 0:
                log.info("tray action ok: %s", what)
                return
            message = _action_error(result)
        except subprocess.TimeoutExpired:
            message = f"no answer after {timeout:g}s"
        except OSError as e:
            message = f"{type(e).__name__}: {e}"
        log.warning("tray action failed: %s: %s", what, message)
        named = shown if shown is not None else argv
        notifier(f"{app}: {what} failed", f"{message}\n({' '.join(str(a) for a in named[1:])})",
                 app=app)
        if on_failure is not None:
            try:
                on_failure(message)
            except Exception:  # noqa: BLE001 - feedback must never take the tray down
                log.exception("tray action on_failure callback raised")

    thread = threading.Thread(target=work, daemon=True, name=f"tray-action-{what}")
    thread.start()
    if wait:
        thread.join()
    return thread


def own_scope(argv: list[str]) -> list[str]:
    """`argv` in its own transient systemd user scope, so it outlives the tray that started it.

    A tray action's child lives in the tray's cgroup (heyma-tray.service, or waxd.service for
    waxd's own tray), and both units use KillMode=control-group. On 2026-11-19 a tray "Recording
    toggle" stopped a 16-hour capture; finalizing 970 segments took a while, the tray was quit
    meanwhile, and the stop's finalizer (the `wax` CLI child) died with it: `finalizer_died`,
    recording blocked until a manual `wax rec salvage`. `systemd-run --scope` execs the command
    in the foreground (same pid, same exit code, same pipes), so the caller still waits and reads
    its output. Falls back to plain `argv` where there is no user manager to ask.
    """
    import os
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    tool = shutil.which("systemd-run")
    if not tool or not runtime or not os.path.exists(os.path.join(runtime, "systemd", "private")):
        return list(argv)
    return [tool, "--user", "--scope", "--quiet", "--collect", "--", *argv]


def run_wax_action(cli, args, *, on_failure=None, app: str = "Wax", scope=own_scope,
                   **kwargs) -> threading.Thread:
    """One tray menu action, `<cli> <args>`, with its label and timeout chosen here.

    Both trays (waxd's own and the HeyMa shell) call this, so a new action gets
    the same feedback, the same "never kill a long rerun" rule, and its own scope
    (quitting or restarting a tray never kills an action it started) in both.
    """
    args = [str(a) for a in args]
    argv = [str(cli), *args]
    return run_reporting(scope(argv), what=describe_action(args), on_failure=on_failure,
                         app=app, timeout=action_timeout(args), shown=argv, **kwargs)


def describe_action(args) -> str:
    """'Retry 89d99381a94d0eb5' style label for a `wax <args>` tray action."""
    verbs = {"retry": "Retry", "skip": "Skip", "ep": "Rerun passes",
             "queue": "Clear completed", "rec": "Recording toggle"}
    args = [str(a) for a in args]
    if not args:
        return "wax"
    verb = verbs.get(args[0], f"wax {args[0]}")
    if args[0] in ("retry", "skip") and len(args) > 1:
        return f"{verb} {args[1]}"
    if args[0] == "ep" and len(args) > 2:
        return f"{verb} for {args[2]}"
    return verb


def ding(event: str) -> bool:
    """Play a desktop event sound without ever blocking or failing the worker."""
    sound = SOUNDS.get(event, "message")
    player = shutil.which("canberra-gtk-play")
    if not player:
        return False
    try:
        subprocess.Popen(
            [player, "--id", sound, "--description", f"Wax transcription {event}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def notify(title: str, body: str, *, urgency: str = "critical", app: str = "Wax") -> bool:
    """Raise a desktop notification. Never raises: the caller is mid-failure.

    A swallowed notification is still logged, because a headless session is
    exactly where "nothing reached the desktop" must not also mean "nothing
    reached the journal".
    """
    if not shutil.which("notify-send"):
        log.warning("notify-send absent, unreported: %s — %s", title, body.replace("\n", " · "))
        return False
    try:
        subprocess.run(["notify-send", "-a", app, "-u", urgency, title, body], check=False)
    except OSError as e:
        log.warning("notify-send failed: %s: %s", type(e).__name__, e)
        return False
    log.info("notified (%s): %s", urgency, title)
    return True


# A muted input makes EVERY recording from that source silent, so the interesting event is the
# first silent one, not the third memo in a row. One notification per source until an audible
# recording from it re-arms the alarm (clear_silent_source) or SILENT_NOTIFY_REPEAT_S passes.
SILENT_NOTIFY_REPEAT_S = 6 * 3600
_SILENT_NOTIFIED: dict[str, float] = {}


def notify_silent_recording(source: str, body: str) -> bool:
    """'Wax: recording is silent' (normal urgency: it is a mic to check, not a Wax fault).

    Deduplicated per intake source. Returns True only when a notification was raised.
    """
    now = time.monotonic()
    with _NOTIFY_LOCK:
        last = _SILENT_NOTIFIED.get(source)
        if last is not None and now - last < SILENT_NOTIFY_REPEAT_S:
            log.info("silent recording from %s repeats, notification suppressed", source)
            return False
        _SILENT_NOTIFIED[source] = now
    log.warning("silent recording from %s: %s", source, body.replace("\n", " · "))
    return notify("Wax: recording is silent", body, urgency="normal")


def notify_quiet_recording(source: str, body: str) -> bool:
    """'Wax: recording input is very quiet': transcribed with gain, but the device level is wrong.

    Same per-source dedup as notify_silent_recording (its own key), normal urgency.
    """
    now = time.monotonic()
    key = f"quiet:{source}"
    with _NOTIFY_LOCK:
        last = _SILENT_NOTIFIED.get(key)
        if last is not None and now - last < SILENT_NOTIFY_REPEAT_S:
            log.info("quiet recording from %s repeats, notification suppressed", source)
            return False
        _SILENT_NOTIFIED[key] = now
    log.warning("quiet recording from %s: %s", source, body.replace("\n", " · "))
    return notify("Wax: recording input is very quiet", body, urgency="normal")


def clear_silent_source(source: str | None = None) -> None:
    """Re-arm the silent-recording alarm for a source (all when None)."""
    with _NOTIFY_LOCK:
        if source is None:
            _SILENT_NOTIFIED.clear()
        else:
            _SILENT_NOTIFIED.pop(source, None)


def notify_stage_failure(slug: str, reason_code: str, *, item: str = "",
                         detail: str = "") -> bool:
    """Announce a failed pipeline sub-stage on the desktop, once per kind.

    Returns True only when a notification was actually raised, so a caller can
    tell "told the user" from "already told them". `detail` is truncated to the
    same 300 chars contract D writes into the note.
    """
    key = (str(slug or "?"), str(reason_code or "unknown"))
    with _NOTIFY_LOCK:
        if key in _NOTIFIED:
            log.info("stage failure repeats, notification suppressed: slug=%s reason_code=%s",
                     key[0], key[1])
            return False
        _NOTIFIED.add(key)
    body = key[1]
    if item:
        body += f" · {item}"
    if detail:
        body += "\n" + detail.strip()[:300]
    log.warning("stage failed: slug=%s reason_code=%s item=%s", key[0], key[1], item or "-")
    return notify(f"Wax: {key[0]} failed", body)


def clear_stage_failure(slug: str | None = None) -> None:
    """Re-arm the desktop alarm after a recovery — all slugs when None.

    Without this a provider that breaks, gets fixed, and breaks again a month
    later would fail in total silence for the rest of the daemon's life.
    """
    with _NOTIFY_LOCK:
        if slug is None:
            _NOTIFIED.clear()
            return
        for key in [k for k in _NOTIFIED if k[0] == slug]:
            _NOTIFIED.discard(key)
