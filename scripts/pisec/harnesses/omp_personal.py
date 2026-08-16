"""CWD-bound fenced runtime for personal OMP agents managed by Herdr."""

from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Any, Mapping

from ..config import load_config
from ..fsutil import _atomic_write, _secure_secret, _secure_tree
from ..models import InvalidRequestError, PisecError, canonical_json
from .omp import _copy_user_surface, _provider_ids


def default_personal_state_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "pisec-personal"


def _canonical_cwd(value: Path | str) -> Path:
    path = Path(value)
    try:
        info = path.lstat()
        canonical = path.resolve(strict=True)
    except OSError as error:
        raise PisecError("personal agent working directory is unavailable") from error
    if not path.is_absolute() or canonical != path or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PisecError("personal agent working directory must be a canonical directory")
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise PisecError("personal agent working directory is not owner-controlled")
    return canonical


def _safe_copy_herdr_extension(agent_dir: Path) -> None:
    source = Path.home() / ".omp" / "agent" / "extensions" / "herdr-agent-state.ts"
    if not source.exists() and not source.is_symlink():
        return
    try:
        resolved = source.resolve(strict=True)
        info = resolved.lstat()
    except OSError as error:
        raise PisecError("Herdr OMP integration is unavailable") from error
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise PisecError("Herdr OMP integration is unsafe")
    target = agent_dir / "extensions" / "herdr-agent-state.ts"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(target, resolved.read_text(), mode=0o600)


