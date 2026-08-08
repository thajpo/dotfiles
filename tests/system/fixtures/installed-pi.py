#!/usr/bin/env python3
"""Installed-process proof for the fresh Pi executable."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def safe_env() -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in os.environ.items():
        result[key] = "<redacted>" if any(marker in key.lower() for marker in ("token", "secret", "password", "credential", "key", "ssh", "auth", "sock", "cookie")) else value
    return result


def run(executable: Path, state_root: Path, request: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(executable), "--state-root", str(state_root), "scoped-read", "--request-json", json.dumps(request, separators=(",", ":"))], env={"PATH": os.defpath, "HOME": os.environ.get("HOME", "/nonexistent"), "LANG": "C", "LC_ALL": "C", "PI_SYSTEM_STATE_ROOT": str(state_root)}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=30, check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--working-copy-id", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    executable = Path(args.executable).resolve(strict=True)
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise SystemExit("installed executable must be a regular executable")
    state_root = Path(args.state_root).resolve(strict=True)
    started = time.monotonic_ns()
    allowed_request = {"projectId": args.project_id, "workingCopyId": args.working_copy_id, "operation": "read", "path": "README"}
    allowed = run(executable, state_root, allowed_request)
    if allowed.returncode != 0:
        raise SystemExit(f"allowed scoped read failed: {allowed.stderr.strip()[:512]}")
    try:
        result = json.loads(allowed.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit("installed process returned non-JSON tool result") from error
    forbidden = run(executable, state_root, {"projectId": args.project_id, "workingCopyId": args.working_copy_id, "operation": "write", "path": "README"})
    if forbidden.returncode == 0:
        raise SystemExit("forbidden write operation was accepted")
    package_files = [executable]
    root = Path(__file__).resolve().parents[3]
    for relative in ("scripts/pi_control/greenfield_cli.py", "scripts/pi_control/scoped_read.py", "pi/extensions/scoped-project-read/index.ts"):
        path = root / relative
        if path.is_file() and not path.is_symlink():
            package_files.append(path)
    evidence = {
        "schemaVersion": 1,
        "scenarioId": "installed-scoped-read",
        "status": "PASS",
        "source": {"repository": str(root), "executable": str(executable), "executableDigest": digest(executable)},
        "installedBuildId": os.environ.get("PI_SYSTEM_BUILD_ID", "development"),
        "role": "host-read-only",
        "registeredTools": ["read", "ls", "grep"],
        "invokedTool": {"name": "read", "request": allowed_request, "result": result},
        "forbiddenTool": {"name": "write", "accepted": False, "returnCode": forbidden.returncode},
        "packageFiles": [{"path": str(path), "digest": digest(path)} for path in package_files],
        "process": {"argv": [str(executable), "scoped-read"], "pid": os.getpid(), "elapsedNs": time.monotonic_ns() - started, "environment": safe_env()},
        "stateRoot": str(state_root),
        "evidenceOutsideRepository": True,
        "noRemoteProvider": True,
    }
    destination = Path(args.evidence).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.write_text(json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
