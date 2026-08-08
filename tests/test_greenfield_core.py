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


if __name__ == "__main__":
    unittest.main()
