"""Separate controlling-TTY authority for one exact sensitive request."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, TextIO

from .command_requests import approval_display, approve_command, execute_approved_command, reject_command
from .pi_store import PiStore
from .package_environment import approve_package_request, execute_approved_package_request, package_approval_display, reject_package_request


class AuthorizationCliError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi-authorize")
    parser.add_argument("--state-root", required=True)
    parser.add_argument("request_id")
    parser.add_argument("request_digest")
    parser.add_argument("--test-only-decision", choices=("approve", "reject"))
    parser.add_argument("--test-only-defer-execution", action="store_true")
    return parser


def _test_fixture(root: Path) -> bool:
    marker = root / ".pi-authorize-test-fixture"
    try:
        info = marker.lstat()
        body = marker.read_text(encoding="ascii")
    except OSError:
        return False
    return root.is_relative_to(Path("/tmp")) and not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and body == "P6-NONPRODUCTION-TEST-ONLY\n" and os.environ.get("PI_AUTHORIZE_TEST_FIXTURE") == "1"


def _write_display(stream: TextIO, display: dict[str, Any]) -> None:
    lines = [
        "Pi exact sensitive request",
        f"request: {display['requestId']}", f"digest: {display['digest']}",
        f"project: {display['project']['name']} ({display['project']['id']})",
        f"conversation: {display['conversation']['id']} ({display['conversation']['role']})", f"run: {display['runId']}",
        f"operation: {json.dumps(display['operation'], sort_keys=True, separators=(',', ':'))}",
        f"argv: {json.dumps(display['argv'], separators=(',', ':'))}", f"cwd: {display['cwd']}",
        f"effect scope: {json.dumps(display['effectScope'], sort_keys=True, separators=(',', ':'))}",
        f"execution place: {display['executionPlace']}", f"expiry: {display['expiresAt']}",
    ]
    stream.write("\n".join(lines) + "\n")
    stream.flush()


def _tty_decision(display: dict[str, Any]) -> str:
    try:
        descriptor = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0))
    except OSError as error:
        raise AuthorizationCliError("pi-authorize requires a real controlling TTY at /dev/tty") from error
    try:
        if not os.isatty(descriptor):
            raise AuthorizationCliError("pi-authorize /dev/tty is not a terminal")
        with os.fdopen(os.dup(descriptor), "w", encoding="utf-8", buffering=1) as output, os.fdopen(os.dup(descriptor), "r", encoding="utf-8", buffering=1) as input_stream:
            _write_display(output, display)
            output.write("Type APPROVE or REJECT: ")
            output.flush()
            decision = input_stream.readline(64).strip()
    finally:
        os.close(descriptor)
    if decision not in {"APPROVE", "REJECT"}:
        raise AuthorizationCliError("authorization decision must be exactly APPROVE or REJECT")
    return decision.lower()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.state_root).expanduser().absolute()
    try:
        with PiStore(root) as store:
            package = args.request_id.startswith("pkreq_")
            if not package and not args.request_id.startswith("cmd_"):
                raise AuthorizationCliError("request ID is not an authorizable release-1 request")
            display = package_approval_display(store, request_id=args.request_id, request_digest=args.request_digest) if package else approval_display(store, request_id=args.request_id, request_digest=args.request_digest)
            if args.test_only_decision:
                if not _test_fixture(root):
                    raise AuthorizationCliError("noninteractive authorization is restricted to an explicit disposable test fixture")
                decision = args.test_only_decision
                _write_display(sys.stderr, display)
            else:
                if args.test_only_defer_execution:
                    raise AuthorizationCliError("deferred execution is test-only")
                decision = _tty_decision(display)
            if decision == "reject":
                result = reject_package_request(store, package_request_id=args.request_id, request_digest=args.request_digest) if package else reject_command(store, command_request_id=args.request_id, request_digest=args.request_digest)
            else:
                receipt = approve_package_request(store, package_request_id=args.request_id, request_digest=args.request_digest) if package else approve_command(store, command_request_id=args.request_id, request_digest=args.request_digest)
                if args.test_only_defer_execution:
                    result = {"state": "approved", **receipt}
                else:
                    result = execute_approved_package_request(store, package_request_id=args.request_id, request_digest=args.request_digest) if package else execute_approved_command(store, command_request_id=args.request_id, request_digest=args.request_digest)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"error": {"message": str(error)[:1024]}}, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 2


__all__ = ["AuthorizationCliError", "main"]
