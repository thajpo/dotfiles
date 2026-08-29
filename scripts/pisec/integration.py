"""Single-acceptance workstream integration and secretary-owned closeout."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .cleanup import cleanup_workstream
from .events import append_event_in_transaction
from .models import ConflictError, IdempotencyConflictError, InvalidRequestError, NeedsAttentionError, NotFoundError, ScopeMismatchError, bounded_text, canonical_json, json_digest, new_id, utc_now, validate_id, validate_sha256
from .projects import get_project
from .secretary_git import _oid, _primary_state, _repository, _run_git
from .runtime_eligibility import unresolved_reporter_issue
from .worker_repo import project_git_lock, validate_worker_repository
from .workstreams import complete_workstream, inspect_workstream, retire_workstream
from .control_plane import control_plane_mutation


_HASH_FIELDS = frozenset({"completionPacketSha256", "taskPacketSha256", "candidatePatchSha256", "scopeSha256"})
_ACCEPTANCE_SCOPE_FIELDS = frozenset({
    "kind",
    "projectId",
    "workstreamId",
    "targetBranch",
    "completionPacketSha256",
    "taskPacketSha256",
    "candidatePatchSha256",
    "changedPaths",
    "acceptance",
    "verification",
    "conflictPolicy",
    "effects",
    "nonEffects",
})


def _project_workstream(store: Any, project_id: str, workstream_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    validate_id(project_id, prefix="prj")
    validate_id(workstream_id, prefix="ws")
    project = get_project(store, project_id)
    row = store.conn.execute("SELECT * FROM workstreams WHERE project_id=? AND workstream_id=?", (project_id, workstream_id)).fetchone()
    if row is None:
        raise NotFoundError("workstream was not found in the project")
    workstream = dict(row)
    if workstream["kind"] != "worker":
        raise ConflictError("only worker workstreams can be accepted")
    if workstream["desired_state"] == "retired":
        raise ConflictError("retired workstream cannot be accepted")
    return project, workstream, dict(row)


def _packet(store: Any, workstream_id: str, packet_sha256: str | None = None) -> dict[str, Any]:
    if packet_sha256 is None:
        row = store.conn.execute(
            "SELECT * FROM completion_packets WHERE workstream_id=? ORDER BY submitted_at DESC,completion_packet_id DESC LIMIT 1",
            (workstream_id,),
        ).fetchone()
    else:
        row = store.conn.execute(
            "SELECT * FROM completion_packets WHERE workstream_id=? AND packet_sha256=?",
            (workstream_id, packet_sha256),
        ).fetchone()
    if row is None:
        raise ConflictError("workstream has no completion packet ready for acceptance")
    try:
        value = json.loads(str(row["packet_json"]))
    except (TypeError, json.JSONDecodeError) as error:
        raise NeedsAttentionError("completion packet is invalid") from error
    if not isinstance(value, dict):
        raise NeedsAttentionError("completion packet is invalid")
    return {**dict(row), "packet": value}


def _task_packet(store: Any, workstream_id: str, packet: Mapping[str, Any]) -> str:
    task = store.conn.execute("SELECT packet_sha256 FROM task_packets WHERE workstream_id=?", (workstream_id,)).fetchone()
    if task is None or packet.get("taskPacketSha256") != task["packet_sha256"]:
        raise ConflictError("completion packet does not match the immutable task packet")
    return str(task["packet_sha256"])


def _changed_paths(repository: Path, base_oid: str, source_oid: str) -> list[str]:
    _code, names = _run_git(
        repository,
        "diff",
        "--name-only",
        "--no-ext-diff",
        "--no-renames",
        "-z",
        base_oid,
        source_oid,
        max_bytes=128 * 1024,
    )
    paths = [item for item in names.split("\x00") if item]
    if any(not isinstance(path, str) or not path or len(path) > 4096 or any(ord(char) < 0x20 for char in path) for path in paths):
        raise NeedsAttentionError("candidate changed-path report is invalid")
    return sorted(set(paths))


def _patch_digest(repository: Path, base_oid: str, source_oid: str) -> str:
    _code, patch = _run_git(
        repository,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-color",
        base_oid,
        source_oid,
        max_bytes=16 * 1024 * 1024,
    )
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()


def _acceptance_identity(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or not value:
        raise NeedsAttentionError("completion packet acceptance is invalid")
    identity: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("criterion"), str) or item.get("status") != "passed":
            raise NeedsAttentionError("completion packet acceptance is invalid")
        identity.append((str(item["criterion"]), str(item["status"])))
    return tuple(identity)


def _candidate(
    store: Any,
    project_id: str,
    workstream_id: str,
    *,
    packet_sha256: str | None = None,
    expected_paths: list[str] | None = None,
    expected_acceptance: Any | None = None,
) -> dict[str, Any]:
    project, workstream, _ = _project_workstream(store, project_id, workstream_id)
    packet = _packet(store, workstream_id, packet_sha256)
    packet_value = packet["packet"]
    task_sha256 = _task_packet(store, workstream_id, packet_value)
    repository = Path(str(workstream["worktree_path"])).absolute()
    integration_row = store.conn.execute(
        "SELECT integration_id,state FROM integration_jobs WHERE workstream_id=? ORDER BY created_at DESC LIMIT 1",
        (workstream_id,),
    ).fetchone()
    private_ref = None
    if integration_row is not None and integration_row["state"] in {"awaiting_worker", "queued", "refreshing", "verifying", "applying"}:
        private_ref = f"refs/pisec/target/{integration_row['integration_id']}"
    primary = _repository(project)
    source_oid = _oid(repository, f"refs/heads/{workstream['branch_name']}")
    if source_oid != str(packet["source_commit_oid"]).lower():
        raise ConflictError("completion packet source commit is stale")
    target_project, _repository_path, target_branch, target_oid, porcelain = _primary_state(store, project_id)
    if target_project["project_id"] != project_id:
        raise ConflictError("registered project identity changed")
    if porcelain:
        raise ConflictError("registered project checkout is dirty")
    _code, symbolic_target = _run_git(primary, "rev-parse", "--symbolic-full-name", "--verify", "--end-of-options", str(workstream["target_ref"]))
    if symbolic_target.strip() != f"refs/heads/{target_branch}":
        raise ConflictError("registered checkout is not on the workstream target branch")
    history_base_oid = None
    if private_ref is not None:
        private_code, _ = _run_git(repository, "show-ref", "--verify", "--quiet", private_ref, accepted=frozenset({0, 1}))
        if private_code == 0:
            history_base_oid = _oid(repository, private_ref)
    validate_worker_repository(
        repository,
        branch_name=str(workstream["branch_name"]),
        base_oid=str(workstream["base_commit_oid"]),
        target_branch=str(workstream["target_ref"]).removeprefix("refs/heads/"),
        allowed_private_ref=private_ref,
        history_base_oid=history_base_oid,
        review_base_oid=history_base_oid,
    )
    if not isinstance(packet_value.get("acceptance"), list) or not isinstance(packet_value.get("verification"), list):
        raise NeedsAttentionError("completion packet acceptance or verification is invalid")
    if expected_acceptance is not None and _acceptance_identity(packet_value["acceptance"]) != _acceptance_identity(expected_acceptance):
        raise ScopeMismatchError("replacement completion packet changed the accepted criteria")
    changed_paths = _changed_paths(repository, str(workstream["base_commit_oid"]), source_oid)
    patch_sha256 = _patch_digest(repository, str(workstream["base_commit_oid"]), source_oid)
    scope_changed_paths: list[str] | None = None
    if expected_paths is not None:
        target_revision = target_oid
        if history_base_oid is not None:
            target_revision = private_ref
        ancestor_code, _output = _run_git(
            repository,
            "merge-base",
            "--is-ancestor",
            target_revision,
            source_oid,
            accepted=frozenset({0, 1, 128}),
        )
        if ancestor_code == 0:
            scope_changed_paths = _changed_paths(repository, target_revision, source_oid)
    if scope_changed_paths is not None and not set(scope_changed_paths).issubset(expected_paths):
        raise ScopeMismatchError("replacement completion packet changed paths outside the accepted scope")
    return {
        "project": project,
        "workstream": workstream,
        "repository": repository,
        "packet": packet,
        "packetValue": packet_value,
        "sourceOid": source_oid,
        "targetOid": target_oid,
        "targetBranch": target_branch,
        "taskSha256": task_sha256,
        "changedPaths": changed_paths,
        "patchSha256": patch_sha256,
    }


def _approval_scope(candidate: Mapping[str, Any]) -> dict[str, Any]:
    packet = candidate["packetValue"]
    return {
        "kind": "workstream.accept",
        "projectId": candidate["project"]["project_id"],
        "workstreamId": candidate["workstream"]["workstream_id"],
        "targetBranch": candidate["targetBranch"],
        "completionPacketSha256": candidate["packet"]["packet_sha256"],
        "taskPacketSha256": candidate["taskSha256"],
        "candidatePatchSha256": candidate["patchSha256"],
        "changedPaths": candidate["changedPaths"],
        "acceptance": packet["acceptance"],
        "verification": packet["verification"],
        "conflictPolicy": "bounded-worker-reconciliation",
        "effects": [
            f"accept completion packet {candidate['packet']['packet_sha256']}",
            f"allow the secretary to refresh {candidate['targetBranch']} and fast-forward it",
            "allow the original worker to resolve target conflicts within the accepted paths",
            "run secretary-owned closeout after integration",
        ],
        "nonEffects": [
            "no push",
            "no unrelated path changes",
            "no second merge approval",
            "no user decision for ordinary target drift or bounded conflicts",
        ],
    }


def _validate_scope(scope_value: Mapping[str, Any]) -> dict[str, Any]:
    scope = dict(scope_value)
    if set(scope) != _ACCEPTANCE_SCOPE_FIELDS:
        raise InvalidRequestError("acceptance scope fields do not match the contract")
    if scope.get("kind") != "workstream.accept":
        raise InvalidRequestError("acceptance scope kind is invalid")
    validate_id(scope.get("projectId"), prefix="prj")
    validate_id(scope.get("workstreamId"), prefix="ws")
    bounded_text(scope.get("targetBranch"), name="targetBranch", limit=512)
    for field in _HASH_FIELDS - {"scopeSha256"}:
        validate_sha256(scope.get(field), f"acceptance scope field {field}")
    paths = scope.get("changedPaths")
    if not isinstance(paths, list) or len(paths) > 4096 or any(not isinstance(path, str) or not path or len(path) > 4096 or any(ord(char) < 0x20 for char in path) for path in paths):
        raise InvalidRequestError("acceptance changed paths are invalid")
    if paths != sorted(set(paths)):
        raise InvalidRequestError("acceptance changed paths are not canonical")
    if scope.get("conflictPolicy") != "bounded-worker-reconciliation":
        raise InvalidRequestError("acceptance conflict policy is invalid")
    for field in ("acceptance", "verification", "effects", "nonEffects"):
        values = scope.get(field)
        if not isinstance(values, list) or not values or any(not isinstance(item, (str, dict)) for item in values):
            raise InvalidRequestError(f"acceptance scope field {field} is invalid")
    canonical_json(scope, max_bytes=65536, max_text=8192)
    return scope


def prepare_workstream_acceptance(store: Any, project_id: str, workstream_id: str) -> dict[str, Any]:
    with project_git_lock(store.state_root, project_id):
        candidate = _candidate(store, project_id, workstream_id)
    existing = store.conn.execute("SELECT a.completion_packet_id,cp.packet_sha256 FROM workstream_acceptances a JOIN completion_packets cp ON cp.completion_packet_id=a.completion_packet_id WHERE a.workstream_id=?", (workstream_id,)).fetchone()
    if existing is not None and existing["packet_sha256"] != candidate["packet"]["packet_sha256"]:
        raise ConflictError("workstream already has an acceptance; use its existing integration")
    scope = _approval_scope(candidate)
    return {
        "approvalScope": scope,
        "candidateSourceCommitOid": candidate["sourceOid"],
        "targetCommitOid": candidate["targetOid"],
        "workstreamId": workstream_id,
    }


def _acceptance_row(store: Any, workstream_id: str, packet_sha256: str) -> dict[str, Any] | None:
    row = store.conn.execute(
        "SELECT a.* FROM workstream_acceptances a JOIN completion_packets cp ON cp.completion_packet_id=a.completion_packet_id WHERE a.workstream_id=? AND cp.packet_sha256=?",
        (workstream_id, packet_sha256),
    ).fetchone()
    return None if row is None else dict(row)


def _integration_row(store: Any, acceptance_id: str) -> dict[str, Any] | None:
    row = store.conn.execute("SELECT * FROM integration_jobs WHERE acceptance_id=?", (acceptance_id,)).fetchone()
    return None if row is None else dict(row)


@control_plane_mutation
def apply_workstream_acceptance(store: Any, project_id: str, scope_value: Mapping[str, Any]) -> dict[str, Any]:
    scope = _validate_scope(scope_value)
    if scope["projectId"] != project_id:
        raise ScopeMismatchError("acceptance scope belongs to another project")
    scope_sha256 = json_digest(scope)
    existing = _acceptance_row(store, scope["workstreamId"], scope["completionPacketSha256"])
    if existing is not None:
        if existing["scope_sha256"] != scope_sha256:
            raise IdempotencyConflictError("completion packet was already accepted with another scope")
        job = _integration_row(store, existing["acceptance_id"])
        if job is None:
            raise NeedsAttentionError("accepted completion packet has no integration job")
        return {"acceptance": existing, "integration": job, "reused": True}
    existing = store.conn.execute("SELECT a.completion_packet_id,cp.packet_sha256 FROM workstream_acceptances a JOIN completion_packets cp ON cp.completion_packet_id=a.completion_packet_id WHERE a.workstream_id=?", (scope["workstreamId"],)).fetchone()
    if existing is not None and existing["packet_sha256"] != scope["completionPacketSha256"]:
        raise ConflictError("workstream already has an acceptance; use its existing integration")
    with project_git_lock(store.state_root, project_id):
        candidate = _candidate(
            store,
            project_id,
            scope["workstreamId"],
            packet_sha256=scope["completionPacketSha256"],
            expected_paths=scope["changedPaths"],
            expected_acceptance=scope["acceptance"],
        )
    expected = _approval_scope(candidate)
    if canonical_json(expected) != canonical_json(scope):
        raise ScopeMismatchError("acceptance scope no longer matches the candidate")
    now = utc_now()
    acceptance_id = new_id("acc")
    integration_id = new_id("int")
    scope_json = canonical_json(scope, max_bytes=65536, max_text=8192)
    with store.transaction():
        store.conn.execute(
            "INSERT INTO workstream_acceptances(acceptance_id,project_id,workstream_id,completion_packet_id,source_commit_oid,target_branch,candidate_patch_sha256,changed_paths_json,scope_json,scope_sha256,accepted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (acceptance_id, project_id, scope["workstreamId"], candidate["packet"]["completion_packet_id"], candidate["sourceOid"], scope["targetBranch"], scope["candidatePatchSha256"], canonical_json(scope["changedPaths"]), scope_json, scope_sha256, now),
        )
        store.conn.execute(
            "INSERT INTO integration_jobs(integration_id,acceptance_id,project_id,workstream_id,state,target_branch,candidate_completion_packet_id,candidate_source_oid,attempt,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (integration_id, acceptance_id, project_id, scope["workstreamId"], "queued", scope["targetBranch"], candidate["packet"]["completion_packet_id"], candidate["sourceOid"], 0, now, now),
        )
        store.conn.execute("UPDATE completion_packets SET accepted_at=? WHERE completion_packet_id=? AND accepted_at IS NULL", (now, candidate["packet"]["completion_packet_id"]))
        append_event_in_transaction(
            store.conn,
            kind="workstream.accepted",
            project_id=project_id,
            workstream_id=scope["workstreamId"],
            payload={"acceptanceId": acceptance_id, "completionPacketSha256": scope["completionPacketSha256"], "scopeSha256": scope_sha256},
        )
        append_event_in_transaction(
            store.conn,
            kind="integration.queued",
            project_id=project_id,
            workstream_id=scope["workstreamId"],
            payload={"integrationId": integration_id, "acceptanceId": acceptance_id, "targetBranch": scope["targetBranch"]},
        )
    acceptance = _acceptance_row(store, scope["workstreamId"], scope["completionPacketSha256"])
    integration = _integration_row(store, acceptance_id)
    if acceptance is None or integration is None:
        raise NeedsAttentionError("accepted completion packet could not be reloaded")
    return {"acceptance": acceptance, "integration": integration, "reused": False}


def inspect_integration(store: Any, project_id: str, integration_id: str) -> dict[str, Any]:
    row = store.conn.execute("SELECT * FROM integration_jobs WHERE integration_id=? AND project_id=?", (integration_id, project_id)).fetchone()
    if row is None:
        raise NotFoundError("integration job was not found")
    result = dict(row)
    acceptance = store.conn.execute("SELECT * FROM workstream_acceptances WHERE acceptance_id=?", (row["acceptance_id"],)).fetchone()
    result["acceptance"] = None if acceptance is None else dict(acceptance)
    result["reports"] = [dict(report) for report in store.conn.execute("SELECT * FROM integration_reports WHERE integration_id=? ORDER BY submitted_at,integration_report_id", (integration_id,))]
    return result


def list_integrations(store: Any, project_id: str, *, states: set[str] | None = None) -> list[dict[str, Any]]:
    get_project(store, project_id)
    params: list[Any] = [project_id]
    where = ["project_id=?"]
    if states is not None:
        if not states or not states.issubset({"queued", "refreshing", "awaiting_worker", "verifying", "applying", "integrated", "needs_attention"}):
            raise InvalidRequestError("integration states are invalid")
        where.append("state IN (" + ",".join("?" for _ in states) + ")")
        params.extend(sorted(states))
    return [dict(row) for row in store.conn.execute(f"SELECT * FROM integration_jobs WHERE {' AND '.join(where)} ORDER BY created_at,integration_id", params)]


def _set_job(store: Any, integration_id: str, *, state: str, next_action: str | None = None, error: str | None = None, candidate_packet: str | None = None, candidate_source: str | None = None, target_oid: str | None = None, integration_source: str | None = None) -> None:
    current = store.conn.execute("SELECT project_id,workstream_id,state FROM integration_jobs WHERE integration_id=?", (integration_id,)).fetchone()
    if current is None:
        raise NotFoundError("integration job was not found")
    now = utc_now()
    assignments = ["state=?", "next_action=?", "last_error=?", "updated_at=?"]
    values: list[Any] = [state, next_action, error, now]
    if state == "integrated":
        assignments.append("integrated_at=COALESCE(integrated_at,?)")
        values.append(now)
    if candidate_packet is not None:
        assignments.append("candidate_completion_packet_id=?")
        values.append(candidate_packet)
    if candidate_source is not None:
        assignments.append("candidate_source_oid=?")
        values.append(candidate_source)
    if target_oid is not None:
        assignments.append("target_oid=?")
        values.append(target_oid)
    if integration_source is not None:
        assignments.append("integration_source_oid=?")
        values.append(integration_source)
    values.append(integration_id)
    with store.transaction():
        store.conn.execute(f"UPDATE integration_jobs SET {','.join(assignments)} WHERE integration_id=?", values)
        if current["state"] != state and state in {"awaiting_worker", "needs_attention"}:
            append_event_in_transaction(store.conn, kind=f"integration.{state}", project_id=current["project_id"], workstream_id=current["workstream_id"], payload={"integrationId": integration_id, "state": state, "nextAction": next_action})


def _latest_replacement(store: Any, candidate: Mapping[str, Any], accepted_paths: list[str], accepted_criteria: Any) -> dict[str, Any] | None:
    workstream_id = str(candidate["workstream"]["workstream_id"])
    source_oid = _oid(candidate["repository"], f"refs/heads/{candidate['workstream']['branch_name']}")
    rows = store.conn.execute(
        "SELECT packet_sha256 FROM completion_packets WHERE workstream_id=? AND task_packet_sha256=? ORDER BY submitted_at DESC,completion_packet_id DESC",
        (workstream_id, candidate["taskSha256"]),
    )
    for row in rows:
        if row["packet_sha256"] == candidate["packet"]["packet_sha256"]:
            continue
        try:
            replacement = _candidate(store, str(candidate["project"]["project_id"]), workstream_id, packet_sha256=row["packet_sha256"], expected_paths=accepted_paths, expected_acceptance=accepted_criteria)
        except (ConflictError, ScopeMismatchError):
            continue
        if replacement["sourceOid"] == source_oid:
            return replacement
    return None


def _current_candidate(store: Any, project_id: str, workstream_id: str, task_sha256: str, accepted_paths: list[str], accepted_criteria: Any) -> dict[str, Any] | None:
    rows = store.conn.execute(
        "SELECT packet_sha256 FROM completion_packets WHERE workstream_id=? AND task_packet_sha256=? ORDER BY submitted_at DESC,completion_packet_id DESC",
        (workstream_id, task_sha256),
    )
    for row in rows:
        try:
            candidate = _candidate(store, project_id, workstream_id, packet_sha256=row["packet_sha256"], expected_paths=accepted_paths, expected_acceptance=accepted_criteria)
        except (ConflictError, ScopeMismatchError, NeedsAttentionError):
            continue
        return candidate
    return None


def _report_and_receipt(store: Any, job: Mapping[str, Any], candidate: Mapping[str, Any], *, previous_target_oid: str, final_source_oid: str, changed_paths: list[str], verification: Mapping[str, Any]) -> None:
    integration_id = str(job["integration_id"])
    report_payload = {
        "integrationId": integration_id,
        "sourceCommitOid": final_source_oid,
        "candidateCompletionPacketSha256": candidate["packet"]["packet_sha256"],
        "verification": verification,
        "changedSurfaces": changed_paths,
        "residualRisk": candidate["packetValue"].get("residualRisk", ""),
    }
    report_sha256 = json_digest(report_payload)
    report_json = canonical_json(verification, max_bytes=65536, max_text=8192)
    changed_json = canonical_json(changed_paths, max_bytes=32768, max_text=4096)
    with store.transaction():
        existing = store.conn.execute("SELECT integration_report_id FROM integration_reports WHERE integration_id=? AND source_commit_oid=?", (integration_id, final_source_oid)).fetchone()
        if existing is None:
            store.conn.execute(
                "INSERT INTO integration_reports(integration_report_id,integration_id,workstream_id,source_commit_oid,verification_json,changed_surfaces_json,residual_risk,report_sha256,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id("int"), integration_id, job["workstream_id"], final_source_oid, report_json, changed_json, str(candidate["packetValue"].get("residualRisk", "")), report_sha256, utc_now()),
            )
        receipt = store.conn.execute("SELECT * FROM merge_receipts WHERE integration_id=?", (integration_id,)).fetchone()
        if receipt is None:
            acceptance = store.conn.execute("SELECT source_commit_oid FROM workstream_acceptances WHERE acceptance_id=?", (job["acceptance_id"],)).fetchone()
            if acceptance is None:
                raise NeedsAttentionError("integration acceptance record is missing")
            event = append_event_in_transaction(
                store.conn,
                kind="project.git_integrated",
                project_id=job["project_id"],
                workstream_id=job["workstream_id"],
                payload={"integrationId": integration_id, "acceptanceId": job["acceptance_id"], "targetBranch": job["target_branch"], "previousTargetOid": previous_target_oid, "sourceCommitOid": final_source_oid, "strategy": "rebase-then-ff", "verification": verification},
            )
            store.conn.execute(
                "INSERT INTO merge_receipts(workstream_id,source_commit_oid,target_branch,previous_target_oid,acceptance_id,integration_id,accepted_source_commit_oid,verification_json,strategy,event_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job["workstream_id"], final_source_oid, job["target_branch"], previous_target_oid, job["acceptance_id"], integration_id, acceptance["source_commit_oid"], report_json, "rebase-then-ff", event["event_id"], utc_now()),
            )


def _closeout(store: Any, job: Mapping[str, Any], workspace: Any | None, harness: Any | None) -> dict[str, Any]:
    workstream_id = str(job["workstream_id"])
    project_id = str(job["project_id"])
    inspected = inspect_workstream(store, project_id, workstream_id)
    packet_row = store.conn.execute("SELECT packet_sha256 FROM completion_packets WHERE completion_packet_id=?", (job["candidate_completion_packet_id"],)).fetchone()
    if packet_row is None:
        raise NeedsAttentionError("integration candidate completion packet is missing")
    packet_sha256 = str(packet_row["packet_sha256"])
    if inspected["workstream"]["desired_state"] == "active":
        if workspace is None:
            raise NeedsAttentionError("integrated workstream requires a runtime workspace for closeout")
        complete_workstream(store, project_id, workstream_id, packet_sha256, workspace)
    inspected = inspect_workstream(store, project_id, workstream_id)
    unresolved = unresolved_reporter_issue(store, workstream_id)
    if inspected["workstream"]["desired_state"] == "completed" and unresolved is not None:
        if workspace is None or harness is None:
            raise NeedsAttentionError("retained issue verifier requires runtime adapters for closeout")
        from .refresh import ensure_runtime

        runtime = ensure_runtime(
            store,
            harness,
            workspace,
            workstream_id=workstream_id,
            wait_seconds=30.0,
        )
        if runtime["action"] == "needs_attention":
            raise NeedsAttentionError(str(runtime.get("reason") or "retained issue verifier runtime requires attention"))
        return {
            "workstreamId": workstream_id,
            "state": "completed",
            "retainedForVerification": True,
            "issueId": unresolved["issue_id"],
            "runtime": runtime,
        }
    if inspected["workstream"]["desired_state"] == "completed":
        if workspace is None:
            raise NeedsAttentionError("completed workstream requires a runtime workspace for retirement")
        retire_workstream(store, project_id, workstream_id, workspace)
    inspected = inspect_workstream(store, project_id, workstream_id)
    if inspected["workstream"]["desired_state"] == "retired":
        if workspace is None or harness is None:
            raise NeedsAttentionError("retired workstream requires adapters for cleanup")
        cleanup_workstream(store, {"project": project_id, "workstreamId": workstream_id, "confirm": workstream_id}, workspace, harness)
    return {"workstreamId": workstream_id, "state": store.conn.execute("SELECT desired_state FROM workstreams WHERE workstream_id=?", (workstream_id,)).fetchone()[0]}


def _process_job(store: Any, job: Mapping[str, Any], workspace: Any | None, harness: Any | None) -> dict[str, Any]:
    integration_id = str(job["integration_id"])
    if job["state"] == "needs_attention":
        last_error = str(job["last_error"] or "")
        receipt = store.conn.execute("SELECT 1 FROM merge_receipts WHERE integration_id=?", (integration_id,)).fetchone()
        integrated_closeout = job["integrated_at"] is not None and receipt is not None
        retryable_git_error = (
            last_error == "Git operation refused"
            or (last_error.startswith("Git ") and " operation failed" in last_error)
            or last_error == "worker Reviewr base ref does not match the approved base"
        )
        if integrated_closeout:
            _set_job(store, integration_id, state="integrated", next_action="retry integrated workstream closeout")
            job = {**dict(job), "state": "integrated", "last_error": None}
        elif last_error != "registered project checkout is dirty" and not retryable_git_error:
            return {"integrationId": integration_id, "state": "needs_attention", "reused": True}
        else:
            next_action = "retry after the target checkout was cleaned" if not retryable_git_error else "retry after the reported Git condition was repaired"
            _set_job(store, integration_id, state="queued", next_action=next_action)
            job = {**dict(job), "state": "queued", "last_error": None}
    if job["state"] != "awaiting_worker":
        with store.transaction():
            store.conn.execute("UPDATE integration_jobs SET attempt=attempt+1,updated_at=? WHERE integration_id=?", (utc_now(), integration_id))
        job = {**dict(job), "attempt": int(job["attempt"]) + 1}
    else:
        job = {**dict(job), "attempt": int(job["attempt"])}
    acceptance = store.conn.execute("SELECT * FROM workstream_acceptances WHERE acceptance_id=?", (job["acceptance_id"],)).fetchone()
    if acceptance is None:
        _set_job(store, integration_id, state="needs_attention", error="acceptance record is missing", next_action="restore the immutable acceptance record")
        return {"integrationId": integration_id, "state": "needs_attention"}
    try:
        if job["state"] == "integrated":
            return {"integrationId": integration_id, "state": "integrated", "closeout": _closeout(store, job, workspace, harness)}
        scope = json.loads(str(acceptance["scope_json"]))
        if not isinstance(scope, dict):
            raise NeedsAttentionError("acceptance scope is invalid")
        accepted_paths = json.loads(str(acceptance["changed_paths_json"]))
        try:
            packet_row = store.conn.execute("SELECT packet_sha256 FROM completion_packets WHERE completion_packet_id=?", (job["candidate_completion_packet_id"],)).fetchone()
            if packet_row is None:
                raise NeedsAttentionError("integration candidate completion packet is missing")
            candidate = _candidate(store, str(job["project_id"]), str(job["workstream_id"]), packet_sha256=str(packet_row["packet_sha256"]), expected_paths=accepted_paths, expected_acceptance=scope["acceptance"])
        except ConflictError as error:
            if "source commit is stale" not in str(error):
                raise
            candidate = _current_candidate(store, str(job["project_id"]), str(job["workstream_id"]), scope["taskPacketSha256"], accepted_paths, scope["acceptance"])
            if candidate is None:
                _set_job(store, integration_id, state="awaiting_worker", error="worker branch moved without a matching verified completion packet", next_action="submit a new ready_review checkpoint for the current branch")
                return {"integrationId": integration_id, "state": "awaiting_worker"}
            _set_job(store, integration_id, state="queued", candidate_packet=candidate["packet"]["completion_packet_id"], candidate_source=candidate["sourceOid"], next_action="candidate refreshed after worker verification")
            job = {**dict(job), "state": "queued", "candidate_completion_packet_id": candidate["packet"]["completion_packet_id"], "candidate_source_oid": candidate["sourceOid"]}
        replacement = _latest_replacement(store, candidate, json.loads(str(acceptance["changed_paths_json"])), scope["acceptance"])
        if replacement is not None:
            candidate = replacement
            _set_job(store, integration_id, state="queued", candidate_packet=candidate["packet"]["completion_packet_id"], candidate_source=candidate["sourceOid"], next_action="candidate refreshed after worker verification")
            job = {**dict(job), "state": "queued", "candidate_completion_packet_id": candidate["packet"]["completion_packet_id"], "candidate_source_oid": candidate["sourceOid"]}
        elif job["state"] == "awaiting_worker":
            return {"integrationId": integration_id, "state": "awaiting_worker", "reused": True}
        _set_job(store, integration_id, state="refreshing", next_action="inspect target and candidate")
        with project_git_lock(store.state_root, str(job["project_id"])):
            _project, repository, target_branch, target_oid, porcelain = _primary_state(store, str(job["project_id"]))
            if target_branch != job["target_branch"]:
                raise NeedsAttentionError("registered checkout is not on the accepted target branch")
            if porcelain:
                _set_job(store, integration_id, state="needs_attention", error="registered project checkout is dirty", next_action="clean the target checkout before the secretary retries")
                return {"integrationId": integration_id, "state": "needs_attention"}
            candidate_source = str(candidate["sourceOid"])
            worker_repository = candidate["repository"]
            target_ref = f"refs/pisec/target/{integration_id}"
            worker_branch_ref = f"refs/heads/{candidate['workstream']['branch_name']}"
            worker_before = _oid(worker_repository, worker_branch_ref)
            _run_git(worker_repository, "fetch", "--no-tags", "--no-write-fetch-head", "--", str(repository), f"refs/heads/{target_branch}:{target_ref}")
            if _oid(worker_repository, worker_branch_ref) != worker_before:
                raise NeedsAttentionError("worker branch moved during target import")
            if _oid(worker_repository, target_ref) != target_oid:
                raise NeedsAttentionError("target import did not reach the recorded target commit")
            _run_git(worker_repository, "update-ref", f"refs/remotes/origin/{target_branch}", target_oid)
            _run_git(worker_repository, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{target_branch}")
            _run_git(repository, "fetch", "--no-tags", "--no-write-fetch-head", "--", str(worker_repository), f"+refs/heads/{candidate['workstream']['branch_name']}:refs/pisec/candidates/{integration_id}")
            candidate_ref = f"refs/pisec/candidates/{integration_id}"
            if _oid(repository, candidate_ref) != candidate_source:
                raise NeedsAttentionError("candidate import did not reach the recorded candidate commit")
            ancestor_code, _output = _run_git(repository, "merge-base", "--is-ancestor", target_oid, candidate_ref, accepted=frozenset({0, 1}))
            if ancestor_code != 0:
                _set_job(store, integration_id, state="awaiting_worker", target_oid=target_oid, error="target advanced beyond the accepted candidate", next_action="rebase onto the current target, resolve conflicts within the accepted paths, rerun verification, and submit a new ready_review checkpoint")
                return {"integrationId": integration_id, "state": "awaiting_worker"}
            target_paths = _changed_paths(repository, target_oid, candidate_ref)
            target_patch_sha256 = _patch_digest(repository, target_oid, candidate_ref)
            if not set(target_paths).issubset(accepted_paths):
                _set_job(store, integration_id, state="needs_attention", error="integrated candidate changed paths outside the accepted scope", next_action="inspect the candidate and request a new bounded task")
                return {"integrationId": integration_id, "state": "needs_attention"}
            _set_job(store, integration_id, state="verifying", target_oid=target_oid, next_action="record the accepted verification")
            verification = {
                "completionPacketSha256": candidate["packet"]["packet_sha256"],
                "checks": candidate["packetValue"]["verification"],
                "targetCommitOid": target_oid,
                "candidateSourceCommitOid": candidate_source,
                "targetChangedPaths": target_paths,
                "targetPatchSha256": target_patch_sha256,
            }
            _set_job(store, integration_id, state="applying", next_action="fast-forward target")
            current_head = _oid(repository, "HEAD")
            if current_head != candidate_source:
                _run_git(repository, "merge", "--ff-only", "--no-edit", "--end-of-options", candidate_ref)
            merged_oid = _oid(repository, "HEAD")
            if merged_oid != candidate_source:
                raise NeedsAttentionError("target branch did not reach the candidate source commit")
            _report_and_receipt(store, job, candidate, previous_target_oid=target_oid, final_source_oid=merged_oid, changed_paths=target_paths, verification=verification)
            _run_git(repository, "update-ref", "-d", candidate_ref)
            _run_git(worker_repository, "update-ref", "-d", target_ref)
        _set_job(store, integration_id, state="integrated", target_oid=merged_oid, integration_source=merged_oid, next_action="complete, retire, and clean up the worker")
        refreshed_job = dict(store.conn.execute("SELECT * FROM integration_jobs WHERE integration_id=?", (integration_id,)).fetchone())
        return {"integrationId": integration_id, "state": "integrated", "closeout": _closeout(store, refreshed_job, workspace, harness)}
    except (ConflictError, InvalidRequestError, ScopeMismatchError) as error:
        next_action = "clean the target checkout before retrying" if "dirty" in str(error) else "inspect the integration evidence and resolve the reported condition"
        _set_job(store, integration_id, state="needs_attention", error=str(error), next_action=next_action)
        return {"integrationId": integration_id, "state": "needs_attention"}
    except NeedsAttentionError as error:
        _set_job(store, integration_id, state="needs_attention", error=str(error), next_action="inspect the integration evidence and resolve the reported condition")
        return {"integrationId": integration_id, "state": "needs_attention"}


@control_plane_mutation
def reconcile_integrations(store: Any, workspace: Any | None = None, harness: Any | None = None, *, limit: int = 32, harness_resolver: Any | None = None) -> dict[str, Any]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
        raise InvalidRequestError("integration limit is invalid")
    rows = [dict(row) for row in store.conn.execute("SELECT * FROM integration_jobs WHERE state IN ('queued','refreshing','awaiting_worker','verifying','applying','integrated','needs_attention') ORDER BY created_at,integration_id LIMIT ?", (limit,))]
    result = {"processed": [], "errors": []}
    for row in rows:
        try:
            selected_harness = harness_resolver(str(row["workstream_id"])) if callable(harness_resolver) else harness
            result["processed"].append(_process_job(store, row, workspace, selected_harness))
        except Exception as error:
            result["errors"].append({"integrationId": row["integration_id"], "code": getattr(error, "code", "internal_error"), "message": str(error)[:512]})
    return result


__all__ = [
    "apply_workstream_acceptance",
    "inspect_integration",
    "list_integrations",
    "prepare_workstream_acceptance",
    "reconcile_integrations",
]
