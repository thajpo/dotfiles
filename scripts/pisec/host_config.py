"""Small atomic installers for user-owned Pisec configuration files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets


COLLIE_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
def _port_from_environment(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        port = int(raw, 10)
    except ValueError as error:
        raise ValueError(f"{name} must be a numeric TCP port") from error
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port

def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if temporary.exists():
            temporary.unlink()


def seed_config(source: Path, destination: Path) -> bool:
    if destination.exists() or destination.is_symlink():
        return False
    _atomic_write(destination, source.read_text(), 0o600)
    return True


def patch_herdr_config(path: Path) -> None:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    desired = {("session", "resume_agents_on_restore"): "true", ("experimental", "pane_history"): "false"}
    sections: dict[str, list[int]] = {}
    current: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            sections.setdefault(current, []).append(index)
    for (section, key), value in desired.items():
        section_indexes = [index for index, line in enumerate(lines) if line.strip() == f"[{section}]"]
        if not section_indexes:
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend([f"[{section}]", f"{key} = {value}"])
            continue
        start = section_indexes[-1]
        end = next((index for index in range(start + 1, len(lines)) if lines[index].strip().startswith("[") and lines[index].strip().endswith("]")), len(lines))
        found = False
        for index in range(start + 1, end):
            stripped = lines[index].strip()
            if stripped.startswith(f"{key} ") or stripped.startswith(f"{key}="):
                lines[index] = f"{key} = {value}"
                found = True
                break
        if not found:
            lines.insert(end, f"{key} = {value}")
    _atomic_write(path, "\n".join(lines).rstrip() + "\n", 0o600)

def write_pisec_herdr_config(path: Path, *, shell_path: str) -> None:
    shell = Path(shell_path).resolve(strict=True)
    if not shell.is_file() or not os.access(shell, os.X_OK):
        raise ValueError("Pisec Herdr shell is not executable")
    text = (
        "onboarding = false\n\n"
        "[session]\n"
        "resume_agents_on_restore = true\n\n"
        "[terminal]\n"
        f"default_shell = {json.dumps(str(shell))}\n"
        'shell_mode = "non_login"\n\n'
        "[experimental]\n"
        "pane_history = false\n"
    )
    _atomic_write(path, text, 0o600)


def _validate_pisec_envelope(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Pisec config must be a JSON object")
    expected = {"schemaVersion", "fencePath", "harness", "workspace"}
    if set(value) != expected:
        unknown = sorted(set(value) - expected)
        missing = sorted(expected - set(value))
        detail = []
        if unknown:
            detail.append("unknown keys: " + ", ".join(unknown))
        if missing:
            detail.append("missing keys: " + ", ".join(missing))
        raise ValueError("Pisec config has unsupported exact shape" + (f" ({'; '.join(detail)})" if detail else ""))
    if value["schemaVersion"] != 3:
        raise ValueError("Pisec config schemaVersion must be 3")
    if not isinstance(value["fencePath"], str) or not value["fencePath"] or "\x00" in value["fencePath"]:
        raise ValueError("Pisec config fencePath is invalid")
    for name in ("harness", "workspace"):
        envelope = value[name]
        if not isinstance(envelope, dict) or set(envelope) != {"id", "config"}:
            raise ValueError(f"Pisec {name} adapter envelope is invalid")
        adapter_id = envelope["id"]
        if not isinstance(adapter_id, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", adapter_id) is None:
            raise ValueError(f"Pisec {name} adapter id is invalid")
        if not isinstance(envelope["config"], dict):
            raise ValueError(f"Pisec {name} adapter config is invalid")
    return value


def _atomic_write_pisec_config(path: Path, value: dict) -> None:
    _validate_pisec_envelope(value)
    _atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n", 0o600)


def patch_pisec_config(path: Path, *, real_omp_path: str, fence_path: str) -> None:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Pisec config is unavailable or invalid: {path}") from error
    legacy_keys = {"realOmpPath", "fencePath", "gateway", "modelRoles", "network"}
    if isinstance(value, dict) and set(value) == legacy_keys:
        gateway = value["gateway"]
        if not isinstance(gateway, dict) or set(gateway) != {"baseUrl", "tokenFile"}:
            raise ValueError("Pisec legacy gateway fields are invalid")
        if not isinstance(gateway["tokenFile"], str) or not gateway["tokenFile"] or "\x00" in gateway["tokenFile"]:
            raise ValueError("Pisec legacy gateway tokenFile is invalid")
        if not isinstance(value["modelRoles"], dict) or not isinstance(value["network"], dict):
            raise ValueError("Pisec legacy model or network configuration is invalid")
        gateway_port = _port_from_environment("PISEC_AUTH_GATEWAY_PORT", 4000)
        value = {
            "schemaVersion": 3,
            "fencePath": str(Path(fence_path).resolve(strict=True)),
            "harness": {
                "id": "omp",
                "config": {
                    "executablePath": str(Path(real_omp_path).resolve(strict=True)),
                    "gateway": {
                        "baseUrl": f"http://127.0.0.1:{gateway_port}",
                        "tokenFile": gateway["tokenFile"],
                    },
                    "modelRoles": value["modelRoles"],
                    "network": value["network"],
                },
            },
            "workspace": {
                "id": "herdr",
                "config": {
                    "sessionName": "pisec",
                    "socketPath": str((Path.home() / ".config" / "herdr" / "sessions" / "pisec" / "herdr.sock").absolute()),
                },
            },
        }
    else:
        value = _validate_pisec_envelope(value)
        gateway_port = _port_from_environment("PISEC_AUTH_GATEWAY_PORT", 4000)
        value["fencePath"] = str(Path(fence_path).resolve(strict=True))
        harness_config = value["harness"]["config"]
        if value["harness"]["id"] == "omp":
            if set(harness_config) != {"executablePath", "gateway", "modelRoles", "network"}:
                raise ValueError("Pisec OMP harness configuration fields are invalid")
            harness_config["executablePath"] = str(Path(real_omp_path).resolve(strict=True))
            gateway = harness_config["gateway"]
            if not isinstance(gateway, dict) or set(gateway) != {"baseUrl", "tokenFile"}:
                raise ValueError("Pisec OMP gateway configuration fields are invalid")
            gateway["baseUrl"] = f"http://127.0.0.1:{gateway_port}"
        workspace_config = value["workspace"]["config"]
        if value["workspace"]["id"] == "herdr":
            if set(workspace_config) != {"sessionName", "socketPath"} or workspace_config.get("sessionName") != "pisec":
                raise ValueError("Pisec Herdr workspace configuration fields are invalid")
            workspace_config["socketPath"] = str(Path(workspace_config["socketPath"]).expanduser().absolute())
    _atomic_write_pisec_config(path, value)

def write_collie_env(path: Path, *, host: str, trusted_user: str) -> None:
    if not COLLIE_HOST_RE.fullmatch(host) or len(host) > 253:
        raise ValueError("Collie host is invalid")
    if not trusted_user or "=" in trusted_user or any(char.isspace() or char in "\r\n\x00" for char in trusted_user):
        raise ValueError("Collie trusted user is invalid")
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            existing[key.strip()] = value.strip()
    managed = {
        "COLLIE_HOST": "127.0.0.1",
        "COLLIE_PORT": str(_port_from_environment("PISEC_COLLIE_PORT", 8787)),
        "COLLIE_SERVE_MODE": "https",
        "COLLIE_MULTI_SESSION": "on",
        "COLLIE_TRANSCRIPT": "on",
        "COLLIE_TRANSCRIPT_ROOT": "%h/.omp/agent/sessions,%h/.local/state/pisec/omp,%h/.local/state/pisec-personal/profiles",
        "COLLIE_TRUSTED_USER": trusted_user,
        "COLLIE_PUBLIC_HOSTS": host,
        "COLLIE_ALLOWED_ORIGINS": f"https://{host}",
    }
    existing.update(managed)
    text = "# Managed by dotfiles Pisec installer; unrelated keys are preserved.\n" + "\n".join(f"{key}={value}" for key, value in sorted(existing.items())) + "\n"
    _atomic_write(path, text, 0o600)

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="pisec-host-config")
    commands = parser.add_subparsers(dest="command", required=True)
    herdr = commands.add_parser("patch-herdr")
    herdr.add_argument("path", type=Path)
    pisec = commands.add_parser("patch-pisec")
    pisec.add_argument("path", type=Path)
    pisec.add_argument("real_omp_path")
    pisec.add_argument("fence_path")
    pisec_herdr = commands.add_parser("write-pisec-herdr")
    pisec_herdr.add_argument("path", type=Path)
    pisec_herdr.add_argument("shell_path")
    collie = commands.add_parser("collie-env")
    collie.add_argument("path", type=Path)
    collie.add_argument("host")
    collie.add_argument("trusted_user")
    args = parser.parse_args(argv)
    if args.command == "patch-herdr":
        patch_herdr_config(args.path)
    elif args.command == "write-pisec-herdr":
        write_pisec_herdr_config(args.path, shell_path=args.shell_path)
    elif args.command == "patch-pisec":
        patch_pisec_config(args.path, real_omp_path=args.real_omp_path, fence_path=args.fence_path)
    else:
        write_collie_env(args.path, host=args.host, trusted_user=args.trusted_user)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
