from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pisec.decisions import list_decisions, record_decision, resolve_decision
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import observe_project, register_project


def make_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Pisec Test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "pisec@example.invalid"], check=True)
    (path / "README").write_text("fixture\n")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "fixture"], check=True)


class ProjectTests(unittest.TestCase):
    def test_registration_uses_common_git_directory_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            link = root / "alias"
            link.symlink_to(repo, target_is_directory=True)
            with PiStore(root / "state") as store:
                first = register_project(store, repo, display_name="Example", default_ref="main")
                second = register_project(store, link, display_name="Ignored", default_ref="main")
                self.assertEqual(first["project_id"], second["project_id"])
                self.assertEqual(first["git_common_dir"], observe_project(repo)["git_common_dir"])
                self.assertEqual(store.conn.execute("SELECT count(*) FROM projects").fetchone()[0], 1)

    def test_linked_worktree_observes_same_common_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            linked = root / "linked"
            make_repo(repo)
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "other", str(linked), "main"], check=True)
            self.assertEqual(observe_project(repo)["git_common_dir"], observe_project(linked)["git_common_dir"])

    def test_decisions_are_project_scoped_and_resolve_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            with PiStore(root / "state") as store:
                project = register_project(store, repo, default_ref="main")
                decision = record_decision(store, project_id=project["project_id"], summary="Use exact scopes", context={"reason": "fail closed"})
                self.assertEqual([decision["decision_id"]], [item["decision_id"] for item in list_decisions(store, project["project_id"], state="open")])
                resolved = resolve_decision(store, project_id=project["project_id"], decision_id=decision["decision_id"], resolution="Approved")
                self.assertEqual(resolved["state"], "resolved")
                self.assertEqual(resolve_decision(store, project_id=project["project_id"], decision_id=decision["decision_id"], resolution="Approved")["resolution"], "Approved")


if __name__ == "__main__":
    unittest.main()
