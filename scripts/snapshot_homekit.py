"""Safe copy of the HomeKit store.

The live files are only ever copied, never opened with SQLite (a read-only open still writes -shm). A copy is
accepted when two back-to-back copies are byte-identical and the source's size and mtime did not move meanwhile.
The accepted snapshot is never opened either: validation and reading happen on a scratch copy.
Standard library only, plus the store file names from src/store_files.py.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import sqlite3
import sys
sys.dont_write_bytecode = True  # no .pyc beside the code: a cache folder could be a link elsewhere
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from src import safe_io  # noqa: E402
from src.store_files import DB, WAL, inside_live_root  # noqa: E402

FILES = (DB, WAL)  # never -shm: a volatile index SQLite rebuilds
SOURCE = Path.home() / "Library" / "HomeKit"
SNAPSHOTS = PROJECT_ROOT / "data" / "raw" / "snapshots"
SCRATCH = PROJECT_ROOT / "data" / "raw" / "scratch"
ATTEMPTS = 3


class SnapshotError(Exception):
    """A failure with a fixed code (E_TORN, E_FDA, E_INTEGRITY, E_EMPTY, E_OTHER); the text never names a home."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def refuse_live_store(path: Path, root: Optional[Path] = None) -> None:
    """Raise SnapshotError(E_LIVE) when `path` is the live HomeKit root or lies beneath it. realpath resolves symlinks
    and, for a path that does not exist yet, its nearest existing parent; nothing is created, opened or deleted here."""
    if inside_live_root(path, SOURCE if root is None else root):
        raise SnapshotError("E_LIVE", "refusing a destination inside the live HomeKit store")


def _fda_error() -> SnapshotError:
    return SnapshotError("E_FDA", f"Full Disk Access needed for {os.path.realpath(sys.executable)}")


def _mkdir_private(path: Path) -> None:
    """Create `path` and any missing parent, each 0700 (the process umask never loosens them)."""
    missing = []
    p = Path(path)
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        safe_io.mkdir(d, 0o700, root=SOURCE)
        safe_io.chmod(d, 0o700, root=SOURCE)


def print_fda_hint() -> None:
    """Interactive runs only: name the interpreter that needs Full Disk Access (the path can hold the account name)."""
    print(f"E_FDA: add this interpreter to Full Disk Access: {os.path.realpath(sys.executable)}", file=sys.stderr)


def copy_to_scratch(db: Path, scratch_root: Path = SCRATCH) -> Path:
    """Copy a store file (plus its -wal when present, never -shm) into a fresh private folder under scratch_root and
    return that folder. The caller reads the copy there and calls remove_scratch() afterwards, also on error."""
    db = Path(db)
    refuse_live_store(Path(scratch_root))
    _mkdir_private(Path(scratch_root))
    folder = safe_io.mkdtemp("extract-", scratch_root, root=SOURCE)
    try:
        for src, name in ((db, db.name), (db.with_name(db.name + "-wal"), db.name + "-wal")):
            if src.exists():
                safe_io.copy_file(src, folder / name, root=SOURCE)
                safe_io.chmod(folder / name, 0o600, root=SOURCE)
    except BaseException:
        remove_scratch(folder, scratch_root)
        raise
    return folder


def _stats(source: Path) -> Dict[str, Optional[Tuple[int, int]]]:
    out: Dict[str, Optional[Tuple[int, int]]] = {}
    for name in FILES:
        try:
            st = (source / name).stat()
            out[name] = (st.st_size, st.st_mtime_ns)
        except FileNotFoundError:
            out[name] = None
        except PermissionError:
            raise _fda_error()
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _copy_set(source: Path, target: Path, present: Dict[str, Optional[Tuple[int, int]]]) -> Dict[str, str]:
    _mkdir_private(target)
    hashes = {}
    for name, stat in present.items():
        if stat is None:
            continue
        try:
            safe_io.copy_file(source / name, target / name, root=SOURCE)  # the source may be the live store: copy only
            safe_io.chmod(target / name, 0o600, root=SOURCE)
        except FileNotFoundError:  # vanished mid-copy: the stat check rejects this attempt
            continue
        except PermissionError:
            raise _fda_error()
        hashes[name] = _sha256(target / name)
    return hashes


def stamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")


