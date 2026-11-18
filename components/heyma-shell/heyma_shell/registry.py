"""The component registry: `collect()` is `heyma status --json`."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import SCHEMA
from .adapters import ADAPTERS, PRODUCTS, Context, section


def _safe(product: str, ctx: Context) -> dict:
    """An adapter that raises is a failed section, never a failed registry."""
    try:
        return ADAPTERS[product](ctx)
    except Exception as exc:  # noqa: BLE001 - one product must not blind the rest
        return section(product, installed=True, running=False, health="fail",
                       summary=f"adapter error: {type(exc).__name__}: {exc}")


def collect(ctx: Context | None = None) -> dict:
    """Ask every product for its status, concurrently (each call has its own timeout)."""
    ctx = ctx or Context()
    with ThreadPoolExecutor(max_workers=len(PRODUCTS)) as pool:
        sections = list(pool.map(lambda product: _safe(product, ctx), PRODUCTS))
    return {"schema": SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sections": sections}


def by_id(doc: dict | None) -> dict[str, dict]:
    return {s["id"]: s for s in (doc or {}).get("sections", [])}


def exit_code(doc: dict) -> int:
    """`wax status`'s contract, for the whole client: 0 all nominal, 2 anything degraded.

    An always-0 status is how a week of 100% pass failure scripted as healthy;
    `heyma status && …` must not pass with waxd down or Phonograph unreachable.
    `off` is not degraded (not installed, or deliberately disabled).
    """
    return 2 if any(s.get("health") in ("warn", "fail") for s in (doc or {}).get("sections", [])) else 0


def render_text(doc: dict) -> str:
    """The human form of `heyma status`: one line per product."""
    lines = []
    for item in doc["sections"]:
        mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "off": "off "}[item["health"]]
        lines.append(f"{item['id']:<11} {mark}  {item['summary']}")
    return "\n".join(lines)
