"""Public controller client for the fresh Pi system."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping, Sequence

from .changes import get_change, list_changes, submit_change
from .command_requests import authorize_command, consume_authorization, reject_command, request_command
from .conversations import archive_conversation, create_conversation, focus_conversation
from .dependencies import detect_dependencies, package_review_gate, record_package_security_review, set_dependency_disposition
from .events import get_events
from .greenfield_store import GreenfieldStore
from .integration import analyze_integration, integrate
from .launch import attest_run, ensure_installed_build, prepare_run, stop_run
from .messages import acknowledge_message, list_messages, mark_delivered, post_message, reply_message
from .models import canonical_json, new_id, utc_now, validate_id
from .projects import project_status, register_project, work_index


class GreenfieldClientError(ValueError):
    pass


def _git_env() -> dict[str, str]:
    return {"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "GIT_EDITOR": "true", "GIT_ASKPASS": "true"}


def _git(cwd: str | Path, args: Sequence[str]) -> str:
    if not args or any(not isinstance(item, str) or "\x00" in item for item in args):
        raise GreenfieldClientError("invalid Git command")
    allowed = {"worktree", "rev-parse", "symbolic-ref", "status", "log", "show", "diff"}
    if args[0] not in allowed:
        raise GreenfieldClientError("Git command is not allowed by the controller")
    result = subprocess.run([shutil.which("git", path=os.defpath) or "git", "-c", "core.hooksPath=/dev/null", "-c", "core.sshCommand=", "-c", "credential.helper=", *args], cwd=str(Path(cwd).resolve(strict=True)), env=_git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=120, check=False)
    if result.returncode != 0:
        raise GreenfieldClientError(f"Git command failed: {result.stderr.strip()[:512]}")
    return result.stdout.strip()


class GreenfieldControllerClient:
    def __init__(self, state_root: os.PathLike[str] | str | None = None, *, read_only: bool = False):
        self.state_root = Path(state_root).expanduser() if state_root is not None else None
        self.read_only = read_only

    def _store(self, *, mutate: bool = False) -> GreenfieldStore:
        if mutate and self.read_only:
            raise GreenfieldClientError("read-only client cannot mutate")
        return GreenfieldStore(self.state_root, read_only=not mutate)

    def negotiate(self) -> dict[str, Any]:
        with self._store() as store:
            return {"protocolVersion": 1, "schema": store.schema_status().as_dict(), "operations": ["project.register", "project.status", "project.work-index", "conversation.create", "conversation.focus", "conversation.archive", "message.post", "message.list", "message.acknowledge", "message.reply", "command.request", "command.authorize", "command.reject", "run.prepare", "run.attest", "run.stop", "change.submit", "review.request", "review.submit", "integration.analyze", "integration.integrate", "dependency.detect", "package-review.record"]}

    def register_project(self, repository: str, display_name: str | None = None) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return register_project(store, repository, display_name)

    def status(self, project_id: str) -> dict[str, Any]:
        with self._store() as store:
            return project_status(store, project_id)

    def work_index(self, project_id: str) -> dict[str, list[dict[str, Any]]]:
        with self._store() as store:
            return work_index(store, project_id)

    def create_conversation(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return create_conversation(store, **request)

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

    def acknowledge_message(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return acknowledge_message(store, **request)

    def reply_message(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return reply_message(store, **request)

    def request_command(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return request_command(store, **request)

    def authorize_command(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return authorize_command(store, **request)

    def reject_command(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return reject_command(store, **request)

    def consume_command(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return consume_authorization(store, **request)

    def prepare_run(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            prepared = prepare_run(store, **request)
            # The controller returns exact launch data; the lock is kept by the
            # actual launcher process, not by this short-lived CLI connection.
            result = {"run": prepared.run, "manifest": prepared.manifest, "manifestPath": str(prepared.manifest_path), "environment": prepared.environment}
            prepared.close()
            return result

    def attest_run(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return attest_run(store, **request)

    def stop_run(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return stop_run(store, **request)

    def submit_change(self, **request: Any) -> dict[str, Any]:
        with self._store(mutate=True) as store:
            return submit_change(store, **request).as_dict()

    def list_changes(self, project_id: str | None = None) -> list[dict[str, Any]]:
        with self._store() as store:
            return list_changes(store, project_id=project_id)

    def get_change(self, change_id: str) -> dict[str, Any]:
        with self._store() as store:
            return get_change(store, change_id)

    def detect_dependencies(self, **request: Any) -> list[dict[str, Any]]:
        with self._store(mutate=True) as store:
            return detect_dependencies(store, **request)

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
        value = dict(request or {})
        routes = {
            "project.register": self.register_project,
            "project.status": lambda **kw: self.status(kw["projectId"]),
            "project.work-index": lambda **kw: self.work_index(kw["projectId"]),
            "conversation.create": self.create_conversation,
            "conversation.focus": self.focus_conversation,
            "conversation.archive": self.archive_conversation,
            "message.post": self.post_message,
            "message.list": self.list_messages,
            "message.acknowledge": self.acknowledge_message,
            "message.reply": self.reply_message,
            "command.request": self.request_command,
            "command.authorize": self.authorize_command,
            "command.reject": self.reject_command,
            "run.prepare": self.prepare_run,
            "run.attest": self.attest_run,
            "run.stop": self.stop_run,
            "change.submit": self.submit_change,
            "dependency.detect": self.detect_dependencies,
            "dependency.disposition": self.set_dependency_disposition,
            "package-review.record": self.record_package_security_review,
        }
        if operation == "negotiate":
            return self.negotiate()
        if operation not in routes:
            raise GreenfieldClientError("unsupported greenfield controller operation")
        return routes[operation](**value)


__all__ = ["GreenfieldClientError", "GreenfieldControllerClient"]
