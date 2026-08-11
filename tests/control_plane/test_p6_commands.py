"""P6 structured command, TTY receipt, replay, stale, and restart tests."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pty
import subprocess
import tempfile
import termios
import time
import unittest

from scripts.pi_control.command_requests import CommandRequestError, approve_command, execute_approved_command, normalize_operation, request_command
from scripts.pi_control.greenfield_client import GreenfieldControllerClient
from scripts.pi_control.greenfield_store import GreenfieldStore
from scripts.pi_control.launch import prepare_run
from scripts.pi_control.models import new_id
from tests.control_plane.test_p2_contract import tool_runtime
from tests.greenfield_test_build import allow_test_only_registered_build_rows
from tests.test_greenfield_core import _BUILD_ID, _host, _register_build, _repo


ROOT = Path(__file__).resolve().parents[2]


def _pty_authorize(argv: list[str], decision: str) -> tuple[int, str]:
    master, slave = pty.openpty()
    def controlling_tty() -> None:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True, preexec_fn=controlling_tty)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 10
    sent = False
    while time.monotonic() < deadline:
        try:
            block = os.read(master, 4096)
        except OSError:
            break
        if not block:
            break
        output.extend(block)
        if not sent and b"Type APPROVE or REJECT:" in output:
            os.write(master, decision.encode("ascii") + b"\n")
            sent = True
        if process.poll() is not None:
            break
    process.wait(timeout=10)
    os.close(master)
    return process.returncode, output.decode("utf-8", errors="replace")


class P6CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fixture(self, root: Path) -> tuple[GreenfieldStore, object, dict, dict]:
        repository = _repo(root, "commands")
        client = GreenfieldControllerClient(root / "state")
        project = client.register_project(str(repository), "commands")
        store = GreenfieldStore(root / "state").open()
        _register_build(store, root)
        working = dict(store.conn.execute("SELECT * FROM working_copies WHERE project_id=?", (project["project_id"],)).fetchone())
        conversation = client.create_conversation(project_id=project["project_id"], role="personal", display_name="P6", working_copy_id=working["working_copy_id"])
        run_id = new_id("run")
        prepared = prepare_run(store, conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host("personal"), tool_runtime=tool_runtime(run_id, project, working), run_id=run_id)
        return store, prepared, project, conversation

    def _request(self, store: GreenfieldStore, prepared: object, project: dict, conversation: dict, operation: str) -> dict:
        run = prepared.run  # type: ignore[attr-defined]
        return request_command(store, project_id=project["project_id"], conversation_id=conversation["conversation_id"], run_id=run["run_id"], writer_generation=run["writer_epoch"], operation=operation, purpose="P6 deterministic fixture")

    def test_grammar_receipt_success_failure_timeout_replay_and_restart(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            root = Path(raw)
            store, prepared, project, conversation = self._fixture(root)
            try:
                with self.assertRaises(CommandRequestError):
                    normalize_operation("shell.arbitrary")
                success = self._request(store, prepared, project, conversation, "host.fixture-success")
                receipt = approve_command(store, command_request_id=success["command_request_id"], request_digest=success["request_digest"])
                scope = receipt["receipt"]
                self.assertEqual(scope["requestDigest"], success["request_digest"])
                self.assertEqual(scope["operation"]["name"], "host.fixture-success")
                self.assertEqual(scope["controller"], store.controller_identity())
                self.assertTrue(scope["oneUse"])
                self.assertEqual(execute_approved_command(store, command_request_id=success["command_request_id"], request_digest=success["request_digest"])["state"], "succeeded")
                with self.assertRaisesRegex(CommandRequestError, "replay|consumed"):
                    execute_approved_command(store, command_request_id=success["command_request_id"], request_digest=success["request_digest"])

                failure = self._request(store, prepared, project, conversation, "host.fixture-failure")
                approve_command(store, command_request_id=failure["command_request_id"], request_digest=failure["request_digest"])
                self.assertEqual(execute_approved_command(store, command_request_id=failure["command_request_id"], request_digest=failure["request_digest"])["state"], "failed")

                timeout = self._request(store, prepared, project, conversation, "host.fixture-timeout")
                approve_command(store, command_request_id=timeout["command_request_id"], request_digest=timeout["request_digest"])
                timed = execute_approved_command(store, command_request_id=timeout["command_request_id"], request_digest=timeout["request_digest"])
                self.assertEqual(timed["state"], "failed")
                self.assertTrue(json.loads(timed["result_json"])["timedOut"])

                restart = self._request(store, prepared, project, conversation, "host.controller-status")
                approve_command(store, command_request_id=restart["command_request_id"], request_digest=restart["request_digest"])
                store.rotate_controller_restart_epoch()
                with self.assertRaisesRegex(CommandRequestError, "controller generation"):
                    execute_approved_command(store, command_request_id=restart["command_request_id"], request_digest=restart["request_digest"])
            finally:
                prepared.close()  # type: ignore[attr-defined]
                store.close()

    def test_real_controlling_tty_approve_reject_and_no_tty_refusal(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            root = Path(raw)
            store, prepared, project, conversation = self._fixture(root)
            try:
                approve = self._request(store, prepared, project, conversation, "host.fixture-success")
                argv = [str(ROOT / "bin/pi-authorize"), "--state-root", str(root / "state"), approve["command_request_id"], approve["request_digest"]]
                code, output = _pty_authorize(argv, "APPROVE")
                self.assertEqual(code, 0, output)
                for expected in (approve["command_request_id"], approve["request_digest"], project["project_id"], conversation["conversation_id"], prepared.run["run_id"], "host.fixture-success", "execution place: host", "effect scope:", "expiry:"):
                    self.assertIn(str(expected), output)
                replay_code, replay_output = _pty_authorize(argv, "APPROVE")
                self.assertNotEqual(replay_code, 0, replay_output)

                reject = self._request(store, prepared, project, conversation, "host.fixture-failure")
                reject_argv = [str(ROOT / "bin/pi-authorize"), "--state-root", str(root / "state"), reject["command_request_id"], reject["request_digest"]]
                reject_code, reject_output = _pty_authorize(reject_argv, "REJECT")
                self.assertEqual(reject_code, 0, reject_output)
                self.assertEqual(store.conn.execute("SELECT state FROM command_requests WHERE command_request_id=?", (reject["command_request_id"],)).fetchone()[0], "rejected")

                no_tty = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
                self.assertNotEqual(no_tty.returncode, 0)
                self.assertIn("controlling TTY", no_tty.stderr)
            finally:
                prepared.close()
                store.close()

    def test_stale_writer_and_changed_digest_fail_before_approval(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
            root = Path(raw)
            store, prepared, project, conversation = self._fixture(root)
            request = self._request(store, prepared, project, conversation, "host.fixture-success")
            with self.assertRaises(CommandRequestError):
                approve_command(store, command_request_id=request["command_request_id"], request_digest="0" * 64)
            with store.transaction():
                store.conn.execute("UPDATE runs SET desired_state='stopped',observed_state='stopped' WHERE run_id=?", (prepared.run["run_id"],))
                store.conn.execute("UPDATE working_copies SET active_writer_run_id=NULL WHERE working_copy_id=?", (prepared.run["working_copy_id"],))
            with self.assertRaisesRegex(CommandRequestError, "stale|terminal"):
                approve_command(store, command_request_id=request["command_request_id"], request_digest=request["request_digest"])
            prepared.close()
            store.close()


if __name__ == "__main__":
    unittest.main()
