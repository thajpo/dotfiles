from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import json

from scripts.pi_control.greenfield_client import GreenfieldControllerClient
from scripts.pi_control.greenfield_store import GreenfieldStore
from scripts.pi_control.messages import ProjectMessageError, acknowledge_message, list_messages, post_message
from scripts.pi_control.greenfield_reconcile import ReconcileError
from scripts.pi_control.launch import prepare_run
from scripts.pi_control.command_requests import CommandRequestError, authorize_command, execute_command, request_command


def _repo(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid", GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid")
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    (path / "README").write_text(name + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True, env=env)
    return path


class GreenfieldCoreTests(unittest.TestCase):
    def test_fresh_schema_messages_and_old_sentinel(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as raw:
            root = Path(raw)
            sentinel = root / "old-pi-state"
            sentinel.write_bytes(b"must-not-be-read-or-changed")
            project = _repo(root, "one")
            client = GreenfieldControllerClient(root / "state")
            registered = client.register_project(str(project), "one")
            project_id = registered["project_id"]
            with GreenfieldStore(root / "state") as store:
                tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn("migration_runs", tables)
                self.assertNotIn("project_activations", tables)
                self.assertIn("project_messages", tables)
                conversation = store.conn.execute("SELECT * FROM conversations WHERE project_id=?", (project_id,)).fetchone()
                prepared = client.prepare_run(project_id=project_id, conversation_id=conversation["conversation_id"], authority="secretary")
                self.assertEqual(prepared["environment"]["PI_RUNTIME_CAPABILITY"], "<controller-issued>")
                client.attest_run(run_id=prepared["run"]["run_id"], manifest_digest=prepared["manifest"]["manifestDigest"])
                first = post_message(store, project_id=project_id, conversation_id=conversation["conversation_id"], run_id=prepared["run"]["run_id"], kind="needs-user", payload={"question": "continue?"}, idempotency_key="same")
                replay = post_message(store, project_id=project_id, conversation_id=conversation["conversation_id"], run_id=prepared["run"]["run_id"], kind="needs-user", payload={"question": "continue?"}, idempotency_key="same")
                self.assertEqual(first["message_id"], replay["message_id"])
                self.assertEqual(len(list_messages(store, project_id=project_id)), 1)
                acknowledge_message(store, project_id=project_id, message_id=first["message_id"])
            self.assertEqual(sentinel.read_bytes(), b"must-not-be-read-or-changed")

    def test_cross_project_reply_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as raw:
            root = Path(raw)
            client = GreenfieldControllerClient(root / "state")
            first = client.register_project(str(_repo(root, "one")), "one")
            second = client.register_project(str(_repo(root, "two")), "two")
            with GreenfieldStore(root / "state") as store:
                c1 = store.conn.execute("SELECT * FROM conversations WHERE project_id=?", (first["project_id"],)).fetchone()
                r1 = client.prepare_run(project_id=first["project_id"], conversation_id=c1["conversation_id"], authority="secretary")
                client.attest_run(run_id=r1["run"]["run_id"], manifest_digest=r1["manifest"]["manifestDigest"])
                message = post_message(store, project_id=first["project_id"], conversation_id=c1["conversation_id"], run_id=r1["run"]["run_id"], kind="needs-user", payload={"q": "x"}, idempotency_key="one")
                c2 = store.conn.execute("SELECT * FROM conversations WHERE project_id=?", (second["project_id"],)).fetchone()
                r2 = client.prepare_run(project_id=second["project_id"], conversation_id=c2["conversation_id"], authority="secretary")
                client.attest_run(run_id=r2["run"]["run_id"], manifest_digest=r2["manifest"]["manifestDigest"])
                with self.assertRaises(ProjectMessageError):
                    post_message(store, project_id=second["project_id"], conversation_id=c2["conversation_id"], run_id=r2["run"]["run_id"], kind="decision-reply", payload={"a": 1}, idempotency_key="two", reply_to_message_id=message["message_id"])

    def test_process_launcher_holds_and_releases_run(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as raw:
            root = Path(raw)
            client = GreenfieldControllerClient(root / "state")
            registered = client.register_project(str(_repo(root, "launcher")), "launcher")
            with GreenfieldStore(root / "state") as store:
                conversation = store.conn.execute("SELECT * FROM conversations WHERE project_id=?", (registered["project_id"],)).fetchone()
            launcher = Path(__file__).resolve().parents[1] / "bin" / "pi-system-run"
            code = "import json,os; print(json.dumps({'manifest':os.environ['PI_RUNTIME_MANIFEST'],'run':os.environ['PI_SYSTEM_RUN_ID']}))"
            result = subprocess.run([str(launcher), "--state-root", str(root / "state"), "--project-id", registered["project_id"], "--conversation-id", conversation["conversation_id"], "--authority", "secretary", "--", "python3", "-c", code], cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout.strip())
            with GreenfieldStore(root / "state") as store:
                run = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (output["run"],)).fetchone()
                self.assertEqual(run["observed_state"], "stopped")
                self.assertEqual(run["desired_state"], "stopped")

    def test_reconcile_fails_closed_until_explicit_recovery(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as raw:
            root = Path(raw)
            client = GreenfieldControllerClient(root / "state")
            registered = client.register_project(str(_repo(root, "recovery")), "recovery")
            with GreenfieldStore(root / "state") as store:
                conversation = store.conn.execute("SELECT * FROM conversations WHERE project_id=?", (registered["project_id"],)).fetchone()
                prepared = prepare_run(store, project_id=registered["project_id"], conversation_id=conversation["conversation_id"], authority="secretary")
                store.conn.execute("UPDATE runs SET owner_pid=?,owner_start_identity=?,observed_state='running' WHERE run_id=?", (999999, "linux:missing:1", prepared.run["run_id"]))
            attention = client.reconcile_run(run_id=prepared.run["run_id"])
            self.assertEqual(attention["decision"], "needs-attention")
            recovered = client.recover_run(run_id=prepared.run["run_id"], actor_id="test-recovery")
            self.assertEqual(recovered["observed_state"], "lost")

    def test_change_revision_is_immutable_and_increments_exactly(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as raw:
            root = Path(raw)
            repository = _repo(root, "revision")
            client = GreenfieldControllerClient(root / "state")
            registered = client.register_project(str(repository), "revision")
            with GreenfieldStore(root / "state") as store:
                primary = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary'", (registered["project_id"],)).fetchone()
            (repository / "README").write_text("first revision\n", encoding="utf-8")
            first = client.submit_change(project_id=registered["project_id"], working_copy_id=primary["working_copy_id"], target_ref=primary["branch_ref"], title="feature", summary="first", capture_mode="dirty", selected_paths=["README"], idempotency_key="revision-1")
            (repository / "README").write_text("second revision\n", encoding="utf-8")
            second = client.submit_change_revision(change_id=first["changeId"], title="feature", summary="second", capture_mode="dirty", selected_paths=["README"], idempotency_key="revision-2")
            self.assertEqual(first["revision"], 1)
            self.assertEqual(second["revision"], 2)
            with GreenfieldStore(root / "state") as store:
                rows = store.conn.execute("SELECT revision,ref_name FROM change_revisions WHERE change_id=? ORDER BY revision", (first["changeId"],)).fetchall()
                self.assertEqual([int(row[0]) for row in rows], [1, 2])

    def test_approved_host_command_is_bounded_and_one_use(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as raw:
            root = Path(raw)
            repository = _repo(root, "commands")
            client = GreenfieldControllerClient(root / "state")
            registered = client.register_project(str(repository), "commands")
            with GreenfieldStore(root / "state") as store:
                primary = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary'", (registered["project_id"],)).fetchone()
                conversation = client.create_conversation(project_id=registered["project_id"], role="personal", display_name="command worker", pi_session_id="pi-command-worker", working_copy_id=primary["working_copy_id"])
                prepared = prepare_run(store, project_id=registered["project_id"], conversation_id=conversation["conversation_id"], working_copy_id=primary["working_copy_id"], authority="writer", runtime={"imageReference": "pi-test:local", "imageConfigId": "sha256:" + "a" * 64, "platform": "linux/amd64"})
                request = request_command(store, project_id=registered["project_id"], conversation_id=conversation["conversation_id"], run_id=prepared.run["run_id"], execution_place="host", command=["python3", "-c", "open('approved-command-output','w').write('ok')"], working_directory=str(repository), required_resource="project working copy", purpose="write an explicitly approved fixture", expected_effect="one bounded fixture file", change_scope={"paths": ["approved-command-output"]})
                authorize_command(store, project_id=registered["project_id"], command_request_id=request["command_request_id"], request_digest=request["request_digest"], actor_id="test-user")
                result = execute_command(store, project_id=registered["project_id"], command_request_id=request["command_request_id"], request_digest=request["request_digest"])
                self.assertEqual(result["state"], "succeeded")
                self.assertEqual((repository / "approved-command-output").read_text(encoding="utf-8"), "ok")
                with self.assertRaises(CommandRequestError):
                    execute_command(store, project_id=registered["project_id"], command_request_id=request["command_request_id"], request_digest=request["request_digest"])
                prepared.close()


if __name__ == "__main__":
    unittest.main()