def take_snapshot(source: Path = SOURCE, dest_root: Path = SNAPSHOTS, stamp: Optional[str] = None,
                  _between_copies: Optional[Callable[[int], None]] = None) -> Path:
    """Copy the store into dest_root/<stamp>/ (core + wal + snapshot.json); raise SnapshotError on failure."""
    source, dest_root = Path(source), Path(dest_root)
    refuse_live_store(dest_root)
    if stamp is None:
        stamp = stamp_now()
        while (dest_root / stamp).exists():  # two runs in one second: wait for the next stamp
            time.sleep(0.2)
            stamp = stamp_now()
    elif (dest_root / stamp).exists():
        raise SnapshotError("E_OTHER", "snapshot folder already exists")
    final = dest_root / stamp
    partial = dest_root / f"{stamp}.partial"
    for attempt in range(1, ATTEMPTS + 1):
        before = _stats(source)
        if before[DB] is None:
            raise SnapshotError("E_EMPTY", f"no {DB} in the source")
        safe_io.rmtree(partial, ignore_errors=True, root=SOURCE)
        try:
            hashes_a = _copy_set(source, partial / "A", before)
            if _between_copies:
                _between_copies(attempt)
            hashes_b = _copy_set(source, partial / "B", before)
            after = _stats(source)
        except SnapshotError:
            safe_io.rmtree(partial, ignore_errors=True, root=SOURCE)
            raise
        if before == after and hashes_a == hashes_b and set(hashes_a) == {n for n, s in before.items() if s}:
            for name in hashes_a:
                safe_io.replace(partial / "A" / name, partial / name, root=SOURCE)
            safe_io.rmtree(partial / "A", root=SOURCE)
            safe_io.rmtree(partial / "B", root=SOURCE)
            meta = {
                "stamp": stamp,
                "taken_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "attempts": attempt,
                "files": {n: {"sha256": h, "size": before[n][0], "source_mtime_ns": before[n][1]}
                          for n, h in hashes_a.items()},
            }
            safe_io.write_text(partial / "snapshot.json", json.dumps(meta, indent=2, sort_keys=True), root=SOURCE)
            safe_io.chmod(partial / "snapshot.json", 0o600, root=SOURCE)
            safe_io.replace(partial, final, root=SOURCE)
            return final
        safe_io.rmtree(partial, ignore_errors=True, root=SOURCE)
    raise SnapshotError("E_TORN", f"source kept changing over {ATTEMPTS} attempts")


def validate(snapshot: Path, scratch_root: Path = SCRATCH) -> Path:
    """Copy the snapshot to scratch_root/<stamp>/ and check it there; return that folder (the reader reads it)."""
    snapshot = Path(snapshot)
    scratch = Path(scratch_root) / snapshot.name
    refuse_live_store(scratch)
    safe_io.rmtree(scratch, ignore_errors=True, root=SOURCE)
    _mkdir_private(scratch)
    for name in FILES:
        if (snapshot / name).exists():
            safe_io.copy_file(snapshot / name, scratch / name, root=SOURCE)
            safe_io.chmod(scratch / name, 0o600, root=SOURCE)
    try:
        conn = safe_io.sqlite_scratch(scratch / DB, root=SOURCE)  # a throwaway copy: SQLite may rebuild -shm here
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise SnapshotError("E_INTEGRITY", "integrity_check did not return ok")
            has_homes = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='ZMKFHOME'").fetchone()[0]
            if not has_homes or conn.execute("SELECT count(*) FROM ZMKFHOME").fetchone()[0] < 1:
                raise SnapshotError("E_EMPTY", "no ZMKFHOME row")
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        remove_scratch(scratch, scratch_root)
        raise SnapshotError("E_INTEGRITY", type(exc).__name__)
    except SnapshotError:
        remove_scratch(scratch, scratch_root)
        raise
    return scratch


def remove_scratch(scratch: Path, scratch_root: Path = SCRATCH) -> None:
    """Remove a scratch folder; one that is not strictly inside the scratch root is refused."""
    real = os.path.realpath(scratch)
    if real == os.path.realpath(scratch_root) or not inside_live_root(scratch, scratch_root):
        raise ValueError("refusing to remove a folder outside the scratch root")
    safe_io.rmtree(scratch, ignore_errors=True, root=SOURCE)


def main() -> int:
    parser = argparse.ArgumentParser(description="Take one safe copy of the HomeKit store and validate it.")
    parser.add_argument("--source", default=str(SOURCE))
    parser.add_argument("--dest", default=str(SNAPSHOTS))
    args = parser.parse_args()
    try:
        snap = take_snapshot(Path(args.source), Path(args.dest))
        remove_scratch(validate(snap))
    except SnapshotError as exc:
        print(exc.code)
        if exc.code == "E_FDA":
            print_fda_hint()
        return 1
    meta = json.loads((snap / "snapshot.json").read_text())
    print(f"OK {snap.name} attempts={meta['attempts']} " +
          " ".join(f"{n}={f['sha256'][:8]}" for n, f in sorted(meta["files"].items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
