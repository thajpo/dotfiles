"""Focused P3 session, channel, and launcher rejection contracts."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.pi_control.controller_channel import ChannelReader, ControllerChannelError, MAX_FRAME_BYTES, receive_frame, send_frame, validate_handshake
from scripts.pi_control.greenfield_client import GreenfieldControllerClient
from scripts.pi_control.greenfield_store import GreenfieldStore
from scripts.pi_control.host_supervisor import HostSupervisorError, _rpc, ensure_session, launch_host_pi
from scripts.pi_control.launch import attest_run, prepare_run, stop_run
from scripts.pi_control.models import canonical_json, utc_now
from scripts.pi_control.run_manifest import executable_sha256


def expected_handshake() -> dict:
    return {
        "runId": "run_" + "1" * 32,
        "manifestDigest": "sha256:" + "2" * 64,
        "childPid": os.getpid(),
        "childStartIdentity": "linux:boot:1",
        "role": "secretary",
        "sessionId": "pi-conv_" + "3" * 32,
        "sessionPath": "/state/session.jsonl",
        "activeTools": ["git_read", "grep", "ls", "read"],
        "toolSources": [{"name": name, "path": "/stage/scoped.ts"} for name in ("git_read", "grep", "ls", "read")],
        "loadedResources": [{"resourceId": "extension:controller-channel", "path": "/stage/channel.ts", "digest": "sha256:" + "4" * 64}],
    }


def handshake() -> dict:
    return {"protocolVersion": 1, "type": "startup", **expected_handshake()}


class ControllerChannelTests(unittest.TestCase):
    def test_exact_handshake_and_all_identity_mismatches(self) -> None:
        self.assertEqual(validate_handshake(handshake(), expected_handshake())["role"], "secretary")
        mutations = {
            "runId": "run_" + "f" * 32,
            "manifestDigest": "sha256:" + "f" * 64,
            "childPid": os.getpid() + 1,
            "childStartIdentity": "linux:boot:2",
            "role": "reviewer",
            "sessionId": "pi-conv_bad",
            "sessionPath": "/wrong",
            "activeTools": ["bash"],
            "toolSources": [],
            "loadedResources": [{"resourceId": "x", "path": "/x", "digest": "sha256:" + "f" * 64}],
        }
        for key, value in mutations.items():
            changed = copy.deepcopy(handshake())
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ControllerChannelError):
                validate_handshake(changed, expected_handshake())

    def test_timeout_eof_oversize_and_noncanonical_frames(self) -> None:
        left, right = socket.socketpair()
        try:
            with self.assertRaisesRegex(ControllerChannelError, "timed out"):
                receive_frame(left, timeout=0.01)
            right.close()
            with self.assertRaisesRegex(ControllerChannelError, "closed"):
                receive_frame(left, timeout=0.1)
        finally:
            left.close()
            right.close()
        for payload, message in ((b"x" * (MAX_FRAME_BYTES + 1), "exceeds"), (b'{"b":1,"a":2}\n', "canonical")):
            left, right = socket.socketpair()
            try:
                right.sendall(payload)
                with self.assertRaisesRegex(ControllerChannelError, message):
                    receive_frame(left, timeout=0.1)
            finally:
                left.close()
                right.close()

    def test_send_and_receive_require_one_canonical_bounded_frame(self) -> None:
        left, right = socket.socketpair()
        try:
            send_frame(left, {"z": 1, "a": 2})
            self.assertEqual(receive_frame(right, timeout=0.1), {"a": 2, "z": 1})
        finally:
            left.close()
            right.close()

    def test_channel_reader_delivers_pipelined_frames_in_order(self) -> None:
        left, right = socket.socketpair()
        try:
            send_frame(left, {"protocolVersion": 1, "type": "request", "requestId": "request-1", "operation": "read", "payload": {"path": "a"}})
            send_frame(left, {"protocolVersion": 1, "type": "request", "requestId": "request-2", "operation": "grep", "payload": {"pattern": "b"}})
            send_frame(left, {"protocolVersion": 1, "type": "cancel", "requestId": "request-1"})
            reader = ChannelReader(right)
            first = reader.receive(timeout=0.1)
            second = reader.receive(timeout=0.1)
            third = reader.receive(timeout=0.1)
            self.assertEqual(first["requestId"], "request-1")
            self.assertEqual(first["operation"], "read")
            self.assertEqual(second["requestId"], "request-2")
            self.assertEqual(second["operation"], "grep")
            self.assertEqual(third["type"], "cancel")
            self.assertEqual(third["requestId"], "request-1")
        finally:
            left.close()
            right.close()

    def test_channel_frame_accepts_large_text_payload(self) -> None:
        # Live tool payloads (file contents, command output) legitimately exceed
        # the 4096-char controller-record text bound; the channel bound is the
        # frame size itself.
        large_text = "x" * 9000
        left, right = socket.socketpair()
        try:
            send_frame(left, {"protocolVersion": 1, "type": "request", "requestId": "request-1", "operation": "read", "payload": {"content": large_text}})
            value = receive_frame(right, timeout=0.1)
            self.assertEqual(value["payload"]["content"], large_text)
        finally:
            left.close()
            right.close()


class ProviderAuthProvisioningTests(unittest.TestCase):
    def test_provision_copies_only_the_selected_provider(self) -> None:
        from scripts.pi_control.host_supervisor import _provision_provider_auth
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            agent_dir = root / "agent"
            agent_dir.mkdir(mode=0o700)
            (agent_dir / "auth.json").write_text(json.dumps({
                "openai-codex": {"type": "oauth", "access": "codex-secret", "refresh": "codex-refresh", "expires": 9999999999999, "accountId": "acc-1"},
                "deepseek": {"type": "key", "key": "deepseek-secret"},
            }))
            runtime = root / "runtime"
            (runtime / "agent").mkdir(parents=True, mode=0o700)
            with mock.patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(agent_dir)}, clear=False):
                _provision_provider_auth(runtime, "deepseek/deepseek-v4-flash")
            destination = runtime / "agent" / "auth.json"
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            provisioned = json.loads(destination.read_text())
            self.assertEqual(set(provisioned), {"deepseek"})
            self.assertEqual(provisioned["deepseek"]["key"], "deepseek-secret")
            self.assertNotIn("openai-codex", provisioned)

    def test_provision_skips_envkey_providers_and_missing_auth(self) -> None:
        from scripts.pi_control.host_supervisor import _provision_provider_auth
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            with mock.patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(root / "missing")}, clear=False):
                _provision_provider_auth(runtime, "deepseek/deepseek-v4-flash")
            self.assertFalse((runtime / "agent" / "auth.json").exists())


class SessionTests(unittest.TestCase):
    def test_session_is_created_canonically_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "state"
            state.mkdir(mode=0o700)
            cwd = Path(raw) / "repo"
            cwd.mkdir()
            path = state / "sessions" / ("prj_" + "1" * 32) / ("conv_" + "2" * 32 + ".jsonl")
            session_id = "pi-conv_" + "2" * 32
            first = ensure_session(path, state_root=state, session_id=session_id, cwd=cwd, timestamp="2026-08-09T00:00:00Z")
            second = ensure_session(path, state_root=state, session_id=session_id, cwd=cwd)
            self.assertEqual(first, second)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_bytes(), (canonical_json(first) + "\n").encode())

    def test_session_rejects_symlink_mode_and_header_mismatch(self) -> None:
        for fault in ("symlink", "mode", "header"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as raw:
                state = Path(raw) / "state"
                project = state / "sessions" / ("prj_" + "1" * 32)
                project.mkdir(parents=True, mode=0o700)
                path = project / ("conv_" + "2" * 32 + ".jsonl")
                cwd = Path(raw) / "repo"
                cwd.mkdir()
                header = {"type": "session", "version": 3, "id": "wrong" if fault == "header" else "pi-conv_" + "2" * 32, "timestamp": "2026-08-09T00:00:00Z", "cwd": str(cwd)}
                target = Path(raw) / "target"
                target.write_text(canonical_json(header) + "\n", encoding="utf-8")
                target.chmod(0o600)
                if fault == "symlink":
                    path.symlink_to(target)
                else:
                    path.write_text(canonical_json(header) + "\n", encoding="utf-8")
                    path.chmod(0o644 if fault == "mode" else 0o600)
                with self.assertRaises(HostSupervisorError):
                    ensure_session(path, state_root=state, session_id="pi-conv_" + "2" * 32, cwd=cwd)

    def test_non_pi_arbitrary_command_tail_is_rejected(self) -> None:
        result = subprocess.run(
            [str(Path(__file__).resolve().parents[2] / "bin/pi-system-run"), "--state-root", "/tmp/x", "--conversation-id", "conv_" + "1" * 32, "--build-id", "build_" + "2" * 32, "--prompt", "x", "--model", "scripted/scripted-1", "--", "/bin/sh"],
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized arguments", result.stderr)


class SupervisorProcessTests(unittest.TestCase):
    def _fixture(self, root: Path, *, child_body: str) -> tuple[GreenfieldStore, dict, str]:
        repository = root / "repo"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        (repository / "README").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "README"], check=True)
        subprocess.run(["git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
        client = GreenfieldControllerClient(root / "state")
        project = client.register_project(str(repository))
        store = GreenfieldStore(root / "state").open()
        conversation = store.conn.execute("SELECT * FROM conversations WHERE project_id=? AND role='secretary'", (project["project_id"],)).fetchone()
        build_id = "build_" + "b" * 32
        manifest = root / "build-manifest.json"
        inventory = root / "release-resources.json"
        manifest.write_text("fixture", encoding="utf-8")
        inventory.write_text("fixture", encoding="utf-8")
        digest = "sha256:" + "a" * 64
        store.conn.execute("INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,build_manifest_path,build_manifest_digest,resource_manifest_path,resource_manifest_digest,pi_version,package_lock_hash,status,installed_at,activated_at,rollback_path,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (build_id, None, digest, str(manifest), digest, str(inventory), "sha256:" + "c" * 64, "0.83.0", digest, "staged", utc_now(), None, None, "{}"))
        child = root / "child.py"
        child.write_text(child_body, encoding="utf-8")
        scoped = root / "scoped.ts"
        channel = root / "channel.ts"
        scoped.write_text("scoped", encoding="utf-8")
        channel.write_text("channel", encoding="utf-8")
        node = Path("/usr/bin/python3").resolve(strict=True)
        resources = [
            {"resourceId": "package:pi-core", "path": str(child), "digest": executable_sha256(node)},
            {"resourceId": "extension:controller-channel", "path": str(channel), "digest": "sha256:" + "1" * 64},
            {"resourceId": "extension:scoped-project-read", "path": str(scoped), "digest": "sha256:" + "2" * 64},
        ]
        selected = {"build": {}, "conversation": dict(conversation), "stage": root, "profile": {"supported": True, "resources": [item["resourceId"] for item in resources], "tools": ["git_read", "grep", "ls", "read"]}, "node": node, "resources": resources}
        return store, selected, build_id

    def _patches(self, selected: dict):
        def verify(store, build_id):
            return store.conn.execute("SELECT * FROM installed_builds WHERE build_id=?", (build_id,)).fetchone()
        return mock.patch("scripts.pi_control.host_supervisor._launch_selection", return_value=selected), mock.patch("scripts.pi_control.launch.verify_registered_build", side_effect=verify)

    def test_actual_child_handshake_binds_and_stops(self) -> None:
        body = '''import json, os, socket
fd = int(os.environ["PI_CONTROLLER_CHANNEL_FD"])
s = socket.socket(fileno=fd)
challenge = json.loads(s.makefile("rb").readline())
value = {"protocolVersion":1,"type":"startup",**{key:challenge[key] for key in ("runId","manifestDigest","childPid","childStartIdentity","role","sessionId","sessionPath","activeTools","toolSources")},"loadedResources":challenge["resources"]}
s.sendall(json.dumps(value,sort_keys=True,separators=(",",":")).encode()+b"\\n")
s.makefile("rb").readline()
'''
        with tempfile.TemporaryDirectory() as raw:
            store, selected, build_id = self._fixture(Path(raw), child_body=body)
            first, second = self._patches(selected)
            try:
                with first, second:
                    code = launch_host_pi(store, conversation_id=selected["conversation"]["conversation_id"], build_id=build_id, prompt="test", model="scripted/scripted-1")
                self.assertEqual(code, 0)
                run = store.conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
                self.assertEqual(run["observed_state"], "stopped")
                self.assertIsNotNone(run["child_pid"])
                self.assertEqual(json.loads(run["host_process_observation_json"])["handshake"]["childPid"], run["child_pid"])
            finally:
                store.close()

    def test_child_exit_before_handshake_is_failed_and_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store, selected, build_id = self._fixture(Path(raw), child_body="raise SystemExit(0)\n")
            first, second = self._patches(selected)
            try:
                with first, second, self.assertRaisesRegex(ControllerChannelError, "closed"):
                    launch_host_pi(store, conversation_id=selected["conversation"]["conversation_id"], build_id=build_id, prompt="test", model="scripted/scripted-1", handshake_timeout=1)
                run = store.conn.execute("SELECT observed_state,error_code FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
                self.assertEqual(tuple(run), ("failed", "HOST_ATTESTATION_FAILED"))
            finally:
                store.close()

    def test_executable_change_between_prepare_and_spawn_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, selected, build_id = self._fixture(root, child_body="raise SystemExit(0)\n")
            copied_node = root / "node"
            shutil.copy2(selected["node"], copied_node)
            selected["node"] = copied_node
            first, second = self._patches(selected)
            try:
                def change() -> None:
                    with copied_node.open("ab") as stream:
                        stream.write(b"changed")
                with first, second, self.assertRaisesRegex(HostSupervisorError, "changed between prepare and spawn"):
                    launch_host_pi(store, conversation_id=selected["conversation"]["conversation_id"], build_id=build_id, prompt="test", model="scripted/scripted-1", before_spawn=change)
                self.assertEqual(store.conn.execute("SELECT observed_state FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0], "failed")
            finally:
                store.close()

    def _prepared_rpc(self, root: Path):
        store, selected, build_id = self._fixture(root, child_body="raise SystemExit(0)\n")
        host = {
            "executable": str(selected["node"]),
            "executableSha256": executable_sha256(selected["node"]),
            "argv": [str(selected["node"]), selected["resources"][0]["path"]],
            "toolProfile": "secretary",
            "environmentKeys": [],
        }
        def verify(value, requested):
            return value.conn.execute("SELECT * FROM installed_builds WHERE build_id=?", (requested,)).fetchone()
        with mock.patch("scripts.pi_control.launch.verify_registered_build", side_effect=verify):
            prepared = prepare_run(store, conversation_id=selected["conversation"]["conversation_id"], build_id=build_id, host_process=host)
        attest_run(store, run_id=prepared.run["run_id"], manifest_digest=prepared.manifest["manifestDigest"])
        return store, prepared

    @staticmethod
    def _request(payload: dict) -> dict:
        return {"protocolVersion": 1, "type": "request", "requestId": "request-1", "operation": "scoped-read", "payload": payload}

    def test_rpc_reloads_exact_binding_and_rejects_payload_scope_override(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store, prepared = self._prepared_rpc(Path(raw))
            try:
                value = _rpc(store, prepared.run["run_id"], prepared.manifest["manifestDigest"], self._request({"operation": "read", "path": "README"}))
                self.assertEqual(value["lines"], ["fixture"])
                for field, identity in (("projectId", "prj_" + "f" * 32), ("workingCopyId", "wc_" + "f" * 32)):
                    with self.subTest(field=field), self.assertRaisesRegex(ControllerChannelError, "override"):
                        _rpc(store, prepared.run["run_id"], prepared.manifest["manifestDigest"], self._request({"operation": "read", "path": "README", field: identity}))
            finally:
                prepared.close()
                store.close()

    def test_rpc_rejects_stale_resource_version_terminal_run_and_wrong_revision(self) -> None:
        for fault in ("version", "terminal", "revision"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                store, prepared = self._prepared_rpc(root)
                try:
                    if fault == "version":
                        store.conn.execute("UPDATE working_copies SET resource_version=resource_version+1 WHERE working_copy_id=?", (prepared.run["working_copy_id"],))
                    elif fault == "terminal":
                        stop_run(store, run_id=prepared.run["run_id"], reason="test-terminal")
                    else:
                        repository = root / "repo"
                        (repository / "README").write_text("moved\n", encoding="utf-8")
                        subprocess.run(["git", "-C", str(repository), "add", "README"], check=True)
                        subprocess.run(["git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "moved"], check=True)
                    with self.assertRaises((ControllerChannelError, PermissionError)):
                        _rpc(store, prepared.run["run_id"], prepared.manifest["manifestDigest"], self._request({"operation": "read", "path": "README"}))
                finally:
                    prepared.close()
                    store.close()


if __name__ == "__main__":
    unittest.main()
