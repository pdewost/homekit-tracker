"""The adopter smoke test stays inside its temporary folder: it never opens the real store, data/ or logs/."""

import importlib.util
import os
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("smoke_test", ROOT / "scripts" / "smoke_test.py")
smoke_test = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke_test)

_RECORDING = []   # paths seen while _ACTIVE is set
_ACTIVE = [False]


def _hook(event, args):
    if _ACTIVE[0] and event in ("open", "sqlite3.connect", "os.listdir", "os.scandir", "shutil.copyfile"):
        for a in (args[:2] if event == "shutil.copyfile" else args[:1]):  # open's 2nd arg is the mode
            if isinstance(a, (str, bytes, os.PathLike)):
                path = os.fsdecode(a)
                if path.startswith("file:"):           # SQLite URI: file:<path>?mode=ro
                    path = path[len("file:"):].split("?", 1)[0]
                _RECORDING.append(os.path.realpath(path))


sys.addaudithook(_hook)  # audit hooks cannot be removed; this one records only while _ACTIVE is set


class TestSmokeStaysInItsFolder(unittest.TestCase):

    def test_no_path_outside_the_temp_folder(self):
        code_roots = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sysconfig.get_paths()["stdlib"],
                                                    str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "tests"))}
        forbidden = [os.path.realpath(p) for p in (Path.home() / "Library" / "HomeKit", ROOT / "data", ROOT / "logs")]
        with tempfile.TemporaryDirectory() as td:
            base = os.path.realpath(td)
            _RECORDING.clear()
            _ACTIVE[0] = True
            try:
                counts = smoke_test.smoke(Path(td))
            finally:
                _ACTIVE[0] = False
            seen = list(_RECORDING)
        self.assertEqual(counts["ok"], 1)
        self.assertTrue(any(p.startswith(base + os.sep) for p in seen))          # the hook did record the run
        self.assertEqual([p for p in seen if any(p == f or p.startswith(f + os.sep) for f in forbidden)], [])
        outside = [p for p in seen if not p.startswith(base + os.sep) and p != base
                   and not any(p == c or p.startswith(c + os.sep) for c in code_roots)
                   and not p.startswith("/dev/")]
        self.assertEqual(outside, [])


if __name__ == "__main__":
    unittest.main()
