"""Process-identity checks that survive PID reuse and reboots.

A bare `/proc/<pid>` existence test is not enough to decide "is my encoder still
running". PIDs are recycled, and after a reboot the same number is almost
certainly a different process. Every liveness answer here is a conjunction of
three independent facts: the boot generation matches, the exe is what we
spawned, and the process start time matches the one we recorded.
"""

import os
from pathlib import Path
from typing import Optional

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def boot_id() -> str:
    return BOOT_ID_PATH.read_text().strip()


def proc_starttime(pid: int) -> Optional[int]:
    """Field 22 of /proc/<pid>/stat: process start time in clock ticks.

    Parsed from the last ')' because the comm field (field 2) is parenthesised
    and may itself contain spaces or parentheses.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    try:
        rest = raw[raw.rindex(")") + 2 :].split()
        return int(rest[19])  # field 22 == index 19 after fields 1-2 removed
    except (ValueError, IndexError):
        return None


def proc_exe_name(pid: int) -> Optional[str]:
    try:
        return os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        return None


def is_alive(pid: int, starttime: int, expect_boot_id: str, exe_name: str = "ffmpeg") -> bool:
    """True only if this is genuinely the same process we started."""
    if not pid or pid <= 0:
        return False
    if expect_boot_id != boot_id():
        # Different boot: the recorded pid cannot be our process, whatever /proc says.
        return False
    if proc_exe_name(pid) != exe_name:
        return False
    return proc_starttime(pid) == starttime


def fsync_path(path: Path) -> None:
    """fsync a file, then its parent directory.

    Syncing the file alone is not enough: the directory entry itself must be
    durable or a crash can leave the sentinel invisible even though its bytes
    reached the platter. The sentinel exists precisely to be readable after a
    crash, so a non-durable one is worthless.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    dfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def descendants(pid: int) -> list[int]:
    """Every live descendant of `pid` (children first), read from /proc.

    `nice -n 15 transcribe` is a shim that spawns python (which spawns more), so
    stopping "the transcriber" means stopping a tree, not one pid.
    """
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text()
            ppid = int(raw[raw.rindex(")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry.name))
    out: list[int] = []
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            out.append(child)
            stack.append(child)
    return list(reversed(out))


def kill_tree(pid: int, *, grace_s: float = 5.0) -> list[int]:
    """TERM `pid` and its descendants, KILL whatever outlives `grace_s`. Returns the pids signalled.

    The caller has already proved `pid` is the process it started (starttime check);
    this only walks the tree below it.
    """
    import signal
    import time

    targets = [*descendants(pid), pid]
    for target in targets:
        try:
            os.kill(target, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and any(Path(f"/proc/{t}").exists() and not _is_zombie(t)
                                              for t in targets):
        time.sleep(0.05)
    for target in targets:
        if Path(f"/proc/{target}").exists() and not _is_zombie(target):
            try:
                os.kill(target, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return targets


def _is_zombie(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return raw[raw.rindex(")") + 2 :].split()[0] == "Z"
    except (OSError, ValueError, IndexError):
        return True
