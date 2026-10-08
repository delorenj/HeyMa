"""The pjangler project registry, as the candidate list for project-aware passes.

Read over plain HTTP from the registry service (`GET /v1/registry`, a systemd
user unit bound to 127.0.0.1:8764), never through the `pj` CLI: waxd's PATH
has no mise shims, the CLI only worked through an accident of system node,
and the direct GET is ~13 ms against ~250 ms with typed failures instead of a
child's exit status.

A registry failure is a FAILURE (reason_code registry_unavailable,
registry_invalid or registry_misconfigured), never an empty candidate list:
every Wax sub-stage that degraded to "empty" instead of failing has run at
100% failure behind a green status for a week.

Records are projected to a few compact fields. Whole records never leave
this module: one of them (`intelliforia`) carries a `secrets` block.

The registry has no alias field, 17 of its 36 descriptions were empty on
2026-10-08, and ids like `px`, `bb` or `tonnybox` say nothing about the
project. Speech
recognition mangles names besides. config/project-aliases.yaml is the
Wax-side hint layer for that gap (spoken forms plus a one-line "about"),
until pjangler manifests carry aliases themselves.
"""

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import yaml

from . import component

# The service binds 127.0.0.1 only, and `localhost` may try ::1 first.
DEFAULT_URL = "http://127.0.0.1:8764"
ALIASES_FILE = component.CONFIG / "project-aliases.yaml"
MAX_REGISTRY_BYTES = 16 * 1024 * 1024
# pjangler's normalizeProjectId: lowercase, digits, inner hyphens.
ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
# Never route loopback through a proxy that happens to be in the environment.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class RegistryError(Exception):
    """reason_code: registry_unavailable | registry_invalid | registry_misconfigured | aliases_invalid"""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def registry_url() -> str:
    """The registry location, with pjangler's own precedence.

    pjangler's registryClient reads PJ_PROJECT_REGISTRY before PJ_REGISTRY_URL;
    a pass must resolve the same registry the `pj` CLI would. A non-HTTP value
    is a YAML fixture path to pjangler, which this client does not read.
    """
    url = (os.environ.get("PJ_PROJECT_REGISTRY") or os.environ.get("PJ_REGISTRY_URL")
           or DEFAULT_URL).strip().rstrip("/")
    if not url.lower().startswith(("http://", "https://")):
        raise RegistryError("registry_misconfigured", f"registry location is not an HTTP URL: {url!r}")
    return url


def _error_code(exc: urllib.error.HTTPError) -> str:
    """The service's own `{error, code}` classification, never the raw body."""
    try:
        body = json.loads(exc.read(4096))
    except (OSError, ValueError, http.client.HTTPException):
        return ""
    code = body.get("code") if isinstance(body, dict) else None
    return f" ({code})" if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else ""


def fetch_registry(url: Optional[str] = None, *, timeout: float = 5.0, attempts: int = 3,
                   backoff: float = 0.5) -> dict[str, Any]:
    """The full `GET /v1/registry` snapshot, validated, with bounded retries.

    Worst case is ~17s at the defaults, under the service's own 30s request
    timeout. The service is a separate user unit that 503s whenever Postgres
    hiccups and waxd does not order itself after it, so transient trouble is
    retried; a 4xx is a real answer and is not.
    """
    endpoint = (url or registry_url()).rstrip("/") + "/v1/registry"
    last: Optional[RegistryError] = None
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(backoff * (2 ** (attempt - 1)))
        try:
            request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
            with _OPENER.open(request, timeout=timeout) as response:
                body = response.read(MAX_REGISTRY_BYTES + 1)
            if len(body) > MAX_REGISTRY_BYTES:
                raise RegistryError("registry_invalid", f"{endpoint}: response exceeds 16 MiB")
            data = json.loads(body)
        except urllib.error.HTTPError as exc:
            last = RegistryError("registry_unavailable", f"HTTP {exc.code} from {endpoint}{_error_code(exc)}")
            if exc.code < 500:
                raise last from None
            continue
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            last = RegistryError("registry_unavailable", f"{endpoint}: {reason}")
            continue
        except (ValueError, UnicodeDecodeError) as exc:
            raise RegistryError("registry_invalid", f"{endpoint}: not JSON ({type(exc).__name__})") from None
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or not isinstance(data.get("projects"), dict)):
            raise RegistryError("registry_invalid", f"{endpoint}: unexpected shape or schema_version")
        return data
    raise last or RegistryError("registry_unavailable", f"{endpoint}: no attempt was made")


