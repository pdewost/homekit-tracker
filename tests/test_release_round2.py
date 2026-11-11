"""Release hardening, round 2: bundle secrets, live-root guard, file modes, home choice, missing event table."""

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.audit.bundle_generator import BUNDLE_FILES, generate_llm_audit_bundle
from src.extractor.app_parser import detect_export_type, parse_app_export
from src.extractor.sqlite_parser import parse_homed_sqlite
from src.models.schema import Accessory, Automation, Characteristic, Household, Service
from src.store_files import DB
from tests.test_sqlite_parser import _build_store, _build_zmkf

ROOT = Path(__file__).resolve().parent.parent
SERIAL_UUID = "00000030-0000-1000-8000-0026BB765291"
PLANTED = "planted-serial"
SECRET = "planted-secret"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _all_text(folder):
    return {n: (Path(folder) / n).read_text(encoding="utf-8") for n in BUNDLE_FILES if (Path(folder) / n).exists()}


class TestN1BundleSecrets(unittest.TestCase):
    def _household(self):
        h = Household(id="h", name="Test Home")
        chars = [Characteristic(id="c1", type=SERIAL_UUID, value=PLANTED),
                 Characteristic(id="c2", type="SerialNumber", value=PLANTED),
                 Characteristic(id="c3", type="x", type_name="SerialNumber", value=PLANTED)]
        h.accessories["a"] = Accessory(id="a", name="Lamp", services=[
            Service(id="s", name="Info", service_type="AccessoryInformation", characteristics=chars)])
        h.automations["t"] = Automation(
            id="t", name="Rule", trigger_type="event",
            triggers=[{"kind": "Characteristic", "wifiPassword": SECRET, "nested": [{"Token": SECRET}]}],
            conditions=[{"keyPath": SECRET, "text": "ok", "SSID": SECRET}])
        return h

    def test_serial_and_secret_fields_reach_no_bundle_file(self):
        with tempfile.TemporaryDirectory() as td:
            out = generate_llm_audit_bundle(self._household(), Path(td) / "b")
            for name, text in _all_text(out).items():
                self.assertNotIn(PLANTED, text, name)
                self.assertNotIn(SECRET, text, name)
            data = json.loads((out / "household_normalized.json").read_text())
            self.assertEqual(data["automations"]["t"]["conditions"][0]["text"], "ok")

    def test_controller_export_through_the_parser(self):
        export = {"version": 1, "homes": [{"name": "Lake Cabin", "uniqueIdentifier": "h"}], "accessories": [{
            "name": "Lamp", "uniqueIdentifier": "a", "services": [{"serviceType": "AccessoryInformation", "characteristics": [
                {"characteristicType": SERIAL_UUID, "value": PLANTED}, {"characteristicType": "SerialNumber", "value": PLANTED}]}]}],
            "automations": [{"name": "R", "conditions": [{"password": SECRET}], "events": [{"secret": SECRET}]}]}
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "export.json"
            src.write_text(json.dumps(export), encoding="utf-8")
            out = generate_llm_audit_bundle(parse_app_export(src), Path(td) / "b")
            for name, text in _all_text(out).items():
                self.assertNotIn(PLANTED, text, name)
                self.assertNotIn(SECRET, text, name)


