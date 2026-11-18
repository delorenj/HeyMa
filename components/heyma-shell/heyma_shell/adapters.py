"""Product adapters: one function per HeyMa product, each calling ONLY that
product's existing CLI or endpoint, with a timeout.

Every adapter returns the same dict (`section()`):

    {id, installed, running, health, summary, actions, detail}

`health` is one of ok | warn | fail | off. `off` means "not degraded, just not
doing anything" (not installed, or deliberately disabled); `warn` and `fail`
are the two degraded grades. `detail` carries what the tray needs beyond the
contract (state strings, endpoints) and is optional for any other reader.

Nothing here imports Vinyl code. Importing Wax's pure helpers is allowed by the
repo rules and used once, for the Wax colour verdict, so the tray and the Wax
health row can never disagree about what "yellow" means.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import paths

HEALTH = ("ok", "warn", "fail", "off")
DEFAULT_PHONOGRAPH_URL = "https://phonograph.intellifornia.com"
CLI_TIMEOUT_S = 10.0
HTTP_TIMEOUT_S = 4.0
# waxd mirrors var/state.json every second; a mirror older than this is a dead daemon.
WAX_STATE_STALE_S = 30.0

PRODUCTS = ("wax", "vinyl", "telephony", "phonograph")


@dataclass
class Run:
    """The outcome of one CLI call. Never raises: a missing binary is rc 127."""

    rc: int
    stdout: str = ""
    stderr: str = ""


def run_cli(argv: list[str], timeout: float = CLI_TIMEOUT_S) -> Run:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return Run(127, "", f"not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return Run(124, "", f"timed out after {timeout:g}s: {' '.join(argv[:3])}")
    except OSError as exc:
        return Run(126, "", f"{type(exc).__name__}: {exc}")
    return Run(done.returncode, done.stdout, done.stderr)


def http_get(url: str, timeout: float = HTTP_TIMEOUT_S) -> tuple[int, str]:
    """(status, body); status 0 means no HTTP answer at all."""
    request = urllib.request.Request(url, headers={"User-Agent": "heyma-shell/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, ""


def read_json(path: Path) -> Any:
    """The parsed file, or None when missing or corrupt. Never raises."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


@dataclass
class Context:
    """Everything an adapter touches, injectable so tests need no real product."""

    root: Path = paths.ROOT
    home: Path = field(default_factory=Path.home)
    environ: dict = field(default_factory=lambda: dict(os.environ))
    run: Callable[..., Run] = run_cli
    http_get: Callable[..., tuple[int, str]] = http_get
    read_json: Callable[[Path], Any] = read_json
    exists: Callable[[Path], bool] = lambda p: Path(p).exists()
    now: Callable[[], float] = time.time

    @property
    def wax_cli(self) -> Path:
        return self.root / "bin" / "wax"

    @property
    def telephony_cli(self) -> Path:
        return self.root / "bin" / "telephony"

    @property
    def wax_state_path(self) -> Path:
        return paths.wax_root(self.environ, self.home) / "var" / "state.json"

    @property
    def vinyl_state_path(self) -> Path:
        return paths.vinyl_state_path(self.home)


def section(product: str, *, installed: bool, running: bool = False, health: str = "off",
            summary: str = "", actions: list[dict] | None = None,
            detail: dict | None = None) -> dict:
    assert health in HEALTH, health
    return {"id": product, "installed": installed, "running": running, "health": health,
            "summary": summary, "actions": list(actions or []), "detail": dict(detail or {})}


def _action(action_id: str, label: str) -> dict:
    return {"id": action_id, "label": label}


