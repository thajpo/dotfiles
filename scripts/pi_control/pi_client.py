"""Public controller client for the fresh Pi system."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping, Sequence

from .changes import get_change, list_changes, submit_change, submit_change_revision
from .command_requests import command_status, request_command
from .conversations import archive_conversation, create_conversation, focus_conversation
from .dependencies import inventory_dependencies, package_review_gate, record_package_security_review, set_dependency_disposition
from .events import get_events
from .pi_store import PiStore
from .pi_protocol import PROTOCOL_VERSION, SPECS, adapt_request
from .integration import analyze_integration, integrate
from .installed_builds import register_staged_build
from .launch import attest_run, prepare_run, start_run, stop_run
from .messages import acknowledge_message, list_messages, mark_delivered, post_message, reply_message
from .models import canonical_json, new_id, utc_now, validate_id
from .package_environment import package_status, request_package_operation
from .projects import project_status, register_project, rename_project, work_index
from .pi_workstreams import create_workstream, retire_workstream
from .reviews import request_review, submit_review
from .pi_review import create_review_assignment
from .pi_reconcile import reconcile_project, reconcile_run, recover_conversation_runs, recover_lost_run


class PiClientError(ValueError):
    pass


def _git_env() -> dict[str, str]:
    return {"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "GIT_EDITOR": "true", "GIT_ASKPASS": "true"}


def _git(cwd: str | Path, args: Sequence[str]) -> str:
    if not args or any(not isinstance(item, str) or "\x00" in item for item in args):
        raise PiClientError("invalid Git command")
    allowed = {"worktree", "rev-parse", "symbolic-ref", "status", "log", "show", "diff"}
    if args[0] not in allowed:
        raise PiClientError("Git command is not allowed by the controller")
    result = subprocess.run([shutil.which("git", path=os.defpath) or "git", "-c", "core.hooksPath=/dev/null", "-c", "core.sshCommand=", "-c", "credential.helper=", *args], cwd=str(Path(cwd).resolve(strict=True)), env=_git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=120, check=False)
    if result.returncode != 0:
        raise PiClientError(f"Git command failed: {result.stderr.strip()[:512]}")
    return result.stdout.strip()


class PiControllerClient:
    def __init__(self, state_root: os.PathLike[str] | str | None = None, *, read_only: bool = False):
        self.state_root = Path(state_root).expanduser() if state_root is not None else None
        self.read_only = read_only

    def _store(self, *, mutate: bool = False) -> PiStore:
        if mutate and self.read_only:
            raise PiClientError("read-only client cannot mutate")
        return PiStore(self.state_root, read_only=not mutate)

    @staticmethod
    def _public_environment(environment: Mapping[str, str]) -> dict[str, str]:
        return dict(environment)

    def negotiate(self) -> dict[str, Any]:
        with self._store() as store:
            return {"protocolVersion": PROTOCOL_VERSION, "schema": store.schema_status().as_dict(), "operations": sorted(SPECS)}

    def register_project(self, repository: str, display_name: str | None = None) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return register_project(store, repository, display_name)

    def status(self, project_id: str) -> dict[str, Any]:
        with self._store() as store:
            return project_status(store, project_id)

    def rename_project(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return rename_project(store, **request)

    def work_index(self, project_id: str) -> dict[str, list[dict[str, Any]]]:
        with self._store() as store:
            return work_index(store, project_id)

    def create_conversation(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return create_conversation(store, **request)

    def register_build(self, staged_root: str) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return register_staged_build(store, staged_root)

    def create_workstream(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return create_workstream(store, **request)

    def retire_workstream(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return retire_workstream(store, **request)

    def focus_conversation(self, **request: Any) -> dict[str, Any]:
        with self._store() as store:
            return focus_conversation(store, **request)

    def archive_conversation(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return archive_conversation(store, **request)

    def post_message(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return post_message(store, **request)

    def list_messages(self, **request: Any) -> list[dict[str, Any]]:
        with self._store() as store:
            return list_messages(store, **request)

    def deliver_message(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return mark_delivered(store, **request)

    def acknowledge_message(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return acknowledge_message(store, **request)

    def reply_message(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return reply_message(store, **request)

    def request_command(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return request_command(store, **request)

    def command_status(self, **request: Any) -> dict[str, Any]:
        with self._store() as store:
            return command_status(store, **request)

    def analyze_integration(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return analyze_integration(
                store,
                project_id=request["project_id"],
                change_id=request["change_id"],
                revision=request["revision"],
                target_working_copy_id=request["target_working_copy_id"],
                target_ref=request["target_ref"],
                integration_id=request.get("integration_id"),
            ).as_dict()

    def authorize_integration(self, **request: Any) -> dict[str, Any]:
        from .integration import authorize_integration
        with self._store(mutate=True) as store:
            return authorize_integration(
                store,
                integration_id=request["integration_id"],
                actor_id=request["actor_id"],
                request_context_id=request["request_context_id"],
                expires_at=request["expires_at"],
                review_id=request.get("review_id"),
            )

    def integrate(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return integrate(
                store,
                integration_id=request["integration_id"],
                authorization_id=request["authorization_id"],
                expected_resource_version=request.get("expected_resource_version"),
                failpoint=request.get("failpoint"),
            ).as_dict()

    def prepare_run(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            conversation = store.conn.execute("SELECT authority_profile FROM conversations WHERE conversation_id=?", (request.get("conversation_id"),)).fetchone()
            if conversation is not None and conversation[0] == "writer-container":
                raise PiClientError("writer run preparation must be held by bin/pi-system-container-run")
            prepared = prepare_run(store, **request)
            # The controller returns exact launch data; the lock is kept by the
            # actual launcher process, not by this short-lived CLI connection.
            result = {"run": prepared.run, "manifest": prepared.manifest, "manifestPath": str(prepared.manifest_path), "environment": self._public_environment(prepared.environment)}
            prepared.close()
            return result

    def attest_run(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return attest_run(store, **request)

    def start_run(self, **request: Any) -> dict[str, Any]:
        raise PiClientError("run.start must be executed by bin/pi-system-container-run so the writer lock remains held")

    def stop_run(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return stop_run(store, **request)

    def reconcile_run(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return reconcile_run(store, **request)

    def recover_run(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return recover_lost_run(store, **request)

    def recover_conversation(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return recover_conversation_runs(store, **request)

    def reconcile_project(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return reconcile_project(store, **request)

    def submit_change(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return submit_change(store, **request).as_dict()

    def submit_change_revision(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return submit_change_revision(store, **request).as_dict()

    def list_changes(self, project_id: str | None = None) -> list[dict[str, Any]]:
        with self._store() as store:
            return list_changes(store, project_id=project_id)

    def get_change(self, change_id: str) -> dict[str, Any]:
        with self._store() as store:
            return get_change(store, change_id)

    def request_review(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return request_review(
                store,
                change_id=request["change_id"],
                revision=request["revision"],
                reviewer_conversation_id=request.get("reviewer_conversation_id"),
                reviewer_run_id=request.get("reviewer_run_id"),
                reviewer_actor_id=request.get("reviewer_actor_id"),
                reviewer_capability_secret=request.get("reviewer_capability_secret"),
                evidence=request.get("evidence"),
                review_id=request.get("review_id"),
                dependency_review_digest=request.get("dependency_review_digest"),
            ).as_dict()

    def create_review_assignment(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            result = create_review_assignment(store, **request)
            if isinstance(result.get("environment"), Mapping):
                result["environment"] = self._public_environment(result["environment"])
            return result

    def submit_review(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return submit_review(
                store,
                review_id=request["review_id"],
                verdict=request["verdict"],
                summary=request.get("summary", ""),
                findings=request.get("findings", ""),
                evidence=request.get("evidence"),
                reviewer_run_id=request.get("reviewer_run_id"),
                reviewer_actor_id=request.get("reviewer_actor_id"),
                reviewer_capability_secret=request.get("reviewer_capability_secret"),
            ).as_dict()

    def detect_dependencies(self, **request: Any) -> list[dict[str, Any]]:
        with self._store(mutate=True) as store:
            return inventory_dependencies(store, **request)

    def request_package_operation(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return request_package_operation(store, **request)

    def package_status(self, **request: Any) -> dict[str, Any]:
        with self._store() as store:
            return package_status(store, **request)

    def set_dependency_disposition(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return set_dependency_disposition(store, **request)

    def record_package_security_review(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return record_package_security_review(store, **request)

    def package_review_gate(self, **request: Any) -> dict[str, Any]:
        with self._store() as store:
            return package_review_gate(store, **request)

    def dispatch(self, operation: str, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        value = adapt_request(operation, request or {})
        routes = {
            "build.register": self.register_build,
            "project.register": self.register_project,
            "project.rename": self.rename_project,
            "project.status": lambda **kw: self.status(kw["project_id"]),
            "project.work-index": lambda **kw: self.work_index(kw["project_id"]),
            "conversation.create": self.create_conversation,
            "workstream.create": self.create_workstream,
            "workstream.retire": self.retire_workstream,
            "conversation.focus": self.focus_conversation,
            "conversation.archive": self.archive_conversation,
            "conversation.recover": self.recover_conversation,
            "message.post": self.post_message,
            "message.list": self.list_messages,
            "message.deliver": self.deliver_message,
            "message.acknowledge": self.acknowledge_message,
            "message.reply": self.reply_message,
            "command.request": self.request_command,
            "command.status": self.command_status,
            "run.prepare": self.prepare_run,
            "run.start": self.start_run,
            "run.attest": self.attest_run,
            "run.stop": self.stop_run,
            "run.reconcile": self.reconcile_run,
            "run.recover": self.recover_run,
            "project.reconcile": self.reconcile_project,
            "change.submit": self.submit_change,
            "change.revise": self.submit_change_revision,
            "change.list": self.list_changes,
            "change.show": self.get_change,
            "review.request": self.request_review,
            "review.submit": self.submit_review,
            "review.create-assignment": self.create_review_assignment,
            "integration.analyze": self.analyze_integration,
            "integration.authorize": self.authorize_integration,
            "integration.integrate": self.integrate,
            "dependency.inventory": self.detect_dependencies,
            "dependency.disposition": self.set_dependency_disposition,
            "package-review.record": self.record_package_security_review,
            "package-review.gate": self.package_review_gate,
            "package.request": self.request_package_operation,
            "package.status": self.package_status,
        }
        if operation == "negotiate":
            return self.negotiate()
        if operation not in routes:
            raise PiClientError("unsupported Pi controller operation")
        return routes[operation](**value)


__all__ = ["PiClientError", "PiControllerClient"]
