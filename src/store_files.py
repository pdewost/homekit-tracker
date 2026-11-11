"""File names of Apple's HomeKit store, defined once so that code, tests and a release scan share one line."""

DB = "core.sqlite"
WAL = DB + "-wal"
SHM = DB + "-shm"

import os
from pathlib import Path

LIVE_ROOT = Path.home() / "Library" / "HomeKit"  # the live HomeKit folder: nothing of ours is read from or written into it


class LiveStoreError(Exception):
    """A path resolves to the live HomeKit root or beneath it."""


def _ident(path):
    """(device, inode) of what `path` is itself (a link is not followed), or None when it does not exist."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _ancestors(real):
    """`real` and every ancestor of it, nearest first."""
    while True:
        yield real
        parent = os.path.dirname(real)
        if parent == real:
            return
        real = parent


def inside_resolved(real: str, root) -> bool:
    """True when `real` (a path whose parents are already resolved) is `root` or lies beneath it: by name, and by
    identity, which also catches letter-case and other spellings of the same folder on a case-insensitive volume."""
    live = os.path.realpath(root)
    if real == live or real.startswith(live.rstrip(os.sep) + os.sep):
        return True
    ident = _ident(live)
    return ident is not None and any(_ident(a) == ident for a in _ancestors(real))


def contains_resolved(real: str, root) -> bool:
    """True when `real` is the root's folder or one of its ancestors (removing or moving it would take the root)."""
    live = os.path.realpath(root)
    if live == real or live.startswith(real.rstrip(os.sep) + os.sep):
        return True
    ident = _ident(real)
    return ident is not None and any(_ident(a) == ident for a in _ancestors(live))


def inside_live_root(path, root=None) -> bool:
    """True when `path` is the live root or lies beneath it. realpath resolves symlinks and, for a path that does not
    exist yet, its nearest existing parent; nothing is created, opened or deleted here."""
    return inside_resolved(os.path.realpath(path), LIVE_ROOT if root is None else root)


def refuse_live_root(path, root=None) -> None:
    """Raise LiveStoreError when `path` is inside the live root."""
    if inside_live_root(path, root):
        raise LiveStoreError("refusing a path inside the live HomeKit store")
