#!/usr/bin/env python3.12
"""The exact list of files a public release ships, and the tools to extract and check it.

`MAP` is the whole release: a file not listed here never leaves this machine. `extract` copies the listed files,
byte for byte, into an empty folder; `check` compares a folder's files (git-tracked if it is a repository) with
MAP and reports what is missing and what is extra.
Usage: python3.12 scripts/release_manifest.py check [DIR] | extract DEST
"""

from __future__ import annotations
import subprocess
import sys
sys.dont_write_bytecode = True  # no .pyc beside the code: a cache folder could be a link elsewhere
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src import safe_io  # noqa: E402

MAP = (
    ".gitignore", "LICENSE", "README.md", "DISCLAIMER.md", "SECURITY.md", "requirements.txt",
    "launchd/track-changes.plist.template",
    "scripts/build_scan_terms.py", "scripts/local_notifier.example.py", "scripts/release_manifest.py",
    "scripts/run_extraction.py",
    "scripts/smoke_test.py", "scripts/snapshot_homekit.py", "scripts/track_changes.py",
    "src/__init__.py", "src/safe_io.py", "src/store_files.py",
    "src/audit/__init__.py", "src/audit/bundle_generator.py",
    "src/extractor/__init__.py", "src/extractor/app_parser.py", "src/extractor/hap_types.py",
    "src/extractor/sqlite_parser.py",
    "src/models/__init__.py", "src/models/schema.py",
    "src/tracking/__init__.py", "src/tracking/canon.py", "src/tracking/diff.py",
    "tests/__init__.py", "tests/fixtures/__init__.py", "tests/fixtures/build_fake_home.py",
    "tests/test_app_parser.py", "tests/test_build_scan_terms.py", "tests/test_bundle_generator.py",
    "tests/test_release_manifest.py", "tests/test_release_round2.py", "tests/test_release_round3.py", "tests/test_release_round4.py", "tests/test_release_round5.py", "tests/test_release_round6.py", "tests/test_run_extraction.py", "tests/test_schema.py",
    "tests/test_smoke.py", "tests/test_sqlite_parser.py", "tests/test_tracking.py",
)
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".git"}


def files_in(folder: Path) -> List[str]:
    """Relative paths of a folder's files: git-tracked ones in a repository, else every file (caches skipped)."""
    folder = Path(folder)
    if (folder / ".git").exists():
        out = subprocess.run(["git", "ls-files", "-z"], cwd=folder, capture_output=True, check=True).stdout
        return sorted(p for p in out.decode().split("\0") if p)
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*")
                  if p.is_file() and not SKIP_PARTS & set(p.relative_to(folder).parts) and p.name != ".DS_Store")


def check(folder: Path) -> Tuple[List[str], List[str]]:
    """(missing, extra) of `folder` against MAP."""
    present = set(files_in(folder))
    return sorted(set(MAP) - present), sorted(present - set(MAP))


def extract(dest: Path, root: Path = ROOT) -> int:
    """Copy every MAP file from `root` into the empty folder `dest`; return the count. Fails if one is missing."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"{dest} is not empty")
    missing = [f for f in MAP if not (Path(root) / f).is_file()]
    if missing:
        raise FileNotFoundError(f"missing from the source: {missing}")
    for rel in MAP:
        target = dest / rel
        safe_io.mkdir(target.parent, 0o777, parents=True, exist_ok=True)
        safe_io.copy_file(Path(root) / rel, target, perm=0o666, keep_times=False)
    return len(MAP)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["extract"] and len(argv) == 2:
        print(f"release_manifest: {extract(Path(argv[1]))} file(s) extracted")
        return 0
    if argv[:1] == ["check"] and len(argv) <= 2:
        missing, extra = check(Path(argv[1]) if len(argv) == 2 else ROOT)
        print(f"release_manifest: {len(MAP)} listed, {len(missing)} missing, {len(extra)} extra")
        for f in missing:
            print(f"  missing: {f}")
        for f in extra:
            print(f"  extra: {f}")
        return 0 if not missing and not extra else 1
    print(__doc__.strip().splitlines()[-1])
    return 2


if __name__ == "__main__":
    sys.exit(main())
