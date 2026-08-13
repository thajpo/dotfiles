#!/usr/bin/env python3
"""Launch one exact controller-selected host Pi process."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.pi_control.controller_channel import ControllerChannelError
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.host_supervisor import ACCEPTANCE_PROFILE, HostSupervisorError, launch_host_pi
from scripts.pi_control.launch import LaunchError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi-system-run", allow_abbrev=False)
    parser.add_argument("--state-root", default=os.environ.get("PI_SYSTEM_STATE_ROOT"))
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--prompt")
    parser.add_argument("--model", required=True)
    parser.add_argument("--interactive", action="store_true", help="run the interactive TUI bound to the controller conversation")
    parser.add_argument("--acceptance-test-profile", choices=[ACCEPTANCE_PROFILE])
    parser.add_argument("--test-provider")
    parser.add_argument("--test-probe")
    parser.add_argument("--child-test-provider")
    parser.add_argument("--tool-image")
    parser.add_argument("--expected-role", choices=["secretary", "investigator", "reviewer", "personal", "workstream"], help=argparse.SUPPRESS)
    parser.add_argument("--child-request-id", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.state_root:
        raise HostSupervisorError("an exact controller state root is required")
    if args.interactive and args.prompt:
        raise HostSupervisorError("interactive launches do not take a one-shot prompt")
    if not args.interactive and not args.prompt:
        raise HostSupervisorError("a prompt is required for one-shot launches")
    with PiStore(Path(args.state_root).expanduser()) as store:
        try:
            return_code = launch_host_pi(
                store, conversation_id=args.conversation_id, build_id=args.build_id,
                prompt=args.prompt or "", model=args.model,
                acceptance_test_profile=args.acceptance_test_profile,
                test_provider=args.test_provider, test_probe=args.test_probe,
                expected_role=args.expected_role,
                tool_image=args.tool_image,
                child_test_provider=args.child_test_provider,
                child_request_id=args.child_request_id,
                interactive=args.interactive,
            )
            if args.child_request_id is not None:
                from scripts.pi_control.subagents import record_child_terminal
                record_child_terminal(
                    store, child_request_id=args.child_request_id,
                    terminal_class="success" if return_code == 0 else "failed",
                    result={"returnCode": return_code},
                )
            return return_code
        except KeyboardInterrupt:
            if args.child_request_id is not None:
                from scripts.pi_control.subagents import SubagentError, record_child_terminal
                try:
                    record_child_terminal(
                        store, child_request_id=args.child_request_id,
                        terminal_class="interrupted", result={"returnCode": 130, "reason": "controller interrupt"},
                    )
                except SubagentError:
                    pass
            raise SystemExit(130)
        except BaseException as error:
            if args.child_request_id is not None:
                from scripts.pi_control.subagents import SubagentError, record_child_terminal
                try:
                    record_child_terminal(
                        store, child_request_id=args.child_request_id,
                        terminal_class="failed", result={"error": type(error).__name__, "message": str(error)[:1024]},
                    )
                except SubagentError:
                    pass
            raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (ControllerChannelError, HostSupervisorError, LaunchError, OSError, ValueError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)[:1024]}, separators=(",", ":")), file=sys.stderr)
        raise SystemExit(2)
