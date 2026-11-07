"""Read-only, project-aware shortcuts over the versioned transcript store.

The project key comes only from the canonical project_ids metadata used by
browse and enrichment; never infer a project from a title or transcript text.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .. import pjregistry
from . import browse, config, search

UTC = dt.timezone.utc
SCHEMA = 1


class TxError(ValueError):
    """A user-facing lookup or argument failure (not a store outage)."""


def bounds(window: str | None, now: dt.datetime | None = None) -> tuple[dt.datetime | None, dt.datetime]:
    """UTC query bounds from local *calendar* midnights, including DST transitions."""
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    local = now.astimezone(config.local_tz())
    day = local.date()
    if window == "yesterday":
        start, end = day - dt.timedelta(days=1), day
    elif window == "today":
        start, end = day, None
    elif window == "week":
        start, end = day - dt.timedelta(days=day.weekday()), None
    elif window is None:
        return None, now
    else:
        raise ValueError(f"unknown time window: {window}")
    midnight = lambda date: dt.datetime.combine(date, dt.time.min, tzinfo=config.local_tz()).astimezone(UTC)
    return midnight(start), midnight(end) if end else now


def _resolve(query: str, projects: list[dict[str, Any]]) -> str:
    """Canonical IDs win over all display names and spoken/repository aliases."""
    needle = query.strip().casefold()
    if not needle:
        raise TxError("project must not be empty")
    ids = {str(p["project_id"]).casefold(): str(p["project_id"]) for p in projects}
    if needle in ids:
        return ids[needle]

    # Consult registry IDs before accepting a display name: a historical name
    # may collide with a newer canonical ID that has no transcripts yet.
    try:
        candidates = pjregistry.candidates(pjregistry.fetch_registry())
    except pjregistry.RegistryError as exc:
        raise TxError(f"cannot resolve project {query!r}: {exc.reason_code}: {exc}") from None
    direct = [c["project_id"] for c in candidates if c["project_id"].casefold() == needle]
    if direct:
        return direct[0]
    try:
        hints = pjregistry.load_aliases()
    except pjregistry.RegistryError as exc:
        raise TxError(f"cannot resolve project {query!r}: {exc.reason_code}: {exc}") from None
    matches = {str(p["project_id"]) for p in projects if str(p["name"]).casefold() == needle}
    for candidate in candidates:
        hinted = pjregistry.hinted(candidate, hints)
        if needle in {hinted["name"].casefold(), *(a.casefold() for a in hinted["aliases"])}:
            matches.add(candidate["project_id"])
    if len(matches) == 1:
        return next(iter(matches))
    if matches:
        raise TxError(f"ambiguous project {query!r}; candidates: {', '.join(sorted(matches))}")
    known = sorted({*ids.values(), *(c["project_id"] for c in candidates)})
    raise TxError(f"unknown project {query!r}; candidates: {', '.join(known)}")


def run(*, project: str | None = None, window: str | None = None, listing: bool = False,
        limit: int = browse.DEFAULT_LIMIT, raw: bool = False, include_hermes: bool = False,
        now: dt.datetime | None = None) -> dict[str, Any]:
    """Browse metadata first, optionally fetch the selected head's content."""
    if limit < 1 or limit > browse.MAX_LIMIT:
        raise TxError(f"--limit must be 1..{browse.MAX_LIMIT}")
    if raw and (listing or window):
        raise TxError("--raw is only available for latest transcript content")
    listing = listing or window is not None
    since, until = bounds(window, now)
    # Project catalog is derived from the same store metadata as browse filters.
    # The unfiltered catalog permits canonical stored IDs without a registry call.
    resolved = None
    if project is not None:
        if not project.strip():
            raise TxError("project must not be empty")
        catalog = browse.run(limit=1, include_hermes=include_hermes)
        resolved = _resolve(project, catalog["projects"])
    result = browse.run(project=resolved, since=since, until=until, include_hermes=include_hermes,
                        limit=limit if listing else 1)
    rows = result["rows"]
    out: dict[str, Any] = {"schema": SCHEMA, "mode": "list" if listing else "latest",
                           "project": resolved, "timezone": config.local_tz_name(),
                           "since": since.isoformat() if since else None, "until": until.isoformat(),
                           "rows": rows, "truncated": listing and result["truncated"]}
    if not listing:
        if not rows:
            raise TxError(f"no transcript found{f' for project {project!r}' if project else ''}")
        shown = search.show(f"#{rows[0]['transcript_id']}", raw=raw)
        if not shown.get("version"):
            raise TxError(f"transcript #{rows[0]['transcript_id']} has no current version")
        out["transcript"] = shown
    return out
