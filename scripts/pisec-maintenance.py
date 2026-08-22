#!/usr/bin/env python3
"""Owner-only executor for exact approved Pisec deployment recipes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ALLOWED_STEPS = {"restart-pisec-broker", "refresh-pisec-runtimes", "restart-herdr", "daemon-reload", "run-pisec-doctor"}
LINUX_COMMANDS = {
    "restart-pisec-broker": ["systemctl", "--user", "restart", "pisec-broker.service"],
    "refresh-pisec-runtimes": ["pisec", "reconcile"],
    "restart-herdr": ["systemctl", "--user", "restart", "herdr.service"],
    "daemon-reload": ["systemctl", "--user", "daemon-reload"],
    "run-pisec-doctor": ["pisec", "doctor"],
}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(_canonical(value) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def validate_request(request: object, source_root: Path) -> dict[str, object]:
    if not isinstance(request, dict) or request.get("action") != "deployment.apply":
        raise ValueError("maintenance request action is invalid")
    if request.get("sourceRoot") != str(source_root.resolve(strict=True)):
        raise ValueError("maintenance source root is not installer pinned")
    steps = request.get("steps")
    if not isinstance(steps, list) or not steps or steps[-1] != "run-pisec-doctor" or any(step not in ALLOWED_STEPS for step in steps):
        raise ValueError("maintenance steps are not an allowed recipe")
    digest = request.get("requestSha256")
    body = dict(request); body.pop("requestSha256", None)
    if not isinstance(digest, str) or hashlib.sha256(_canonical(body).encode()).hexdigest() != digest:
        raise ValueError("maintenance request digest does not match")
    return request


def execute(request_path: Path, status_path: Path, source_root: Path, *, command_runner=subprocess.run) -> dict[str, object]:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    validated = validate_request(request, source_root)
    result: dict[str, object] = {"action": validated["action"], "state": "running", "steps": []}
    _atomic_json(status_path, result)
    for step in validated["steps"]:
        command = LINUX_COMMANDS[step]
        result["currentStep"] = step
        _atomic_json(status_path, result)
        try:
            completed = command_runner(command, cwd=str(source_root), env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C", "LC_ALL": "C"}, capture_output=True, text=True, timeout=300, check=False)
        except Exception as error:
            result.update(state="needs_attention", error=str(error)[:512])
            _atomic_json(status_path, result)
            return result
        item = {"step": step, "returncode": int(completed.returncode)}
        result["steps"].append(item)
        if completed.returncode != 0:
            result.update(state="failed", error=f"{step} failed")
            _atomic_json(status_path, result)
            return result
    result.update(state="applied", currentStep=None)
    _atomic_json(status_path, result)
    return result


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) != 2:
        print("usage: pisec-maintenance REQUEST STATUS", file=sys.stderr); return 2
    source_root = Path(os.environ.get("PISEC_MAINTENANCE_SOURCE_ROOT", "")).resolve(strict=True)
    result = execute(Path(args[0]), Path(args[1]), source_root)
    return 0 if result.get("state") == "applied" else 1


if __name__ == "__main__":
    raise SystemExit(main())
