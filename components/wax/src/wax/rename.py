"""renameat2(RENAME_NOREPLACE) — atomic move that refuses to clobber.

Plain os.rename() silently overwrites the destination. The whole point of the
inbox handoff is that a finished recording appears exactly once, complete, and
never destroys something already there — the pre-Wax pipeline lost data doing
exactly that (two different files both named clip_0057.mp3). RENAME_NOREPLACE
turns a collision into EEXIST instead of a silent deletion.
"""

import ctypes
import ctypes.util
import errno
import os
from pathlib import Path

RENAME_NOREPLACE = 1 << 0

# x86_64. Guarded so a port to another arch fails loudly rather than issuing a
# wrong syscall number.
_SYS_renameat2 = {"x86_64": 316, "aarch64": 276}.get(os.uname().machine)

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.syscall.restype = ctypes.c_long


class RenameCollision(FileExistsError):
    """Destination already exists; caller must pick another name, never clobber."""


def renameat2_noreplace(src: Path, dst: Path) -> None:
    """Atomically move src -> dst, failing with RenameCollision if dst exists."""
    if _SYS_renameat2 is None:
        raise RuntimeError(f"renameat2 syscall number unknown for {os.uname().machine}")
    AT_FDCWD = -100
    rc = _libc.syscall(
        ctypes.c_long(_SYS_renameat2),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(str(src).encode()),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(str(dst).encode()),
        ctypes.c_uint(RENAME_NOREPLACE),
    )
    if rc == 0:
        return
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise RenameCollision(f"destination exists: {dst}")
    if err in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
        # Filesystem or kernel lacks RENAME_NOREPLACE. Emulate as best we can:
        # link() is atomic and fails with EEXIST, then drop the source.
        try:
            os.link(src, dst)
        except FileExistsError as e:
            raise RenameCollision(f"destination exists: {dst}") from e
        os.unlink(src)
        return
    raise OSError(err, os.strerror(err), str(src), None, str(dst))


def move_noclobber(src: Path, dst: Path, max_attempts: int = 50) -> Path:
    """Move src to dst, uniquifying with -1, -2, ... on collision.

    Returns the path actually used. Never overwrites, never deletes.
    """
    try:
        renameat2_noreplace(src, dst)
        return dst
    except RenameCollision:
        pass
    stem, suffix = dst.stem, dst.suffix
    for n in range(1, max_attempts + 1):
        cand = dst.with_name(f"{stem}-{n}{suffix}")
        try:
            renameat2_noreplace(src, cand)
            return cand
        except RenameCollision:
            continue
    raise RenameCollision(f"could not find a free name for {dst} after {max_attempts} tries")
