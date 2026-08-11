"""Command-line surface for the fresh Pi system controller."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from .greenfield_client import GreenfieldClientError, GreenfieldControllerClient
from .errors import ControlPlaneError, ErrorCode
from .greenfield_protocol import PROTOCOL_VERSION, protocol_request


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi-control")
    parser.add_argument("--state-root", default=os.environ.get("PI_SYSTEM_STATE_ROOT"))
    parser.add_argument("--json", action="store_true", dest="json_output")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("schema").add_argument("schema_command", choices=["status"])
    build = sub.add_parser("build")
    build_sub = build.add_subparsers(dest="build_command", required=True)
    build_register = build_sub.add_parser("register")
    build_register.add_argument("--staged-root", required=True)
    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    register = project_sub.add_parser("register")
    register.add_argument("--repository", required=True)
    register.add_argument("--name", default=None)
    project_sub.add_parser("list")
    for name in ("status", "work-index", "reconcile"):
        item = project_sub.add_parser(name)
        item.add_argument("project_id")
    conversation = sub.add_parser("conversation")
    conversation_sub = conversation.add_subparsers(dest="conversation_command", required=True)
    create = conversation_sub.add_parser("create")
    create.add_argument("--request-json", required=True)
    for name in ("focus", "archive"):
        item = conversation_sub.add_parser(name)
        item.add_argument("--request-json", required=True)
    workstream = sub.add_parser("workstream")
    workstream_sub = workstream.add_subparsers(dest="workstream_command", required=True)
    workstream_create = workstream_sub.add_parser("create")
    workstream_create.add_argument("--request-json", required=True)
    message = sub.add_parser("message")
    message_sub = message.add_subparsers(dest="message_command", required=True)
    for name in ("post", "list", "deliver", "acknowledge", "reply"):
        item = message_sub.add_parser(name)
        item.add_argument("--request-json", required=True)
    command = sub.add_parser("command")
    command_sub = command.add_subparsers(dest="command_command", required=True)
    for name in ("request", "status"):
        item = command_sub.add_parser(name)
        item.add_argument("--request-json", required=True)
    run = sub.add_parser("run")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    for name in ("prepare", "attest", "start", "stop", "reconcile", "recover"):
        item = run_sub.add_parser(name)
        item.add_argument("--request-json", required=True)
    change = sub.add_parser("change")
    change_sub = change.add_subparsers(dest="change_command", required=True)
    submit = change_sub.add_parser("submit")
    submit.add_argument("--request-json", required=True)
    revise = change_sub.add_parser("revise")
    revise.add_argument("--request-json", required=True)
    for name in ("list", "show"):
        item = change_sub.add_parser(name)
        item.add_argument("--request-json", required=True)
    review = sub.add_parser("review")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    for name in ("request", "create-assignment", "submit"):
        item = review_sub.add_parser(name)
        item.add_argument("--request-json", required=True)
    dependency = sub.add_parser("dependency")
    dependency_sub = dependency.add_subparsers(dest="dependency_command", required=True)
    for name in ("inventory", "disposition"):
        item = dependency_sub.add_parser(name)
        item.add_argument("--request-json", required=True)
    package = sub.add_parser("package-review")
    package_sub = package.add_subparsers(dest="package_command", required=True)
    for name in ("record", "gate"):
        item = package_sub.add_parser(name)
        item.add_argument("--request-json", required=True)
    package_operation = sub.add_parser("package")
    package_operation_sub = package_operation.add_subparsers(dest="package_operation_command", required=True)
    for name in ("request", "status"):
        item = package_operation_sub.add_parser(name)
        item.add_argument("--request-json", required=True)
    integration = sub.add_parser("integration")
    integration_sub = integration.add_subparsers(dest="integration_command", required=True)
    for name in ("analyze", "authorize", "integrate"):
        item = integration_sub.add_parser(name)
        item.add_argument("--request-json", required=True)
    investigation = sub.add_parser("investigation")
    investigation_sub = investigation.add_subparsers(dest="investigation_command", required=True)
    investigation_start = investigation_sub.add_parser("start")
    investigation_start.add_argument("--request-json", required=True)
    presentation = sub.add_parser("presentation")
    presentation_sub = presentation.add_subparsers(dest="presentation_command", required=True)
    presentation_ensure = presentation_sub.add_parser("ensure")
    presentation_ensure.add_argument("--request-json", required=True)
    scoped = sub.add_parser("scoped-read")
    scoped.add_argument("--request-json", required=True)
    protocol = sub.add_parser("protocol")
    protocol.add_argument("--request-json", required=True)
    return parser


def _request(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else (path if path.lstrip().startswith("{") else Path(path).read_text(encoding="utf-8"))
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise GreenfieldClientError("request JSON must be an object")
    return value


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    client = GreenfieldControllerClient(args.state_root)
    try:
        if args.command == "schema":
            value = client.negotiate()["schema"]
        elif args.command == "build":
            value = client.register_build(args.staged_root)
        elif args.command == "project":
            if args.project_command == "register":
                value = client.register_project(args.repository, args.name)
            elif args.project_command == "list":
                with client._store() as store:
                    value = [dict(row) for row in store.conn.execute("SELECT * FROM projects ORDER BY display_name,project_id")]
            elif args.project_command == "status":
                value = client.status(args.project_id)
            elif args.project_command == "reconcile":
                value = client.reconcile_project(project_id=args.project_id)
            else:
                value = client.work_index(args.project_id)
        elif args.command == "protocol":
            request = _request(args.request_json)
            value = protocol_request(client, request)
        elif args.command == "scoped-read":
            from .scoped_read import ScopedProjectReader
            request = _request(args.request_json)
            with client._store() as store:
                reader = ScopedProjectReader(store, project_id=request["projectId"], working_copy_id=request.get("workingCopyId"))
                operation = request.get("operation")
                if operation == "read":
                    value = reader.read(request.get("path", "."), start_line=request.get("startLine", 1), max_lines=request.get("maxLines", 2000))
                elif operation == "list":
                    value = reader.list(request.get("path", "."), pattern=request.get("pattern", "*"))
                elif operation == "grep":
                    value = reader.grep(request["pattern"], request.get("path", "."))
                else:
                    raise GreenfieldClientError("unsupported scoped read operation")
        else:
            request = _request(args.request_json)
            if args.command == "conversation":
                value = client.dispatch(f"conversation.{args.conversation_command}", request)
            elif args.command == "workstream":
                value = client.dispatch("workstream.create", request)
            elif args.command == "message":
                value = client.dispatch(f"message.{args.message_command}", request)
            elif args.command == "command":
                value = client.dispatch(f"command.{args.command_command}", request)
            elif args.command == "run":
                value = client.dispatch(f"run.{args.run_command}", request)
            elif args.command == "change":
                if args.change_command == "submit":
                    value = client.dispatch("change.submit", request)
                elif args.change_command == "revise":
                    value = client.dispatch("change.revise", request)
                elif args.change_command == "list":
                    value = client.dispatch("change.list", request)
                else:
                    value = client.dispatch("change.show", request)
            elif args.command == "review":
                value = client.dispatch(f"review.{args.review_command}", request)
            elif args.command == "dependency":
                value = client.dispatch(f"dependency.{args.dependency_command}", request)
            elif args.command == "package-review":
                value = client.dispatch(f"package-review.{args.package_command}", request)
            elif args.command == "package":
                value = client.dispatch(f"package.{args.package_operation_command}", request)
            elif args.command == "integration":
                value = client.dispatch(f"integration.{args.integration_command}", request)
            elif args.command == "investigation":
                value = client.dispatch("investigation.start", request)
            elif args.command == "presentation":
                value = client.dispatch("presentation.ensure", request)
            else:
                raise GreenfieldClientError("unsupported CLI command")
        _print(value)
        return 0
    except (ControlPlaneError, GreenfieldClientError, KeyError, ValueError, OSError, RuntimeError) as error:
        if isinstance(error, ControlPlaneError):
            _print({"error": error.as_dict()})
        else:
            _print({"error": {"code": ErrorCode.INVALID_REQUEST, "message": str(error)[:1024], "detail": {}}})
        return 2


__all__ = ["main"]
