"""Build a fictional HomeKit-shaped store for tests and the smoke test.

Three fictional homes; the first holds rooms, accessories, scenes and automations. Every name is invented, every id
is derived from a fixed fake seed, and the store holds no address, MAC, serial or code of any kind.
"""

from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.store_files import DB  # noqa: E402
from tests.test_sqlite_parser import _build_zmkf  # noqa: E402


def build(folder: Path) -> Path:
    """Write a fictional store at <folder>/<DB> and return its path. The folder must be empty of a store."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / DB
    if path.exists():
        raise FileExistsError(path)
    _build_zmkf(path, profile=True)
    return path