class TestN2LiveRootGuard(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self.live = self.base / "HomeKit"
        self.live.mkdir()
        self.snap = _load("snapshot_homekit")
        self.alias = self.base / "alias"
        self.alias.symlink_to(self.live)

    def tearDown(self):
        self._td.cleanup()

    def test_guard_refuses_root_beneath_symlink_and_missing_paths(self):
        with mock.patch.object(self.snap, "SOURCE", self.live):
            for path in (self.live, self.live / "new" / "deeper", self.alias, self.alias / "new"):
                with self.subTest(path=path), self.assertRaises(self.snap.SnapshotError) as ctx:
                    self.snap.refuse_live_store(path)
                self.assertEqual(ctx.exception.code, "E_LIVE")
            self.snap.refuse_live_store(self.base / "elsewhere")
            self.snap.refuse_live_store(self.base / "HomeKit-other")  # a name prefix is not a child

    def test_snapshot_and_scratch_entry_points_refuse_before_writing(self):
        src = self.base / "src"
        src.mkdir()
        (src / DB).write_bytes(b"x")
        with mock.patch.object(self.snap, "SOURCE", self.live):
            with self.assertRaises(self.snap.SnapshotError):
                self.snap.take_snapshot(src, self.alias / "snaps")
            with self.assertRaises(self.snap.SnapshotError):
                self.snap.copy_to_scratch(src / DB, self.alias / "scratch")
            with self.assertRaises(self.snap.SnapshotError):
                self.snap.validate(src, self.alias / "scratch")
        self.assertEqual(list(self.live.iterdir()), [])

    def test_run_extraction_output_inside_the_live_root_is_refused(self):
        module = _load("run_extraction")
        csv_file = self.base / "inventory.csv"
        csv_file.write_text("Name,Room,Category\nLamp,Lounge,Lighting\n", encoding="utf-8")
        for out in (self.live / "bundle", self.alias / "bundle"):
            err = io.StringIO()
            with mock.patch.object(module, "HOMEKIT_ROOT", self.live), \
                    mock.patch.object(sys, "argv", ["x", "--input", str(csv_file), "--output", str(out)]), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    module.main()
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("refusing", err.getvalue())
        self.assertEqual(list(self.live.iterdir()), [])


class TestN3Modes(unittest.TestCase):
    def test_existing_loose_folder_becomes_0700_and_files_0600(self):
        old = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "bundle"
                out.mkdir(mode=0o755)
                os.chmod(out, 0o755)
                generate_llm_audit_bundle(Household(id="h", name="Test Home"), out)
                self.assertEqual(out.stat().st_mode & 0o777, 0o700)
                for name in BUNDLE_FILES:
                    self.assertEqual((out / name).stat().st_mode & 0o777, 0o600, name)
        finally:
            os.umask(old)

    def test_files_left_by_an_earlier_run_become_0600(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "bundle"
            out.mkdir(mode=0o700)
            for name in BUNDLE_FILES:
                (out / name).write_text("old", encoding="utf-8")
                os.chmod(out / name, 0o644)
            generate_llm_audit_bundle(Household(id="h", name="Test Home"), out)
            for name in BUNDLE_FILES:
                self.assertEqual((out / name).stat().st_mode & 0o777, 0o600, name)

    def test_folder_owned_by_someone_else_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("src.audit.bundle_generator.os.getuid", return_value=os.getuid() + 1):
                with self.assertRaises(PermissionError):
                    generate_llm_audit_bundle(Household(id="h", name="Test Home"), Path(td))


class TestN5HomeChoice(unittest.TestCase):
    def _export(self, td, homes, controller):
        path = Path(td) / "export.json"
        data = {"homes": homes, "accessories": [], "rooms": []}
        if controller:
            data["version"] = 1  # what detect_export_type reads as a Controller export
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_catalyst_and_controller_policy(self):
        two = [{"name": "Maple House", "uniqueIdentifier": "1"}, {"name": "Lake Cabin", "uniqueIdentifier": "2"}]
        one = two[:1]
        with tempfile.TemporaryDirectory() as td:
            for label, controller in (("catalyst", False), ("controller", True)):
                path = self._export(td, two, controller)
                with self.subTest(label=label):
                    self.assertEqual(detect_export_type(path), f"{label}_json")
                    with self.assertRaises(ValueError) as ctx:
                        parse_app_export(path, household_name="Chalet")
                    self.assertIn("Chalet", str(ctx.exception))
                    with self.assertRaises(ValueError) as ctx:
                        parse_app_export(path)
                    self.assertIn("--home", str(ctx.exception))
                    if controller:  # its entity arrays carry no home reference: one home cannot be cut out
                        with self.assertRaises(ValueError) as ctx:
                            parse_app_export(path, household_name="lake cabin")
                        self.assertIn("several homes", str(ctx.exception))
                    else:
                        self.assertEqual(parse_app_export(path, household_name="lake cabin").name, "Lake Cabin")
                single = self._export(td, one, controller)
                with self.subTest(label=label, homes=1):
                    self.assertEqual(parse_app_export(single).name, "Maple House")
                    with self.assertRaises(ValueError):
                        parse_app_export(single, household_name="Chalet")

    def test_legacy_sqlite_policy(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / DB
            conn = _build_store(db)
            conn.execute("INSERT INTO ZHMHOMEMODEL (Z_PK, ZNAME, ZUUID) VALUES (2, 'Lake Cabin', ?)", (b"home000000000002",))
            conn.commit()
            conn.close()
            with self.assertRaises(ValueError) as ctx:
                parse_homed_sqlite(db)
            self.assertIn("--home", str(ctx.exception))
            with self.assertRaises(ValueError) as ctx:
                parse_homed_sqlite(db, household_name="Chalet")
            self.assertIn("Chalet", str(ctx.exception))
            self.assertEqual(parse_homed_sqlite(db, household_name="Lake Cabin").name, "Lake Cabin")
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / DB
            _build_store(db).close()
            self.assertEqual(parse_homed_sqlite(db).name, "Maple House")
            with self.assertRaises(ValueError):
                parse_homed_sqlite(db, household_name="Chalet")


class TestN7EventTable(unittest.TestCase):
    def test_missing_event_table_marks_triggers_unknown_not_hidden(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / DB
            _build_zmkf(db, skip_table="ZMKFEVENT")
            household = parse_homed_sqlite(db, household_name="Maple House")
            autos = {a.name: a for a in household.automations.values()}
            self.assertFalse(any(a.hidden_in_app for a in autos.values()))
            self.assertEqual(autos["Sunset"].triggers[0]["kind"], "unknown")
            out = generate_llm_audit_bundle(household, Path(td) / "b")
            self.assertNotIn("cannot fire", (out / "03_automations_analysis.md").read_text())

    def test_present_event_table_still_flags_an_eventless_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / DB
            _build_zmkf(db)
            conn = sqlite3.connect(db)
            conn.execute("DELETE FROM ZMKFEVENT")
            conn.commit()
            conn.close()
            household = parse_homed_sqlite(db, household_name="Maple House")
            self.assertTrue({a.name: a for a in household.automations.values()}["Sunset"].hidden_in_app)


if __name__ == "__main__":
    unittest.main()
