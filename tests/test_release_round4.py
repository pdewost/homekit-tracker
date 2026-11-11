"""Release hardening, round 4: one I/O helper for every destination (a lint keeps it that way; it is a lint, not a
proof: it follows import aliases and a list of known calls, nothing more), the live root and
symlinks refused at each kind of destination, Controller exports of several homes, and unknown event data."""

import ast
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import release_manifest
from src import safe_io
from src.audit.bundle_generator import generate_llm_audit_bundle
from src.extractor.app_parser import parse_app_export
from src.extractor.sqlite_parser import parse_homed_sqlite
from src.models.schema import Automation, Household
from src.store_files import DB, LiveStoreError
from src.tracking import canon, diff
from tests.fixtures.build_fake_home import build
from tests.test_sqlite_parser import _build_zmkf

ROOT = Path(__file__).resolve().parent.parent
HELPER = "src/safe_io.py"
# (file, exact stripped line) pairs allowed to hold a raw call, each with its reason. An entry that no longer matches
# fails the test.
ALLOWLIST = (
    # reads git's list of tracked files; writes nothing
    ("scripts/release_manifest.py",
     'out = subprocess.run(["git", "ls-files", "-z"], cwd=folder, capture_output=True, check=True).stdout'),
    # test fixture writing into a fresh temporary folder (the smoke test imports these)
    ("tests/fixtures/build_fake_home.py", "folder.mkdir(parents=True, exist_ok=True)"),
    ("tests/test_sqlite_parser.py", "conn = sqlite3.connect(db_file)"),
)
MODULES = {"os", "shutil", "sqlite3", "pathlib", "subprocess"}
RAW_ATTRS = {"write_text", "write_bytes", "chmod", "unlink", "mkdir", "makedirs", "fchmod", "remove", "rmdir",
             "rename", "rmtree", "mkdtemp", "truncate", "move", "touch", "symlink_to", "hardlink_to"}