def _sanitized_git_config(home: Path) -> None:
    document = configparser.ConfigParser()
    values: dict[str, str] = {}
    executable = shutil.which("git", path=os.defpath)
    if executable is not None:
        environment = {"HOME": str(Path.home()), "PATH": os.defpath, "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        for key in ("user.name", "user.email"):
            result = subprocess.run([executable, "config", "--global", "--get", key], env=environment, text=True, capture_output=True, timeout=5, check=False)
            value = result.stdout.strip()
            if result.returncode == 0 and value and len(value) <= 512 and "\x00" not in value and "\n" not in value:
                values[key.split(".", 1)[1]] = value
    if values:
        document["user"] = values
    output = []
    for section in document.sections():
        output.append(f"[{section}]")
        output.extend(f"\t{key} = {value}" for key, value in document[section].items())
    _atomic_write(home / ".gitconfig", "\n".join(output) + ("\n" if output else ""), mode=0o600)


def materialize_personal_runtime(
    cwd_value: Path | str,
    *,
    state_root: Path | str | None = None,
    config: Mapping[str, Any] | None = None,
    policy_renderer: Any = None,
) -> dict[str, str]:
    cwd = _canonical_cwd(cwd_value)
    root = Path(state_root) if state_root is not None else default_personal_state_root()
    profile_id = hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()[:32]
    profile_root = root / "profiles" / profile_id
    personal_home = profile_root / "home"
    agent_dir = personal_home / ".omp" / "agent"
    _secure_tree(root, agent_dir)
    metadata_path = profile_root / "profile.json"
    metadata = {"version": 1, "profileId": profile_id, "canonicalCwd": str(cwd)}
    if metadata_path.exists() or metadata_path.is_symlink():
        try:
            existing = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise PisecError("personal runtime profile metadata is invalid") from error
        if existing != metadata:
            raise PisecError("personal runtime profile identity drifted")
    else:
        _atomic_write(metadata_path, canonical_json(metadata) + "\n")

    _copy_user_surface(agent_dir)
    _safe_copy_herdr_extension(agent_dir)
    selected_config = dict(config) if config is not None else load_config()
    harness_config = selected_config["harness"]["config"]
    gateway = harness_config["gateway"]
    gateway_token = _secure_secret(Path(gateway["tokenFile"]))
    providers = {
        provider: {
            "baseUrl": gateway["baseUrl"],
            "apiKey": gateway_token,
            "transport": "pi-native",
        }
        for provider in _provider_ids(harness_config["modelRoles"])
    }
    _atomic_write(agent_dir / "models.yml", json.dumps({"providers": providers}, indent=2, sort_keys=True) + "\n")
    overlay = {
        "setupVersion": 1,
        "modelRoles": harness_config["modelRoles"],
        "mcp": {"enableProjectConfig": True},
        "web_search": {"enabled": True},
        "providers": {"webSearchOrder": ["duckduckgo"], "webSearchTimeoutSeconds": 30},
    }
    overlay_path = agent_dir / "config.yml"
    _atomic_write(overlay_path, json.dumps(overlay, indent=2, sort_keys=True) + "\n")
    _sanitized_git_config(personal_home)
    for directory in (personal_home / ".omp" / "run", personal_home / ".cache", personal_home / ".config", personal_home / ".local" / "share", personal_home / ".local" / "state", personal_home / "tmp"):
        _secure_tree(root, directory)

    if policy_renderer is None:
        from ..fence import render_personal_policy
        renderer = render_personal_policy
    else:
        renderer = policy_renderer
    policy_path, policy_digest = renderer(
        root,
        profile_id,
        cwd,
        personal_home,
        selected_config,
        harness_home=agent_dir,
        adapter_replacements={
            "HARNESS_EXECUTABLE": harness_config["executablePath"],
            "HARNESS_NATIVES": Path.home() / ".omp" / "natives",
            "WORKSPACE_CONFIG": Path.home() / ".config" / "herdr",
        },
    )
    return {
        "profile_id": profile_id,
        "canonical_cwd": str(cwd),
        "personal_home": str(personal_home),
        "omp_agent_dir": str(agent_dir),
        "omp_overlay_path": str(overlay_path),
        "fence_policy_path": str(policy_path),
        "fence_policy_sha256": policy_digest,
        "real_omp_path": str(Path(harness_config["executablePath"])),
        "fence_path": str(Path(selected_config["fencePath"])),
    }


def _validate_original_args(arguments: list[str]) -> None:
    if not arguments:
        return
    if len(arguments) == 1 and arguments[0].startswith("--resume=") and arguments[0] != "--resume=":
        return
    if len(arguments) == 2 and arguments[0] == "--resume" and arguments[1]:
        return
    raise InvalidRequestError("Herdr supplied unsupported personal OMP arguments")


def _isolated_resume_args(arguments: list[str], agent_dir_value: str) -> list[str]:
    if not arguments:
        return []
    inline = len(arguments) == 1
    raw_value = arguments[0].split("=", 1)[1] if inline else arguments[1]
    source = Path(raw_value)
    if not source.is_absolute():
        return list(arguments)
    agent_dir = Path(agent_dir_value).resolve(strict=True)
    try:
        canonical = source.resolve(strict=True)
        info = source.lstat()
    except OSError as error:
        raise PisecError("personal OMP resume session is unavailable") from error
    if canonical != source or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise PisecError("personal OMP resume session is unsafe")
    if canonical.is_relative_to(agent_dir):
        isolated = canonical
    else:
        legacy_root = (Path.home() / ".omp" / "agent" / "sessions").resolve(strict=True)
        if not canonical.is_relative_to(legacy_root):
            raise PisecError("personal OMP resume session escapes isolated or legacy session storage")
        if info.st_size > 64 * 1024 * 1024:
            raise PisecError("personal OMP resume session is too large to migrate")
        isolated = agent_dir / "sessions" / canonical.relative_to(legacy_root)
        isolated.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = agent_dir
        for part in isolated.relative_to(agent_dir).parts[:-1]:
            current = current / part
            current_info = current.lstat()
            if stat.S_ISLNK(current_info.st_mode) or not stat.S_ISDIR(current_info.st_mode) or current_info.st_uid != os.geteuid():
                raise PisecError("personal OMP isolated session directory is unsafe")
            os.chmod(current, 0o700)
        if isolated.exists() or isolated.is_symlink():
            isolated_info = isolated.lstat()
            if stat.S_ISLNK(isolated_info.st_mode) or not stat.S_ISREG(isolated_info.st_mode) or isolated_info.st_uid != os.geteuid() or isolated_info.st_mode & 0o022:
                raise PisecError("personal OMP isolated resume session is unsafe")
        else:
            _atomic_write(isolated, canonical.read_text(), mode=0o600)
    return [f"--resume={isolated}"] if inline else ["--resume", str(isolated)]


def _secure_executable(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise PisecError("personal runtime executable is missing") from error
    if not path.is_absolute() or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022 or not os.access(path, os.X_OK):
        raise PisecError("personal runtime executable is unsafe")


def main() -> None:
    try:
        _validate_original_args(sys.argv[1:])
        artifacts = materialize_personal_runtime(Path.cwd().resolve(strict=True))
        launch_arguments = _isolated_resume_args(sys.argv[1:], artifacts["omp_agent_dir"])
        real_omp = Path(artifacts["real_omp_path"])
        fence = Path(artifacts["fence_path"])
        _secure_executable(real_omp)
        _secure_executable(fence)
        environment: dict[str, str] = {
            "HOME": artifacts["personal_home"],
            "USER": os.environ.get("USER", ""),
            "LOGNAME": os.environ.get("LOGNAME", ""),
            "SHELL": "/bin/bash",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PI_CODING_AGENT_DIR": artifacts["omp_agent_dir"],
            "HERDR_AGENT": "omp",
            "GIT_CONFIG_GLOBAL": str(Path(artifacts["personal_home"]) / ".gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "TMPDIR": str(Path(artifacts["personal_home"]) / "tmp"),
            "XDG_CACHE_HOME": str(Path(artifacts["personal_home"]) / ".cache"),
            "XDG_CONFIG_HOME": str(Path(artifacts["personal_home"]) / ".config"),
            "XDG_DATA_HOME": str(Path(artifacts["personal_home"]) / ".local" / "share"),
            "XDG_STATE_HOME": str(Path(artifacts["personal_home"]) / ".local" / "state"),
        }
        for key in ("TERM", "COLORTERM", "LANG", "TZ"):
            value = os.environ.get(key)
            if value:
                environment[key] = value
        for key, value in os.environ.items():
            if (key.startswith("LC_") or key.startswith("HERDR_")) and value and len(value) <= 4096 and "\x00" not in value:
                environment[key] = value
        argv = [
            str(fence),
            "--settings",
            artifacts["fence_policy_path"],
            "--",
            str(real_omp),
            "--config",
            artifacts["omp_overlay_path"],
            *launch_arguments,
        ]
        os.execve(fence, argv, environment)
    except (InvalidRequestError, PisecError, OSError) as error:
        print(f"pisec personal omp shim: {error}", file=sys.stderr)
        raise SystemExit(126) from error


if __name__ == "__main__":
    main()
