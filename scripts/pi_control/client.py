"""Producer-neutral controller client facade for Phase 8.

This is a direct library client, not a daemon or second state store.  Launchers,
secretary code, and extensions use the same bounded semantic operations here;
presentation systems never become lifecycle authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .changes import submit_change
from .errors import ActivationMismatchError, ConstraintError, ControlPlaneError, NotFoundError, ResourceStaleError, UnsafeDatabaseError
from .events import append_event_in_transaction
from .integration import analyze_integration, authorize_integration, integrate
from .models import bounded_text, canonical_json, new_id, parse_canonical_json, utc_now, validate_id, validate_pi_session_id
from .reviews import request_review, submit_review
from .reconcile import inspect_project, reconcile_observe_only
from .store import ControllerStore

PROTOCOL_VERSION = 2
_MAX_REQUEST_BYTES = 64 * 1024
_OPERATION_NAMES = frozenset({
    "negotiate", "status", "focus", "submit", "create-workstream", "select-personal",
    "review-request", "review-submit", "integration-analyze", "integration-authorize",
    "integration-integrate", "recovery-status", "technical-details",
    "activation.inspect", "activation.plan", "activation.apply", "activation.rollback",
    "launch.resolve", "conversation.ensure", "conversation.archive", "conversation.focus",
    "run.prepare", "run.attest", "run.start", "run.stop", "run.reconcile",
    "workstream.plan", "workstream.create", "workstream.focus", "workstream.relaunch",
    "workstream.retire", "presentation.observe", "presentation.assign", "migration.inventory",
    "migration.plan", "migration.shadow-import", "migration.compare", "migration.final-import",
    "migration.cutover", "migration.rollback", "change.submit", "change.close",
    "review.request", "review.submit", "integration.analyze", "integration.authorize",
    "integration.apply", "cleanup.plan", "cleanup.apply", "publish", "personal.select",
})
_OPERATION_ALIASES = {
    "change.submit": "submit", "workstream.create": "create-workstream",
    "review.request": "review-request", "review.submit": "review-submit",
    "integration.analyze": "integration-analyze", "integration.authorize": "integration-authorize",
    "integration.apply": "integration-integrate", "conversation.focus": "focus",
    "personal.select": "select-personal",
}
_HOST_ONLY_OPERATIONS = frozenset({
    "activation.apply", "activation.rollback", "migration.final-import",
    "migration.cutover", "migration.rollback", "integration.apply", "cleanup.apply", "publish",
})
_PLANNED_OPERATIONS = _OPERATION_NAMES - {"negotiate", "status", "focus", "submit", "create-workstream", "select-personal", "review-request", "review-submit", "integration-analyze", "integration-authorize", "integration-integrate", "recovery-status", "technical-details", "personal.select", "launch.resolve", "workstream.plan", "workstream.focus", "workstream.relaunch", "workstream.retire"} - _HOST_ONLY_OPERATIONS
_PLANNED_FIELDS = frozenset({
    "projectId", "conversationId", "workingCopyId", "workstreamId", "resourceId", "changeId", "reviewId",
    "runId", "migrationId", "inventoryId", "resolutionId", "buildId", "authorizationId", "idempotencyKey",
    "expectedResourceVersion", "expectedProjectVersion", "requestContextId", "targetRef", "mode", "backend",
    "strategy", "revision", "desiredState", "observedState", "reason", "scope", "options",
})
_SESSION_PATH = re.compile(r"^/[^\x00]{1,2048}$")
_REDACTED_FIELD = re.compile(r"(?:secret|capability|token|password|credential|private.?key)", re.IGNORECASE)


class ClientProtocolError(ControlPlaneError):
    """A bounded producer request is malformed or unsupported."""


def _strict_mapping(value: Any, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ClientProtocolError(f"{name} fields are not exact")
    return dict(value)


def _optional_mapping(value: Any, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not set(value).issubset(keys):
        raise ClientProtocolError(f"{name} fields are not allowlisted")
    return dict(value)


def _absolute_session_path(value: Any) -> str:
    if not isinstance(value, str) or _SESSION_PATH.fullmatch(value) is None:
        raise ClientProtocolError("session file must be an absolute bounded path")
    return value


def _resource_version(row: Mapping[str, Any], expected: Any) -> None:
    if expected is None:
        return
    if not isinstance(expected, int) or expected < 1:
        raise ClientProtocolError("expected resource version is invalid")
    actual = int(row["resource_version"])
    if actual != expected:
        raise ResourceStaleError(str(row.get("project_id") or row.get("conversation_id") or row.get("working_copy_id") or row.get("change_id") or row.get("run_id")), expected, actual)


@dataclass(frozen=True)
class ClientResponse:
    protocol_version: int
    operation: str
    value: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"protocolVersion": self.protocol_version, "operation": self.operation, "value": dict(self.value)}


class ControllerClient:
    """Bounded direct client shared by personal, secretary, and extensions."""

    def __init__(self, state_root: os.PathLike[str] | str | None = None, *, read_only: bool = False):
        if state_root is None:
            state_root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "pi-control"
        self.state_root = Path(state_root).expanduser()
        self.read_only = read_only

    def _store(self, *, mutate: bool = False) -> ControllerStore:
        if mutate and self.read_only:
            raise UnsafeDatabaseError("read-only controller client cannot mutate")
        return ControllerStore(self.state_root, read_only=not mutate)

    def negotiate(self) -> dict[str, Any]:
        with self._store(mutate=False) as store:
            status = store.schema_status().as_dict()
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "supportedProtocolVersions": [PROTOCOL_VERSION],
            "schemaVersion": status["schema_version"],
            "readOnly": self.read_only,
            "operations": sorted(_OPERATION_NAMES),
        }

    @staticmethod
    def _planned(operation: str, request: Mapping[str, Any] | None) -> dict[str, Any]:
        if request is None:
            request = {}
        if not isinstance(request, Mapping) or not set(request).issubset(_PLANNED_FIELDS):
            raise ClientProtocolError("semantic request fields are not allowlisted")
        # Planned effects are intentionally a pure shape/fake-effect response.
        # They do not create an operation row, authorization, or external effect.
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "operation": operation,
            "planned": True,
            "authority": "controller",
            "effects": [],
            "requestKeys": sorted(str(key) for key in request),
        }

    def status(self, project_id: str, *, refresh: bool = True) -> dict[str, Any]:
        validate_id(project_id, prefix="prj")
        with self._store(mutate=refresh) as store:
            if refresh:
                observed = reconcile_observe_only(store, project_id)
            else:
                observed = inspect_project(store, project_id)
            project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if project is None:
                raise NotFoundError("project was not found", detail={"project_id": project_id})
            worktrees = [dict(row) for row in store.conn.execute("SELECT * FROM working_copies WHERE project_id=? ORDER BY created_at", (project_id,))]
            changes = [dict(row) for row in store.conn.execute("SELECT * FROM changes WHERE project_id=? ORDER BY created_at", (project_id,))]
            attention = [dict(row) for row in store.conn.execute("SELECT * FROM attention WHERE project_id=? AND state='open' ORDER BY created_at", (project_id,))]
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "project": dict(project),
                "workingCopies": worktrees,
                "changes": changes,
                "attention": attention,
                "observation": observed,
                "facts": {"source": "controller+git-observation", "refreshed": refresh},
                "intent": {"desiredState": project["desired_state"]},
            }

    def launch_resolve(self, request: Mapping[str, Any]) -> dict[str, Any]:
        fields = {"projectId", "conversationId", "workingCopyId", "sessionFile", "expectedActivationResourceVersion"}
        value = _strict_mapping(request, fields, "launch.resolve")
        validate_id(value["projectId"], prefix="prj")
        validate_id(value["conversationId"], prefix="conv")
        validate_id(value["workingCopyId"], prefix="wc")
        _absolute_session_path(value["sessionFile"])
        expected = value["expectedActivationResourceVersion"]
        if not isinstance(expected, int) or expected < 1:
            raise ClientProtocolError("activation resource version is invalid")
        with self._store(mutate=False) as store:
            activation = store.conn.execute("SELECT * FROM project_activations WHERE project_id=?", (value["projectId"],)).fetchone()
            project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (value["projectId"],)).fetchone()
            conversation = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (value["conversationId"],)).fetchone()
            working_copy = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (value["workingCopyId"],)).fetchone()
            if activation is None or project is None or conversation is None or working_copy is None:
                raise NotFoundError("launch binding resource was not found")
            if activation["mode"] != "controller" or int(activation["resource_version"]) != expected:
                raise ActivationMismatchError("controller activation is unavailable")
            if conversation["project_id"] != value["projectId"] or conversation["working_copy_id"] != value["workingCopyId"] or conversation["session_file"] != value["sessionFile"]:
                raise ConstraintError("launch binding does not match exact conversation")
            if working_copy["project_id"] != value["projectId"]:
                raise ConstraintError("launch working copy belongs to another project")
            return {"protocolVersion": PROTOCOL_VERSION, "operation": "launch.resolve", "projectId": value["projectId"], "conversationId": value["conversationId"], "workingCopyId": value["workingCopyId"], "sessionFile": value["sessionFile"], "buildId": activation["controller_build_id"], "activationResourceVersion": int(activation["resource_version"]), "authority": "controller"}

    def focus(self, project_id: str, resource_id: str, *, expected_resource_version: int | None = None) -> dict[str, Any]:
        validate_id(project_id, prefix="prj")
        if not isinstance(resource_id, str) or not resource_id:
            raise ClientProtocolError("focus requires an exact resource ID")
        try:
            prefix = resource_id.split("_", 1)[0]
            validate_id(resource_id, prefix=prefix)
        except (ValueError, IndexError) as error:
            raise ClientProtocolError("focus requires a controller resource ID, not a label or fuzzy query") from error
        with self._store(mutate=False) as store:
            table_by_prefix = {"conv": ("conversations", "conversation_id"), "wc": ("working_copies", "working_copy_id"), "chg": ("changes", "change_id"), "run": ("runs", "run_id")}
            if prefix not in table_by_prefix:
                raise ClientProtocolError("resource type cannot be focused")
            table, column = table_by_prefix[prefix]
            row = store.conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (resource_id,)).fetchone()
            if row is None:
                raise NotFoundError("focus target was not found", detail={"resource_id": resource_id})
            if row["project_id"] != project_id:
                raise ConstraintError("focus target belongs to another project")
            _resource_version(dict(row), expected_resource_version)
            return {"protocolVersion": PROTOCOL_VERSION, "resourceType": prefix, "resourceId": resource_id, "resource": dict(row), "presentationOnly": True}

    def submit(self, request: Mapping[str, Any]) -> dict[str, Any]:
        fields = {"projectId", "workingCopyId", "targetRef", "title", "summary", "captureMode", "selectedPaths", "excludedPaths", "expectedStatusHash", "idempotencyKey", "conversationId", "actorType", "actorId", "authorizationId"}
        value = _strict_mapping(request, fields, "submit")
        with self._store(mutate=True) as store:
            result = submit_change(
                store, project_id=value["projectId"], working_copy_id=value["workingCopyId"], target_ref=value["targetRef"],
                title=value["title"], summary=value["summary"], capture_mode=value["captureMode"],
                selected_paths=value["selectedPaths"], excluded_paths=value["excludedPaths"], expected_status_hash=value["expectedStatusHash"],
                idempotency_key=value["idempotencyKey"], created_by_conversation_id=value["conversationId"],
                actor_type=value["actorType"], actor_id=value["actorId"], authorization_id=value["authorizationId"],
            )
            return result.as_dict()

    def create_workstream(self, request: Mapping[str, Any]) -> dict[str, Any]:
        fields = {"projectId", "workingCopyId", "displayName", "piSessionId", "sessionFile", "approval"}
        value = _strict_mapping(request, fields, "create-workstream")
        approval = _strict_mapping(value["approval"], {"action", "projectId", "workingCopyId", "approved"}, "approval")
        if approval != {"action": "create-workstream", "projectId": value["projectId"], "workingCopyId": value["workingCopyId"], "approved": True}:
            raise ClientProtocolError("workstream creation requires exact semantic approval")
        validate_id(value["projectId"], prefix="prj")
        validate_id(value["workingCopyId"], prefix="wc")
        validate_pi_session_id(value["piSessionId"])
        display_name = bounded_text(value["displayName"], name="displayName", limit=512)
        session_file = _absolute_session_path(value["sessionFile"])
        with self._store(mutate=True) as store:
            with store.transaction():
                wc = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=? AND project_id=?", (value["workingCopyId"], value["projectId"])).fetchone()
                if wc is None:
                    raise NotFoundError("workstream working copy was not found")
                if wc["kind"] not in {"worktree", "isolated"} or not bool(wc["controller_owned"]):
                    raise ConstraintError("workstream requires an explicit controller-owned separate worktree")
                conversation_id = new_id("conv")
                now = utc_now()
                store.conn.execute(
                    "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (conversation_id, value["projectId"], value["workingCopyId"], "workstream", display_name, value["piSessionId"], session_file, "active", "unknown", 1, now, now),
                )
                append_event_in_transaction(store.conn, event_kind="workstream.created", resource_type="conversation", resource_id=conversation_id, resource_version=1, payload={"conversationId": conversation_id, "projectId": value["projectId"], "workingCopyId": value["workingCopyId"]})
                return dict(store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone())

    @staticmethod
    def _redact_value(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): ControllerClient._redact_value(item) for key, item in value.items() if not _REDACTED_FIELD.search(str(key))}
        if isinstance(value, list):
            return [ControllerClient._redact_value(item) for item in value[:128]]
        return value

    @staticmethod
    def _redact_record(row: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in row.items():
            name = str(key)
            if _REDACTED_FIELD.search(name):
                continue
            if name.endswith("_json") and isinstance(value, str):
                try:
                    value = ControllerClient._redact_value(parse_canonical_json(value))
                except ControlPlaneError:
                    value = "[unavailable]"
            result[name] = value
        return result

    def recovery_status(self, project_id: str) -> dict[str, Any]:
        validate_id(project_id, prefix="prj")
        with self._store(mutate=False) as store:
            project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if project is None:
                raise NotFoundError("recovery project was not found", detail={"project_id": project_id})
            resource_ids = [project_id]
            for table, column in (("working_copies", "working_copy_id"), ("conversations", "conversation_id"), ("runs", "run_id"), ("changes", "change_id"), ("integration_attempts", "integration_id")):
                resource_ids.extend(str(row[0]) for row in store.conn.execute(f"SELECT {column} FROM {table} WHERE project_id=?", (project_id,)))
            placeholders = ",".join("?" for _ in resource_ids)
            operations = [self._redact_record(dict(row)) for row in store.conn.execute(f"SELECT * FROM operations WHERE resource_id IN ({placeholders}) ORDER BY created_at DESC LIMIT 128", resource_ids)]
            events = [self._redact_record(dict(row)) for row in store.conn.execute(f"SELECT * FROM control_events WHERE resource_id IN ({placeholders}) ORDER BY sequence DESC LIMIT 128", resource_ids)]
            return {"schemaVersion": 1, "projectId": project_id, "project": self._redact_record(dict(project)), "operations": operations, "events": list(reversed(events)), "attention": [self._redact_record(dict(row)) for row in store.conn.execute("SELECT * FROM attention WHERE project_id=? ORDER BY created_at DESC LIMIT 128", (project_id,))], "integrations": [self._redact_record(dict(row)) for row in store.conn.execute("SELECT * FROM integration_attempts WHERE project_id=? ORDER BY created_at DESC LIMIT 128", (project_id,))]}

    @staticmethod
    def _project_for_resource(store: ControllerStore, resource_type: str, resource_id: str) -> str | None:
        normalized = resource_type.replace("-", "_")
        if normalized == "project":
            row = store.conn.execute("SELECT project_id FROM projects WHERE project_id=?", (resource_id,)).fetchone()
        elif normalized in {"working_copy", "conversation", "run", "change"}:
            table, column = {"working_copy": ("working_copies", "working_copy_id"), "conversation": ("conversations", "conversation_id"), "run": ("runs", "run_id"), "change": ("changes", "change_id")}[normalized]
            row = store.conn.execute(f"SELECT project_id FROM {table} WHERE {column}=?", (resource_id,)).fetchone()
        elif normalized == "integration":
            row = store.conn.execute("SELECT project_id FROM integration_attempts WHERE integration_id=?", (resource_id,)).fetchone()
        elif normalized == "review":
            row = store.conn.execute("SELECT c.project_id FROM reviews r JOIN changes c ON c.change_id=r.change_id WHERE r.review_id=?", (resource_id,)).fetchone()
        else:
            return None
        return str(row[0]) if row is not None and row[0] is not None else None

    def technical_details(self, project_id: str, resource_type: str, resource_id: str) -> dict[str, Any]:
        validate_id(project_id, prefix="prj")
        tables = {"project": ("projects", "project_id", "prj"), "working-copy": ("working_copies", "working_copy_id", "wc"), "conversation": ("conversations", "conversation_id", "conv"), "run": ("runs", "run_id", "run"), "change": ("changes", "change_id", "chg"), "review": ("reviews", "review_id", "review"), "integration": ("integration_attempts", "integration_id", "int"), "operation": ("operations", "operation_id", "op"), "migration": ("migration_runs", "migration_id", "mig")}
        if resource_type not in tables:
            raise ClientProtocolError("technical-details resource type is invalid")
        table, column, prefix = tables[resource_type]
        validate_id(resource_id, prefix=prefix)
        with self._store(mutate=False) as store:
            if store.conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone() is None:
                raise NotFoundError("technical-details project was not found", detail={"project_id": project_id})
            row = store.conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (resource_id,)).fetchone()
            if row is None:
                raise NotFoundError("technical-details resource was not found in the project", detail={"project_id": project_id, "resource_id": resource_id})
            if resource_type == "operation":
                owner_project_id = self._project_for_resource(store, str(row["resource_type"]), str(row["resource_id"]))
            elif resource_type == "migration":
                # Shadow migrations are global to their disposable state root,
                # not project-scoped. Do not expose them through a project tool.
                owner_project_id = None
            else:
                owner_project_id = self._project_for_resource(store, resource_type, resource_id)
            if owner_project_id != project_id:
                raise NotFoundError("technical-details resource was not found in the project", detail={"project_id": project_id, "resource_id": resource_id})
            if resource_type == "operation":
                event_rows = store.conn.execute("SELECT * FROM control_events WHERE operation_id=? ORDER BY sequence DESC LIMIT 64", (resource_id,))
            else:
                event_type = resource_type.replace("-", "_")
                event_rows = store.conn.execute("SELECT * FROM control_events WHERE resource_type=? AND resource_id=? ORDER BY sequence DESC LIMIT 64", (event_type, resource_id))
            events = [self._redact_record(dict(item)) for item in event_rows]
            return {"schemaVersion": 1, "projectId": project_id, "resourceType": resource_type, "resourceId": resource_id, "resource": self._redact_record(dict(row)), "events": list(reversed(events)), "readOnly": True}

    def request_review(self, request: Mapping[str, Any]) -> dict[str, Any]:
        fields = {"changeId", "revision", "reviewerConversationId", "reviewerRunId", "reviewerActorId", "reviewerCapabilitySecret", "evidence", "reviewId"}
        value = _strict_mapping(request, fields, "review-request")
        with self._store(mutate=True) as store:
            result = request_review(
                store,
                change_id=value["changeId"],
                revision=value["revision"],
                reviewer_conversation_id=value["reviewerConversationId"],
                reviewer_run_id=value["reviewerRunId"],
                reviewer_actor_id=value["reviewerActorId"],
                reviewer_capability_secret=value["reviewerCapabilitySecret"],
                evidence=value["evidence"],
                review_id=value["reviewId"],
            )
            return result.as_dict()

    def submit_review(self, request: Mapping[str, Any]) -> dict[str, Any]:
        fields = {"reviewId", "verdict", "summary", "findings", "evidence", "reviewerRunId", "reviewerActorId", "reviewerCapabilitySecret"}
        value = _strict_mapping(request, fields, "review-submit")
        with self._store(mutate=True) as store:
            result = submit_review(
                store,
                review_id=value["reviewId"],
                verdict=value["verdict"],
                summary=value["summary"],
                findings=value["findings"],
                evidence=value["evidence"],
                reviewer_run_id=value["reviewerRunId"],
                reviewer_actor_id=value["reviewerActorId"],
                reviewer_capability_secret=value["reviewerCapabilitySecret"],
            )
            return result.as_dict()

    def analyze_integration(self, request: Mapping[str, Any]) -> dict[str, Any]:
        fields = {"projectId", "changeId", "revision", "targetWorkingCopyId", "targetRef", "integrationId"}
        value = _strict_mapping(request, fields, "integration-analyze")
        with self._store(mutate=True) as store:
            result = analyze_integration(
                store,
                project_id=value["projectId"],
                change_id=value["changeId"],
                revision=value["revision"],
                target_working_copy_id=value["targetWorkingCopyId"],
                target_ref=value["targetRef"],
                integration_id=value["integrationId"],
            )
            return result.as_dict()

    def authorize_integration(self, request: Mapping[str, Any]) -> dict[str, Any]:
        fields = {"integrationId", "actorId", "requestContextId", "expiresAt", "reviewId"}
        value = _strict_mapping(request, fields, "integration-authorize")
        with self._store(mutate=True) as store:
            return authorize_integration(
                store,
                integration_id=value["integrationId"],
                actor_id=value["actorId"],
                request_context_id=value["requestContextId"],
                expires_at=value["expiresAt"],
                review_id=value["reviewId"],
            )

    def integrate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        fields = {"integrationId", "authorizationId", "expectedResourceVersion"}
        value = _strict_mapping(request, fields, "integration-integrate")
        with self._store(mutate=True) as store:
            return integrate(
                store,
                integration_id=value["integrationId"],
                authorization_id=value["authorizationId"],
                expected_resource_version=value["expectedResourceVersion"],
            ).as_dict()

    def select_personal(self, project_id: str, conversation_id: str, working_copy_id: str, strategy: str) -> dict[str, Any]:
        validate_id(project_id, prefix="prj")
        validate_id(conversation_id, prefix="conv")
        validate_id(working_copy_id, prefix="wc")
        if strategy not in {"primary", "separate-worktree"}:
            raise ClientProtocolError("personal working-copy strategy is invalid")
        with self._store(mutate=False) as store:
            conversation = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            wc = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()
            if conversation is None or wc is None or conversation["project_id"] != project_id or wc["project_id"] != project_id:
                raise NotFoundError("personal selection target was not found")
            if conversation["role"] != "personal" or conversation["working_copy_id"] != working_copy_id:
                raise ConstraintError("personal selection is not bound to the exact conversation working copy")
            expected_kind = "primary" if strategy == "primary" else "worktree"
            if wc["kind"] != expected_kind:
                raise ConstraintError("personal strategy does not match the selected working copy")
            return {"projectId": project_id, "conversationId": conversation_id, "workingCopyId": working_copy_id, "strategy": strategy, "path": wc["path"], "presentationOnly": True}

    def dispatch(self, operation: str, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if operation not in _OPERATION_NAMES:
            raise ClientProtocolError("unsupported controller client operation")
        if operation in _HOST_ONLY_OPERATIONS:
            raise ClientProtocolError("host-only semantic operation is unavailable to ordinary clients")
        if operation in _PLANNED_OPERATIONS:
            return self._planned(operation, request)
        operation = _OPERATION_ALIASES.get(operation, operation)
        if operation == "negotiate":
            return self.negotiate()
        if operation == "launch.resolve":
            return self.launch_resolve(request or {})
        if operation == "workstream.plan":
            from .workstreams import plan_workstream
            with self._store(mutate=False) as store:
                return plan_workstream(store, request or {})
        if operation in {"workstream.focus", "workstream.relaunch", "workstream.retire"}:
            from .workstreams import focus_workstream, relaunch_workstream, retire_workstream
            value = _strict_mapping(request, {"workstreamId", "expectedResourceVersion"}, operation)
            if operation == "workstream.focus":
                with self._store(mutate=False) as store:
                    return focus_workstream(store, value["workstreamId"])
            with self._store(mutate=True) as store:
                method = relaunch_workstream if operation == "workstream.relaunch" else retire_workstream
                return method(store, value["workstreamId"], expected_resource_version=value["expectedResourceVersion"])
        if operation == "status":
            value = _strict_mapping(request, {"projectId", "refresh"}, "status")
            if not isinstance(value["refresh"], bool):
                raise ClientProtocolError("status refresh must be boolean")
            return self.status(value["projectId"], refresh=value["refresh"])
        if operation == "focus":
            value = _strict_mapping(request, {"projectId", "resourceId", "expectedResourceVersion"}, "focus")
            return self.focus(value["projectId"], value["resourceId"], expected_resource_version=value["expectedResourceVersion"])
        if operation == "submit":
            return self.submit(request or {})
        if operation == "create-workstream":
            return self.create_workstream(request or {})
        if operation == "review-request":
            return self.request_review(request or {})
        if operation == "review-submit":
            return self.submit_review(request or {})
        if operation == "integration-analyze":
            return self.analyze_integration(request or {})
        if operation == "integration-authorize":
            return self.authorize_integration(request or {})
        if operation == "integration-integrate":
            return self.integrate(request or {})
        if operation == "recovery-status":
            value = _strict_mapping(request, {"projectId"}, "recovery-status")
            return self.recovery_status(value["projectId"])
        if operation == "technical-details":
            value = _strict_mapping(request, {"projectId", "resourceType", "resourceId"}, "technical-details")
            return self.technical_details(value["projectId"], value["resourceType"], value["resourceId"])
        value = _strict_mapping(request, {"projectId", "conversationId", "workingCopyId", "strategy"}, "select-personal")
        return self.select_personal(value["projectId"], value["conversationId"], value["workingCopyId"], value["strategy"])


def protocol_request(client: ControllerClient, payload: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        value = parse_canonical_json(canonical_json(payload, max_bytes=_MAX_REQUEST_BYTES), max_bytes=_MAX_REQUEST_BYTES)
    else:
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        if not isinstance(raw, bytes) or len(raw) > _MAX_REQUEST_BYTES:
            raise ClientProtocolError("controller request exceeds its size bound")
        value = parse_canonical_json(raw, max_bytes=_MAX_REQUEST_BYTES)
    request = _strict_mapping(value, {"protocolVersion", "operation", "request"}, "protocol")
    if isinstance(request["protocolVersion"], bool) or request["protocolVersion"] != PROTOCOL_VERSION:
        raise ClientProtocolError("unsupported controller protocol version")
    if not isinstance(request["operation"], str) or request["operation"] not in _OPERATION_NAMES:
        raise ClientProtocolError("controller operation is invalid")
    if not isinstance(request["request"], Mapping):
        raise ClientProtocolError("controller request body must be an object")
    result = client.dispatch(request["operation"], request["request"])
    response = ClientResponse(PROTOCOL_VERSION, request["operation"], result).as_dict()
    try:
        canonical_json(response, max_bytes=_MAX_REQUEST_BYTES)
    except Exception as error:
        raise ClientProtocolError("controller response exceeds its size bound") from error
    return response


__all__ = ["ClientProtocolError", "ClientResponse", "ControllerClient", "PROTOCOL_VERSION", "protocol_request"]
