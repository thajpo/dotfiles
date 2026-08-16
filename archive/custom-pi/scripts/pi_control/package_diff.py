"""Deterministic package observations from immutable Git trees."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
from typing import Any, Mapping


class PackageInputError(ValueError):
    pass


UNSUPPORTED = {
    "yarn.lock": "yarn", "pnpm-lock.yaml": "pnpm", "pnpm-workspace.yaml": "pnpm",
    "poetry.lock": "Poetry", "Pipfile": "Pipenv", "Pipfile.lock": "Pipenv",
    "requirements.in": "pip-tools", "Cargo.toml": "Cargo", "Cargo.lock": "Cargo",
    "go.mod": "Go", "go.sum": "Go",
}


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _git(repository: Path, args: list[str], *, allow_missing: bool = False) -> bytes | None:
    git = shutil.which("git", path=os.defpath)
    if git is None:
        raise PackageInputError("Git is unavailable for immutable package observation")
    environment = {"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run([git, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "credential.helper=", "-c", "protocol.allow=never", *args], cwd=repository, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False, shell=False)
    if result.returncode != 0:
        if allow_missing:
            return None
        raise PackageInputError(result.stderr.decode("utf-8", errors="replace").strip()[:1024] or "immutable Git package observation failed")
    if len(result.stdout) > 8 * 1024 * 1024:
        raise PackageInputError("immutable package input exceeds its bound")
    return result.stdout


def _tree_paths(repository: Path, tree_oid: str) -> set[str]:
    raw = _git(repository, ["ls-tree", "-r", "--name-only", tree_oid]) or b""
    try:
        values = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise PackageInputError("package tree paths are not UTF-8") from error
    return set(values)


def _resolve_tree(repository: Path, value: str) -> str:
    raw = _git(repository, ["rev-parse", "--verify", value])
    try:
        resolved = (raw or b"").decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise PackageInputError("package tree identity is not ASCII") from error
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", resolved) is None:
        raise PackageInputError("package tree identity did not resolve to one exact object")
    kind = (_git(repository, ["cat-file", "-t", resolved]) or b"").decode("ascii", errors="replace").strip()
    if kind != "tree":
        raise PackageInputError("package observation must resolve to an exact tree object")
    return resolved


def _blob(repository: Path, tree_oid: str, path: str) -> bytes | None:
    if not path or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
        raise PackageInputError("package input path is invalid")
    return _git(repository, ["show", f"{tree_oid}:{path}"], allow_missing=True)


def read_tree_file(repository: str | Path, tree_oid: str, path: str) -> bytes:
    root = Path(repository).resolve(strict=True)
    exact = _resolve_tree(root, tree_oid)
    value = _blob(root, exact, path)
    if value is None:
        raise PackageInputError("immutable package input file is absent")
    return value


def _json(raw: bytes, path: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageInputError(f"{path} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise PackageInputError(f"{path} must contain an object")
    return value


def _npm(repository: Path, tree_oid: str, paths: set[str]) -> dict[str, Any] | None:
    if "package.json" not in paths:
        if "package-lock.json" in paths:
            raise PackageInputError("package-lock.json has no package.json")
        return None
    if "package-lock.json" not in paths:
        raise PackageInputError("npm input is unlocked and fails closed")
    manifest_raw = _blob(repository, tree_oid, "package.json")
    lock_raw = _blob(repository, tree_oid, "package-lock.json")
    assert manifest_raw is not None and lock_raw is not None
    manifest = _json(manifest_raw, "package.json")
    lock = _json(lock_raw, "package-lock.json")
    manager = manifest.get("packageManager")
    match = re.fullmatch(r"npm@([0-9]+\.[0-9]+\.[0-9]+)", manager) if isinstance(manager, str) else None
    if match is None:
        raise PackageInputError("npm must be pinned exactly by packageManager")
    packages = lock.get("packages")
    if lock.get("lockfileVersion") != 3 or not isinstance(packages, dict):
        raise PackageInputError("npm requires committed package-lock.json v3")
    resolved: dict[str, str] = {}
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        direct = manifest.get(section, {})
        if direct is None:
            continue
        if not isinstance(direct, dict):
            raise PackageInputError(f"package.json {section} must be an object")
        for name in direct:
            if not isinstance(name, str) or not name:
                raise PackageInputError("npm dependency name is invalid")
            entry = packages.get("node_modules/" + name)
            version = entry.get("version") if isinstance(entry, dict) else None
            if not isinstance(version, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version) is None:
                raise PackageInputError(f"npm lock has no exact resolution for {name}")
            if isinstance(entry.get("resolved"), str) and entry["resolved"].startswith(("http://", "https://")) and not isinstance(entry.get("integrity"), str):
                raise PackageInputError(f"npm lock lacks integrity for {name}")
            resolved[name] = version
    return {"ecosystem": "npm", "managerVersion": match.group(1), "manifestPath": "package.json", "manifestDigest": digest_bytes(manifest_raw), "lockPath": "package-lock.json", "lockDigest": digest_bytes(lock_raw), "resolved": dict(sorted(resolved.items()))}


_REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s+--hash=sha256:[0-9a-fA-F]{64})+(?:\s*)$")


def _requirements(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PackageInputError("requirements.txt is not UTF-8") from error
    if "pip-compile" in text.lower():
        raise PackageInputError("pip-tools generated inputs are unsupported")
    resolved: dict[str, str] = {}
    logical = ""
    lines: list[str] = []
    for physical in text.splitlines():
        stripped = physical.strip()
        if not stripped or stripped.startswith("#"):
            continue
        logical += (" " if logical else "") + stripped.removesuffix("\\").strip()
        if not stripped.endswith("\\"):
            lines.append(logical)
            logical = ""
    if logical:
        lines.append(logical)
    for line in lines:
        match = _REQUIREMENT.fullmatch(line)
        if match is None:
            raise PackageInputError("Python requirements must be exact and hash-pinned")
        resolved[match.group(1).lower().replace("_", "-")] = match.group(2)
    if not resolved:
        raise PackageInputError("hash-pinned requirements contain no packages")
    return dict(sorted(resolved.items()))


def _uv(raw: bytes) -> dict[str, str]:
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PackageInputError("uv.lock is not valid UTF-8 TOML") from error
    packages = value.get("package")
    if not isinstance(packages, list):
        raise PackageInputError("uv.lock has no package resolution")
    resolved: dict[str, str] = {}
    for item in packages:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("version"), str):
            raise PackageInputError("uv.lock package identity is incomplete")
        name = item["name"].lower().replace("_", "-")
        version = item["version"]
        artifacts: list[Mapping[str, Any]] = []
        if isinstance(item.get("sdist"), dict):
            artifacts.append(item["sdist"])
        if isinstance(item.get("wheels"), list):
            artifacts.extend(value for value in item["wheels"] if isinstance(value, dict))
        source = item.get("source")
        local = isinstance(source, dict) and any(key in source for key in ("editable", "virtual", "directory", "path"))
        if not local and (not artifacts or any(not isinstance(artifact.get("hash"), str) or not artifact["hash"].startswith("sha256:") for artifact in artifacts)):
            raise PackageInputError(f"uv.lock lacks hash-pinned artifacts for {name}")
        resolved[name] = version
    return dict(sorted(resolved.items()))


def _python(repository: Path, tree_oid: str, paths: set[str]) -> dict[str, Any] | None:
    manifest_path = "pyproject.toml" if "pyproject.toml" in paths else "requirements.txt" if "requirements.txt" in paths else None
    if manifest_path is None:
        if "uv.lock" in paths:
            raise PackageInputError("uv.lock has no Python manifest")
        return None
    manifest_raw = _blob(repository, tree_oid, manifest_path)
    assert manifest_raw is not None
    if "uv.lock" in paths:
        lock_path = "uv.lock"
        lock_raw = _blob(repository, tree_oid, lock_path)
        assert lock_raw is not None
        resolved = _uv(lock_raw)
        manager = "uv"
    elif "requirements.txt" in paths:
        lock_path = "requirements.txt"
        lock_raw = _blob(repository, tree_oid, lock_path)
        assert lock_raw is not None
        resolved = _requirements(lock_raw)
        manager = "pip-hash"
    else:
        raise PackageInputError("Python input is unlocked and fails closed")
    return {"ecosystem": "python", "managerVersion": manager, "manifestPath": manifest_path, "manifestDigest": digest_bytes(manifest_raw), "lockPath": lock_path, "lockDigest": digest_bytes(lock_raw), "resolved": resolved}


def observe_package_tree(repository: str | Path, tree_oid: str) -> dict[str, Any]:
    root = Path(repository).resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise PackageInputError("package observation repository is unavailable")
    exact_tree_oid = _resolve_tree(root, tree_oid)
    paths = _tree_paths(root, exact_tree_oid)
    unsupported = sorted({manager for path, manager in UNSUPPORTED.items() if path in paths})
    if unsupported:
        raise PackageInputError("unsupported package manager input: " + ", ".join(unsupported))
    ecosystems = [value for value in (_npm(root, exact_tree_oid, paths), _python(root, exact_tree_oid, paths)) if value is not None]
    return {"treeOid": exact_tree_oid, "ecosystems": ecosystems}


def diff_observations(base: Mapping[str, Any], candidate: Mapping[str, Any]) -> list[dict[str, str]]:
    base_by_ecosystem = {item["ecosystem"]: item for item in base.get("ecosystems", [])}
    candidate_by_ecosystem = {item["ecosystem"]: item for item in candidate.get("ecosystems", [])}
    changes: list[dict[str, str]] = []
    for ecosystem in sorted(set(base_by_ecosystem) | set(candidate_by_ecosystem)):
        before = base_by_ecosystem.get(ecosystem, {}).get("resolved", {})
        after = candidate_by_ecosystem.get(ecosystem, {}).get("resolved", {})
        for name in sorted(set(before) | set(after)):
            if before.get(name) == after.get(name):
                continue
            kind = "add" if name not in before else "remove" if name not in after else "change"
            changes.append({"ecosystem": ecosystem, "changeKind": kind, "packageName": name, "baseVersion": before.get(name, ""), "exactVersion": after.get(name, before.get(name, ""))})
    return changes


__all__ = ["PackageInputError", "diff_observations", "digest_bytes", "observe_package_tree", "read_tree_file"]
