"""The one place released code writes, deletes or opens a database.

Every destination is checked first: it must not resolve into the live HomeKit folder (by name or by identity, so
letter-case and other spellings of it are caught), a final path component that is a symbolic link is refused (opens
use O_NOFOLLOW), a file with several names is never written, and a folder that holds the live folder is never
removed or moved. Reading FROM the live folder (the snapshot copy) is allowed:
only destinations are guarded. A released file calls these helpers and never the raw calls; a test scans for that.
"""

from __future__ import annotations
import os
import shutil
import stat
import sqlite3
import tempfile
from pathlib import Path
from typing import IO, Optional
from urllib.parse import quote

from . import store_files
from .store_files import LiveStoreError

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class SymlinkError(LiveStoreError):
    """A destination is a symbolic link."""


def _target(path) -> str:
    """The path with its parent resolved but its last component kept: where an unlink or rename would act. The path
    is never normalised lexically: `..` is applied by the system after symlinks, so the parent is resolved first."""
    p = os.fspath(path)
    if not os.path.isabs(p):
        p = os.path.join(os.getcwd(), p)
    name = os.path.basename(p)
    if name in ("", ".", ".."):
        raise LiveStoreError("refusing a path whose last component is empty, '.' or '..'")
    return os.path.join(os.path.realpath(os.path.dirname(p)), name)


def check(path, root=None, follow: bool = True, container: bool = False) -> None:
    """Raise LiveStoreError for a destination inside the live root (`root` adds one more root to refuse); with
    follow=False the last component is not resolved (an unlink or rename acts on the link itself). container=True
    also refuses a folder that holds the live root (removing or moving it would take the live root with it)."""
    reals = [_target(path)] + ([os.path.realpath(path)] if follow else [])
    for real in reals:
        for r in ([store_files.LIVE_ROOT] if root is None else [store_files.LIVE_ROOT, root]):
            if store_files.inside_resolved(real, r):
                raise LiveStoreError("refusing a path inside the live HomeKit store")
            if container and store_files.contains_resolved(real, r):
                raise LiveStoreError("refusing a folder that holds the live HomeKit store")


def _no_symlink(path) -> None:
    if os.path.islink(path):
        raise SymlinkError("refusing a destination that is a symbolic link")


def open_fd(path, flags: int, mode: int = 0o600, root=None) -> int:
    """os.open for a destination: live root and symlink refused."""
    check(path, root)
    try:
        return os.open(path, flags | _NOFOLLOW, mode)
    except OSError as exc:
        if os.path.islink(path):
            raise SymlinkError("refusing a destination that is a symbolic link") from exc
        raise


def _open_write(path, append: bool, truncate: bool, root, perm: int, binary: bool, encoding: str = "utf-8") -> IO:
    """Open for writing without truncating, then validate the descriptor: a regular file, owned by this user, with one
    name only (a hard link would write through to another folder). The mode is set before any byte is written and
    only then is the file truncated."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else 0)
    fd = open_fd(path, flags, perm, root)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise LiveStoreError("refusing a destination that is not a regular file owned by you")
        if st.st_nlink != 1:
            raise LiveStoreError("refusing a destination with several names (a hard link)")
        umask = os.umask(0)
        os.umask(umask)
        os.fchmod(fd, perm & ~umask)
        if truncate:
            os.ftruncate(fd, 0)
        return os.fdopen(fd, ("a" if append else "w") + ("b" if binary else ""), **({} if binary else {"encoding": encoding}))
    except BaseException:
        os.close(fd)
        raise


def open_text(path, mode: str = "w", root=None, perm: int = 0o600, encoding: str = "utf-8") -> IO[str]:
    """Open a text file for writing ('w' truncates, 'a' appends), created `perm`; mode is 'w' or 'a'."""
    return _open_write(path, mode == "a", mode != "a", root, perm, False, encoding)


def open_lock(path, root=None) -> IO[str]:
    """Open a lock file without truncating it, created 0600."""
    return os.fdopen(open_fd(path, os.O_RDWR | os.O_CREAT, 0o600, root), "r+")


def write_text(path, text: str, root=None, perm: int = 0o600) -> None:
    with open_text(path, "w", root, perm) as f:
        f.write(text)


def mkdir(path, mode: int = 0o700, parents: bool = False, exist_ok: bool = False, root=None) -> None:
    check(path, root)
    _no_symlink(path)
    Path(path).mkdir(mode=mode, parents=parents, exist_ok=exist_ok)


def mkdtemp(prefix: str, dir, root=None) -> Path:
    check(dir, root)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=dir))


def _single_name(path) -> None:
    """Refuse an existing regular file with more than one name: a hard link may be another name of a live file."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISREG(st.st_mode) and st.st_nlink != 1:
        raise LiveStoreError("refusing a destination with several names (a hard link)")


def chmod(path, mode: int, root=None) -> None:
    check(path, root)
    _no_symlink(path)
    _single_name(path)
    if os.chmod in os.supports_follow_symlinks:
        os.chmod(path, mode, follow_symlinks=False)
    else:
        os.chmod(path, mode)


def unlink(path, missing_ok: bool = False, root=None) -> None:
    """Remove a file or a link itself (never what a link points at)."""
    check(path, root, follow=False, container=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        if not missing_ok:
            raise


def rmtree(path, ignore_errors: bool = False, root=None) -> None:
    check(path, root, follow=False, container=True)
    shutil.rmtree(path, ignore_errors=ignore_errors)


def replace(src, dst, root=None) -> None:
    check(src, root, follow=False, container=True)
    check(dst, root, follow=False, container=True)
    os.replace(src, dst)


def copy_file(src, dst, root=None, perm: int = 0o600, keep_times: bool = True) -> None:
    """Copy a file's bytes to a new or existing regular file. The source may lie in the live root."""
    with _open_write(dst, False, True, root, perm, True) as out, open(src, "rb") as inp:
        shutil.copyfileobj(inp, out)
        if keep_times:
            st = os.stat(src)
            os.utime(out.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))


def sqlite_ro(path, root=None) -> sqlite3.Connection:
    """A read-only SQLite connection; a path inside the live root is refused."""
    check(path, root)
    _single_name(path)
    return sqlite3.connect(f"file:{quote(os.fspath(path))}?mode=ro", uri=True)


def sqlite_scratch(path, root=None) -> sqlite3.Connection:
    """A read-write connection to a throwaway copy (SQLite may rebuild -shm beside it); the live root is refused."""
    check(path, root)
    _no_symlink(path)
    _single_name(path)
    return sqlite3.connect(os.fspath(path))
