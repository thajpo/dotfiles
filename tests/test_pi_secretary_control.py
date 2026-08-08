import contextlib
import fcntl
import hashlib
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
        self.env = {"HOME": str(self.home), "XDG_STATE_HOME": str(root / "state"),
                     "PI_CODING_AGENT_DIR": str(self.home / ".pi" / "agent")}
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

    def test_registry_round_trip_is_readable_without_capability(self):
        registered = secretary.register_project(self.source, "primary-repo")
        self.assertEqual(registered["projectId"], self.initial["projectId"])
        self.assertEqual(registered["alias"], "primary-repo")
        listing = secretary.registry_list()
        self.assertEqual(len(listing), 1)
        self.assertNotIn("capability", listing[0])
        info = secretary.launch_info(self.initial["projectId"])
        self.assertEqual(info["secretarySessionId"], registered["secretarySessionId"])
        self.assertNotIn("capability", info)
        internal = secretary.launch_info(self.initial["projectId"], internal=True)
        self.assertEqual(internal["capability"], self.capability)
        state = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary"
        project_record = json.loads((state / "projects" / self.initial["projectId"] / "project.json").read_text())
        self.assertEqual(project_record["schemaVersion"], 1)
        self.assertNotIn("secretarySessionId", project_record)
        self.assertTrue((state / "registry" / f"{self.initial['projectId']}.json").is_file())

    def test_secretary_git_read_is_bounded_and_read_only(self):
        registered = secretary.register_project(self.source, "git-project")
        result = secretary.git_read(registered["projectId"], "status", [])
        self.assertEqual(result["operation"], "status")
        self.assertIn("On branch", result["stdout"])
        with self.assertRaisesRegex(secretary.SecretaryError, "supported"):
            secretary.git_read(registered["projectId"], "worktree", ["add", "/tmp/unsafe"])
        with self.assertRaises(secretary.SecretaryError):
            secretary.git_read(registered["projectId"], "diff", ["--output=/tmp/unsafe"])

        marker = Path(self.tmp.name) / "git-helper-ran"
        helper = Path(self.tmp.name) / "git-helper"
        helper.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        helper.chmod(0o755)
        git(self.source, "config", "core.fsmonitor", str(helper))
        git(self.source, "config", "diff.external", str(helper))
        (self.source / "tracked").write_text("changed\n")
        diff = secretary.git_read(registered["projectId"], "diff", [])
        self.assertIn("changed", diff["stdout"])
        secretary.git_read(registered["projectId"], "status", [])
        self.assertFalse(marker.exists(), "repository-configured helper was executed")

        before = {
            "branches": git(self.source, "branch", "--format=%(refname)"),
            "tags": git(self.source, "tag", "--list"),
            "remotes": git(self.source, "remote", "-v"),
        }
        for operation, args in [
            ("branch", ["new-branch"]),
            ("branch", ["--delete", "main"]),
            ("tag", ["new-tag"]),
            ("tag", ["--delete", "old-tag"]),
            ("remote", ["add", "unsafe", "https://example.invalid/repo"]),
            ("remote", ["set-url", "origin", "https://example.invalid/repo"]),
        ]:
            with self.subTest(operation=operation, args=args):
                if operation in {"branch", "tag"} and args[0] not in {"--delete"}:
                    # Bare values are harmless patterns because the controller
                    # forces --list; they must not create refs.
                    secretary.git_read(registered["projectId"], operation, args)
                else:
                    with self.assertRaises(secretary.SecretaryError):
                        secretary.git_read(registered["projectId"], operation, args)
        after = {
            "branches": git(self.source, "branch", "--format=%(refname)"),
            "tags": git(self.source, "tag", "--list"),
            "remotes": git(self.source, "remote", "-v"),
        }
        self.assertEqual(after, before)

    def test_secretary_git_write_is_capability_bounded_and_redacts_git_diagnostics(self):
        registered = secretary.register_project(self.source, "git-write-project")
        project_id = registered["projectId"]
        with mock.patch.dict(os.environ, {"PI_SECRETARY_CAPABILITY": "wrong"}, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "capability"):
                secretary.git_write(project_id, "push")
            with self.assertRaisesRegex(secretary.SecretaryError, "capability"):
                secretary.git_write(project_id, "commit", "message", ["tracked"])

        with mock.patch.dict(os.environ, {"PI_SECRETARY_CAPABILITY": self.capability}, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "supported"):
                secretary.git_write(project_id, "force-push")
            with self.assertRaisesRegex(secretary.SecretaryError, "relative"):
                secretary.git_write(project_id, "commit", "message", ["../outside"])
            with self.assertRaisesRegex(secretary.SecretaryError, "relative"):
                secretary.git_write(project_id, "commit", "message", ["*.py"])
            with self.assertRaisesRegex(secretary.SecretaryError, "commit arguments"):
                secretary.git_write(project_id, "push", "not-allowed", [])
            with self.assertRaisesRegex(secretary.SecretaryError, "origin remote"):
                secretary.git_write(project_id, "push")
            head_before = git(self.source, "rev-parse", "HEAD")
            (self.source / "tracked").write_text("commit-and-push must preflight\n")
            with self.assertRaisesRegex(secretary.SecretaryError, "origin remote"):
                secretary.git_write(project_id, "commit-and-push", "must not commit", ["tracked"])
            self.assertEqual(git(self.source, "rev-parse", "HEAD"), head_before)

            (self.source / "tracked").write_text("secretary commit\n")
            committed = secretary.git_write(project_id, "commit", "bounded commit", ["tracked"])
            self.assertEqual(committed["operation"], "commit")
            self.assertEqual(committed["branch"], "main")
            self.assertNotIn("remote", committed)

            remote = Path(self.tmp.name) / "origin.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                           text=True, capture_output=True)
            git(self.source, "remote", "add", "origin", str(remote))
            pushed = secretary.git_write(project_id, "push")
            self.assertEqual(pushed["remote"], "origin")
            self.assertTrue(pushed["pushed"])
            rendered = json.dumps(pushed)
            self.assertNotIn(str(remote), rendered)
            self.assertNotIn("https://", rendered)

            (self.source / "new-file").write_text("new secretary file\n")
            added = secretary.git_write(project_id, "commit", "bounded new file", ["new-file"])
            self.assertEqual(added["operation"], "commit")
            self.assertIn("new-file", git(self.source, "show", "--format=", "--name-only", "HEAD"))

            (self.source / "tracked").write_text("secretary composite\n")
            combined = secretary.git_write(project_id, "commit-and-push", "bounded combined", ["tracked"])
            self.assertEqual(combined["operation"], "commit-and-push")
            self.assertTrue(combined["pushed"])

        failed = subprocess.CompletedProcess(
            ["git", "push"], 1, "", "fatal: https://user:secret@example.invalid/repo.git failed")
        with mock.patch.object(secretary.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(secretary.SecretaryError, "exit 1") as error:
                secretary._git_write_command(self.source, "push", ["git", "push"])
        self.assertNotIn("user:secret", str(error.exception))
        self.assertNotIn("example.invalid", str(error.exception))

    def test_secretary_git_write_preserves_index_on_failed_commit(self):
        registered = secretary.register_project(self.source, "git-write-index-project")
        with mock.patch.dict(os.environ, {"PI_SECRETARY_CAPABILITY": self.capability}, clear=False):
            (self.source / "unrelated").write_text("keep staged\n")
            git(self.source, "add", "unrelated")
            before = git(self.source, "diff", "--cached", "--name-status")
            head_before = git(self.source, "rev-parse", "HEAD")
            (self.source / "tracked").write_text("failed secretary commit\n")
            original = secretary._git_write_command

            def fail_commit(repo_path, operation, command, **kwargs):
                if operation == "commit":
                    raise secretary.SecretaryError("git commit failed (exit 1)")
                return original(repo_path, operation, command, **kwargs)

            with mock.patch.object(secretary, "_git_write_command", side_effect=fail_commit):
                with self.assertRaisesRegex(secretary.SecretaryError, "exit 1"):
                    secretary.git_write(registered["projectId"], "commit", "will fail", ["tracked"])
            self.assertEqual(git(self.source, "diff", "--cached", "--name-status"), before)
            self.assertEqual(git(self.source, "rev-parse", "HEAD"), head_before)
            committed = secretary.git_write(registered["projectId"], "commit", "bounded after failure", ["tracked"])
            self.assertEqual(committed["operation"], "commit")
            self.assertIn("A\tunrelated", git(self.source, "diff", "--cached", "--name-status"))
            self.assertNotIn("unrelated", git(self.source, "show", "--format=", "--name-only", "HEAD"))

    def test_secretary_git_cleanup_plans_and_applies_exact_owned_resources(self):
        registered = secretary.register_project(self.source, "cleanup-project")
        project_id = registered["projectId"]
        for branch in ("benchmark/38-direct-sol", "benchmark/39-direct-sol", "benchmark/discard", "side-agent/stale"):
            git(self.source, "branch", branch)
        side_path = Path(self.tmp.name) / "worktrees" / "side-agent-stale"
        git(self.source, "worktree", "add", str(side_path), "side-agent/stale")

        agent_dir = self.home / ".pi" / "agent"
        artifact = agent_dir / "sessions" / "sec-cleanup" / "subagent-artifacts" / "benchmark-meta.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("benchmark-only artifact\n")
        artifact.chmod(0o600)
        artifact_digest = __import__("hashlib").sha256(artifact.read_bytes()).hexdigest()
        oid = git(self.source, "rev-parse", "benchmark/38-direct-sol")
        side_oid = git(self.source, "rev-parse", "side-agent/stale")
        plan = {
            "version": 1,
            "renames": [
                {"from": "benchmark/38-direct-sol", "to": "feature/rq024-foundation-execution", "expectedOid": oid},
                {"from": "benchmark/39-direct-sol", "to": "feature/rq024-evidence-index", "expectedOid": oid},
            ],
            "deletions": [
                {"branch": "benchmark/discard", "expectedOid": oid},
                {"branch": "side-agent/stale", "expectedOid": side_oid},
            ],
            "worktrees": [{"path": str(side_path), "branch": "side-agent/stale", "expectedOid": side_oid}],
            "artifacts": [{"path": str(artifact), "kind": "subagent-artifact", "expectedSha256": artifact_digest}],
        }
        source_before = (self.source / "tracked").read_text()
        with mock.patch.object(secretary, "_cleanup_worktree_has_live_process", return_value=False), \
             mock.patch.dict(os.environ, {"PI_SECRETARY_CAPABILITY": self.capability, "PI_CODING_AGENT_DIR": str(agent_dir)}, clear=False):
            planned = secretary.git_cleanup(project_id, "plan", plan)
            self.assertEqual(planned["operation"], "plan")
            self.assertEqual(planned["counts"], {"renames": 2, "deletions": 2, "worktrees": 1, "artifacts": 1})
            self.assertIn("expectedIdentity", planned["plan"]["artifacts"][0])
            self.assertIn("expectedParentIdentity", planned["plan"]["artifacts"][0])
            with self.assertRaisesRegex(secretary.SecretaryError, "pinned artifact identities"):
                secretary.git_cleanup(project_id, "apply", plan, planned["planHash"])
            self.assertTrue(side_path.exists())
            self.assertTrue(artifact.exists())
            self.assertTrue(subprocess.run(["git", "show-ref", "--verify", "--quiet", "refs/heads/benchmark/discard"], cwd=self.source).returncode == 0)

            applied = secretary.git_cleanup(project_id, "apply", planned["plan"], planned["planHash"])
        self.assertTrue(applied["applied"])
        self.assertFalse(side_path.exists())
        self.assertFalse(artifact.exists())
        self.assertFalse(subprocess.run(["git", "show-ref", "--verify", "--quiet", "refs/heads/benchmark/38-direct-sol"], cwd=self.source).returncode == 0)
        self.assertFalse(subprocess.run(["git", "show-ref", "--verify", "--quiet", "refs/heads/benchmark/discard"], cwd=self.source).returncode == 0)
        self.assertEqual(git(self.source, "rev-parse", "feature/rq024-foundation-execution"), oid)
        self.assertEqual(git(self.source, "rev-parse", "feature/rq024-evidence-index"), oid)
        self.assertEqual(git(self.source, "status", "--porcelain=v1", "--untracked-files=all"), "")
        self.assertEqual((self.source / "tracked").read_text(), source_before)

    def test_secretary_git_cleanup_refuses_active_shared_worktree_lease(self):
        registered = secretary.register_project(self.source, "cleanup-active-lease")
        project_id = registered["projectId"]
        branch = "side-agent/leased"
        side_path = Path(self.tmp.name) / "worktrees" / "side-agent-leased"
        git(self.source, "branch", branch)
        git(self.source, "worktree", "add", str(side_path), branch)
        oid = git(self.source, "rev-parse", branch)
        plan = {"version": 1, "deletions": [{"branch": branch, "expectedOid": oid}],
                "worktrees": [{"path": str(side_path), "branch": branch, "expectedOid": oid}]}
        common = secretary._git_path(self.source, "rev-parse", "--path-format=absolute", "--git-common-dir")
        key = hashlib.sha256(f"{common}\0{side_path.resolve()}".encode()).hexdigest()
        lease_root = Path(self.env["XDG_STATE_HOME"]) / "pi" / "worktree-leases"
        lease_root.mkdir(parents=True, mode=0o700)
        for path in (lease_root.parent.parent, lease_root.parent, lease_root):
            path.chmod(0o700)
        lease_path = lease_root / f"{key}.lock"
        lease_fd = os.open(lease_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lease_fd, fcntl.LOCK_SH)
        try:
            with mock.patch.object(secretary, "_cleanup_worktree_has_live_process", return_value=False), \
                 mock.patch.dict(os.environ, {"PI_SECRETARY_CAPABILITY": self.capability}, clear=False):
                planned = secretary.git_cleanup(project_id, "plan", plan)
                with self.assertRaisesRegex(secretary.SecretaryError, "active Pi lease"):
                    secretary.git_cleanup(project_id, "apply", planned["plan"], planned["planHash"])
            self.assertTrue(side_path.exists())
        finally:
            os.close(lease_fd)
        with mock.patch.object(secretary, "_cleanup_worktree_has_live_process", return_value=False), \
             mock.patch.dict(os.environ, {"PI_SECRETARY_CAPABILITY": self.capability}, clear=False):
            applied = secretary.git_cleanup(project_id, "apply", planned["plan"], planned["planHash"])
        self.assertTrue(applied["applied"])
        self.assertFalse(side_path.exists())

    def test_secretary_git_cleanup_replays_after_worktree_side_effect_before_refs(self):
        registered = secretary.register_project(self.source, "cleanup-replay")
        project_id = registered["projectId"]
        branch = "side-agent/replay"
        side_path = Path(self.tmp.name) / "worktrees" / "side-agent-replay"
        git(self.source, "branch", branch)
        git(self.source, "worktree", "add", str(side_path), branch)
        oid = git(self.source, "rev-parse", branch)
        plan = {"version": 1, "deletions": [{"branch": branch, "expectedOid": oid}],
                "worktrees": [{"path": str(side_path), "branch": branch, "expectedOid": oid}]}
        environment = {"PI_SECRETARY_CAPABILITY": self.capability}
        with mock.patch.object(secretary, "_cleanup_worktree_has_live_process", return_value=False), \
             mock.patch.dict(os.environ, environment, clear=False):
            planned = secretary.git_cleanup(project_id, "plan", plan)
            with mock.patch.object(secretary, "_apply_cleanup_ref_transaction",
                                   side_effect=secretary.SecretaryError("simulated ref interruption")):
                with self.assertRaisesRegex(secretary.SecretaryError, "simulated ref interruption"):
                    secretary.git_cleanup(project_id, "apply", planned["plan"], planned["planHash"])
        project = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary" / "projects" / project_id
        recovery = next((project / "operations").glob(f"cleanup-git-{planned['planHash']}.json"))
        manifest = json.loads(recovery.read_text())
        self.assertEqual(manifest["phase"], "error")
        self.assertIn(str(side_path), manifest["completedWorktrees"])
        with mock.patch.object(secretary, "_cleanup_worktree_has_live_process", return_value=False), \
             mock.patch.dict(os.environ, environment, clear=False):
            replayed = secretary.git_cleanup(project_id, "apply", planned["plan"], planned["planHash"])
        self.assertTrue(replayed["recovered"])
        self.assertFalse(side_path.exists())
        self.assertNotEqual(subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                                             cwd=self.source, check=False).returncode, 0)

    def test_secretary_git_cleanup_refuses_stale_oids_and_source_artifacts(self):
        registered = secretary.register_project(self.source, "cleanup-refusal")
        git(self.source, "branch", "benchmark/stale")
        with mock.patch.dict(os.environ, {"PI_SECRETARY_CAPABILITY": self.capability}, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "OID changed"):
                secretary.git_cleanup(registered["projectId"], "plan", {
                    "version": 1,
                    "deletions": [{"branch": "benchmark/stale", "expectedOid": "0" * 40}],
                })
            with self.assertRaisesRegex(secretary.SecretaryError, "artifact cannot be inside"):
                secretary.git_cleanup(registered["projectId"], "plan", {
                    "version": 1,
                    "artifacts": [{"path": str(self.source / "tracked"), "kind": "subagent-artifact", "expectedSha256": "0" * 64}],
                })

    def test_concurrent_duplicate_alias_registration_fails_once(self):
        other = repo(Path(self.tmp.name), "other-repository")
        barrier = threading.Barrier(2)
        results, errors = [], []

        def register(repository):
            try:
                barrier.wait()
                results.append(secretary.register_project(repository, "same-alias"))
            except Exception as error:
                errors.append(str(error))

        threads = [threading.Thread(target=register, args=(repository,))
                   for repository in (self.source, other)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("alias already registered", errors[0])
        self.assertEqual([item["alias"] for item in secretary.registry_list()], ["same-alias"])

    def test_registry_lookup_revalidates_primary_repository_identity(self):
        secretary.register_project(self.source, "Primary")
        state = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary"
        record_path = state / "registry" / f"{self.initial['projectId']}.json"
        record = json.loads(record_path.read_text())
        record["primaryRepository"] = str(Path(self.tmp.name) / "missing")
        record_path.write_text(json.dumps(record))
        record_path.chmod(0o600)
        with self.assertRaises(secretary.SecretaryError):
            secretary.registry_list()

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

    def test_promote_allocates_stable_runtime_and_launches_with_bounded_environment(self):
        registered = secretary.register_project(self.source, "promotion-project")
        output = Path(self.tmp.name) / "pidev-env.json"
        fake = Path(self.tmp.name) / "fake-pidev"
        fake.write_text("#!/usr/bin/env python3\nimport json,os,pathlib\npathlib.Path(%r).write_text(json.dumps({k:os.environ.get(k) for k in ['PI_PIDEV_SESSION_ID','PI_PIDEV_WORKSTREAM_ID','PI_PIDEV_BRIEF_PATH','PI_ROOT_PROFILE']}))\n" % str(output))
        fake.chmod(0o700)
        with mock.patch.object(secretary, "_pidev_path", return_value=fake), \
             mock.patch.object(secretary, "_wait_for_managed_process", return_value=True), \
             mock.patch.dict(os.environ, {"TMUX": "/tmp/promotion-tmux,11,0"}, clear=False):
            promoted = secretary.promote_workstream(registered["projectId"], "Promoted", "Goal and boundaries", "feature")
        self.assertRegex(promoted["piSessionId"], r"^ws-[0-9a-f]{48}$")
        self.assertTrue(promoted["tmuxSession"].startswith("pi-source-"))
        self.assertEqual(promoted["tmuxSocket"], "/tmp/promotion-tmux")
        launched = json.loads(output.read_text())
        self.assertEqual(launched["PI_PIDEV_SESSION_ID"], promoted["piSessionId"])
        self.assertEqual(launched["PI_PIDEV_WORKSTREAM_ID"], promoted["workstreamId"])
        self.assertTrue(Path(launched["PI_PIDEV_BRIEF_PATH"]).is_file())
        self.assertEqual(launched["PI_ROOT_PROFILE"], "worker")
        opened = secretary.open_workstream(self.source, self.capability, promoted["workstreamId"])
        self.assertEqual(opened["piSessionId"], promoted["piSessionId"])
        with mock.patch.object(secretary, "_pidev_path", return_value=fake), \
             mock.patch.dict(os.environ, {"TMUX": "/tmp/other-tmux,22,0"}, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "different tmux server"):
                secretary.launch_workstream(registered["projectId"], promoted["workstreamId"])

    def test_failed_initial_launch_preserves_durable_workstream_for_reopen(self):
        registered = secretary.register_project(self.source, "failed-launch")
        fake = Path(self.tmp.name) / "failing-pidev"
        fake.write_text("#!/bin/sh\nexit 17\n")
        fake.chmod(0o700)
        with mock.patch.object(secretary, "_pidev_path", return_value=fake):
            with self.assertRaisesRegex(secretary.SecretaryError, "workstream launch failed"):
                secretary.promote_workstream(registered["projectId"], "Survives", "Durable intent", "feature")
        workstreams = secretary.list_workstreams(self.source, self.capability)
        self.assertEqual(len(workstreams), 1)
        self.assertTrue(Path(workstreams[0]["workspace"]).is_dir())
        self.assertEqual(len(secretary.list_briefs(self.source, self.capability)), 1)

    def test_bounded_attention_requires_owned_feature_route_and_exact_ack(self):
        registered = secretary.register_project(self.source, "attention-project")
        brief = self._brief()
        workstream = self._workstream(brief["briefId"], workstream_id="attention-work")
        route_cap = "route-capability"
        route = Path(self.tmp.name) / "route.json"
        route.write_text(json.dumps({"uid": os.getuid(), "capabilityHash": __import__("hashlib").sha256(route_cap.encode()).hexdigest(),
                                     "readOnly": False, "worktree": workstream["workspace"]}))
        route.chmod(0o600)
        with mock.patch.dict(os.environ, {"PI_TASK_ROUTE_FILE": str(route),
                                          "PI_TASK_ROUTE_CAPABILITY": route_cap}, clear=False):
            event = secretary.append_event(registered["projectId"], workstream["workstreamId"],
                                            "needs-user", "Choose the durable format", "Two bounded options")
        self.assertEqual(event["source"], "agent")
        self.assertEqual(secretary.list_events(registered["projectId"])[0]["eventId"], event["eventId"])
        with mock.patch.dict(os.environ, {"PI_TASK_ROUTE_FILE": str(route),
                                          "PI_TASK_ROUTE_CAPABILITY": "wrong"}, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "route rejected"):
                secretary.append_event(registered["projectId"], workstream["workstreamId"],
                                        "referral", "Wrong route")
        with mock.patch.dict(os.environ, {"PI_SECRETARY_CAPABILITY": self.capability}, clear=False):
            acknowledged = secretary.acknowledge_event(registered["projectId"], event["eventId"])
        self.assertIsNotNone(acknowledged["acknowledgedAt"])
        self.assertEqual(secretary.list_events(registered["projectId"]), [])
        self.assertEqual(len(secretary.list_events(registered["projectId"], include_acknowledged=True)), 1)

    def test_progress_attention_event_is_bounded_and_project_scoped(self):
        registered = secretary.register_project(self.source, "progress-project")
        brief = self._brief("Progress", "bounded progress")
        workstream = self._workstream(brief["briefId"], workstream_id="progress-work")
        route_cap = "progress-route"
        route = Path(self.tmp.name) / "progress-route.json"
        route.write_text(json.dumps({"uid": os.getuid(), "capabilityHash": __import__("hashlib").sha256(route_cap.encode()).hexdigest(),
                                     "readOnly": False, "worktree": workstream["workspace"]}))
        route.chmod(0o600)
        with mock.patch.dict(os.environ, {"PI_TASK_ROUTE_FILE": str(route), "PI_TASK_ROUTE_CAPABILITY": route_cap}, clear=False):
            event = secretary.append_event(registered["projectId"], workstream["workstreamId"],
                                           "progress", "Reached the validation seam", '{"AGENT_FEEDBACK":{"kind":"suggestion"}}')
        self.assertEqual(event["kind"], "progress")
        self.assertEqual(event["source"], "agent")
        self.assertNotEqual(event["details"], '{"AGENT_FEEDBACK":{"kind":"suggestion"}}')
        event_details = json.loads(event["details"])
        self.assertEqual(event_details["feedbackId"], event["eventId"])
        self.assertEqual(event_details["form"]["kind"], "suggestion")
        self.assertEqual(secretary.list_events(registered["projectId"])[0]["eventId"], event["eventId"])
        feedback_path = self.home / ".pi" / "agent" / "feedback" / "records" / f"{event['eventId']}.json"
        feedback = json.loads(feedback_path.read_text())
        self.assertEqual(feedback["source"]["projectId"], registered["projectId"])
        self.assertEqual(feedback["source"]["workstreamId"], workstream["workstreamId"])
        self.assertFalse((self.source / "feedback").exists())

    def test_feedback_store_rejects_a_project_root_path(self):
        registered = secretary.register_project(self.source, "feedback-boundary")
        brief = self._brief("Feedback boundary", "reject repository storage")
        workstream = self._workstream(brief["briefId"], workstream_id="feedback-boundary-work")
        route_cap = "feedback-boundary-route"
        route = Path(self.tmp.name) / "feedback-boundary-route.json"
        route.write_text(json.dumps({"uid": os.getuid(), "capabilityHash": __import__("hashlib").sha256(route_cap.encode()).hexdigest(),
                                     "readOnly": False, "worktree": workstream["workspace"]}))
        route.chmod(0o600)
        inside_agent_dir = self.source / "feedback-agent"
        with mock.patch.dict(os.environ, {"PI_TASK_ROUTE_FILE": str(route),
                                          "PI_TASK_ROUTE_CAPABILITY": route_cap,
                                          "PI_CODING_AGENT_DIR": str(inside_agent_dir)}, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "must not be inside"):
                secretary.append_event(registered["projectId"], workstream["workstreamId"],
                                       "progress", "Rejected storage", "AGENT_FEEDBACK {\"kind\":\"risk\"}")
        self.assertFalse(inside_agent_dir.exists())

    def test_process_exit_is_host_fact_not_completion(self):
        registered = secretary.register_project(self.source, "process-exit")
        brief = self._brief()
        workstream = self._workstream(brief["briefId"], workstream_id="exiting-work")
        event = secretary.record_process_exit(workstream["workspace"], 143)
        self.assertEqual(event["kind"], "process-exit")
        self.assertEqual(event["source"], "host")
        opened = secretary.open_workstream(self.source, self.capability, workstream["workstreamId"])
        self.assertIsNone(opened["closedAt"])

    def test_exact_oid_review_receipt_and_later_commit_staleness(self):
        registered = secretary.register_project(self.source, "review-project")
        brief = self._brief()
        workstream = self._workstream(brief["briefId"], workstream_id="reviewed-work")
        feature = Path(workstream["workspace"])
        (feature / "tracked").write_text("candidate\n")
        git(feature, "add", "tracked"); git(feature, "commit", "-m", "candidate")
        route_cap = "review-route"
        route = Path(self.tmp.name) / "review-route.json"
        import hashlib
        route.write_text(json.dumps({"uid": os.getuid(), "capabilityHash": hashlib.sha256(route_cap.encode()).hexdigest(),
                                     "readOnly": False, "worktree": workstream["workspace"]}))
        route.chmod(0o600)
        with mock.patch.dict(os.environ, {"PI_TASK_ROUTE_FILE": str(route), "PI_TASK_ROUTE_CAPABILITY": route_cap}, clear=False):
            event = secretary.append_event(registered["projectId"], workstream["workstreamId"],
                                            "review-requested", "Review the candidate", "model text is replaced")
        request_id = json.loads(event["details"])["reviewRequestId"]
        fake = Path(self.tmp.name) / "review-pidev"
        fake.write_text("#!/bin/sh\nexit 19\n"); fake.chmod(0o700)
        with mock.patch.object(secretary, "_pidev_path", return_value=fake), \
             mock.patch.object(secretary, "_reviewer_process_live", return_value=False), \
             mock.patch.dict(os.environ, {"PI_SECRETARY_CAPABILITY": self.capability}, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "review launch failed"):
                secretary.create_reviewer(registered["projectId"], event["eventId"])
        fake.write_text("#!/bin/sh\nexit 0\n")
        with mock.patch.object(secretary, "_pidev_path", return_value=fake), \
             mock.patch.object(secretary, "_wait_for_reviewer_process", return_value=True), \
             mock.patch.dict(os.environ, {"PI_SECRETARY_CAPABILITY": self.capability}, clear=False):
            request = secretary.create_reviewer(registered["projectId"], event["eventId"])
        review_workspace = Path(request["reviewWorkspace"])
        self.assertEqual(git(review_workspace, "rev-parse", "HEAD^{commit}"), request["candidateOid"])
        self.assertEqual(git(review_workspace, "branch", "--show-current"), "")
        git(review_workspace, "checkout", "--detach", request["baseOid"])
        with self.assertRaisesRegex(secretary.SecretaryError, "moved"):
            secretary.review_launch_info(registered["projectId"], request_id)
        git(review_workspace, "checkout", "--detach", request["candidateOid"])
        review_env = {"PI_REVIEW_CAPABILITY": self.capability, "PI_REVIEW_SESSION_ID": request["reviewerSessionId"]}
        with mock.patch.dict(os.environ, {**review_env, "PI_REVIEW_CAPABILITY": "wrong"}, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "capability"):
                secretary.submit_review(registered["projectId"], request_id, "accept", "Looks good", "No findings")
        with mock.patch.dict(os.environ, {**review_env, "PI_REVIEW_SESSION_ID": "wrong"}, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "capability"):
                secretary.submit_review(registered["projectId"], request_id, "accept", "Looks good", "No findings")
        (review_workspace / "dirty").write_text("not allowed")
        with mock.patch.dict(os.environ, review_env, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "dirty"):
                secretary.submit_review(registered["projectId"], request_id, "accept", "Looks good", "No findings")
        (review_workspace / "dirty").unlink()
        project_dir = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / registered["projectId"]
        request_path = project_dir / "reviews/requests" / f"{request_id}.json"
        real_lock = secretary._project_lock
        raced = False
        @contextlib.contextmanager
        def racing_lock(project):
            nonlocal raced
            if not raced:
                changed = json.loads(request_path.read_text())
                changed["reviewerSessionId"] = "rv-" + "f" * 48
                request_path.write_text(json.dumps(changed)); request_path.chmod(0o600)
                raced = True
            with real_lock(project):
                yield
        with mock.patch.dict(os.environ, review_env, clear=False), mock.patch.object(secretary, "_project_lock", racing_lock):
            with self.assertRaisesRegex(secretary.SecretaryError, "changed during"):
                secretary.submit_review(registered["projectId"], request_id, "accept", "Looks good", "No findings")
        request_path.write_text(json.dumps(request)); request_path.chmod(0o600)
        @contextlib.contextmanager
        def workspace_races_after_lock(project):
            with real_lock(project):
                git(review_workspace, "checkout", "--detach", request["baseOid"])
                yield
        with mock.patch.dict(os.environ, review_env, clear=False), mock.patch.object(secretary, "_project_lock", workspace_races_after_lock):
            with self.assertRaisesRegex(secretary.SecretaryError, "moved"):
                secretary.submit_review(registered["projectId"], request_id, "accept", "Looks good", "No findings")
        git(review_workspace, "checkout", "--detach", request["candidateOid"])
        with mock.patch.dict(os.environ, review_env, clear=False):
            receipt = secretary.submit_review(registered["projectId"], request_id, "accept", "Looks good", "No findings")
        self.assertEqual(receipt["candidateOid"], request["candidateOid"])
        with mock.patch.dict(os.environ, review_env, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "conflicting"):
                secretary.submit_review(registered["projectId"], request_id, "reject", "Changed verdict", "Finding")
        (feature / "tracked").write_text("later\n")
        git(feature, "add", "tracked"); git(feature, "commit", "-m", "later")
        status = secretary.review_status(registered["projectId"], request_id)
        self.assertTrue(status["stale"])
        self.assertEqual(status["receipt"]["candidateOid"], request["candidateOid"])
        receipt_path = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / registered["projectId"] / "reviews/receipts" / f"{receipt['receiptId']}.json"
        original = json.loads(receipt_path.read_text())
        malformed = {**original, "workstreamId": "different-work"}
        receipt_path.write_text(json.dumps(malformed)); receipt_path.chmod(0o600)
        with self.assertRaisesRegex(secretary.SecretaryError, "exact assignment"):
            secretary.review_status(registered["projectId"], request_id)
        receipt_path.write_text(json.dumps(original)); receipt_path.chmod(0o600)

    def test_review_request_rejects_dirty_or_rebasing_candidate(self):
        registered = secretary.register_project(self.source, "review-readiness")
        brief = self._brief("Review readiness", "candidate boundaries")
        workstream = self._workstream(brief["briefId"], "Ready candidate", workstream_id="ready-candidate")
        feature = Path(workstream["workspace"])
        route_cap = "review-readiness-route"
        route = Path(self.tmp.name) / "review-readiness-route.json"
        route.write_text(json.dumps({"uid": os.getuid(), "capabilityHash": __import__("hashlib").sha256(route_cap.encode()).hexdigest(),
                                     "readOnly": False, "worktree": workstream["workspace"]}))
        route.chmod(0o600)
        with mock.patch.dict(os.environ, {"PI_TASK_ROUTE_FILE": str(route),
                                          "PI_TASK_ROUTE_CAPABILITY": route_cap}, clear=False):
            (feature / "dirty-candidate").write_text("do not review\n")
            with self.assertRaisesRegex(secretary.SecretaryError, "candidate worktree is dirty"):
                secretary.append_event(registered["projectId"], workstream["workstreamId"],
                                       "review-requested", "Ready")
            (feature / "dirty-candidate").unlink()
            rebase_merge = Path(git(feature, "rev-parse", "--git-path", "rebase-merge"))
            if not rebase_merge.is_absolute():
                rebase_merge = feature / rebase_merge
            rebase_merge.mkdir(parents=True)
            try:
                with self.assertRaisesRegex(secretary.SecretaryError, "unfinished rebase"):
                    secretary.append_event(registered["projectId"], workstream["workstreamId"],
                                           "review-requested", "Ready")
            finally:
                shutil.rmtree(rebase_merge)

    def test_tmux_cleanup_probe_preserves_server_and_fails_closed(self):
        calls = []
        def fake_run(args, **kwargs):
            calls.append((args, kwargs["env"]))
            if args[3] == "list-sessions":
                return subprocess.CompletedProcess(args, 0, stdout="pi-project\n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="w-feature\n", stderr="")
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/custom-tmux,123,0", "TMUX_TMPDIR": "/tmp/custom"}, clear=False), \
             mock.patch.object(secretary.subprocess, "run", side_effect=fake_run):
            socket = secretary._current_tmux_socket()
            self.assertEqual(socket, "/tmp/custom-tmux")
            self.assertTrue(secretary._tmux_window_live("pi-project", "w-feature", socket))
        self.assertTrue(all(args[1:3] == ["-S", "/tmp/custom-tmux"] for args, _ in calls))
        failed = subprocess.CompletedProcess(["tmux"], 1, stdout="", stderr="unreachable")
        with mock.patch.object(secretary.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(secretary.SecretaryError, "cannot prove"):
                secretary._tmux_window_live("pi-project", "w-feature", "/tmp/custom-tmux")

    def test_worktree_recovery_is_bound_and_fails_closed_on_unknown_tmux(self):
        branch = "side-agent/recovery-bound"
        git(self.source, "branch", branch)
        worktree_root = Path(self.tmp.name) / "worktrees"
        worktree_root.mkdir()
        original = worktree_root / "recovery-bound"
        git(self.source, "worktree", "add", str(original), branch)
        oid = git(self.source, "rev-parse", branch)
        quarantine = secretary._worktree_quarantine_path(original, "candidate")
        git(self.source, "worktree", "move", str(original), str(quarantine))
        expected = {
            "kind": "candidate", "originalPath": str(original), "workstreamId": "ws-recovery-bound",
            "requestId": None, "branch": branch, "expectedOid": oid,
            "tmuxSession": "pi-test", "tmuxWindow": "w-candidate", "tmuxSocket": "/tmp/missing-tmux",
            "sessionId": "ws-recovery-bound", "state": "moved",
        }
        with self.assertRaisesRegex(secretary.SecretaryError, "not bound"):
            secretary._resume_quarantined_worktree(self.source, quarantine.with_name("other"),
                                                   worktree_root, expected)
        with mock.patch.object(secretary, "_tmux_window_live",
                               side_effect=secretary.SecretaryError("socket unavailable")), \
             mock.patch.object(secretary, "_cleanup_worktree_has_live_process", return_value=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "socket unavailable"):
                secretary._resume_quarantined_worktree(self.source, quarantine, worktree_root, expected)
        with mock.patch.object(secretary, "_tmux_window_live", return_value=False), \
             mock.patch.object(secretary, "_cleanup_worktree_has_live_process", return_value=False):
            self.assertTrue(secretary._resume_quarantined_worktree(self.source, quarantine, worktree_root, expected))
        self.assertFalse(quarantine.exists())

    def test_artifact_recovery_rejects_reappeared_source(self):
        agent_dir = self.home / ".pi" / "agent"
        artifact = agent_dir / "sessions" / "race" / "subagent-artifacts" / "artifact.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("original\n")
        artifact.chmod(0o600)
        expected = secretary._cleanup_lstat(artifact, directory=False)
        assert expected is not None
        expected_identity = secretary._cleanup_artifact_identity(expected)
        parent_identity = secretary._cleanup_inode(artifact.parent, directory=True)
        plan_hash = "a" * 64
        quarantine = secretary._artifact_quarantine_path(artifact, plan_hash)
        quarantine.write_text("original\n")
        quarantine.chmod(0o600)
        with self.assertRaisesRegex(secretary.SecretaryError, "source reappeared"):
            secretary._quarantine_delete_artifact(
                artifact, __import__("hashlib").sha256(b"original\n").hexdigest(),
                expected_identity, parent_identity, plan_hash,
                owned_quarantine=secretary._cleanup_artifact_identity(quarantine.lstat()),
            )
        self.assertTrue(artifact.exists())
        self.assertTrue(quarantine.exists())

    def test_fast_forward_landing_integration_escalation_and_guarded_cleanup(self):
        registered = secretary.register_project(self.source, "landing-project")
        import hashlib
        hooks = Path(git(self.source, "rev-parse", "--git-path", "hooks"))
        if not hooks.is_absolute():
            hooks = self.source / hooks
        checkout_marker = Path(self.tmp.name) / "post-checkout-ran"
        merge_marker = Path(self.tmp.name) / "post-merge-ran"
        for name, marker in (("post-checkout", checkout_marker), ("post-merge", merge_marker)):
            hook = hooks / name
            hook.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('ran\\n')\n"
            )
            hook.chmod(0o700)
        def accepted(title, workstream_id):
            brief = self._brief(title, "landing brief")
            workstream = self._workstream(brief["briefId"], title=title, workstream_id=workstream_id)
            feature = Path(workstream["workspace"])
            (feature / "tracked").write_text(title + "\n")
            git(feature, "add", "tracked"); git(feature, "commit", "-m", title)
            route_cap = "route-" + workstream_id
            route = Path(self.tmp.name) / f"{workstream_id}.route.json"
            route.write_text(json.dumps({"uid": os.getuid(), "capabilityHash": hashlib.sha256(route_cap.encode()).hexdigest(),
                                         "readOnly": False, "worktree": workstream["workspace"]})); route.chmod(0o600)
            with mock.patch.dict(os.environ, {"PI_TASK_ROUTE_FILE": str(route), "PI_TASK_ROUTE_CAPABILITY": route_cap}, clear=False):
                event = secretary.append_event(registered["projectId"], workstream_id, "review-requested", "Ready")
            request_id = json.loads(event["details"])["reviewRequestId"]
            fake = Path(self.tmp.name) / f"{workstream_id}.pidev"
            fake.write_text("#!/bin/sh\nexit 0\n"); fake.chmod(0o700)
            with mock.patch.object(secretary, "_pidev_path", return_value=fake), \
                 mock.patch.object(secretary, "_wait_for_reviewer_process", return_value=True), \
                 mock.patch.dict(os.environ, {"PI_SECRETARY_CAPABILITY": self.capability}, clear=False):
                request = secretary.create_reviewer(registered["projectId"], event["eventId"])
            with mock.patch.dict(os.environ, {"PI_REVIEW_CAPABILITY": self.capability,
                                              "PI_REVIEW_SESSION_ID": request["reviewerSessionId"]}, clear=False):
                secretary.submit_review(registered["projectId"], request_id, "accept", "Accepted", "")
            return workstream, request_id, request

        workstream, request_id, request = accepted("landable", "landable-work")
        secretary_env = {"PI_SECRETARY_CAPABILITY": self.capability}
        with mock.patch.dict(os.environ, secretary_env, clear=False), \
             mock.patch.object(secretary, "_record_landing", side_effect=secretary.SecretaryError("simulated fact interruption")):
            with self.assertRaisesRegex(secretary.SecretaryError, "fact interruption"):
                secretary.land_reviewed(registered["projectId"], request_id)
        with mock.patch.dict(os.environ, secretary_env, clear=False):
            landed = secretary.land_reviewed(registered["projectId"], request_id)
            landed_again = secretary.land_reviewed(registered["projectId"], request_id)
        self.assertTrue(landed["landed"]); self.assertTrue(landed_again["landed"])
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), request["candidateOid"])
        self.assertFalse(checkout_marker.exists())
        self.assertFalse(merge_marker.exists())
        feature = Path(workstream["workspace"])
        with mock.patch.dict(os.environ, secretary_env, clear=False), \
             mock.patch.object(secretary, "_tmux_window_live", side_effect=secretary.SecretaryError("tmux uncertain")):
            with self.assertRaisesRegex(secretary.SecretaryError, "tmux uncertain"):
                secretary.cleanup_workstream(registered["projectId"], workstream["workstreamId"])
        self.assertTrue(feature.exists())
        self.assertEqual(git(feature, "rev-parse", "HEAD"), request["candidateOid"])
        with mock.patch.dict(os.environ, secretary_env, clear=False), \
             mock.patch.object(secretary, "_tmux_window_live", return_value=True):
            with self.assertRaisesRegex(secretary.SecretaryError, "live"):
                secretary.cleanup_workstream(registered["projectId"], workstream["workstreamId"])
        (feature / "dirty").write_text("preserve")
        with mock.patch.dict(os.environ, secretary_env, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "dirty"):
                secretary.cleanup_workstream(registered["projectId"], workstream["workstreamId"])
        self.assertTrue((feature / "dirty").exists())
        (feature / "dirty").unlink()
        record_path = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / registered["projectId"] / "workstreams" / f"{workstream['workstreamId']}.json"
        real_atomic = secretary._atomic
        def interrupted_atomic(path, content):
            if Path(path) == record_path:
                raise secretary.SecretaryError("simulated cleanup interruption")
            return real_atomic(path, content)
        with mock.patch.object(secretary, "_cleanup_worktree_has_live_process", return_value=False), \
             mock.patch.dict(os.environ, secretary_env, clear=False), \
             mock.patch.object(secretary, "_tmux_window_live", return_value=False), \
             mock.patch.object(secretary, "_atomic", side_effect=interrupted_atomic):
            with self.assertRaisesRegex(secretary.SecretaryError, "cleanup interruption"):
                secretary.cleanup_workstream(registered["projectId"], workstream["workstreamId"])
        self.assertFalse(feature.exists())
        with mock.patch.object(secretary, "_cleanup_worktree_has_live_process", return_value=False), \
             mock.patch.dict(os.environ, secretary_env, clear=False), \
             mock.patch.object(secretary, "_tmux_window_live", return_value=False):
            cleaned = secretary.cleanup_workstream(registered["projectId"], workstream["workstreamId"])
        self.assertFalse(feature.exists())
        self.assertFalse(Path(request["reviewWorkspace"]).exists())
        self.assertIsNotNone(cleaned["closedAt"])
        self.assertTrue((Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / registered["projectId"] / "reviews/receipts").exists())

        moved, moved_request_id, _ = accepted("needs integration", "integration-source")
        (self.source / "target-dirty").write_text("preserve\n")
        with mock.patch.dict(os.environ, secretary_env, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "target worktree is dirty"):
                secretary.land_reviewed(registered["projectId"], moved_request_id)
        self.assertTrue((self.source / "target-dirty").exists())
        (self.source / "target-dirty").unlink()
        (self.source / "target-only").write_text("advance\n")
        git(self.source, "add", "target-only"); git(self.source, "commit", "-m", "advance target")
        with mock.patch.dict(os.environ, secretary_env, clear=False):
            outcome = secretary.land_reviewed(registered["projectId"], moved_request_id)
        self.assertTrue(outcome["requiresIntegration"])
        self.assertEqual(outcome["reason"], "target-moved")
        fake = Path(self.tmp.name) / "integration-pidev"
        fake.write_text("#!/bin/sh\nexit 0\n"); fake.chmod(0o700)
        with mock.patch.object(secretary, "_pidev_path", return_value=fake), \
             mock.patch.object(secretary, "_wait_for_managed_process", return_value=True), \
             mock.patch.dict(os.environ, secretary_env, clear=False):
            integration = secretary.create_integration(registered["projectId"], moved_request_id)
        self.assertEqual(integration["role"], "integration")
        self.assertNotEqual(integration["workstreamId"], moved["workstreamId"])
        self.assertEqual(git(Path(integration["workspace"]), "rev-parse", "HEAD"), git(self.source, "rev-parse", "HEAD"))
        with mock.patch.dict(os.environ, secretary_env, clear=False):
            with self.assertRaisesRegex(secretary.SecretaryError, "unlanded"):
                secretary.cleanup_workstream(registered["projectId"], integration["workstreamId"])

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

    def test_list_tolerates_safely_removed_worktree_but_open_stays_strict(self):
        brief = self._brief("Removed worktree", "inventory remains available")
        ws = self._workstream(brief["briefId"], "Removed", workstream_id="removed-worktree")
        workspace = Path(ws["workspace"])
        git(self.source, "worktree", "remove", str(workspace))
        git(self.source, "branch", "-D", ws["branch"])

        listing = secretary.list_workstreams(self.source, self.capability)
        removed = next(item for item in listing if item["workstreamId"] == ws["workstreamId"])
        self.assertIsNone(removed["currentOid"])
        self.assertFalse(workspace.exists())
        with self.assertRaisesRegex(secretary.SecretaryError, "workspace is unavailable"):
            secretary.open_workstream(self.source, self.capability, ws["workstreamId"])

    def test_list_rejects_missing_worktree_with_stale_git_registration(self):
        brief = self._brief("Registered missing worktree", "fail closed")
        ws = self._workstream(brief["briefId"], "Registered", workstream_id="registered-missing")
        workspace = Path(ws["workspace"])
        shutil.rmtree(workspace)

        with self.assertRaisesRegex(secretary.SecretaryError, "still has a Git registration"):
            secretary.list_workstreams(self.source, self.capability)

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
        registered = secretary.register_project(sha_root, "sha256-review")
        route_cap = "sha256-route"
        import hashlib
        route = Path(self.tmp.name) / "sha256-route.json"
        route.write_text(json.dumps({"uid": os.getuid(), "capabilityHash": hashlib.sha256(route_cap.encode()).hexdigest(),
                                     "readOnly": False, "worktree": ws["workspace"]})); route.chmod(0o600)
        with mock.patch.dict(os.environ, {"PI_TASK_ROUTE_FILE": str(route), "PI_TASK_ROUTE_CAPABILITY": route_cap}, clear=False):
            event = secretary.append_event(registered["projectId"], ws["workstreamId"], "review-requested", "SHA review")
        request_id = json.loads(event["details"])["reviewRequestId"]
        project = Path(self.env["XDG_STATE_HOME"]) / "pi-secretary/projects" / registered["projectId"]
        request = secretary._validate_review_request(secretary._review_request_path(project, request_id), registered["projectId"])
        self.assertEqual({len(request[name]) for name in ("candidateOid", "candidateTree", "baseOid")}, {64})


if __name__ == "__main__":
    unittest.main()
