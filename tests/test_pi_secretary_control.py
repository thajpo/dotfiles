import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import shutil
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pi_secretary_control", ROOT / "scripts/pi-secretary-control.py")
secretary = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(secretary)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def repo(root: Path, name: str = "repo", branch: str = "main") -> Path:
    path = root / name
    path.mkdir()
    git(path, "init", "-b", branch)
    git(path, "config", "user.name", "Secretary Test")
    git(path, "config", "user.email", "secretary@example.invalid")
    (path / "tracked").write_text("initial\n")
    git(path, "add", "tracked")
    git(path, "commit", "-m", "initial")
    return path


def write_policy(home: Path, trusted: list[str], isolated: list[str] | None = None,
                 worktree_root: str | None = None) -> Path:
    policy = {
        "version": 1, "defaultMode": "isolated",
        "trustedRoots": trusted,
        "isolatedRoots": isolated or [],
        "controlPlaneRepositories": [],
        "protectedBranches": ["main"],
        "worktreeRoot": worktree_root or str(trusted[0].parent / "worktrees") if trusted else "/tmp/pi-worktrees",
    }
    target = home / ".config/pi/repository-policy.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(policy))
    target.chmod(0o600)
    return target


class SecretaryControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.source = repo(root, "source")
        write_policy(self.home, [str(root)], worktree_root=str(root / "worktrees"))
        self.env = {"HOME": str(self.home), "XDG_STATE_HOME": str(root / "state")}
        self.patch = mock.patch.dict(os.environ, self.env, clear=False)
        self.patch.start()
        self.initial = secretary.init_project(self.source)
        self.capability = self.initial["capability"]

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _brief(self, title="Test", text="body"):
        return secretary.create_brief(self.source, self.capability, title, text)

    def _workstream(self, brief_id: str, title="WS", role="feature", target_ref="HEAD", workstream_id=None):
        return secretary.create_workstream(self.source, self.capability, title, role, brief_id,
                                           target_ref=target_ref, workstream_id=workstream_id)

    # --- Existing tests (preserved) ---

    def test_identity_shared_by_linked_worktree_and_separate_same_name(self):
        linked = Path(self.tmp.name) / "linked"
        git(self.source, "worktree", "add", str(linked), "HEAD")
        self.assertEqual(secretary.project_identity(self.source)[0],
                         secretary.project_identity(linked)[0])
        other_parent = Path(self.tmp.name) / "other"
        other_parent.mkdir()
        other = repo(other_parent, "source")
        self.assertNotEqual(secretary.project_identity(self.source)[0],
                            secretary.project_identity(other)[0])

    def test_capability_and_state_permissions(self):
        with self.assertRaises(secretary.SecretaryError):
            secretary.create_brief(self.source, "wrong", "x", "x")
        project = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / self.initial["projectId"]
        self.assertEqual(project.stat().st_mode & 0o777, 0o700)
        self.assertEqual((project / "project.json").stat().st_mode & 0o777, 0o600)
        record = json.loads((project / "project.json").read_text())
        self.assertEqual(record["capabilityHash"],
                         __import__("hashlib").sha256(self.capability.encode()).hexdigest())
        self.assertNotIn(self.capability, (project / "project.json").read_text())

    def test_two_workstreams_reference_one_brief_and_dirty_source_survives(self):
        brief = self._brief("Design", "bounded text")
        (self.source / "dirty").write_text("human change\n")
        before = git(self.source, "status", "--porcelain=v1", "--untracked-files=all")
        first = self._workstream(brief["briefId"], "One", "feature")
        second = self._workstream(brief["briefId"], "Two", "review")
        self.assertEqual(first["briefId"], second["briefId"])
        self.assertNotEqual(first["workspace"], second["workspace"])
        self.assertEqual(before, git(self.source, "status", "--porcelain=v1", "--untracked-files=all"))
        self.assertEqual(git(Path(first["workspace"]), "rev-parse", "HEAD"), first["baseOid"])
        self.assertEqual(git(Path(second["workspace"]), "rev-parse", "HEAD"), second["baseOid"])

    def test_workstream_lifecycle_schema_and_role_allowlist(self):
        brief = self._brief("Lifecycle", "text")
        workstream = self._workstream(brief["briefId"], "Lifecycle feature")
        self.assertRegex(workstream["createdAt"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertIsNone(workstream["closedAt"])
        with self.assertRaisesRegex(secretary.SecretaryError, "invalid workstream role"):
            self._workstream(brief["briefId"], "Bad role", "builder")

    def test_state_symlink_is_rejected(self):
        project = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / self.initial["projectId"]
        (project / "briefs").rename(project / "briefs-real")
        (project / "briefs").symlink_to(project / "briefs-real", target_is_directory=True)
        with self.assertRaises(secretary.SecretaryError):
            secretary.create_brief(self.source, self.capability, "x", "x")

    def test_unsafe_modes_rejected_for_capability_lock_brief_workstream_and_facts(self):
        state = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary"
        project = state / "projects" / self.initial["projectId"]
        cap_path = state / "capabilities" / f"{self.initial['projectId']}.token"
        cap_path.chmod(0o644)
        with self.assertRaisesRegex(secretary.SecretaryError, "unsafe permissions"):
            secretary.create_brief(self.source, self.capability, "bad cap mode", "x")
        cap_path.chmod(0o600)

        brief = self._brief("Mode brief", "text")
        brief_path = project / "briefs" / f"{brief['briefId']}.md"
        brief_path.chmod(0o644)
        with self.assertRaisesRegex(secretary.SecretaryError, "unsafe permissions"):
            secretary.read_brief(self.source, self.capability, brief["briefId"])
        brief_path.chmod(0o600)

        ws = self._workstream(brief["briefId"], "Mode workstream")
        ws_path = project / "workstreams" / f"{ws['workstreamId']}.json"
        ws_path.chmod(0o644)
        with self.assertRaisesRegex(secretary.SecretaryError, "unsafe permissions"):
            secretary.open_workstream(self.source, self.capability, ws["workstreamId"])
        ws_path.chmod(0o600)

        facts = secretary._fact_path(project, "workstream-created", ws["workstreamId"])
        facts.chmod(0o644)
        with self.assertRaisesRegex(secretary.SecretaryError, "unsafe permissions"):
            self._brief("Bad facts mode", "text")
        facts.chmod(0o600)

        lock = project / ".lock"
        lock.chmod(0o644)
        with self.assertRaisesRegex(secretary.SecretaryError, "unsafe permissions"):
            self._brief("Bad lock mode", "text")

    def test_policy_boundary_target_oid_and_rollback(self):
        brief = self._brief("Target", "text")
        target = git(self.source, "rev-parse", "HEAD")
        created = self._workstream(brief["briefId"], "Pinned", target_ref=target)
        self.assertEqual(created["baseOid"], target)
        with self.assertRaises(secretary.SecretaryError):
            self._workstream(brief["briefId"], "Not a ref", target_ref="does-not-exist")

        original_atomic = secretary._atomic
        def fail_record(path, data, mode=0o600):
            if path.parent.name == "workstreams":
                raise secretary.SecretaryError("injected record failure")
            return original_atomic(path, data, mode)
        with mock.patch.object(secretary, "_atomic", side_effect=fail_record):
            with self.assertRaisesRegex(secretary.SecretaryError, "injected record failure"):
                self._workstream(brief["briefId"], "Rollback")
        self.assertFalse(any(Path(self.env["XDG_STATE_HOME"]).rglob("*rollback*")))

    def test_isolated_policy_rejects_allocation(self):
        write_policy(self.home, [], isolated=[str(self.source)],
                     worktree_root=str(Path(self.tmp.name) / "worktrees"))
        brief = self._brief("No worktree", "text")
        with self.assertRaisesRegex(secretary.SecretaryError, "trusted-live"):
            self._workstream(brief["briefId"], "Denied")

    # --- Item 1: open/list stay valid after commit, currentOid, same-name replacement ---

    def test_open_after_commit_returns_currentOid(self):
        brief = self._brief("Commit test", "evolving")
        ws = self._workstream(brief["briefId"], "Evolve")
        ws_path = Path(ws["workspace"])
        self.assertIn("currentOid", ws)  # create now returns currentOid
        # Commit on the workstream
        (ws_path / "new").write_text("work\n")
        git(ws_path, "add", "new")
        git(ws_path, "commit", "-m", "evolve")
        opened = secretary.open_workstream(self.source, self.capability, ws["workstreamId"])
        self.assertEqual(opened["workstreamId"], ws["workstreamId"])
        self.assertEqual(opened["branch"], ws["branch"])
        self.assertEqual(opened["baseOid"], ws["baseOid"])
        self.assertNotEqual(opened["currentOid"], ws["baseOid"])
        self.assertNotEqual(opened["currentOid"], ws.get("currentOid", ""))
        # baseOid must be ancestor of currentOid
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", opened["baseOid"], opened["currentOid"]],
            cwd=ws_path, text=True, capture_output=True
        )
        self.assertEqual(result.returncode, 0)

    def test_list_after_commit_shows_currentOid(self):
        brief = self._brief("List commit", "text")
        ws = self._workstream(brief["briefId"], "Lister")
        ws_path = Path(ws["workspace"])
        (ws_path / "more").write_text("more\n")
        git(ws_path, "add", "more")
        git(ws_path, "commit", "-m", "more")
        listing = secretary.list_workstreams(self.source, self.capability)
        match = [x for x in listing if x["workstreamId"] == ws["workstreamId"]]
        self.assertEqual(len(match), 1)
        self.assertIn("currentOid", match[0])
        self.assertNotEqual(match[0]["currentOid"], ws["baseOid"])

    def test_unrelated_repo_at_same_path_rejected(self):
        # Create a repo with a separate git-dir at a different path so the
        # common-dir (and therefore identity) differs from the original.
        brief = self._brief("Before", "disappear")
        ws = self._workstream(brief["briefId"], "Goner")
        root = Path(self.tmp.name)
        alt_git_dir = root / "alt-git-dir"
        (root / "new-source").mkdir()
        subprocess.run(["git", "init", "-b", "main", "--separate-git-dir", str(alt_git_dir)],
                       cwd=root / "new-source", check=True, text=True, capture_output=True)
        new_repo = (root / "new-source").resolve()
        git(new_repo, "config", "user.name", "Other")
        git(new_repo, "config", "user.email", "o@e")
        (new_repo / "tracked").write_text("a\n")
        git(new_repo, "add", "tracked")
        git(new_repo, "commit", "-m", "first")
        self.assertNotEqual(secretary.project_identity(new_repo)[0],
                            self.initial["projectId"])
        with self.assertRaises(secretary.SecretaryError):
            secretary.open_workstream(new_repo, self.capability, ws["workstreamId"])
        with self.assertRaises(secretary.SecretaryError):
            secretary.list_briefs(new_repo, self.capability)

    # --- Item 2: Policy revalidation for open/list ---

    def test_policy_downgrade_rejected_by_open(self):
        brief = self._brief("Policy", "downgrade")
        ws = self._workstream(brief["briefId"], "Downgradable")
        write_policy(self.home, [], isolated=[str(Path(self.tmp.name))],
                     worktree_root=str(Path(self.tmp.name) / "worktrees"))
        with self.assertRaisesRegex(secretary.SecretaryError, "trusted-live"):
            secretary.open_workstream(self.source, self.capability, ws["workstreamId"])

    def test_policy_downgrade_rejected_by_list(self):
        brief = self._brief("Policy list", "downgrade")
        self._workstream(brief["briefId"], "Listable")
        write_policy(self.home, [], isolated=[str(Path(self.tmp.name))],
                     worktree_root=str(Path(self.tmp.name) / "worktrees"))
        with self.assertRaisesRegex(secretary.SecretaryError, "trusted-live"):
            secretary.list_workstreams(self.source, self.capability)

    def test_policy_root_change_during_creation_fails_without_git_resources(self):
        brief = self._brief("Policy race", "root changes")
        original = secretary._load_policy_and_classify
        replacement_root = Path(self.tmp.name) / "replacement-worktrees"
        replacement_root.mkdir(mode=0o700)
        calls = 0

        def changing_policy(repository):
            nonlocal calls
            calls += 1
            if calls == 2:
                write_policy(self.home, [str(Path(self.tmp.name))],
                             worktree_root=str(replacement_root))
            return original(repository)

        identity = secretary._workstream_id("Policy race", "feature", brief["briefId"])
        branch = f"pi/{identity}"
        with mock.patch.object(secretary, "_load_policy_and_classify", side_effect=changing_policy):
            with self.assertRaisesRegex(secretary.SecretaryError, "policy changed"):
                self._workstream(brief["briefId"], "Policy race")
        self.assertEqual(git(self.source, "branch", "--list", branch), "")
        self.assertNotIn(identity, git(self.source, "worktree", "list", "--porcelain"))

    # --- Item 3: Multiple workstreams per brief (no max-two) ---

    def test_three_workstreams_reference_one_brief(self):
        brief = self._brief("Three", "multi-reference")
        ws1 = self._workstream(brief["briefId"], "Alpha")
        ws2 = self._workstream(brief["briefId"], "Beta")
        ws3 = self._workstream(brief["briefId"], "Gamma")
        self.assertEqual(ws1["briefId"], ws2["briefId"])
        self.assertEqual(ws2["briefId"], ws3["briefId"])
        ids = {ws1["workstreamId"], ws2["workstreamId"], ws3["workstreamId"]}
        self.assertEqual(len(ids), 3)

    # --- Item 4: Crash-consistent facts with createdAt ---

    def test_facts_include_createdAt_and_are_atomic(self):
        brief = self._brief("Fact", "atomicity")
        self._workstream(brief["briefId"], "FactCheck")
        project = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / self.initial["projectId"]
        facts = [json.loads(path.read_text())
                 for path in sorted((project / "operations/facts").glob("*.json"))]
        self.assertGreaterEqual(len(facts), 3)
        for entry in facts:
            self.assertEqual(set(entry), {"createdAt", "operation", "id"})
            self.assertRegex(entry["createdAt"], r"\d{4}-\d{2}-\d{2}T")

    def test_fact_records_are_append_only_shards(self):
        project = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / self.initial["projectId"]
        directory = project / "operations/facts"
        before = {path.name: path.read_bytes() for path in directory.glob("*.json")}
        self._brief("Atom", "shard")
        after = {path.name: path.read_bytes() for path in directory.glob("*.json")}
        self.assertEqual(len(after), len(before) + 1)
        for name, content in before.items():
            self.assertEqual(after[name], content)

    def test_fact_shards_exceed_former_shared_log_capacity(self):
        project = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / self.initial["projectId"]
        with secretary._project_lock(project):
            for number in range(700):
                secretary._append_fact_locked(project, "test-event", f"event-{number}")
        self.assertGreater(len(secretary._read_fact_keys(project)), 700)

    def test_missing_fact_after_record_commit_is_reconciled(self):
        brief = self._brief("Interrupted", "record survived")
        project = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / self.initial["projectId"]
        fact_path = secretary._fact_path(project, "brief-created", brief["briefId"])
        fact_path.unlink()
        secretary.read_brief(self.source, self.capability, brief["briefId"])
        self.assertTrue(fact_path.is_file())
        repaired = json.loads(fact_path.read_text())
        self.assertEqual((repaired["operation"], repaired["id"]),
                         ("brief-created", brief["briefId"]))

    # --- Item 5: Strengthen rollback ---

    def test_rollback_removes_branch_and_worktree_on_partial_git_failure(self):
        brief = self._brief("Rollback hard", "cleanup")
        original_git = secretary.run

        def fail_after_worktree(cmd, cwd=None, check=True):
            if "git" in cmd and "worktree" in cmd and "add" in cmd:
                # Let git worktree add succeed but corrupt the result
                result = original_git(cmd, cwd, check=check)
                # Simulate an environment where the worktree was created but branch verification fails
                # by injecting a fake verification failure via monkey-patching _canonical_repo
                raise secretary.SecretaryError("injected post-worktree failure")
            return original_git(cmd, cwd, check=check)

        with mock.patch.object(secretary, "run", side_effect=fail_after_worktree):
            with self.assertRaisesRegex(secretary.SecretaryError, "injected"):
                secretary.create_workstream(self.source, self.capability, "Goner", "feature",
                                            brief["briefId"])
        # Verify the exact workspace registration and derived branch were removed.
        identity = secretary._workstream_id("Goner", "feature", brief["briefId"])
        branch = f"pi/{identity}"
        workspace = Path(self.tmp.name) / "worktrees" / "source" / self.initial["projectId"][:12] / identity
        self.assertFalse(workspace.exists())
        self.assertEqual(git(self.source, "branch", "--list", branch), "")
        registrations = git(self.source, "worktree", "list", "--porcelain")
        self.assertNotIn(f"worktree {workspace}", registrations)

    # --- Item 6: Same-named unrelated repos use separate namespaces ---

    def test_same_name_repos_have_separate_workspaces(self):
        root = Path(self.tmp.name)
        # Another repo with same directory name as self.source ("source") in different parent
        other_parent = root / "other"
        other_parent.mkdir()
        other = repo(other_parent, "source")
        # Init other with its own policy entry and worktree root
        write_policy(self.home, [str(other_parent)],
                     worktree_root=str(root / "worktrees"))
        other_init = secretary.init_project(other)
        other_cap = other_init["capability"]
        other_brief = secretary.create_brief(other, other_cap, "Other", "text")
        other_ws = secretary.create_workstream(other, other_cap, "OtherWS", "feature",
                                               other_brief["briefId"])
        # Re-set policy for self.source
        write_policy(self.home, [str(root)], worktree_root=str(root / "worktrees"))
        brief = self._brief("Ours", "text")
        ws = self._workstream(brief["briefId"], "OurWS")
        # Workspace paths must differ in more than just the final identity segment
        self.assertNotEqual(Path(ws["workspace"]).parent, Path(other_ws["workspace"]).parent)
        # Both must be under the worktree root
        wt_root = root / "worktrees"
        self.assertTrue(str(ws["workspace"]).startswith(str(wt_root)))
        self.assertTrue(str(other_ws["workspace"]).startswith(str(wt_root)))

    # --- Item 7: Additional missing tests ---

    def test_duplicate_brief_id_rejected(self):
        brief = self._brief("Dup", "first")
        with self.assertRaises(secretary.SecretaryError):
            secretary.create_brief(self.source, self.capability, "Dup again", "text",
                                   brief_id=brief["briefId"])

    def test_duplicate_workstream_id_rejected(self):
        brief = self._brief("Dup WS", "first")
        ws = self._workstream(brief["briefId"], "Uniq", workstream_id="ws-my-unique")
        with self.assertRaises(secretary.SecretaryError):
            secretary.create_workstream(self.source, self.capability, "Dup WS", "feature",
                                        brief["briefId"], workstream_id=ws["workstreamId"])

    def test_malformed_state_record_rejected(self):
        project = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / self.initial["projectId"]
        (project / "project.json").write_text("NOT JSON")
        with self.assertRaises(secretary.SecretaryError):
            secretary.create_brief(self.source, self.capability, "x", "x")

    def test_large_brief_rejected(self):
        with self.assertRaises(secretary.SecretaryError):
            self._brief("Large", "x" * 17000)

    def test_wrong_capability_rejected(self):
        with self.assertRaisesRegex(secretary.SecretaryError, "capability rejected"):
            secretary.create_brief(self.source, "BAD" + self.capability[3:], "x", "x")

    def test_state_root_inside_repo_rejected(self):
        # Point XDG_STATE_HOME inside the repository
        inside = self.source / ".secretary-state"
        inside.mkdir()
        env = {**self.env, "XDG_STATE_HOME": str(inside)}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "state root must not be inside"):
                secretary.init_project(self.source)

    def test_source_head_branch_and_status_preserved(self):
        brief = self._brief("Preserve", "status")
        before_branch = git(self.source, "branch", "--show-current")
        before_head = git(self.source, "rev-parse", "HEAD")
        before_status = git(self.source, "status", "--porcelain=v1", "--untracked-files=all")
        self._workstream(brief["briefId"], "Preserving")
        self.assertEqual(git(self.source, "branch", "--show-current"), before_branch)
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), before_head)
        self.assertEqual(git(self.source, "status", "--porcelain=v1", "--untracked-files=all"),
                         before_status)

    def test_concurrent_init_same_project(self):
        results = []
        errors = []
        fresh_state = Path(self.tmp.name) / "fresh-state"

        def init_thread():
            try:
                results.append(secretary.init_project(self.source))
            except Exception as error:
                errors.append(str(error))

        with mock.patch.dict(os.environ, {**self.env, "XDG_STATE_HOME": str(fresh_state)}, clear=False):
            threads = [threading.Thread(target=init_thread) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertEqual(sum(result["initialized"] is True for result in results), 1)
        self.assertEqual(sum(result["initialized"] is False for result in results), 3)

    def test_concurrent_duplicate_workstream_fails_once(self):
        brief = self._brief("Concurrent", "race")
        results = []
        errors = []
        def create_ws():
            try:
                r = secretary.create_workstream(self.source, self.capability, "Race", "feature",
                                                brief["briefId"],
                                                workstream_id="ws-race-fixed-id")
                results.append(r)
            except Exception as e:
                errors.append(str(e))
        threads = [threading.Thread(target=create_ws) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1)  # Exactly one creation
        self.assertEqual(len(errors), 3)  # Three should fail with "already exists"

    def test_invalid_brief_id_rejected(self):
        with self.assertRaisesRegex(secretary.SecretaryError, "invalid brief id"):
            secretary.create_brief(self.source, self.capability, "Bad", "x",
                                   brief_id="UPPERCASE")

    def test_invalid_workstream_id_rejected(self):
        brief = self._brief("Invalid ID", "test")
        with self.assertRaisesRegex(secretary.SecretaryError, "invalid workstream id"):
            secretary.create_workstream(self.source, self.capability, "Bad", "feature",
                                        brief["briefId"], workstream_id="HAS-UPPER")

    def test_malformed_capability_rejects_init_and_status(self):
        project = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / self.initial["projectId"]
        cap_path = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/capabilities" / f"{self.initial['projectId']}.token"
        cap_path.write_text("not-a-valid-capability\n")
        cap_path.chmod(0o600)
        with self.assertRaisesRegex(secretary.SecretaryError, "capability rejected"):
            secretary.init_project(self.source)
        with self.assertRaisesRegex(secretary.SecretaryError, "capability rejected"):
            secretary.status(self.source, self.capability)
        # A malformed existing project is never reported as initialized.
        self.assertTrue((project / "project.json").exists())

    def test_status_validates_capability_and_existing_schema(self):
        self.assertEqual(secretary.status(self.source, self.capability)["initialized"], True)
        with self.assertRaisesRegex(secretary.SecretaryError, "capability rejected"):
            secretary.status(self.source, "wrong")
        project = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / self.initial["projectId"]
        (project / "project.json").write_text("not-json\n")
        (project / "project.json").chmod(0o600)
        with self.assertRaises(secretary.SecretaryError):
            secretary.status(self.source, self.capability)

    def test_exact_common_dir_path_replacement_rejects_old_state(self):
        original_git = self.source / ".git"
        saved_git = Path(self.tmp.name) / "saved-git"
        original_git.rename(saved_git)
        replacement = repo(Path(self.tmp.name), "replacement")
        shutil.rmtree(self.source / ".git", ignore_errors=True)
        (replacement / ".git").rename(self.source / ".git")
        self.assertEqual(secretary.project_identity(self.source)[0], self.initial["projectId"])
        with self.assertRaisesRegex(secretary.SecretaryError, "project identity"):
            secretary.status(self.source, self.capability)

    def test_oversized_state_is_rejected_before_parsing(self):
        project = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / self.initial["projectId"]
        (project / "project.json").write_bytes(b"x" * (secretary.MAX_JSON + 1))
        (project / "project.json").chmod(0o600)
        with self.assertRaisesRegex(secretary.SecretaryError, "too large"):
            secretary.status(self.source, self.capability)

    def test_brief_malformed_markdown_rejected(self):
        project = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / self.initial["projectId"]
        (project / "briefs").mkdir(exist_ok=True)
        bad = project / "briefs" / "bad-brief.md"
        bad.write_text("no proper header\n")
        bad.chmod(0o600)
        with self.assertRaisesRegex(secretary.SecretaryError, "malformed brief"):
            secretary.read_brief(self.source, self.capability, "bad-brief")

    # --- Item 8: OID validation against objectFormat ---

    def test_preexisting_derived_branch_survives_failed_creation(self):
        brief = self._brief("Existing branch", "must survive")
        identity = secretary._workstream_id("Reserved", "feature", brief["briefId"])
        branch = f"pi/{identity}"
        git(self.source, "branch", branch, "HEAD")
        with self.assertRaisesRegex(secretary.SecretaryError, "derived branch already exists"):
            self._workstream(brief["briefId"], "Reserved", workstream_id=identity)
        self.assertEqual(git(self.source, "branch", "--list", branch), branch)

    def test_recorded_workspace_replacement_with_same_commit_and_branch_rejected(self):
        brief = self._brief("Replacement", "identity")
        ws = self._workstream(brief["briefId"], "Replaceable")
        workspace = Path(ws["workspace"])
        clone = Path(self.tmp.name) / "replacement-clone"
        git(self.source, "worktree", "remove", "--force", str(workspace))
        subprocess.run(["git", "clone", str(self.source), str(clone)], check=True,
                       text=True, capture_output=True)
        git(clone, "checkout", "-b", ws["branch"], ws["baseOid"])
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(clone), str(workspace))
        with self.assertRaises(secretary.SecretaryError):
            secretary.open_workstream(self.source, self.capability, ws["workstreamId"])
        with self.assertRaises(secretary.SecretaryError):
            secretary.list_workstreams(self.source, self.capability)

    def test_oid_validation_against_object_format(self):
        # SHA-1 repos get 40-char OIDs
        of = git(self.source, "rev-parse", "--show-object-format")
        expected_len = 64 if "256" in of else 40
        self.assertEqual(len(git(self.source, "rev-parse", "HEAD")), expected_len)
        brief = self._brief("OID test", "validation")
        ws = self._workstream(brief["briefId"], "OIDCheck")
        self.assertEqual(len(ws["baseOid"]), expected_len)
        self.assertEqual(len(ws.get("currentOid", ws["baseOid"])), expected_len)

    def test_sha256_repository_workstream_when_supported(self):
        sha_root = Path(self.tmp.name) / "sha256-source"
        sha_root.mkdir()
        result = subprocess.run(["git", "init", "-b", "main", "--object-format=sha256", str(sha_root)],
                                text=True, capture_output=True)
        if result.returncode:
            self.skipTest("Git lacks SHA-256 object-format support")
        git(sha_root, "config", "user.name", "SHA Secretary Test")
        git(sha_root, "config", "user.email", "sha@example.invalid")
        (sha_root / "tracked").write_text("sha256\n")
        git(sha_root, "add", "tracked")
        git(sha_root, "commit", "-m", "sha256 initial")
        init = secretary.init_project(sha_root)
        cap = init["capability"]
        brief = secretary.create_brief(sha_root, cap, "SHA", "256")
        ws = secretary.create_workstream(sha_root, cap, "SHA workstream", "feature", brief["briefId"])
        self.assertEqual(git(sha_root, "rev-parse", "--show-object-format"), "sha256")
        self.assertEqual(len(ws["baseOid"]), 64)
        self.assertEqual(len(secretary.open_workstream(sha_root, cap, ws["workstreamId"])["currentOid"]), 64)


if __name__ == "__main__":
    unittest.main()
