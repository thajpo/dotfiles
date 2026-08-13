"""Personal writer on the registered primary checkout (directory-form .git)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from scripts.pi_control.conversations import create_conversation
from scripts.pi_control.docker_runtime import (
    PINNED_ACCEPTANCE_IMAGE, DockerRuntimeError, prepare_tool_runtime,
)
from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.launch import prepare_run
from scripts.pi_control.models import new_id, utc_now
from scripts.pi_control.run_manifest import executable_sha256


class PersonalPrimaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("docker") is None or subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            raise unittest.SkipTest("STOP/77: Docker daemon is unavailable")
        if subprocess.run(["docker", "image", "inspect", PINNED_ACCEPTANCE_IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            raise unittest.SkipTest("STOP/77: exact local acceptance image is unavailable")

    def fixture(self, root: Path):
        # A real repository with directory-form .git registered directly.
        source = root / "repo"
        source.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
        (source / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
        client = PiControllerClient(root / "state")
        project = client.register_project(str(source))
        store = PiStore(root / "state").open()
        working = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary'", (project["project_id"],)).fetchone()
        self.assertEqual(Path(working["path"]).resolve(), source.resolve())
        conversation = create_conversation(store, project_id=project["project_id"], role="personal", display_name="personal", working_copy_id=working["working_copy_id"])
        store.conn.execute("UPDATE conversations SET observed_state='ready' WHERE conversation_id=?", (conversation["conversation_id"],))
        build_id = "build_" + "b" * 32
        manifest = root / "build-manifest.json"
        inventory = root / "release-resources.json"
        manifest.write_text("fixture", encoding="utf-8")
        inventory.write_text("fixture", encoding="utf-8")
        digest = "sha256:" + "a" * 64
        store.conn.execute("INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,build_manifest_path,build_manifest_digest,resource_manifest_path,resource_manifest_digest,pi_version,package_lock_hash,status,installed_at,activated_at,rollback_path,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (build_id, None, digest, str(manifest), digest, str(inventory), "sha256:" + "c" * 64, "0.83.0", digest, "staged", utc_now(), None, None, "{}"))
        return store, dict(project), dict(working), conversation, build_id, source

    def test_personal_primary_prepares_writer_runtime_with_git_mask(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, project, working, conversation, build_id, source = self.fixture(root)
            try:
                run_id = new_id("run")
                tool = prepare_tool_runtime(state_root=store.state_root, run_id=run_id, image_reference=PINNED_ACCEPTANCE_IMAGE, project=project, working_copy=working, build_id=build_id, writer_epoch=int(working["writer_epoch"]) + 1)
                self.assertEqual(tool["mounts"][0]["source"], str(source))
                self.assertEqual(tool["mounts"][1]["target"], "/workspace/.git")
                self.assertTrue(tool["mounts"][1]["readOnly"])
                python = Path(sys.executable).resolve(strict=True)
                host = {"executable": str(python), "executableSha256": executable_sha256(python), "argv": [str(python)], "toolProfile": "personal", "environmentKeys": ["PI_RUNTIME_MANIFEST"]}
                with mock.patch("scripts.pi_control.launch.verify_registered_build", side_effect=lambda store, build_id: store.conn.execute("SELECT * FROM installed_builds WHERE build_id=?", (build_id,)).fetchone()):
                    prepared = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=build_id, host_process=host, tool_runtime=tool, run_id=run_id)
                manifest = prepared.manifest
                self.assertEqual(manifest["workingCopy"]["kind"], "primary")
                self.assertEqual(manifest["workingCopy"]["purpose"], "personal")
                self.assertEqual(manifest["workingCopy"]["hostPath"], str(source))
                self.assertGreaterEqual(manifest["workingCopy"]["writerEpoch"], 1)
                self.assertEqual(manifest["conversation"]["role"], "personal")
                prepared.close()
            finally:
                store.close()

    def test_directory_form_git_on_non_primary_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, project, working, conversation, build_id, source = self.fixture(root)
            try:
                store.conn.execute("UPDATE working_copies SET kind='worktree' WHERE working_copy_id=?", (working["working_copy_id"],))
                row = dict(store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (working["working_copy_id"],)).fetchone())
                with self.assertRaisesRegex(DockerRuntimeError, "only for the registered primary"):
                    prepare_tool_runtime(state_root=store.state_root, run_id=new_id("run"), image_reference=PINNED_ACCEPTANCE_IMAGE, project=project, working_copy=row, build_id=build_id, writer_epoch=1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
