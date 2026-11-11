"""The terms builder: spellings, case rule, '#' skip, 0600 file, counts-only output; on fictional data only."""

import contextlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("build_scan_terms", ROOT / "scripts" / "build_scan_terms.py")
bst = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bst)


class TestBuildScanTerms(unittest.TestCase):

    def test_terms_rules(self):
        lines, skipped = bst.terms(["Véranda", "Lake Cabin", "A#B", "TV", "  Hooli   Motion "])
        self.assertEqual(lines, ["Hooli Motion", "Lake Cabin", "cs:Veranda", "cs:Véranda"])
        self.assertEqual(skipped, 1)

    def test_file_mode_newest_form_and_no_name_printed(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "data" / "tracking" / "h-1"
            home.mkdir(parents=True)
            old = {"form": {"home": {"id": "h-1", "name": "Old Name"}}}
            new = {"form": {"home": {"id": "h-1", "name": "Maple House"},
                            "rooms": {"r": {"name": "Galerie"}},
                            "accessories": {"a": {"name": "Hooli Motion", "manufacturer": "Hooli", "model": "M1",
                                                  "matter_vendor_id": 65523, "matter_product_id": 32770},
                                         "b": {"name": "Plain", "matter_vendor_id": None, "matter_product_id": 7}}}}
            (home / "2026-01-01_000000Z.json").write_text(json.dumps(old))
            (home / "2026-01-02_000000Z.json").write_text(json.dumps(new))
            extra = Path(td) / "extra.txt"
            extra.write_text("Jane Example\n")
            out = Path(td) / "terms"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                bst.main(["--data", str(Path(td) / "data"), "--out", str(out), "--extra", str(extra)])
            self.assertEqual(out.read_text().splitlines(),
                             ["Hooli Motion", "Jane Example", "Maple House", "cs:32770", "cs:65523", "cs:Galerie", "cs:Hooli",
                              "cs:Plain"])
            self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
            printed = buf.getvalue()
            self.assertEqual(printed, "build_scan_terms: 1 home(s), 8 term line(s), 0 skipped (contain #)\n")


if __name__ == "__main__":
    unittest.main()
