"""P4 descriptor-rooted reader and fixed Git query rejection tests."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.projects import register_project
from scripts.pi_control.scoped_read import MAX_FILE_BYTES, ScopedProjectReader, ScopedReadError


def git(path: Path, *args: str) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "P4 Fixture",
        "GIT_AUTHOR_EMAIL": "p4@example.invalid",
        "GIT_COMMITTER_NAME": "P4 Fixture",
        "GIT_COMMITTER_EMAIL": "p4@example.invalid",
    }
    return subprocess.run(["git", "-C", str(path), *args], env=environment, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


class ScopedProjectReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init", "-q", "-b", "main")
        (self.repository / "README").write_text("first\n", encoding="utf-8")
        git(self.repository, "add", "README")
        git(self.repository, "commit", "-qm", "first")
        (self.repository / "README").write_text("second\n", encoding="utf-8")
        (self.repository / "src").mkdir()
        (self.repository / "src/code.py").write_text("needle = True\n", encoding="utf-8")
        git(self.repository, "add", "README", "src/code.py")
        git(self.repository, "commit", "-qm", "second")
        self.store = PiStore(self.root / "state").open()
        self.project = register_project(self.store, self.repository)
        self.working = self.store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (self.project["project_id"],)).fetchone()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def reader(self) -> ScopedProjectReader:
        return ScopedProjectReader(self.store, project_id=self.project["project_id"], working_copy_id=self.working["working_copy_id"])

    def test_read_list_and_grep_are_descriptor_rooted_and_bounded(self) -> None:
        with self.reader() as reader:
            self.assertEqual(reader.read("README")["lines"], ["second"])
            self.assertEqual(reader.list("src"), [{"name": "code.py", "kind": "file"}])
            self.assertEqual(reader.grep("needle", "."), [{"path": "src/code.py", "line": 1, "text": "needle = True"}])
            for path in ("../outside", "/etc/passwd", "~/.ssh/id_ed25519", ".git/config", "src//code.py"):
                with self.subTest(path=path), self.assertRaises(ScopedReadError):
                    reader.read(path)

    def test_file_and_directory_symlinks_never_escape(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_text("secret\n", encoding="utf-8")
        (self.repository / "file-link").symlink_to(outside / "secret")
        (self.repository / "dir-link").symlink_to(outside, target_is_directory=True)
        with self.reader() as reader:
            with self.assertRaises(ScopedReadError):
                reader.read("file-link")
            with self.assertRaises(ScopedReadError):
                reader.read("dir-link/secret")
            self.assertEqual(reader.grep("secret", "."), [])
            kinds = {item["name"]: item["kind"] for item in reader.list(".")}
            self.assertEqual(kinds["file-link"], "symlink")
            self.assertEqual(kinds["dir-link"], "symlink")

    def test_root_inode_replacement_is_rejected(self) -> None:
        reader = self.reader()
        original = self.root / "original"
        self.repository.rename(original)
        self.repository.mkdir()
        (self.repository / "README").write_text("replacement\n", encoding="utf-8")
        try:
            with self.assertRaisesRegex(ScopedReadError, "replaced"):
                reader.read("README")
        finally:
            reader.close()

    def test_huge_files_and_trees_fail_before_unbounded_consumption(self) -> None:
        (self.repository / "huge").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        tree = self.repository / "tree"
        tree.mkdir()
        for index in range(5):
            (tree / f"file-{index}").write_text("x\n", encoding="utf-8")
        with self.reader() as reader:
            with self.assertRaisesRegex(ScopedReadError, "byte bound"):
                reader.read("huge")
            with mock.patch("scripts.pi_control.scoped_read.MAX_TREE_ENTRIES", 3), self.assertRaisesRegex(ScopedReadError, "entry bound"):
                reader.grep("missing", "tree")

    def test_cross_project_working_copy_is_rejected(self) -> None:
        other = self.root / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        (other / "README").write_text("other\n", encoding="utf-8")
        git(other, "add", "README")
        git(other, "commit", "-qm", "other")
        project = register_project(self.store, other)
        working = self.store.conn.execute("SELECT working_copy_id FROM working_copies WHERE project_id=?", (project["project_id"],)).fetchone()[0]
        with self.assertRaisesRegex(ScopedReadError, "not registered in project"):
            ScopedProjectReader(self.store, project_id=self.project["project_id"], working_copy_id=working)

    def test_fixed_git_queries_bind_repository_and_revision(self) -> None:
        with self.reader() as reader:
            self.assertEqual(reader.git("rev-parse")["revision"], self.working["expected_head_oid"])
            self.assertIn("second", reader.git("log", limit=1)["output"])
            self.assertIn("second", reader.git("show", path="README")["output"])
            self.assertIn("README", reader.git("diff")["output"])
            self.assertIn("# branch.oid", reader.git("status")["output"])
            with self.assertRaises(ScopedReadError):
                reader.git("fetch")
        (self.repository / "README").write_text("moved\n", encoding="utf-8")
        git(self.repository, "add", "README")
        git(self.repository, "commit", "-qm", "branch moved")
        with self.reader() as reader, self.assertRaisesRegex(ScopedReadError, "authenticated revision"):
            reader.git("show", path="README")


if __name__ == "__main__":
    unittest.main()
