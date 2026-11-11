"""Release hardening, round 5 (same-user scope): the tracker writes its own log, and the live-folder guard holds for
letter-case variants, hard links, `..` after a symlink, and folders that hold the live folder. Fictional folders only."""

import contextlib
import io
import os
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from src import safe_io
from src.store_files import DB, LiveStoreError
from tests.test_release_round4 import ROOT, _Live, _load

LINE = "2000-01-01T00:00:00+00:00 ok fictional line"


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class TestLogDir(_Live):
    def setUp(self):
        super().setUp()
        self.track = _load("track_changes")
        self.snap = self.track._snapshot_module()
        self.logs = self.base / "logs"
        self.dated = self.logs / f"track_changes.{datetime.now():%Y-%m-%d}.log"
        for p in (mock.patch.object(self.snap, "SOURCE", self.live),
                  mock.patch.object(self.track, "_default_notifier", return_value=lambda t: True),
                  mock.patch.object(self.track, "log_line", return_value=LINE)):
            p.start()
            self.addCleanup(p.stop)
        self.run_mock = mock.patch.object(self.track, "run", return_value={"status": "ok", "failure_codes": []})
        self.run_mock = self.run_mock.start()
        self.addCleanup(mock.patch.stopall)

    def _main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            return self.track.main(list(args)), out.getvalue(), err.getvalue()

    def test_no_argument_prints_the_line_as_before(self):
        rc, out, err = self._main()
        self.assertEqual((rc, out, err), (0, LINE + "\n", ""))
        self.run_mock.assert_called_once()
        self.assertFalse(self.logs.exists())

    def test_log_dir_writes_the_line_into_the_dated_file(self):
        self.run_mock.side_effect = lambda **kw: print("to stderr", file=__import__("sys").stderr) or \
            {"status": "ok", "failure_codes": []}
        rc, out, err = self._main("--log-dir", str(self.logs))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.dated.read_text(), "to stderr\n" + LINE + "\n")
        self.assertEqual((_mode(self.logs), _mode(self.dated)), (0o700, 0o600))
        self._main("--log-dir", str(self.logs))  # a second run appends
        self.assertEqual(self.dated.read_text().count(LINE), 2)

    def test_dated_log_that_is_a_symlink_is_refused_and_the_target_untouched(self):
        target = self.base / "target.txt"
        target.write_text("keep")
        self.logs.mkdir()
        self.dated.symlink_to(target)
        rc, out, err = self._main("--log-dir", str(self.logs))
        self.assertEqual(rc, 1)
        self.assertEqual(target.read_text(), "keep")
        self.run_mock.assert_not_called()

    def test_dated_log_that_is_a_hard_link_is_refused_and_the_target_untouched(self):
        target = self.base / "target.txt"
        target.write_text("keep")
        os.chmod(target, 0o644)
        self.logs.mkdir()
        os.link(target, self.dated)
        rc, out, err = self._main("--log-dir", str(self.logs))
        self.assertEqual(rc, 1)
        self.assertEqual((target.read_text(), _mode(target)), ("keep", 0o644))
        self.run_mock.assert_not_called()

    def test_log_dir_inside_the_live_root_is_refused_before_anything_is_created(self):
        before = self.tree()
        rc, out, err = self._main("--log-dir", str(self.live / "logs"))
        self.assertEqual(rc, 1)
        self.assertEqual(self.tree(), before)
        self.intact()
        self.run_mock.assert_not_called()

    def test_template_runs_python_directly(self):
        text = (ROOT / "launchd" / "track-changes.plist.template").read_text(encoding="utf-8").lower()
        for banned in ("bash", "mkdir", ">>", "&gt;"):
            self.assertNotIn(banned, text)


