"""Release hardening, round 6: hard links on chmod and database opens, no bytecode cache, the smoke test's temp folder.

Fictional folders only: a fake live root under a temporary directory.
"""

import ast
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import release_manifest
from src import safe_io
from src.store_files import DB, LiveStoreError
from tests.test_release_round4 import ROOT, _Live, _load


class TestHardLinkOnChmodAndDatabases(_Live):
    def setUp(self):
        super().setUp()
        self.db = self.live / ("store" + Path(DB).suffix)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
        conn.close()
        os.chmod(self.db, 0o600)
        self.db_bytes = self.db.read_bytes()
        self.link = self.base / ("alias" + Path(DB).suffix)
        os.link(self.db, self.link)

    def test_chmod_and_both_opens_refuse_a_second_name_of_a_live_file(self):
        for label, call in (("chmod", lambda: safe_io.chmod(self.link, 0o644)),
                            ("read-only open", lambda: safe_io.sqlite_ro(self.link)),
                            ("scratch open", lambda: safe_io.sqlite_scratch(self.link))):
            with self.subTest(label=label), self.assertRaises(LiveStoreError):
                call()
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.db.read_bytes(), self.db_bytes)

    def test_a_folder_and_a_single_named_file_are_still_accepted(self):
        folder = self.base / "folder"
        folder.mkdir()
        (folder / "sub").mkdir()  # a folder has several links by nature
        safe_io.chmod(folder, 0o700)
        single = self.base / ("copy" + Path(DB).suffix)
        shutil.copyfile(self.db, single)
        safe_io.chmod(single, 0o600)
        safe_io.sqlite_ro(single).close()
        safe_io.sqlite_scratch(single).close()


def _released_scripts():
    return [f for f in release_manifest.MAP if f.startswith("scripts/") and f.endswith(".py")]


class TestNoBytecodeCache(unittest.TestCase):
    def test_every_released_script_turns_bytecode_off_before_project_imports(self):
        for rel in _released_scripts():
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            off, first_project = None, None
            for node in tree.body:
                if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "dont_write_bytecode"
                                                         for t in node.targets)
                        and isinstance(node.value, ast.Constant) and node.value.value is True and off is None):
                    off = node.lineno
                if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in ("src", "tests"):
                    first_project = first_project or node.lineno
                if isinstance(node, ast.Import) and any(a.name.split(".")[0] in ("src", "tests") for a in node.names):
                    first_project = first_project or node.lineno
            imports_project = "from src" in (ROOT / rel).read_text(encoding="utf-8") or "import src" in (ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(script=rel):
                if imports_project:
                    self.assertIsNotNone(off, "sys.dont_write_bytecode = True missing")
                    if first_project:
                        self.assertLess(off, first_project)

    def test_the_launcher_runs_python_with_minus_b(self):
        text = (ROOT / "launchd" / "track-changes.plist.template").read_text(encoding="utf-8")
        args = plistlib.loads(text.replace("{{PYTHON}}", "/py").replace("{{PROJECT_DIR}}", "/p")
                              .replace("{{HOUR}}", "12").replace("{{MINUTE}}", "0").encode())["ProgramArguments"]
        self.assertEqual(args[:2], ["/py", "-B"])

    def test_a_cache_folder_linked_into_the_live_root_receives_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            live = base / "HomeKit"
            live.mkdir()
            copy = base / "copy"
            shutil.copytree(ROOT / "src", copy / "src", ignore=shutil.ignore_patterns("__pycache__", "energy"))
            (copy / "scripts").mkdir()
            shutil.copy(ROOT / "scripts" / "release_manifest.py", copy / "scripts")
            (copy / "src" / "__pycache__").symlink_to(live)
            env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
            subprocess.run([sys.executable, str(copy / "scripts" / "release_manifest.py"), "check", str(base / "none")],
                           cwd=base, env=env, capture_output=True)
            self.assertEqual(list(live.iterdir()), [])


class TestSmokeTempFolder(_Live):
    def test_a_temp_folder_inside_the_live_root_is_refused_before_any_write(self):
        smoke = _load("smoke_test")
        inside = self.live / "tmp"
        inside.mkdir()
        before = self.tree()
        with mock.patch.object(tempfile, "tempdir", str(inside)):
            with self.assertRaises(LiveStoreError):
                smoke.main(["--fixture"])
        self.assertEqual(self.tree(), before)
        self.intact()

    def test_a_temp_folder_above_the_live_root_is_refused(self):
        smoke = _load("smoke_test")
        with mock.patch.object(tempfile, "tempdir", str(self.base)):
            with self.assertRaises(LiveStoreError):
                smoke.main(["--fixture"])
        self.intact()


if __name__ == "__main__":
    unittest.main()
