"""Controller-owned immutable local change submission.

The controller records lifecycle in SQLite while Git owns the immutable commit,
tree, and ``refs/pi/changes/<change>/<revision>`` content.  Dirty capture uses
an isolated temporary index and never mutates the caller's real index.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

from .errors import ConstraintError, ControlPlaneError, IdempotencyConflictError, InvalidRequestError, NotFoundError, ResourceStaleError
from .events import append_event_in_transaction
from .git_adapter import GitObservationError, GitRepositoryObservation, observe_repository
from .models import bounded_text, canonical_json, json_digest, new_id, parse_canonical_json, utc_now, validate_id

_CHANGE_ID = re.compile(r"^chg_[0-9a-f]{32}$")
_REF = re.compile(r"^refs/heads/[A-Za-z0-9._/-]{1,220}$")
_OID = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_PATHS = 4096
_MAX_PATH_BYTES = 4096
_MAX_OUTPUT = 512 * 1024


class ChangeSubmissionError(ControlPlaneError):
    """Base class for bounded change submission failures."""


class ChangeSelectionRequired(ChangeSubmissionError):
    """Dirty source state requires explicit path attribution/selection."""


class ChangeIntegrityError(ChangeSubmissionError):
    """A change ref, revision, or source-preservation assertion failed."""


def _change_id(value: Any) -> str:
    if not isinstance(value, str) or _CHANGE_ID.fullmatch(value) is None:
        raise InvalidRequestError("change ID is invalid")
    return value


def _ref(value: Any) -> str:
    if not isinstance(value, str) or _REF.fullmatch(value) is None or ".." in value or "//" in value:
        raise InvalidRequestError("target ref is invalid")
    return value


def _oid(value: Any, name: str) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise ChangeIntegrityError(f"{name} is not a Git object ID")
    return value


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_PATH_BYTES or "\x00" in value:
        raise InvalidRequestError("selected path is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or ".git" in pure.parts or "" in pure.parts:
        raise InvalidRequestError("selected path escapes the working copy")
    return pure.as_posix()


def _paths(values: Sequence[str] | None, *, name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)) or len(values) > _MAX_PATHS:
        raise InvalidRequestError(f"{name} must be a bounded path list")
    result = tuple(sorted({_path(item) for item in values}))
    return result


def _safe_title(value: Any, name: str) -> str:
    try:
        return bounded_text(value, name=name, limit=512)
    except ControlPlaneError:
        raise
    except Exception as error:
        raise InvalidRequestError(f"{name} is invalid") from error


def _git_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "true",
        "GIT_ASKPASS": "true",
    }
    for key, value in (extra or {}).items():
        if key in {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_OPTIONAL_LOCKS", "GIT_PAGER", "GIT_EDITOR", "GIT_ASKPASS"}:
            continue
        env[key] = str(value)
    return env


def _git(cwd: Path, args: Sequence[str], *, environment: Mapping[str, str] | None = None, input_text: str | None = None) -> str:
    if not args or any(not isinstance(item, str) or "\x00" in item for item in args):
        raise ChangeSubmissionError("Git command arguments are invalid")
    if any(not item for item in args) and not (args[0] == "update-ref" and len(args) == 4 and args[-1] == ""):
        raise ChangeSubmissionError("Git command arguments are invalid")
    allowed = {
        "rev-parse", "status", "symbolic-ref", "read-tree", "add", "write-tree", "commit-tree",
        "update-ref", "diff-tree",
    }
    if args[0] not in allowed:
        raise ChangeSubmissionError("Git command is not allowlisted for change submission")
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise ChangeSubmissionError("Git executable is unavailable")
    command = [
        executable,
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false",
        "-c", "core.sshCommand=",
        "-c", "credential.helper=",
        "-c", "core.attributesFile=/dev/null",
        "-c", "core.excludesFile=/dev/null",
        *args,
    ]
    try:
        result = subprocess.run(
            command, cwd=str(cwd), env=_git_environment(environment), input=input_text,
            stdin=subprocess.DEVNULL if input_text is None else None, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ChangeSubmissionError("Git change operation was unavailable or timed out") from error
    if len(result.stdout.encode()) > _MAX_OUTPUT or len(result.stderr.encode()) > _MAX_OUTPUT:
        raise ChangeSubmissionError("Git change operation output exceeded its bound")
    if result.returncode != 0:
        raise ChangeSubmissionError("Git change operation failed", detail={"command": args[0], "stderr": result.stderr.strip()[:512]})
    return result.stdout.strip()


def _optional_git(cwd: Path, args: Sequence[str], *, environment: Mapping[str, str] | None = None) -> str | None:
    try:
        value = _git(cwd, args, environment=environment)
    except ChangeSubmissionError as error:
        if "HEAD" in args and "not a valid object name" in error.message:
            return None
        raise
    return value or None


def _index_path(repository: Path) -> Path:
    raw = Path(_git(repository, ["rev-parse", "--git-path", "index"]))
    if not raw.is_absolute():
        raw = repository / raw
    path = raw.resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise ChangeIntegrityError("real Git index is not a regular file")
    return path


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _source_fingerprint(observation: GitRepositoryObservation, index_digest: str) -> dict[str, Any]:
    return {
        "headOid": observation.head_oid,
        "treeOid": observation.tree_oid,
        "branchRef": observation.branch_ref,
        "statusHash": observation.status_hash,
        "indexDigest": index_digest,
        "objectFormat": observation.object_format,
    }


def _assert_source_unchanged(repository: Path, before: GitRepositoryObservation, index_digest: str) -> dict[str, Any]:
    after = observe_repository(repository, include_worktrees=False)
    after_index = _file_digest(_index_path(repository))
    if _source_fingerprint(before, index_digest) != _source_fingerprint(after, after_index):
        raise ChangeIntegrityError("source working copy changed during submission")
    return _source_fingerprint(after, after_index)


def _changed_paths(repository: Path, tip_oid: str) -> tuple[str, ...]:
    output = _git(repository, ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", tip_oid])
    result = tuple(sorted(item for item in output.split("\x00") if item))
    for item in result:
        _path(item)
    return result


def _ref_name(change_id: str, revision: int) -> str:
    return f"refs/pi/changes/{change_id}/{revision}"


def _ref_oid(repository: Path, ref_name: str) -> str | None:
    try:
        return _git(repository, ["rev-parse", "--verify", ref_name]) or None
    except ChangeSubmissionError:
        return None


def _publish_ref(repository: Path, ref_name: str, tip_oid: str) -> None:
    existing = _ref_oid(repository, ref_name)
    if existing is not None:
        if existing != tip_oid:
            raise ChangeIntegrityError("immutable change ref is bound to another revision")
        return
    _git(repository, ["update-ref", ref_name, tip_oid, ""])
    if _ref_oid(repository, ref_name) != tip_oid:
        raise ChangeIntegrityError("change ref verification failed")


def _capture_dirty(
    repository: Path,
    *,
    baseline_oid: str,
    title: str,
    summary: str,
    selected_paths: tuple[str, ...],
    state_root: Path,
) -> tuple[str, str, tuple[str, ...]]:
    if not selected_paths:
        raise ChangeSelectionRequired("dirty submission requires explicit selected paths")
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_root, 0o700)
    descriptor, raw_index = tempfile.mkstemp(prefix=".change-index-", dir=str(state_root))
    os.close(descriptor)
    temporary_index = Path(raw_index)
    temporary_index.unlink()
    environment = {"GIT_INDEX_FILE": str(temporary_index)}
    try:
        _git(repository, ["read-tree", "--reset", baseline_oid], environment=environment)
        _git(repository, ["add", "-A", "--", *selected_paths], environment=environment)
        tree_oid = _oid(_git(repository, ["write-tree"], environment=environment), "change tree")
        message = f"{title}\n\n{summary}\n"
        commit_oid = _oid(
            _git(
                repository,
                ["commit-tree", tree_oid, "-p", baseline_oid],
                environment={
                    **environment,
                    "GIT_AUTHOR_NAME": "pi-control change",
                    "GIT_AUTHOR_EMAIL": "pi-control@example.invalid",
                    "GIT_COMMITTER_NAME": "pi-control change",
                    "GIT_COMMITTER_EMAIL": "pi-control@example.invalid",
                },
                input_text=message,
            ),
            "change commit",
        )
        return commit_oid, tree_oid, selected_paths
    finally:
        try:
            temporary_index.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class ChangeSubmission:
    change_id: str
    revision: int
    operation_id: str
    ref_name: str
    base_oid: str
    tip_oid: str
    tree_oid: str
    capture_mode: str
    changed_paths: tuple[str, ...]
    excluded_paths: tuple[str, ...]
    source_fingerprint: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "changeId": self.change_id,
            "revision": self.revision,
            "operationId": self.operation_id,
            "refName": self.ref_name,
            "baseOid": self.base_oid,
            "tipOid": self.tip_oid,
            "treeOid": self.tree_oid,
            "captureMode": self.capture_mode,
            "changedPaths": list(self.changed_paths),
            "excludedPaths": list(self.excluded_paths),
            "sourceFingerprint": dict(self.source_fingerprint),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChangeSubmission":
        return cls(
            change_id=_change_id(value["changeId"]), revision=value["revision"], operation_id=validate_id(value["operationId"], prefix="op"),
            ref_name=value["refName"], base_oid=_oid(value["baseOid"], "base"), tip_oid=_oid(value["tipOid"], "tip"),
            tree_oid=_oid(value["treeOid"], "tree"), capture_mode=value["captureMode"],
            changed_paths=tuple(value.get("changedPaths", ())), excluded_paths=tuple(value.get("excludedPaths", ())),
            source_fingerprint=dict(value.get("sourceFingerprint", {})),
        )


def _operation_request(
    *, project_id: str, working_copy_id: str, target_ref: str, title: str, summary: str,
    capture_mode: str, selected_paths: tuple[str, ...], excluded_paths: tuple[str, ...], expected_status_hash: str | None,
) -> dict[str, Any]:
    return {
        "projectId": project_id, "workingCopyId": working_copy_id, "targetRef": target_ref,
        "title": title, "summary": summary, "captureMode": capture_mode,
        "selectedPaths": list(selected_paths), "excludedPaths": list(excluded_paths),
        "expectedStatusHash": expected_status_hash,
    }


def _load_submission_from_operation(operation: Mapping[str, Any]) -> ChangeSubmission | None:
    raw = operation.get("result_json")
    if not raw:
        return None
    try:
        value = parse_canonical_json(str(raw))
    except ControlPlaneError:
        return None
    if not isinstance(value, Mapping):
        return None
    return ChangeSubmission.from_dict(value)


def _finish_submission(store: Any, *, operation_id: str, change_id: str, repository: Path, operation: Mapping[str, Any]) -> ChangeSubmission:
    change = store.conn.execute("SELECT * FROM changes WHERE change_id=?", (change_id,)).fetchone()
    if change is None:
        raise NotFoundError("change draft was not found", detail={"change_id": change_id})
    revision = store.conn.execute("SELECT * FROM change_revisions WHERE change_id=? ORDER BY revision DESC LIMIT 1", (change_id,)).fetchone()
    revision_number = int(revision["revision"]) if revision is not None else 1
    ref_name = _ref_name(change_id, revision_number)
    ref_oid = _ref_oid(repository, ref_name)
    if ref_oid is None:
        raise ChangeIntegrityError("change revision ref is absent after capture")
    tree_oid = _oid(_git(repository, ["rev-parse", f"{ref_name}^{{tree}}"]), "change tree")
    base_oid = _oid(change["baseline_oid"], "change baseline")
    if revision is None:
        request = parse_canonical_json(str(operation["request_json"]))
        selected = tuple(request.get("selectedPaths", ()))
        excluded = tuple(request.get("excludedPaths", ()))
        capture_mode = "branch-tip" if request.get("captureMode") == "clean" else "temporary-index"
        before = parse_canonical_json(str(change["baseline_state_json"]))
        changed = _changed_paths(repository, ref_oid)
        verification = {"refVerified": True, "sourceUnchanged": True, "objectFormat": before.get("objectFormat")}
        provenance = {"controller": "pi-control-change-v1", "sourceStatusHash": before.get("statusHash"), "indexDigest": before.get("indexDigest")}
        now = utc_now()
        store.conn.execute(
            "INSERT INTO change_revisions(change_id,revision,base_oid,tip_oid,tree_oid,source_head_oid,capture_mode,source_status_hash,changed_paths_json,diffstat_json,verification_json,provenance_json,ref_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (change_id, revision_number, base_oid, ref_oid, tree_oid, before.get("headOid"), capture_mode, before.get("statusHash"), canonical_json(list(changed)), canonical_json({"changedPaths": list(changed)}), canonical_json(verification), canonical_json(provenance), ref_name, now),
        )
        store.conn.execute(
            "UPDATE changes SET state='open',current_revision=?,submitted_at=?,updated_at=?,resource_version=resource_version+1 WHERE change_id=? AND state='draft' AND resource_version=?",
            (revision_number, now, now, change_id, int(change["resource_version"])),
        )
        if store.conn.execute("SELECT state,current_revision FROM changes WHERE change_id=?", (change_id,)).fetchone()["current_revision"] != revision_number:
            raise ResourceStaleError(change_id, int(change["resource_version"]), int(change["resource_version"]))
        result = ChangeSubmission(change_id, revision_number, operation_id, ref_name, base_oid, ref_oid, tree_oid, capture_mode, changed, excluded, before)
        result_json = canonical_json(result.as_dict())
        store.conn.execute("UPDATE operations SET state='succeeded',step='completed',result_json=?,updated_at=?,completed_at=? WHERE operation_id=?", (result_json, now, now, operation_id))
        append_event_in_transaction(store.conn, event_kind="change.submitted", resource_type="change", resource_id=change_id, resource_version=int(change["resource_version"]) + 1, operation_id=operation_id, payload={"changeId": change_id, "revision": revision_number, "tipOid": ref_oid, "treeOid": tree_oid, "refName": ref_name})
        return result
    if revision["tip_oid"] != ref_oid or revision["tree_oid"] != tree_oid:
        raise ChangeIntegrityError("stored change revision does not match immutable Git ref")
    baseline = parse_canonical_json(str(change["baseline_state_json"]))
    return ChangeSubmission(change_id, int(revision["revision"]), operation_id, ref_name, base_oid, ref_oid, tree_oid, revision["capture_mode"], tuple(parse_canonical_json(revision["changed_paths_json"])), tuple(baseline.get("excludedPaths", ())), parse_canonical_json(revision["verification_json"]))


def submit_change(
    store: Any,
    *,
    project_id: str,
    working_copy_id: str,
    target_ref: str,
    title: str,
    summary: str,
    capture_mode: str = "clean",
    selected_paths: Sequence[str] | None = None,
    excluded_paths: Sequence[str] | None = None,
    expected_status_hash: str | None = None,
    idempotency_key: str,
    created_by_conversation_id: str | None = None,
    actor_type: str = "conversation",
    actor_id: str | None = None,
    authorization_id: str | None = None,
    failpoint: Any | None = None,
) -> ChangeSubmission:
    """Submit one immutable branch-tip or explicitly selected dirty revision."""

    validate_id(project_id, prefix="prj")
    validate_id(working_copy_id, prefix="wc")
    _ref(target_ref)
    title = _safe_title(title, "title")
    summary = _safe_title(summary, "summary")
    if capture_mode not in {"clean", "dirty"}:
        raise InvalidRequestError("capture mode must be clean or dirty")
    selected = _paths(selected_paths, name="selected_paths")
    excluded = _paths(excluded_paths, name="excluded_paths")
    if set(selected) & set(excluded):
        raise ChangeSelectionRequired("a path cannot be both selected and excluded")
    if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 256 or "\x00" in idempotency_key:
        raise InvalidRequestError("idempotency key is invalid")
    if expected_status_hash is not None and _SHA256.fullmatch(expected_status_hash) is None:
        raise InvalidRequestError("expected status hash is invalid")
    conversation = None
    if created_by_conversation_id is not None:
        validate_id(created_by_conversation_id, prefix="conv")
    request = _operation_request(project_id=project_id, working_copy_id=working_copy_id, target_ref=target_ref, title=title, summary=summary, capture_mode=capture_mode, selected_paths=selected, excluded_paths=excluded, expected_status_hash=expected_status_hash)
    request_digest = json_digest(request)
    existing = store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if existing is not None:
        existing = dict(existing)
        same_binding = (
            existing["kind"] == "change.submit"
            and existing["resource_type"] == "change"
            and existing["actor_type"] == actor_type
            and existing["actor_id"] == actor_id
            and existing["authorization_id"] == authorization_id
        )
        if existing["request_digest"] != request_digest or not same_binding:
            raise IdempotencyConflictError(idempotency_key, existing_digest=existing["request_digest"], request_digest=request_digest)
        validate_id(existing["resource_id"], prefix="chg")
        prior = _load_submission_from_operation(existing)
        if prior is not None:
            return prior
        operation_id = existing["operation_id"]
        change_id = existing["resource_id"]
        operation = existing
    else:
        change_id = new_id("chg")
        store.create_operation(idempotency_key=idempotency_key, kind="change.submit", resource_type="change", resource_id=change_id, actor_type=actor_type, actor_id=actor_id, authorization_id=authorization_id, request=request, state="applying", step="intent-recorded")
        operation = dict(store.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone())
        operation_id = operation["operation_id"]
    project = store.conn.execute("SELECT project_id FROM projects WHERE project_id=?", (project_id,)).fetchone()
    working_copy = store.conn.execute("SELECT project_id,path FROM working_copies WHERE working_copy_id=?", (working_copy_id,)).fetchone()
    if project is None or working_copy is None or working_copy["project_id"] != project_id:
        raise NotFoundError("change source project or working copy was not found")
    repository = Path(working_copy["path"]).expanduser().resolve(strict=True)
    try:
        observation = observe_repository(repository, include_worktrees=False)
    except GitObservationError as error:
        raise ChangeSubmissionError("change source could not be observed") from error
    if observation.head_oid is None or observation.tree_oid is None:
        raise ConstraintError("change submission requires a repository with a committed HEAD")
    index_digest = _file_digest(_index_path(repository))
    existing_change = store.conn.execute("SELECT * FROM changes WHERE change_id=?", (change_id,)).fetchone()
    if existing_change is not None:
        prior_baseline = parse_canonical_json(str(existing_change["baseline_state_json"]))
        current_fingerprint = _source_fingerprint(observation, index_digest)
        for key in ("headOid", "treeOid", "branchRef", "statusHash", "indexDigest"):
            if prior_baseline.get(key) != current_fingerprint.get(key):
                raise ChangeIntegrityError("change source moved or changed during retry")
    if expected_status_hash is not None and expected_status_hash != observation.status_hash:
        raise ResourceStaleError(working_copy_id, 1, 2)
    if capture_mode == "clean" and observation.dirty:
        raise ChangeSelectionRequired("dirty source requires explicit dirty capture")
    if capture_mode == "dirty" and not observation.dirty:
        raise ConstraintError("dirty capture requires dirty source state")
    if capture_mode == "dirty" and not selected:
        raise ChangeSelectionRequired("dirty submission requires explicit selected paths")
    if set(selected) & set(excluded):
        raise ChangeSelectionRequired("selected paths overlap excluded paths")
    baseline_state = _source_fingerprint(observation, index_digest)
    baseline_state.update({"excludedPaths": list(excluded), "selectedPaths": list(selected), "captureMode": capture_mode})
    with store.transaction():
        existing_change = store.conn.execute("SELECT * FROM changes WHERE change_id=?", (change_id,)).fetchone()
        if existing_change is None:
            store.conn.execute(
                "INSERT INTO changes(change_id,project_id,source_working_copy_id,created_by_conversation_id,title,summary,target_ref,baseline_oid,baseline_tree_oid,baseline_state_json,state,current_revision,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (change_id, project_id, working_copy_id, created_by_conversation_id, title, summary, target_ref, observation.head_oid, observation.tree_oid, canonical_json(baseline_state), "draft", 0, 1, utc_now(), utc_now()),
            )
        elif existing_change["state"] != "draft":
            raise ConstraintError("change identity is already terminal")
    if failpoint is not None:
        failpoint("after-draft")
    ref_name = _ref_name(change_id, 1)
    existing_ref = _ref_oid(repository, ref_name)
    if existing_ref is None:
        if capture_mode == "clean":
            tip_oid, tree_oid = observation.head_oid, observation.tree_oid
        else:
            tip_oid, tree_oid, _ = _capture_dirty(repository, baseline_oid=observation.head_oid, title=title, summary=summary, selected_paths=selected, state_root=store.state_root)
        _publish_ref(repository, ref_name, tip_oid)
    else:
        tip_oid = existing_ref
        tree_oid = _oid(_git(repository, ["rev-parse", f"{ref_name}^{{tree}}"]), "change tree")
    if failpoint is not None:
        failpoint("after-ref")
    source_fingerprint = _assert_source_unchanged(repository, observation, index_digest)
    if failpoint is not None:
        failpoint("after-source-check")
    return _finish_submission(store, operation_id=operation_id, change_id=change_id, repository=repository, operation=operation)


def list_changes(store: Any, *, project_id: str | None = None) -> list[dict[str, Any]]:
    if project_id is not None:
        validate_id(project_id, prefix="prj")
        rows = store.conn.execute("SELECT * FROM changes WHERE project_id=? ORDER BY created_at", (project_id,))
    else:
        rows = store.conn.execute("SELECT * FROM changes ORDER BY created_at")
    return [dict(row) for row in rows]


def get_change(store: Any, change_id: str, *, revision: int | None = None) -> dict[str, Any]:
    _change_id(change_id)
    row = store.conn.execute("SELECT * FROM changes WHERE change_id=?", (change_id,)).fetchone()
    if row is None:
        raise NotFoundError("change was not found", detail={"change_id": change_id})
    result = dict(row)
    if revision is None:
        revision = int(row["current_revision"])
    if revision:
        item = store.conn.execute("SELECT * FROM change_revisions WHERE change_id=? AND revision=?", (change_id, revision)).fetchone()
        if item is None:
            raise NotFoundError("change revision was not found", detail={"change_id": change_id, "revision": revision})
        result["revision"] = dict(item)
    return result


__all__ = [
    "ChangeIntegrityError", "ChangeSelectionRequired", "ChangeSubmission",
    "ChangeSubmissionError", "get_change", "list_changes", "submit_change",
]
