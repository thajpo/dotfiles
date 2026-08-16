"""Adapter-neutral Pisec diagnostics and deployment policy checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .adapters import AdapterRegistry
from .models import NeedsAttentionError, canonical_json
from .pi_schema import MIGRATION_NAME, SCHEMA_NAME, SCHEMA_VERSION, schema_digest
from .pi_store import default_state_root


def _check(checks: list[dict[str, Any]], name: str, ok: bool, detail: str) -> None:
    checks.append({"name": name, "status": "ok" if ok else "error", "detail": detail})
def _path(checks: list[dict[str, Any]], name: str, value: Any, mode: int | None = None) -> None:
    try:
        if value is None or "\x00" in str(value):
            raise OSError("path is unavailable")
        path = Path(value).expanduser()
        if not str(path):
            raise OSError("path is unavailable")
        info = path.lstat()
        ok = path.is_absolute() and not stat.S_ISLNK(info.st_mode) and info.st_uid == os.geteuid() and (mode is None or stat.S_IMODE(info.st_mode) == mode)
        _check(checks, name, ok, str(path))
    except (OSError, TypeError, ValueError) as error:
        _check(checks, name, False, str(error)[:256])


def _command(name: str, *args: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        result = subprocess.run([name, *args], text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return 127, str(error)[:256]
    output = result.stdout if result.stdout else result.stderr
    return result.returncode, output.strip()


def _probe_status(url: str, headers: Mapping[str, str] | None = None, timeout: float = 5.0) -> tuple[int, str]:
    request = Request(url, headers=dict(headers or {}), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(64 * 1024).decode("utf-8", "replace")
    except HTTPError as error:
        try:
            body = error.read(64 * 1024).decode("utf-8", "replace")
        except OSError:
            body = ""
        return int(error.code), body
    except (OSError, URLError, ValueError):
        return 0, ""


def _collie_route_ok(raw: str, public_host: str) -> bool:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict) or not isinstance(public_host, str) or not public_host:
        return False
    web = value.get("Web")
    if not isinstance(web, dict) or set(web) != {f"{public_host}:443"}:
        return False
    route = web.get(f"{public_host}:443")
    if not isinstance(route, dict) or not isinstance(route.get("Handlers"), dict):
        return False
    handler = route["Handlers"].get("/")
    if not isinstance(handler, dict) or not isinstance(handler.get("Proxy"), str):
        return False
    try:
        from urllib.parse import urlsplit

        proxy = urlsplit(handler["Proxy"])
        if proxy.scheme != "http" or proxy.hostname != "127.0.0.1" or proxy.port is None or not 1 <= proxy.port <= 65535 or proxy.path not in {"", "/"} or proxy.query or proxy.fragment:
            return False
    except ValueError:
        return False
    tcp = value.get("TCP")
    if tcp is not None and (not isinstance(tcp, dict) or not isinstance(tcp.get("443"), dict) or tcp["443"].get("HTTPS") is not True):
        return False
    return True


def _funnel_is_disabled(raw: str) -> bool:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    allow = value.get("AllowFunnel")
    if isinstance(allow, bool):
        return not allow
    if isinstance(allow, dict):
        return all(item is False for item in allow.values())
    funnel = value.get("funnel")
    if isinstance(funnel, dict) and funnel.get("enabled") is True:
        return False
    return isinstance(value.get("TCP"), dict) and isinstance(value.get("Web"), dict)


def _deployment_checks(checks: list[dict[str, Any]]) -> None:
    probe_url = os.environ.get("PISEC_COLLIE_PROBE_URL")
    public_host = os.environ.get("COLLIE_PUBLIC_HOSTS") or os.environ.get("COLLIE_HOST")
    if probe_url:
        status, _ = _probe_status(probe_url)
        _check(checks, "Collie loopback health", status == 200, f"status={status}")
        if public_host:
            status, _ = _probe_status(probe_url, {"Tailscale-User-Login": "untrusted@example.invalid"})
            _check(checks, "Collie trusted-user gate", status == 403, f"status={status}")
            status, _ = _probe_status(probe_url, {"Host": "wrong.example.invalid"})
            _check(checks, "Collie host gate", status == 403, f"status={status}")
            status, _ = _probe_status(probe_url, {"Origin": "https://wrong.example.invalid"})
            _check(checks, "Collie origin gate", status == 403, f"status={status}")
    if public_host:
        status, output = _command("tailscale", "serve", "status", "--json")
        _check(checks, "Tailscale Serve route", status == 0 and _collie_route_ok(output, public_host), output[:256])
        status, output = _command("tailscale", "funnel", "status", "--json")
        _check(checks, "Tailscale Funnel disabled", status == 0 and _funnel_is_disabled(output), output[:256])


def run_doctor(store: Any = None, config: Mapping[str, Any] | None = None, registry: AdapterRegistry | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    selected = dict(config or {})
    root = Path(getattr(store, "state_root", default_state_root())) if store is not None else default_state_root()
    _path(checks, "Pisec state root", root, 0o700)
    selected_harness_id: str | None = None
    selected_workspace_id: str | None = None
    if isinstance(selected, Mapping):
        _check(checks, "Configuration schema", selected.get("schemaVersion") == 3, f"schemaVersion={selected.get('schemaVersion')}")
        _path(checks, "Fence configuration", selected.get("fencePath", ""))
        harness = selected.get("harness", {})
        workspace = selected.get("workspace", {})
        if isinstance(harness, Mapping):
            selected_harness_id = harness.get("id") if isinstance(harness.get("id"), str) else None
            _check(checks, "Configured harness", selected_harness_id is not None, str(harness.get("id", "missing")))
            harness_config = harness.get("config")
            if isinstance(harness_config, Mapping):
                _path(checks, "Harness executable", harness_config.get("executablePath", ""))
                gateway = harness_config.get("gateway")
                if isinstance(gateway, Mapping):
                    _path(checks, "Harness gateway token", gateway.get("tokenFile", ""), 0o600)
        else:
            _check(checks, "Configured harness", False, "invalid")
        if isinstance(workspace, Mapping):
            selected_workspace_id = workspace.get("id") if isinstance(workspace.get("id"), str) else None
            _check(checks, "Configured workspace", selected_workspace_id is not None, str(workspace.get("id", "missing")))
            workspace_config = workspace.get("config")
            if isinstance(workspace_config, Mapping):
                _path(checks, "Workspace socket", workspace_config.get("socketPath", ""), 0o600)
        else:
            _check(checks, "Configured workspace", False, "invalid")
    else:
        _check(checks, "Configuration schema", False, "configuration unavailable")

    if registry is None:
        _check(checks, "Adapter registry", False, "adapter registry unavailable")
        harness_ids: tuple[str, ...] = ()
        workspace_ids: tuple[str, ...] = ()
    else:
        harness_ids = registry.harness_ids()
        workspace_ids = registry.workspace_ids()
        _check(checks, "Harness adapter registry", selected_harness_id in harness_ids, canonical_json({"selected": selected_harness_id, "ids": harness_ids}))
        _check(checks, "Workspace adapter registry", selected_workspace_id in workspace_ids, canonical_json({"selected": selected_workspace_id, "ids": workspace_ids}))
        if selected_harness_id in harness_ids:
            try:
                for health in registry.resolve_harness(selected_harness_id).health_checks({}, {}):
                    _check(checks, f"Harness {selected_harness_id}: {health.name}", health.ok, health.detail)
            except Exception as error:
                _check(checks, f"Harness {selected_harness_id}: health", False, str(error)[:256])
        if selected_workspace_id in workspace_ids:
            try:
                for health in registry.resolve_workspace(selected_workspace_id).health_checks():
                    _check(checks, f"Workspace {selected_workspace_id}: {health.name}", health.ok, health.detail)
            except Exception as error:
                _check(checks, f"Workspace {selected_workspace_id}: health", False, str(error)[:256])

    schema_identity = None
    if store is not None:
        row = store.conn.execute("SELECT schema_name,schema_version,schema_sha256,migration_name FROM control_meta WHERE singleton=1").fetchone()
        if row is None:
            _check(checks, "Schema identity", False, "control metadata is missing")
        else:
            schema_identity = dict(row)
            expected = {"schema_name": SCHEMA_NAME, "schema_version": SCHEMA_VERSION, "schema_sha256": schema_digest(), "migration_name": MIGRATION_NAME}
            _check(checks, "Schema identity", schema_identity == expected, canonical_json(schema_identity))
        for row in store.conn.execute(
            "SELECT r.*,w.kind AS workstream_kind,w.desired_state AS workstream_desired_state,w.execution_profile AS workstream_execution_profile,w.worktree_path AS workstream_worktree_path,w.harness_id AS workstream_harness_id,w.workspace_adapter_id AS workstream_workspace_adapter_id "
            "FROM runtime_bindings r JOIN workstreams w USING(workstream_id) ORDER BY r.workstream_id"
        ):
            ids_ok = (
                registry is not None
                and row["harness_id"] in harness_ids
                and row["workspace_adapter_id"] in workspace_ids
                and row["harness_id"] == selected_harness_id
                and row["workspace_adapter_id"] == selected_workspace_id
                and row["harness_id"] == row["workstream_harness_id"]
                and row["workspace_adapter_id"] == row["workstream_workspace_adapter_id"]
            )
            _check(checks, f"Binding {row['workstream_id']}", ids_ok, f"harness={row['harness_id']} workspace={row['workspace_adapter_id']} state={row['observed_state']}")
            cleaned = row["workstream_desired_state"] == "retired" and row["observed_state"] == "stopped" and row["workspace_id"] is None and row["workspace_view_id"] is None and row["workspace_surface_id"] is None
            if ids_ok and registry is not None and not cleaned:
                binding = dict(row)
                workstream = {
                    "kind": row["workstream_kind"],
                    "worktree_path": row["workstream_worktree_path"],
                    "desired_state": row["workstream_desired_state"],
                    "execution_profile": row["workstream_execution_profile"],
                }
                try:
                    for health in registry.resolve_harness(str(row["harness_id"])).health_checks(binding, workstream):
                        _check(checks, f"Harness {row['harness_id']} {row['workstream_id']}: {health.name}", health.ok, health.detail)
                except Exception as error:
                    _check(checks, f"Harness {row['harness_id']} {row['workstream_id']}: health", False, str(error)[:256])
    _deployment_checks(checks)
    ok = all(item["status"] == "ok" for item in checks)
    return {
        "ok": ok,
        "checks": checks,
        "schema": {"name": SCHEMA_NAME, "version": SCHEMA_VERSION, "migration": MIGRATION_NAME, "digest": schema_digest(), "actual": schema_identity},
        "adapters": {"harness": harness_ids, "workspace": workspace_ids},
    }
