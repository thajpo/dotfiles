#!/usr/bin/env python3
"""Installed review-exact-revision journey: submit -> accept review -> revise ->
stale receipt rejected -> re-review -> all-submitted-reviews-must-accept gate."""

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


def command(argv: list[str], *, env: dict[str, str] | None = None, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **(env or {})}
    result = subprocess.run(argv, env=merged, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False, timeout=timeout)
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


def _reviewer_run(state: Path, *, conversation_id: str, project_id: str, working_copy_id: str, build_id: str, run_id: str, op_id: str) -> None:
    now = utc_now()
    with sqlite3.connect(state / "control.db") as conn:
        conn.execute("INSERT OR IGNORE INTO operations(operation_id,idempotency_key,kind,resource_type,resource_id,actor_type,request_digest,state,step,request_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (op_id, "u-review-op-" + run_id, "run.prepare", "run", run_id, "controller", "sha256:test", "succeeded", "run-recorded", json.dumps({"test": True}), now, now))
        conn.execute("INSERT OR IGNORE INTO runs(run_id,operation_id,conversation_id,project_id,working_copy_id,authority,desired_state,observed_state,runtime_spec_hash,build_id,channel_binding_hash,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, op_id, conversation_id, project_id, working_copy_id, "host-read-only", "running", "created", "runtime-spec", build_id, "sha256:" + "0" * 64, 1, now, now))
        for obs in ("preparing", "ready", "running"):
            conn.execute("UPDATE runs SET observed_state=?,updated_at=? WHERE run_id=?", (obs, utc_now(), run_id))


def _stop_run(state: Path, run_id: str) -> None:
    with sqlite3.connect(state / "control.db") as conn:
        conn.execute("UPDATE runs SET desired_state='stopped',observed_state='stopped',ended_at=?,updated_at=? WHERE run_id=?", (utc_now(), utc_now(), run_id))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pi-u-review-") as raw:
        root = Path(raw)
        stage = Path(os.environ["PI_SYSTEM_STAGED_ROOT"]).resolve(strict=True) if os.environ.get("PI_SYSTEM_STAGED_ROOT") else root / "stage"
        try:
            built = json.loads((stage / "build-manifest.json").read_text()) if stage.exists() else install(stage)
        except StagedInstallUnavailable as error:
            print(f"STOP/77: staged generation unavailable offline: {error}", file=sys.stderr)
            return 77
        build_id = built.get("buildId")
        controller = stage / "bin/pi-control"
        state = root / "state"

        repo = root / "repository"
        command(["git", "init", "-q", "-b", "main", str(repo)])
        (repo / "README").write_text("revision one\n")
        git_env = {"GIT_AUTHOR_NAME": "U", "GIT_AUTHOR_EMAIL": "u@example.invalid", "GIT_COMMITTER_NAME": "U", "GIT_COMMITTER_EMAIL": "u@example.invalid", "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z"}
        command(["git", "-C", str(repo), "add", "README"], env=git_env)
        command(["git", "-C", str(repo), "commit", "-qm", "base"], env=git_env)
        base_oid = git(repo, "rev-parse", "HEAD")
        command(["git", "-C", str(repo), "branch", "target", base_oid])

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

        # Submit revision 1, review accept.
        (repo / "README").write_text("revision two\n")
        command(["git", "-C", str(repo), "add", "README"], env=git_env)
        command(["git", "-C", str(repo), "commit", "-qm", "two"], env=git_env)
        change = cli([str(controller), "--state-root", str(state), "change", "submit", "--request-json", json.dumps({"project_id": project["project_id"], "working_copy_id": working["working_copy_id"], "target_ref": "refs/heads/target", "title": "review loop", "summary": "r1", "idempotency_key": "u-review-change"})], label="change submit")
        analysis = cli([str(controller), "--state-root", str(state), "integration", "analyze", "--request-json", json.dumps({"projectId": project["project_id"], "changeId": change["changeId"], "revision": change["revision"], "targetWorkingCopyId": working["working_copy_id"], "targetRef": "refs/heads/target"})], label="integration analyze")
        review_evidence = {"integrationId": analysis["integrationId"], "analysisDigest": analysis["analysisDigest"], "targetOid": analysis["targetOid"]}
        assignment = cli([str(controller), "--state-root", str(state), "review", "create-assignment", "--request-json", json.dumps({"change_id": change["changeId"], "revision": change["revision"]})], label="review create-assignment r1")
        run_id = "run_" + "c" * 32
        _reviewer_run(state, conversation_id=assignment["conversationId"], project_id=project["project_id"], working_copy_id=assignment["workingCopyId"], build_id=build_id, run_id=run_id, op_id="op_" + "d" * 32)
        review = cli([str(controller), "--state-root", str(state), "review", "request", "--request-json", json.dumps({"changeId": change["changeId"], "revision": change["revision"], "reviewerConversationId": assignment["conversationId"], "reviewerRunId": run_id, "reviewerActorId": "u-review", "evidence": review_evidence})], label="review request r1")
        _stop_run(state, run_id)
        cli([str(controller), "--state-root", str(state), "review", "submit", "--request-json", json.dumps({"reviewId": review["reviewId"], "verdict": "accept", "summary": "r1 approved", "findings": "first reviewer", "evidence": review_evidence, "reviewerRunId": run_id, "reviewerActorId": "u-review"})], label="review submit r1 accept")

        # Revise: revision 2 supersedes; the r1 receipt must become stale.
        (repo / "README").write_text("revision three\n")
        command(["git", "-C", str(repo), "add", "README"], env=git_env)
        command(["git", "-C", str(repo), "commit", "-qm", "three"], env=git_env)
        revised = cli([str(controller), "--state-root", str(state), "change", "revise", "--request-json", json.dumps({"change_id": change["changeId"], "title": "review loop", "summary": "r2", "capture_mode": "clean", "idempotency_key": "u-review-revise"})], label="change revise r2")
        if revised["revision"] != change["revision"] + 1:
            raise AssertionError(f"revision did not advance: {revised['revision']}")
        stale_rejected = cli([str(controller), "--state-root", str(state), "review", "create-assignment", "--request-json", json.dumps({"change_id": change["changeId"], "revision": change["revision"]})], label="stale review assignment", check=False)
        stale_rejected_ok = "error" in stale_rejected

        # Re-review revision 2: two reviewers, both accept -> authorize succeeds.
        analysis2 = cli([str(controller), "--state-root", str(state), "integration", "analyze", "--request-json", json.dumps({"projectId": project["project_id"], "changeId": change["changeId"], "revision": revised["revision"], "targetWorkingCopyId": working["working_copy_id"], "targetRef": "refs/heads/target"})], label="integration analyze r2")
        evidence2 = {"integrationId": analysis2["integrationId"], "analysisDigest": analysis2["analysisDigest"], "targetOid": analysis2["targetOid"]}
        submitted_reviews: list[dict] = []
        for index, verdict in enumerate(("accept", "accept")):
            assignment2 = cli([str(controller), "--state-root", str(state), "review", "create-assignment", "--request-json", json.dumps({"change_id": change["changeId"], "revision": revised["revision"]})], label=f"review create-assignment r2 reviewer{index}")
            run2 = "run_" + f"{'e' if index == 0 else 'f'}" * 32
            _reviewer_run(state, conversation_id=assignment2["conversationId"], project_id=project["project_id"], working_copy_id=assignment2["workingCopyId"], build_id=build_id, run_id=run2, op_id="op_" + f"{'3' if index == 0 else '4'}" * 32)
            review2 = cli([str(controller), "--state-root", str(state), "review", "request", "--request-json", json.dumps({"changeId": change["changeId"], "revision": revised["revision"], "reviewerConversationId": assignment2["conversationId"], "reviewerRunId": run2, "reviewerActorId": f"u-reviewer-{index}", "evidence": evidence2})], label=f"review request r2 reviewer{index}")
            _stop_run(state, run2)
            submitted = cli([str(controller), "--state-root", str(state), "review", "submit", "--request-json", json.dumps({"reviewId": review2["reviewId"], "verdict": verdict, "summary": f"reviewer {index}", "findings": "second revision", "evidence": evidence2, "reviewerRunId": run2, "reviewerActorId": f"u-reviewer-{index}"})], label=f"review submit r2 reviewer{index}")
            submitted_reviews.append(submitted)
        auth = cli([str(controller), "--state-root", str(state), "integration", "authorize", "--request-json", json.dumps({"integrationId": analysis2["integrationId"], "actorId": "u-review", "requestContextId": "u-review-auth", "expiresAt": "2099-01-01T00:00:00Z"})], label="integration authorize all-accept")

        # A third reviewer submitting changes_requested must block authorization.
        assignment3 = cli([str(controller), "--state-root", str(state), "review", "create-assignment", "--request-json", json.dumps({"change_id": change["changeId"], "revision": revised["revision"]})], label="review create-assignment r2 blocking")
        run3 = "run_" + "1" * 32
        _reviewer_run(state, conversation_id=assignment3["conversationId"], project_id=project["project_id"], working_copy_id=assignment3["workingCopyId"], build_id=build_id, run_id=run3, op_id="op_" + "2" * 32)
        review3 = cli([str(controller), "--state-root", str(state), "review", "request", "--request-json", json.dumps({"changeId": change["changeId"], "revision": revised["revision"], "reviewerConversationId": assignment3["conversationId"], "reviewerRunId": run3, "reviewerActorId": "u-blocker", "evidence": evidence2})], label="review request r2 blocking")
        _stop_run(state, run3)
        cli([str(controller), "--state-root", str(state), "review", "submit", "--request-json", json.dumps({"reviewId": review3["reviewId"], "verdict": "changes_requested", "summary": "block", "findings": "third reviewer", "evidence": evidence2, "reviewerRunId": run3, "reviewerActorId": "u-blocker"})], label="review submit r2 changes_requested")
        blocked = cli([str(controller), "--state-root", str(state), "integration", "authorize", "--request-json", json.dumps({"integrationId": analysis2["integrationId"], "actorId": "u-review", "requestContextId": "u-review-blocked", "expiresAt": "2099-01-01T00:00:00Z"})], label="integration authorize blocked", check=False)
        if "error" not in blocked:
            raise AssertionError("authorization succeeded despite a changes_requested review")

        assertions = {
            "revisionAdvanced": revised["revision"] == change["revision"] + 1,
            "staleReceiptRejected": stale_rejected_ok,
            "allAcceptAuthorizes": auth.get("state") == "active",
            "changesRequestedBlocks": True,
            "submittedReviewCount": len(submitted_reviews),
            "submittedVerdicts": [item["verdict"] for item in submitted_reviews],
        }

        evidence = Evidence(
            "review-exact-revision", ("HA-008",), "PASS", "staged-installed", assertions,
            commands=tuple(cli_commands), fixture_id=change["changeId"], source_build_id=build_id, build_id=build_id,
            before={"revision": change["revision"]}, after={"revision": revised["revision"]},
            capability={"authorityProfile": "host-read-only", "toolRuntime": None},
            installed_product_action_observed=True, production_mutation_performed=False, remote_provider_contacted=False,
        )
        evidence_root = Path(os.environ.get("PI_SYSTEM_EVIDENCE_DIR", root))
        evidence_root.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_root / f"u-review-exact-revision-{change['changeId']}.json"
        write_evidence(evidence.as_dict(), evidence_path)
        print(json.dumps({"evidence": str(evidence_path), "assertions": assertions}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
