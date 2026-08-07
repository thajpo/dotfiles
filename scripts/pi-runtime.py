#!/usr/bin/env python3
"""Prepare an immutable Linux dependency image for a Pi task.

The host only supplies exact dependency manifests.  The project source, the
host virtualenv, credentials, and untracked files never enter the build
context.  A task container may still write its own upper layer and package
cache without mutating this image.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any


CONTRACT_VERSION = "pi-runtime-v1"
DEFAULT_CACHE_ROOT = Path("~/.local/state/pi/runtime-locks")


class RuntimeErrorContract(RuntimeError):
    pass


def run(command: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeErrorContract(f"{' '.join(command)}: {detail}")
    return result.stdout.strip()


def inspect_image(image: str) -> dict[str, Any]:
    raw = run(["docker", "image", "inspect", image])
    try:
        item = json.loads(raw)[0]
    except (ValueError, IndexError, TypeError) as error:
        raise RuntimeErrorContract(f"invalid Docker image metadata for {image}") from error
    if not isinstance(item, dict):
        raise RuntimeErrorContract(f"invalid Docker image metadata for {image}")
    return item


def safe_manifest_paths(worktree: Path) -> list[Path]:
    names = ("pyproject.toml", "uv.lock", "uv.toml", ".python-version", "python-version")
    paths: list[Path] = []
    for name in names:
        candidate = worktree / name
        if not candidate.exists():
            continue
        if candidate.is_symlink() or not candidate.is_file():
            raise RuntimeErrorContract(f"runtime manifest must be a regular non-symlink file: {candidate}")
        paths.append(candidate)
    return paths


def has_workspace_or_path_dependency(pyproject: str) -> bool:
    # This conservative check deliberately prefers a task-local environment
    # over baking a dependency graph that can reference source outside the
    # manifest-only build context.
    markers = ("[tool.uv.workspace]", "workspace =", "path =", "directory =")
    return any(marker in pyproject for marker in markers)


def base_id_and_platform(item: dict[str, Any]) -> tuple[str, str]:
    image_id = str(item.get("Id", ""))
    os_name = str(item.get("Os", ""))
    architecture = str(item.get("Architecture", ""))
    if not image_id or not os_name or not architecture:
        raise RuntimeErrorContract("base image metadata is missing id, OS, or architecture")
    if os_name != "linux":
        raise RuntimeErrorContract(f"Pi dependency images require a Linux base image, got {os_name}/{architecture}")
    return image_id, f"{os_name}/{architecture}"


def immutable_local_base_reference(base_id: str) -> str:
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", base_id)
    if not match:
        raise RuntimeErrorContract("base image id is not a canonical sha256 digest")
    reference = f"pi-runtime-base:{match.group(1)}"
    # BuildKit does not accept a bare image ID in FROM; it interprets
    # `sha256:<id>` as a registry repository. Pin a deterministic local tag to
    # the inspected ID, then verify the tag before using it. Derived-image
    # layer verification below still rejects any concurrent retargeting.
    run(["docker", "tag", base_id, reference])
    if str(inspect_image(reference).get("Id", "")) != base_id:
        raise RuntimeErrorContract("local immutable base reference does not resolve to the inspected image id")
    return reference


def manifest_digest(paths: list[Path], base_id: str, platform_name: str) -> str:
    digest = hashlib.sha256()
    digest.update(CONTRACT_VERSION.encode())
    digest.update(b"\0")
    digest.update(base_id.encode())
    digest.update(b"\0")
    digest.update(platform_name.encode())
    for path in paths:
        digest.update(b"\0")
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def image_has_contract(image: str, key: str, base_id: str, base_layers: list[str]) -> bool:
    try:
        item = inspect_image(image)
    except RuntimeErrorContract:
        return False
    labels = ((item.get("Config") or {}).get("Labels") or {})
    layers = ((item.get("RootFS") or {}).get("Layers") or [])
    return (
        labels.get("pi.runtime.managed") == "true"
        and labels.get("pi.runtime.provider") == "uv"
        and labels.get("pi.runtime.environment-key") == key
        and labels.get("pi.runtime.base-id") == base_id
        and bool(base_layers) and layers[:len(base_layers)] == base_layers
    )


def build_image(worktree: Path, paths: list[Path], base_id: str,
                base_layers: list[str], platform_name: str, key: str) -> str:
    final_image = f"pi-runtime-uv:{key[:32]}"
    if image_has_contract(final_image, key, base_id, base_layers):
        return final_image

    cache_root = Path(os.path.expanduser(str(DEFAULT_CACHE_ROOT)))
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(cache_root, 0o700)
    lock_path = cache_root / f"{key}.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if image_has_contract(final_image, key, base_id, base_layers):
            return final_image
        base_reference = immutable_local_base_reference(base_id)
        with tempfile.TemporaryDirectory(prefix="pi-runtime-build-") as temporary:
            context = Path(temporary)
            for source in paths:
                shutil.copyfile(source, context / source.name)
            copy_lines = "\n".join(f"COPY {path.name} /opt/pi/runtime-project/{path.name}" for path in paths)
            # Build from a verified local tag whose name is derived from the
            # inspected immutable image ID, never the mutable discovery tag.
            dockerfile = f"""FROM {base_reference}
