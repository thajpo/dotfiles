#!/usr/bin/env python3
"""Prepare, attest, and wait on one coding container while holding its writer lock."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.pi_control.docker_runtime import DockerRuntimeError, wait_container
from scripts.pi_control.greenfield_store import GreenfieldStore
from scripts.pi_control.launch import LaunchError, attest_run, prepare_run, start_run, stop_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pi-system-container-run")
    parser.add_argument("--state-root", default=os.environ.get("PI_SYSTEM_STATE_ROOT"))
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--working-copy-id", required=True)
    parser.add_argument("--runtime-json", default="{}")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or any(not isinstance(item, str) or not item or "\x00" in item for item in command):
        raise LaunchError("a non-empty container command is required after --")
    runtime = json.loads(args.runtime_json)
    if not isinstance(runtime, dict):
        raise LaunchError("--runtime-json must be an object")
    store = GreenfieldStore(args.state_root)
    prepared = None
    try:
        store.open()
        prepared = prepare_run(store, project_id=args.project_id, conversation_id=args.conversation_id, working_copy_id=args.working_copy_id, authority="writer", runtime=runtime, owner_pid=os.getpid())
        attest_run(store, run_id=prepared.run["run_id"], manifest_digest=prepared.manifest["manifestDigest"])
        started = start_run(store, run_id=prepared.run["run_id"], command=command)
        return_code = wait_container(str(started["container_id"]))
        stop_run(store, run_id=prepared.run["run_id"], reason="container-exited" if return_code == 0 else f"process-failed:{return_code}")
        return int(return_code)
    except DockerRuntimeError as error:
        raise LaunchError(str(error)) from error
    finally:
        if prepared is not None:
            prepared.close()
        store.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LaunchError, DockerRuntimeError, OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)[:1024]}, separators=(",", ":")), file=sys.stderr)
        raise SystemExit(2)
