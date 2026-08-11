#!/usr/bin/env python3
"""Installed P9 integration journey: analyze, authorize, and fast-forward integrate through the staged controller."""

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


def command(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **(env or {})}
    result = subprocess.run(argv, cwd=cwd, env=merged, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False, timeout=120)
    if check and result.returncode != 0:
        raise AssertionError(f"command failed ({result.returncode}): {argv!r}: stdout={result.stdout[-1024:]} stderr={result.stderr[-1024:]}")
    return result


def json_command(argv: list[str], *, cwd: Path | None = None) -> dict:
    return json.loads(command(argv, cwd=cwd).stdout)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False, timeout=60)
    if result.returncode != 0:
        raise AssertionError(f"git in {repo} {args!r} failed: {result.stderr.strip()[-512:]}")
    return result.stdout.strip()


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pi-p9-") as raw:
        root = Path(raw)
        stage = Path(os.environ["PI_SYSTEM_STAGED_ROOT"]).resolve(strict=True) if os.environ.get("PI_SYSTEM_STAGED_ROOT") else root / "stage"
        try:
            built = json.loads((stage / "build-manifest.json").read_text()) if stage.exists() else install(stage)
        except StagedInstallUnavailable as error:
            print(f"STOP/77: staged generation unavailable offline: {error}", file=sys.stderr)
            return 77
        build_id = built.get("buildId")
        controller = stage / "bin/pi-control"
        if not controller.is_file():
            raise AssertionError("staged build is missing pi-control")

        # -- repo and target branch -------------------------------------------
        repo = root / "repository"
        command(["git", "init", "-q", "-b", "main", str(repo)])
        (repo / "base.txt").write_text("base\n")
        git_env = {"GIT_AUTHOR_NAME": "P9", "GIT_AUTHOR_EMAIL": "p9@example.invalid", "GIT_COMMITTER_NAME": "P9", "GIT_COMMITTER_EMAIL": "p9@example.invalid", "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z"}
        command(["git", "-C", str(repo), "add", "base.txt"], env=git_env)
        command(["git", "-C", str(repo), "commit", "-qm", "base"], env=git_env)
        base_oid = git(repo, "rev-parse", "HEAD")
        command(["git", "-C", str(repo), "branch", "target", base_oid])

        state = root / "state"
        cli_commands: list[dict] = []
        def cli(argv: list[str], *, label: str) -> dict:
            result = command(argv)
            cli_commands.append({"argv": argv, "returncode": result.returncode, "stdoutDigest": digest(result.stdout), "stderrDigest": digest(result.stderr), "label": label})
            return json.loads(result.stdout)

        # -- build register, project register ---------------------------------
        cli([str(controller), "--state-root", str(state), "build", "register", "--staged-root", str(stage)], label="build register")
        project = cli([str(controller), "--state-root", str(state), "project", "register", "--repository", str(repo)], label="project register")
        status = cli([str(controller), "--state-root", str(state), "project", "status", project["project_id"]], label="project status")
        primary = next(item for item in status["workingCopies"] if item["kind"] == "primary")

        # -- submit change against target branch ------------------------------
        (repo / "feature.txt").write_text("feature content\n")
        command(["git", "-C", str(repo), "add", "feature.txt"], env=git_env)
        command(["git", "-C", str(repo), "commit", "-qm", "feature"], env=git_env)
        change = cli([str(controller), "--state-root", str(state), "change", "submit", "--request-json", json.dumps({
            "project_id": project["project_id"], "working_copy_id": primary["working_copy_id"],
            "target_ref": "refs/heads/target", "title": "P9 feature", "summary": "integration candidate",
            "idempotency_key": "p9-integration",
        })], label="change submit")
        (repo / "feature.txt").unlink()
        command(["git", "-C", str(repo), "checkout", "-q", "main"])
        command(["git", "-C", str(repo), "reset", "--hard", base_oid])
        # main is now at base, clean tree; target ref has the candidate tip

        # -- integration analyze (do this FIRST to get analysis IDs for review evidence)
        target_before = git(repo, "rev-parse", "refs/heads/target")
        analysis = cli([str(controller), "--state-root", str(state), "integration", "analyze", "--request-json", json.dumps({
            "projectId": project["project_id"], "changeId": change["changeId"],
            "revision": change["revision"], "targetWorkingCopyId": primary["working_copy_id"],
            "targetRef": "refs/heads/target",
        })], label="integration analyze")
        if analysis["strategy"] not in ("fast-forward", "already-contained"):
            raise AssertionError(f"unexpected integration strategy: {analysis['strategy']}")
        if not analysis.get("analysisDigest"):
            raise AssertionError("integration analysis missing analysisDigest")
        review_evidence = {"integrationId": analysis["integrationId"], "analysisDigest": analysis["analysisDigest"], "targetOid": analysis["targetOid"]}

        # -- create review assignment, insert reviewer run, submit receipt -----
        assignment = cli([str(controller), "--state-root", str(state), "review", "create-assignment", "--request-json", json.dumps({
            "change_id": change["changeId"], "revision": change["revision"],
        })], label="review create-assignment")
        now = utc_now()
        run_id = "run_" + "c" * 32
        op_id = "op_" + "d" * 32
        with sqlite3.connect(state / "control.db") as conn:
            conn.execute(
                "INSERT OR IGNORE INTO operations(operation_id,idempotency_key,kind,resource_type,resource_id,actor_type,request_digest,state,step,request_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (op_id, "p9-review-op", "run.prepare", "run", run_id, "controller", "sha256:test", "succeeded", "run-recorded", json.dumps({"test": True}), now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO runs(run_id,operation_id,conversation_id,project_id,working_copy_id,authority,desired_state,observed_state,runtime_spec_hash,build_id,channel_binding_hash,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, op_id, assignment["conversationId"], project["project_id"], assignment["workingCopyId"], "host-read-only", "running", "created", "runtime-spec", build_id, "sha256:" + "0" * 64, 1, now, now),
            )
            for obs in ("preparing", "ready", "running"):
                conn.execute("UPDATE runs SET observed_state=?,updated_at=? WHERE run_id=?", (obs, utc_now(), run_id))
        review = cli([str(controller), "--state-root", str(state), "review", "request", "--request-json", json.dumps({
            "changeId": change["changeId"], "revision": change["revision"],
            "reviewerConversationId": assignment["conversationId"], "reviewerRunId": run_id,
            "reviewerActorId": "p9-system", "evidence": review_evidence,
        })], label="review request")
        if review["state"] != "requested":
            raise AssertionError(f"review request state is {review['state']}, expected requested")
        # Stop the reviewer run before submitting the verdict
        with sqlite3.connect(state / "control.db") as conn:
            conn.execute("UPDATE runs SET desired_state='stopped',observed_state='stopped',ended_at=?,updated_at=? WHERE run_id=?", (utc_now(), utc_now(), run_id))
        submitted = cli([str(controller), "--state-root", str(state), "review", "submit", "--request-json", json.dumps({
            "reviewId": review["reviewId"], "verdict": "accept", "summary": "P9 approved",
            "findings": "fast-forward candidate", "evidence": review_evidence,
            "reviewerRunId": run_id, "reviewerActorId": "p9-system",
        })], label="review submit")
        if submitted["state"] != "submitted":
            raise AssertionError(f"review submit state is {submitted['state']}, expected submitted")

        # -- integration authorize --------------------------------------------
        auth = cli([str(controller), "--state-root", str(state), "integration", "authorize", "--request-json", json.dumps({
            "integrationId": analysis["integrationId"], "actorId": "p9-actor",
            "requestContextId": "p9-auth", "expiresAt": "2099-01-01T00:00:00Z",
            "reviewId": review["reviewId"],
        })], label="integration authorize")
        if auth["state"] != "active":
            raise AssertionError(f"authorization state is {auth['state']}, expected active")

        # -- integration integrate --------------------------------------------
        integrate_result = cli([str(controller), "--state-root", str(state), "integration", "integrate", "--request-json", json.dumps({
            "integrationId": analysis["integrationId"], "authorizationId": auth["authorizationId"],
        })], label="integration integrate")
        if integrate_result["state"] != "succeeded":
            raise AssertionError(f"integration state is {integrate_result['state']}, expected succeeded")

        # -- assertions -------------------------------------------------------
        target_after = git(repo, "rev-parse", "refs/heads/target")
        if target_after == target_before:
            raise AssertionError("integration did not advance the target ref")
        if integrate_result.get("resultOid") != change["tipOid"]:
            raise AssertionError(f"integration result OID {integrate_result.get('resultOid')} differs from candidate tip {change['tipOid']}")
        rollback_ref = integrate_result.get("rollbackRef", "")
        if not rollback_ref or git(repo, "rev-parse", rollback_ref) != target_before:
            raise AssertionError(f"rollback ref {rollback_ref} does not point to the pre-integration target")

        with sqlite3.connect(state / "control.db") as conn:
            conn.row_factory = sqlite3.Row
            auth_row = conn.execute("SELECT state FROM authorizations WHERE authorization_id=?", (auth["authorizationId"],)).fetchone()
            if auth_row is None or auth_row["state"] != "consumed":
                raise AssertionError("authorization was not consumed after integration")
            change_row = conn.execute("SELECT state FROM changes WHERE change_id=?", (change["changeId"],)).fetchone()
            if change_row is None or change_row["state"] != "merged":
                raise AssertionError("change was not marked merged after integration")

        assertions = {
            "strategy": analysis["strategy"],
            "targetBefore": target_before,
            "targetAfter": target_after,
            "resultOid": integrate_result["resultOid"],
            "candidateTipOid": change["tipOid"],
            "rollbackRef": rollback_ref,
            "rollbackOid": git(repo, "rev-parse", rollback_ref),
            "authorizationConsumed": auth_row["state"],
            "changeMerged": change_row["state"],
        }
        evidence_obj = Evidence(
            "fast-forward-integrate", ("HA-009",), "PASS", "staged-installed", assertions,
            commands=tuple(cli_commands),
            fixture_id=change["changeId"], source_build_id=build_id, build_id=build_id,
            before={"targetOid": target_before, "changeState": "open"},
            after={"targetOid": target_after, "changeState": "merged"},
            capability={"authorityProfile": "host-read-only", "toolRuntime": None},
            installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False,
        )
        evidence_root = Path(os.environ.get("PI_SYSTEM_EVIDENCE_DIR", root))
        evidence_root.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_root / f"p9-ha009-{change['changeId']}.json"
        write_evidence(evidence_obj.as_dict(), evidence_path)
        print(json.dumps({"evidence": str(evidence_path), "assertions": assertions, "analysis": analysis}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
