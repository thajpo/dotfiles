"""Focused P2 schema, role, protocol, build, and preparation contracts."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from scripts.pi_control.conversations import create_conversation
from scripts.pi_control.command_requests import CommandRequestError
from scripts.pi_control.errors import DatabaseCorruptError, ErrorCode
from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_protocol import ProtocolError, protocol_request
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.launch import LaunchError, prepare_run
from scripts.pi_control.installed_builds import register_staged_build
from scripts.pi_control.models import canonical_json, new_id, utc_now
from scripts.pi_control.run_manifest import executable_sha256
from scripts.pi_control.pi_install import stage
from tests.pi_test_build import allow_test_only_registered_build_rows
from tests.test_pi_install import ROOT, pack_test_pi_core


DIGEST = "sha256:" + "a" * 64
BUILD_ID = "build_" + "b" * 32


def repository(root: Path, name: str = "repo") -> Path:
    path = root / name
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    return path


def register_test_build(store: PiStore, root: Path) -> None:
    build_manifest = root / "build-manifest.json"
    resources = root / "release-resources.json"
    build_manifest.write_text("test", encoding="utf-8")
    resources.write_text("test", encoding="utf-8")
    now = utc_now()
    store.conn.execute(
        "INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,build_manifest_path,build_manifest_digest,resource_manifest_path,resource_manifest_digest,pi_version,package_lock_hash,status,installed_at,activated_at,rollback_path,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (BUILD_ID, None, DIGEST, str(build_manifest), DIGEST, str(resources), "sha256:" + "c" * 64, "0.83.0", DIGEST, "staged", now, None, None, canonical_json({"verified": True})),
    )


def host_process(role: str = "secretary") -> dict[str, object]:
    executable = Path("/usr/bin/true").resolve(strict=True)
    return {"executable": str(executable), "executableSha256": executable_sha256(executable), "argv": [str(executable)], "toolProfile": role, "environmentKeys": ["PATH", "PI_RUNTIME_MANIFEST"]}


def tool_runtime(run_id: str, project: dict, working: dict, *, build_id: str = BUILD_ID, writer_epoch: int | None = None) -> dict:
    epoch = writer_epoch if writer_epoch is not None else int(working["writer_epoch"]) + 1
    image_digest = "sha256:" + "d" * 64
    value = {
        "specVersion": 2, "specHash": "", "platform": "linux/amd64", "imageReference": "registry.invalid/pi@" + image_digest,
        "imageConfigId": "sha256:" + "f" * 64, "registryDigest": image_digest, "command": ["python3", "-c", "idle"],
        "uid": 1000, "gid": 1000, "workdir": "/workspace", "readOnlyRoot": True,
        "mounts": [
            {"kind": "working-copy", "source": working["path"], "target": "/workspace", "readOnly": False, "sourceDevice": 1, "sourceInode": 2},
            {"kind": "git-mask", "source": "/state/git-mask", "target": "/workspace/.git", "readOnly": True, "sourceDevice": 1, "sourceInode": 3},
            {"kind": "package-environment", "source": f"/state/environments/{working['working_copy_id']}", "target": "/environments", "readOnly": False, "sourceDevice": 1, "sourceInode": 4},
        ],
        "tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777"}, "networkMode": "none", "capDrop": ["ALL"], "securityOpt": ["no-new-privileges:true"],
        "environment": {"HOME": "/tmp"}, "labels": {"pi.control.managed": "true", "pi.control.run-id": run_id, "pi.control.project-id": project["project_id"], "pi.control.working-copy-id": working["working_copy_id"], "pi.control.writer-epoch": str(epoch), "pi.control.controller-build-id": build_id},
        "resources": {"memoryBytes": 1, "nanoCpus": 1, "pidsLimit": 1},
    }
    body = dict(value); body.pop("specHash")
    value["specHash"] = "sha256:" + hashlib.sha256(canonical_json(body).encode()).hexdigest()
    return value


class P2ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.build_verifier_patch = allow_test_only_registered_build_rows()
        self.build_verifier_patch.start()
        self.addCleanup(self.build_verifier_patch.stop)

    def test_epoch_one_database_is_rejected_without_touching_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            root.mkdir(mode=0o700)
            sentinel = Path(temporary) / "old-state"
            sentinel.write_bytes(b"old-state-sentinel\x00unchanged")
            connection = sqlite3.connect(root / "control.db")
            connection.execute("PRAGMA user_version=1")
            connection.execute("CREATE TABLE old_epoch(value TEXT)")
            connection.commit()
            connection.close()
            os.chmod(root / "control.db", 0o600)
            database_before = (root / "control.db").read_bytes()
            with self.assertRaises(DatabaseCorruptError):
                PiStore(root).open()
            self.assertEqual((root / "control.db").read_bytes(), database_before)
            self.assertEqual(sentinel.read_bytes(), b"old-state-sentinel\x00unchanged")

    def test_roles_and_session_identity_are_controller_derived(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = PiControllerClient(root / "state")
            project = client.register_project(str(repository(root)))
            with PiStore(root / "state") as store:
                primary = store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (project["project_id"],)).fetchone()
                personal = create_conversation(store, project_id=project["project_id"], role="personal", display_name="personal", working_copy_id=primary["working_copy_id"])
                self.assertEqual(personal["authority_profile"], "writer-container")
                self.assertEqual(personal["pi_session_id"], "pi-" + personal["conversation_id"])
                self.assertTrue(personal["session_file"].endswith(f"/sessions/{project['project_id']}/{personal['conversation_id']}.jsonl"))
                for role in ("review", "host"):
                    with self.assertRaises(ValueError):
                        create_conversation(store, project_id=project["project_id"], role=role, display_name=role, working_copy_id=primary["working_copy_id"])
                with self.assertRaises(TypeError):
                    create_conversation(store, project_id=project["project_id"], role="secretary", display_name="bad", pi_session_id="caller")  # type: ignore[call-arg]
                project_sessions = root / "state" / "sessions" / project["project_id"]
                project_sessions.rmdir()
                os.symlink(root, project_sessions)
                with self.assertRaisesRegex(ValueError, "symlinked or unsafe"):
                    create_conversation(store, project_id=project["project_id"], role="personal", display_name="unsafe", working_copy_id=primary["working_copy_id"])

    def test_prepare_derives_scope_authority_and_operation_and_replays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = PiControllerClient(root / "state")
            project = client.register_project(str(repository(root)))
            with PiStore(root / "state") as store:
                register_test_build(store, root)
                conversation = store.conn.execute("SELECT * FROM conversations WHERE project_id=? AND role='secretary'", (project["project_id"],)).fetchone()
                first = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=BUILD_ID, host_process=host_process(), idempotency_key="prepare-one")
                replay = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=BUILD_ID, host_process=host_process(), idempotency_key="prepare-one")
                self.assertEqual(first.run["run_id"], replay.run["run_id"])
                self.assertEqual(first.run["authority"], "host-read-only")
                self.assertIsNone(first.manifest["workingCopy"])
                self.assertEqual(first.manifest["scope"]["source"], "project-primary")
                self.assertEqual(first.manifest["operationId"], first.run["operation_id"])
                self.assertNotIn("PI_RUNTIME_CAPABILITY", first.environment)
                operation = store.conn.execute("SELECT state FROM operations WHERE operation_id=?", (first.run["operation_id"],)).fetchone()
                self.assertEqual(operation[0], "succeeded")
                first.close()
                replay.close()

    def test_unregistered_build_and_writer_without_tool_runtime_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = PiControllerClient(root / "state")
            project = client.register_project(str(repository(root)))
            with PiStore(root / "state") as store:
                secretary = store.conn.execute("SELECT * FROM conversations WHERE role='secretary'").fetchone()
                with self.assertRaises(LaunchError):
                    prepare_run(store, conversation_id=secretary["conversation_id"], build_id=BUILD_ID, host_process=host_process())
                register_test_build(store, root)
                primary = store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (project["project_id"],)).fetchone()
                personal = create_conversation(store, project_id=project["project_id"], role="personal", display_name="writer", working_copy_id=primary["working_copy_id"])
                with self.assertRaises(LaunchError):
                    prepare_run(store, conversation_id=personal["conversation_id"], build_id=BUILD_ID, host_process=host_process("personal"))

    def test_database_rejects_cross_project_run_and_second_live_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = PiControllerClient(root / "state")
            first = client.register_project(str(repository(root, "one")))
            second = client.register_project(str(repository(root, "two")))
            with PiStore(root / "state") as store:
                register_test_build(store, root)
                primary = store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (first["project_id"],)).fetchone()
                one = create_conversation(store, project_id=first["project_id"], role="personal", display_name="one", working_copy_id=primary["working_copy_id"])
                two = create_conversation(store, project_id=first["project_id"], role="personal", display_name="two", working_copy_id=primary["working_copy_id"])
                first_run = new_id("run")
                prepared = prepare_run(store, conversation_id=one["conversation_id"], build_id=BUILD_ID, host_process=host_process("personal"), tool_runtime=tool_runtime(first_run, dict(first), dict(primary)), run_id=first_run)
                current = dict(store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (primary["working_copy_id"],)).fetchone())
                second_run = new_id("run")
                with self.assertRaises(LaunchError):
                    prepare_run(store, conversation_id=two["conversation_id"], build_id=BUILD_ID, host_process=host_process("personal"), tool_runtime=tool_runtime(second_run, dict(first), current), run_id=second_run)
                second_conversation = store.conn.execute("SELECT conversation_id FROM conversations WHERE project_id=?", (second["project_id"],)).fetchone()[0]
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute("UPDATE runs SET conversation_id=? WHERE run_id=?", (second_conversation, prepared.run["run_id"]))
                prepared.close()

    def test_protocol_is_exact_versioned_and_adapts_extension_requests(self) -> None:
        class FakeClient:
            def dispatch(self, operation, request):
                from scripts.pi_control.pi_protocol import adapt_request
                return adapt_request(operation, request)

        envelope = {"protocolVersion": 2, "operation": "message.post", "request": {"projectId": "p", "conversationId": "c", "runId": "r", "kind": "progress", "payload": {}, "idempotencyKey": "one"}}
        result = protocol_request(FakeClient(), envelope)
        self.assertEqual(result["result"]["idempotency_key"], "one")
        for value, code in (
            ({**envelope, "extra": True}, ErrorCode.PROTOCOL_ENVELOPE),
            ({**envelope, "protocolVersion": 1}, ErrorCode.PROTOCOL_VERSION),
            ({**envelope, "operation": "unknown"}, ErrorCode.PROTOCOL_OPERATION),
            ({**envelope, "request": {**envelope["request"], "authority": "writer"}}, ErrorCode.PROTOCOL_REQUEST),
        ):
            with self.subTest(code=code), self.assertRaises(ProtocolError) as raised:
                protocol_request(FakeClient(), value)
            self.assertEqual(raised.exception.code, code)

        command = {"projectId": "p", "conversationId": "c", "runId": "r", "writerGeneration": 7, "operation": "host.controller-status", "purpose": "test"}
        adapted = protocol_request(FakeClient(), {"protocolVersion": 2, "operation": "command.request", "request": command})["result"]
        self.assertEqual(adapted["writer_generation"], 7)

    def test_stale_protocol_writer_generation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = PiControllerClient(root / "state")
            project = client.register_project(str(repository(root)))
            with PiStore(root / "state") as store:
                register_test_build(store, root)
                primary = store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (project["project_id"],)).fetchone()
                conversation = create_conversation(store, project_id=project["project_id"], role="personal", display_name="writer", working_copy_id=primary["working_copy_id"])
                run_id = new_id("run")
                prepared = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=BUILD_ID, host_process=host_process("personal"), tool_runtime=tool_runtime(run_id, dict(project), dict(primary)), run_id=run_id)
                request = {"projectId": project["project_id"], "conversationId": conversation["conversation_id"], "runId": prepared.run["run_id"], "writerGeneration": prepared.run["writer_epoch"] + 1, "operation": "host.controller-status", "purpose": "test stale generation"}
                with self.assertRaisesRegex(CommandRequestError, "stale"):
                    client.dispatch("command.request", request)
                request["writerGeneration"] = prepared.run["writer_epoch"]
                accepted = client.dispatch("command.request", request)
                self.assertEqual(accepted["writer_generation"], prepared.run["writer_epoch"])
                prepared.close()

    def test_real_registered_stage_tamper_blocks_prepare_before_claims(self) -> None:
        self.build_verifier_patch.stop()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core = pack_test_pi_core(root)
            stage_root = root / "stage"
            staged = stage(ROOT, stage_root, pi_core_tarball=core)
            client = PiControllerClient(root / "state")
            project = client.register_project(str(repository(root)))
            with PiStore(root / "state") as store:
                register_staged_build(store, stage_root)
                primary = store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (project["project_id"],)).fetchone()
                conversation = create_conversation(store, project_id=project["project_id"], role="personal", display_name="writer", working_copy_id=primary["working_copy_id"])
                operations_before = store.conn.execute("SELECT count(*) FROM operations").fetchone()[0]
                runs_before = store.conn.execute("SELECT count(*) FROM runs").fetchone()[0]
                writer_before = (primary["writer_epoch"], primary["active_writer_run_id"])
                executable = stage_root / "bin/pi-control"
                executable.write_bytes(executable.read_bytes() + b"\n# tampered after registration\n")
                image_digest = "sha256:" + "d" * 64
                tool = {"specVersion": 1, "specHash": "sha256:" + "e" * 64, "platform": "linux/amd64", "imageReference": "registry.invalid/pi@" + image_digest, "imageConfigId": "sha256:" + "f" * 64, "registryDigest": image_digest, "environmentKey": "test"}
                with self.assertRaisesRegex(LaunchError, "reverification"):
                    prepare_run(store, conversation_id=conversation["conversation_id"], build_id=staged["buildId"], host_process=host_process("personal"), tool_runtime=tool, idempotency_key="tampered-stage")
                self.assertEqual(store.conn.execute("SELECT count(*) FROM operations").fetchone()[0], operations_before)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM runs").fetchone()[0], runs_before)
                current = store.conn.execute("SELECT writer_epoch,active_writer_run_id FROM working_copies WHERE working_copy_id=?", (primary["working_copy_id"],)).fetchone()
                self.assertEqual(tuple(current), writer_before)


if __name__ == "__main__":
    unittest.main()
