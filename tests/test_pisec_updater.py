import fcntl
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.pisec.pi_store import PiStore
from scripts.pisec.operations import create_operation
from scripts.pisec.update_refresh import refresh_for_update


ROOT = Path(__file__).resolve().parents[1]
UPDATER_PATH = ROOT / "scripts" / "pisec-update.py"


class UpdaterContractTests(unittest.TestCase):
    def setUp(self):
        self.updater = runpy.run_path(str(UPDATER_PATH))
        self.module_globals = self.updater["update"].__globals__
        self.original_refresh_under_lock = self.module_globals["_refresh_under_lock"]
        self.module_globals["_refresh_under_lock"] = lambda _current, _wait, _state, _descriptor: {"ok": True, "failed": [], "pending": []}
        self.addCleanup(self.module_globals.__setitem__, "_refresh_under_lock", self.original_refresh_under_lock)

    def _install_verified_current(self, install: Path) -> tuple[Path, dict]:
        install.mkdir(mode=0o700)
        commit = self.updater["_git"](ROOT, "rev-parse", "HEAD")
        candidate = self.updater["_stage_candidate"](ROOT, commit, install)
        old = install / "deploy-old"
        os.replace(candidate["staging"], old)
        identity = self.updater["_deployment_identity"](old)
        record = self.updater["_write_verification"](install, old, {"refresh": {}})
        self.updater["_write_marker"](install, record)
        (install / "current").symlink_to(old.name)
        return old, identity

    def test_archive_contains_exact_collie_patch_and_no_other_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            commit, _tree, _digest = self.updater["_archive"](ROOT, "HEAD", bundle)
            self.assertTrue(commit)
            self.assertEqual(
                sorted(path.relative_to(bundle).as_posix() for path in (bundle / "patches").glob("*") if path.is_file()),
                ["patches/collie-v0.28-unread-idle.patch"],
            )

    def test_archive_manifest_preserves_opaque_unix_socket_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "state.archive-opaque"
            install = root / "install"
            archive.mkdir(mode=0o700)
            socket_path = archive / "runtime" / "broker.sock"
            socket_path.parent.mkdir(mode=0o700)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(socket_path))
                manifest = self.updater["_write_archive_manifest"](install, root / "state", archive)
            self.assertEqual(manifest["filesystemSha256"], self.updater["_opaque_archive_digest"](archive))
            self.assertTrue((install / "archive-manifests" / f"{archive.name}.json").is_file())

    def test_broker_health_wait_requires_ready_admin_socket(self):
        module_globals = self.updater["_wait_for_broker"].__globals__
        old_ready = module_globals["_broker_ready"]
        old_sleep = module_globals["time"].sleep
        states = iter((False, True))
        module_globals["_broker_ready"] = lambda: next(states)
        module_globals["time"].sleep = lambda _seconds: None
        try:
            self.updater["_wait_for_broker"](1)
        finally:
            module_globals["_broker_ready"] = old_ready
            module_globals["time"].sleep = old_sleep

    def test_broker_readiness_requires_accepting_admin_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            socket_path = root / "admin" / "control.sock"
            socket_path.parent.mkdir(mode=0o700)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            result = type("Result", (), {"returncode": 0})()
            try:
                with patch.dict(os.environ, {"PISEC_RUNTIME_ROOT": str(root)}), patch.object(self.updater["subprocess"], "run", return_value=result):
                    self.assertFalse(self.updater["_broker_ready"]())
                    server.listen()
                    self.assertTrue(self.updater["_broker_ready"]())
            finally:
                server.close()

    def test_locked_refresh_uses_candidate_runtime_and_inherited_control_plane_lock(self):
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps({"ok": True, "failed": [], "pending": []}), stderr="")
        current = Path("/tmp/deployment")
        state = Path("/tmp/state")
        with patch.object(self.updater["subprocess"], "run", return_value=completed) as run:
            result = self.original_refresh_under_lock(current, 7, state, 19)
        command = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual(command[:3], [os.sys.executable, "-m", "scripts.pisec.update_refresh"])
        self.assertEqual(options["pass_fds"], (19,))
        self.assertEqual(options["env"]["PISEC_CONTROL_PLANE_LOCK_FD"], "19")
        self.assertEqual(options["env"]["PISEC_STATE_ROOT"], str(state))
        self.assertEqual(options["env"]["PYTHONPATH"].split(os.pathsep)[0], str(current))
        self.assertTrue(result["ok"])

    def test_update_refresh_skips_adapter_surfaces_without_active_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            with PiStore(state):
                pass
            with patch("scripts.pisec.update_refresh.build_adapters") as build_adapters:
                result = refresh_for_update(state, 0)
            build_adapters.assert_not_called()
            self.assertEqual(result, {"generation": None, "upgraded": [], "pending": [], "skipped": [], "failed": [], "ok": True})

    def test_post_switch_starts_broker_after_locked_refresh_before_strict_doctor(self):
        calls = []

        def fake_run(argv, **_kwargs):
            calls.append(tuple(argv[1:3]))
            if argv[1] == "doctor":
                payload = {"ok": True}
            elif argv[1] == "reconcile":
                payload = {"reconciled": True, "errors": []}
            else:  # pragma: no cover - protects the expected command contract
                raise AssertionError(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

        module_globals = self.updater["_post_switch"].__globals__
        old_systemctl = module_globals["_systemctl"]
        old_wait = module_globals["_wait_for_broker"]
        old_semantic_health = module_globals["_semantic_state_health"]
        module_globals["_systemctl"] = lambda _action: None
        module_globals["_wait_for_broker"] = lambda _seconds: None
        module_globals["_semantic_state_health"] = lambda _state: None
        try:
            with patch.object(self.updater["subprocess"], "run", side_effect=fake_run):
                result = self.updater["_post_switch"](Path("/tmp/deployment"), 1, {"ok": True, "failed": [], "pending": []})
        finally:
            module_globals["_systemctl"] = old_systemctl
            module_globals["_wait_for_broker"] = old_wait
            module_globals["_semantic_state_health"] = old_semantic_health

        self.assertEqual(calls, [("doctor", "--json"), ("reconcile", "--json"), ("doctor", "--json")])
        self.assertEqual(result["doctorAfter"], "ok")

    def test_update_refreshes_while_locked_and_runs_health_after_releasing_control_plane(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            install = root / "install"
            with PiStore(state):
                pass
            module_globals = self.updater["update"].__globals__
            original_refresh = module_globals["_refresh_under_lock"]
            original_post_switch = module_globals["_post_switch"]
            original_systemctl = module_globals["_systemctl"]
            lock_path = state / "locks" / "control-plane.lock"

            def assert_refresh_lock(_current, _wait, _state, descriptor):
                self.assertEqual(os.fstat(descriptor).st_ino, lock_path.stat().st_ino)
                contender = os.open(lock_path, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(contender)
                return {"ok": True, "failed": [], "pending": []}

            def assert_health_unlocked(_current, _wait, refresh):
                contender = os.open(lock_path, os.O_RDWR)
                try:
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(contender, fcntl.LOCK_UN)
                finally:
                    os.close(contender)
                return {"doctor": "ok", "refresh": refresh, "reconcile": "ok"}

            module_globals["_refresh_under_lock"] = assert_refresh_lock
            module_globals["_post_switch"] = assert_health_unlocked
            module_globals["_systemctl"] = lambda _action: None
            try:
                code, result = self.updater["update"](ROOT, "HEAD", 0, state, install)
            finally:
                module_globals["_refresh_under_lock"] = original_refresh
                module_globals["_post_switch"] = original_post_switch
                module_globals["_systemctl"] = original_systemctl
            self.assertEqual(code, 0, result)

    def test_unsupported_state_writes_status_outside_state_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            install = root / "install"
            with PiStore(state) as store:
                store.conn.execute("UPDATE control_meta SET schema_name='unsupported'")
            code, result = self.updater["update"](ROOT, "HEAD", 0, state, install)
            self.assertEqual(code, self.updater["EXIT_UNSUPPORTED_STATE"])
            self.assertEqual(result["state"], "failed")
            self.assertFalse((state / "update-status.json").exists())
            self.assertTrue((install / "update-status.json").is_file())

    def test_health_failure_keeps_candidate_current_and_preserves_previous_verified_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            install = root / "install"
            with PiStore(state):
                pass
            old, _identity = self._install_verified_current(install)
            marker_before = (install / "last-known-good.json").read_bytes()
            verified_before = (install / "verified" / "deploy-old.json").read_bytes()
            module_globals = self.updater["update"].__globals__
            old_systemctl = module_globals["_systemctl"]
            old_post_switch = module_globals["_post_switch"]
            module_globals["_systemctl"] = lambda _action: None
            module_globals["_post_switch"] = lambda _current, _wait, _refresh: (_ for _ in ()).throw(RuntimeError("health failed"))
            try:
                code, result = self.updater["update"](ROOT, "HEAD", 0, state, install)
            finally:
                module_globals["_systemctl"] = old_systemctl
                module_globals["_post_switch"] = old_post_switch
            self.assertEqual(code, self.updater["EXIT_NEEDS_ATTENTION"])
            current = (install / "current").resolve()
            self.assertNotEqual(current.name, old.name)
            self.assertTrue(old.is_dir())
            self.assertEqual(marker_before, (install / "last-known-good.json").read_bytes())
            self.assertEqual(verified_before, (install / "verified" / "deploy-old.json").read_bytes())
            self.assertEqual(result["state"], "needs_attention")
            self.assertFalse((install / "stable-updater.json").exists())

    def test_update_establishes_recovery_marker_before_switching_from_verified_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            install = root / "install"
            with PiStore(state):
                pass
            old, _identity = self._install_verified_current(install)
            (install / "last-known-good.json").unlink()
            module_globals = self.updater["update"].__globals__
            old_systemctl = module_globals["_systemctl"]
            old_post_switch = module_globals["_post_switch"]
            module_globals["_systemctl"] = lambda _action: None
            module_globals["_post_switch"] = lambda _current, _wait, _refresh: (_ for _ in ()).throw(RuntimeError("health failed"))
            try:
                code, result = self.updater["update"](ROOT, "HEAD", 0, state, install)
            finally:
                module_globals["_systemctl"] = old_systemctl
                module_globals["_post_switch"] = old_post_switch

            self.assertEqual(code, self.updater["EXIT_NEEDS_ATTENTION"])
            marker = json.loads((install / "last-known-good.json").read_text())
            self.assertEqual(marker["deployment"], old.name)
            self.assertTrue(result["recoveryAvailable"])

    def test_successful_update_can_recover_from_an_unverified_current_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            install = root / "install"
            with PiStore(state):
                pass
            install.mkdir(mode=0o700)
            commit = self.updater["_git"](ROOT, "rev-parse", "HEAD")
            candidate = self.updater["_stage_candidate"](ROOT, commit, install)
            unverified = install / "deploy-unverified"
            os.replace(candidate["staging"], unverified)
            (install / "current").symlink_to(unverified.name)
            module_globals = self.updater["update"].__globals__
            old_systemctl = module_globals["_systemctl"]
            old_post_switch = module_globals["_post_switch"]
            module_globals["_systemctl"] = lambda _action: None
            module_globals["_post_switch"] = lambda _current, _wait, refresh: {"doctor": "ok", "refresh": refresh, "reconcile": "ok"}
            try:
                code, result = self.updater["update"](ROOT, "HEAD", 0, state, install)
            finally:
                module_globals["_systemctl"] = old_systemctl
                module_globals["_post_switch"] = old_post_switch

            self.assertEqual(code, 0, result)
            self.assertFalse(result["recoveryAvailable"])
            self.assertFalse((install / "last-known-good.json").exists())

    def test_semantic_health_rejects_needs_attention_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            with PiStore(state) as store:
                create_operation(store, kind="runtime.refresh", idempotency_key="needs-attention-health", request={"workstreamId": "ws_" + "a" * 32}, state="needs_attention", step="attention")
            with self.assertRaisesRegex(RuntimeError, "needs_attention"):
                self.updater["_semantic_state_health"](state)

    def test_successful_update_records_verification_then_manual_recovery_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            install = root / "install"
            with PiStore(state):
                pass
            old, _identity = self._install_verified_current(install)
            module_globals = self.updater["update"].__globals__
            old_systemctl = module_globals["_systemctl"]
            old_post_switch = module_globals["_post_switch"]
            module_globals["_systemctl"] = lambda _action: None
            module_globals["_post_switch"] = lambda _current, _wait, refresh: {"doctor": "ok", "refresh": refresh, "reconcile": "ok"}
            try:
                code, result = self.updater["update"](ROOT, "HEAD", 0, state, install)
            finally:
                module_globals["_systemctl"] = old_systemctl
                module_globals["_post_switch"] = old_post_switch
            self.assertEqual(code, 0, result)
            self.assertTrue(result["recoveryAvailable"])
            self.assertEqual(result["lastKnownGood"]["deployment"], old.name)
            self.assertTrue((install / "verified" / f"{(install / 'current').resolve().name}.json").is_file())
            self.assertTrue((install / "last-known-good.json").is_file())

    def test_recovery_health_failure_leaves_selected_previous_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            install = root / "install"
            with PiStore(state):
                pass
            old, _identity = self._install_verified_current(install)
            commit = self.updater["_git"](ROOT, "rev-parse", "HEAD")
            candidate = self.updater["_stage_candidate"](ROOT, commit, install)
            selected_current = install / "deploy-current"
            os.replace(candidate["staging"], selected_current)
            (install / "current").unlink()
            (install / "current").symlink_to(selected_current.name)
            marker_before = (install / "last-known-good.json").read_bytes()
            module_globals = self.updater["recover_previous"].__globals__
            old_systemctl = module_globals["_systemctl"]
            old_post_switch = module_globals["_post_switch"]
            module_globals["_systemctl"] = lambda _action: None
            module_globals["_post_switch"] = lambda _current, _wait, _refresh: (_ for _ in ()).throw(RuntimeError("recovery health failed"))
            try:
                code, result = self.updater["recover_previous"](state, install, 0)
            finally:
                module_globals["_systemctl"] = old_systemctl
                module_globals["_post_switch"] = old_post_switch
            self.assertEqual(code, self.updater["EXIT_NEEDS_ATTENTION"])
            self.assertEqual((install / "current").resolve().name, old.name)
            self.assertEqual(marker_before, (install / "last-known-good.json").read_bytes())
            self.assertEqual(result["state"], "needs_attention")

    def test_tampered_recovery_marker_is_refused_without_switching_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            install = root / "install"
            with PiStore(state):
                pass
            _old, _identity = self._install_verified_current(install)
            marker = json.loads((install / "last-known-good.json").read_text())
            marker["database"]["sha256"] = "f" * 64
            (install / "last-known-good.json").write_text(json.dumps(marker) + "\n")
            os.chmod(install / "last-known-good.json", 0o600)
            current_before = (install / "current").resolve()
            code, result = self.updater["recover_previous"](state, install, 0)
            self.assertEqual(code, self.updater["EXIT_FAILED"])
            self.assertEqual(current_before, (install / "current").resolve())
            self.assertEqual(result["state"], "failed")

    def test_first_v1_update_has_no_compatible_recovery_predecessor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            install = root / "install"
            with PiStore(state):
                pass
            module_globals = self.updater["update"].__globals__
            old_systemctl = module_globals["_systemctl"]
            old_post_switch = module_globals["_post_switch"]
            module_globals["_systemctl"] = lambda _action: None
            module_globals["_post_switch"] = lambda _current, _wait, refresh: {"doctor": "ok", "refresh": refresh, "reconcile": "ok"}
            try:
                code, result = self.updater["update"](ROOT, "HEAD", 0, state, install)
            finally:
                module_globals["_systemctl"] = old_systemctl
                module_globals["_post_switch"] = old_post_switch
            self.assertEqual(code, 0, result)
            self.assertFalse(result["recoveryAvailable"])
            self.assertEqual(result["recoveryReason"], "no compatible last-known-good deployment")
            self.assertFalse((install / "last-known-good.json").exists())

    def test_archive_reset_failure_after_state_move_keeps_archive_and_does_not_restore_old_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            subprocess.run(["git", "clone", "--no-hardlinks", str(ROOT), str(repo)], check=True, capture_output=True, text=True)
            state = root / "state"
            install = root / "install"
            with PiStore(state):
                pass
            install.mkdir(mode=0o700)
            module_globals = self.updater["archive_reset_state"].__globals__
            old_systemctl = module_globals["_systemctl"]
            old_initialize = module_globals["_initialize_candidate_state"]
            module_globals["_systemctl"] = lambda _action: None
            module_globals["_initialize_candidate_state"] = lambda _bundle, _state: (_ for _ in ()).throw(RuntimeError("initializer failed"))
            previous_quiescent = os.environ.get("PISEC_BROKER_QUIESCENT")
            os.environ["PISEC_BROKER_QUIESCENT"] = "1"
            try:
                code, result = self.updater["archive_reset_state"](repo, "HEAD", 0, state, install)
            finally:
                module_globals["_systemctl"] = old_systemctl
                module_globals["_initialize_candidate_state"] = old_initialize
                if previous_quiescent is None:
                    os.environ.pop("PISEC_BROKER_QUIESCENT", None)
                else:
                    os.environ["PISEC_BROKER_QUIESCENT"] = previous_quiescent
            self.assertEqual(code, self.updater["EXIT_FAILED"], result)
            self.assertTrue(sorted(state.parent.glob("state.archive-*")))
            self.assertFalse(state.exists())
            self.assertIn("archive=", result["error"])

    def test_install_updater_only_changes_stable_updater_and_manifest_from_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            repo = root / "repo"
            subprocess.run(["git", "clone", "--no-hardlinks", str(ROOT), str(repo)], check=True, capture_output=True, text=True)
            code, result = self.updater["install_updater_only"](repo, "HEAD", install, root / "state")
            self.assertEqual(code, 0)
            stable = install / "bin" / "pisec-update"
            manifest = install / "stable-updater.json"
            self.assertTrue(stable.is_file())
            self.assertTrue(manifest.is_file())
            document = json.loads(manifest.read_text())
            self.assertEqual(document["sourceCommit"], result["sourceCommit"])
            self.assertEqual(document["fileSha256"], hashlib.sha256(stable.read_bytes()).hexdigest())
            self.assertFalse((install / "current").exists())
            self.assertFalse((install / "update-status.json").exists())

    def test_archive_reset_moves_opaque_state_and_initializes_fresh_v1_without_replacing_stable_updater(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            subprocess.run(["git", "clone", "--no-hardlinks", str(ROOT), str(repo)], check=True, capture_output=True, text=True)
            state = root / "state"
            install = root / "install"
            with PiStore(state) as store:
                store.conn.execute("CREATE TABLE opaque_previous_state(value TEXT)")
                store.conn.execute("INSERT INTO opaque_previous_state VALUES('preserve')")
            stable = install / "bin" / "pisec-update"
            install.mkdir(mode=0o700)
            stable.parent.mkdir(mode=0o700)
            stable.write_text("old stable updater\n")
            os.chmod(stable, 0o700)
            stable_manifest = {"schemaVersion": 1, "sourceCommit": "0" * 40, "sourceTree": "1" * 40, "fileSha256": hashlib.sha256(stable.read_bytes()).hexdigest()}
            stable_manifest["manifestSha256"] = self.updater["_document_digest"](stable_manifest, "manifestSha256")
            (install / "stable-updater.json").write_text(json.dumps(stable_manifest) + "\n")
            os.chmod(install / "stable-updater.json", 0o600)
            before = stable.read_bytes()
            module_globals = self.updater["archive_reset_state"].__globals__
            old_systemctl = module_globals["_systemctl"]
            old_post_switch = module_globals["_post_switch"]
            module_globals["_systemctl"] = lambda _action: None
            module_globals["_post_switch"] = lambda _current, _wait, refresh: {"doctor": "ok", "refresh": refresh, "reconcile": "ok"}
            previous_quiescent = os.environ.get("PISEC_BROKER_QUIESCENT")
            os.environ["PISEC_BROKER_QUIESCENT"] = "1"
            try:
                code, result = self.updater["archive_reset_state"](repo, "HEAD", 0, state, install)
            finally:
                module_globals["_systemctl"] = old_systemctl
                module_globals["_post_switch"] = old_post_switch
                if previous_quiescent is None:
                    os.environ.pop("PISEC_BROKER_QUIESCENT", None)
                else:
                    os.environ["PISEC_BROKER_QUIESCENT"] = previous_quiescent
            self.assertEqual(code, 0, result)
            self.assertFalse((install / "last-known-good.json").exists())
            self.assertEqual(stable.read_bytes(), before)
            archives = sorted(state.parent.glob("state.archive-*"))
            self.assertEqual(len(archives), 1)
            with sqlite3.connect(archives[0] / "control.db") as connection:
                self.assertIsNotNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='opaque_previous_state'").fetchone())
            with PiStore(state) as store:
                self.assertEqual(store.conn.execute("SELECT schema_name,schema_version FROM control_meta").fetchone()[:], ("pisec-core-v1", 1))


if __name__ == "__main__":
    unittest.main()
