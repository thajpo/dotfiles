#!/usr/bin/env python3
"""Conservative garbage collection for Pi-owned Docker resources.

Dry-run is the default.  ``--apply`` only removes resources carrying the
explicit Pi labels; it never invokes a global Docker prune.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


MANAGED_CONTAINER = "pi.container-sandbox.managed=true"
MANAGED_IMAGE = "pi.runtime.managed=true"
MANAGED_VOLUME = "pi.package-cache.managed=true"


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"docker {' '.join(args)} exited with {result.returncode}")
    return result.stdout


def inspect(kind: str, identifier: str) -> dict[str, Any]:
    parsed = json.loads(docker(kind, "inspect", identifier))
    return parsed[0]


def owner_identity(pid: int) -> str:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return f"linux:{fields[21]}"
    except (OSError, IndexError):
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        value = result.stdout.strip()
        return f"darwin:{value}" if result.returncode == 0 and value else "unavailable"


def owner_alive(labels: dict[str, str]) -> bool:
    try:
        pid = int(labels.get("pi.container-sandbox.owner", ""))
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    expected = labels.get("pi.container-sandbox.owner-identity", "")
    actual = hashlib.sha256(owner_identity(pid).encode()).hexdigest()[:16]
    return bool(expected) and expected == actual


def created_epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="remove eligible labeled resources")
    parser.add_argument("--older-than-days", type=int, default=30)
    args = parser.parse_args()
    if args.older_than_days < 1:
        parser.error("--older-than-days must be positive")
    cutoff = time.time() - args.older_than_days * 86400
    actions: list[dict[str, Any]] = []

    container_ids = docker("ps", "-aq", "--filter", f"label={MANAGED_CONTAINER}").split()
    referenced_volumes: set[str] = set()
    for identifier in container_ids:
        item = inspect("container", identifier)
        labels = item.get("Config", {}).get("Labels", {}) or {}
        state = item.get("State", {}) or {}
        for mount in item.get("Mounts", []) or []:
            if mount.get("Type") == "volume" and mount.get("Name", "").startswith("pi-package-cache-"):
                referenced_volumes.add(mount["Name"])
        if owner_alive(labels):
            actions.append({"kind": "container", "id": identifier, "action": "retain", "reason": "owner-alive"})
            continue
        target = labels.get("pi.container-sandbox.target", "unknown")
        if target == "trusted-live":
            action = "remove" if args.apply else "propose-remove"
            if args.apply:
                docker("rm", "-f", identifier)
            actions.append({"kind": "container", "id": identifier, "action": action, "reason": "dead trusted-live owner"})
        elif state.get("Status") in {"exited", "dead"} and labels.get("pi.container-sandbox.recoverable") == "clean":
            action = "remove" if args.apply else "propose-remove"
            if args.apply:
                docker("rm", "-f", identifier)
            actions.append({"kind": "container", "id": identifier, "action": action, "reason": "dead clean isolated task"})
        else:
            actions.append({"kind": "container", "id": identifier, "action": "retain", "reason": "isolated task recovery state unproven"})

    image_ids = docker("images", "-q", "--filter", f"label={MANAGED_IMAGE}").split()
    images: list[tuple[float, str, dict[str, str]]] = []
    for identifier in dict.fromkeys(image_ids):
        item = inspect("image", identifier)
        labels = ((item.get("Config") or {}).get("Labels") or {})
        images.append((created_epoch(str(item.get("Created", ""))), identifier, labels))
    grouped: dict[str, list[tuple[float, str, dict[str, str]]]] = {}
    for image in images:
        grouped.setdefault(image[2].get("pi.runtime.worktree", "unknown"), []).append(image)
    for group in grouped.values():
        group.sort(reverse=True)
        for index, (created, identifier, labels) in enumerate(group):
            if index < 2 or created >= cutoff:
                actions.append({"kind": "image", "id": identifier, "action": "retain", "reason": "recent or newest-two"})
                continue
            action = "remove" if args.apply else "propose-remove"
            if args.apply:
                docker("image", "rm", identifier)
            actions.append({"kind": "image", "id": identifier, "action": action, "reason": "older than retention window"})

    volume_ids = docker("volume", "ls", "-q", "--filter", f"label={MANAGED_VOLUME}").split()
    for identifier in dict.fromkeys(volume_ids):
        item = inspect("volume", identifier)
        name = item.get("Name", identifier)
        if name in referenced_volumes:
            actions.append({"kind": "volume", "id": name, "action": "retain", "reason": "referenced by managed container"})
            continue
        created = created_epoch(str(item.get("CreatedAt", "")))
        if created >= cutoff:
            actions.append({"kind": "volume", "id": name, "action": "retain", "reason": "recent"})
            continue
        action = "remove" if args.apply else "propose-remove"
        if args.apply:
            docker("volume", "rm", name)
        actions.append({"kind": "volume", "id": name, "action": action, "reason": "unreferenced and older than retention window"})

    print(json.dumps({"apply": args.apply, "olderThanDays": args.older_than_days, "actions": actions}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        print(f"pi sandbox gc: {error}", file=sys.stderr)
        raise SystemExit(2)
