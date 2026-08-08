"""Command-line surface for the fresh Pi system controller."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from .greenfield_client import GreenfieldClientError, GreenfieldControllerClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi-control")
    parser.add_argument("--state-root", default=os.environ.get("PI_SYSTEM_STATE_ROOT"))
    parser.add_argument("--json", action="store_true", dest="json_output")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("schema").add_argument("schema_command", choices=["status"])
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
    for name in ("request", "authorize", "reject", "consume"):
        item = command_sub.add_parser(name)
        item.add_argument("--request-json", required=True)
    run = sub.add_parser("run")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    for name in ("prepare", "attest", "stop", "reconcile", "recover"):
        item = run_sub.add_parser(name)
        item.add_argument("--request-json", required=True)
    change = sub.add_parser("change")
    change_sub = change.add_subparsers(dest="change_command", required=True)
    submit = change_sub.add_parser("submit")
    submit.add_argument("--request-json", required=True)
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
    for name in ("detect", "disposition"):
        item = dependency_sub.add_parser(name)
        item.add_argument("--request-json", required=True)
    package = sub.add_parser("package-review")
    package_sub = package.add_subparsers(dest="package_command", required=True)
    for name in ("record", "gate"):
        item = package_sub.add_parser(name)
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
            operation = request.get("operation")
            if not isinstance(operation, str):
                raise GreenfieldClientError("protocol operation is required")
            value = {"protocolVersion": 1, "operation": operation, "value": client.dispatch(operation, request.get("request") or {})}
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
                value = getattr(client, f"{args.conversation_command}_conversation")(**request)
            elif args.command == "workstream":
                value = client.create_workstream(**request)
            elif args.command == "message":
                value = getattr(client, f"{args.message_command}_message")(**request)
            elif args.command == "command":
                value = {"request": client.request_command(**request)} if args.command_command == "request" else (client.authorize_command(**request) if args.command_command == "authorize" else (client.reject_command(**request) if args.command_command == "reject" else client.consume_command(**request)))
            elif args.command == "run":
                value = getattr(client, f"{args.run_command}_run")(**request)
            elif args.command == "change":
                if args.change_command == "submit":
                    value = client.submit_change(**request)
                elif args.change_command == "list":
                    value = client.list_changes(request.get("project_id") or request.get("projectId"))
                else:
                    value = client.get_change(request.get("change_id") or request.get("changeId"))
            elif args.command == "review":
                value = client.request_review(**request) if args.review_command == "request" else (client.create_review_assignment(**request) if args.review_command == "create-assignment" else client.submit_review(**request))
            elif args.command == "dependency":
                value = client.detect_dependencies(**request) if args.dependency_command == "detect" else client.set_dependency_disposition(**request)
            elif args.command == "package-review":
                value = client.record_package_security_review(**request) if args.package_command == "record" else client.package_review_gate(**request)
            elif args.command == "integration":
                value = client.analyze_integration(**request) if args.integration_command == "analyze" else (client.authorize_integration(**request) if args.integration_command == "authorize" else client.integrate(**request))
            elif args.command == "investigation":
                from .investigators import start_investigation
                with client._store(mutate=True) as store:
                    value = start_investigation(store, **request)
            elif args.command == "presentation":
                from .presentation import ensure_presentation
                with client._store(mutate=True) as store:
                    value = ensure_presentation(store, **request)
            else:
                raise GreenfieldClientError("unsupported CLI command")
        _print(value)
        return 0
    except (GreenfieldClientError, KeyError, ValueError, OSError, RuntimeError) as error:
        _print({"error": type(error).__name__, "message": str(error)[:1024]})
        return 2


__all__ = ["main"]
