import fcntl
import hashlib
import json
import threading
import time
import os
from contextlib import contextmanager

from . import paths

_local = threading.local()


@contextmanager
def item_lock(item_id, *, timeout=None):
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
        if timeout is None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + max(0, timeout)
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"item {item_id} has not settled")
                    # Request-local bounded settlement, not a scheduled job.
                    threading.Event().wait(min(0.025, remaining))
        held.add(item_id)
        try:
            yield
        finally:
            held.remove(item_id)
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def try_item_lock(item_id):
    """Yield True holding the item flock, or False at once if someone else holds it.

    Same lock file as `item_lock`, so a pass (which runs under `item_lock`)
    and a background drain exclude each other. Re-entrant like `item_lock`:
    a thread that already holds the item gets True without re-locking.
    """
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = set()
    if item_id in held:
        yield True
        return
    directory = paths.VAR / "item-locks"
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(str(item_id).encode()).hexdigest()
    with (directory / name).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        held.add(item_id)
        try:
            yield True
        finally:
            held.remove(item_id)
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_json(destination, value):
    """Durable metadata replacement; interrupted temp files remain recoverable."""
    import uuid
    durable_directory(destination.parent)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    fsync_dir(destination.parent)


def fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_directory(directory):
    """Persist new ancestor entries as well as the files inside the leaf."""
    if not directory.is_dir():
        durable_directory(directory.parent)
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        fsync_dir(directory.parent)


def hold_path(item_id):
    name = hashlib.sha256(str(item_id).encode()).hexdigest()
    return paths.VAR / "split-holds" / (name + ".json")


def is_held(item_id):
    return hold_path(item_id).exists()


def hold_source(item_id, source):
    """Install the fence BEFORE waiting for the source's active operation.

    Atomic, no-clobber link keeps the first request's identity across repeats.
    Readers use existence only, so no half-written hold can permit fresh work.
    """
    destination = hold_path(item_id)
    if not destination.exists():
        import uuid
        durable_directory(destination.parent)
        temporary = destination.parent / (uuid.uuid4().hex + ".tmp")
        with temporary.open("x") as handle:
            json.dump({"item_id": item_id, "source": str(source),
                       "reason": "operator_split", "created_epoch": time.time()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass
        fsync_dir(destination.parent)
        temporary.unlink()
    return destination


@contextmanager
def item_work(item_id):
    """Fence fresh work, while allowing an already-entered operation to settle.

    The allowance is THREAD-local and scoped to the outer operation, never
    inferred merely from owning a raw item_lock (split owns that lock too).
    A queued stale claimant checks the fence AFTER acquiring the flock.
    """
    allowed = getattr(_local, "allowed", None)
    if allowed is None:
        allowed = _local.allowed = set()
    if item_id in allowed:
        yield True
        return
    with item_lock(item_id):
        if is_held(item_id):
            yield False
            return
        allowed.add(item_id)
        try:
            yield True
        finally:
            allowed.remove(item_id)


def definition_id(ep):
    value = {key: value for key, value in ep.items() if not key.startswith("_")}
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
