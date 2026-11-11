"""Release hardening, round 3: action values in the bundle, live-root guard entry points, scan-terms file mode,
home-name policy, and missing optional tables reported as unknown."""

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.audit.bundle_generator import BUNDLE_FILES, generate_llm_audit_bundle
from src.extractor.app_parser import parse_app_export
from src.extractor.sqlite_parser import parse_homed_sqlite
from src.models.schema import Action, Automation, Household, Scene
from src.store_files import DB, LiveStoreError
from src.tracking import canon, diff
from tests.test_sqlite_parser import _build_store, _build_zmkf

ROOT = Path(__file__).resolve().parent.parent
SECRET = "planted-secret"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _all_text(folder):
    return {n: (Path(folder) / n).read_text(encoding="utf-8") for n in BUNDLE_FILES if (Path(folder) / n).exists()}


class TestActionValuesInBundle(unittest.TestCase):
    def _assert_clean(self, household, td):
        out = generate_llm_audit_bundle(household, Path(td) / "b")
        texts = _all_text(out)
        self.assertTrue(texts)
        for name, text in texts.items():
            self.assertNotIn(SECRET, text, name)
        return out

    def test_household_action_values_are_scrubbed(self):
        value = {"password": SECRET, "level": 40, "nested": [{"Token": SECRET, "ok": 1}]}
        h = Household(id="h", name="Maple House")
        h.scenes["s"] = Scene(id="s", name="Evening", actions=[
            Action(target_accessory_id="a", target_characteristic_type="Brightness", target_value=value)])
        h.automations["t"] = Automation(id="t", name="Rule", trigger_type="timer", actions=[
            Action(target_accessory_id="a", target_characteristic_type="Brightness", target_value=[value])])
        with tempfile.TemporaryDirectory() as td:
            out = self._assert_clean(h, td)
            data = json.loads((out / "household_normalized.json").read_text())
            self.assertEqual(data["scenes"]["s"]["actions"][0]["target_value"]["level"], 40)

    def test_exports_with_secret_action_values(self):
        action = {"accessoryIdentifier": "a", "characteristicType": "Brightness", "targetValue": {"password": SECRET}}
        for controller in (False, True):
            export = {"homes": [{"name": "Lake Cabin", "uniqueIdentifier": "h"}], "accessories": [], "rooms": [],
                      "scenes": [{"name": "Evening", "uniqueIdentifier": "s", "actions": [action]}],
                      "automations": [{"name": "R", "uniqueIdentifier": "t", "actions": [action]}]}
            if controller:
                export["version"] = 1
            with self.subTest(controller=controller), tempfile.TemporaryDirectory() as td:
                src = Path(td) / "export.json"
                src.write_text(json.dumps(export), encoding="utf-8")
                self._assert_clean(parse_app_export(src), td)