def candidates(raw: dict[str, Any], *, include_archived: bool = False) -> list[dict[str, Any]]:
    """Compact, sorted candidate records: indexed-ok, not archived, valid ids.

    `planned` stays in: ten planned projects have live boards (pjangler, bb,
    candystore, momo, 33god, ...), so status is not an activity signal.
    """
    status = raw.get("__registry_status") or {}
    out = []
    for pid, record in raw["projects"].items():
        if not isinstance(record, dict) or not ID_RE.match(pid) or record.get("project_id") != pid:
            continue
        if (status.get(pid) or {}).get("status", "ok") != "ok":
            continue  # stale or missing manifest
        if record.get("status") == "archived" and not include_archived:
            continue
        provider = record.get("ticket_provider") if isinstance(record.get("ticket_provider"), dict) else {}
        out.append({
            "project_id": pid,
            "name": str(record.get("name") or pid).strip() or pid,
            "description": str(record.get("description") or "").strip(),
            "repo_path": str(record.get("repo_path") or ""),
            "status": str(record.get("status") or ""),
            "provider": str(provider.get("type") or "none"),
            "workspace": str(provider.get("workspace") or ""),
            "identifier": str(provider.get("identifier") or ""),
            "board_id": str(provider.get("board_id") or ""),
            "board_state": str(provider.get("state") or ""),
        })
    out.sort(key=lambda c: c["project_id"])
    return out


def load_aliases(path: Optional[Path] = None) -> Optional[dict[str, dict[str, Any]]]:
    """{project_id: {"aliases": [...], "about": str}}, or None if the file is absent.

    Absent is survivable (the registry alone still works, at lower recall) and
    the caller records it. Present but malformed is a configuration error and
    raises: a typo here would otherwise quietly drop the hints it exists for.
    """
    target = Path(path or os.environ.get("WAX_PROJECT_ALIASES") or ALIASES_FILE)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RegistryError("aliases_invalid", f"{target}: {exc}") from None
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RegistryError("aliases_invalid", f"{target}: unparseable ({exc})") from None
    projects = doc.get("projects") if isinstance(doc, dict) else None
    if not isinstance(projects, dict):
        raise RegistryError("aliases_invalid", f"{target}: expected a top-level 'projects' mapping")
    out: dict[str, dict[str, Any]] = {}
    for pid, entry in projects.items():
        entry = entry or {}
        if (not isinstance(pid, str) or not ID_RE.match(pid) or not isinstance(entry, dict)
                or set(entry) - {"aliases", "about"}):
            raise RegistryError("aliases_invalid", f"{target}: bad entry for {pid!r}")
        aliases = entry.get("aliases") or []
        about = entry.get("about") or ""
        if (not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases)
                or not isinstance(about, str)):
            raise RegistryError("aliases_invalid", f"{target}: {pid}: aliases must be strings, about a string")
        out[pid] = {"aliases": [a.strip() for a in aliases], "about": " ".join(about.split())}
    return out


def _squash(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def hinted(candidate: dict[str, Any], hints: Optional[dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """{project_id, name, aliases, about} for prompting: registry text plus Wax hints.

    Aliases are the hint file's spoken forms, kept verbatim: "blood bank" and
    "P. Jangler" differ from "Bloodbank" and "pjangler" only in exactly the way
    speech recognition differs from a spelling, which is the point of them.
    The repo directory name is added only when it is not just the id, name or
    an alias spelled differently (pile-of-dumb-things is the old name of
    tower-of-dumb-things; KeepyMoney is just keepy-money).
    """
    hint = (hints or {}).get(candidate["project_id"]) or {}
    seen = {candidate["project_id"].casefold(), candidate["name"].casefold()}
    aliases = []
    for alias in hint.get("aliases") or []:
        if alias.casefold() not in seen:
            seen.add(alias.casefold())
            aliases.append(alias)
    repo = os.path.basename(candidate.get("repo_path", "").rstrip("/"))
    if _squash(repo) and _squash(repo) not in {_squash(s) for s in seen}:
        aliases.append(repo)
    about = " ".join(part.rstrip(".") + "." for part in (candidate.get("description", ""), hint.get("about", ""))
                     if part)
    return {"project_id": candidate["project_id"], "name": candidate["name"],
            "aliases": aliases, "about": about}
