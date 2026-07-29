#!/usr/bin/env python3
"""Host-owned workspace policy, route, and context preparation for Pi."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

POLICY_PATH = pathlib.Path("~/.config/pi/repository-policy.json")
DEFAULT_WORKTREE_ROOT = pathlib.Path("~/.local/share/pi/worktrees")
DEFAULT_IMAGE = "pi-tool-sandbox:node22-bookworm-20260728"
ROUTE_ENV = "PI_TASK_ROUTE_FILE"
CAPABILITY_ENV = "PI_TASK_ROUTE_CAPABILITY"
ALLOWED_POLICY_KEYS = {
    "version",
    "defaultMode",
    "trustedRoots",
    "isolatedRoots",
    "controlPlaneRepositories",
    "protectedBranches",
    "worktreeRoot",
}
MODES = {"trusted-live", "isolated"}


class WorkspaceError(RuntimeError):
    pass


def run(command: list[str], cwd: pathlib.Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(pathlib.Path.home()),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise WorkspaceError(f"{' '.join(command)}: {detail}")
    return result


def atomic_write(path: pathlib.Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def canonical_existing(value: str, label: str) -> pathlib.Path:
    expanded = pathlib.Path(os.path.expanduser(value))
    if not expanded.is_absolute():
        raise WorkspaceError(f"{label} must be absolute after ~ expansion")
    try:
        return expanded.resolve(strict=True)
    except OSError as error:
        raise WorkspaceError(f"{label} does not resolve: {expanded}: {error}") from error


def canonical_output_root(value: str, label: str) -> pathlib.Path:
    expanded = pathlib.Path(os.path.expanduser(value))
    if not expanded.is_absolute():
        raise WorkspaceError(f"{label} must be absolute after ~ expansion")
    expanded.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    expanded.mkdir(parents=True, exist_ok=True, mode=0o700)
    return expanded.resolve(strict=True)


def within(root: pathlib.Path, candidate: pathlib.Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def policy_path() -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(str(POLICY_PATH)))


def fallback_policy() -> dict[str, Any]:
    return {
        "version": 1,
        "defaultMode": "isolated",
        "trustedRoots": [],
        "isolatedRoots": [],
        "controlPlaneRepositories": [],
        "protectedBranches": ["main", "master"],
        "worktreeRoot": str(pathlib.Path(os.path.expanduser(str(DEFAULT_WORKTREE_ROOT)))),
        "policyValid": False,
    }


def load_policy(path: pathlib.Path | None = None, *, fail_closed: bool = True) -> dict[str, Any]:
    source = path or policy_path()
    try:
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or source.is_symlink():
            raise WorkspaceError("policy must be a regular, non-symlink file")
        if info.st_uid != os.getuid():
            raise WorkspaceError("policy must be owned by the invoking user")
        if info.st_mode & 0o077:
            raise WorkspaceError("policy must not be accessible by group or other users")
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) - ALLOWED_POLICY_KEYS:
            raise WorkspaceError("policy has an invalid object shape or unknown keys")
        if raw.get("version") != 1 or raw.get("defaultMode") != "isolated":
            raise WorkspaceError("policy must be version 1 with defaultMode isolated")
        for key in ("trustedRoots", "isolatedRoots", "controlPlaneRepositories", "protectedBranches"):
            if not isinstance(raw.get(key), list) or not all(isinstance(item, str) and item for item in raw[key]):
                raise WorkspaceError(f"policy {key} must be a string array")
        canonical = dict(raw)
        canonical["trustedRoots"] = [str(canonical_existing(item, "trusted root")) for item in raw["trustedRoots"]]
        canonical["isolatedRoots"] = [str(canonical_existing(item, "isolated root")) for item in raw["isolatedRoots"]]
        canonical["controlPlaneRepositories"] = [
            str(canonical_existing(item, "control-plane repository")) for item in raw["controlPlaneRepositories"]
        ]
        canonical["worktreeRoot"] = str(canonical_output_root(raw["worktreeRoot"], "worktreeRoot"))
        canonical["policyValid"] = True
        canonical["policyHash"] = hashlib.sha256(source.read_bytes()).hexdigest()
        return canonical
    except (OSError, ValueError, json.JSONDecodeError, WorkspaceError) as error:
        if not fail_closed:
            if isinstance(error, WorkspaceError):
                raise
            raise WorkspaceError(f"invalid repository policy {source}: {error}") from error
        policy = fallback_policy()
        policy["policyError"] = str(error)
        policy["policyHash"] = "invalid"
        return policy


def classify(repository: pathlib.Path, policy: dict[str, Any]) -> tuple[str, bool]:
    repo = repository.resolve(strict=True)
    controls = [pathlib.Path(item) for item in policy["controlPlaneRepositories"]]
    isolated = [pathlib.Path(item) for item in policy["isolatedRoots"]]
    trusted = [pathlib.Path(item) for item in policy["trustedRoots"]]
    if any(repo == item for item in controls):
        return "trusted-live", True
    if any(within(item, repo) for item in isolated):
        return "isolated", False
    if any(within(item, repo) for item in trusted):
        return "trusted-live", False
    return "isolated", False


def git_output(repository: pathlib.Path, *args: str) -> str:
    return run(["git", *args], cwd=repository).stdout.strip()


def repository_root(cwd: pathlib.Path) -> pathlib.Path:
    result = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    return pathlib.Path(result.stdout.strip()).resolve(strict=True)


def process_start_ticks(pid: int) -> str:
    try:
        fields = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return fields[21]
    except (OSError, IndexError):
        return "unavailable"


def task_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(8)


def safe_component(value: str, fallback: str = "repo") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.")
    return (cleaned or fallback)[:32]


def create_linked_worktree(
    repository: pathlib.Path,
    worktree_root: pathlib.Path,
    session: str,
    starting_oid: str,
) -> tuple[pathlib.Path, str]:
    if within(repository, worktree_root) or within(worktree_root, repository):
        raise WorkspaceError("worktreeRoot must not overlap the source repository")
    lock_root = pathlib.Path.home() / ".local/share/pi/locks"
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_name = hashlib.sha256(str(repository).encode()).hexdigest()[:20] + ".lock"
    lock_path = lock_root / lock_name
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current_oid = git_output(repository, "rev-parse", "--verify", "HEAD^{commit}")
        if current_oid != starting_oid:
            raise WorkspaceError("repository HEAD changed while preparing the task worktree")
        status_text = git_output(repository, "status", "--porcelain=v1", "--untracked-files=all")
        if status_text:
            raise WorkspaceError("protected or detached checkout is dirty; refusing to stash, copy, or discard changes")
        branch = f"pi/{session}"
        destination = worktree_root / safe_component(repository.name) / session
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists():
            raise WorkspaceError(f"task worktree already exists: {destination}")
        run(["git", "worktree", "add", "-b", branch, str(destination), starting_oid], cwd=repository)
        task_root = repository_root(destination)
        if task_root != destination.resolve(strict=True):
            raise WorkspaceError("created task worktree canonical path does not match its destination")
        if git_output(task_root, "branch", "--show-current") != branch:
            raise WorkspaceError("created task worktree is on an unexpected branch")
        if git_output(task_root, "rev-parse", "HEAD^{commit}") != starting_oid:
            raise WorkspaceError("created task worktree moved from the recorded starting commit")
        if git_output(repository, "rev-parse", "HEAD^{commit}") != starting_oid or git_output(
            repository, "status", "--porcelain=v1", "--untracked-files=all"
        ):
            run(["git", "worktree", "remove", "--force", str(task_root)], cwd=repository, check=False)
            run(["git", "branch", "-D", branch], cwd=repository, check=False)
            raise WorkspaceError("protected checkout changed while preparing the task worktree")
        return task_root, branch


def safe_command_path(name: str, *, exclude: list[pathlib.Path] | None = None) -> pathlib.Path | None:
    excluded = [item.resolve(strict=False) for item in (exclude or [])]
    home = pathlib.Path.home().resolve()
    allowed = [
        pathlib.Path("/usr/bin"),
        pathlib.Path("/usr/local/bin"),
        home / ".nvm",
        home / ".npm-global",
        home / ".local/bin",
        home / ".local/share",
        home / ".bun/bin",
    ]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not pathlib.Path(entry).is_absolute():
            continue
        candidate = pathlib.Path(entry) / name
        try:
            resolved = candidate.resolve(strict=True)
            info = resolved.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
            continue
        if info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
            continue
        if not any(within(root.resolve(strict=False), resolved) for root in allowed):
            continue
        if any(within(root, resolved) for root in excluded):
            continue
        return resolved
    return None


def resolve_pi(self_path: pathlib.Path, cwd: pathlib.Path) -> pathlib.Path:
    resolved_self = self_path.resolve(strict=True)
    repo_result = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd.resolve(strict=True), check=False)
    excluded = [pathlib.Path(repo_result.stdout.strip()).resolve(strict=True)] if repo_result.returncode == 0 else []
    core_bin = pathlib.Path.home() / ".local/share/pi/core/node_modules/.bin"
    original = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{core_bin}{os.pathsep}{original}"
    try:
        candidate = safe_command_path("pi", exclude=excluded)
    finally:
        os.environ["PATH"] = original
    if not candidate or candidate == resolved_self:
        # Continue searching after excluding the wrapper itself.
        original = os.environ.get("PATH", "")
        parts = []
        for entry in original.split(os.pathsep):
            if not entry or not pathlib.Path(entry).is_absolute():
                continue
            try:
                if (pathlib.Path(entry) / "pi").resolve(strict=False) == resolved_self:
                    continue
            except OSError:
                continue
            parts.append(entry)
        os.environ["PATH"] = os.pathsep.join(parts)
        try:
            candidate = safe_command_path("pi", exclude=excluded)
        finally:
            os.environ["PATH"] = original
    if not candidate:
        raise WorkspaceError("no trusted underlying Pi executable found in host-owned installation paths")
    return candidate


def first_line(command: list[str]) -> str:
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=5, check=False)
        return result.stdout.strip().splitlines()[0][:300] if result.stdout.strip() else "unavailable"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def gpu_description() -> str:
    try:
        result = subprocess.run(
            ["/usr/bin/lspci"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        gpu_lines = [
            line[:300]
            for line in result.stdout.splitlines()
            if "VGA" in line or "3D" in line or "Display" in line
        ]
        return "; ".join(gpu_lines) if gpu_lines else "unavailable"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def host_context(route: dict[str, Any], pi_executable: pathlib.Path | None) -> str:
    os_release = "unavailable"
    try:
        values: dict[str, str] = {}
        for line in pathlib.Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"')
        os_release = values.get("PRETTY_NAME", values.get("NAME", "unavailable"))
    except OSError:
        pass
    cpu = "unavailable"
    try:
        for line in pathlib.Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                cpu = line.split(":", 1)[1].strip()[:300]
                break
    except OSError:
        pass
    ram = "unavailable"
    try:
        match = re.search(r"^MemTotal:\s+(\d+)\s+kB", pathlib.Path("/proc/meminfo").read_text(), re.MULTILINE)
        if match:
            ram = f"{int(match.group(1)) // 1024} MiB"
    except OSError:
        pass
    gpu = gpu_description()
    disk = shutil.disk_usage(route["worktree"])
    shell = pwd.getpwuid(os.getuid()).pw_shell
    tool_names = ["node", "npm", "bun", "python3", "git", "docker", "tmux", "nvim"]
    tool_lines: list[str] = []
    excluded_tools = [pathlib.Path(route["repository"]), pathlib.Path(route["worktree"]), pathlib.Path(route["worktreeRoot"])]
    for name in tool_names:
        executable = safe_command_path(name, exclude=excluded_tools)
        if executable:
            tool_lines.append(f"- {name}: {executable} — {first_line([str(executable), '--version'])}")
        else:
            tool_lines.append(f"- {name}: unavailable")
    pi_version = first_line([str(pi_executable), "--version"]) if pi_executable else "unavailable"
    limitations = (
        "assigned repository and Git metadata are writable; outbound network and the host kernel are shared"
        if route["mode"] == "trusted-live"
        else "private clone; publication only through isolated checkpoint branch; no host source bind"
    )
    return "\n".join(
        [
            "# Generated host context",
            "",
            f"- OS: {os_release}",
            f"- Kernel: {first_line(['/usr/bin/uname', '-sr'])}",
            f"- Architecture: {first_line(['/usr/bin/uname', '-m'])}",
            f"- CPU: {cpu}",
            f"- GPU: {gpu}",
            f"- RAM: {ram}",
            f"- Disk free ({route['worktree']}): {disk.free // (1024 ** 3)} GiB",
            f"- Shell: {shell}",
            f"- Pi: {pi_executable or 'unavailable'} — {pi_version}",
            f"- Repository: {route['repository']}",
            f"- Workspace mode: {route['mode']}",
            f"- Task worktree: {route['worktree']}",
            f"- Branch at task start: {route['branch']}",
            f"- Starting OID: {route['startingOid']}",
            f"- Container image: {route['image']}",
            f"- Container identity: {route['container']}",
            "- Loopback development ports: 8000-8010",
            "- Development resource aliases: none",
            f"- Known limitations: {limitations}",
            "",
            "## Development tools",
            *tool_lines,
            "",
        ]
    )


def prepare(cwd: pathlib.Path, owner_pid: int, pi_executable: pathlib.Path | None = None) -> dict[str, Any]:
    policy = load_policy()
    repository = repository_root(cwd.resolve(strict=True))
    mode, control_plane = classify(repository, policy)
    starting_oid = git_output(repository, "rev-parse", "--verify", "HEAD^{commit}")
    branch = git_output(repository, "branch", "--show-current")
    session = task_id()
    worktree = repository
    starting_status = git_output(repository, "status", "--porcelain=v1", "--untracked-files=all")
    protected = branch in policy["protectedBranches"]
    if mode == "trusted-live" and (protected or not branch):
        worktree, branch = create_linked_worktree(
            repository,
            pathlib.Path(policy["worktreeRoot"]),
            session,
            starting_oid,
        )
        starting_status = ""
    common_dir = pathlib.Path(git_output(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
    git_dir = pathlib.Path(git_output(worktree, "rev-parse", "--path-format=absolute", "--git-dir")).resolve(strict=True)
    digest = hashlib.sha256(f"{repository}\0{worktree}\0{session}".encode()).hexdigest()[:16]
    container = f"pi-task-{safe_component(repository.name)}-{digest}"[:63]
    capability = secrets.token_urlsafe(48)
    generated_root = pathlib.Path.home() / ".pi/agent/generated"
    machine_context_path = generated_root / "HOST_CONTEXT.md"
    context_path = generated_root / "tasks" / session / "HOST_CONTEXT.md"
    route: dict[str, Any] = {
        "version": 1,
        "task": session,
        "session": session,
        "mode": mode,
        "repository": str(repository),
        "worktree": str(worktree),
        "branch": branch or "(detached)",
        "startingOid": starting_oid,
        "startingStatus": starting_status,
        "gitCommonDir": str(common_dir),
        "gitDir": str(git_dir),
        "container": container,
        "ownerPid": owner_pid,
        "ownerStartTicks": process_start_ticks(owner_pid),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "image": DEFAULT_IMAGE,
        "worktreeRoot": policy["worktreeRoot"],
        "hostContext": str(context_path),
        "policyHash": policy.get("policyHash", "invalid"),
        "policyValid": bool(policy.get("policyValid")),
        "controlPlane": control_plane,
        "parentOwned": True,
        "capabilityHash": hashlib.sha256(capability.encode()).hexdigest(),
        "createdAt": int(time.time()),
    }
    context = host_context(route, pi_executable)
    atomic_write(machine_context_path, context, 0o600)
    atomic_write(context_path, context, 0o600)
    runtime_root = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR", str(pathlib.Path.home() / ".local/share/pi/runtime"))) / "pi-tasks"
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime_root, 0o700)
    route_path = runtime_root / f"{session}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(route_path, flags, 0o600)
    try:
        payload = (json.dumps(route, sort_keys=True, indent=2) + "\n").encode()
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    return {
        "route": str(route_path),
        "capability": capability,
        "mode": mode,
        "worktree": str(worktree),
        "container": container,
        "worktreeRoot": policy["worktreeRoot"],
        "controlPlane": control_plane,
        "policyValid": bool(policy.get("policyValid")),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-policy")
    validate.add_argument("path", nargs="?", type=pathlib.Path)
    classify_parser = subparsers.add_parser("classify")
    classify_parser.add_argument("cwd", type=pathlib.Path)
    resolve = subparsers.add_parser("resolve-pi")
    resolve.add_argument("--self", required=True, type=pathlib.Path)
    resolve.add_argument("--cwd", default=".", type=pathlib.Path)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--cwd", default=".", type=pathlib.Path)
    prepare_parser.add_argument("--owner-pid", required=True, type=int)
    prepare_parser.add_argument("--pi-executable", type=pathlib.Path)
    args = parser.parse_args()
    try:
        if args.command == "validate-policy":
            policy = load_policy(args.path, fail_closed=False)
            print(json.dumps(policy, sort_keys=True))
        elif args.command == "classify":
            policy = load_policy()
            repo = repository_root(args.cwd.resolve(strict=True))
            mode, control = classify(repo, policy)
            print(json.dumps({"mode": mode, "controlPlane": control, "repository": str(repo), "policyValid": policy["policyValid"]}))
        elif args.command == "resolve-pi":
            print(resolve_pi(args.self, args.cwd))
        elif args.command == "prepare":
            print(json.dumps(prepare(args.cwd, args.owner_pid, args.pi_executable), sort_keys=True))
        return 0
    except WorkspaceError as error:
        print(f"pi workspace: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
