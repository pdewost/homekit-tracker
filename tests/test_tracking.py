"""Change tracking: the safe copy and its scratch validation."""

import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.store_files import DB, WAL, SHM  # noqa: E402
_spec = importlib.util.spec_from_file_location("snapshot_homekit", ROOT / "scripts" / "snapshot_homekit.py")
snapshot_homekit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(snapshot_homekit)
SnapshotError = snapshot_homekit.SnapshotError


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _WalStore:
    """A WAL-mode store with ZMKFHOME rows; the connection stays open so the -wal file holds the data."""

    def __init__(self, folder, homes=2):
        self.path = Path(folder) / DB
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA wal_autocheckpoint=0")
        self.conn.execute("CREATE TABLE ZMKFHOME (Z_PK INTEGER PRIMARY KEY, ZNAME VARCHAR)")
        self.conn.executemany("INSERT INTO ZMKFHOME (ZNAME) VALUES (?)", [(f"H{i}",) for i in range(homes)])
        self.conn.commit()

    def close(self):
        self.conn.close()


class TestSafeCopy(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        base = Path(self._td.name)
        self.src, self.dest, self.scratch = base / "HomeKit", base / "snapshots", base / "scratch"
        self.src.mkdir()
        self.store = _WalStore(self.src)

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _source_state(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(self.src.iterdir())
                if p.name in (DB, WAL)}

    def test_stable_source_gives_one_identical_snapshot_without_shm(self):
        self.assertTrue((self.src / WAL).exists())
        self.assertTrue((self.src / SHM).exists())
        before = self._source_state()
        snap = snapshot_homekit.take_snapshot(self.src, self.dest)
        self.assertEqual([p.name for p in self.dest.iterdir()], [snap.name])
        self.assertRegex(snap.name, r"^\d{4}-\d{2}-\d{2}_\d{6}Z$")
        self.assertEqual(sorted(p.name for p in snap.iterdir()), [DB, WAL, "snapshot.json"])
        meta = json.loads((snap / "snapshot.json").read_text())
        for name in (DB, WAL):
            self.assertEqual((snap / name).read_bytes(), before[name][0])
            self.assertEqual(meta["files"][name]["sha256"], _sha(snap / name))
        self.assertEqual(self._source_state(), before)

    def test_wal_copy_without_shm_opens_in_scratch_and_snapshot_is_untouched(self):
        snap = snapshot_homekit.take_snapshot(self.src, self.dest)
        hashes = {p.name: _sha(p) for p in snap.iterdir()}
        scratch = snapshot_homekit.validate(snap, self.scratch)
        conn = sqlite3.connect(f"file:{scratch / DB}?mode=ro", uri=True)
        self.assertEqual(conn.execute("SELECT count(*) FROM ZMKFHOME").fetchone()[0], 2)
        conn.close()
        self.assertEqual({p.name: _sha(p) for p in snap.iterdir()}, hashes)
        snapshot_homekit.remove_scratch(scratch, self.scratch)
        self.assertFalse(scratch.exists())

    def test_source_rewritten_once_is_retried_and_accepted(self):
        calls = []

        def hook(attempt):
            calls.append(attempt)
            if attempt == 1:
                self.store.conn.execute("INSERT INTO ZMKFHOME (ZNAME) VALUES ('late')")
                self.store.conn.commit()

        snap = snapshot_homekit.take_snapshot(self.src, self.dest, _between_copies=hook)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(json.loads((snap / "snapshot.json").read_text())["attempts"], 2)

    def test_source_rewritten_every_time_fails_torn_with_no_folder_left(self):
        def hook(attempt):
            self.store.conn.execute("INSERT INTO ZMKFHOME (ZNAME) VALUES (?)", (f"x{attempt}",))
            self.store.conn.commit()

        with self.assertRaises(SnapshotError) as ctx:
            snapshot_homekit.take_snapshot(self.src, self.dest, _between_copies=hook)
        self.assertEqual(ctx.exception.code, "E_TORN")
        self.assertEqual(list(self.dest.iterdir()) if self.dest.exists() else [], [])

    def test_mtime_change_with_identical_bytes_is_retried(self):
        def hook(attempt):
            if attempt == 1:
                st = (self.src / DB).stat()
                os.utime(self.src / DB, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

        snap = snapshot_homekit.take_snapshot(self.src, self.dest, _between_copies=hook)
        self.assertEqual(json.loads((snap / "snapshot.json").read_text())["attempts"], 2)

    def test_corrupt_store_fails_integrity(self):
        self.store.close()
        for name in (WAL, SHM):
            (self.src / name).unlink(missing_ok=True)
        (self.src / DB).write_bytes(b"SQLite format 3\x00" + os.urandom(4096))
        snap = snapshot_homekit.take_snapshot(self.src, self.dest)
        with self.assertRaises(SnapshotError) as ctx:
            snapshot_homekit.validate(snap, self.scratch)
        self.assertEqual(ctx.exception.code, "E_INTEGRITY")

    def test_empty_store_and_store_without_homes_fail_empty(self):
        self.store.close()
        for name in (WAL, SHM):
            (self.src / name).unlink(missing_ok=True)
        (self.src / DB).write_bytes(b"")
        with self.assertRaises(SnapshotError) as ctx:
            snapshot_homekit.validate(snapshot_homekit.take_snapshot(self.src, self.dest), self.scratch)
        self.assertEqual(ctx.exception.code, "E_EMPTY")
        (self.src / DB).unlink()
        conn = sqlite3.connect(self.src / DB)
        conn.execute("CREATE TABLE ZMKFHOME (Z_PK INTEGER PRIMARY KEY, ZNAME VARCHAR)")
        conn.commit()
        conn.close()
        with self.assertRaises(SnapshotError) as ctx:
            snapshot_homekit.validate(snapshot_homekit.take_snapshot(self.src, self.dest), self.scratch)
        self.assertEqual(ctx.exception.code, "E_EMPTY")

    @unittest.skipIf(os.geteuid() == 0, "root reads any file")
    def test_unreadable_source_fails_fda_and_names_the_real_binary(self):
        target = self.src / DB
        target.chmod(0)
        try:
            with self.assertRaises(SnapshotError) as ctx:
                snapshot_homekit.take_snapshot(self.src, self.dest)
        finally:
            target.chmod(0o644)
        self.assertEqual(ctx.exception.code, "E_FDA")
        self.assertIn(os.path.realpath(sys.executable), str(ctx.exception))
        self.assertEqual(list(self.dest.iterdir()) if self.dest.exists() else [], [])

    def test_snapshot_and_scratch_are_private_whatever_the_umask(self):
        old = os.umask(0o022)
        try:
            snap = snapshot_homekit.take_snapshot(self.src, self.dest)
            scratch = snapshot_homekit.validate(snap, self.scratch)
        finally:
            os.umask(old)
        modes = {p: p.stat().st_mode & 0o777 for p in
                 [self.dest, snap, self.scratch, scratch, *snap.iterdir(), *scratch.iterdir()]}
        self.assertEqual({p: m for p, m in modes.items() if m != (0o700 if p.is_dir() else 0o600)}, {})
        snapshot_homekit.remove_scratch(scratch, self.scratch)

    def test_the_fda_hint_names_the_real_interpreter_on_stderr_only(self):
        import contextlib
        import io
        from unittest import mock
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(snapshot_homekit, "take_snapshot", side_effect=SnapshotError("E_FDA")), \
                mock.patch.object(sys, "argv", ["snapshot_homekit.py", "--dest", str(self.dest)]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(snapshot_homekit.main(), 1)
        self.assertEqual(out.getvalue(), "E_FDA\n")
        self.assertIn(os.path.realpath(sys.executable), err.getvalue())

    def test_missing_store_fails_empty(self):
        self.store.close()
        for p in list(self.src.iterdir()):
            p.unlink()
        with self.assertRaises(SnapshotError) as ctx:
            snapshot_homekit.take_snapshot(self.src, self.dest)
        self.assertEqual(ctx.exception.code, "E_EMPTY")


from src.extractor.sqlite_parser import parse_homed_sqlite  # noqa: E402
from tests.test_sqlite_parser import NIGHT_CONDITION, _archive, _build_zmkf, _offset, _compare, _now  # noqa: E402
from src.tracking import canon, diff  # noqa: E402

MAPLE = "Maple House"


def _db(folder, edits=(), name=DB):
    """The profile fixture plus a second room (12) in Maple, then the given SQL edits."""
    path = Path(folder) / name
    path.unlink(missing_ok=True)
    _build_zmkf(path, profile=True)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO ZMKFROOM (Z_PK, Z_ENT, ZHOME, ZNAME, ZMODELID) VALUES (12, 0, 1, 'Study', ?)",
                 (hashlib.md5(b"ZMKFROOM:12").digest(),))
    for sql, params in edits:
        conn.execute(sql, params)
    conn.commit()
    conn.close()
    return path


def _canon(path):
    return canon.canonical(parse_homed_sqlite(path, household_name=MAPLE))


def _reloaded(form, folder):
    p = Path(folder) / "description.json"
    p.write_text(json.dumps(form), encoding="utf-8")
    return json.loads(p.read_text(encoding="utf-8"))


class TestHomesById(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = _db(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _set_names(self, names):
        conn = sqlite3.connect(self.path)
        for pk, name in names.items():
            conn.execute("UPDATE ZMKFHOME SET ZNAME=? WHERE Z_PK=?", (name, pk))
        conn.commit()
        conn.close()

    def test_ordinals_follow_z_pk_and_ids_come_from_zmodelid(self):
        homes = canon.list_homes(self.path)
        self.assertEqual([(h["ordinal"], h["name"]) for h in homes], [(1, MAPLE), (2, "Lake Cabin"), (3, "Barge")])
        self.assertEqual(len({h["id"] for h in homes}), 3)

    def test_case_folded_duplicates_and_nameless_homes_get_codes(self):
        self._set_names({2: "Home", 3: "home"})
        ok, failures = canon.classify_homes(canon.list_homes(self.path))
        self.assertEqual([h["name"] for h in ok], [MAPLE])
        self.assertEqual(failures, [(2, "E_DUP_HOME_NAME"), (3, "E_DUP_HOME_NAME")])
        self._set_names({2: None, 3: ""})
        ok, failures = canon.classify_homes(canon.list_homes(self.path))
        self.assertEqual(failures, [(2, "E_NO_HOME_NAME"), (3, "E_NO_HOME_NAME")])

    def test_home_list_changes_by_id(self):
        before = canon.list_homes(self.path)
        self._set_names({2: "Country Home"})
        after = canon.list_homes(self.path)
        entries = diff.diff_home_lists(before, after)
        self.assertEqual([(e["kind"], e["field"], e["before"], e["after"]) for e in entries],
                         [("changed", "name", "Lake Cabin", "Country Home")])
        self.assertEqual([(e["kind"], e["entity"]) for e in diff.diff_home_lists(before, before[:2])],
                         [("removed", "home")])
        self.assertEqual([(e["kind"], e["entity"]) for e in diff.diff_home_lists(before[:2], before)],
                         [("added", "home")])

    def test_a_skipped_home_is_not_reported_removed(self):
        before = canon.list_homes(self.path)
        self._set_names({2: "Home", 3: "home"})
        entries = diff.diff_home_lists(before, canon.list_homes(self.path))
        self.assertEqual(sorted(e["kind"] for e in entries), ["changed", "changed"])


class TestCanonicalForm(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._td.cleanup()

    def test_shuffled_lists_and_disk_reload_give_the_same_form(self):
        h = parse_homed_sqlite(_db(self._td.name), household_name=MAPLE)
        first = canon.canonical(h)
        for acc in h.accessories.values():
            acc.services.reverse()
            acc.senses.reverse()
            for svc in acc.services:
                svc.characteristics.reverse()
        for auto in h.automations.values():
            auto.triggers.reverse()
            auto.actions.reverse()
        for group in h.service_groups.values():
            group.service_ids.reverse()
        self.assertEqual(canon.canonical(h), first)
        self.assertEqual(_reloaded(first, self._td.name), first)

    def test_unresolved_actions_are_counted_not_listed(self):
        h = parse_homed_sqlite(_db(self._td.name), household_name=MAPLE)
        sunset = next(a for a in h.automations.values() if a.name == "Sunset")
        sunset.actions[0].target_accessory_id = "raw-pk-999"
        form = canon.canonical(h)
        self.assertEqual(form["automations"][sunset.id]["actions"], [])
        self.assertEqual(form["automations"][sunset.id]["unresolved"], 1)

    def test_fallback_ids_are_counted(self):
        path = _db(self._td.name, [("UPDATE ZMKFROOM SET ZMODELID=NULL WHERE Z_PK IN (10, 12)", ())])
        h = parse_homed_sqlite(path, household_name=MAPLE)
        self.assertEqual(h.metadata["fallback_ids"], 2)
        self.assertEqual(parse_homed_sqlite(_db(self._td.name, name="c.db"),
                                             household_name=MAPLE).metadata["fallback_ids"], 0)

    def test_reader_version_is_a_12_hex_digest(self):
        self.assertRegex(canon.reader_version(), r"^[0-9a-f]{12}$")


class TestCompareGolden(unittest.TestCase):
    """Real reader on two fixtures, previous description reloaded from disk."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = _reloaded(_canon(_db(self._td.name, name="a.db")), self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _changes(self, *edits):
        return diff.diff(self.base, _canon(_db(self._td.name, list(edits), name="b.db")))

    def _one(self, changes, kind, entity, field):
        self.assertEqual([(c["kind"], c["entity"], c["field"]) for c in changes], [(kind, entity, field)], changes)
        return changes[0]

    def test_unchanged_gives_nothing(self):
        self.assertEqual(self._changes(), [])

    def test_room_rename_does_not_cascade(self):
        c = self._one(self._changes(("UPDATE ZMKFROOM SET ZNAME='Grand Lounge' WHERE Z_PK=10", ())),
                      "changed", "room", "name")
        self.assertEqual((c["before"], c["after"]), ("Lounge", "Grand Lounge"))

    def test_accessory_rename_is_one_entry(self):
        c = self._one(self._changes(("UPDATE ZMKFACCESSORY SET ZPROVIDEDNAME='Capteur Véranda' WHERE Z_PK=103", ())),
                      "changed", "accessory", "name")
        self.assertEqual(c["after"], "Capteur Véranda")

    def test_firmware_bump(self):
        self._one(self._changes(("UPDATE ZMKFACCESSORY SET ZDISPLAYABLEFIRMWAREVERSION='2.3' WHERE Z_PK=100", ())),
                  "changed", "accessory", "firmware_version")

    def test_room_move(self):
        self._one(self._changes(("UPDATE ZMKFACCESSORY SET ZROOM=12 WHERE Z_PK=101", ())), "changed", "accessory", "room_id")

    def test_new_and_deleted_rule(self):
        self._one(self._changes(("INSERT INTO ZMKFTRIGGER (Z_PK, Z_ENT, ZHOME, ZNAME, ZACTIVE, ZMODELID) "
                                 "VALUES (830, 9, 1, 'New rule', 1, ?)", (hashlib.md5(b"t830").digest(),))),
                  "added", "automation", None)
        self._one(self._changes(("DELETE FROM ZMKFTRIGGER WHERE Z_PK=822", ())), "removed", "automation", None)

    def test_changed_condition(self):
        other = {"$c": "NSCompoundPredicate", "NSCompoundPredicateType": 1, "NSSubpredicates": [
            _compare(_offset("sunset", -120), 1, _now()), _compare(_now(), 1, _offset("sunrise", 120))]}
        c = self._one(self._changes(("UPDATE ZMKFTRIGGER SET ZEVALUATIONCONDITION=? WHERE Z_PK=821", (_archive(other),))),
                      "changed", "automation", "conditions")
        self.assertEqual((c["before"], c["after"], len(c["added"]), len(c["removed"])), (1, 1, 1, 1))

    def test_changed_auto_off_is_one_entry(self):
        c = self._one(self._changes(("UPDATE ZMKFEVENT SET ZDURATION=600.0 WHERE Z_PK=922", ())),
                      "changed", "automation", "auto_off_s")
        self.assertEqual((c["before"], c["after"]), (300.0, 600.0))

    def test_relinked_scene(self):
        self._one(self._changes(("INSERT INTO Z_7TRIGGERS_ VALUES (300, 820)", ())), "changed", "automation", "action_set_ids")

    def test_disabled_rule(self):
        self._one(self._changes(("UPDATE ZMKFTRIGGER SET ZACTIVE=0 WHERE Z_PK=820", ())), "changed", "automation", "is_enabled")

    def test_known_unknown_radio_is_one_change_and_a_new_code_adds_one_entry(self):
        self._one(self._changes(("UPDATE ZMKFACCESSORY SET ZSUPPORTEDLINKLAYERTYPES=7 WHERE Z_PK=104", ())),
                  "changed", "accessory", "radio")
        changes = self._changes(("UPDATE ZMKFACCESSORY SET ZSUPPORTEDLINKLAYERTYPES=9 WHERE Z_PK=104", ()))
        self.assertEqual(sorted((c["kind"], c["field"] or c["id"]) for c in changes),
                         [("changed", "radio"), ("new_code", "unknown:9")])

    def test_baseline_and_edit_then_undo(self):
        new = _canon(_db(self._td.name, name="c.db"))
        self.assertEqual([c["kind"] for c in diff.diff(None, new)], ["baseline"])
        edited = _canon(_db(self._td.name, [("UPDATE ZMKFROOM SET ZNAME='X' WHERE Z_PK=10", ())], name="d.db"))
        there, back = diff.diff(self.base, edited), diff.diff(_reloaded(edited, self._td.name), new)
        self.assertEqual([(c["before"], c["after"]) for c in there + back], [("Lounge", "X"), ("X", "Lounge")])

    def test_changes_carry_no_label_of_another_entity(self):
        c = self._one(self._changes(("UPDATE ZMKFEVENT SET ZEVENTVALUE=X'08' WHERE Z_PK=921", ())),
                      "changed", "automation", "triggers")
        self.assertNotIn("Presence Sensor", json.dumps(c))


_spec2 = importlib.util.spec_from_file_location("track_changes", ROOT / "scripts" / "track_changes.py")
track_changes = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(track_changes)


class TestRunLedgerAndRetention(unittest.TestCase):
    """Criteria 5 and 6, on a fake HomeKit folder (the fixture store, no WAL)."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        base = Path(self._td.name)
        self.src, self.data, self.logs = base / "HomeKit", base / "data", base / "logs"
        self.src.mkdir()
        _db(self.src)
        self.clock = [1_700_000_000.0]

    def tearDown(self):
        self._td.cleanup()

    def _edit(self, sql, params=()):
        conn = sqlite3.connect(self.src / DB)
        conn.execute(sql, params)
        conn.commit()
        conn.close()

    def _run(self, **kwargs):
        self.clock[0] += 3600
        from datetime import datetime, timezone
        return track_changes.run(self.src, self.data, self.logs, now=datetime.fromtimestamp(self.clock[0], timezone.utc),
                                 **kwargs)

    def _ledger(self):
        p = self.data / "equipment_ledger.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def _runs(self):
        return [json.loads(line) for line in (self.data / "tracking" / "runs.jsonl").read_text().splitlines()]

    def test_first_run_baselines_then_a_no_change_run_logs_nothing(self):
        first = self._run()
        self.assertEqual(first["status"], "ok")
        self.assertEqual(sorted(e["kind"] for e in self._ledger()), ["baseline"] * 3)
        sha = _sha(self.data / "equipment_ledger.jsonl")
        second = self._run()
        self.assertEqual((second["status"], second["changes"]), ("ok", 0))
        self.assertEqual(_sha(self.data / "equipment_ledger.jsonl"), sha)
        self.assertEqual([r["status"] for r in self._runs()], ["ok", "ok"])
        self.assertTrue((self.data / "tracking" / "last_success").exists())
        self.assertEqual(oct((self.data / "equipment_ledger.jsonl").stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.data.stat().st_mode & 0o777), "0o700")

    def test_retention_also_runs_when_the_snapshot_fails(self):
        from datetime import datetime, timedelta, timezone
        self._run()
        old = datetime.fromtimestamp(self.clock[0], timezone.utc) - timedelta(days=20)
        stale = self.data / "raw" / "snapshots" / old.strftime("%Y-%m-%d_%H%M%SZ")
        stale.mkdir(parents=True)
        os.utime(stale, (old.timestamp(),) * 2)
        self.clock[0] += 3600
        failed = track_changes.run(self.src / "no-such-folder", self.data, self.logs,
                                   now=datetime.fromtimestamp(self.clock[0], timezone.utc))
        self.assertEqual(failed["failure_codes"], ["E_EMPTY"])
        self.assertFalse(stale.exists())

    def test_crash_between_ledger_and_description_logs_each_change_once(self):
        self._run()
        self._edit("UPDATE ZMKFROOM SET ZNAME='Grand Lounge' WHERE Z_PK=10")
        with self.assertRaises(RuntimeError):
            self._run(_crash_after_ledger=True)
        self._run()  # a new snapshot stamp, the same baseline
        renames = [e for e in self._ledger() if e["entity"] == "room"]
        self.assertEqual([(e["before"], e["after"]) for e in renames], [("Lounge", "Grand Lounge")])

    def test_two_runs_in_the_same_second_do_not_collide(self):
        from datetime import datetime, timezone
        fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        first = track_changes.run(self.src, self.data, self.logs, now=fixed)
        second = track_changes.run(self.src, self.data, self.logs, now=fixed)
        self.assertEqual((first["status"], second["status"]), ("ok", "ok"))
        self.assertNotEqual(first["snapshot"], second["snapshot"])

    def test_edit_then_undo_gives_two_entries(self):
        self._run()
        self._edit("UPDATE ZMKFROOM SET ZNAME='X' WHERE Z_PK=10")
        self._run()
        self._edit("UPDATE ZMKFROOM SET ZNAME='Lounge' WHERE Z_PK=10")
        self._run()
        self.assertEqual([(e["before"], e["after"]) for e in self._ledger() if e["entity"] == "room"],
                         [("Lounge", "X"), ("X", "Lounge")])

    def test_a_failing_home_makes_a_partial_run_and_keeps_last_success(self):
        self._run()
        stamp = (self.data / "tracking" / "last_success").read_text()
        self._edit("UPDATE ZMKFHOME SET ZNAME='Barge' WHERE Z_PK=2")
        result = self._run()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(sorted(result["failure_codes"]), ["E_DUP_HOME_NAME", "E_DUP_HOME_NAME"])
        self.assertEqual((self.data / "tracking" / "last_success").read_text(), stamp)
        self.assertNotIn("removed", [e["kind"] for e in self._ledger()])

    def test_a_locked_run_writes_one_run_line_and_nothing_else(self):
        import fcntl
        (self.data / "tracking").mkdir(parents=True)
        with open(self.data / "tracking" / ".lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self._run()
        self.assertEqual(result["status"], "locked")
        self.assertEqual([r["status"] for r in self._runs()], ["locked"])
        self.assertFalse((self.data / "raw").exists())
        self.assertFalse((self.data / "equipment_ledger.jsonl").exists())

    def test_reader_change_rereads_the_previous_snapshot_or_rebaselines(self):
        self._run()
        self._edit("UPDATE ZMKFROOM SET ZNAME='Atelier' WHERE Z_PK=12")
        real = canon.reader_version
        try:
            canon.reader_version = lambda: "ffffffffffff"
            self._run()
            kinds = [e["kind"] for e in self._ledger() if e["kind"] != "baseline"]
            self.assertEqual(kinds, ["changed"])  # the real edit, no rebaseline
            for snap in (self.data / "raw" / "snapshots").iterdir():
                import shutil
                shutil.rmtree(snap)
            canon.reader_version = lambda: "eeeeeeeeeeee"
            self._run()
        finally:
            canon.reader_version = real
        self.assertEqual([e["kind"] for e in self._ledger()].count("rebaseline"), 3)

    def test_retention_keeps_exactly_the_expected_items(self):
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        snaps = self.data / "raw" / "snapshots"
        stamps = {"old": now - timedelta(days=20), "new": now - timedelta(days=2),
                  "changed": now - timedelta(days=60), "ancient": now - timedelta(days=100)}
        names = {k: v.strftime("%Y-%m-%d_%H%M%SZ") for k, v in stamps.items()}
        for name in names.values():
            (snaps / name).mkdir(parents=True)
        (snaps / "keep-me").mkdir()
        (snaps / (names["old"] + ".partial")).mkdir()
        tracking = self.data / "tracking"
        tracking.mkdir(parents=True)
        (tracking / "runs.jsonl").write_text("\n".join(json.dumps({"snapshot": names[k], "changes": 1})
                                                       for k in ("changed", "ancient")) + "\n")
        self.logs.mkdir()
        (self.logs / f"track_changes.{(now - timedelta(days=40)):%Y-%m-%d}.log").write_text("x")
        (self.logs / f"track_changes.{(now - timedelta(days=3)):%Y-%m-%d}.log").write_text("x")
        home = tracking / "home-1"
        home.mkdir()
        for i in range(35):
            (home / f"{(now - timedelta(days=i)):%Y-%m-%d_%H%M%SZ}.json").write_text("{}")
        (home / "x.tmp").write_text("")
        os.utime(home / "x.tmp", (now.timestamp() - 3 * 86400,) * 2)
        track_changes.retention(self.data, self.logs, now)
        self.assertEqual(sorted(p.name for p in snaps.iterdir()), sorted([names["new"], names["changed"], "keep-me"]))
        self.assertEqual(len(list(self.logs.iterdir())), 1)
        self.assertEqual(len([p for p in home.iterdir() if p.suffix == ".json"]), 30)
        self.assertFalse((home / "x.tmp").exists())


class TestNotifications(unittest.TestCase):
    """Criterion 7: one message at most per run, codes and counts only, heartbeat and repeat rules."""

    NAMES = ("Maple House", "Lake Cabin", "Barge", "Lounge", "Lamp Lounge", "Presence Sensor", "Sunset", "ZX9")

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        base = Path(self._td.name)
        self.src, self.data, self.logs = base / "HomeKit", base / "data", base / "logs"
        self.src.mkdir()
        _db(self.src)
        self.sent = []
        self.day = 0

    def tearDown(self):
        self._td.cleanup()

    def _run(self, days=1, **kwargs):
        from datetime import datetime, timezone, timedelta
        self.day += days
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc) + timedelta(days=self.day)
        before = len(self.sent)
        summary = track_changes.run(self.src, self.data, self.logs, now=now,
                                    notifier=lambda text: self.sent.append(text) or True, **kwargs)
        return summary, self.sent[before:]

    def _edit(self, sql):
        conn = sqlite3.connect(self.src / DB)
        conn.execute(sql)
        conn.commit()
        conn.close()

    def _assert_no_names(self, texts):
        for text in texts:
            self.assertTrue(text.startswith("HOME_AUTO — "), text)
            for name in self.NAMES:
                self.assertNotIn(name, text)

    def test_first_run_baseline_with_heartbeat_then_silence_then_a_change(self):
        _, msgs = self._run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("baseline recorded for 3 home(s)", msgs[0])
        self.assertIn("tracking alive", msgs[0])
        _, msgs = self._run()
        self.assertEqual(msgs, [])
        self._edit("UPDATE ZMKFROOM SET ZNAME='Grand Lounge' WHERE Z_PK=10")
        _, msgs = self._run()
        self.assertEqual(len(msgs), 1)
        self.assertIn("1 change(s) in 1 home(s)", msgs[0])
        self.assertIn("see data/equipment_ledger.jsonl", msgs[0])
        self._assert_no_names(self.sent)

    def test_stale_heartbeat_is_sent_alone_after_seven_days(self):
        self._run()
        _, msgs = self._run(days=3)
        self.assertEqual(msgs, [])
        _, msgs = self._run(days=5)  # 8 days after the first heartbeat
        self.assertEqual(len(msgs), 1)
        self.assertRegex(msgs[0], r"tracking alive: \d+ runs in 7 days, 0 changes, last success 2026-01-\d\d")

    def test_failure_message_and_weekly_repeat(self):
        self._run()
        self._edit("UPDATE ZMKFHOME SET ZNAME='Barge' WHERE Z_PK=2")
        days = {}
        for day in (1, 2, 3, 7, 8):
            _, msgs = self._run(days=day - (self.day - 1))
            days[day] = [m for m in msgs if "tracking failed" in m]
        self.assertEqual([d for d, m in days.items() if m], [1, 8])
        self.assertIn("E_DUP_HOME_NAME (home #2, home #3)", days[1][0])
        self._assert_no_names(self.sent)

    def test_reader_error_text_never_reaches_a_message_or_the_log_line(self):
        import src.extractor.sqlite_parser as parser
        real = parser.parse_homed_sqlite

        def boom(*args, **kwargs):
            raise ValueError("No home named 'X'; homes in the store: ['ZX9']")

        parser.parse_homed_sqlite = boom
        try:
            summary, msgs = self._run()
        finally:
            parser.parse_homed_sqlite = real
        text = " ".join(msgs) + " " + track_changes.log_line(summary)
        self.assertIn("E_READER", text)
        self.assertNotIn("ZX9", text)
        self.assertNotIn("'X'", text)

    def test_a_failing_import_in_main_prints_only_a_code(self):
        import contextlib
        import io
        real_run, real_argv = track_changes.run, sys.argv

        def failing(**kwargs):
            raise ImportError("No module named 'secret_ZX9_module'")

        track_changes.run, sys.argv = failing, ["track_changes.py"]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = track_changes.main()
        finally:
            track_changes.run, sys.argv = real_run, real_argv
        self.assertEqual(code, 1)
        self.assertIn("E_OTHER ImportError", out.getvalue())
        self.assertNotIn("secret_ZX9_module", out.getvalue())

    def test_a_locked_run_sends_nothing(self):
        import fcntl
        self._run()
        with open(self.data / "tracking" / ".lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            summary, msgs = self._run(days=10)
        self.assertEqual((summary["status"], msgs), ("locked", []))

    def test_gitignore_lists_logs(self):
        self.assertIn("logs/", (ROOT / ".gitignore").read_text().splitlines())

    def test_gitignore_lists_the_local_notifier(self):
        self.assertIn("/scripts/local_notifier.py", (ROOT / ".gitignore").read_text().splitlines())
        self.assertTrue((ROOT / "scripts" / "local_notifier.example.py").is_file())

    def test_main_prints_the_fda_path_only_when_interactive(self):
        import contextlib
        import io
        from unittest import mock
        summary = {"ts": "t", "status": "failed", "snapshot": None, "homes": 0, "changes": 0, "baselines": 0,
                   "failure_codes": ["E_FDA"], "failed_homes": [], "fallback_ids": 0, "notify": None}
        for tty in (True, False):
            err = io.StringIO()
            with mock.patch.object(track_changes, "run", return_value=summary), \
                    mock.patch.object(track_changes, "_default_notifier", return_value=lambda t: True), \
                    mock.patch.object(sys, "argv", ["track_changes.py"]), \
                    mock.patch.object(sys.stdin, "isatty", return_value=tty), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(track_changes.main(), 1)
            self.assertEqual(os.path.realpath(sys.executable) in err.getvalue(), tty)


class TestDefaultNotifier(unittest.TestCase):
    """Without a local adapter a run prints its message; with one, the adapter's notifier is used."""

    def test_without_an_adapter_the_message_is_printed(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as td:
            notify = track_changes._default_notifier(Path(td))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertTrue(notify("HOME_AUTO — test"))
        self.assertEqual(out.getvalue(), "NOTIFY HOME_AUTO — test\n")

    def test_a_local_adapter_is_used_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            sent = Path(td) / "sent.txt"
            (Path(td) / "local_notifier.py").write_text(
                f"from pathlib import Path\n\ndef notifier():\n"
                f"    return lambda text: Path({str(sent)!r}).write_text(text) > 0\n")
            notify = track_changes._default_notifier(Path(td))
            self.assertTrue(notify("HOME_AUTO — 1 change(s)"))
            self.assertEqual(sent.read_text(), "HOME_AUTO — 1 change(s)")


class TestPlistTemplate(unittest.TestCase):
    """The shipped job template holds placeholders only: no user path, and it parses once they are filled."""

    def test_placeholders_and_parse(self):
        import plistlib
        text = (ROOT / "launchd" / "track-changes.plist.template").read_text()
        self.assertNotIn(str(Path.home()), text)
        self.assertEqual(sorted(set(re.findall(r"{{([A-Z_]+)}}", text))), ["PROJECT_DIR", "PYTHON"])
        filled = text.replace("{{PROJECT_DIR}}", "/opt/tracker").replace("{{PYTHON}}", "/opt/py/bin/python3.12")
        plist = plistlib.loads(filled.encode())
        self.assertEqual(plist["Label"], "local.homekit-change-tracker")
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 12, "Minute": 0})
        self.assertFalse([k for k in plist if k.startswith("Standard")])
        self.assertEqual(plist["ProgramArguments"], ["/opt/py/bin/python3.12", "-B", "/opt/tracker/scripts/track_changes.py",
                                                     "--log-dir", "/opt/tracker/logs"])


if __name__ == "__main__":
    unittest.main()
