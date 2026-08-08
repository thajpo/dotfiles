"""Phase 2/3 controller CLI.

Phase 3 adds read-only Git/project inventory and an explicitly named
``--observe-only`` reconciliation.  No command here invokes a mutating Git
operation or legacy writer.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Sequence

from .errors import ControlPlaneError, NotFoundError
from .error_messages import projected_error
from .client import ControllerClient, protocol_request
from .models import parse_canonical_json
from .legacy_inventory import inventory_legacy
from .migration import inventory as inventory_migration, load_inventory, shadow_import, shadow_reconcile
from .project_policy import load_policy
from .staged_build import create_build_manifest, write_build_manifest
from .reconcile import (
    inspect_project,
    inventory_working_copies,
    reconcile_observe_only,
    register_project,
)
from .store import ControllerStore


def _read_root_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-root", dest="state_root", default=argparse.SUPPRESS)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi-control")
    parser.add_argument("--state-root", default=os.environ.get("PI_CONTROL_STATE_ROOT"))
    parser.add_argument("--db", dest="db_path", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    schema = sub.add_parser("schema")
    schema_sub = schema.add_subparsers(dest="schema_command", required=True)
    status = schema_sub.add_parser("status")
    status.add_argument("--json", action="store_true")

    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    plist = project_sub.add_parser("list")
    plist.add_argument("--json", action="store_true")
    register = project_sub.add_parser("register")
    _read_root_option(register)
    register.add_argument("--repository", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--policy", default=None)
    register.add_argument("--json", action="store_true")
    inspect_cmd = project_sub.add_parser("inspect")
    _read_root_option(inspect_cmd)
    inspect_cmd.add_argument("project_id")
    inspect_cmd.add_argument("--policy", default=None)
    inspect_cmd.add_argument("--json", action="store_true")

    working_copy = sub.add_parser("working-copy")
    working_sub = working_copy.add_subparsers(dest="working_command", required=True)
    inventory = working_sub.add_parser("inventory")
    _read_root_option(inventory)
    inventory.add_argument("project_id")
    inventory.add_argument("--policy", default=None)
    inventory.add_argument("--json", action="store_true")

    inspect = sub.add_parser("inspect")
    _read_root_option(inspect)
    inspect.add_argument("project_id")
    inspect.add_argument("--policy", default=None)
    inspect.add_argument("--json", action="store_true")

    reconcile = sub.add_parser("reconcile")
    _read_root_option(reconcile)
    reconcile.add_argument("project_id")
    reconcile.add_argument("--policy", default=None)
    reconcile.add_argument("--observe-only", action="store_true")
    reconcile.add_argument("--json", action="store_true")

    operation = sub.add_parser("operation")
    operation_sub = operation.add_subparsers(dest="operation_command", required=True)
    olist = operation_sub.add_parser("list")
    olist.add_argument("--json", action="store_true")

    event = sub.add_parser("event")
    event_sub = event.add_subparsers(dest="event_command", required=True)
    elist = event_sub.add_parser("list")
    elist.add_argument("--after", type=int, default=0)
    elist.add_argument("--limit", type=int, default=256)
    elist.add_argument("--json", action="store_true")

    status_cmd = sub.add_parser("status")
    _read_root_option(status_cmd)
    status_cmd.add_argument("project_id")
    status_cmd.add_argument("--no-refresh", action="store_true")
    status_cmd.add_argument("--json", action="store_true")

    focus_cmd = sub.add_parser("focus")
    _read_root_option(focus_cmd)
    focus_cmd.add_argument("project_id")
    focus_cmd.add_argument("resource_id")
    focus_cmd.add_argument("--expected-version", type=int, default=None)
    focus_cmd.add_argument("--json", action="store_true")

    protocol = sub.add_parser("protocol")
    _read_root_option(protocol)
    protocol.add_argument("--request-json", required=True)
    protocol.add_argument("--json", action="store_true")

    change = sub.add_parser("change")
    change_sub = change.add_subparsers(dest="change_command", required=True)
    change_submit = change_sub.add_parser("submit")
    _read_root_option(change_submit)
    change_submit.add_argument("--request-json", required=True)
    change_submit.add_argument("--json", action="store_true")

    workstream = sub.add_parser("workstream")
    workstream_sub = workstream.add_subparsers(dest="workstream_command", required=True)
    workstream_create = workstream_sub.add_parser("create")
    _read_root_option(workstream_create)
    workstream_create.add_argument("--request-json", required=True)
    workstream_create.add_argument("--json", action="store_true")

    personal = sub.add_parser("personal")
    personal_sub = personal.add_subparsers(dest="personal_command", required=True)
    personal_select = personal_sub.add_parser("select")
    _read_root_option(personal_select)
    personal_select.add_argument("--request-json", required=True)
    personal_select.add_argument("--json", action="store_true")

    review = sub.add_parser("review")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_request_cmd = review_sub.add_parser("request")
    _read_root_option(review_request_cmd)
    review_request_cmd.add_argument("--request-json", required=True)
    review_request_cmd.add_argument("--json", action="store_true")
    review_submit_cmd = review_sub.add_parser("submit")
    _read_root_option(review_submit_cmd)
    review_submit_cmd.add_argument("--request-json", required=True)
    review_submit_cmd.add_argument("--json", action="store_true")

    integration = sub.add_parser("integration")
    integration_sub = integration.add_subparsers(dest="integration_command", required=True)
    integration_analyze_cmd = integration_sub.add_parser("analyze")
    _read_root_option(integration_analyze_cmd)
    integration_analyze_cmd.add_argument("--request-json", required=True)
    integration_analyze_cmd.add_argument("--json", action="store_true")
    integration_authorize_cmd = integration_sub.add_parser("authorize")
    _read_root_option(integration_authorize_cmd)
    integration_authorize_cmd.add_argument("--request-json", required=True)
    integration_authorize_cmd.add_argument("--json", action="store_true")
    integration_integrate_cmd = integration_sub.add_parser("integrate")
    _read_root_option(integration_integrate_cmd)
    integration_integrate_cmd.add_argument("--request-json", required=True)
    integration_integrate_cmd.add_argument("--json", action="store_true")

    recovery = sub.add_parser("recovery")
    recovery_sub = recovery.add_subparsers(dest="recovery_command", required=True)
    recovery_status_cmd = recovery_sub.add_parser("status")
    _read_root_option(recovery_status_cmd)
    recovery_status_cmd.add_argument("project_id")
    recovery_status_cmd.add_argument("--json", action="store_true")
    recovery_details_cmd = recovery_sub.add_parser("details")
    _read_root_option(recovery_details_cmd)
    recovery_details_cmd.add_argument("project_id")
    recovery_details_cmd.add_argument("resource_type")
    recovery_details_cmd.add_argument("resource_id")
    recovery_details_cmd.add_argument("--json", action="store_true")

    legacy = sub.add_parser("legacy")
    legacy_sub = legacy.add_subparsers(dest="legacy_command", required=True)
    legacy_list = legacy_sub.add_parser("inventory")
    legacy_list.add_argument("--root", action="append", default=[])
    legacy_list.add_argument("--json", action="store_true")

    migration = sub.add_parser("migration")
    migration_sub = migration.add_subparsers(dest="migration_command", required=True)
    migration_inventory = migration_sub.add_parser("inventory")
    migration_inventory.add_argument("--root", action="append", required=True)
    migration_inventory.add_argument("--destination", required=True)
    migration_inventory.add_argument("--json", action="store_true")
    migration_import = migration_sub.add_parser("shadow-import")
    migration_import.add_argument("--state-root", required=True, help="explicit disposable shadow state root")
    migration_import.add_argument("--inventory", required=True)
    migration_import.add_argument("--idempotency-key", default=None)
    migration_import.add_argument("--json", action="store_true")
    migration_compare = migration_sub.add_parser("shadow-reconcile")
    _read_root_option(migration_compare)
    migration_compare.add_argument("--inventory", required=True)
    migration_compare.add_argument("--json", action="store_true")

    build = sub.add_parser("build")
    build_sub = build.add_subparsers(dest="build_command", required=True)
    build_manifest = build_sub.add_parser("manifest")
    build_manifest.add_argument("--source-root", required=True)
    build_manifest.add_argument("--repository", default=None)
    build_manifest.add_argument("--file", action="append", default=None)
    build_manifest.add_argument("--output", required=True)
    build_manifest.add_argument("--metadata-json", default=None)
    build_manifest.add_argument("--tests-json", default=None)
    build_manifest.add_argument("--json", action="store_true")
    return parser


def _payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_json_request(path: str) -> Any:
    if path == "-":
        raw = os.sys.stdin.buffer.read(64 * 1024 + 1)
    else:
        with Path(path).open("rb") as stream:
            raw = stream.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ControlPlaneError("request JSON exceeds its size bound", code="CP_INVALID_REQUEST")
    return parse_canonical_json(raw, max_bytes=64 * 1024)


def _policy(value: str | None):
    return load_policy(value) if value else load_policy()


def _open_existing_read_store(args: Any) -> ControllerStore:
    root = Path(args.state_root).expanduser() if args.state_root else Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "pi-control"
    db = Path(args.db_path).expanduser() if getattr(args, "db_path", None) else root / "control.db"
    if not db.is_absolute():
        db = root / db
    if not db.exists() or db.stat().st_size == 0:
        raise NotFoundError("controller database was not found", detail={"state_root": str(root)})
    return ControllerStore(root, db_path=db, read_only=True)


def run(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            value = ControllerClient(args.state_root).status(args.project_id, refresh=not args.no_refresh)
        elif args.command == "focus":
            value = ControllerClient(args.state_root, read_only=True).focus(args.project_id, args.resource_id, expected_resource_version=args.expected_version)
        elif args.command == "protocol":
            value = protocol_request(ControllerClient(args.state_root), _read_json_request(args.request_json))
        elif args.command == "change" and args.change_command == "submit":
            value = ControllerClient(args.state_root).submit(_read_json_request(args.request_json))
        elif args.command == "workstream" and args.workstream_command == "create":
            value = ControllerClient(args.state_root).create_workstream(_read_json_request(args.request_json))
        elif args.command == "personal" and args.personal_command == "select":
            request = _read_json_request(args.request_json)
            if not isinstance(request, dict) or set(request) != {"projectId", "conversationId", "workingCopyId", "strategy"}:
                raise ControlPlaneError("personal selection request fields are not exact", code="CP_INVALID_REQUEST")
            value = ControllerClient(args.state_root, read_only=True).select_personal(request["projectId"], request["conversationId"], request["workingCopyId"], request["strategy"])
        elif args.command == "review" and args.review_command == "request":
            value = ControllerClient(args.state_root).request_review(_read_json_request(args.request_json))
        elif args.command == "review" and args.review_command == "submit":
            value = ControllerClient(args.state_root).submit_review(_read_json_request(args.request_json))
        elif args.command == "integration" and args.integration_command == "analyze":
            value = ControllerClient(args.state_root).analyze_integration(_read_json_request(args.request_json))
        elif args.command == "integration" and args.integration_command == "authorize":
            value = ControllerClient(args.state_root).authorize_integration(_read_json_request(args.request_json))
        elif args.command == "integration" and args.integration_command == "integrate":
            value = ControllerClient(args.state_root).integrate(_read_json_request(args.request_json))
        elif args.command == "recovery" and args.recovery_command == "status":
            value = ControllerClient(args.state_root, read_only=True).recovery_status(args.project_id)
        elif args.command == "recovery" and args.recovery_command == "details":
            value = ControllerClient(args.state_root, read_only=True).technical_details(args.project_id, args.resource_type, args.resource_id)
        elif args.command == "legacy" and args.legacy_command == "inventory":
            value = inventory_legacy(args.root)
        elif args.command == "migration" and args.migration_command == "inventory":
            report = inventory_migration(args.root, destination=args.destination)
            value = report.as_dict()
        elif args.command == "migration" and args.migration_command == "shadow-import":
            report = load_inventory(args.inventory)
            value = shadow_import(report, args.state_root, idempotency_key=args.idempotency_key)
        elif args.command == "migration" and args.migration_command == "shadow-reconcile":
            report = load_inventory(args.inventory)
            with _open_existing_read_store(args) as store:
                value = shadow_reconcile(store, report)
        elif args.command == "build" and args.build_command == "manifest":
            metadata = _read_json_request(args.metadata_json) if args.metadata_json else None
            outcomes = _read_json_request(args.tests_json) if args.tests_json else None
            manifest = create_build_manifest(args.source_root, files=args.file, metadata=metadata, repository=args.repository, test_outcomes=outcomes)
            saved = write_build_manifest(manifest, args.output)
            value = saved.as_dict()
        elif args.command == "project" and args.project_command == "register":
            with ControllerStore(args.state_root, db_path=getattr(args, "db_path", None)) as store:
                value = register_project(store, args.repository, args.name, policy=_policy(args.policy))
        elif args.command == "project" and args.project_command == "inspect":
            with _open_existing_read_store(args) as store:
                value = inspect_project(store, args.project_id, policy=_policy(args.policy))
        elif args.command == "working-copy" and args.working_command == "inventory":
            with _open_existing_read_store(args) as store:
                value = inventory_working_copies(store, args.project_id, policy=_policy(args.policy))
        elif args.command == "inspect":
            with _open_existing_read_store(args) as store:
                value = inspect_project(store, args.project_id, policy=_policy(args.policy))
        elif args.command == "reconcile":
            if not args.observe_only:
                raise ControlPlaneError("reconcile requires explicit --observe-only", code="CP_INVALID_REQUEST")
            with ControllerStore(args.state_root, db_path=getattr(args, "db_path", None)) as store:
                value = reconcile_observe_only(store, args.project_id, policy=_policy(args.policy))
        else:
            with ControllerStore(args.state_root, db_path=getattr(args, "db_path", None)) as store:
                if args.command == "schema" and args.schema_command == "status":
                    value = store.schema_status().as_dict()
                elif args.command == "project" and args.project_command == "list":
                    value = store.list_projects()
                elif args.command == "operation" and args.operation_command == "list":
                    value = store.list_operations()
                elif args.command == "event" and args.event_command == "list":
                    value = store.list_events(after=args.after, limit=args.limit)
                else:  # pragma: no cover - argparse prevents this
                    parser.error("unsupported command")
                    return 2
        print(_payload(value))
        return 0
    except BaseException as error:
        payload = projected_error(error)
        print(_payload(payload), file=os.sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
