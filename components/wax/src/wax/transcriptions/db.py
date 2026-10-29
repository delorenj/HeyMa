"""Lazy Postgres connections for the `transcriptions` store (design D.9).

psycopg is imported here and only here, on first use, so `import wax.transcriptions`
costs nothing and works under an interpreter that has no psycopg (mise Python):
every store operation then fails with `StoreUnavailable`, which the CLI's
RuntimeError handler already turns into `error: ...` and exit 2.

Passwords never live in a file or a DSN. The role's password comes from
`$WAX_TRANSCRIPTS_PASSWORD` (direct override) or `op read <op://...>` through
`provider.resolve_key`, and is cached once per process.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

from . import config


class StoreUnavailable(RuntimeError):
    """The store cannot be reached or used right now (no DSN, no psycopg, auth, network)."""


# role -> (dsn getter, direct-password env var, op:// reference getter)
_ROLES = {
    "app": (config.dsn, "WAX_TRANSCRIPTS_PASSWORD", config.password_op),
    "owner": (config.owner_dsn, "WAX_TRANSCRIPTS_OWNER_PASSWORD", config.owner_password_op),
    # SELECT-only (0001 grants + default privileges). Used by `wax transcripts browse` alone, so a bug in
    # the reader Holocene spawns cannot write (docs/HOLOCENE-TRANSCRIPTS-TAB.md B.7, C.1).
    "ro": (config.ro_dsn, "WAX_TRANSCRIPTS_RO_PASSWORD", config.ro_password_op),
}
_DSN_ENV = {"app": "WAX_TRANSCRIPTS_DSN", "owner": "WAX_TRANSCRIPTS_OWNER_DSN", "ro": "WAX_TRANSCRIPTS_RO_DSN"}

DEFAULT_STATEMENT_TIMEOUT_MS = 5000
SEARCH_STATEMENT_TIMEOUT_MS = 10000
CONNECT_TIMEOUT_S = 2
# deploy/systemd/user/waxd.service ships op://DeLoSecrets/REPLACE_WITH_{APP,OWNER}_ITEM_UUID/password
# until deploy/provision-transcriptions --apply fills in the real item UUIDs.
UNIT_PLACEHOLDER = "REPLACE_WITH_"

_lock = threading.Lock()
_passwords: dict[tuple[str, str, str], Optional[str]] = {}


def psycopg():
    """The psycopg module, or StoreUnavailable when this interpreter lacks it."""
    try:
        import psycopg as module  # noqa: PLC0415 - lazy on purpose
    except ImportError as exc:
        raise StoreUnavailable(
            "psycopg is not importable in this interpreter (use /usr/bin/python3, which has "
            f"python3-psycopg): {exc}") from None
    return module


def configured(role: str = "app") -> bool:
    getter = _ROLES.get(role, (lambda: None,))[0]
    return bool(getter())


def _password(role: str) -> Optional[str]:
    _, env_var, op_getter = _ROLES[role]
    op_ref = op_getter() or ""
    direct = os.environ.get(env_var, "").strip()
    if not direct and not op_ref:
        return None          # libpq may still authenticate (peer, .pgpass, trust)
    if not direct and UNIT_PLACEHOLDER in op_ref:
        # The committed unit ships REPLACE_WITH_*_ITEM_UUID until provisioning fills it in. Say so,
        # instead of a 5 s `op read` that fails with "item not found" on every tick.
        raise StoreUnavailable(f"transcriptions {role} password is still the unit placeholder ({op_ref}); "
                               "run deploy/provision-transcriptions --apply, then reinstall the unit")
    key = (role, env_var, op_ref)
    with _lock:
        if key in _passwords and not direct:
            return _passwords[key]
    from .. import provider
    try:
        password = provider.resolve_key(env_var=env_var, op_ref=op_ref)
    except provider.ProviderError as exc:
        raise StoreUnavailable(f"transcriptions {role} password did not resolve: {exc}") from None
    if not direct:
        with _lock:
            _passwords[key] = password
    return password


def forget_passwords() -> None:
    """Drop cached passwords (tests; or after a rotation)."""
    with _lock:
        _passwords.clear()


def connect(role: str = "app", *, statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
            autocommit: bool = False, dsn: Optional[str] = None) -> Any:
    """Open one connection as `role` ('app', 'owner' or 'ro'). Raises StoreUnavailable."""
    if role not in _ROLES:
        raise ValueError(f"unknown transcriptions role {role!r}")
    target = dsn or _ROLES[role][0]()
    if not target:
        raise StoreUnavailable(f"${_DSN_ENV[role]} is not configured (capture-only mode)")
    pg = psycopg()
    password = _password(role)
    kwargs: dict[str, Any] = {
        "connect_timeout": CONNECT_TIMEOUT_S,
        "options": f"-c statement_timeout={int(statement_timeout_ms)} -c search_path=tx,public",
        "autocommit": autocommit,
    }
    if password is not None:
        kwargs["password"] = password
    try:
        return pg.connect(target, **kwargs)
    except pg.Error as exc:
        # Never echo the DSN back: a hand-set one could carry a password.
        raise StoreUnavailable(f"transcriptions store unreachable as {role}: "
                               f"{type(exc).__name__}: {str(exc).strip()[:300]}") from None


def is_outage(exc: BaseException) -> bool:
    """True for connection-level failures (retry later), False for data/logic errors."""
    if isinstance(exc, StoreUnavailable):
        return True
    try:
        pg = psycopg()
    except StoreUnavailable:
        return True
    return isinstance(exc, (pg.OperationalError, pg.InterfaceError))
