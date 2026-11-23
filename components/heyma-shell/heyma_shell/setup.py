"""`heyma setup`: per-product setup steps (story S4's skeleton; Voxxy is the first step).

Each step calls only its product's own installer or setup command and records
what this host runs in ~/.config/heyma/host.toml (the joining-HeyMa contract,
docs/heyma-shell.md point 4). A product with no step yet is simply not listed.

host.toml holds one table per product, scalar values only, never a secret:

    [voxxy]
    role = "client"
    url = "https://vox.delo.sh"
    key = "ok"
    speak_here = false
    sink = "big-chungus"
    configured_at = "2026-11-23T19:10:00Z"
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .adapters import Context, voxxy_command

STEP_TIMEOUT_S = 120.0


def host_toml_path(home: Path) -> Path:
    return home / ".config" / "heyma" / "host.toml"


def read_host(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))  # a JSON string is a valid TOML basic string


def write_host(path: Path, doc: dict) -> None:
    lines = ["# Written by `heyma setup`: what this host runs. One table per product; no secrets."]
    for table in sorted(doc):
        values = doc[table]
        if not isinstance(values, dict):
            continue
        lines += ["", f"[{table}]"]
        lines += [f"{key} = {_toml_value(v)}" for key, v in values.items() if v is not None]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)


@dataclass
class Options:
    url: str | None = None
    key_ref: str | None = None
    speak_here: bool | None = None
    extra: dict = field(default_factory=dict)


def voxxy_step(ctx: Context, opts: Options) -> dict:
    """Client role: `voxxy setup --json`, which writes Voxxy's own config and proves the key."""
    command = voxxy_command(ctx)
    if command is None:
        return {"product": "voxxy", "exit": 3, "detail":
                "voxxy CLI not installed: curl -fsSL https://vox.delo.sh/install.sh | sh", "host": None}
    argv = command + ["setup", "--json"]
    if opts.url:
        argv += ["--url", opts.url]
    key_ref = opts.key_ref or ctx.environ.get("HEYMA_VOXXY_KEY_REF")
    if key_ref:
        argv += ["--key-ref", key_ref]
    if opts.speak_here is not None:
        argv.append("--speak-here" if opts.speak_here else "--no-speak-here")
    result = ctx.run(argv, STEP_TIMEOUT_S)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        report = None
    if not isinstance(report, dict):
        error = (result.stderr or result.stdout or f"exit {result.rc}").strip().splitlines()
        return {"product": "voxxy", "exit": 3, "detail": f"voxxy setup failed: {error[0][:160] if error else ''}",
                "host": None}
    speaker = report.get("speaker") or {}
    host = {"role": "client", "url": report.get("url"), "key": report.get("key"),
            "speak_here": bool(speaker.get("active")), "sink": speaker.get("key")}
    lines = [f"{s['state']}: {s['name']}: {s['detail']}" for s in report.get("steps") or []]
    return {"product": "voxxy", "exit": int(report.get("exit", result.rc)), "detail": "; ".join(lines),
            "host": host, "steps": report.get("steps") or []}


STEPS: dict[str, Callable[[Context, Options], dict]] = {
    "voxxy": voxxy_step,
}


def run(products: list[str], opts: Options, ctx: Context | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    ctx = ctx or Context()
    wanted = products or list(STEPS)
    unknown = [p for p in wanted if p not in STEPS]
    if unknown:
        raise ValueError(f"no setup step for {', '.join(unknown)} (have: {', '.join(STEPS)})")
    path = host_toml_path(ctx.home)
    host = read_host(path)
    results = []
    for product in wanted:
        outcome = STEPS[product](ctx, opts)
        if outcome.get("host"):
            stamp = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
            host[product] = {**(host.get(product) or {}), **outcome["host"], "configured_at": stamp}
        results.append(outcome)
    if any(r.get("host") for r in results):
        write_host(path, host)
    return {"schema": "heyma.setup.v1", "host_toml": str(path), "results": results,
            "exit": max((r["exit"] for r in results), default=0)}


def render_text(doc: dict) -> str:
    out = []
    for r in doc["results"]:
        word = {0: "ok", 2: "WARN"}.get(r["exit"], "FAIL")
        out.append(f"{r['product']:<11} {word:<5} {r['detail']}")
    out.append(f"host: {doc['host_toml']}")
    return "\n".join(out)
