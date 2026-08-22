"""Host administrator CLI for Pisec."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
import re
import sys
from typing import Any

from .bootstrap import run_broker
from .broker import socket_paths
from .models import PisecError
from .protocol import request


_ROOT_DESCRIPTION = """Pisec is the host-side workflow broker for registered projects.

Commands are safe to inspect by default. Human-readable output is the default;
add --json when another program needs the complete response document."""

_ROOT_EPILOG = """Examples:
  pisec project register --path ~/src/project --name project
  pisec project list
  pisec project open ~/src/project
  pisec status --project ~/src/project
  pisec doctor
  pisec doctor --json

Run `pisec <command> --help` for command-specific examples."""


def _call(operation: str, payload: dict[str, Any], *, timeout: float = 30.0) -> Any:
    return request(socket_paths()["admin"], operation, payload, timeout=timeout)


def _add_json_argument(target: argparse.ArgumentParser, *, default: Any = argparse.SUPPRESS) -> None:
    target.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=default,
        help="print the complete response as JSON",
    )


def _parser_kwargs(**kwargs: Any) -> dict[str, Any]:
    return {"formatter_class": argparse.RawDescriptionHelpFormatter, **kwargs}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="pisec",
        **_parser_kwargs(description=_ROOT_DESCRIPTION, epilog=_ROOT_EPILOG),
    )
    _add_json_argument(root, default=False)
    commands = root.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="{project,status,reconcile,board,broker,doctor,workstream}",
    )

    project = commands.add_parser(
        "project",
        **_parser_kwargs(
            help="register, list, open, refresh, deactivate, or reactivate Git projects",
            description="Manage the projects known to the Pisec broker.",
            epilog="Examples:\n  pisec project register --path ~/src/project\n  pisec project list\n  pisec project open ~/src/project\n  pisec project deactivate ~/src/project --confirm ~/src/project\n  pisec project activate ~/src/project",
        ),
    )
    _add_json_argument(project)
    project_commands = project.add_subparsers(dest="project_command", required=True, title="project commands")
    register = project_commands.add_parser(
        "register",
        **_parser_kwargs(
            help="register a Git repository",
            description="Register one Git repository as a Pisec project.",
            epilog="Example:\n  pisec project register --path ~/src/project --name project --default-ref main",
        ),
    )
    _add_json_argument(register)
    register.add_argument("--path", required=True, metavar="PATH", help="path to a Git repository")
    register.add_argument("--name", metavar="NAME", help="display name; defaults to the repository name")
    register.add_argument("--default-ref", metavar="REF", help="Git ref used as the project base; defaults to HEAD")
    register.add_argument("--data-dir", action="append", default=None, metavar="DIR", help="host directory to expose read-only to this project's workers; repeatable")
    project_list = project_commands.add_parser(
        "list",
        **_parser_kwargs(
            help="list registered projects",
            description="List active projects in display-name order; pass --all to include inactive projects.",
            epilog="Examples:\n  pisec project list\n  pisec project list --all",
        ),
    )
    _add_json_argument(project_list)
    project_list.add_argument("--all", action="store_true", help="include inactive projects")
    project_open = project_commands.add_parser(
        "open",
        **_parser_kwargs(
            help="open a project's coordinator",
            description="Create or focus the project's durable Herdr project room and coordinator.",
            epilog="Example:\n  pisec project open ~/src/project",
        ),
    )
    _add_json_argument(project_open)
    project_open.add_argument("project", metavar="PROJECT", help="repository path, display name, or project id")
    project_refresh = project_commands.add_parser(
        "refresh",
        **_parser_kwargs(
            help="roll stale Pisec runtimes to the desired generation",
            description="Wait for stale active runtimes to become idle, then restart and resume them one at a time.",
            epilog="Example:\n  pisec project refresh --all",
        ),
    )
    _add_json_argument(project_refresh)
    project_refresh.add_argument("--all", action="store_true", required=True, help="refresh all stale active Pisec runtime bindings")
    project_refresh.add_argument("--wait-seconds", type=float, default=300.0, metavar="SECONDS", help="maximum time to wait for busy runtimes to become idle (default: 300)")
    project_deactivate = project_commands.add_parser(
        "deactivate",
        **_parser_kwargs(
            help="mark a project inactive and retire its coordinator",
            description="Retire the project's coordinator workstream, close its Herdr surfaces, and mark the project inactive without deleting its registration or history.",
            epilog="Example:\n  pisec project deactivate ~/src/project --confirm ~/src/project",
        ),
    )
    _add_json_argument(project_deactivate)
    project_deactivate.add_argument("project", metavar="PROJECT", help="repository path, display name, or project id")
    project_deactivate.add_argument("--confirm", required=True, metavar="PROJECT", help="repeat the exact PROJECT selector")
    project_activate = project_commands.add_parser(
        "activate",
        **_parser_kwargs(
            help="reactivate an inactive project",
            description="Mark a previously deactivated project active again; open a fresh coordinator with `pisec project open`.",
            epilog="Example:\n  pisec project activate ~/src/project",
        ),
    )
    _add_json_argument(project_activate)
    project_activate.add_argument("project", metavar="PROJECT", help="repository path, display name, or project id")


    status = commands.add_parser(
        "status",
        **_parser_kwargs(
            help="show project or system status",
            description="Show registered projects, workstreams, decisions, and research counts.",
            epilog="Examples:\n  pisec status\n  pisec status --project ~/src/project\n  pisec status --project prj_<32-lowercase-hex>",
        ),
    )
    _add_json_argument(status)
    status.add_argument("--project", metavar="PROJECT", help="repository path, display name, or project id")

    reconcile = commands.add_parser(
        "reconcile",
        **_parser_kwargs(
            help="reconcile durable state with adapters",
            description="Observe workspace state and resume authorized interrupted operations.",
            epilog="Example:\n  pisec reconcile",
        ),
    )
    _add_json_argument(reconcile)

    board = commands.add_parser(
        "board",
        **_parser_kwargs(
            help="show the compact task activity board",
            description="Show task cards, latest checkpoints, next actions, and blockers.",
            epilog="Example:\n  pisec board",
        ),
    )
    _add_json_argument(board)

    broker = commands.add_parser(
        "broker",
        **_parser_kwargs(
            help="run the local broker service",
            description="Run Pisec's local Unix-socket broker in the foreground.",
        ),
    )
    _add_json_argument(broker)

    doctor = commands.add_parser(
        "doctor",
        **_parser_kwargs(
            help="check installation and adapter health",
            description="Run fail-closed checks for state, configuration, adapters, and deployment policy.",
            epilog="Examples:\n  pisec doctor\n  pisec doctor --live-search-workstream ws_<id>",
        ),
    )
    _add_json_argument(doctor)
    doctor.add_argument("--live-search-workstream", metavar="WORKSTREAM", help="exercise live fenced search in an existing approved idle worker")
    workstream = commands.add_parser(
        "workstream",
        **_parser_kwargs(
            help="perform explicit workstream administration",
            description="Run host-side operations that are intentionally separate from workstream completion.",
        ),
    )
    _add_json_argument(workstream)
    workstream_commands = workstream.add_subparsers(dest="workstream_command", required=True, title="workstream commands")
    cleanup = workstream_commands.add_parser(
        "cleanup",
        **_parser_kwargs(
            help="remove a retired worker checkout",
            description="Remove the managed checkout and runtime artifacts for a retired worker.",
            epilog="Example:\n  pisec workstream cleanup ws_<32-lowercase-hex> --confirm ws_<32-lowercase-hex>",
        ),
    )
    _add_json_argument(cleanup)
    cleanup.add_argument("workstream", metavar="WORKSTREAM", help="retired worker workstream id")
    cleanup.add_argument("--confirm", required=True, metavar="WORKSTREAM", help="repeat the exact workstream id")
    cleanup.add_argument("--force-dirty", action="store_true", help="remove a dirty managed checkout")
    return root


def _command_path(args: argparse.Namespace) -> tuple[str, ...]:
    path = [str(args.command)]
    for name in ("project_command", "workstream_command"):
        value = getattr(args, name, None)
        if value:
            path.append(str(value))
    return tuple(path)


def _clip(value: Any, limit: int = 96) -> str:
    text = _scalar_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _scalar_text(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        if not value:
            return "(none)"
        if all(not isinstance(item, (Mapping, list, tuple)) for item in value):
            return ", ".join(_scalar_text(item) for item in value)
        return f"{len(value)} item(s)"
    if isinstance(value, (str, int, float)):
        return str(value)
    return str(value)


def _is_simple(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool, int, float)) or (
        isinstance(value, (list, tuple)) and all(not isinstance(item, (Mapping, list, tuple)) for item in value)
    )


def _key_label(value: Any) -> str:
    text = str(value).replace("_", " ").replace("-", " ")
    text = re.sub(r"(?<!^)(?=[A-Z])", " ", text)
    return text.capitalize()


def _render_value(value: Any, indent: int = 0) -> list[str]:
    prefix = " " * indent
    if _is_simple(value):
        return [prefix + _scalar_text(value)]
    if isinstance(value, Mapping):
        if not value:
            return [prefix + "(none)"]
        lines: list[str] = []
        for key, item in value.items():
            label = _key_label(key)
            if _is_simple(item):
                lines.append(f"{prefix}{label}: {_scalar_text(item)}")
            else:
                lines.append(f"{prefix}{label}:")
                lines.extend(_render_value(item, indent + 2))
        return lines
    if isinstance(value, (list, tuple)):
        if not value:
            return [prefix + "(none)"]
        lines = []
        for item in value:
            if _is_simple(item):
                lines.append(f"{prefix}- {_scalar_text(item)}")
            else:
                lines.append(f"{prefix}-")
                lines.extend(_render_value(item, indent + 2))
        return lines
    return [prefix + _scalar_text(value)]


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    if not rows:
        return ["  (none)"]
    values = [[_clip(value) for value in row] for row in rows]
    widths = [len(str(header)) for header in headers]
    for row in values:
        for index, value in enumerate(row):
            if index >= len(widths):
                widths.append(0)
            widths[index] = max(widths[index], len(value))
    def line(row: Sequence[str]) -> str:
        return "  " + "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
    return [line([str(header) for header in headers]), line(["-" * width for width in widths]), *(line(row) for row in values)]


def _project_lines(project: Any, heading: str = "Project") -> list[str]:
    if not isinstance(project, Mapping):
        return [heading, *_render_value(project, 2)]
    lines = [heading]
    fields = (
        ("Name", "display_name"),
        ("ID", "project_id"),
        ("Default ref", "default_ref"),
        ("Coordinator workstream", "secretary_workstream_id"),
    )
    for label, key in fields:
        if key in project:
            lines.append(f"  {label}: {_scalar_text(project[key])}")
    return lines


def _project_table(projects: Any, *, show_state: bool = False) -> list[str]:
    if not isinstance(projects, list):
        return _render_value(projects, 2)
    rows = []
    for item in projects:
        if not isinstance(item, Mapping):
            continue
        row = [
            item.get("display_name", "-"),
            item.get("project_id", "-"),
            item.get("default_ref", "-"),
        ]
        if show_state:
            row.append("inactive" if not item.get("active") else "active")
        rows.append(tuple(row))
    headers = ("NAME", "ID", "DEFAULT REF") + (("STATE",) if show_state else ())
    return _table(headers, rows)


def _first_mate_lines(first_mate: Any) -> list[str]:
    if not isinstance(first_mate, Mapping):
        return [f"First Mate: {_scalar_text(first_mate)}"]
    if not first_mate.get("present"):
        return ["First Mate: not provisioned (`pisec` admin first_mate.ensure can create it)"]
    return [
        "First Mate: present",
        f"  Workstream: {_scalar_text(first_mate.get('workstreamId'))}",
        f"  State: {_scalar_text(first_mate.get('provisioningState'))} / {_scalar_text(first_mate.get('observedState'))}",
    ]


def _workstream_table(workstreams: Any) -> list[str]:
    if not isinstance(workstreams, list):
        return _render_value(workstreams, 2)
    rows = []
    for item in workstreams:
        if not isinstance(item, Mapping):
            continue
        rows.append(
            (
                item.get("desired_state", "-"),
                item.get("observed_state", "-"),
                item.get("kind", "-"),
                item.get("title", "-"),
                item.get("workstream_id", "-"),
            )
        )
    return _table(("STATE", "RUNTIME", "KIND", "TITLE", "ID"), rows)


def _decision_table(decisions: Any) -> list[str]:
    if not isinstance(decisions, list):
        return _render_value(decisions, 2)
    rows = []
    for item in decisions:
        if not isinstance(item, Mapping):
            continue
        rows.append((item.get("state", "-"), item.get("summary", "-"), item.get("decision_id", "-")))
    return _table(("STATE", "SUMMARY", "ID"), rows)


def _status_lines(result: Any, heading: str) -> list[str]:
    if not isinstance(result, Mapping):
        return [heading, *_render_value(result, 2)]
    if "project" not in result:
        lines = [heading]
        if "schema" in result:
            lines.append(f"Schema: {_scalar_text(result.get('schema'))} v{_scalar_text(result.get('version'))}")
        projects = result.get("projects", [])
        lines.append(f"Active Projects ({len(projects) if isinstance(projects, list) else 0})")
        lines.extend(_project_table(projects))
        inactive = result.get("inactiveProjects", [])
        if isinstance(inactive, list) and inactive:
            lines.append(f"Inactive Projects ({len(inactive)})")
            lines.extend(_project_table(inactive, show_state=True))
        if "firstMate" in result:
            lines.extend(_first_mate_lines(result.get("firstMate")))
        return lines

    project = result.get("project")
    project_name = project.get("display_name", "-") if isinstance(project, Mapping) else "-"
    project_id = project.get("project_id", "-") if isinstance(project, Mapping) else "-"
    lines = [f"{heading}: {project_name} ({project_id})"]
    if isinstance(project, Mapping):
        lines.append(f"Default ref: {_scalar_text(project.get('default_ref'))}")
    if "source" in result:
        lines.append(f"Source: {_scalar_text(result.get('source'))}")
    workstreams = result.get("workstreams", [])
    lines.append(f"Workstreams ({len(workstreams) if isinstance(workstreams, list) else 0})")
    lines.extend(_workstream_table(workstreams))
    decisions = result.get("decisions", [])
    lines.append(f"Decisions ({len(decisions) if isinstance(decisions, list) else 0})")
    lines.extend(_decision_table(decisions))
    counts = result.get("researchCounts")
    if isinstance(counts, Mapping):
        order = ("pending", "researching", "needs_context", "answered", "declined", "acknowledged", "unacknowledged")
        summary = ", ".join(f"{_key_label(key)}={_scalar_text(counts.get(key, 0))}" for key in order)
        lines.append(f"Research: {summary}")
    return lines


def _human_result(command: tuple[str, ...], result: Any) -> str:
    if command == ("project", "register") and isinstance(result, Mapping):
        return "\n".join(_project_lines(result, "Project registered"))
    if command == ("project", "list") and isinstance(result, Mapping):
        projects = result.get("projects", [])
        lines = [f"{'All Projects' if result.get('includeInactive') else 'Active Projects'} ({len(projects) if isinstance(projects, list) else 0})"]
        lines.extend(_project_table(projects, show_state=bool(result.get("includeInactive"))))
        inactive = result.get("inactiveProjects", [])
        if isinstance(inactive, list) and inactive:
            lines.append(f"Inactive Projects ({len(inactive)})")
            lines.extend(_project_table(inactive, show_state=True))
        return "\n".join(lines)
    if command == ("project", "deactivate") and isinstance(result, Mapping):
        project = result.get("project")
        heading = "Project already inactive" if result.get("reused") else "Project deactivated"
        lines = [heading]
        if isinstance(project, Mapping):
            lines.append(f"Project: {_scalar_text(project.get('display_name'))} ({_scalar_text(project.get('project_id'))})")
        if result.get("workstreamId"):
            lines.append(f"Retired coordinator: {_scalar_text(result.get('workstreamId'))}")
        if result.get("retainedSessionRoot"):
            lines.append(f"Retained session root: {_scalar_text(result.get('retainedSessionRoot'))}")
        lines.append("Registration: retained (reactivate with `pisec project activate`)")
        return "\n".join(lines)
    if command == ("project", "activate") and isinstance(result, Mapping):
        project = result.get("project")
        heading = "Project already active" if result.get("reused") else "Project activated"
        lines = [heading]
        if isinstance(project, Mapping):
            lines.append(f"Project: {_scalar_text(project.get('display_name'))} ({_scalar_text(project.get('project_id'))})")
        lines.append("Open a fresh coordinator with `pisec project open`.")
        return "\n".join(lines)
    if command == ("status",):
        return "\n".join(_status_lines(result, "Pisec status"))
    if command == ("board",):
        return "\n".join(_status_lines(result, "Pisec board"))
    if command == ("project", "open") and isinstance(result, Mapping):
        heading = "Project already open" if result.get("reused") else "Project opened"
        lines = [heading]
        project = result.get("project")
        if isinstance(project, Mapping):
            lines.append(f"Project: {_scalar_text(project.get('display_name'))} ({_scalar_text(project.get('project_id'))})")
        workstream = result.get("workstream")
        if isinstance(workstream, Mapping):
            lines.append(f"Coordinator: {_scalar_text(workstream.get('workstream_id'))}")
            lines.append(f"State: {_scalar_text(workstream.get('desired_state'))} / {_scalar_text(workstream.get('provisioning_state'))}")
        binding = result.get("binding")
        if isinstance(binding, Mapping):
            lines.append(f"Runtime: {_scalar_text(binding.get('observed_state'))}")
        return "\n".join(lines)
    if command == ("project", "refresh") and isinstance(result, Mapping):
        lines = ["Pisec runtime refresh", f"Generation: {_scalar_text(result.get('generation'))}"]
        for key in ("upgraded", "pending", "skipped", "failed"):
            values = result.get(key, [])
            lines.append(f"{_key_label(key)} ({len(values) if isinstance(values, list) else 0})")
            if isinstance(values, list):
                rows = [(item.get("project", "-"), item.get("workstreamId", "-"), item.get("reason", item.get("state", item.get("generation", "-")))) for item in values if isinstance(item, Mapping)]
                lines.extend(_table(("PROJECT", "WORKSTREAM", "RESULT"), rows))
        return "\n".join(lines)
    if command == ("reconcile",) and isinstance(result, Mapping):
        lines = ["Reconcile complete" if result.get("reconciled") else "Reconcile incomplete"]
        workspace = result.get("workspace")
        if isinstance(workspace, Mapping):
            lines.append("Workspace:")
            lines.extend(_render_value(workspace, 2))
        resumed = result.get("resumed", [])
        lines.append(f"Resumed operations ({len(resumed) if isinstance(resumed, list) else 0})")
        rows = [(item.get("operationId", "-"), item.get("state", item.get("reused", "-"))) for item in resumed if isinstance(item, Mapping)]
        lines.extend(_table(("OPERATION", "RESULT"), rows))
        errors = result.get("errors", [])
        if errors:
            lines.append(f"Errors ({len(errors)})")
            lines.extend(_render_value(errors, 2))
        return "\n".join(lines)
    if command == ("doctor",) and isinstance(result, Mapping):
        lines = [f"Pisec doctor: {'OK' if result.get('ok') else 'FAILED'}"]
        checks = result.get("checks", [])
        rows = [
            ("OK" if item.get("status") == "ok" else "ERROR", item.get("name", "-"), item.get("detail", "-"))
            for item in checks
            if isinstance(item, Mapping)
        ]
        lines.extend(_table(("STATUS", "CHECK", "DETAIL"), rows))
        schema = result.get("schema")
        if isinstance(schema, Mapping):
            lines.append(f"Schema: {_scalar_text(schema.get('name'))} v{_scalar_text(schema.get('version'))} ({_scalar_text(schema.get('migration'))})")
        adapters = result.get("adapters")
        if isinstance(adapters, Mapping):
            lines.append(f"Adapters: harness={_scalar_text(adapters.get('harness'))}; workspace={_scalar_text(adapters.get('workspace'))}")
        return "\n".join(lines)
    if command == ("workstream", "cleanup") and isinstance(result, Mapping):
        lines = ["Workstream cleanup replayed" if result.get("reused") else "Workstream cleanup complete"]
        workstream = result.get("workstream")
        if isinstance(workstream, Mapping):
            lines.append(f"Workstream: {_scalar_text(workstream.get('title'))} ({_scalar_text(workstream.get('workstream_id'))})")
            lines.append(f"State: {_scalar_text(workstream.get('desired_state'))} / {_scalar_text(workstream.get('provisioning_state'))}")
        operation = result.get("operation")
        if isinstance(operation, Mapping):
            lines.append(f"Operation: {_scalar_text(operation.get('operation_id'))} ({_scalar_text(operation.get('state'))})")
        lines.append("Branch: retained")
        return "\n".join(lines)
    if isinstance(result, Mapping) and result.get("focused") and "workstreamId" in result:
        return f"Focused workstream {_scalar_text(result.get('workstreamId'))}"
    return "\n".join(_render_value(result))


def format_result(command: Sequence[str], result: Any, *, as_json: bool = False) -> str:
    """Format one broker response for a terminal or a machine consumer."""
    if as_json:
        return json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False)
    return _human_result(tuple(command), result)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or all(argument == "--json" for argument in arguments):
        parser().print_help()
        return 0
    args = parser().parse_args(arguments)
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
            if args.data_dir is not None:
                payload["dataDirs"] = [str(Path(d).expanduser()) for d in args.data_dir]
            result = _call("project.register", payload)
        elif args.command == "project" and args.project_command == "list":
            result = _call("project.list", {"includeInactive": bool(args.all)})
        elif args.command == "project" and args.project_command == "open":
            result = _call("project.open", {"project": args.project})
        elif args.command == "project" and args.project_command == "deactivate":
            result = _call("project.deactivate", {"project": args.project, "confirm": args.confirm})
        elif args.command == "project" and args.project_command == "activate":
            result = _call("project.activate", {"project": args.project})
        elif args.command == "project" and args.project_command == "refresh":
            result = _call("project.refresh", {"all": bool(args.all), "waitSeconds": args.wait_seconds}, timeout=max(30.0, args.wait_seconds + 120.0))
        elif args.command == "status":
            result = _call("system.status", {} if args.project is None else {"project": args.project})
        elif args.command == "reconcile":
            result = _call("system.reconcile", {})
        elif args.command == "board":
            result = _call("fleet.activity", {})
        elif args.command == "doctor":
            result = _call("system.doctor", {} if args.live_search_workstream is None else {"liveSearchWorkstream": args.live_search_workstream})
        elif args.command == "workstream" and args.workstream_command == "cleanup":
            result = _call("workstream.cleanup", {"workstreamId": args.workstream, "confirm": args.confirm, "forceDirty": bool(args.force_dirty)})
        else:
            raise AssertionError("unhandled command")
        print(format_result(_command_path(args), result, as_json=bool(args.json_output)))
        return 0
    except (PisecError, OSError) as error:
        print(f"pisec: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
