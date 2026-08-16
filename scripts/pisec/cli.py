"""Host administrator CLI for Pisec."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from .bootstrap import run_broker
from .broker import socket_paths
from .models import PisecError
from .protocol import request


def _call(operation: str, payload: dict[str, Any]) -> Any:
    return request(socket_paths()["admin"], operation, payload)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="pisec")
    commands = root.add_subparsers(dest="command", required=True)

    project = commands.add_parser("project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    register = project_commands.add_parser("register")
    register.add_argument("--path", required=True)
    register.add_argument("--name")
    register.add_argument("--default-ref")
    project_commands.add_parser("list")

    secretary = commands.add_parser("secretary")
    secretary_commands = secretary.add_subparsers(dest="secretary_command", required=True)
    for name in ("ensure", "focus"):
        item = secretary_commands.add_parser(name)
        item.add_argument("project")

    status = commands.add_parser("status")
    status.add_argument("--project")
    commands.add_parser("reconcile")
    commands.add_parser("board")
    commands.add_parser("broker")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")

    workstream = commands.add_parser("workstream")
    workstream_commands = workstream.add_subparsers(dest="workstream_command", required=True)
    cleanup = workstream_commands.add_parser("cleanup")
    cleanup.add_argument("workstream")
    cleanup.add_argument("--confirm", required=True)
    cleanup.add_argument("--force-dirty", action="store_true")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "broker":
        run_broker()
        return 0
    try:
        result: Any
        if args.command == "project" and args.project_command == "register":
            payload = {"path": str(Path(args.path).expanduser())}
            if args.name is not None:
                payload["displayName"] = args.name
            if args.default_ref is not None:
                payload["defaultRef"] = args.default_ref
            result = _call("project.register", payload)
        elif args.command == "project" and args.project_command == "list":
            result = _call("project.list", {})
        elif args.command == "secretary":
            result = _call(f"secretary.{args.secretary_command}", {"project": args.project})
        elif args.command == "status":
            result = _call("system.status", {} if args.project is None else {"project": args.project})
        elif args.command == "reconcile":
            result = _call("system.reconcile", {})
        elif args.command == "board":
            result = _call("system.status", {})
        elif args.command == "doctor":
            result = _call("system.doctor", {})
        elif args.command == "workstream" and args.workstream_command == "cleanup":
            result = _call("workstream.cleanup", {"workstreamId": args.workstream, "confirm": args.confirm, "forceDirty": bool(args.force_dirty)})
        else:
            raise AssertionError("unhandled command")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (PisecError, OSError) as error:
        print(f"pisec: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
