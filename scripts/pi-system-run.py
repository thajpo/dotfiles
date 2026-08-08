#!/usr/bin/env python3
"""Run one controller-prepared Pi process while retaining its writer lease."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.pi_control.greenfield_store import GreenfieldStore
from scripts.pi_control.launch import LaunchError, prepare_run, stop_run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi-system-run")
    parser.add_argument("--state-root", default=os.environ.get("PI_SYSTEM_STATE_ROOT"))
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--working-copy-id")
    parser.add_argument("--authority", choices=("read-only", "writer", "secretary", "host-maintenance"), default="read-only")
    parser.add_argument("--task-id")
    parser.add_argument("--build-id")
    parser.add_argument("--runtime-json", default="{}")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _runtime(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise LaunchError("--runtime-json must be an object") from error
    if not isinstance(parsed, dict):
        raise LaunchError("--runtime-json must be an object")
    return parsed


def _command(raw: list[str]) -> list[str]:
    command = raw[1:] if raw[:1] == ["--"] else raw
    if not command or any(not item or "\x00" in item for item in command):
        raise LaunchError("a non-empty command is required after --")
    executable = command[0]
    if "/" in executable:
        path = Path(executable)
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise LaunchError("run executable must be an existing non-symlink absolute file")
        command[0] = str(path)
    else:
        resolved = shutil.which(executable, path=os.defpath)
        if resolved is None:
            raise LaunchError("run executable is not available on the controlled PATH")
        command[0] = resolved
    return command


def _safe_env(prepared: Any, state_root: Path, authority: str) -> dict[str, str]:
    env = {
        "PATH": os.defpath,
        "HOME": "/nonexistent" if authority == "writer" else str(Path.home()),
        "LANG": "C",
        "LC_ALL": "C",
        "TERM": os.environ.get("TERM", "dumb"),
        "PI_SYSTEM_CONTROL": str(Path(__file__).resolve().parent.parent / "bin" / "pi-control"),
        "PI_SYSTEM_STATE_ROOT": str(state_root),
        "PI_SYSTEM_RUN_AUTHORITY": authority,
    }
    env.update(prepared.environment)
    for key in ("PI_SYSTEM_WORKSTREAM_ID", "PI_SYSTEM_PACKAGE_ENVIRONMENT_ID"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = _command(args.command)
    runtime = _runtime(args.runtime_json)
    state_root = Path(args.state_root).expanduser() if args.state_root else None
    store = GreenfieldStore(state_root)
    prepared = None
    process = None
    try:
        store.open()
        prepared = prepare_run(
            store,
            project_id=args.project_id,
            conversation_id=args.conversation_id,
            working_copy_id=args.working_copy_id,
            authority=args.authority,
            runtime=runtime,
            task_id=args.task_id,
            build_id=args.build_id,
            owner_pid=os.getpid(),
        )
        from scripts.pi_control.launch import attest_run
        attest_run(store, run_id=prepared.run["run_id"], manifest_digest=prepared.manifest["manifestDigest"])
        working = store.conn.execute("SELECT path FROM working_copies WHERE working_copy_id=?", (args.working_copy_id,)).fetchone() if args.working_copy_id else None
        project = store.conn.execute("SELECT primary_checkout FROM projects WHERE project_id=?", (args.project_id,)).fetchone()
        cwd = Path(working[0] if working else project[0]).resolve(strict=True)
        process = subprocess.Popen(command, cwd=str(cwd), env=_safe_env(prepared, Path(store.state_root).resolve(), args.authority), stdin=None, stdout=None, stderr=None, shell=False)
        return_code = process.wait()
        if return_code != 0:
            stop_run(store, run_id=prepared.run["run_id"], reason=f"process-failed:{return_code}")
        return int(return_code)
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
            process.wait()
        return 130
    finally:
        if prepared is not None:
            try:
                stop_run(store, run_id=prepared.run["run_id"], reason="process-exited")
            except Exception:
                # Preserve the original process result; a later reconcile can
                # surface a durable stop failure as needs_attention.
                pass
            prepared.close()
        store.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LaunchError, OSError, ValueError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)[:1024]}, separators=(",", ":")), file=sys.stderr)
        raise SystemExit(2)
