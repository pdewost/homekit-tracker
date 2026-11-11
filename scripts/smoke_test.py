#!/usr/bin/env python3.12
"""Adopter smoke test: run the reader and one tracking run on a fictional home, never on a real one.

Everything happens inside one fresh temporary folder: the fictional store, the data folder and the log folder are
all injected, and messages are printed. The real HomeKit store and this project's data/ and logs/ are never read.
Usage: python3.12 scripts/smoke_test.py --fixture
"""

from __future__ import annotations
import argparse
import importlib.util
import sys
sys.dont_write_bytecode = True  # no .pyc beside the code: a cache folder could be a link elsewhere
import tempfile
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src import safe_io  # noqa: E402


def _track_changes():
    spec = importlib.util.spec_from_file_location("track_changes_smoke", ROOT / "scripts" / "track_changes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def smoke(base: Path) -> Dict[str, int]:
    """Build the fictional home under `base`, read it, run tracking once; return counts only."""
    from src.extractor.sqlite_parser import parse_homed_sqlite
    from tests.fixtures.build_fake_home import build
    source = Path(base) / "HomeKit"
    store = build(source)
    household = parse_homed_sqlite(store, "Maple House")  # the fictional home that holds the content
    tc = _track_changes()
    printed = []
    summary = tc.run(source=source, data=Path(base) / "data", logs=Path(base) / "logs",
                     notifier=lambda text: printed.append(text) or True)
    return {"rooms": len(household.rooms), "accessories": len(household.accessories),
            "automations": len(household.automations), "homes_tracked": summary["homes"],
            "baselines": summary["baselines"], "changes": summary["changes"],
            "ok": int(summary["status"] == "ok"), "messages": len(printed)}


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", action="store_true", required=True, help="use the fictional home (the only mode)")
    parser.parse_args(argv)
    parent = Path(tempfile.gettempdir())
    safe_io.check(parent, container=True)  # the fixture is written under this folder: never inside or above HomeKit
    with tempfile.TemporaryDirectory(dir=parent) as td:
        safe_io.check(td)
        counts = smoke(Path(td))
    print(" ".join(f"{k}={v}" for k, v in counts.items()))
    return 0 if counts["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