RAW_NAMES = {"os.open", "os.replace", "os.rename", "os.system", "os.popen", "os.link", "os.symlink", "sqlite3.connect"}
RAW_PREFIXES = ("shutil.copy", "subprocess.", "os.exec", "os.spawn")


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _aliases(tree):
    """Names bound by `import os as x` or `from os import open as y` (and the same for the other watched modules)."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update({a.asname: a.name for a in node.names if a.asname and a.name.split(".")[0] in MODULES})
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in MODULES:
            out.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
    return out


def _resolve(name, aliases):
    first = name.split(".")[0]
    return aliases[first] + name[len(first):] if first in aliases else name


def _raw_calls(source):
    """(line number, name) of each raw destination call in `source`; comments and strings are never calls. This is a
    lint: it follows import aliases and a list of known names, and cannot see every way to reach the same calls."""
    tree = ast.parse(source)
    aliases, found = _aliases(tree), []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _resolve(_dotted(node.func), aliases)
        if name.startswith("safe_io."):
            continue
        tail = name.rsplit(".", 1)[-1]
        if name == "getattr":
            if node.args and _resolve(_dotted(node.args[0]), aliases) in MODULES:
                found.append((node.lineno, name))
        elif name in RAW_NAMES or name.startswith(RAW_PREFIXES) or tail in RAW_ATTRS \
                or (tail == "replace" and len(node.args) == 1 and not node.keywords):  # str.replace takes two
            found.append((node.lineno, name))
        elif tail == "open":
            mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
            if mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                                         and not set(mode.value) & set("wax+")):
                found.append((node.lineno, name))
    return found


def _fixture_imports(rel, seen=None):
    """The files under tests/ that a released file imports (`from tests.x import y`), followed transitively."""
    seen = set() if seen is None else seen
    if not (ROOT / rel).is_file():
        return seen
    for node in ast.walk(ast.parse((ROOT / rel).read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tests."):
            path = node.module.replace(".", "/") + ".py"
            if path not in seen and (ROOT / path).is_file():
                seen.add(path)
                _fixture_imports(path, seen)
    return seen


def _scan(files, allowlist):
    problems, used = [], set()
    fixtures = _fixture_imports("scripts/smoke_test.py")
    for rel in sorted(set(files) | fixtures):
        if rel == HELPER or not rel.endswith(".py") or rel.startswith("src/energy") \
                or (rel.startswith("tests/") and rel not in fixtures):
            continue
        text = (ROOT / rel).read_text(encoding="utf-8")
        lines = text.splitlines()
        for lineno, name in _raw_calls(text):
            entry = (rel, lines[lineno - 1].strip())
            if entry in allowlist:
                used.add(entry)
            else:
                problems.append(f"{rel}:{lineno} {name}")
    stale = [e for e in allowlist if e not in used]
    return problems, stale


class TestNoRawDestinationCalls(unittest.TestCase):
    def test_released_code_uses_only_the_helper(self):
        problems, stale = _scan(release_manifest.MAP, ALLOWLIST)
        self.assertEqual(problems, [])
        self.assertEqual(stale, [])

    def test_scanner_sees_each_raw_call_and_ignores_comments_and_reads(self):
        bad = ("open(p, 'w')\nopen(p, mode='ab')\nos.open(p, 0)\nx.write_text('a')\nx.write_bytes(b'')\nos.chmod(p, 0)\n"
               "x.chmod(0)\nx.unlink()\nos.remove(p)\nos.replace(a, b)\nos.rename(a, b)\nshutil.rmtree(p)\n"
               "shutil.copy2(a, b)\nshutil.copyfile(a, b)\nx.mkdir()\nsqlite3.connect(p)\n")
        self.assertEqual(len(_raw_calls(bad)), 16)
        fine = "# open(p, 'w')\n'''x.mkdir()'''\nopen(p)\nopen(p, 'rb')\ns.replace('a', 'b')\n"
        self.assertEqual(_raw_calls(fine), [])

    def test_scanner_sees_aliases_and_the_wider_call_list(self):
        bad = ("import os as x\nx.open(p, 0)\n"
               "from os import open as y\ny(p, 0)\n"
               "from sqlite3 import connect as z\nz(p)\n"
               "import shutil as s\ns.rmtree(p)\n"
               "from shutil import copyfile\ncopyfile(a, b)\n"
               "getattr(os, 'unlink')(p)\ngetattr(s, 'rmtree')(p)\n"
               "subprocess.run(['x'])\nos.system('x')\nos.popen('x')\nos.execv(a, b)\nos.spawnl(0, a)\n"
               "q.replace(t)\nq.rename(t)\nq.touch()\nq.symlink_to(t)\nq.hardlink_to(t)\nq.rmdir()\n")
        found = [name for _, name in _raw_calls(bad)]
        self.assertEqual(len(found), 18, found)
        for line in bad.splitlines():
            if line.startswith(("import", "from")):
                continue
            with self.subTest(line=line):
                self.assertTrue(_raw_calls("import os as x\nimport shutil as s\nfrom os import open as y\n"
                                           "from sqlite3 import connect as z\nfrom shutil import copyfile\n" + line + "\n"))
        self.assertEqual(_raw_calls("x = 'a'.replace('a', 'b')\nd.replace(tzinfo=None)\n"), [])

    def test_the_fixtures_the_smoke_test_imports_are_scanned(self):
        self.assertEqual(_fixture_imports("scripts/smoke_test.py"),
                         {"tests/fixtures/build_fake_home.py", "tests/test_sqlite_parser.py"})

    def test_the_launchd_template_holds_no_shell_redirect_or_mkdir(self):
        text = (ROOT / "launchd" / "track-changes.plist.template").read_text(encoding="utf-8").lower()
        for banned in ("bash", "mkdir", ">>", "&gt;&gt;", "/bin/sh", "/bin/zsh"):
            self.assertNotIn(banned, text)

    def test_allowlist_entries_must_match(self):
        with tempfile.TemporaryDirectory() as td, mock.patch(f"{__name__}.ROOT", Path(td)):
            (Path(td) / "a.py").write_text("x.mkdir()\n")
            self.assertEqual(_scan(["a.py"], {("a.py", "x.mkdir()")}), ([], []))
            self.assertEqual(_scan(["a.py"], {("a.py", "y.mkdir()")})[1], [("a.py", "y.mkdir()")])


class _Live(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self.live = self.base / "HomeKit"
        self.live.mkdir()
        self.dummy = self.live / "dummy.bin"
        self.dummy.write_bytes(os.urandom(4096))
        self.before = self.dummy.read_bytes()
        self.patch = mock.patch("src.store_files.LIVE_ROOT", self.live)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self._td.cleanup()

    def tree(self):
        return sorted(str(p.relative_to(self.live)) for p in self.live.rglob("*"))

    def intact(self):
        self.assertEqual(self.dummy.read_bytes(), self.before)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestHelper(_Live):
    def test_lock_open_keeps_an_existing_file(self):
        lock = self.base / ".lock"
        lock.write_text("keep", encoding="utf-8")
        safe_io.open_lock(lock).close()
        self.assertEqual(lock.read_text(encoding="utf-8"), "keep")

    def test_each_destination_kind_refuses_the_live_root_and_symlinks(self):
        inside, outside = self.live / "x", self.base / "x"
        link, elsewhere = self.base / "link", self.base / "elsewhere.bin"
        elsewhere.write_bytes(b"x")
        link.symlink_to(elsewhere)
        for label, call in (("open", lambda p: safe_io.open_text(p, "w")), ("append", lambda p: safe_io.open_text(p, "a")),
                            ("lock", safe_io.open_lock), ("mkdir", safe_io.mkdir),
                            ("chmod", lambda p: safe_io.chmod(p, 0o600)), ("unlink", safe_io.unlink),
                            ("rmtree", safe_io.rmtree), ("replace", lambda p: safe_io.replace(self.dummy, p)),
                            ("copy", lambda p: safe_io.copy_file(self.dummy, p)), ("sqlite", safe_io.sqlite_ro),
                            ("scratch", safe_io.sqlite_scratch), ("temp", lambda p: safe_io.mkdtemp("t-", p))):
            with self.subTest(label=label), self.assertRaises(LiveStoreError):
                call(inside)
        for label, call in (("open", lambda p: safe_io.open_text(p, "w")), ("lock", safe_io.open_lock),
                            ("chmod", lambda p: safe_io.chmod(p, 0o600)), ("copy", lambda p: safe_io.copy_file(outside, p))):
            with self.subTest(label=label, link=True), self.assertRaises(safe_io.SymlinkError):
                call(link)
        self.intact()
        self.assertEqual(self.tree(), ["dummy.bin"])

    def test_the_live_store_can_be_copied_from(self):
        dest = self.base / "copy.bin"
        safe_io.copy_file(self.dummy, dest)
        self.assertEqual(dest.read_bytes(), self.before)
        self.assertEqual(oct(dest.stat().st_mode & 0o777), "0o600")

    def test_unlink_removes_a_link_not_its_target(self):
        link = self.base / "link"
        link.symlink_to(self.dummy)
        safe_io.unlink(link)
        self.assertFalse(link.is_symlink())
        self.intact()


class TestTrackChangesDestinations(_Live):
    def _run(self, track, source, data):
        snap = track._snapshot_module()
        with mock.patch.object(snap, "SOURCE", self.live):
            return track.run(source=source, data=data, logs=self.base / "logs", notifier=lambda t: True)

    def test_lock_file_symlinked_into_the_live_root_is_refused_and_untouched(self):
        build(self.live)
        before = self.tree()
        track = _load("track_changes")
        (self.base / "data" / "tracking").mkdir(parents=True)
        (self.base / "data" / "tracking" / ".lock").symlink_to(self.dummy)
        with self.assertRaises(LiveStoreError):
            self._run(track, self.live, self.base / "data")
        self.intact()
        self.assertEqual(self.tree(), before)

    def test_lock_file_symlinked_elsewhere_is_refused_too(self):
        track = _load("track_changes")
        other = self.base / "other.bin"
        other.write_bytes(b"x" * 100)
        (self.base / "data" / "tracking").mkdir(parents=True)
        (self.base / "data" / "tracking" / ".lock").symlink_to(other)
        build(self.live)
        with self.assertRaises(LiveStoreError):
            self._run(track, self.live, self.base / "data")
        self.assertEqual(other.read_bytes(), b"x" * 100)

    def test_a_normal_run_copies_from_the_live_root_and_works_on_copies(self):
        build(self.live)
        before = {p.name: p.read_bytes() for p in self.live.iterdir() if p.is_file()}
        track = _load("track_changes")
        summary = self._run(track, self.live, self.base / "data")
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["baselines"] > 0, True)
        self.assertEqual({p.name: p.read_bytes() for p in self.live.iterdir() if p.is_file()}, before)
        self.assertTrue(list((self.base / "data" / "raw" / "snapshots").glob("*/snapshot.json")))


class TestBundleDestinations(_Live):
    def _household(self):
        return Household(id="h", name="Maple House")

    def test_output_folder_resolving_into_the_live_root_is_refused(self):
        alias = self.base / "alias"
        alias.symlink_to(self.live)
        for out in (self.live / "bundle", alias / "bundle", alias):
            with self.subTest(out=str(out.name)), self.assertRaises(LiveStoreError):
                generate_llm_audit_bundle(self._household(), out)
        self.intact()
        self.assertEqual(self.tree(), ["dummy.bin"])

    def test_a_link_inside_the_output_folder_is_refused(self):
        out = self.base / "out"
        out.mkdir()
        (out / "household_normalized.json").symlink_to(self.dummy)
        with self.assertRaises(LiveStoreError):
            generate_llm_audit_bundle(self._household(), out)
        self.intact()
        self.assertEqual(self.tree(), ["dummy.bin"])

    def test_a_normal_bundle_is_written(self):
        out = generate_llm_audit_bundle(self._household(), self.base / "ok")
        self.assertTrue((out / "household_normalized.json").is_file())


class TestCanonRefusesLiveRoot(_Live):
    def test_list_homes(self):
        db = self.live / DB
        _build_zmkf(db)
        with self.assertRaises(LiveStoreError):
            canon.list_homes(db)
        copy = self.base / DB
        copy.write_bytes(db.read_bytes())
        self.assertTrue(canon.list_homes(copy))


class TestControllerExports(unittest.TestCase):
    def _parse(self, data, name=None):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "export.json"
            path.write_text(json.dumps(dict(data, version=1)), encoding="utf-8")
            return parse_app_export(path, household_name=name)

    def test_requested_home_absent_from_a_homeless_export_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._parse({"accessories": [], "rooms": []}, "Lake Cabin")
        self.assertIn("Lake Cabin", str(ctx.exception))

    def test_homeless_export_without_a_request_keeps_the_default_and_a_matching_name_passes(self):
        self.assertEqual(self._parse({"rooms": []}).name, "Home")
        self.assertEqual(self._parse({"name": "Lake Cabin", "rooms": []}, "lake cabin").name, "Lake Cabin")

    def test_several_homes_are_not_mixed(self):
        homes = [{"name": "Maple House", "uniqueIdentifier": "1"}, {"name": "Lake Cabin", "uniqueIdentifier": "2"}]
        for name in (None, "Maple House"):
            with self.subTest(name=name), self.assertRaises(ValueError) as ctx:
                self._parse({"homes": homes, "rooms": [{"name": "Hall", "uniqueIdentifier": "r"}]}, name)
            self.assertTrue("several homes" in str(ctx.exception) or "--home" in str(ctx.exception))

    def test_one_home_is_read(self):
        h = self._parse({"homes": [{"name": "Maple House", "uniqueIdentifier": "1"}],
                         "rooms": [{"name": "Hall", "uniqueIdentifier": "r"}]}, "Maple House")
        self.assertEqual(len(h.rooms), 1)


class TestUnknownEvents(unittest.TestCase):
    def _store(self, td, skip):
        db = Path(td) / f"{len(list(Path(td).iterdir()))}{Path(DB).suffix}"
        _build_zmkf(db, skip_table=skip, profile=True)
        return db

    def test_missing_event_table_is_unavailable_and_not_compared(self):
        with tempfile.TemporaryDirectory() as td:
            full = parse_homed_sqlite(self._store(td, None), household_name="Maple House")
            gap = parse_homed_sqlite(self._store(td, "ZMKFEVENT"), household_name="Maple House")
            self.assertNotIn("events", full.unavailable)
            self.assertIn("events", gap.unavailable)
            old, new = canon.canonical(full), canon.canonical(gap)
            self.assertNotIn("events", old.get("unavailable", []))
            self.assertTrue(any(a["triggers"] for a in old["automations"].values()))
            fields = {c["field"] for c in diff.diff(old, new)}
            self.assertFalse(fields & {"triggers", "auto_off_s", "hidden_in_app"})
            self.assertEqual(diff.diff(new, old), [])
            changed = json.loads(json.dumps(new))
            for a in changed["automations"].values():
                a["is_enabled"] = not a["is_enabled"]
            self.assertTrue(any(c["field"] == "is_enabled" for c in diff.diff(new, changed)))

    def test_to_dict_and_bundle_carry_it(self):
        h = Household(id="h", name="Maple House", unavailable=["zones", "events"])
        self.assertEqual(h.to_dict()["unavailable"], ["events", "zones"])
        self.assertEqual(Household(id="h", name="x").to_dict()["unavailable"], [])
        with tempfile.TemporaryDirectory() as td:
            out = generate_llm_audit_bundle(h, Path(td) / "b")
            self.assertEqual(json.loads((out / "household_normalized.json").read_text())["unavailable"],
                             ["events", "zones"])
            self.assertIn("Information not available in this store (reported as unknown): events, zones",
                          (out / "00_README.md").read_text())
            out2 = generate_llm_audit_bundle(Household(id="h", name="x"), Path(td) / "c")
            self.assertNotIn("not available in this store", (out2 / "00_README.md").read_text())
