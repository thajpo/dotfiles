import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.pisec.adapters import AdapterRegistry
from scripts.pisec.broker import BrokerDispatcher
from scripts.pisec.doctor import _collie_route_ok, _funnel_is_disabled, run_doctor
from scripts.pisec.operations import create_operation
from scripts.pisec.pi_store import PiStore
from scripts.pisec.projects import register_project
from scripts.pisec.secretary import ensure_secretary
from tests.pisec_fixture import FixtureHarness, FixtureWorkspace, make_repo


class DoctorCollieTests(unittest.TestCase):
    def test_collie_route_requires_https_loopback_root_and_public_host(self):
        route = {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                "pisec.example.ts.net:443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:8787", "Extra": "ignored"}}
                }
            },
        }
        self.assertTrue(_collie_route_ok(json.dumps(route), "pisec.example.ts.net"))
        self.assertTrue(_collie_route_ok(json.dumps({key: value for key, value in route.items() if key != "TCP"}), "pisec.example.ts.net"))
        extra_route = json.loads(json.dumps(route))
        extra_route["Web"]["other.example.ts.net:443"] = route["Web"]["pisec.example.ts.net:443"]
        self.assertFalse(_collie_route_ok(json.dumps(extra_route), "pisec.example.ts.net"))
        route["TCP"]["443"]["HTTPS"] = False
        self.assertFalse(_collie_route_ok(json.dumps(route), "pisec.example.ts.net"))
        route["TCP"]["443"]["HTTPS"] = True
        route["Web"]["pisec.example.ts.net:8443"] = route["Web"].pop("pisec.example.ts.net:443")
        self.assertFalse(_collie_route_ok(json.dumps(route), "pisec.example.ts.net"))
        route["Web"]["pisec.example.ts.net:443"] = route["Web"].pop("pisec.example.ts.net:8443")
        route["Web"]["pisec.example.ts.net:443"]["Handlers"]["/"]["Proxy"] = "http://0.0.0.0:8787"
        self.assertFalse(_collie_route_ok(json.dumps(route), "pisec.example.ts.net"))

    def test_funnel_parser_fails_closed_for_enabled_or_invalid_state(self):
        self.assertFalse(_funnel_is_disabled("not json"))
        self.assertTrue(_funnel_is_disabled('{"AllowFunnel":{"pisec.example.ts.net:443":false}}'))
        self.assertFalse(_funnel_is_disabled('{"AllowFunnel":{"pisec.example.ts.net:443":true}}'))
        self.assertFalse(_funnel_is_disabled('{"funnel":{"enabled":true}}'))
        self.assertTrue(_funnel_is_disabled('{"TCP":{},"Web":{}}'))
        self.assertFalse(_funnel_is_disabled("funnel enabled"))

    def test_doctor_fails_active_error_binding_even_when_adapter_ids_and_generation_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            fence = root / "fence"
            executable = root / "fixture-agent"
            token = root / "gateway.token"
            socket = root / "workspace.sock"
            for path in (fence, executable, socket):
                path.write_text("fixture\n")
            token.write_text("g" * 48 + "\n")
            os.chmod(token, 0o600)
            os.chmod(socket, 0o600)
            harness = FixtureHarness(root)
            registry = AdapterRegistry()
            registry.register_harness(harness)
            with PiStore(root / "state") as store:
                workspace = FixtureWorkspace(root, store)
                registry.register_workspace(workspace)
                project = register_project(store, repo)
                opened = ensure_secretary(store, project["project_id"], harness, workspace)
                workstream_id = str(opened["workstream"]["workstream_id"])
                store.conn.execute(
                    "UPDATE workstreams SET provisioning_state='needs_attention',attention_reason='runtime refresh failed' WHERE workstream_id=?",
                    (workstream_id,),
                )
                store.conn.execute(
                    "UPDATE runtime_bindings SET observed_state='error',applied_generation_sha256=desired_generation_sha256,launch_generation_sha256=NULL,refresh_pending=0,refresh_operation_id=NULL,refresh_started_at=NULL WHERE workstream_id=?",
                    (workstream_id,),
                )
                config = {
                    "schemaVersion": 3,
                    "fencePath": str(fence),
                    "harness": {
                        "id": harness.manifest.adapter_id,
                        "config": {
                            "executablePath": str(executable),
                            "gateway": {"baseUrl": "http://127.0.0.1:4000", "tokenFile": str(token)},
                        },
                    },
                    "workspace": {
                        "id": workspace.manifest.adapter_id,
                        "config": {"sessionName": workspace.manifest.session_name, "socketPath": str(socket)},
                    },
                }

                result = run_doctor(store=store, config=config, registry=registry)
                store.conn.execute(
                    "DELETE FROM runtime_bindings WHERE workstream_id=?",
                    (workstream_id,),
                )
                store.conn.execute(
                    "UPDATE workstreams SET provisioning_state='bound',attention_reason=NULL WHERE workstream_id=?",
                    (workstream_id,),
                )
                missing = run_doctor(store=store, config=config, registry=registry)

            checks = {item["name"]: item for item in result["checks"]}
            self.assertFalse(result["ok"])
            self.assertEqual(checks[f"Binding {workstream_id}"]["status"], "error")
            self.assertEqual(checks[f"Binding generation {workstream_id}"]["status"], "error")
            missing_checks = {item["name"]: item for item in missing["checks"]}
            self.assertFalse(missing["ok"])
            self.assertEqual(missing_checks[f"Active binding {workstream_id}"]["status"], "error")
            self.assertEqual(missing_checks[f"Project supervisor {project['project_id']}"]["status"], "error")

    def test_doctor_negative_matrix_rejects_unusable_active_binding_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            make_repo(repo)
            fence = root / "fence"
            executable = root / "fixture-agent"
            token = root / "gateway.token"
            socket = root / "workspace.sock"
            for path in (fence, executable, socket):
                path.write_text("fixture\n")
            token.write_text("g" * 48 + "\n")
            os.chmod(token, 0o600)
            os.chmod(socket, 0o600)
            harness = FixtureHarness(root)
            registry = AdapterRegistry()
            registry.register_harness(harness)
            config = {
                "schemaVersion": 3,
                "fencePath": str(fence),
                "harness": {
                    "id": harness.manifest.adapter_id,
                    "config": {
                        "executablePath": str(executable),
                        "gateway": {"baseUrl": "http://127.0.0.1:4000", "tokenFile": str(token)},
                    },
                },
                "workspace": {
                    "id": "fixture-workspace",
                    "config": {"sessionName": "fixture-session", "socketPath": str(socket)},
                },
            }

            with PiStore(root / "state") as store:
                workspace = FixtureWorkspace(root, store)
                registry.register_workspace(workspace)
                project = register_project(store, repo)
                opened = ensure_secretary(store, project["project_id"], harness, workspace)
                workstream_id = str(opened["workstream"]["workstream_id"])
                dispatcher = BrokerDispatcher(
                    lambda: PiStore(root / "state"),
                    registry=registry,
                    harness=harness,
                    workspace=workspace,
                    config=config,
                )

                green = dispatcher.dispatch("admin", "system.doctor", {})
                self.assertTrue(green["ok"])

                cases = {
                    "needs_attention": {
                        "workstream": "UPDATE workstreams SET provisioning_state='needs_attention' WHERE workstream_id=?",
                        "binding": "UPDATE runtime_bindings SET observed_state='idle' WHERE workstream_id=?",
                    },
                    "error": {
                        "workstream": "UPDATE workstreams SET provisioning_state='bound' WHERE workstream_id=?",
                        "binding": "UPDATE runtime_bindings SET observed_state='error' WHERE workstream_id=?",
                    },
                    "missing": {
                        "workstream": "UPDATE workstreams SET provisioning_state='bound' WHERE workstream_id=?",
                        "binding": "UPDATE runtime_bindings SET observed_state='missing' WHERE workstream_id=?",
                    },
                    "starting": {
                        "workstream": "UPDATE workstreams SET provisioning_state='bound' WHERE workstream_id=?",
                        "binding": "UPDATE runtime_bindings SET observed_state='starting' WHERE workstream_id=?",
                    },
                    "stale-generation": {
                        "workstream": "UPDATE workstreams SET provisioning_state='bound' WHERE workstream_id=?",
                        "binding": "UPDATE runtime_bindings SET observed_state='idle',applied_generation_sha256=? WHERE workstream_id=?",
                    },
                    "reservation": {
                        "workstream": "UPDATE workstreams SET provisioning_state='bound' WHERE workstream_id=?",
                        "binding": "UPDATE runtime_bindings SET observed_state='idle',refresh_pending=1,refresh_operation_id='op_reservation',refresh_started_at='2026-08-25T00:00:00Z' WHERE workstream_id=?",
                    },
                    "missing-project-secretary": {
                        "workstream": "UPDATE workstreams SET provisioning_state='bound' WHERE workstream_id=?",
                        "binding": "UPDATE runtime_bindings SET observed_state='idle' WHERE workstream_id=?",
                    },
                    "absent-binding": {
                        "workstream": "UPDATE workstreams SET provisioning_state='bound' WHERE workstream_id=?",
                        "binding": "DELETE FROM runtime_bindings WHERE workstream_id=?",
                    },
                }
                for name, statements in cases.items():
                    store.conn.execute(
                        "UPDATE projects SET secretary_workstream_id=? WHERE project_id=?",
                        (workstream_id, project["project_id"]),
                    )
                    store.conn.execute(
                        "UPDATE workstreams SET provisioning_state='bound' WHERE workstream_id=?",
                        (workstream_id,),
                    )
                    store.conn.execute(
                        "UPDATE runtime_bindings SET observed_state='idle',refresh_pending=0,refresh_operation_id=NULL,refresh_started_at=NULL,launch_generation_sha256=NULL,applied_generation_sha256=desired_generation_sha256 WHERE workstream_id=?",
                        (workstream_id,),
                    )
                    if name == "missing-project-secretary":
                        store.conn.execute(
                            "UPDATE projects SET secretary_workstream_id=NULL WHERE project_id=?",
                            (project["project_id"],),
                        )
                    else:
                        store.conn.execute(statements["workstream"], (workstream_id,))
                        if name == "stale-generation":
                            store.conn.execute(statements["binding"], ("f" * 64, workstream_id))
                        elif name == "reservation":
                            operation, _ = create_operation(
                                store,
                                kind="runtime.refresh",
                                project_id=project["project_id"],
                                workstream_id=workstream_id,
                                idempotency_key=f"doctor-reservation-{name}",
                                request={"workstreamId": workstream_id},
                                state="applying",
                                step="reserved",
                            )
                            store.conn.execute(
                                "UPDATE runtime_bindings SET refresh_pending=1,refresh_operation_id=?,refresh_started_at='2026-08-25T00:00:00Z',launch_generation_sha256=desired_generation_sha256 WHERE workstream_id=?",
                                (operation.operation_id, workstream_id),
                            )
                        else:
                            store.conn.execute(statements["binding"], (workstream_id,))
                    result = dispatcher.dispatch("admin", "system.doctor", {})
                    self.assertFalse(result["ok"], name)
                    checks = {item["name"]: item for item in result["checks"]}
                    if name != "absent-binding":
                        expected_binding = "error" if name != "missing-project-secretary" else "ok"
                        self.assertEqual(checks[f"Binding {workstream_id}"]["status"], expected_binding, name)
                    if name in {"absent-binding", "missing-project-secretary"}:
                        if name == "absent-binding":
                            self.assertEqual(checks[f"Active binding {workstream_id}"]["status"], "error", name)
                        self.assertEqual(checks[f"Project supervisor {project['project_id']}"]["status"], "error", name)


if __name__ == "__main__":
    unittest.main()
