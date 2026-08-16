from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import json

from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.messages import ProjectMessageError, acknowledge_message, list_messages, post_message
from scripts.pi_control.pi_reconcile import ReconcileError
from scripts.pi_control.launch import prepare_run
from scripts.pi_control.command_requests import CommandRequestError, approve_command, execute_approved_command, request_command
from scripts.pi_control.models import canonical_json, new_id, utc_now
from scripts.pi_control.run_manifest import executable_sha256
from tests.pi_test_build import allow_test_only_registered_build_rows
from tests.control_plane.test_p2_contract import tool_runtime


def _repo(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid", GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid")
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    (path / "README").write_text(name + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True, env=env)
    return path


_BUILD_ID = "build_" + "b" * 32
_DIGEST = "sha256:" + "a" * 64


def _register_build(store: PiStore, root: Path) -> None:
    build = root / "build-manifest.json"
    resources = root / "release-resources.json"
    build.write_text("test", encoding="utf-8")
    resources.write_text("test", encoding="utf-8")
    store.conn.execute("INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,build_manifest_path,build_manifest_digest,resource_manifest_path,resource_manifest_digest,pi_version,package_lock_hash,status,installed_at,activated_at,rollback_path,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (_BUILD_ID, None, _DIGEST, str(build), _DIGEST, str(resources), "sha256:" + "c" * 64, "0.83.0", _DIGEST, "staged", utc_now(), None, None, canonical_json({"verified": True})))


def _host(role: str = "secretary") -> dict[str, object]:
    executable = Path("/usr/bin/true").resolve(strict=True)
    return {"executable": str(executable), "executableSha256": executable_sha256(executable), "argv": [str(executable)], "toolProfile": role, "environmentKeys": ["PATH"]}


class PiCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fresh_schema_messages_and_old_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sentinel = root / "old-pi-state"
            sentinel.write_bytes(b"must-not-be-read-or-changed")
            project = _repo(root, "one")
            client = PiControllerClient(root / "state")
            registered = client.register_project(str(project), "one")
            project_id = registered["project_id"]
            with PiStore(root / "state") as store:
                _register_build(store, root)
                tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn("migration_runs", tables)
                self.assertNotIn("project_activations", tables)
                self.assertIn("project_messages", tables)
                conversation = store.conn.execute("SELECT * FROM conversations WHERE project_id=?", (project_id,)).fetchone()
                prepared = client.prepare_run(conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host())
                self.assertNotIn("PI_RUNTIME_CAPABILITY", prepared["environment"])
                client.attest_run(run_id=prepared["run"]["run_id"], manifest_digest=prepared["manifest"]["manifestDigest"])
                first = post_message(store, project_id=project_id, conversation_id=conversation["conversation_id"], run_id=prepared["run"]["run_id"], kind="needs-user", payload={"question": "continue?"}, idempotency_key="same")
                replay = post_message(store, project_id=project_id, conversation_id=conversation["conversation_id"], run_id=prepared["run"]["run_id"], kind="needs-user", payload={"question": "continue?"}, idempotency_key="same")
                self.assertEqual(first["message_id"], replay["message_id"])
                self.assertEqual(len(list_messages(store, project_id=project_id)), 1)
                acknowledge_message(store, project_id=project_id, message_id=first["message_id"])
            self.assertEqual(sentinel.read_bytes(), b"must-not-be-read-or-changed")

    def test_cross_project_reply_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            first = client.register_project(str(_repo(root, "one")), "one")
            second = client.register_project(str(_repo(root, "two")), "two")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                c1 = store.conn.execute("SELECT * FROM conversations WHERE project_id=?", (first["project_id"],)).fetchone()
                r1 = client.prepare_run(conversation_id=c1["conversation_id"], build_id=_BUILD_ID, host_process=_host())
                client.attest_run(run_id=r1["run"]["run_id"], manifest_digest=r1["manifest"]["manifestDigest"])
                message = post_message(store, project_id=first["project_id"], conversation_id=c1["conversation_id"], run_id=r1["run"]["run_id"], kind="needs-user", payload={"q": "x"}, idempotency_key="one")
                c2 = store.conn.execute("SELECT * FROM conversations WHERE project_id=?", (second["project_id"],)).fetchone()
                r2 = client.prepare_run(conversation_id=c2["conversation_id"], build_id=_BUILD_ID, host_process=_host())
                client.attest_run(run_id=r2["run"]["run_id"], manifest_digest=r2["manifest"]["manifestDigest"])
                with self.assertRaises(ProjectMessageError):
                    post_message(store, project_id=second["project_id"], conversation_id=c2["conversation_id"], run_id=r2["run"]["run_id"], kind="decision-reply", payload={"a": 1}, idempotency_key="two", reply_to_message_id=message["message_id"])

    def test_reconcile_fails_closed_until_explicit_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = PiControllerClient(root / "state")
            registered = client.register_project(str(_repo(root, "recovery")), "recovery")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                conversation = store.conn.execute("SELECT * FROM conversations WHERE project_id=?", (registered["project_id"],)).fetchone()
                prepared = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host())
                store.conn.execute("UPDATE runs SET owner_pid=?,owner_start_identity=?,observed_state='running' WHERE run_id=?", (999999, "linux:missing:1", prepared.run["run_id"]))
            attention = client.reconcile_run(run_id=prepared.run["run_id"])
            self.assertEqual(attention["decision"], "needs-attention")
            recovered = client.recover_run(run_id=prepared.run["run_id"], actor_id="test-recovery")
            self.assertEqual(recovered["observed_state"], "lost")

    def test_change_revision_is_immutable_and_increments_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = _repo(root, "revision")
            client = PiControllerClient(root / "state")
            registered = client.register_project(str(repository), "revision")
            with PiStore(root / "state") as store:
                primary = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary'", (registered["project_id"],)).fetchone()
            (repository / "README").write_text("first revision\n", encoding="utf-8")
            first = client.submit_change(project_id=registered["project_id"], working_copy_id=primary["working_copy_id"], target_ref=primary["branch_ref"], title="feature", summary="first", capture_mode="dirty", selected_paths=["README"], idempotency_key="revision-1")
            (repository / "README").write_text("second revision\n", encoding="utf-8")
            second = client.submit_change_revision(change_id=first["changeId"], title="feature", summary="second", capture_mode="dirty", selected_paths=["README"], idempotency_key="revision-2")
            self.assertEqual(first["revision"], 1)
            self.assertEqual(second["revision"], 2)
            with PiStore(root / "state") as store:
                rows = store.conn.execute("SELECT revision,ref_name FROM change_revisions WHERE change_id=? ORDER BY revision", (first["changeId"],)).fetchall()
                self.assertEqual([int(row[0]) for row in rows], [1, 2])

    def test_approved_host_command_is_bounded_and_one_use(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = _repo(root, "commands")
            client = PiControllerClient(root / "state")
            registered = client.register_project(str(repository), "commands")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                primary = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary'", (registered["project_id"],)).fetchone()
                conversation = client.create_conversation(project_id=registered["project_id"], role="personal", display_name="command worker", working_copy_id=primary["working_copy_id"])
                run_id = new_id("run")
                prepared = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host("personal"), tool_runtime=tool_runtime(run_id, dict(registered), dict(primary)), run_id=run_id)
                request = request_command(store, project_id=registered["project_id"], conversation_id=conversation["conversation_id"], run_id=prepared.run["run_id"], writer_generation=prepared.run["writer_epoch"], operation="host.fixture-success", purpose="run one approved deterministic no-op")
                approve_command(store, command_request_id=request["command_request_id"], request_digest=request["request_digest"], actor_id="test-user")
                result = execute_approved_command(store, command_request_id=request["command_request_id"], request_digest=request["request_digest"])
                self.assertEqual(result["state"], "succeeded")
                self.assertFalse((repository / "approved-command-output").exists())
                with self.assertRaises(CommandRequestError):
                    execute_approved_command(store, command_request_id=request["command_request_id"], request_digest=request["request_digest"])
                prepared.close()


if __name__ == "__main__":
    unittest.main()
