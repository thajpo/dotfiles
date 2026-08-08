"""Read-only Pi session observation and controller conversation binding."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping

from .models import bounded_text, new_id, validate_pi_session_id, utc_now

_MAX_SESSION_BYTES = 64 * 1024 * 1024
_MAX_HEADER_BYTES = 64 * 1024


class SessionObservationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionObservation:
    path: str
    pi_session_id: str
    cwd: str | None
    header: Mapping[str, Any]
    size_bytes: int
    sha256: str
    observed_at: str
    state: str = "ready"

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "pi_session_id": self.pi_session_id, "cwd": self.cwd,
            "size_bytes": self.size_bytes, "sha256": self.sha256,
            "observed_at": self.observed_at, "state": self.state,
            "provenance": "pi-session-observation-v1",
        }


def _safe_session_path(path: os.PathLike[str] | str) -> Path:
    raw = Path(path).expanduser()
    try:
        info = raw.lstat()
    except FileNotFoundError as error:
        raise SessionObservationError("session file is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SessionObservationError("session file must be a regular non-symlink file")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise SessionObservationError("session file is not user-owned")
    if info.st_size > _MAX_SESSION_BYTES:
        raise SessionObservationError("session file exceeds observation bound")
    return raw.resolve(strict=True)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def observe_session(path: os.PathLike[str] | str) -> SessionObservation:
    resolved = _safe_session_path(path)
    try:
        with resolved.open("rb") as handle:
            first = handle.readline(_MAX_HEADER_BYTES + 1)
        if len(first) > _MAX_HEADER_BYTES:
            raise SessionObservationError("session header exceeds bound")
        header = json.loads(first.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SessionObservationError("session header is invalid") from error
    if not isinstance(header, dict):
        raise SessionObservationError("session header must be an object")
    session_id = header.get("id") or header.get("sessionId") or header.get("session_id")
    try:
        session_id = validate_pi_session_id(session_id)
    except (TypeError, ValueError) as error:
        raise SessionObservationError("session header has no valid Pi session ID") from error
    cwd = header.get("cwd")
    if cwd is not None:
        cwd = bounded_text(cwd, name="session cwd", limit=4096)
    info = resolved.stat()
    return SessionObservation(str(resolved), session_id, cwd, header, int(info.st_size), _hash(resolved), utc_now())


def _validate_conversation_binding(store: Any, *, role: str, project_id: str | None, working_copy_id: str | None) -> None:
    if role not in {"secretary", "personal", "workstream", "review", "integration", "host"}:
        raise SessionObservationError("conversation role is invalid")
    if role == "host":
        if project_id is not None or working_copy_id is not None:
            raise SessionObservationError("host conversations cannot bind project or working copy")
        return
    if role in {"secretary", "personal", "workstream", "review", "integration"} and project_id is None:
        raise SessionObservationError("conversation role requires a project")
    if role == "secretary" and working_copy_id is not None:
        raise SessionObservationError("secretary conversations cannot own a working copy")
    if role in {"workstream", "review", "integration"} and working_copy_id is None:
        raise SessionObservationError("conversation role requires a working copy")
    if project_id is not None:
        project = store.conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if project is None:
            raise SessionObservationError("conversation project does not exist")
    if working_copy_id is not None:
        working = store.conn.execute("SELECT project_id,kind,effective_mode,branch_ref FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()
        if working is None:
            raise SessionObservationError("conversation working copy does not exist")
        if project_id != working["project_id"]:
            raise SessionObservationError("conversation working copy belongs to another project")
        if role == "review" and not (working["kind"] == "review" and working["effective_mode"] == "read-only" and working["branch_ref"] is None):
            raise SessionObservationError("review conversation requires a read-only detached review working copy")


def register_conversation(
    store: Any,
    session_file: os.PathLike[str] | str,
    *,
    role: str,
    display_name: str,
    project_id: str | None = None,
    working_copy_id: str | None = None,
    desired_state: str = "active",
) -> dict[str, Any]:
    """Bind an exact session file; cwd is observation only, never authority."""

    observation = observe_session(session_file)
    conversation_id = new_id("conv")
    name = bounded_text(display_name, name="display_name", limit=512)
    now = utc_now()
    with store.transaction():
        _validate_conversation_binding(store, role=role, project_id=project_id, working_copy_id=working_copy_id)
        store.conn.execute(
            "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at,last_reconciled_at,error_code,error_detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (conversation_id, project_id, working_copy_id, role, name, observation.pi_session_id, observation.path, desired_state, observation.state, 1, now, now, observation.observed_at, None, None),
        )
    row = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    return {key: row[key] for key in row.keys()}


def bind_controller_session(
    store: Any,
    *,
    conversation_id: str,
    session_file: os.PathLike[str] | str,
    project_id: str,
    working_copy_id: str | None = None,
    activation_resource_version: int | None = None,
) -> dict[str, Any]:
    """Project one exact JSONL session onto an existing controller conversation."""
    observation = observe_session(session_file)
    row = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
    if row is None:
        raise SessionObservationError("controller conversation does not exist")
    if row["project_id"] != project_id or row["working_copy_id"] != working_copy_id:
        raise SessionObservationError("controller conversation project/working-copy binding differs")
    if row["pi_session_id"] != observation.pi_session_id or Path(row["session_file"]).resolve() != Path(observation.path).resolve():
        raise SessionObservationError("controller conversation session identity differs")
    duplicate = store.conn.execute("SELECT conversation_id FROM conversations WHERE (pi_session_id=? OR session_file=?) AND conversation_id<>?", (observation.pi_session_id, observation.path, conversation_id)).fetchone()
    if duplicate is not None:
        raise SessionObservationError("duplicate session history requires an explicit mapping decision")
    activation = store.conn.execute("SELECT * FROM project_activations WHERE project_id=?", (project_id,)).fetchone()
    if activation is None or activation["mode"] != "controller":
        raise SessionObservationError("controller session binding requires controller activation")
    if activation_resource_version is not None and int(activation["resource_version"]) != activation_resource_version:
        raise SessionObservationError("activation resource version is stale")
    return {
        "schemaVersion": 1,
        "conversationId": conversation_id,
        "projectId": project_id,
        "workingCopyId": working_copy_id,
        "sessionFile": observation.path,
        "piSessionId": observation.pi_session_id,
        "sessionDigest": observation.sha256,
        "headerCwd": observation.cwd,
        "activationResourceVersion": int(activation["resource_version"]),
        "provenance": "controller-session-projection-v1",
    }


def session_cwd_disagrees(observation: SessionObservation, expected_path: os.PathLike[str] | str) -> bool:
    if observation.cwd is None:
        return False
    try:
        return Path(observation.cwd).expanduser().resolve(strict=False) != Path(expected_path).expanduser().resolve(strict=False)
    except OSError:
        return True


__all__ = ["SessionObservation", "SessionObservationError", "bind_controller_session", "observe_session", "register_conversation", "session_cwd_disagrees"]
