"""P12 activation approval and extended activate() source tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.activation_approval import ActivationApprovalError, consume_activation_approval, request_activation_approval
from scripts.pi_control.errors import IdempotencyConflictError, NotFoundError
from scripts.pi_control.pi_install import InstallError, activate
from scripts.pi_control.pi_store import PiStore


class ActivationApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp", prefix="p12-approval-")
        self.root = Path(self.temporary.name)
        self.store = PiStore(self.root / "state").open()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _request(self) -> dict:
        return request_activation_approval(
            self.store,
            build_id="build_" + "a" * 32,
            staged_root=str(self.root / "stage"),
            data_root=str(self.root / "data"),
            rollback_plan={"existingDataRoot": False, "availableRollbackGenerations": 0},
            actor_id="p12-user",
        )

    def test_request_consume_cancel_lifecycle(self) -> None:
        approval = self._request()
        self.assertEqual(approval["state"], "active")
        consumed = consume_activation_approval(self.store, approval_id=approval["approval_id"], scope_digest=approval["scope_digest"])
        self.assertEqual(consumed["state"], "consumed")
        with self.assertRaises(ActivationApprovalError):
            consume_activation_approval(self.store, approval_id=approval["approval_id"], scope_digest=approval["scope_digest"])

    def test_request_idempotent_and_conflict(self) -> None:
        first = self._request()
        replay = self._request()
        self.assertEqual(first["approval_id"], replay["approval_id"])
        from scripts.pi_control.activation_approval import cancel_activation_approval
        cancel_activation_approval(self.store, approval_id=first["approval_id"])
        with self.assertRaises(IdempotencyConflictError):
            self._request()

    def test_consume_rejects_wrong_digest(self) -> None:
        approval = self._request()
        with self.assertRaises(ActivationApprovalError):
            consume_activation_approval(self.store, approval_id=approval["approval_id"], scope_digest="sha256:wrong")

    def test_consume_missing_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            consume_activation_approval(self.store, approval_id="act_" + "c" * 32, scope_digest="sha256:x")

    def test_cancel_then_consume_rejected(self) -> None:
        approval = self._request()
        from scripts.pi_control.activation_approval import cancel_activation_approval
        cancel_activation_approval(self.store, approval_id=approval["approval_id"])
        with self.assertRaises(ActivationApprovalError):
            consume_activation_approval(self.store, approval_id=approval["approval_id"], scope_digest=approval["scope_digest"])


class ActivationSmokeTests(unittest.TestCase):
    def test_activate_runs_smoke_and_initializes_fresh_state(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="p12-smoke-") as raw:
            root = Path(raw)
            from tests.system.staged_install import install
            stage_root = root / "stage"
            install(stage_root)
            data_root = root / "data"
            result = activate(stage_root, data_root)
            self.assertTrue(result["activated"])
            self.assertTrue((data_root / "activation.json").is_file())
            self.assertTrue((data_root / "state" / "control.db").is_file())

    def test_activate_second_generation_preserves_rollback(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="p12-smoke2-") as raw:
            root = Path(raw)
            from tests.system.staged_install import install
            first = root / "stage1"
            install(first)
            data_root = root / "data"
            activate(first, data_root)
            second = root / "stage2"
            install(second)
            activate(second, data_root)
            rollbacks = list(root.glob("data.rollback.*"))
            self.assertGreaterEqual(len(rollbacks), 1)
            self.assertTrue((data_root / "activation.json").is_file())

    def test_activate_missing_controller_fails_smoke(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="p12-smoke3-") as raw:
            root = Path(raw)
            from tests.system.staged_install import install
            stage_root = root / "stage"
            built = install(stage_root)
            # Remove the controller after verify passes but before smoke: smoke
            # runs the activated controller, so removing it makes smoke fail.
            from scripts.pi_control.pi_install import _bounded_smoke
            data_root = root / "data"
            import shutil
            shutil.copytree(stage_root, data_root, symlinks=True)
            (data_root / "bin" / "pi-control").unlink()
            with self.assertRaises(InstallError):
                _bounded_smoke(data_root)


if __name__ == "__main__":
    unittest.main()
