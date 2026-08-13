"""Deterministic offline npm and Python package materialization tests."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.pi_control.dependencies import inventory_dependencies
from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.launch import prepare_run
from scripts.pi_control.models import new_id
from scripts.pi_control.package_environment import PackageEnvironmentError, _load_cache_policy, approve_package_request, execute_approved_package_request, request_package_operation
from tests.control_plane.test_p2_contract import tool_runtime
from tests.pi_test_build import allow_test_only_registered_build_rows
from tests.system.package_cache_fixture import NODE_IMAGE_CONFIG, PYTHON_IMAGE_CONFIG, create_package_caches
from tests.test_pi_core import _BUILD_ID, _host, _register_build, _repo


class P6PackageMaterializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("docker") is None or subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            raise unittest.SkipTest("STOP/77: Docker daemon is unavailable")
        for image in (NODE_IMAGE_CONFIG, PYTHON_IMAGE_CONFIG):
            if subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
                raise unittest.SkipTest("STOP/77: exact local P6 package image is unavailable")

    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _materialize(self, root: Path) -> dict[str, object]:
        repository = _repo(root, "packages")
        client = PiControllerClient(root / "state")
        project = client.register_project(str(repository), "packages")
        cache = create_package_caches(root, root / "state")
        npm_spec = "file:/cache/npm/p6-tiny-npm-1.0.0.tgz"
        (repository / "package.json").write_text(json.dumps({"name": "fixture", "version": "1.0.0", "packageManager": "npm@10.9.8", "dependencies": {"p6-tiny-npm": npm_spec}}), encoding="utf-8")
        (repository / "package-lock.json").write_text(json.dumps({"name": "fixture", "version": "1.0.0", "lockfileVersion": 3, "packages": {"": {"name": "fixture", "version": "1.0.0", "dependencies": {"p6-tiny-npm": npm_spec}}, "node_modules/p6-tiny-npm": {"version": "1.0.0", "resolved": npm_spec, "integrity": cache["npmIntegrity"]}}}), encoding="utf-8")
        (repository / "requirements.txt").write_text(f"p6-tiny-python==1.0.0 --hash=sha256:{cache['pythonSha256']}\n", encoding="utf-8")
        with PiStore(root / "state") as store:
            working = dict(store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (project["project_id"],)).fetchone())
        change = client.submit_change(project_id=project["project_id"], working_copy_id=working["working_copy_id"], target_ref=working["branch_ref"], title="offline packages", summary="npm and Python exact local artifacts", capture_mode="dirty", selected_paths=["package.json", "package-lock.json", "requirements.txt"], idempotency_key="p6-offline-packages")
        with PiStore(root / "state") as store:
            _register_build(store, root)
            inventory = inventory_dependencies(store, project_id=project["project_id"], change_id=change["changeId"], revision=change["revision"])
            self.assertEqual({item["ecosystem"] for item in inventory["differences"]}, {"npm", "python"})
            conversation = client.create_conversation(project_id=project["project_id"], role="personal", display_name="offline packages", working_copy_id=working["working_copy_id"])
            run_id = new_id("run")
            prepared = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host("personal"), tool_runtime=tool_runtime(run_id, project, working), run_id=run_id)
            results: dict[str, object] = {"inventoryDigest": cache["inventoryDigest"]}
            try:
                for ecosystem, package_name in (("npm", "p6-tiny-npm"), ("python", "p6-tiny-python")):
                    request = request_package_operation(store, project_id=project["project_id"], conversation_id=conversation["conversation_id"], run_id=run_id, writer_generation=prepared.run["writer_epoch"], change_id=change["changeId"], revision=change["revision"], ecosystem=ecosystem, action="add", package_name=package_name, exact_version="1.0.0")
                    approve_package_request(store, package_request_id=request["package_request_id"], request_digest=request["request_digest"])
                    completed = execute_approved_package_request(store, package_request_id=request["package_request_id"], request_digest=request["request_digest"])
                    self.assertEqual(completed["state"], "succeeded", completed["result_json"])
                    result = json.loads(completed["result_json"])
                    self.assertTrue(result["materialized"])
                    self.assertFalse(result["networkContacted"])
                    self.assertFalse(result["remoteProviderContacted"])
                    self.assertEqual(result["cacheInventoryDigest"], cache["inventoryDigest"])
                    self.assertEqual(result["installedPackages"], [{"name": package_name, "version": "1.0.0"}])
                    self.assertTrue(result["cleanup"]["absentById"] and result["cleanup"]["absentByName"])
                    results[ecosystem] = {"treeDigest": result["environmentTreeDigest"], "image": result["image"], "scriptsPolicy": result["scriptsPolicy"]}
            finally:
                prepared.close()
            self.assertEqual(store.conn.execute("SELECT count(*) FROM package_environments").fetchone()[0], 2)
            return results

    def test_npm_and_python_materialize_deterministically_from_external_caches(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as first_raw, tempfile.TemporaryDirectory(dir="/tmp") as second_raw:
            first = self._materialize(Path(first_raw))
            second = self._materialize(Path(second_raw))
        self.assertEqual(first, second)

    def test_config_only_image_and_cache_tamper_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            root = Path(raw)
            state = root / "state"
            with PiStore(state) as store:
                create_package_caches(root, state)
                marker = state / ".pi-package-cache-test-fixture"
                marker.unlink()
                with self.assertRaisesRegex(PackageEnvironmentError, "test fixture"):
                    _load_cache_policy(store, "npm")
                marker.write_text("P6-NONPRODUCTION-PACKAGE-CACHE\n", encoding="ascii")
                marker.chmod(0o600)
                artifact = root / "package-cache/npm/p6-tiny-npm-1.0.0.tgz"
                artifact.chmod(0o600)
                artifact.write_bytes(artifact.read_bytes() + b"tamper")
                artifact.chmod(0o400)
                with self.assertRaisesRegex(PackageEnvironmentError, "artifact differs"):
                    _load_cache_policy(store, "npm")

    def test_input_tree_tamper_after_approval_finalizes_request_as_failed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            root = Path(raw)
            repository = _repo(root, "packages")
            client = PiControllerClient(root / "state")
            project = client.register_project(str(repository), "packages")
            cache = create_package_caches(root, root / "state")
            npm_spec = "file:/cache/npm/p6-tiny-npm-1.0.0.tgz"
            (repository / "package.json").write_text(json.dumps({"name": "fixture", "version": "1.0.0", "packageManager": "npm@10.9.8", "dependencies": {"p6-tiny-npm": npm_spec}}), encoding="utf-8")
            (repository / "package-lock.json").write_text(json.dumps({"name": "fixture", "version": "1.0.0", "lockfileVersion": 3, "packages": {"": {"name": "fixture", "version": "1.0.0", "dependencies": {"p6-tiny-npm": npm_spec}}, "node_modules/p6-tiny-npm": {"version": "1.0.0", "resolved": npm_spec, "integrity": cache["npmIntegrity"]}}}), encoding="utf-8")
            with PiStore(root / "state") as store:
                working = dict(store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (project["project_id"],)).fetchone())
            change = client.submit_change(project_id=project["project_id"], working_copy_id=working["working_copy_id"], target_ref=working["branch_ref"], title="tamper fixture", summary="input tree disappears after approval", capture_mode="dirty", selected_paths=["package.json", "package-lock.json"], idempotency_key="p6-tamper-tree")
            with PiStore(root / "state") as store:
                _register_build(store, root)
                inventory_dependencies(store, project_id=project["project_id"], change_id=change["changeId"], revision=change["revision"])
                conversation = client.create_conversation(project_id=project["project_id"], role="personal", display_name="tamper", working_copy_id=working["working_copy_id"])
                run_id = new_id("run")
                prepared = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host("personal"), tool_runtime=tool_runtime(run_id, project, working), run_id=run_id)
                try:
                    request = request_package_operation(store, project_id=project["project_id"], conversation_id=conversation["conversation_id"], run_id=run_id, writer_generation=prepared.run["writer_epoch"], change_id=change["changeId"], revision=change["revision"], ecosystem="npm", action="add", package_name="p6-tiny-npm", exact_version="1.0.0")
                    approve_package_request(store, package_request_id=request["package_request_id"], request_digest=request["request_digest"])
                    tree_oid = change["treeOid"]
                    loose = repository / ".git" / "objects" / tree_oid[:2] / tree_oid[2:]
                    if not loose.is_file():
                        raise AssertionError("fixture tree object is not a loose object")
                    loose.unlink()
                    completed = execute_approved_package_request(store, package_request_id=request["package_request_id"], request_digest=request["request_digest"])
                    self.assertEqual(completed["state"], "failed")
                    self.assertEqual(store.conn.execute("SELECT state FROM package_requests WHERE package_request_id=?", (request["package_request_id"],)).fetchone()[0], "failed")
                    auth_state = store.conn.execute("SELECT a.state FROM package_requests r JOIN authorizations a ON a.authorization_id=r.authorization_id WHERE r.package_request_id=?", (request["package_request_id"],)).fetchone()
                    self.assertEqual(auth_state[0], "consumed")
                finally:
                    prepared.close()


if __name__ == "__main__":
    unittest.main()