class TestLiveRootEntryPoints(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self.live = self.base / "HomeKit"
        self.live.mkdir()
        self.alias = self.base / "alias"
        self.alias.symlink_to(self.live)

    def tearDown(self):
        self._td.cleanup()

    def _tree(self):
        return sorted(p.name for p in self.live.rglob("*"))

    def test_track_changes_refuses_folders_inside_the_live_root(self):
        track = _load("track_changes")
        snap = track._snapshot_module()
        for label, data, logs in (("data", self.alias / "d", self.base / "logs"),
                                  ("logs", self.base / "d", self.alias / "logs"),
                                  ("direct", self.live / "d", self.base / "logs")):
            with self.subTest(label=label), mock.patch.object(snap, "SOURCE", self.live):
                with self.assertRaises(snap.SnapshotError) as ctx:
                    track.run(source=self.base / "none", data=data, logs=logs)
                self.assertEqual(ctx.exception.code, "E_LIVE")
                self.assertEqual(self._tree(), [])
        self.assertFalse((self.base / "d").exists())

    def test_track_changes_main_prints_the_fixed_code(self):
        track = _load("track_changes")
        snap = track._snapshot_module()
        buf = io.StringIO()
        with mock.patch.object(snap, "SOURCE", self.live), mock.patch.object(track, "DATA", self.alias / "d"), \
                mock.patch.object(track, "run", side_effect=snap.SnapshotError("E_LIVE")), \
                mock.patch("sys.argv", ["track_changes"]), contextlib.redirect_stdout(buf):
            self.assertEqual(track.main(), 1)
        self.assertIn("E_LIVE", buf.getvalue())

    def test_build_scan_terms_refuses_out_inside_the_live_root(self):
        build = _load("build_scan_terms")
        data = self.base / "data"
        (data / "tracking").mkdir(parents=True)
        for out in (self.alias / "release" / "terms", self.live / "terms"):
            with mock.patch("src.store_files.LIVE_ROOT", self.live), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(build.main(["--data", str(data), "--out", str(out)]), 2)
            self.assertEqual(self._tree(), [])

    def test_parser_refuses_a_database_inside_the_live_root(self):
        db = self.live / DB
        _build_zmkf(db)
        with mock.patch("src.store_files.LIVE_ROOT", self.live):
            for path in (db, self.alias / DB):
                with self.subTest(path=path.parent.name), self.assertRaises(LiveStoreError):
                    parse_homed_sqlite(path, household_name="Maple House")
            copy = self.base / DB
            copy.write_bytes(db.read_bytes())
            self.assertEqual(parse_homed_sqlite(copy, household_name="Maple House").name, "Maple House")


class TestScanTermsFileMode(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        (self.base / "data" / "tracking").mkdir(parents=True)
        self.build = _load("build_scan_terms")

    def tearDown(self):
        self._td.cleanup()

    def _run(self, out):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return self.build.main(["--data", str(self.base / "data"), "--out", str(out)])

    def test_new_and_existing_files_end_0600(self):
        fresh = self.base / "fresh"
        self.assertEqual(self._run(fresh), 0)
        self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o600)
        old = self.base / "old"
        old.write_text("x\n")
        os.chmod(old, 0o644)
        self.assertEqual(self._run(old), 0)
        self.assertEqual(stat.S_IMODE(old.stat().st_mode), 0o600)

    def test_a_symlink_or_directory_destination_is_refused(self):
        target = self.base / "target"
        target.write_text("keep\n")
        link = self.base / "link"
        link.symlink_to(target)
        self.assertEqual(self._run(link), 2)
        self.assertEqual(target.read_text(), "keep\n")
        folder = self.base / "folder"
        folder.mkdir()
        self.assertEqual(self._run(folder), 2)


class TestHomeNamePolicy(unittest.TestCase):
    def test_flat_export_checks_the_requested_home(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "export.json"
            path.write_text(json.dumps({"name": "Maple House", "accessories": [], "rooms": []}), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                parse_app_export(path, household_name="Lake Cabin")
            self.assertIn("Lake Cabin", str(ctx.exception))
            self.assertEqual(parse_app_export(path, household_name="maple house").name, "Maple House")
            self.assertEqual(parse_app_export(path).name, "Maple House")

    def test_ambiguous_names_raise_in_exports_and_stores(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "export.json"
            path.write_text(json.dumps({"homes": [{"name": "Maple House"}, {"name": "MAPLE house"}],
                                        "accessories": [], "rooms": []}), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                parse_app_export(path, household_name="maple house")
            self.assertIn("ambiguous", str(ctx.exception))
            zmkf = Path(td) / ("zmkf" + Path(DB).suffix)
            _build_zmkf(zmkf)
            conn = sqlite3.connect(zmkf)
            conn.execute("UPDATE ZMKFHOME SET ZNAME = 'MAPLE HOUSE' WHERE Z_PK = 2")
            conn.commit()
            conn.close()
            with self.assertRaises(ValueError) as ctx:
                parse_homed_sqlite(zmkf, household_name="Maple House")
            self.assertIn("ambiguous", str(ctx.exception))
            legacy = Path(td) / ("legacy" + Path(DB).suffix)
            conn = _build_store(legacy)
            conn.execute("INSERT INTO ZHMHOMEMODEL (Z_PK, ZNAME, ZUUID) VALUES (2, 'Lake Cabin', ?)", (b"home000000000002",))
            conn.execute("UPDATE ZHMHOMEMODEL SET ZNAME = 'Lake Cabin' WHERE Z_PK = 1")
            conn.commit()
            conn.close()
            with self.assertRaises(ValueError) as ctx:
                parse_homed_sqlite(legacy, household_name="lake cabin")
            self.assertIn("ambiguous", str(ctx.exception))


class TestMissingOptionalTables(unittest.TestCase):
    def _form(self, td, skip):
        """Maple House with zones, groups and a pending update, minus the one table named in `skip`."""
        db = Path(td) / f"{len(list(Path(td).iterdir()))}{Path(DB).suffix}"
        _build_zmkf(db, skip_table=skip, profile=True)
        if skip != "ZMKFZONE":
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE ZMKFZONE (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, "
                         "ZHOME INTEGER, ZNAME VARCHAR, ZMODELID BLOB)")
            conn.execute("INSERT INTO ZMKFZONE (Z_PK, Z_ENT, ZHOME, ZNAME, ZMODELID) VALUES (900, 0, 1, 'Downstairs', ?)",
                         (b"zone000000000900",))
            conn.commit()
            conn.close()
        return canon.canonical(parse_homed_sqlite(db, household_name="Maple House"))

    def test_missing_table_is_unknown_not_removed(self):
        with tempfile.TemporaryDirectory() as td:
            previous = self._form(td, None)
            self.assertNotIn("unavailable", previous)  # the form of a complete store does not change
            self.assertTrue(previous["zones"] and previous["service_groups"])
            for table, label, entity in (("ZMKFZONE", "zones", "zone"),
                                         ("ZMKFSERVICEGROUP", "service_groups", "service group")):
                with self.subTest(table=table):
                    new = self._form(td, table)
                    self.assertEqual(new["unavailable"], [label])
                    removed = {c["entity"] for c in diff.diff(previous, new) if c["kind"] == "removed"}
                    self.assertNotIn(entity, removed)
                    self.assertEqual(removed, set())
                    self.assertEqual(diff.diff(new, previous), [])  # and not reported as added either

    def test_missing_update_table_keeps_pending_updates_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            previous = self._form(td, None)
            self.assertTrue(any(a["pending_update"] for a in previous["accessories"].values()))
            new = self._form(td, "ZMKFSOFTWAREUPDATE")
            self.assertEqual(new["unavailable"], ["pending_updates"])
            self.assertEqual(diff.diff(previous, new), [])

    def test_present_tables_still_report_removals(self):
        with tempfile.TemporaryDirectory() as td:
            previous = self._form(td, None)
            new = json.loads(json.dumps(previous))
            new["zones"] = {}
            changes = diff.diff(previous, new)
            self.assertTrue(any(c["kind"] == "removed" and c["entity"] == "zone" for c in changes))


if __name__ == "__main__":
    unittest.main()
