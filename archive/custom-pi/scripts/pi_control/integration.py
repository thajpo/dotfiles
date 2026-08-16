"""Exact-revision integration analysis, authorization, and CAS mutation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping

from .errors import ConstraintError, ControlPlaneError, IdempotencyConflictError, InvalidRequestError, NotFoundError, ResourceStaleError
from .events import append_event_in_transaction
from .locks import secure_directory_fd, secure_lock_directory
from .models import bounded_text, canonical_json, json_digest, new_id, parse_canonical_json, utc_now, validate_id
from .reconcile import inspect_project
from .reviews import list_reviews
from .git_adapter import GitObservationError, observe_repository

_INTEGRATION_ID = re.compile(r"^int_[0-9a-f]{32}$")
_REF = re.compile(r"^refs/heads/[A-Za-z0-9._/-]{1,220}$")
_OID = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_MAX_OUTPUT = 512 * 1024


class IntegrationError(ControlPlaneError):
    """Base integration failure."""


class AnalysisStaleError(IntegrationError):
    pass


class IntegrationNeedsResolution(IntegrationError):
    pass


class AuthorizationError(IntegrationError):
    pass


def _oid(value: Any, name: str) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise IntegrationError(f"{name} is not a Git object ID")
    return value


def _ref(value: Any) -> str:
    if not isinstance(value, str) or _REF.fullmatch(value) is None or ".." in value or "//" in value:
        raise InvalidRequestError("integration target ref is invalid")
    return value


def _git_result(cwd: Path, args: list[str], *, input_text: str | None = None, pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[str]:
    allowed = {"rev-parse", "merge-base", "merge-tree", "diff-tree", "update-ref", "worktree", "merge", "commit", "status"}
    if not args or args[0] not in allowed or any(not isinstance(item, str) or "\x00" in item for item in args):
        raise IntegrationError("Git integration command is not allowlisted")
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise IntegrationError("Git executable is unavailable")
    command = [executable, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.sshCommand=", "-c", "credential.helper=", "-c", "core.attributesFile=/dev/null", "-c", "core.excludesFile=/dev/null", *args]
    env = {
        "PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "GIT_EDITOR": "true", "GIT_ASKPASS": "true",
        "GIT_AUTHOR_NAME": "pi-control integration", "GIT_AUTHOR_EMAIL": "pi-control@example.invalid",
        "GIT_COMMITTER_NAME": "pi-control integration", "GIT_COMMITTER_EMAIL": "pi-control@example.invalid",
    }
    try:
        result = subprocess.run(command, cwd=str(cwd), env=env, input=input_text, stdin=subprocess.DEVNULL if input_text is None else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=90, check=False, shell=False, pass_fds=pass_fds)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise IntegrationError("Git integration operation was unavailable or timed out") from error
    if len(result.stdout.encode()) > _MAX_OUTPUT or len(result.stderr.encode()) > _MAX_OUTPUT:
        raise IntegrationError("Git integration output exceeded its bound")
    return result


def _git(cwd: Path, args: list[str], *, input_text: str | None = None, pass_fds: tuple[int, ...] = ()) -> str:
    result = _git_result(cwd, args, input_text=input_text, pass_fds=pass_fds)
    if result.returncode != 0:
        raise IntegrationError("Git integration operation failed", detail={"command": args[0], "stderr": result.stderr.strip()[:512]})
    return result.stdout.strip()


def _resolve_ref(cwd: Path, ref: str, *, pass_fds: tuple[int, ...] = ()) -> str:
    return _oid(_git(cwd, ["rev-parse", "--verify", ref], pass_fds=pass_fds), "Git ref")


def _ancestor(cwd: Path, older: str, newer: str) -> bool:
    return _git_result(cwd, ["merge-base", "--is-ancestor", older, newer]).returncode == 0


def _merge_base(cwd: Path, left: str, right: str) -> str | None:
    result = _git_result(cwd, ["merge-base", left, right])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _conflict_paths(cwd: Path, target: str, candidate: str) -> tuple[str, ...]:
    result = _git_result(cwd, ["merge-tree", target, candidate])
    if result.returncode not in {0, 1}:
        raise IntegrationError("merge analysis failed", detail={"stderr": result.stderr.strip()[:512]})
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if "CONFLICT" not in line.upper():
            continue
        match = re.search(r"(?:in|file) ['\"]?([^'\"]+)['\"]?", line, re.IGNORECASE)
        if match:
            paths.add(match.group(1).strip())
    return tuple(sorted(paths))


def _project_target(store: Any, project_id: str, working_copy_id: str) -> tuple[Mapping[str, Any], Mapping[str, Any], Path]:
    validate_id(project_id, prefix="prj")
    validate_id(working_copy_id, prefix="wc")
    project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    target = store.conn.execute("SELECT * FROM working_copies WHERE working_copy_id=? AND project_id=?", (working_copy_id, project_id)).fetchone()
    if project is None or target is None:
        raise NotFoundError("integration project or target working copy was not found")
    path = Path(target["path"]).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise IntegrationNeedsResolution("integration target working copy is unavailable")
    return project, target, path


def _candidate(store: Any, project_id: str, change_id: str, revision: int, repository: Path) -> tuple[Mapping[str, Any], Mapping[str, Any], str, str]:
    validate_id(change_id, prefix="chg")
    row = store.conn.execute("SELECT c.*,r.* FROM changes c JOIN change_revisions r ON r.change_id=c.change_id AND r.revision=? WHERE c.change_id=?", (revision, change_id)).fetchone()
    if row is None or row["project_id"] != project_id:
        raise NotFoundError("change revision was not found")
    ref = str(row["ref_name"])
    tip = _resolve_ref(repository, ref)
    if tip != row["tip_oid"]:
        raise AnalysisStaleError("candidate change ref no longer matches its immutable revision")
    tree = _oid(_git(repository, ["rev-parse", f"{ref}^{{tree}}"]), "candidate tree")
    if tree != row["tree_oid"]:
        raise AnalysisStaleError("candidate change tree no longer matches its immutable revision")
    return row, {"changeId": change_id, "revision": revision, "tipOid": tip, "treeOid": tree, "baseOid": row["base_oid"], "refName": ref}, tip, tree


@contextmanager
def _exclusive_locks(state_root: Path, project_id: str, working_copy_id: str):
    lock_root = state_root / "locks"
    directory_fd = secure_lock_directory(lock_root, create=True)
    if directory_fd is None:
        raise IntegrationError("integration lock directory is unavailable")
    handles = []
    try:
        # Contract order: project, then target working copy, then target ref.
        for name in (f"project-{project_id}.lock", f"working-copy-{working_copy_id}.lock", f"integration-ref-{project_id}.lock"):
            fd = os.open(name, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            handles.append(fd)
        yield
    finally:
        for fd in reversed(handles):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        os.close(directory_fd)


@dataclass(frozen=True)
class IntegrationAnalysis:
    integration_id: str
    project_id: str
    change_id: str
    revision: int
    target_working_copy_id: str
    target_ref: str
    target_oid: str
    candidate_tip_oid: str
    candidate_tree_oid: str
    merge_base_oid: str | None
    strategy: str
    relation: str
    conflict_paths: tuple[str, ...]
    target_dirty: bool
    target_checked_out: bool
    target_in_use: bool
    target_status_hash: str
    analysis_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1, "integrationId": self.integration_id, "projectId": self.project_id,
            "changeId": self.change_id, "revision": self.revision, "targetWorkingCopyId": self.target_working_copy_id,
            "targetRef": self.target_ref, "targetOid": self.target_oid, "candidateTipOid": self.candidate_tip_oid,
            "candidateTreeOid": self.candidate_tree_oid, "mergeBaseOid": self.merge_base_oid, "strategy": self.strategy,
            "relation": self.relation, "conflictPaths": list(self.conflict_paths), "targetDirty": self.target_dirty,
            "targetCheckedOut": self.target_checked_out, "targetInUse": self.target_in_use,
            "targetStatusHash": self.target_status_hash, "analysisDigest": self.analysis_digest,
        }


def _analysis_from_row(row: Mapping[str, Any]) -> IntegrationAnalysis:
    body = parse_canonical_json(str(row["analysis_json"]))
    return IntegrationAnalysis(
        str(row["integration_id"]), str(row["project_id"]), str(row["change_id"]), int(row["revision"]),
        str(body["targetWorkingCopyId"]), str(body["targetRef"]), str(body["targetOid"]), str(body["candidateTipOid"]),
        str(body["candidateTreeOid"]), body.get("mergeBaseOid"), str(body["strategy"]), str(body["relation"]),
        tuple(str(item) for item in body.get("conflictPaths", [])), bool(body["targetDirty"]), bool(body["targetCheckedOut"]),
        bool(body["targetInUse"]), str(body["targetStatusHash"]), str(body["analysisDigest"]),
    )


def analyze_integration(
    store: Any,
    *,
    project_id: str,
    change_id: str,
    revision: int,
    target_working_copy_id: str,
    target_ref: str,
    integration_id: str | None = None,
) -> IntegrationAnalysis:
    _ref(target_ref)
    integration_id = integration_id or new_id("int")
    validate_id(integration_id, prefix="int")
    existing = store.conn.execute("SELECT * FROM integration_attempts WHERE integration_id=?", (integration_id,)).fetchone()
    if existing is not None:
        body = parse_canonical_json(str(existing["analysis_json"]))
        same_request = (
            existing["project_id"] == project_id and existing["change_id"] == change_id and int(existing["revision"]) == revision
            and body.get("targetWorkingCopyId") == target_working_copy_id and body.get("targetRef") == target_ref
        )
        if not same_request:
            raise IdempotencyConflictError(integration_id, existing_digest=str(body.get("analysisDigest", "")), request_digest=json_digest({"projectId": project_id, "changeId": change_id, "revision": revision, "targetWorkingCopyId": target_working_copy_id, "targetRef": target_ref}))
        return _analysis_from_row(existing)
    _, target, repository = _project_target(store, project_id, target_working_copy_id)
    try:
        observation = observe_repository(repository, include_worktrees=False)
    except GitObservationError as error:
        raise IntegrationNeedsResolution("integration target could not be observed") from error
    target_oid = _resolve_ref(repository, target_ref)
    candidate_row, candidate, candidate_tip, candidate_tree = _candidate(store, project_id, change_id, revision, repository)
    merge_base = _merge_base(repository, target_oid, candidate_tip)
    if target_oid == candidate_tip or _ancestor(repository, candidate_tip, target_oid):
        relation, strategy = "already-contained", "fast-forward"
    elif _ancestor(repository, target_oid, candidate_tip):
        relation, strategy = "fast-forward", "fast-forward"
    else:
        relation, strategy = "diverged", "integration-worktree"
    conflicts = () if relation == "already-contained" else _conflict_paths(repository, target_oid, candidate_tip)
    active = store.conn.execute("SELECT 1 FROM runs WHERE working_copy_id=? AND observed_state NOT IN ('stopped','failed','lost','needs_attention') LIMIT 1", (target_working_copy_id,)).fetchone() is not None
    checked_out = target["branch_ref"] == target_ref
    body = {
        "schemaVersion": 1, "integrationId": integration_id, "projectId": project_id, "changeId": change_id,
        "revision": revision, "targetWorkingCopyId": target_working_copy_id, "targetRef": target_ref,
        "targetOid": target_oid, "candidateTipOid": candidate_tip, "candidateTreeOid": candidate_tree,
        "mergeBaseOid": merge_base, "strategy": strategy, "relation": relation,
        "conflictPaths": list(conflicts), "targetDirty": observation.dirty, "targetCheckedOut": checked_out,
        "targetInUse": active, "targetStatusHash": observation.status_hash,
    }
    digest = "sha256:" + hashlib.sha256(canonical_json(body).encode()).hexdigest()
    body["analysisDigest"] = digest
    now = utc_now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO integration_attempts(integration_id,project_id,change_id,revision,requested_target_oid,strategy,state,result_oid,rollback_ref,operation_id,analysis_json,verification_json,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (integration_id, project_id, change_id, revision, target_oid, strategy, "planned", None, None, None, canonical_json(body), canonical_json({"candidateRef": candidate["refName"], "candidateTreeVerified": True}), 1, now, now),
        )
        append_event_in_transaction(store.conn, event_kind="integration.analyzed", resource_type="integration", resource_id=integration_id, resource_version=1, payload={"integrationId": integration_id, "changeId": change_id, "revision": revision, "targetOid": target_oid, "analysisDigest": digest})
    return IntegrationAnalysis(integration_id, project_id, change_id, revision, target_working_copy_id, target_ref, target_oid, candidate_tip, candidate_tree, merge_base, strategy, relation, conflicts, observation.dirty, checked_out, active, observation.status_hash, digest)


def authorize_integration(
    store: Any,
    *,
    integration_id: str,
    actor_id: str,
    request_context_id: str,
    expires_at: str,
    review_id: str | None = None,
) -> dict[str, Any]:
    validate_id(integration_id, prefix="int")
    if not isinstance(actor_id, str) or not actor_id or len(actor_id) > 256 or not isinstance(request_context_id, str) or not request_context_id or len(request_context_id) > 256:
        raise InvalidRequestError("integration authorization identity is invalid")
    if not isinstance(expires_at, str) or not expires_at.endswith("Z"):
        raise InvalidRequestError("authorization expiry must be a UTC RFC3339 timestamp")
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidRequestError("authorization expiry is invalid") from error
    if expiry.tzinfo is None or expiry.utcoffset() != timezone.utc.utcoffset(expiry):
        raise InvalidRequestError("authorization expiry must be UTC")
    if expiry <= datetime.now(timezone.utc):
        raise AuthorizationError("integration authorization is already expired")
    with store.transaction():
        row = store.conn.execute("SELECT * FROM integration_attempts WHERE integration_id=?", (integration_id,)).fetchone()
        if row is None:
            raise NotFoundError("integration attempt was not found")
        change_row = store.conn.execute("SELECT state,current_revision FROM changes WHERE change_id=?", (row["change_id"],)).fetchone()
        if change_row is None or change_row["state"] != "open" or int(change_row["current_revision"]) != int(row["revision"]):
            raise AuthorizationError("integration change is not open at the analyzed revision")
        from .dependencies import package_review_gate
        package_gate = package_review_gate(store, change_id=row["change_id"], revision=int(row["revision"]))
        if not package_gate["ready"]:
            raise AuthorizationError("exact package security review is incomplete for this revision")
        analysis = parse_canonical_json(str(row["analysis_json"]))
        reviews = list_reviews(store, change_id=row["change_id"], revision=int(row["revision"]))
        analysis_bound = [
            item for item in reviews
            if item.state == "submitted"
            and item.reviewer_run_id is not None
            and item.reviewer_actor_id is not None
            and item.reviewer_source.get("changeId") == row["change_id"]
            and int(item.reviewer_source.get("revision", -1)) == int(row["revision"])
            and item.evidence.get("integrationId") == integration_id
            and item.evidence.get("analysisDigest") == analysis.get("analysisDigest")
            and item.evidence.get("targetOid") == row["requested_target_oid"]
        ]
        blocking = [item for item in analysis_bound if item.verdict != "accept"]
        if blocking:
            raise AuthorizationError("exact revision requires every submitted review to accept this analysis and target")
        accepted = [item for item in analysis_bound if item.verdict == "accept"]
        if review_id is not None:
            validate_id(review_id, prefix="review")
            accepted = [item for item in accepted if item.review_id == review_id]
        if not accepted:
            raise AuthorizationError("exact revision requires an accepted review bound to this analysis and target")
        scope = {"integrationId": integration_id, "projectId": row["project_id"], "changeId": row["change_id"], "revision": int(row["revision"]), "targetOid": row["requested_target_oid"], "strategy": row["strategy"], "analysisDigest": analysis["analysisDigest"], "reviewId": accepted[-1].review_id, "actorId": actor_id, "requestContextId": request_context_id, "expiresAt": expires_at}
        now = utc_now()
        digest = json_digest(scope)
        existing = store.conn.execute("SELECT * FROM authorizations WHERE request_context_id=? AND kind='integrate-change' AND resource_type='integration' AND resource_id=? ORDER BY issued_at LIMIT 1", (request_context_id, integration_id)).fetchone()
        if existing is not None:
            if existing["scope_digest"] != digest or existing["actor_id"] != actor_id or existing["expires_at"] != expires_at:
                raise IdempotencyConflictError(request_context_id, existing_digest=str(existing["scope_digest"]), request_digest=digest)
            return {"authorizationId": existing["authorization_id"], "scope": parse_canonical_json(str(existing["scope_json"])), "scopeDigest": existing["scope_digest"], "state": existing["state"]}
        authorization_id = new_id("auth")
        store.conn.execute(
            "INSERT INTO authorizations(authorization_id,kind,actor_type,actor_id,project_id,resource_type,resource_id,request_context_id,scope_json,scope_digest,issued_at,expires_at,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (authorization_id, "integrate-change", "user", actor_id, row["project_id"], "integration", integration_id, request_context_id, canonical_json(scope), digest, now, expires_at, "active"),
        )
        append_event_in_transaction(store.conn, event_kind="integration.authorized", resource_type="integration", resource_id=integration_id, payload={"integrationId": integration_id, "authorizationId": authorization_id, "scopeDigest": digest})
        return {"authorizationId": authorization_id, "scope": scope, "scopeDigest": digest, "state": "active"}


@dataclass(frozen=True)
class IntegrationResult:
    integration_id: str
    state: str
    result_oid: str | None
    rollback_ref: str | None
    result_change_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"integrationId": self.integration_id, "state": self.state, "resultOid": self.result_oid, "rollbackRef": self.rollback_ref, "resultChangeId": self.result_change_id}


def _mark_resolution(store: Any, row: Mapping[str, Any], *, code: str, detail: str) -> None:
    now = utc_now()
    with store.transaction():
        cursor = store.conn.execute("UPDATE integration_attempts SET state='needs_resolution',updated_at=?,completed_at=?,error_code=?,error_detail=?,resource_version=resource_version+1 WHERE integration_id=? AND resource_version=?", (now, now, code, detail[:1024], row["integration_id"], row["resource_version"]))
        if cursor.rowcount != 1:
            current = store.conn.execute("SELECT resource_version FROM integration_attempts WHERE integration_id=?", (row["integration_id"],)).fetchone()
            raise ResourceStaleError(str(row["integration_id"]), int(row["resource_version"]), int(current[0]) if current is not None else None)
        append_event_in_transaction(store.conn, event_kind="integration.needs_resolution", resource_type="integration", resource_id=row["integration_id"], resource_version=int(row["resource_version"]) + 1, payload={"integrationId": row["integration_id"], "reason": code})


def _publish_ref(repository: Path, ref: str, oid: str, expected: str = "") -> None:
    current = _git_result(repository, ["rev-parse", "--verify", ref])
    if current.returncode == 0:
        if current.stdout.strip() != oid:
            raise IntegrationError("immutable rollback or result ref is bound to another object")
        return
    _git(repository, ["update-ref", ref, oid, expected])
    if _resolve_ref(repository, ref) != oid:
        raise IntegrationError("integration ref verification failed")


def _consume_and_merge(store: Any, row: Mapping[str, Any], auth: Mapping[str, Any], *, result_oid: str, rollback_ref: str | None, verification: Mapping[str, Any], result_change_id: str | None = None) -> IntegrationResult:
    now = utc_now()
    try:
        with store.transaction():
            current = store.conn.execute("SELECT * FROM integration_attempts WHERE integration_id=?", (row["integration_id"],)).fetchone()
            if current is None or current["resource_version"] != row["resource_version"]:
                raise ResourceStaleError(row["integration_id"], int(row["resource_version"]), int(current["resource_version"]) if current is not None else None)
            current_auth = store.conn.execute("SELECT * FROM authorizations WHERE authorization_id=?", (auth["authorization_id"],)).fetchone()
            if (
                current_auth is None
                or current_auth["kind"] != "integrate-change"
                or current_auth["resource_id"] != row["integration_id"]
                or current_auth["state"] != "active"
                or current_auth["scope_digest"] != auth["scope_digest"]
                or current_auth["scope_json"] != auth["scope_json"]
                or current_auth["actor_id"] != auth["actor_id"]
                or current_auth["request_context_id"] != auth["request_context_id"]
                or current_auth["expires_at"] != auth["expires_at"]
            ):
                raise AuthorizationError("integration authorization changed before success was recorded")
            try:
                if datetime.fromisoformat(str(current_auth["expires_at"]).replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                    raise AuthorizationError("integration authorization expired before success was recorded")
            except (TypeError, ValueError) as error:
                raise AuthorizationError("integration authorization expiry is invalid") from error
            auth_cursor = store.conn.execute("UPDATE authorizations SET state='consumed',consumed_at=? WHERE authorization_id=? AND state='active'", (now, auth["authorization_id"]))
            if auth_cursor.rowcount != 1:
                raise AuthorizationError("integration authorization changed before it could be consumed")
            cursor = store.conn.execute("UPDATE integration_attempts SET state='succeeded',result_oid=?,rollback_ref=?,verification_json=?,updated_at=?,completed_at=?,resource_version=resource_version+1 WHERE integration_id=? AND resource_version=?", (result_oid, rollback_ref, canonical_json(verification), now, now, row["integration_id"], row["resource_version"]))
            if cursor.rowcount != 1:
                current = store.conn.execute("SELECT resource_version FROM integration_attempts WHERE integration_id=?", (row["integration_id"],)).fetchone()
                raise ResourceStaleError(row["integration_id"], int(row["resource_version"]), int(current[0]) if current is not None else None)
            if result_change_id is None:
                change_cursor = store.conn.execute("UPDATE changes SET state='merged',merged_at=?,updated_at=?,resource_version=resource_version+1 WHERE change_id=? AND state='open'", (now, now, row["change_id"]))
                if change_cursor.rowcount != 1:
                    raise IntegrationNeedsResolution("source change state changed before integration success was recorded")
            append_event_in_transaction(store.conn, event_kind="integration.succeeded", resource_type="integration", resource_id=row["integration_id"], resource_version=int(row["resource_version"]) + 1, payload={"integrationId": row["integration_id"], "changeId": row["change_id"], "revision": row["revision"], "resultOid": result_oid, "rollbackRef": rollback_ref, "resultChangeId": result_change_id})
    except AuthorizationError:
        _mark_resolution(store, row, code="CP_PERMISSION_INVALID", detail="authorization changed after integration side-effect observation")
        raise
    except IntegrationNeedsResolution:
        _mark_resolution(store, row, code="CP_OPERATION_AMBIGUOUS", detail="change state changed after integration side-effect observation")
        raise
    return IntegrationResult(row["integration_id"], "succeeded", result_oid, rollback_ref, result_change_id)


def _result_change_id(integration_id: str) -> str:
    return "chg_" + hashlib.sha256(("integration-result:" + integration_id).encode("utf-8")).hexdigest()[:32]


def _result_change(store: Any, row: Mapping[str, Any], repository: Path, target_oid: str, result_oid: str, result_tree: str) -> str:
    result_id = _result_change_id(str(row["integration_id"]))
    ref = f"refs/pi/changes/{result_id}/1"
    _publish_ref(repository, ref, result_oid)
    now = utc_now()
    analysis = parse_canonical_json(str(row["analysis_json"]))
    baseline = {"headOid": target_oid, "treeOid": _git(repository, ["rev-parse", f"{target_oid}^{{tree}}"]), "captureMode": "integration-result", "sourceStatusHash": analysis.get("targetStatusHash")}
    with store.transaction():
        existing = store.conn.execute("SELECT * FROM changes WHERE change_id=?", (result_id,)).fetchone()
        if existing is None:
            store.conn.execute("INSERT INTO changes(change_id,project_id,source_working_copy_id,title,summary,target_ref,baseline_oid,baseline_tree_oid,baseline_state_json,state,current_revision,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (result_id, row["project_id"], None, "Integration result", "Controller-created integration result awaiting fresh authorization", analysis["targetRef"], target_oid, baseline["treeOid"], canonical_json(baseline), "draft", 0, 1, now, now))
            store.conn.execute("INSERT INTO change_revisions(change_id,revision,base_oid,tip_oid,tree_oid,source_head_oid,capture_mode,source_status_hash,changed_paths_json,diffstat_json,verification_json,provenance_json,ref_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (result_id, 1, target_oid, result_oid, result_tree, target_oid, "integration-result", None, "[]", canonical_json({}), canonical_json({"refVerified": True}), canonical_json({"inputs": [{"changeId": row["change_id"], "revision": row["revision"]}]}), ref, now))
            store.conn.execute("UPDATE changes SET state='open',current_revision=1,submitted_at=?,updated_at=?,resource_version=resource_version+1 WHERE change_id=?", (now, now, result_id))
            store.conn.execute("INSERT INTO change_revision_inputs(result_change_id,result_revision,input_change_id,input_revision,relation) VALUES(?,?,?,?,?)", (result_id, 1, row["change_id"], row["revision"], "includes"))
        else:
            revision = store.conn.execute("SELECT tip_oid,tree_oid,ref_name FROM change_revisions WHERE change_id=? AND revision=1", (result_id,)).fetchone()
            if revision is None or revision["tip_oid"] != result_oid or revision["tree_oid"] != result_tree or revision["ref_name"] != ref:
                raise IntegrationNeedsResolution("deterministic integration result disagrees with recorded change")
    return result_id


def integrate(
    store: Any,
    *,
    integration_id: str,
    authorization_id: str,
    expected_resource_version: int | None = None,
    failpoint: Any | None = None,
) -> IntegrationResult:
    validate_id(integration_id, prefix="int")
    validate_id(authorization_id, prefix="auth")
    row = store.conn.execute("SELECT * FROM integration_attempts WHERE integration_id=?", (integration_id,)).fetchone()
    if row is None:
        raise NotFoundError("integration attempt was not found")
    if row["state"] == "succeeded":
        raise AuthorizationError("integration authorization was already consumed")
    change_row = store.conn.execute("SELECT state,current_revision FROM changes WHERE change_id=?", (row["change_id"],)).fetchone()
    if change_row is None or change_row["state"] != "open" or int(change_row["current_revision"]) != int(row["revision"]):
        raise AuthorizationError("integration change is not open at the analyzed revision")
    if expected_resource_version is not None and int(row["resource_version"]) != expected_resource_version:
        raise ResourceStaleError(integration_id, expected_resource_version, int(row["resource_version"]))
    auth = store.conn.execute("SELECT * FROM authorizations WHERE authorization_id=?", (authorization_id,)).fetchone()
    if auth is None or auth["kind"] != "integrate-change" or auth["state"] != "active" or auth["resource_id"] != integration_id:
        raise AuthorizationError("integration authorization is missing, consumed, or not bound")
    try:
        if datetime.fromisoformat(str(auth["expires_at"]).replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            raise AuthorizationError("integration authorization has expired")
    except (TypeError, ValueError) as error:
        raise AuthorizationError("integration authorization expiry is invalid") from error
    scope = parse_canonical_json(str(auth["scope_json"]))
    analysis = parse_canonical_json(str(row["analysis_json"]))
    expected_scope = {
        "integrationId": integration_id,
        "projectId": row["project_id"],
        "changeId": row["change_id"],
        "revision": int(row["revision"]),
        "targetOid": row["requested_target_oid"],
        "strategy": row["strategy"],
        "analysisDigest": analysis.get("analysisDigest"),
        "actorId": auth["actor_id"],
        "requestContextId": auth["request_context_id"],
        "expiresAt": auth["expires_at"],
    }
    if json_digest(scope) != auth["scope_digest"] or any(scope.get(key) != value for key, value in expected_scope.items()):
        raise AnalysisStaleError("authorization is not bound to the current analysis, actor, request, and expiry")
    _, target, repository = _project_target(store, row["project_id"], _target_wc_from_analysis(analysis))
    current_target = _resolve_ref(repository, analysis["targetRef"])
    revision_ref = store.conn.execute("SELECT ref_name FROM change_revisions WHERE change_id=? AND revision=?", (row["change_id"], row["revision"])).fetchone()
    candidate_now = _resolve_ref(repository, str(revision_ref[0])) if revision_ref is not None else None
    if current_target != row["requested_target_oid"]:
        rollback_ref = f"refs/pi/rollback/{integration_id}"
        rollback_now = _git_result(repository, ["rev-parse", "--verify", rollback_ref])
        if candidate_now == current_target and rollback_now.returncode == 0 and rollback_now.stdout.strip() == row["requested_target_oid"]:
            return _consume_and_merge(store, row, auth, result_oid=current_target, rollback_ref=rollback_ref, verification={"targetRef": analysis["targetRef"], "targetOid": current_target, "rollbackRef": rollback_ref, "recovered": True})
        _mark_resolution(store, row, code="CP_GIT_REF_MOVED", detail="target ref moved after analysis")
        raise AnalysisStaleError("target ref moved after analysis")
    try:
        observation = observe_repository(repository, include_worktrees=False)
    except GitObservationError as error:
        raise IntegrationNeedsResolution("target observation failed") from error
    active = store.conn.execute("SELECT 1 FROM runs WHERE working_copy_id=? AND observed_state NOT IN ('stopped','failed','lost','needs_attention') LIMIT 1", (_target_wc_from_analysis(analysis),)).fetchone() is not None
    if observation.dirty or active or target["branch_ref"] == analysis["targetRef"]:
        _mark_resolution(store, row, code="CP_TARGET_IN_USE", detail="target is dirty, checked out, or actively owned")
        raise IntegrationNeedsResolution("target is dirty, checked out, or in use")
    with _exclusive_locks(store.state_root, row["project_id"], _target_wc_from_analysis(analysis)):
        # Re-observe after acquiring the complete lock set.
        moved = _resolve_ref(repository, analysis["targetRef"])
        if moved != row["requested_target_oid"]:
            _mark_resolution(store, row, code="CP_GIT_REF_MOVED", detail="target ref moved under integration lock")
            raise AnalysisStaleError("target ref moved under integration lock")
        revision_ref_row = store.conn.execute("SELECT ref_name FROM change_revisions WHERE change_id=? AND revision=?", (row["change_id"], row["revision"])).fetchone()
        if revision_ref_row is None:
            raise NotFoundError("candidate revision disappeared")
        candidate = _resolve_ref(repository, str(revision_ref_row[0]))
        if candidate != analysis["candidateTipOid"]:
            _mark_resolution(store, row, code="CP_GIT_REF_MOVED", detail="candidate ref moved after analysis")
            raise AnalysisStaleError("candidate ref moved after analysis")
        if analysis["relation"] == "already-contained":
            return _consume_and_merge(store, row, auth, result_oid=moved, rollback_ref=None, verification={"targetOid": moved, "alreadyContained": True})
        if analysis["strategy"] == "fast-forward":
            rollback_ref = f"refs/pi/rollback/{integration_id}"
            _publish_ref(repository, rollback_ref, moved)
            if failpoint is not None:
                failpoint("rollback-ref")
            result = _git_result(repository, ["update-ref", analysis["targetRef"], candidate, moved])
            if result.returncode != 0:
                _mark_resolution(store, row, code="CP_GIT_REF_MOVED", detail="target CAS failed")
                raise AnalysisStaleError("target CAS failed because target moved")
            if failpoint is not None:
                failpoint("target-ref")
            observed = _resolve_ref(repository, analysis["targetRef"])
            if observed != candidate:
                _mark_resolution(store, row, code="CP_INTEGRATION_CONFLICT", detail="target proof did not match candidate")
                raise IntegrationNeedsResolution("target proof failed")
            return _consume_and_merge(store, row, auth, result_oid=candidate, rollback_ref=rollback_ref, verification={"targetRef": analysis["targetRef"], "targetOid": candidate, "rollbackRef": rollback_ref})
        return _integrate_worktree(store, row, auth, repository, analysis, failpoint=failpoint)


def _target_wc_from_analysis(analysis: Mapping[str, Any]) -> str:
    value = analysis.get("targetWorkingCopyId")
    validate_id(value, prefix="wc")
    return value


def _integrate_worktree(store: Any, row: Mapping[str, Any], auth: Mapping[str, Any], repository: Path, analysis: Mapping[str, Any], *, failpoint: Any | None) -> IntegrationResult:
    parent = store.state_root / "integration-worktrees"
    directory_fd = secure_directory_fd(parent, create=True)
    if directory_fd is None:  # pragma: no cover - create=True returns or raises
        raise IntegrationError("integration worktree directory is unavailable")
    path = Path(f"/proc/self/fd/{directory_fd}") / str(row["integration_id"])
    held_fds = (directory_fd,)
    try:
        result_id = _result_change_id(str(row["integration_id"]))
        result_ref = f"refs/pi/changes/{result_id}/1"
        result_ref_result = _git_result(repository, ["rev-parse", "--verify", result_ref])
        if result_ref_result.returncode == 0:
            result_oid = result_ref_result.stdout.strip()
            recorded = store.conn.execute("SELECT r.tip_oid,r.tree_oid FROM changes c JOIN change_revisions r ON r.change_id=c.change_id AND r.revision=1 WHERE c.change_id=?", (result_id,)).fetchone()
            result_tree = _git(repository, ["rev-parse", f"{result_oid}^{{tree}}"])
            if recorded is not None and (recorded["tip_oid"] != result_oid or recorded["tree_oid"] != result_tree):
                raise IntegrationNeedsResolution("deterministic integration result disagrees with recorded change")
            if path.exists():
                try:
                    head = _resolve_ref(path, "HEAD", pass_fds=held_fds)
                except IntegrationError:
                    head = None
                if head != result_oid:
                    raise IntegrationNeedsResolution("integration worktree from an earlier attempt requires attention")
            # The ref may have committed before the SQLite result rows. Rebuild
            # the deterministic result record from that verified immutable commit.
            durable_result_id = _result_change(store, row, repository, analysis["targetOid"], result_oid, result_tree)
            if path.exists():
                _git(repository, ["worktree", "remove", str(path)], pass_fds=held_fds)
            return _consume_and_merge(store, row, auth, result_oid=result_oid, rollback_ref=None, verification={"resultChangeId": durable_result_id, "candidatePreserved": True, "recovered": True}, result_change_id=durable_result_id)
        if path.exists():
            raise IntegrationNeedsResolution("integration worktree from an earlier attempt requires attention")
        _git(repository, ["worktree", "add", "--detach", str(path), analysis["targetRef"]], pass_fds=held_fds)
        candidate = _oid(analysis["candidateTipOid"], "candidate")
        try:
            merged = _git_result(path, ["merge", "--no-commit", "--no-ff", candidate], pass_fds=held_fds)
            if merged.returncode != 0:
                _mark_resolution(store, row, code="CP_INTEGRATION_CONFLICT", detail="integration worktree has unresolved conflicts")
                raise IntegrationNeedsResolution("integration worktree has unresolved conflicts")
            _git(path, ["commit", "--no-edit"], pass_fds=held_fds)
            result_oid = _resolve_ref(path, "HEAD", pass_fds=held_fds)
            result_tree = _oid(_git(path, ["rev-parse", "HEAD^{tree}"], pass_fds=held_fds), "integration result tree")
            if failpoint is not None:
                failpoint("integration-worktree")
            result_change_id = _result_change(store, row, repository, analysis["targetOid"], result_oid, result_tree)
            _git(repository, ["worktree", "remove", str(path)], pass_fds=held_fds)
            return _consume_and_merge(store, row, auth, result_oid=result_oid, rollback_ref=None, verification={"resultChangeId": result_change_id, "candidatePreserved": True}, result_change_id=result_change_id)
        except BaseException:
            if path.exists() and not (path / ".git").exists():
                shutil.rmtree(path, ignore_errors=True)
            raise
    finally:
        os.close(directory_fd)


__all__ = ["AnalysisStaleError", "AuthorizationError", "IntegrationAnalysis", "IntegrationError", "IntegrationNeedsResolution", "IntegrationResult", "analyze_integration", "authorize_integration", "integrate"]
