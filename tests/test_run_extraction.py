"""Test the extraction CLI's --no-audit path."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from src.audit.bundle_generator import STRIPPED_KEYS
from src.store_files import DB

WAL, SHM = DB + "-wal", DB + "-shm"

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_extraction.py"


def _keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


class TestRunExtraction(unittest.TestCase):
    def test_no_audit_writes_the_stripped_json(self):
        """--no-audit goes through the generator; the always-present `metadata` key is gone."""
        with tempfile.TemporaryDirectory() as td:
            csv_file = Path(td) / "inventory.csv"
            csv_file.write_text("Name,Room,Category\nLamp,Lounge,Lighting\n", encoding="utf-8")
            out_dir = Path(td) / "bundle"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--input", str(csv_file), "--output", str(out_dir), "--no-audit"],
                capture_output=True, text=True, check=True,
            )
            data = json.loads((out_dir / "household_normalized.json").read_text(encoding="utf-8"))
            self.assertEqual(_keys(data) & STRIPPED_KEYS, set())
            self.assertNotIn("raw data", result.stdout.lower())


def _load():
    spec = importlib.util.spec_from_file_location("run_extraction_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _call(module, *argv):
    err = io.StringIO()
    with mock.patch.object(sys, "argv", ["run_extraction.py", *argv]), contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(err):
        try:
            module.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, err.getvalue()


class TestLiveStoreAndScratch(unittest.TestCase):
    """F-1: the live store is refused; a SQLite input is only copied, and the copy is what SQLite reads."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self.module = _load()
        self.snap = self.module._snapshot_module()
        self.scratch_root = self.base / "scratch"

    def tearDown(self):
        self._td.cleanup()

    def test_input_under_the_live_store_is_refused_even_through_a_symlink(self):
        live = self.base / "HomeKit"
        live.mkdir()
        (live / DB).write_bytes(b"x")
        link = self.base / ("link" + Path(DB).suffix)
        link.symlink_to(live / DB)
        seen = []
        with mock.patch.object(self.module, "HOMEKIT_ROOT", live), \
                mock.patch.object(self.module, "parse_homed_sqlite", lambda *a, **k: seen.append(a)):
            for path in (live / DB, live / WAL, link):
                (live / WAL).write_bytes(b"w")
                code, err = _call(self.module, "--input", str(path))
                self.assertEqual(code, 1)
                self.assertIn("refusing", err)
        self.assertEqual(seen, [])

    def test_sqlite_input_is_read_from_a_scratch_copy_that_is_removed(self):
        src = self.base / "copy"
        src.mkdir()
        (src / DB).write_bytes(b"db")
        (src / WAL).write_bytes(b"wal")
        (src / SHM).write_bytes(b"shm")
        seen = []

        def fake_parse(path, household_name=None):
            path = Path(path)
            seen.append((path, sorted(p.name for p in path.parent.iterdir()), path.read_bytes()))
            raise RuntimeError("stop here")  # no household needed: the path is the point

        with mock.patch.object(self.module, "HOMEKIT_ROOT", self.base / "elsewhere"), \
                mock.patch.object(self.snap, "SCRATCH", self.scratch_root), \
                mock.patch.object(self.module, "parse_homed_sqlite", fake_parse):
            code, _ = _call(self.module, "--input", str(src / DB))
        self.assertEqual(code, 1)
        (path, names, content), = seen
        self.assertNotEqual(path, src / DB)
        self.assertEqual(path.parent.parent, self.scratch_root)
        self.assertEqual((names, content), ([DB, WAL], b"db"))  # never -shm
        self.assertEqual(list(self.scratch_root.iterdir()), [])  # removed, also after the error
        self.assertEqual(self.scratch_root.stat().st_mode & 0o777, 0o700)


class TestExportFolder(unittest.TestCase):
    """F-6: the default export folder comes from a slug and stays inside exports/, created 0700."""

    def test_slug(self):
        module = _load()
        self.assertEqual(module._slug("../spill"), "___spill")
        self.assertEqual(module._slug("My Home"), "my_home")
        self.assertEqual(module._slug(""), "home")

    def test_a_hostile_home_name_stays_inside_exports(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            csv_file = base / "inventory.csv"
            csv_file.write_text("Name,Room,Category\nLamp,Lounge,Lighting\n", encoding="utf-8")
            module = _load()
            with mock.patch.object(module, "PROJECT_ROOT", base / "project"):
                code, _ = _call(module, "--input", str(csv_file), "--home", "../spill", "--no-audit")
            self.assertEqual(code, 0)
            exports = base / "project" / "exports"
            self.assertEqual([p.name for p in (base / "project").iterdir()], ["exports"])
            (out,) = list(exports.iterdir())
            self.assertEqual(out.name, "___spill_audit")
            self.assertEqual(out.stat().st_mode & 0o777, 0o700)
            self.assertTrue((out / "household_normalized.json").is_file())


if __name__ == "__main__":
    unittest.main()