class TestCaseVariants(_Live):
    def setUp(self):
        super().setUp()
        self.variant = self.base / "homekit"
        if not self.variant.exists():
            self.skipTest("the temporary volume is case-sensitive: a case variant is a different folder here")

    def test_each_kind_of_destination_refuses_the_lower_case_spelling(self):
        (self.live / DB).write_bytes(b"x")
        src = self.base / "s.bin"
        src.write_bytes(b"s")
        for label, call in (("check", lambda: safe_io.check(self.variant / "dummy.bin")),
                            ("write", lambda: safe_io.write_text(self.variant / "new.bin", "x")),
                            ("append", lambda: safe_io.open_text(self.variant / "new.bin", "a")),
                            ("unlink", lambda: safe_io.unlink(self.variant / "dummy.bin")),
                            ("rmtree", lambda: safe_io.rmtree(self.variant)),
                            ("rmtree-file", lambda: safe_io.rmtree(self.variant / "dummy.bin")),
                            ("replace", lambda: safe_io.replace(src, self.variant / "dummy.bin")),
                            ("sqlite", lambda: safe_io.sqlite_ro(self.variant / DB))):
            with self.subTest(label=label), self.assertRaises(LiveStoreError):
                call()
        self.intact()
        self.assertEqual(self.tree(), sorted(["dummy.bin", DB]))


