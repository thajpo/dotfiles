"""Focused production-path Docker runtime, isolation, and writer fencing tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from scripts.pi_control.conversations import create_conversation
from scripts.pi_control.command_requests import approve_command, execute_approved_command, request_command
from scripts.pi_control.docker_runtime import (
    PINNED_ACCEPTANCE_IMAGE, DockerRuntimeError, cleanup_run_container, create_start_container,
    execute_file_tool, execute_shell_tool, inspect_container, prepare_tool_runtime,
)
from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.launch import attest_run, fail_run, prepare_run, stop_run
from scripts.pi_control.models import new_id, utc_now
from scripts.pi_control.run_manifest import executable_sha256


class DockerRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("docker") is None or subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            raise unittest.SkipTest("STOP/77: Docker daemon is unavailable")
        if subprocess.run(["docker", "image", "inspect", PINNED_ACCEPTANCE_IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            raise unittest.SkipTest("STOP/77: exact local acceptance image is unavailable")

    def fixture(self, root: Path):
        source = root / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
        (source / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
        repository = root / "repo"
        subprocess.run(["git", "-C", str(source), "worktree", "add", "-q", "-b", "tool-fixture", str(repository)], check=True)
        client = PiControllerClient(root / "state")
        project = client.register_project(str(repository))
        store = PiStore(root / "state").open()
        working = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary'", (project["project_id"],)).fetchone()
        conversation = create_conversation(store, project_id=project["project_id"], role="personal", display_name="personal", working_copy_id=working["working_copy_id"])
        store.conn.execute("UPDATE conversations SET observed_state='ready' WHERE conversation_id=?", (conversation["conversation_id"],))
        build_id = "build_" + "b" * 32
        manifest = root / "build-manifest.json"
        inventory = root / "release-resources.json"
        manifest.write_text("fixture", encoding="utf-8")
        inventory.write_text("fixture", encoding="utf-8")
        digest = "sha256:" + "a" * 64
        store.conn.execute("INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,build_manifest_path,build_manifest_digest,resource_manifest_path,resource_manifest_digest,pi_version,package_lock_hash,status,installed_at,activated_at,rollback_path,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (build_id, None, digest, str(manifest), digest, str(inventory), "sha256:" + "c" * 64, "0.83.0", digest, "staged", utc_now(), None, None, "{}"))
        return store, dict(project), dict(working), conversation, build_id, repository

    @staticmethod
    def verify(store, build_id):
        return store.conn.execute("SELECT * FROM installed_builds WHERE build_id=?", (build_id,)).fetchone()

    def prepare(self, store, project, working, conversation, build_id):
        run_id = new_id("run")
        tool = prepare_tool_runtime(state_root=store.state_root, run_id=run_id, image_reference=PINNED_ACCEPTANCE_IMAGE, project=project, working_copy=working, build_id=build_id, writer_epoch=int(working["writer_epoch"]) + 1)
        python = Path(os.sys.executable).resolve(strict=True)
        host = {"executable": str(python), "executableSha256": executable_sha256(python), "argv": [str(python)], "toolProfile": "personal", "environmentKeys": ["PI_RUNTIME_MANIFEST"]}
        with mock.patch("scripts.pi_control.launch.verify_registered_build", side_effect=self.verify):
            return prepare_run(store, conversation_id=conversation["conversation_id"], build_id=build_id, host_process=host, tool_runtime=tool, run_id=run_id)

    def test_runtime_tools_isolation_and_exact_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store, project, working, conversation, build_id, repository = self.fixture(Path(raw))
            prepared = self.prepare(store, project, working, conversation, build_id)
            try:
                started = create_start_container(store, run_id=prepared.run["run_id"], manifest=prepared.manifest)
                attest_run(store, run_id=prepared.run["run_id"], manifest_digest=prepared.manifest["manifestDigest"])
                container_id = started["containerId"]
                self.assertEqual(execute_file_tool(container_id, "read", {"path": "tracked.txt"})["lines"], ["base"])
                execute_file_tool(container_id, "edit", {"path": "tracked.txt", "oldText": "base", "newText": "edited"})
                execute_file_tool(container_id, "write", {"path": "created.txt", "content": "created\n"})
                self.assertEqual(execute_shell_tool(container_id, {"argv": ["python3", "-c", "print(open('created.txt').read().strip())"]})["stdout"], "created\n")
                for request in ({"argv": ["python3", "-c", "open('/etc/forbidden','w').write('x')"]}, {"command": "test ! -e /var/run/docker.sock && test ! -e /root/.ssh && test -f .git && test ! -s .git"}, {"argv": ["python3", "-c", "import socket;s=socket.socket();s.settimeout(.2);s.connect(('1.1.1.1',53))"]}):
                    result = execute_shell_tool(container_id, request)
                    if request.get("command"):
                        self.assertEqual(result["exitCode"], 0, result)
                    else:
                        self.assertNotEqual(result["exitCode"], 0, result)
                with self.assertRaises(DockerRuntimeError):
                    execute_file_tool(container_id, "read", {"path": "../outside"})
                observation = inspect_container(container_id)
                self.assertNotEqual(observation["pid"], os.getpid())
                self.assertEqual(observation["networkMode"], "none")
                self.assertEqual(observation["capDrop"], ["ALL"])
                cleanup = cleanup_run_container(store, run_id=prepared.run["run_id"])
                self.assertTrue(cleanup["absent"], cleanup)
                stop_run(store, run_id=prepared.run["run_id"], reason="test", container_absent=True)
                self.assertEqual((repository / "tracked.txt").read_text(), "edited\n")
                self.assertEqual((repository / "created.txt").read_text(), "created\n")
            finally:
                prepared.close()
                store.close()

    def test_concurrent_second_writer_is_rejected_by_lock_and_database(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store, project, working, conversation, build_id, _repository = self.fixture(Path(raw))
            prepared = self.prepare(store, project, working, conversation, build_id)
            second_store = PiStore(store.state_root).open()
            try:
                current = second_store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (working["working_copy_id"],)).fetchone()
                with self.assertRaises(Exception):
                    self.prepare(second_store, project, dict(current), conversation, build_id)
                live = second_store.conn.execute("SELECT COUNT(*) FROM runs WHERE working_copy_id=? AND observed_state IN ('preparing','ready','running','stopping','needs_attention')", (working["working_copy_id"],)).fetchone()[0]
                self.assertEqual(live, 1)
                fail_run(store, run_id=prepared.run["run_id"], code="TEST", detail="no container created", release_writer=True)
            finally:
                prepared.close()
                second_store.close()
                store.close()

    def test_nested_git_is_rejected_before_container_intent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store, project, working, _conversation, build_id, repository = self.fixture(Path(raw))
            try:
                nested = repository / "nested/.git"
                nested.mkdir(parents=True)
                with self.assertRaisesRegex(DockerRuntimeError, "nested Git"):
                    prepare_tool_runtime(state_root=store.state_root, run_id=new_id("run"), image_reference=PINNED_ACCEPTANCE_IMAGE, project=project, working_copy=working, build_id=build_id, writer_epoch=1)
            finally:
                store.close()

    def test_approved_one_shot_network_namespace_is_separate_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store, project, working, conversation, build_id, _repository = self.fixture(Path(raw))
            prepared = self.prepare(store, project, working, conversation, build_id)
            try:
                request = request_command(store, project_id=project["project_id"], conversation_id=conversation["conversation_id"], run_id=prepared.run["run_id"], writer_generation=prepared.run["writer_epoch"], operation="network.namespace-probe", purpose="prove bridge namespace without contacting it")
                approve_command(store, command_request_id=request["command_request_id"], request_digest=request["request_digest"])
                completed = execute_approved_command(store, command_request_id=request["command_request_id"], request_digest=request["request_digest"])
                self.assertEqual(completed["state"], "succeeded", completed["result_json"])
                result = json.loads(completed["result_json"])
                self.assertEqual(result["networkMode"], "bridge")
                self.assertFalse(result["networkContacted"])
                self.assertIn("NETWORK_NAMESPACE_OK", result["stdout"])
                self.assertEqual(result["mountMode"], "read-only")
                self.assertEqual(result["cleanup"]["absentById"], True)
                self.assertEqual(result["cleanup"]["absentByName"], True)
                leaked = "OPENAI_API_KEY=must-not-leak"
                self.assertNotIn(leaked, result["stdout"] + result["stderr"])
                query = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=pi.control.request-id={request['command_request_id']}"], stdout=subprocess.PIPE, text=True, check=True)
                self.assertEqual(query.stdout.strip(), "")
            finally:
                prepared.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