def _parse_json_object(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _first_error(result: Run) -> str:
    """The first line of what went wrong. stderr first: stdout may be a JSON body."""
    text = (result.stderr or "").strip() or (result.stdout or "").strip() or f"exit {result.rc}"
    return text.splitlines()[0][:160]


# -- wax --------------------------------------------------------------------

def _wax_bridge():
    """Wax's pure verdict helpers (`colour_for`, `flash_for`), or None.

    Imported from the checkout beside this one, never from PATH. The module
    needs no `gi` until a Tray is built, so this is safe under any python.
    """
    try:
        if str(paths.WAX_SRC) not in sys.path:
            sys.path.insert(0, str(paths.WAX_SRC))
        from wax import tray
        return tray
    except Exception:  # noqa: BLE001 - a missing wax degrades the row, nothing else
        return None


def snapshot_age_s(snapshot: Any, now_epoch: float) -> float | None:
    """Seconds since waxd wrote `updated_at`, or None when the field is unusable."""
    stamp = (snapshot or {}).get("updated_at") if isinstance(snapshot, dict) else None
    if not isinstance(stamp, str):
        return None
    try:
        written = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    return now_epoch - written.timestamp()


def wax_verdict(snapshot: Any, now_epoch: float) -> dict:
    """(colour, tip, recording, flash, fresh) for one Wax snapshot.

    A missing or stale mirror is `yellow` with `fresh` False: waxd being down is
    the loudest thing Wax can say that is not "recording".
    """
    age = snapshot_age_s(snapshot, now_epoch)
    fresh = isinstance(snapshot, dict) and age is not None and age <= WAX_STATE_STALE_S
    if not fresh:
        why = "no readable state" if not isinstance(snapshot, dict) else f"state {int(age or 0)}s old"
        return {"colour": "yellow", "tip": f"waxd: {why}\n(daemon down or starting)",
                "recording": False, "flash": None, "fresh": False}
    tray = _wax_bridge()
    if tray is None:
        stream = (snapshot.get("stream") or {}).get("state")
        return {"colour": "red" if stream == "recording" else "green",
                "tip": f"stream: {stream}", "recording": stream == "recording",
                "flash": None, "fresh": True}
    colour, tip = tray.colour_for(snapshot)
    return {"colour": colour, "tip": tip,
            "recording": (snapshot.get("stream") or {}).get("state") == "recording",
            "flash": tray.flash_for(snapshot), "fresh": True}


def wax_adapter(ctx: Context) -> dict:
    """Liveness from waxd's mirror; content from `wax --json status`.

    The CLI computes its snapshot itself and stamps `updated_at` with its own
    clock, so it answers just as freshly with waxd dead: it can never be the
    proof that the daemon is alive. Only the mirror waxd rewrites every second
    is. And `wax status` exits 2 for anything non-nominal, with its JSON intact,
    so the exit code is a health verdict here, not a failure to answer.
    """
    actions = [_action("rec.toggle", "Toggle recording"), _action("open.inbox", "Open inbox"),
               _action("open.settings", "Settings")]
    if not ctx.exists(ctx.wax_cli):
        return section("wax", installed=False, summary="not installed", actions=[])
    mirror = ctx.read_json(ctx.wax_state_path)
    age = snapshot_age_s(mirror, ctx.now())
    if not isinstance(mirror, dict) or age is None or age > WAX_STATE_STALE_S:
        why = ("no readable var/state.json" if not isinstance(mirror, dict)
               else f"var/state.json is {int(age)}s old" if age is not None
               else "var/state.json has no updated_at")
        return section("wax", installed=True, running=False, health="fail",
                       summary=f"waxd not running ({why})", actions=actions,
                       detail={"colour": "yellow", "recording": False, "error": why})
    result = ctx.run([str(ctx.wax_cli), "--json", "status"], CLI_TIMEOUT_S)
    cli_snapshot = _parse_json_object(result.stdout) if result.rc in (0, 2) else None
    if cli_snapshot is not None:
        # The CLI's own stamp says nothing about waxd; carry the mirror's.
        snapshot = {**cli_snapshot, "updated_at": mirror.get("updated_at")}
    else:
        snapshot = mirror  # the CLI could not answer; the live mirror still can
    verdict = wax_verdict(snapshot, ctx.now())
    stream = (snapshot.get("stream") or {}).get("state")
    inbox = (snapshot.get("inbox") or {}).get("state")
    degraded = verdict["colour"] == "yellow"
    summary = "recording" if verdict["recording"] else f"stream {stream} · inbox {inbox}"
    if degraded:
        summary += " · " + verdict["tip"].splitlines()[-1]
    detail = {"colour": verdict["colour"], "recording": verdict["recording"],
              "stream": stream, "inbox": inbox}
    if cli_snapshot is None:
        detail["cli_error"] = _first_error(result)
    return section("wax", installed=True, running=True, health="warn" if degraded else "ok",
                   summary=summary, actions=actions, detail=detail)


# -- vinyl ------------------------------------------------------------------

def vinyl_command(ctx: Context) -> list[str] | None:
    """The vinyl CLI, resolved like components/vinyl/bin/vinyl-tray's `_vinyl_cmd`.

    VINYL_BIN, then /usr/bin/vinyl, then ~/.local/bin/vinyl. Never PATH: mise
    and uv put a python first on PATH with no transcribe_cpp, and the failure
    looks like a broken daemon.
    """
    override = ctx.environ.get("VINYL_BIN")
    if override:
        return shlex.split(override)
    for candidate in (Path("/usr/bin/vinyl"), ctx.home / ".local" / "bin" / "vinyl"):
        if ctx.exists(candidate):
            return [str(candidate)]
    return None


def vinyl_settings_command(ctx: Context) -> list[str] | None:
    """`vinyl-settings` (a sibling UI), launched with the system GTK interpreter."""
    override = ctx.environ.get("VINYL_SETTINGS")
    if override:
        return shlex.split(override)
    for candidate in (Path("/usr/lib/vinyl/bin/vinyl-settings"),
                      ctx.root / "components" / "vinyl" / "bin" / "vinyl-settings"):
        if ctx.exists(candidate):
            return ["/usr/bin/python3", str(candidate)]
    return None


VINYL_HOT_STATES = ("listening", "stopping")


def vinyl_verdict(snapshot: Any) -> dict:
    """(colour, tip, listening) for a Vinyl state.json, as vinyl-tray's `colour_for` does."""
    if not isinstance(snapshot, dict):
        return {"colour": "yellow", "tip": "vinyld: no readable state\n(daemon down or starting)",
                "listening": False, "error": "no readable state"}
    state = snapshot.get("state", "idle")
    error = snapshot.get("last_error") or snapshot.get("trace_error") or ""
    tip = f"state: {state}\nsegments: {snapshot.get('segments_decoded', 0)}"
    if snapshot.get("last_text"):
        tip += f"\nlast: {str(snapshot['last_text'])[:80]}"
    if error:
        tip += f"\nerror: {str(error)[:80]}"
    if state in VINYL_HOT_STATES:
        return {"colour": "red", "tip": tip, "listening": True, "error": str(error)}
    return {"colour": "yellow" if error else "green", "tip": tip, "listening": False,
            "error": str(error)}


def vinyl_adapter(ctx: Context) -> dict:
    command = vinyl_command(ctx)
    if command is None:
        return section("vinyl", installed=False, summary="not installed")
    actions = [_action("dictation.toggle", "Toggle dictation"),
               _action("trace.copy", "Copy last trace reference")]
    result = ctx.run(command + ["status"], 8.0)
    status = _parse_json_object(result.stdout) if result.rc == 0 else None
    mirror = ctx.read_json(ctx.vinyl_state_path)
    mirror = mirror if isinstance(mirror, dict) else {}
    endpoint = str(mirror.get("server") or mirror.get("peer") or "")
    if status is None:
        return section("vinyl", installed=True, running=False, health="fail",
                       summary=f"vinyl daemon not answering: {_first_error(result)}",
                       actions=actions,
                       detail={"state": None, "endpoint": endpoint, "mode": mirror.get("mode")})
    verdict = vinyl_verdict(status)
    state = status.get("state", "idle")
    summary = "dictating" if verdict["listening"] else f"{state} · {status.get('segments_decoded', 0)} segments"
    if endpoint:
        summary += f" · {endpoint}"
    if verdict["error"]:
        summary += f" · error: {verdict['error'][:60]}"
    return section("vinyl", installed=True, running=True,
                   health="warn" if verdict["colour"] == "yellow" else "ok",
                   summary=summary, actions=actions,
                   detail={"state": state, "endpoint": endpoint, "mode": mirror.get("mode"),
                           "listening": verdict["listening"],
                           "trace_ref": status.get("trace_ref") or ""})


# -- telephony --------------------------------------------------------------

def telephony_adapter(ctx: Context) -> dict:
    if not ctx.exists(ctx.telephony_cli):
        return section("telephony", installed=False, summary="not installed")
    actions = [_action("telephony.silence", "Silence local ringer"),
               _action("open.telephony", "Open Telephony page")]
    result = ctx.run([str(ctx.telephony_cli), "--json", "status"], CLI_TIMEOUT_S)
    status = _parse_json_object(result.stdout) if result.rc == 0 else None
    if status is None:
        return section("telephony", installed=True, running=False, health="fail",
                       summary=f"telephony status failed: {_first_error(result)}", actions=actions)
    mode = str(status.get("mode") or "unknown")
    ring = bool(status.get("ring_enabled"))
    if mode == "disabled":
        health, summary = "off", "disabled (calls still ring and record normally)"
    else:
        health = "ok"
        summary = f"{mode} · ring {'on' if ring else 'off'}"
        if not status.get("verified_ingress"):
            summary += " · ingress unverified"
    return section("telephony", installed=True, running=True, health=health, summary=summary,
                   actions=actions, detail={"mode": mode, "ring_enabled": ring})


# -- phonograph ---------------------------------------------------------------

def phonograph_base_url(ctx: Context) -> str:
    base = (ctx.environ.get("HEYMA_PHONOGRAPH_URL") or DEFAULT_PHONOGRAPH_URL).strip().rstrip("/")
    return base[: -len("/healthz")] if base.endswith("/healthz") else base


def phonograph_adapter(ctx: Context) -> dict:
    """Nothing installs on a client: 'installed' means HeyMa is pointed at a URL."""
    if ctx.environ.get("HEYMA_PHONOGRAPH", "").strip().lower() in ("0", "off", "false", "no"):
        return section("phonograph", installed=False, summary="disabled (HEYMA_PHONOGRAPH=off)")
    base = phonograph_base_url(ctx)
    actions = [_action("open.phonograph", "Open Phonograph")]
    status, body = ctx.http_get(base + "/healthz", HTTP_TIMEOUT_S)
    payload = _parse_json_object(body) or {}
    if status == 200 and payload.get("ok", True) is not False:
        version = payload.get("version")
        return section("phonograph", installed=True, running=True, health="ok",
                       summary=f"up{' · v' + str(version) if version else ''} · {base}",
                       actions=actions, detail={"url": base, "version": version})
    reason = f"HTTP {status}" if status else "unreachable"
    return section("phonograph", installed=True, running=False, health="fail",
                   summary=f"{reason} · {base}", actions=actions, detail={"url": base})


ADAPTERS: dict[str, Callable[[Context], dict]] = {
    "wax": wax_adapter,
    "vinyl": vinyl_adapter,
    "telephony": telephony_adapter,
    "phonograph": phonograph_adapter,
}


def quick_installed(ctx: Context) -> dict[str, bool]:
    """Cheap, no subprocess: which products are present on this host.

    The tray draws its first menu from this while the first full collect runs.
    """
    return {
        "wax": ctx.exists(ctx.wax_cli),
        "vinyl": vinyl_command(ctx) is not None,
        "telephony": ctx.exists(ctx.telephony_cli),
        "phonograph": ctx.environ.get("HEYMA_PHONOGRAPH", "").strip().lower()
        not in ("0", "off", "false", "no"),
    }
