import fcntl
import hashlib
import json
import threading
from contextlib import contextmanager

from . import paths

_local = threading.local()


@contextmanager
def item_lock(item_id):
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = set()
    if item_id in held:
        yield
        return
    directory = paths.VAR / "item-locks"
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(str(item_id).encode()).hexdigest()
    with (directory / name).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        held.add(item_id)
        try:
            yield
        finally:
            held.remove(item_id)
            fcntl.flock(handle, fcntl.LOCK_UN)


def definition_id(ep):
    value = {key: value for key, value in ep.items() if not key.startswith("_")}
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
