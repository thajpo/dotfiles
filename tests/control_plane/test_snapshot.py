from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.pi_control.snapshot import (
    SnapshotConcurrentMutationError,
    SnapshotConflictError,
    SnapshotError,
    SnapshotIntegrityError,
    SnapshotPolicy,
    _manifest_digest,
    capture_snapshot,
    load_snapshot,
    reconcile_snapshot,
)


class Failpoint:
    def __init__(self, selected: str):
        self.selected = selected

    def hit(self, name: str, _context: dict[str, str]) -> None:
        if name == self.selected:
            raise RuntimeError(name)


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Snapshot Test")
        self._git("config", "user.email", "snapshot@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-qm", "base")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Snapshot Test",
            "GIT_AUTHOR_EMAIL": "snapshot@example.invalid",
            "GIT_COMMITTER_NAME": "Snapshot Test",
            "GIT_COMMITTER_EMAIL": "snapshot@example.invalid",
        }
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=environment,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _index_digest(self) -> str:
        index = self.repo / ".git" / "index"
        return hashlib.sha256(index.read_bytes()).hexdigest()

    def _tree_paths(self, commit: str) -> set[str]:
        return set(self._git("ls-tree", "-r", "--name-only", commit).stdout.splitlines())

    def test_clean_snapshot_is_immutable_and_does_not_touch_real_index(self) -> None:
        before_head = self._head()
        before_index = self._index_digest()
        record = capture_snapshot(self.repo, self.state, snapshot_id="snap_" + "1" * 32)
        self.assertFalse(record.dirty)
        self.assertEqual(record.source_head_oid, before_head)
        self.assertEqual(record.changed_paths, ())
        self.assertEqual(record.snapshot_tree_oid, record.source_tree_oid)
        self.assertEqual(self._head(), before_head)
        self.assertEqual(self._index_digest(), before_index)
        self.assertTrue((self.state / "snapshots" / record.snapshot_id / "manifest.json").stat().st_mode & 0o777 == 0o600)
        self.assertEqual(load_snapshot(self.state, record.snapshot_id, self.repo).manifest_digest, record.manifest_digest)

    def test_dirty_tracked_capture_excludes_untracked_by_default(self) -> None:
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("not selected\n", encoding="utf-8")
        record = capture_snapshot(self.repo, self.state, snapshot_id="snap_" + "2" * 32)
        self.assertTrue(record.dirty)
        self.assertEqual(set(record.changed_paths), {"tracked.txt"})
        self.assertEqual(self._tree_paths(record.snapshot_commit_oid), {"tracked.txt"})

    def test_untracked_and_ignored_policy_is_explicit(self) -> None:
        self._git("config", "core.excludesFile", os.devnull)
        (self.repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (self.repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        selected = capture_snapshot(
            self.repo,
            self.state,
            snapshot_id="snap_" + "3" * 32,
            policy=SnapshotPolicy(include_untracked=True),
        )
        selected_paths = self._tree_paths(selected.snapshot_commit_oid)
        self.assertIn("untracked.txt", selected_paths)
        self.assertNotIn("ignored.txt", selected_paths)
        included = capture_snapshot(
            self.repo,
            self.state,
            snapshot_id="snap_" + "4" * 32,
            policy=SnapshotPolicy(include_untracked=True, include_ignored=True),
        )
        self.assertIn("ignored.txt", self._tree_paths(included.snapshot_commit_oid))

    def test_staged_and_unstaged_same_file_captures_worktree_and_preserves_index(self) -> None:
        (self.repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        index_before = self._index_digest()
        (self.repo / "tracked.txt").write_text("final worktree\n", encoding="utf-8")
        record = capture_snapshot(self.repo, self.state, snapshot_id="snap_" + "5" * 32)
        content = self._git("show", f"{record.snapshot_commit_oid}:tracked.txt").stdout
        self.assertEqual(content, "final worktree\n")
        self.assertEqual(self._index_digest(), index_before)

    def test_all_repository_execution_surface_configuration_is_rejected(self) -> None:
        surfaces = (
            ("filter.unsafe.clean", "sh -c 'cat'"),
            ("include.path", str(self.root / "included-config")),
            ("credential.helper", "!echo unsafe"),
            ("config.worktree", str(self.root / "other")),
            ("core.worktree", str(self.root / "other")),
            ("core.fsmonitor", "true"),
            ("core.splitIndex", "true"),
            ("diff.external", "unsafe-diff"),
        )
        for index, (key, value) in enumerate(surfaces, start=1):
            with self.subTest(key=key):
                self._git("config", key, value)
                try:
                    with self.assertRaises(SnapshotError):
                        capture_snapshot(self.repo, self.state, snapshot_id=f"snap_{index:032x}")
                finally:
                    self._git("config", "--unset-all", key, check=False)

    def test_symlink_and_special_files_are_rejected(self) -> None:
        os.symlink("tracked.txt", self.repo / "link.txt")
        with self.assertRaises(SnapshotError):
            capture_snapshot(self.repo, self.state, snapshot_id="snap_" + "6" * 32)
        (self.repo / "link.txt").unlink()
        os.mkfifo(self.repo / "pipe")
        with self.assertRaises(SnapshotError):
            capture_snapshot(self.repo, self.state, snapshot_id="snap_" + "7" * 32)

    def test_allow_symlink_policy_captures_link_without_following_target(self) -> None:
        os.symlink("tracked.txt", self.repo / "link.txt")
        record = capture_snapshot(
            self.repo,
            self.state,
            snapshot_id="snap_" + "8" * 32,
            policy=SnapshotPolicy(allow_symlinks=True, include_untracked=True),
        )
        mode = self._git("ls-tree", record.snapshot_commit_oid, "link.txt").stdout
        self.assertIn("link.txt", mode)

    def test_duplicate_snapshot_id_is_rejected(self) -> None:
        capture_snapshot(self.repo, self.state, snapshot_id="snap_" + "9" * 32)
        with self.assertRaises(SnapshotConflictError):
            capture_snapshot(self.repo, self.state, snapshot_id="snap_" + "9" * 32)

    def test_ref_update_crash_is_reconciled_without_deleting_ref(self) -> None:
        snapshot_id = "snap_" + "a" * 32
        with self.assertRaises(RuntimeError):
            capture_snapshot(self.repo, self.state, snapshot_id=snapshot_id, failpoint=Failpoint("snapshot.ref.after"))
        ref = f"refs/pi/snapshots/{snapshot_id}"
        self.assertTrue(self._git("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0)
        recovered = reconcile_snapshot(self.repo, self.state, snapshot_id)
        self.assertTrue(recovered.recovered)
        self.assertEqual(load_snapshot(self.state, snapshot_id, self.repo).snapshot_commit_oid, recovered.snapshot_commit_oid)

    def test_manifest_write_crash_is_reconciled_from_existing_ref(self) -> None:
        snapshot_id = "snap_" + "b" * 32
        with self.assertRaises(RuntimeError):
            capture_snapshot(self.repo, self.state, snapshot_id=snapshot_id, failpoint=Failpoint("manifest.write.before"))
        recovered = reconcile_snapshot(self.repo, self.state, snapshot_id)
        self.assertTrue(recovered.recovered)
        self.assertEqual(recovered.snapshot_id, snapshot_id)

    def test_manifest_or_ref_disagreement_fails_closed(self) -> None:
        snapshot_id = "snap_" + "c" * 32
        record = capture_snapshot(self.repo, self.state, snapshot_id=snapshot_id)
        manifest = self.state / "snapshots" / snapshot_id / "manifest.json"
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["refName"] = "refs/pi/snapshots/snap_" + "d" * 32
        value["manifestDigest"] = _manifest_digest(value)
        manifest.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        with self.assertRaises(SnapshotIntegrityError):
            load_snapshot(self.state, snapshot_id, self.repo)

    def test_dirty_submodule_is_rejected_even_with_explicit_submodule_policy(self) -> None:
        module = self.repo / "module"
        module.mkdir()
        environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Snapshot Test",
            "GIT_AUTHOR_EMAIL": "snapshot@example.invalid",
            "GIT_COMMITTER_NAME": "Snapshot Test",
            "GIT_COMMITTER_EMAIL": "snapshot@example.invalid",
        }
        def module_git(*args: str) -> str:
            result = subprocess.run(["git", *args], cwd=module, env=environment, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return result.stdout.strip()
        module_git("init", "-q", "-b", "main")
        module_git("config", "user.name", "Nested")
        module_git("config", "user.email", "nested@example.invalid")
        (module / "nested.txt").write_text("base\n", encoding="utf-8")
        module_git("add", "nested.txt")
        module_git("commit", "-qm", "nested")
        nested_head = module_git("rev-parse", "HEAD")
        (self.repo / ".gitmodules").write_text("[submodule \"module\"]\n\tpath = module\n\turl = ../module\n", encoding="utf-8")
        self._git("add", ".gitmodules")
        self._git("update-index", "--add", "--cacheinfo", f"160000,{nested_head},module")
        self._git("commit", "-qm", "gitlink")
        capture_snapshot(self.repo, self.state, snapshot_id="snap_" + "a" * 32, policy=SnapshotPolicy(allow_submodules=True))
        (module / "nested.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(SnapshotError):
            capture_snapshot(self.repo, self.state, snapshot_id="snap_" + "b" * 32, policy=SnapshotPolicy(allow_submodules=True))

    def test_mutation_during_capture_aborts(self) -> None:
        import scripts.pi_control.snapshot as snapshot_module
        original = snapshot_module._filesystem_inventory
        calls = 0

        def mutate(repository: Path, policy: SnapshotPolicy):
            nonlocal calls
            calls += 1
            value = original(repository, policy)
            if calls == 3:
                (repository / "tracked.txt").write_text("raced after final inventory\n", encoding="utf-8")
            return value

        snapshot_module._filesystem_inventory = mutate
        try:
            with self.assertRaises(SnapshotConcurrentMutationError):
                capture_snapshot(self.repo, self.state, snapshot_id="snap_" + "d" * 32)
        finally:
            snapshot_module._filesystem_inventory = original

    def test_invalid_snapshot_id_is_rejected_before_filesystem_access(self) -> None:
        with self.assertRaises(ValueError):
            capture_snapshot(self.repo, self.state, snapshot_id="snapshot/not-safe")


if __name__ == "__main__":
    unittest.main()
