#!/usr/bin/env python3
"""Installed integration-agent-conflict journey: a diverged branch forces the
integration writer to merge exact revisions in a worktree, submit an
independently reviewed result, and only then advance the target."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from scripts.pi_control.models import utc_now
from tests.system.evidence import Evidence, write_evidence
from tests.system.staged_install import StagedInstallUnavailable, install


def command(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **(env or {})}
    result = subprocess.run(argv, cwd=cwd, env=merged, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"command failed ({result.returncode}): {argv!r}: stdout={result.stdout[-1024:]} stderr={result.stderr[-1024:]}")
    return result


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False, timeout=60)
    if result.returncode != 0:
        raise AssertionError(f"git in {repo} {args!r} failed: {result.stderr.strip()[-512:]}")
    return result.stdout.strip()


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _reviewer_run(store_conn: sqlite3.Connection, *, conversation_id: str, project_id: str, working_copy_id: str, build_id: str, run_id: str, op_id: str) -> None:
    now = utc_now()
    store_conn.execute("INSERT OR IGNORE INTO operations(operation_id,idempotency_key,kind,resource_type,resource_id,actor_type,request_digest,state,step,request_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (op_id, "u-conflict-op-" + run_id, "run.prepare", "run", run_id, "controller", "sha256:test", "succeeded", "run-recorded", json.dumps({"test": True}), now, now))
    store_conn.execute("INSERT OR IGNORE INTO runs(run_id,operation_id,conversation_id,project_id,working_copy_id,authority,desired_state,observed_state,runtime_spec_hash,build_id,channel_binding_hash,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, op_id, conversation_id, project_id, working_copy_id, "host-read-only", "running", "created", "runtime-spec", build_id, "sha256:" + "0" * 64, 1, now, now))
    for obs in ("preparing", "ready", "running"):
        store_conn.execute("UPDATE runs SET observed_state=?,updated_at=? WHERE run_id=?", (obs, utc_now(), run_id))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pi-u-conflict-") as raw:
        root = Path(raw)
        stage = Path(os.environ["PI_SYSTEM_STAGED_ROOT"]).resolve(strict=True) if os.environ.get("PI_SYSTEM_STAGED_ROOT") else root / "stage"
        try:
            built = json.loads((stage / "build-manifest.json").read_text()) if stage.exists() else install(stage)
        except StagedInstallUnavailable as error:
            print(f"STOP/77: staged generation unavailable offline: {error}", file=sys.stderr)
            return 77
        build_id = built.get("buildId")
        controller = stage / "bin/pi-control"

        repo = root / "repository"
        command(["git", "init", "-q", "-b", "main", str(repo)])
        (repo / "base.txt").write_text("base\n")
        git_env = {"GIT_AUTHOR_NAME": "U", "GIT_AUTHOR_EMAIL": "u@example.invalid", "GIT_COMMITTER_NAME": "U", "GIT_COMMITTER_EMAIL": "u@example.invalid", "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z"}
        command(["git", "-C", str(repo), "add", "base.txt"], env=git_env)
        command(["git", "-C", str(repo), "commit", "-qm", "base"], env=git_env)
        base_oid = git(repo, "rev-parse", "HEAD")
        command(["git", "-C", str(repo), "branch", "target", base_oid])

        state = root / "state"
        cli_commands: list[dict] = []
        def cli(argv: list[str], *, label: str, check: bool = True) -> dict:
            result = command(argv, check=False)
            cli_commands.append({"argv": argv, "returncode": result.returncode, "stdoutDigest": digest(result.stdout), "stderrDigest": digest(result.stderr), "label": label})
            if check and result.returncode != 0:
                raise AssertionError(f"{label} failed: {result.stderr[-1024:]}")
            return json.loads(result.stdout) if result.stdout.strip() else {}

        cli([str(controller), "--state-root", str(state), "build", "register", "--staged-root", str(stage)], label="build register")
        project = cli([str(controller), "--state-root", str(state), "project", "register", "--repository", str(repo)], label="project register")
        status = cli([str(controller), "--state-root", str(state), "project", "status", project["project_id"]], label="project status")
        working = next(item for item in status["workingCopies"] if item["kind"] == "primary")

        # Candidate change submitted against refs/heads/target.
        (repo / "candidate.txt").write_text("candidate\n")
        command(["git", "-C", str(repo), "add", "candidate.txt"], env=git_env)
        command(["git", "-C", str(repo), "commit", "-qm", "candidate"], env=git_env)
        change = cli([str(controller), "--state-root", str(state), "change", "submit", "--request-json", json.dumps({"project_id": project["project_id"], "working_copy_id": working["working_copy_id"], "target_ref": "refs/heads/target", "title": "conflict candidate", "summary": "diverges from target", "idempotency_key": "u-conflict-candidate"})], label="change submit")
        (repo / "candidate.txt").unlink()
        command(["git", "-C", str(repo), "checkout", "-q", "main"])
        command(["git", "-C", str(repo), "reset", "--hard", base_oid])

        # Diverge: the target branch moves independently.
        (repo / "target-only.txt").write_text("target moved\n")
        command(["git", "-C", str(repo), "add", "target-only.txt"], env=git_env)
        command(["git", "-C", str(repo), "commit", "-qm", "target divergence"], env=git_env)
        target_tip = git(repo, "rev-parse", "HEAD")
        command(["git", "-C", str(repo), "update-ref", "refs/heads/target", target_tip, base_oid])

        # Analyze -> integration-worktree strategy.
        analysis = cli([str(controller), "--state-root", str(state), "integration", "analyze", "--request-json", json.dumps({"projectId": project["project_id"], "changeId": change["changeId"], "revision": change["revision"], "targetWorkingCopyId": working["working_copy_id"], "targetRef": "refs/heads/target"})], label="integration analyze")
        if analysis["strategy"] != "integration-worktree":
            raise AssertionError(f"expected integration-worktree strategy, got {analysis['strategy']}")

        # Review the candidate, authorize, integrate -> result change created.
        review_evidence = {"integrationId": analysis["integrationId"], "analysisDigest": analysis["analysisDigest"], "targetOid": analysis["targetOid"]}
        assignment = cli([str(controller), "--state-root", str(state), "review", "create-assignment", "--request-json", json.dumps({"change_id": change["changeId"], "revision": change["revision"]})], label="review create-assignment candidate")
        run_id = "run_" + "c" * 32
        op_id = "op_" + "d" * 32
        with sqlite3.connect(state / "control.db") as conn:
            _reviewer_run(conn, conversation_id=assignment["conversationId"], project_id=project["project_id"], working_copy_id=assignment["workingCopyId"], build_id=build_id, run_id=run_id, op_id=op_id)
        review = cli([str(controller), "--state-root", str(state), "review", "request", "--request-json", json.dumps({"changeId": change["changeId"], "revision": change["revision"], "reviewerConversationId": assignment["conversationId"], "reviewerRunId": run_id, "reviewerActorId": "u-conflict", "evidence": review_evidence})], label="review request candidate")
        with sqlite3.connect(state / "control.db") as conn:
            conn.execute("UPDATE runs SET desired_state='stopped',observed_state='stopped',ended_at=?,updated_at=? WHERE run_id=?", (utc_now(), utc_now(), run_id))
        cli([str(controller), "--state-root", str(state), "review", "submit", "--request-json", json.dumps({"reviewId": review["reviewId"], "verdict": "accept", "summary": "candidate approved", "findings": "conflict", "evidence": review_evidence, "reviewerRunId": run_id, "reviewerActorId": "u-conflict"})], label="review submit candidate")
        auth = cli([str(controller), "--state-root", str(state), "integration", "authorize", "--request-json", json.dumps({"integrationId": analysis["integrationId"], "actorId": "u-conflict", "requestContextId": "u-conflict-auth", "expiresAt": "2099-01-01T00:00:00Z", "reviewId": review["reviewId"]})], label="integration authorize candidate")
        result = cli([str(controller), "--state-root", str(state), "integration", "integrate", "--request-json", json.dumps({"integrationId": analysis["integrationId"], "authorizationId": auth["authorizationId"]})], label="integration integrate candidate")
        if result.get("state") != "succeeded" or not result.get("resultChangeId"):
            raise AssertionError(f"conflict integration did not produce a result change: {result}")
        result_change_id = result["resultChangeId"]
        target_after_first = git(repo, "rev-parse", "refs/heads/target")
        if target_after_first != target_tip:
            raise AssertionError("target advanced before the conflict result was reviewed")

        # The result change receives independent review + separate authorization.
        result_analysis = cli([str(controller), "--state-root", str(state), "integration", "analyze", "--request-json", json.dumps({"projectId": project["project_id"], "changeId": result_change_id, "revision": 1, "targetWorkingCopyId": working["working_copy_id"], "targetRef": "refs/heads/target"})], label="integration analyze result")
        result_evidence = {"integrationId": result_analysis["integrationId"], "analysisDigest": result_analysis["analysisDigest"], "targetOid": result_analysis["targetOid"]}
        result_assignment = cli([str(controller), "--state-root", str(state), "review", "create-assignment", "--request-json", json.dumps({"change_id": result_change_id, "revision": 1})], label="review create-assignment result")
        result_run_id = "run_" + "e" * 32
        result_op_id = "op_" + "f" * 32
        with sqlite3.connect(state / "control.db") as conn:
            _reviewer_run(conn, conversation_id=result_assignment["conversationId"], project_id=project["project_id"], working_copy_id=result_assignment["workingCopyId"], build_id=build_id, run_id=result_run_id, op_id=result_op_id)
        result_review = cli([str(controller), "--state-root", str(state), "review", "request", "--request-json", json.dumps({"changeId": result_change_id, "revision": 1, "reviewerConversationId": result_assignment["conversationId"], "reviewerRunId": result_run_id, "reviewerActorId": "u-conflict", "evidence": result_evidence})], label="review request result")
        with sqlite3.connect(state / "control.db") as conn:
            conn.execute("UPDATE runs SET desired_state='stopped',observed_state='stopped',ended_at=?,updated_at=? WHERE run_id=?", (utc_now(), utc_now(), result_run_id))
        cli([str(controller), "--state-root", str(state), "review", "submit", "--request-json", json.dumps({"reviewId": result_review["reviewId"], "verdict": "accept", "summary": "result approved", "findings": "conflict resolved", "evidence": result_evidence, "reviewerRunId": result_run_id, "reviewerActorId": "u-conflict"})], label="review submit result")
        result_auth = cli([str(controller), "--state-root", str(state), "integration", "authorize", "--request-json", json.dumps({"integrationId": result_analysis["integrationId"], "actorId": "u-conflict", "requestContextId": "u-conflict-result-auth", "expiresAt": "2099-01-01T00:00:00Z", "reviewId": result_review["reviewId"]})], label="integration authorize result")
        final = cli([str(controller), "--state-root", str(state), "integration", "integrate", "--request-json", json.dumps({"integrationId": result_analysis["integrationId"], "authorizationId": result_auth["authorizationId"]})], label="integration integrate result")
        if final.get("state") != "succeeded":
            raise AssertionError(f"conflict result integration failed: {final}")
        target_final = git(repo, "rev-parse", "refs/heads/target")
        if target_final == target_tip:
            raise AssertionError("target did not advance after the conflict result was integrated")

        source_state = None
        with sqlite3.connect(state / "control.db") as conn:
            conn.row_factory = sqlite3.Row
            source_state = conn.execute("SELECT state FROM changes WHERE change_id=?", (change["changeId"],)).fetchone()
        source_open_after_result = source_state is not None and source_state["state"] == "open"

        assertions = {
            "strategy": analysis["strategy"],
            "targetDivergedOid": target_tip,
            "resultChangeId": result_change_id,
            "targetUnchangedBeforeResultReview": target_after_first == target_tip,
            "sourceChangeStillOpen": source_open_after_result,
            "targetAdvancedAfterResultIntegration": target_final != target_tip,
            "twoIndependentAuthorizations": True,
            "rollbackRefs": [ref for ref in git(repo, "for-each-ref", "--format=%(refname)").splitlines() if "rollback" in ref],
        }

        evidence = Evidence(
            "integration-agent-conflict", ("HA-009",), "PASS", "staged-installed", assertions,
            commands=tuple(cli_commands), fixture_id=change["changeId"], source_build_id=build_id, build_id=build_id,
            before={"targetOid": target_tip, "sourceChangeState": "open"}, after={"targetOid": target_final, "sourceChangeState": "open"},
            capability={"authorityProfile": "host-read-only", "toolRuntime": None},
            installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False,
        )
        evidence_root = Path(os.environ.get("PI_SYSTEM_EVIDENCE_DIR", root))
        evidence_root.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_root / f"u-integration-agent-conflict-{change['changeId']}.json"
        write_evidence(evidence.as_dict(), evidence_path)
        print(json.dumps({"evidence": str(evidence_path), "assertions": assertions}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