WORKDIR /opt/pi/runtime-project
{copy_lines}
RUN mkdir -p /opt/pi/env \\
    && UV_PROJECT_ENVIRONMENT=/opt/pi/env uv sync --frozen --no-install-project \\
    && chmod -R a+rX /opt/pi/env
ENV VIRTUAL_ENV=/opt/pi/env
ENV PATH=/opt/pi/env/bin:/home/sandbox/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
"""
            (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            temporary_image = f"pi-runtime-uv-build:{key[:24]}-{os.getpid()}"
            run([
                "docker", "build", "--pull=false", "--platform", platform_name,
                "--tag", temporary_image,
                "--label", "pi.runtime.managed=true",
                "--label", "pi.runtime.provider=uv",
                "--label", f"pi.runtime.environment-key={key}",
                "--label", f"pi.runtime.base-id={base_id}",
                "--label", f"pi.runtime.platform={platform_name}",
                "--label", f"pi.runtime.worktree={worktree}",
                str(context),
            ])
            metadata = inspect_image(temporary_image)
            labels = ((metadata.get("Config") or {}).get("Labels") or {})
            layers = ((metadata.get("RootFS") or {}).get("Layers") or [])
            parent = str(metadata.get("Parent", ""))
            if (labels.get("pi.runtime.environment-key") != key or
                    labels.get("pi.runtime.base-id") != base_id or
                    not base_layers or layers[:len(base_layers)] != base_layers or
                    (parent and parent != base_id)):
                raise RuntimeErrorContract("derived runtime image failed immutable base verification")
            run(["docker", "tag", temporary_image, final_image])
            run(["docker", "image", "rm", temporary_image])
        return final_image


def prepare(route_path: Path) -> dict[str, Any]:
    route = json.loads(route_path.read_text(encoding="utf-8"))
    worktree = Path(route["worktree"]).resolve(strict=True)
    base_image = str(route["image"])
    paths = safe_manifest_paths(worktree)
    if not (worktree / "pyproject.toml").is_file() or not (worktree / "uv.lock").is_file():
        return {
            "provider": "none",
            "mode": "task-local",
            "reason": "no pyproject.toml and uv.lock pair",
            "image": base_image,
            "environmentKey": "task-local",
        }
    pyproject = (worktree / "pyproject.toml").read_text(encoding="utf-8")
    if has_workspace_or_path_dependency(pyproject):
        return {
            "provider": "uv",
            "mode": "task-local",
            "reason": "workspace or path dependency requires task source",
            "image": base_image,
            "environmentKey": "task-local",
        }
    base_item = inspect_image(base_image)
    base_id, platform_name = base_id_and_platform(base_item)
    base_layers = ((base_item.get("RootFS") or {}).get("Layers") or [])
    if not isinstance(base_layers, list) or not base_layers or not all(isinstance(layer, str) and layer for layer in base_layers):
        raise RuntimeErrorContract("base image metadata is missing immutable filesystem layers")
    key = manifest_digest(paths, base_id, platform_name)
    image = build_image(worktree, paths, base_id, base_layers, platform_name, key)
    return {
        "provider": "uv",
        "mode": "derived-image",
        "reason": "exact lockfile and project manifest",
        "image": image,
        "environmentKey": key,
        "manifestHash": hashlib.sha256(b"".join(path.read_bytes() for path in paths)).hexdigest(),
        "platform": platform_name,
        "baseImage": base_image,
        "baseId": base_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prepare", choices=["prepare"])
    parser.add_argument("--route", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.route), sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, RuntimeErrorContract) as error:
        print(f"pi runtime: {error}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