class TestHardLinksAndModes(_Live):
    def test_a_file_owned_by_someone_else_is_refused_and_kept(self):
        target = self.base / "theirs.txt"
        target.write_text("theirs", encoding="utf-8")
        with mock.patch("src.safe_io.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(LiveStoreError):
                safe_io.write_text(target, "x")
        self.assertEqual(target.read_text(encoding="utf-8"), "theirs")

    def test_a_database_symlink_into_the_live_folder_is_refused(self):
        link = self.base / ("alias" + Path(DB).suffix)
        link.symlink_to(self.dummy)
        with self.assertRaises(LiveStoreError):
            safe_io.sqlite_ro(link)
        self.intact()

    def test_a_hard_link_to_a_live_file_is_never_written_through(self):
        link, src = self.base / "alias.bin", self.base / "src.bin"
        os.link(self.dummy, link)
        src.write_bytes(b"new bytes")
        for label, call in (("write", lambda: safe_io.open_text(link, "w")),
                            ("append", lambda: safe_io.open_text(link, "a")),
                            ("write_text", lambda: safe_io.write_text(link, "x")),
                            ("copy", lambda: safe_io.copy_file(src, link))):
            with self.subTest(label=label), self.assertRaises(LiveStoreError):
                call()
        self.intact()
        self.assertEqual(link.read_bytes(), self.before)

    def test_an_existing_0644_file_is_0600_before_the_first_byte_and_not_truncated_early(self):
        path = self.base / "f.txt"
        path.write_text("keep")
        os.chmod(path, 0o644)
        with mock.patch("src.safe_io.os.ftruncate", side_effect=OSError("stop")), self.assertRaises(OSError):
            safe_io.write_text(path, "new")
        self.assertEqual((_mode(path), path.read_text()), (0o600, "keep"))
        safe_io.write_text(path, "new")
        self.assertEqual((_mode(path), path.read_text()), (0o600, "new"))

    def test_a_refused_hard_link_keeps_its_mode_and_bytes(self):
        path, link = self.base / "f.txt", self.base / "g.txt"
        path.write_text("keep")
        os.chmod(path, 0o644)
        os.link(path, link)
        with self.assertRaises(LiveStoreError):
            safe_io.write_text(link, "new")
        self.assertEqual((_mode(path), path.read_text()), (0o644, "keep"))

    def test_copy_sets_the_mode_of_an_existing_destination(self):
        dst = self.base / "d.bin"
        dst.write_bytes(b"old")
        os.chmod(dst, 0o644)
        safe_io.copy_file(self.dummy, dst)
        self.assertEqual((_mode(dst), dst.read_bytes()), (0o600, self.before))

    def test_a_new_file_honours_the_umask_of_a_wider_request(self):
        old = os.umask(0o022)
        try:
            safe_io.write_text(self.base / "w.txt", "x", perm=0o666)
        finally:
            os.umask(old)
        self.assertEqual(_mode(self.base / "w.txt"), 0o644)


class TestDotDotAfterSymlink(_Live):
    def setUp(self):
        super().setUp()
        (self.live / "child").mkdir()
        self.victim = self.live / "victim.bin"
        self.victim.write_bytes(b"victim")
        (self.base / "outside").mkdir()
        (self.base / "outside" / "alias").symlink_to(self.live / "child")
        # the system resolves alias first, so this is the live victim; a lexical clean-up would see outside/victim.bin
        self.sneaky = os.path.join(self.base, "outside", "alias", "..", "victim.bin")
        self.assertTrue(os.path.exists(self.sneaky))
        (self.base / "outside" / "victim.bin").write_bytes(b"decoy")

    def test_unlink_rmtree_and_replace_are_refused(self):
        other = self.base / "other.bin"
        other.write_bytes(b"o")
        for label, call in (("unlink", lambda: safe_io.unlink(self.sneaky)),
                            ("rmtree", lambda: safe_io.rmtree(self.sneaky)),
                            ("replace-src", lambda: safe_io.replace(self.sneaky, self.base / "moved.bin")),
                            ("replace-dst", lambda: safe_io.replace(other, self.sneaky)),
                            ("dotdot-last", lambda: safe_io.unlink(os.path.join(self.base, "outside", "alias", ".."))),
                            ("dot-last", lambda: safe_io.rmtree(os.path.join(self.base, "outside", ".")))):
            with self.subTest(label=label), self.assertRaises(LiveStoreError):
                call()
        self.assertEqual(self.victim.read_bytes(), b"victim")
        self.assertEqual(other.read_bytes(), b"o")
        self.assertFalse((self.base / "moved.bin").exists())
        self.intact()


class TestAncestorsOfTheLiveFolder(_Live):
    def setUp(self):
        super().setUp()
        self.library = self.base / "Library"
        self.live2 = self.library / "HomeKit"
        self.live2.mkdir(parents=True)
        (self.live2 / "keep.bin").write_bytes(b"keep")
        patch = mock.patch("src.store_files.LIVE_ROOT", self.live2)
        patch.start()
        self.addCleanup(patch.stop)

    def test_removing_or_moving_a_folder_that_holds_the_live_folder_is_refused(self):
        other = self.base / "other"
        other.mkdir()
        for label, call in (("rmtree", lambda: safe_io.rmtree(self.library)),
                            ("rmtree-link", lambda: safe_io.rmtree(self.base / "Library" / ".." / "Library")),
                            ("unlink", lambda: safe_io.unlink(self.library)),
                            ("replace-src", lambda: safe_io.replace(self.library, self.base / "moved")),
                            ("replace-dst", lambda: safe_io.replace(other, self.library))):
            with self.subTest(label=label), self.assertRaises(LiveStoreError):
                call()
        self.assertEqual((self.live2 / "keep.bin").read_bytes(), b"keep")
        self.assertTrue(other.is_dir())

    def test_an_unrelated_folder_is_still_removed(self):
        other = self.base / "other"
        other.mkdir()
        safe_io.rmtree(other)
        self.assertFalse(other.exists())

    def test_remove_scratch_refuses_what_is_not_inside_its_scratch_root(self):
        snap = _load("snapshot_homekit")
        scratch_root, outside = self.base / "scratch", self.base / "outside"
        inside = scratch_root / "run"
        inside.mkdir(parents=True)
        outside.mkdir()
        (outside / "f.bin").write_bytes(b"f")
        with mock.patch.object(snap, "SOURCE", self.live2):
            for target in (outside, scratch_root, self.library):
                with self.subTest(target=target.name), self.assertRaises(ValueError):
                    snap.remove_scratch(target, scratch_root)
            snap.remove_scratch(inside, scratch_root)
        self.assertEqual((outside / "f.bin").read_bytes(), b"f")
        self.assertTrue(scratch_root.is_dir())
        self.assertFalse(inside.exists())


class TestTermsFile(_Live):
    def _main(self, out):
        terms = _load("build_scan_terms")
        (self.base / "data" / "tracking").mkdir(parents=True, exist_ok=True)
        extra = self.base / "extra.txt"
        extra.write_text("Jane Example\n")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return terms.main(["--data", str(self.base / "data"), "--out", str(out), "--extra", str(extra)])

    def test_an_existing_0644_terms_file_is_written_0600(self):
        out = self.base / "terms"
        out.write_text("old")
        os.chmod(out, 0o644)
        self.assertEqual(self._main(out), 0)
        self.assertEqual((_mode(out), out.read_text()), (0o600, "Jane Example\n"))

    def test_a_hard_linked_terms_file_is_refused(self):
        out = self.base / "terms"
        os.link(self.dummy, out)
        self.assertEqual(self._main(out), 2)
        self.intact()

    def test_the_source_has_no_chmod_after_the_write(self):
        self.assertNotIn("chmod", (ROOT / "scripts" / "build_scan_terms.py").read_text(encoding="utf-8").replace(
            "# the mode is set before", ""))


if __name__ == "__main__":
    unittest.main()
