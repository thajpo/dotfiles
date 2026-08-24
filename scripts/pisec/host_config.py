"""Small atomic installers for user-owned Pisec configuration files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
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


HERDR_PLUGIN_KEYS_START = "# >>> dotfiles Herdr plugin keys >>>"
HERDR_PLUGIN_KEYS_END = "# <<< dotfiles Herdr plugin keys <<<"
HERDR_PLUGIN_KEYS = (
    ("prefix+shift+e", "plugin:chmarax.herdr-nvim:toggle"),
    ("prefix+shift+f", "plugin:chmarax.herdr-nvim:pick-file"),
    ("prefix+shift+v", "plugin:persiyanov.reviewr:toggle"),
)


def patch_herdr_config(path: Path) -> None:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    if HERDR_PLUGIN_KEYS_START in lines:
        start = lines.index(HERDR_PLUGIN_KEYS_START)
        try:
            end = lines.index(HERDR_PLUGIN_KEYS_END, start + 1)
        except ValueError as error:
            raise ValueError("Herdr plugin key block is incomplete") from error
        del lines[start : end + 1]
        while lines and lines[-1] == "":
            lines.pop()
    elif HERDR_PLUGIN_KEYS_END in lines:
        raise ValueError("Herdr plugin key block is incomplete")
    desired = {
        ("keys", "prefix"): '"ctrl+a"',
        ("session", "resume_agents_on_restore"): "false",
        ("experimental", "pane_history"): "false",
    }
    for (section, key), value in desired.items():
        section_indexes = [index for index, line in enumerate(lines) if line.strip() == f"[{section}]"]
        if not section_indexes:
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend([f"[{section}]", f"{key} = {value}"])
            continue
        start = section_indexes[-1]
        end = next(
            (
                index
                for index in range(start + 1, len(lines))
                if lines[index].strip().startswith("[") and lines[index].strip().endswith("]")
            ),
            len(lines),
        )
        found = False
        for index in range(start + 1, end):
            stripped = lines[index].strip()
            if stripped.startswith(f"{key} ") or stripped.startswith(f"{key}="):
                lines[index] = f"{key} = {value}"
                found = True
                break
        if not found:
            lines.insert(end, f"{key} = {value}")
    if lines and lines[-1] != "":
        lines.append("")
    lines.append(HERDR_PLUGIN_KEYS_START)
    for key, command in HERDR_PLUGIN_KEYS:
        lines.extend(
            (
                "[[keys.command]]",
                f'key = "{key}"',
                f'command = "{command}"',
                "",
            )
        )
    lines.append(HERDR_PLUGIN_KEYS_END)
    _atomic_write(path, "\n".join(lines).rstrip() + "\n", 0o600)

def patch_bashrc(path: Path, *, bin_dir: str) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlink: {path}")
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = ""
    start = "# >>> Pisec OMP routing >>>"
    end = "# <<< Pisec OMP routing <<<"
    pattern = re.compile(rf"(?ms)^{re.escape(start)}\n.*?^{re.escape(end)}\n?")
    text = pattern.sub("", text)
    block = "\n".join(
        (
            start,
            f"export PATH={shlex.quote(bin_dir)}:\"$PATH\"",
            "omp() {",
            "  printf '%s\\n' 'Pisec owns project OMP sessions. Use \"pisec project open <repository>\" for project work or \"omp-admin\" for broad host work.' >&2",
            "  return 126",
            "}",
            end,
            "",
        )
    )
    _atomic_write(path, text.rstrip() + ("\n\n" if text.strip() else "") + block, 0o600)



def _validate_pisec_envelope(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Pisec config must be a JSON object")
    expected = {"schemaVersion", "fencePath", "harness", "workspace"}
    optional = {"workerRouting", "workerHarnesses"}
    if not expected.issubset(value) or set(value) - expected - optional:
        unknown = sorted(set(value) - expected - optional)
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
    worker_harnesses = value.get("workerHarnesses")
    if worker_harnesses is not None:
        if not isinstance(worker_harnesses, dict):
            raise ValueError("Pisec workerHarnesses is invalid")
        for adapter_id, envelope in worker_harnesses.items():
            if not isinstance(adapter_id, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", adapter_id) is None or not isinstance(envelope, dict) or envelope.get("id") != adapter_id or set(envelope) != {"id", "config"} or not isinstance(envelope["config"], dict):
                raise ValueError("Pisec worker harness envelope is invalid")
    worker_routing = value.get("workerRouting")
    if worker_routing is not None and (not isinstance(worker_routing, dict) or set(worker_routing) != {"defaultModel", "fallbackHarness", "routes"}):
        raise ValueError("Pisec workerRouting is invalid")
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
                    "sessionName": "main",
                    "socketPath": str((Path.home() / ".config" / "herdr" / "sessions" / "main" / "herdr.sock").absolute()),
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
        if value["workspace"]["id"] == "herdr":
            workspace_config = value["workspace"]["config"]
            if set(workspace_config) != {"sessionName", "socketPath"}:
                raise ValueError("Pisec Herdr workspace configuration fields are invalid")
            workspace_config["sessionName"] = "main"
            workspace_config["socketPath"] = str((Path.home() / ".config" / "herdr" / "sessions" / "main" / "herdr.sock").absolute())
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
        "COLLIE_MULTI_SESSION": "off",
        "COLLIE_TRANSCRIPT": "on",
        "COLLIE_TRANSCRIPT_ROOT": "%h/.config/herdr/sessions/main",
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
    collie = commands.add_parser("collie-env")
    collie.add_argument("path", type=Path)
    collie.add_argument("host")
    collie.add_argument("trusted_user")
    bashrc = commands.add_parser("patch-bashrc")
    bashrc.add_argument("path", type=Path)
    bashrc.add_argument("bin_dir")
    args = parser.parse_args(argv)
    if args.command == "patch-herdr":
        patch_herdr_config(args.path)
    elif args.command == "patch-bashrc":
        patch_bashrc(args.path, bin_dir=args.bin_dir)
    elif args.command == "patch-pisec":
        patch_pisec_config(args.path, real_omp_path=args.real_omp_path, fence_path=args.fence_path)
    else:
        write_collie_env(args.path, host=args.host, trusted_user=args.trusted_user)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
