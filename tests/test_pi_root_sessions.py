import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/pi-root-session.py"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Root Test")
    git(repo, "config", "user.email", "root@example.invalid")
    (repo / "tracked").write_text("initial\n")
    git(repo, "add", "tracked")
    git(repo, "commit", "-m", "initial")
    return repo


class RootSessionTests(unittest.TestCase):
    def run_helper(self, home: Path, worktrees: Path, *args: str, check: bool = True):
        env = os.environ.copy()
        env.update({"HOME": str(home), "PI_ROOT_WORKTREE_ROOT": str(worktrees)})
        return subprocess.run(
            [str(HELPER), "--agent-dir", str(home / ".pi/agent"), *args],
            env=env, text=True, capture_output=True, check=check,
        )

    def test_dirty_root_retains_in_place_branch_and_uncommitted_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); home = root / "home"; home.mkdir(); worktrees = root / "worktrees"; worktrees.mkdir()
            repo = make_repo(root)
            tracked = repo / "tracked"
            tracked.write_text("initial\nuncommitted\n")
            first = json.loads(self.run_helper(home, worktrees, "ensure", "--conversation-id", "dirty-root", "--cwd", str(repo)).stdout)
            second = json.loads(self.run_helper(home, worktrees, "ensure", "--conversation-id", "dirty-root", "--cwd", str(repo)).stdout)
            self.assertEqual(first["branch"], "main")
            self.assertEqual(first["worktree"], str(repo))
            self.assertEqual(first["worktree"], second["worktree"])
            self.assertEqual(tracked.read_text(), "initial\nuncommitted\n")

    def test_exact_root_and_worktree_are_reused_without_cwd_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"; home.mkdir()
            worktrees = root / "worktrees"; worktrees.mkdir()
            repo = make_repo(root)
            first = json.loads(self.run_helper(home, worktrees, "ensure", "--conversation-id", "root-one", "--profile", "personal", "--cwd", str(repo)).stdout)
            second = json.loads(self.run_helper(home, worktrees, "ensure", "--conversation-id", "root-one", "--profile", "personal", "--cwd", str(root)).stdout)
            self.assertEqual(first["sessionFile"], second["sessionFile"])
            self.assertEqual(first["worktree"], second["worktree"])
            self.assertNotEqual(Path(first["worktree"]).resolve(), repo.resolve())
            self.assertEqual(json.loads(Path(first["sessionFile"]).read_text().splitlines()[0])["id"], "root-one")

            other = json.loads(self.run_helper(home, worktrees, "ensure", "--conversation-id", "root-two", "--profile", "personal", "--cwd", str(repo)).stdout)
            self.assertNotEqual(first["sessionFile"], other["sessionFile"])
            self.assertNotEqual(first["worktree"], other["worktree"])
            records = json.loads((home / ".pi/agent/root-registry.json").read_text())["records"]
            self.assertEqual({record["conversationId"] for record in records}, {"root-one", "root-two"})
            self.assertTrue(all(Path(record["sessionFile"]).parent.name == "root" for record in records))

    def test_migration_selects_newest_duplicate_and_archives_other_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"; agent = home / ".pi/agent"; legacy = agent / "sessions/tmux/old"; legacy.mkdir(parents=True)
            worktrees = root / "worktrees"; worktrees.mkdir()
            repo = make_repo(root)
            lines = [
                json.dumps({"type": "session", "version": 3, "id": "legacy-one", "timestamp": "2026-01-01T00:00:00Z", "cwd": str(repo)}),
                json.dumps({"type": "message", "id": "m1", "message": {"role": "user", "content": "old"}}),
            ]
            older = legacy / "older.jsonl"; newer = legacy / "newer.jsonl"
            older.write_text("\n".join(lines) + "\n"); newer.write_text("\n".join(lines).replace("old", "new") + "\n")
            os.utime(older, (1, 1)); os.utime(newer, (2, 2))
            result = self.run_helper(home, worktrees, "migrate")
            self.assertEqual(result.returncode, 0, result.stderr)
            records = json.loads((agent / "root-registry.json").read_text())["records"]
            active = [record for record in records if record["status"] == "active"]
            archived = [record for record in records if record["status"] == "archived"]
            self.assertEqual(len(active), 1)
            self.assertEqual(len(archived), 1)
            self.assertEqual(active[0]["conversationId"], "legacy-one")
            self.assertTrue(Path(active[0]["sessionFile"]).parent.name == "root")
            self.assertTrue(Path(archived[0]["sessionFile"]).parent.name == "archive")
            migrated_header = json.loads(Path(active[0]["sessionFile"]).read_text().splitlines()[0])
            self.assertEqual(migrated_header["id"], "legacy-one")
            self.assertEqual(migrated_header["parentSession"], str(newer.resolve()))
            self.assertEqual(older.read_text(), "\n".join(lines) + "\n")
            self.assertIn("old", older.read_text())
            self.assertTrue(Path(active[0]["worktree"]).is_dir())

    def test_cleanup_only_removes_stale_metadata_inside_managed_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); home = root / "home"; home.mkdir()
            worktrees = root / "worktrees"; worktrees.mkdir()
            unmanaged = root / "unmanaged"; unmanaged.mkdir()
            repo = make_repo(root)
            managed_branch = "pi/20260101T000000Z-aaaaaaaa"
            unmanaged_branch = "pi/20260101T000000Z-bbbbbbbb"
            managed_path = worktrees / "repo"; unmanaged_path = unmanaged / "repo"
            git(repo, "worktree", "add", "-b", managed_branch, str(managed_path))
            git(repo, "worktree", "add", "-b", unmanaged_branch, str(unmanaged_path))
            shutil.rmtree(managed_path)
            shutil.rmtree(unmanaged_path)

            preview = json.loads(self.run_helper(home, worktrees, "cleanup", "--repository", str(repo)).stdout)
            self.assertEqual([item["branch"] for item in preview["staleWorktrees"]], [managed_branch])
            self.assertEqual([item["branch"] for item in preview["prunableWorktrees"]], [managed_branch])
            self.assertIn(unmanaged_branch, git(repo, "branch", "--list", unmanaged_branch))

            self.run_helper(home, worktrees, "cleanup", "--repository", str(repo), "--apply")
            self.assertNotIn(managed_branch, git(repo, "branch", "--list", managed_branch))
            self.assertIn(unmanaged_branch, git(repo, "branch", "--list", unmanaged_branch))
            self.assertIn(unmanaged_branch, git(repo, "worktree", "list", "--porcelain"))

    def test_archive_moves_only_session_file_and_does_not_prune_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); home = root / "home"; home.mkdir(); worktrees = root / "worktrees"; worktrees.mkdir(); repo = make_repo(root)
            record = json.loads(self.run_helper(home, worktrees, "ensure", "--conversation-id", "archive-me", "--cwd", str(repo)).stdout)
            before = git(repo, "worktree", "list", "--porcelain")
            self.run_helper(home, worktrees, "archive", "--conversation-id", "archive-me")
            after = git(repo, "worktree", "list", "--porcelain")
            self.assertEqual(before, after)
            self.assertFalse(Path(record["sessionFile"]).exists())
            self.assertTrue(any(Path(item).parent.name == "archive" for item in (home / ".pi/agent/sessions/root/archive").glob("*.jsonl")))


if __name__ == "__main__":
    unittest.main()
