"""The release manifest: every listed file exists, extraction is exact, and an extra or missing file is caught."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_manifest", ROOT / "scripts" / "release_manifest.py")
rm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rm)


class TestReleaseManifest(unittest.TestCase):

    def test_every_listed_file_exists_here(self):
        self.assertEqual([f for f in rm.MAP if not (ROOT / f).is_file()], [])

    def test_extract_is_exact_and_check_catches_drift(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "release"
            self.assertEqual(rm.extract(dest), len(rm.MAP))
            self.assertEqual(rm.check(dest), ([], []))
            for rel in rm.MAP:
                self.assertEqual((dest / rel).read_bytes(), (ROOT / rel).read_bytes(), rel)
            (dest / "notes.txt").write_text("x")
            (dest / "README.md").unlink()
            self.assertEqual(rm.check(dest), (["README.md"], ["notes.txt"]))

    def test_extract_refuses_a_non_empty_folder(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "x").write_text("x")
            with self.assertRaises(FileExistsError):
                rm.extract(Path(td))

    def test_only_allowed_places_are_listed(self):
        root_files = {".gitignore", "LICENSE", "README.md", "DISCLAIMER.md", "SECURITY.md", "requirements.txt"}
        src_parts = {"__init__.py", "store_files.py", "safe_io.py", "audit", "extractor", "models", "tracking"}
        bad = [f for f in rm.MAP
               if not (f in root_files
                       or f.startswith(("scripts/", "tests/", "launchd/"))
                       and f.split("/")[0] in ("scripts", "tests", "launchd"))
               and not (f.startswith("src/") and f.split("/")[1] in src_parts)]
        self.assertEqual(bad, [])
        self.assertEqual([f for f in rm.MAP if f.startswith("launchd/") and not f.endswith(".template")], [])
        self.assertNotIn("scripts/local_notifier.py", rm.MAP)  # the local notifier never ships (README)
        self.assertIn("scripts/local_notifier.example.py", rm.MAP)  # an example does, git-ignore protects the copy
        self.assertIn("tests/test_release_round2.py", rm.MAP)  # the round-2 review tests ship with the code
        self.assertIn("tests/test_release_round3.py", rm.MAP)
        self.assertIn("tests/test_release_round4.py", rm.MAP)
        self.assertIn("tests/test_release_round5.py", rm.MAP)
        self.assertIn("tests/test_release_round6.py", rm.MAP)


if __name__ == "__main__":
    unittest.main()
