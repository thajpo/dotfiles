"""P6 immutable npm/Python lock adapter tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pi_control.package_diff import PackageInputError, diff_observations, observe_package_tree
from scripts.pi_control.dependencies import DependencyError, inventory_dependencies, package_review_gate, record_package_security_review, set_dependency_disposition
from scripts.pi_control.greenfield_client import GreenfieldControllerClient
from scripts.pi_control.greenfield_store import GreenfieldStore
from scripts.pi_control.investigators import bind_investigation_run, start_investigation
from scripts.pi_control.launch import prepare_run
from scripts.pi_control.models import new_id
from scripts.pi_control.package_environment import approve_package_request, execute_approved_package_request, request_package_operation
from tests.control_plane.test_p2_contract import tool_runtime
from tests.greenfield_test_build import allow_test_only_registered_build_rows
from tests.test_greenfield_core import _BUILD_ID, _host, _register_build, _repo


def _git(root: Path, *args: str) -> str:
    environment = {**os.environ, "GIT_AUTHOR_NAME": "P6", "GIT_AUTHOR_EMAIL": "p6@example.invalid", "GIT_COMMITTER_NAME": "P6", "GIT_COMMITTER_EMAIL": "p6@example.invalid"}
    return subprocess.run(["git", "-C", str(root), *args], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True).stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", ".")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD^{tree}")


class P6PackageTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_npm_v3_pinned_manager_and_exact_lock_delta(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _git(root.parent, "init", "-q", str(root))
            (root / "README").write_text("base\n")
            base = _commit(root, "base")
            (root / "package.json").write_text(json.dumps({"name": "fixture", "version": "1.0.0", "packageManager": "npm@10.8.2", "dependencies": {"left-pad": "^1.3.0"}}))
            (root / "package-lock.json").write_text(json.dumps({"name": "fixture", "version": "1.0.0", "lockfileVersion": 3, "packages": {"": {"name": "fixture", "version": "1.0.0", "dependencies": {"left-pad": "^1.3.0"}}, "node_modules/left-pad": {"version": "1.3.0", "resolved": "https://registry.invalid/left-pad.tgz", "integrity": "sha512-fixture"}}}))
            candidate = _commit(root, "npm")
            before = observe_package_tree(root, base)
            after = observe_package_tree(root, candidate)
            self.assertEqual(after["ecosystems"][0]["managerVersion"], "10.8.2")
            self.assertEqual(diff_observations(before, after), [{"ecosystem": "npm", "changeKind": "add", "packageName": "left-pad", "baseVersion": "", "exactVersion": "1.3.0"}])
            self.assertTrue(after["ecosystems"][0]["lockDigest"].startswith("sha256:"))

    def test_python_uv_and_hash_requirements_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _git(root.parent, "init", "-q", str(root))
            (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1.0.0'\n")
            (root / "uv.lock").write_text("version = 1\n[[package]]\nname = 'demo'\nversion = '2.0.0'\nsdist = { url = 'https://example.invalid/demo.tar.gz', hash = 'sha256:" + "a" * 64 + "' }\n")
            tree = _commit(root, "uv")
            self.assertEqual(observe_package_tree(root, tree)["ecosystems"][0]["resolved"], {"demo": "2.0.0"})

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _git(root.parent, "init", "-q", str(root))
            (root / "requirements.txt").write_text("demo==2.0.0 --hash=sha256:" + "b" * 64 + "\n")
            tree = _commit(root, "requirements")
            value = observe_package_tree(root, tree)["ecosystems"][0]
            self.assertEqual(value["managerVersion"], "pip-hash")
            self.assertEqual(value["resolved"], {"demo": "2.0.0"})

    def test_unsupported_unlocked_and_range_only_inputs_fail_closed(self) -> None:
        fixtures = {
            "yarn": {"package.json": "{}", "yarn.lock": "lock"},
            "npm-unlocked": {"package.json": json.dumps({"packageManager": "npm@10.8.2", "dependencies": {"x": "^1"}})},
            "python-range": {"requirements.txt": "demo>=2\n"},
            "cargo": {"Cargo.toml": "[package]\nname='x'\nversion='1.0.0'\n"},
        }
        for name, files in fixtures.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _git(root.parent, "init", "-q", str(root))
                for path, body in files.items():
                    (root / path).write_text(body)
                tree = _commit(root, name)
                with self.assertRaises(PackageInputError):
                    observe_package_tree(root, tree)

    def test_inventory_package_request_refusal_and_real_investigator_negative_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = _repo(root, "integrated")
            client = GreenfieldControllerClient(root / "state")
            project = client.register_project(str(repository), "integrated")
            (repository / "package.json").write_text(json.dumps({"name": "fixture", "version": "1.0.0", "packageManager": "npm@10.8.2", "dependencies": {"left-pad": "1.3.0"}}))
            (repository / "package-lock.json").write_text(json.dumps({"name": "fixture", "version": "1.0.0", "lockfileVersion": 3, "packages": {"": {"dependencies": {"left-pad": "1.3.0"}}, "node_modules/left-pad": {"version": "1.3.0", "resolved": "https://registry.invalid/left-pad.tgz", "integrity": "sha512-fixture"}}}))
            with GreenfieldStore(root / "state") as store:
                working = dict(store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (project["project_id"],)).fetchone())
            change = client.submit_change(project_id=project["project_id"], working_copy_id=working["working_copy_id"], target_ref=working["branch_ref"], title="package", summary="locked package", capture_mode="dirty", selected_paths=["package.json", "package-lock.json"], idempotency_key="p6-package-candidate")
            with GreenfieldStore(root / "state") as store:
                _register_build(store, root)
                inventory = inventory_dependencies(store, project_id=project["project_id"], change_id=change["changeId"], revision=change["revision"])
                self.assertEqual([(item["changeKind"], item["packageName"], item["exactVersion"]) for item in inventory["differences"]], [("add", "left-pad", "1.3.0")])
                dependency = inventory["records"][0]
                writer = client.create_conversation(project_id=project["project_id"], role="personal", display_name="package writer", working_copy_id=working["working_copy_id"])
                writer_run_id = new_id("run")
                writer_prepared = prepare_run(store, conversation_id=writer["conversation_id"], build_id=_BUILD_ID, host_process=_host("personal"), tool_runtime=tool_runtime(writer_run_id, project, working), run_id=writer_run_id)
                package = request_package_operation(store, project_id=project["project_id"], conversation_id=writer["conversation_id"], run_id=writer_run_id, writer_generation=writer_prepared.run["writer_epoch"], change_id=change["changeId"], revision=change["revision"], ecosystem="npm", action="add", package_name="left-pad", exact_version="1.3.0")
                approve_package_request(store, package_request_id=package["package_request_id"], request_digest=package["request_digest"])
                refused = execute_approved_package_request(store, package_request_id=package["package_request_id"], request_digest=package["request_digest"])
                result = json.loads(refused["result_json"])
                self.assertEqual((refused["state"], result["reason"], result["scriptsPolicy"]), ("failed", "exact-local-artifact-cache-unavailable", "disabled"))
                self.assertFalse(result["remoteProviderContacted"])
                self.assertTrue(result["privateEnvironmentIdentity"].startswith("pkg_"))
                self.assertFalse(result["materialized"])
                with self.assertRaisesRegex(Exception, "replay|consumed"):
                    execute_approved_package_request(store, package_request_id=package["package_request_id"], request_digest=package["request_digest"])

                set_dependency_disposition(store, dependency_change_id=dependency["dependency_change_id"], disposition="review-required")
                with self.assertRaises(DependencyError):
                    record_package_security_review(store, dependency_change_id=dependency["dependency_change_id"], evidence={}, risk_level="low", recommendation="approve", investigator_run_id=writer_run_id)
                assignment = start_investigation(store, project_id=project["project_id"], purpose="package security", working_copy_id=working["working_copy_id"])
                investigator_prepared = prepare_run(store, conversation_id=assignment["conversation_id"], build_id=_BUILD_ID, host_process=_host("investigator"))
                bind_investigation_run(store, conversation_id=assignment["conversation_id"], run_id=investigator_prepared.run["run_id"])
                review = record_package_security_review(store, dependency_change_id=dependency["dependency_change_id"], evidence={"source": "local fixture"}, risk_level="high", recommendation="reject", investigator_run_id=investigator_prepared.run["run_id"])
                self.assertEqual(review["state"], "rejected")
                gate = package_review_gate(store, change_id=change["changeId"], revision=change["revision"])
                self.assertFalse(gate["ready"])
                self.assertEqual(gate["rejected"], [dependency["dependency_change_id"]])
                investigator_prepared.close()
                writer_prepared.close()


if __name__ == "__main__":
    unittest.main()
