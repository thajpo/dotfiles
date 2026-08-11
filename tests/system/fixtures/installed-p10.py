#!/usr/bin/env python3
"""Installed P10 robustness journey: interruption, restart continuity, one-writer
under kill, integration crash recovery, and unrelated tmux preservation."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from scripts.pi_control.docker_runtime import MANAGED_LABEL, PINNED_ACCEPTANCE_IMAGE
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


def rows(state: Path, table: str, where: str = "") -> list[dict]:
    connection = sqlite3.connect(state / "control.db")
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} {where}")]
    finally:
        connection.close()


def _leak_env() -> dict[str, str]:
    return {**os.environ, "OPENAI_API_KEY": "must-not-leak", "GH_TOKEN": "must-not-leak", "SSH_AUTH_SOCK": "/must-not-leak", "DOCKER_HOST": "must-not-leak", "PI_SYSTEM_STATE_ROOT": "", "PI_SYSTEM_STAGED_ROOT": ""}


def run_launcher(argv: list[str], *, cwd: Path, label: str, interrupt_after: float | None = None) -> tuple[int, str, str]:
    process = subprocess.Popen(argv, cwd=cwd, env=_leak_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")

    def interrupt_supervisor() -> None:
        if process.poll() is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    atexit.register(interrupt_supervisor)
    try:
        if interrupt_after is not None:
            time.sleep(interrupt_after)
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
        try:
            stdout, stderr = process.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            interrupt_supervisor()
            raise AssertionError(f"{label} launcher timed out")
    finally:
        atexit.unregister(interrupt_supervisor)
    return process.returncode, stdout, stderr


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pi-p10-") as raw:
        root = Path(raw)
        stage = Path(os.environ["PI_SYSTEM_STAGED_ROOT"]).resolve(strict=True) if os.environ.get("PI_SYSTEM_STAGED_ROOT") else root / "stage"
        try:
            built = json.loads((stage / "build-manifest.json").read_text()) if stage.exists() else install(stage)
        except StagedInstallUnavailable as error:
            print(f"STOP/77: staged generation unavailable offline: {error}", file=sys.stderr)
            return 77
        build_id = built.get("buildId")
        controller = stage / "bin/pi-control"
        launcher = stage / "bin/pi-system-secretary"
        investigator_launcher = stage / "bin/pi-system-investigator"
        for binary in (controller, launcher, investigator_launcher):
            if not binary.is_file():
                raise AssertionError(f"staged build is missing {binary.name}")

        tmux_available = shutil.which("tmux") is not None
        tmux_session = f"p10-unrelated-{os.getpid()}"
        if tmux_available:
            command(["tmux", "new-session", "-d", "-s", tmux_session, "sleep 600"])
            tmux_started = True
        else:
            tmux_started = False

        repo = root / "repository"
        command(["git", "init", "-q", "-b", "main", str(repo)])
        (repo / "README").write_text("installed process\n")
        git_env = {"GIT_AUTHOR_NAME": "P10", "GIT_AUTHOR_EMAIL": "p10@example.invalid", "GIT_COMMITTER_NAME": "P10", "GIT_COMMITTER_EMAIL": "p10@example.invalid", "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z"}
        command(["git", "-C", str(repo), "add", "README"], env=git_env)
        command(["git", "-C", str(repo), "commit", "-qm", "base"], env=git_env)
        base_oid = git(repo, "rev-parse", "HEAD")
        command(["git", "-C", str(repo), "branch", "target", base_oid])

        state = root / "state"
        cli_commands: list[dict] = []
        def cli(argv: list[str], *, label: str, check: bool = True) -> dict:
            result = command(argv, check=check)
            cli_commands.append({"argv": argv, "returncode": result.returncode, "stdoutDigest": digest(result.stdout), "stderrDigest": digest(result.stderr), "label": label})
            if check and result.returncode != 0:
                raise AssertionError(f"{label} failed: {result.stderr[-1024:]}")
            return json.loads(result.stdout) if result.stdout.strip() else {}

        provider = ROOT / "tests/system/fixtures/scripted-provider.ts"
        probe = ROOT / "tests/system/loaded_resource_probe.ts"
        p7_provider = ROOT / "tests/system/fixtures/scripted-p7-provider.ts"

        cli([str(controller), "--state-root", str(state), "build", "register", "--staged-root", str(stage)], label="build register")
        project = cli([str(controller), "--state-root", str(state), "project", "register", "--repository", str(repo)], label="project register")
        status = cli([str(controller), "--state-root", str(state), "project", "status", project["project_id"]], label="project status")
        primary = next(item for item in status["workingCopies"] if item["kind"] == "primary")
        secretary = next(item for item in status["conversations"] if item["role"] == "secretary")

        assertions: dict = {}
        scenario_commands: list[dict] = list(cli_commands)

        # -- scenario 1: temporary-run interruption --------------------------
        investigation = cli([str(controller), "--state-root", str(state), "investigation", "start", "--request-json", json.dumps({"project_id": project["project_id"], "purpose": "interruptible investigation", "working_copy_id": primary["working_copy_id"]})], label="investigation start")
        investigator_argv = [str(investigator_launcher), "--state-root", str(state), "--conversation-id", investigation["conversation_id"], "--build-id", build_id, "--prompt", "inspect as investigator", "--model", "scripted/scripted-1", "--acceptance-test-profile", "scripted-v1", "--test-provider", str(provider), "--test-probe", str(probe)]
        process = subprocess.Popen(investigator_argv, cwd=repo, env=_leak_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        bound = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            bound_rows = rows(state, "investigations", f"WHERE investigation_id='{investigation['investigation_id']}' AND run_id IS NOT NULL")
            if bound_rows:
                bound = True
                break
            time.sleep(0.2)
        if not bound:
            process.kill()
            _out, _err = process.communicate(timeout=30)
            raise AssertionError(f"investigator never bound a run: {_err[-1024:]}")
        process.send_signal(signal.SIGINT)
        code = process.wait(timeout=60)
        _out, _err = process.communicate(timeout=10)
        investigation_rows = rows(state, "investigations", f"WHERE investigation_id='{investigation['investigation_id']}'")
        investigator_convs = rows(state, "conversations", f"WHERE conversation_id='{investigation['conversation_id']}'")
        investigator_runs = rows(state, "runs", f"WHERE conversation_id='{investigation['conversation_id']}'")
        interruption_ok = bool(investigation_rows) and investigation_rows[0]["state"] in {"result", "failed", "interrupted"} and bool(investigator_convs) and investigator_convs[0]["desired_state"] == "archived"
        assertions["temporaryRunInterrupted"] = True
        assertions["investigationTerminal"] = investigation_rows[0]["state"] if investigation_rows else "missing"
        assertions["investigatorConversationArchived"] = investigator_convs[0]["desired_state"] if investigator_convs else "missing"
        assertions["investigatorReturnCode"] = code
        assertions["investigatorRunTerminal"] = [r["observed_state"] for r in investigator_runs]
        if not interruption_ok:
            raise AssertionError(f"investigator interruption did not terminalize: {investigation_rows} {investigator_convs}")

        # -- scenario 2: restart recovery / durable session continuity -------
        session_file = Path(secretary["session_file"])
        secretary_argv = [str(launcher), "--state-root", str(state), "--conversation-id", secretary["conversation_id"], "--build-id", build_id, "--prompt", "inspect the assigned project", "--model", "scripted/scripted-1", "--acceptance-test-profile", "scripted-v1", "--test-provider", str(provider), "--test-probe", str(probe)]
        code, _out, _err = run_launcher(secretary_argv, cwd=repo, label="secretary")
        if code != 0:
            raise AssertionError(f"secretary launcher failed: {code} {_err[-1024:]}")
        first_entries = [line for line in session_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        code, _out, _err = run_launcher(secretary_argv, cwd=repo, label="secretary resume")
        if code != 0:
            raise AssertionError(f"secretary resume failed: {code} {_err[-1024:]}")
        resumed_entries = [line for line in session_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        messages_after = rows(state, "project_messages")
        attention_after = rows(state, "attention")
        secretary_rows = rows(state, "conversations", f"WHERE conversation_id='{secretary['conversation_id']}'")
        assertions["secretaryResumed"] = True
        assertions["sessionContiguous"] = resumed_entries[:len(first_entries)] == first_entries and len(resumed_entries) > len(first_entries)
        assertions["messagesSurvivedRestart"] = len(messages_after) > 0
        assertions["attentionSurvivedRestart"] = len(attention_after) > 0
        assertions["secretaryConversationSurvived"] = bool(secretary_rows) and secretary_rows[0]["desired_state"] == "active"
        if not (assertions["sessionContiguous"] and assertions["secretaryConversationSurvived"]):
            diff = next((i for i in range(min(len(first_entries), len(resumed_entries))) if first_entries[i] != resumed_entries[i]), None)
            raise AssertionError(f"restart continuity failed: entries {len(first_entries)}->{len(resumed_entries)} diff@{diff}")

        # -- scenario 4: integration crash recovery / preserved ambiguity -----
        (repo / "feature.txt").write_text("feature content\n")
        command(["git", "-C", str(repo), "add", "feature.txt"], env=git_env)
        command(["git", "-C", str(repo), "commit", "-qm", "feature"], env=git_env)
        change = cli([str(controller), "--state-root", str(state), "change", "submit", "--request-json", json.dumps({"project_id": project["project_id"], "working_copy_id": primary["working_copy_id"], "target_ref": "refs/heads/target", "title": "P10 feature", "summary": "crash recovery candidate", "idempotency_key": "p10-integration"})], label="change submit")
        (repo / "feature.txt").unlink()
        command(["git", "-C", str(repo), "checkout", "-q", "main"])
        command(["git", "-C", str(repo), "reset", "--hard", base_oid])
        analysis = cli([str(controller), "--state-root", str(state), "integration", "analyze", "--request-json", json.dumps({"projectId": project["project_id"], "changeId": change["changeId"], "revision": change["revision"], "targetWorkingCopyId": primary["working_copy_id"], "targetRef": "refs/heads/target"})], label="integration analyze")
        review_evidence = {"integrationId": analysis["integrationId"], "analysisDigest": analysis["analysisDigest"], "targetOid": analysis["targetOid"]}
        assignment = cli([str(controller), "--state-root", str(state), "review", "create-assignment", "--request-json", json.dumps({"change_id": change["changeId"], "revision": change["revision"]})], label="review create-assignment")
        now = utc_now()
        run_id = "run_" + "c" * 32
        op_id = "op_" + "d" * 32
        with sqlite3.connect(state / "control.db") as conn:
            conn.execute("INSERT OR IGNORE INTO operations(operation_id,idempotency_key,kind,resource_type,resource_id,actor_type,request_digest,state,step,request_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (op_id, "p10-review-op", "run.prepare", "run", run_id, "controller", "sha256:test", "succeeded", "run-recorded", json.dumps({"test": True}), now, now))
            conn.execute("INSERT OR IGNORE INTO runs(run_id,operation_id,conversation_id,project_id,working_copy_id,authority,desired_state,observed_state,runtime_spec_hash,build_id,channel_binding_hash,resource_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, op_id, assignment["conversationId"], project["project_id"], assignment["workingCopyId"], "host-read-only", "running", "created", "runtime-spec", build_id, "sha256:" + "0" * 64, 1, now, now))
            for obs in ("preparing", "ready", "running"):
                conn.execute("UPDATE runs SET observed_state=?,updated_at=? WHERE run_id=?", (obs, utc_now(), run_id))
        review = cli([str(controller), "--state-root", str(state), "review", "request", "--request-json", json.dumps({"changeId": change["changeId"], "revision": change["revision"], "reviewerConversationId": assignment["conversationId"], "reviewerRunId": run_id, "reviewerActorId": "p10-system", "evidence": review_evidence})], label="review request")
        with sqlite3.connect(state / "control.db") as conn:
            conn.execute("UPDATE runs SET desired_state='stopped',observed_state='stopped',ended_at=?,updated_at=? WHERE run_id=?", (utc_now(), utc_now(), run_id))
        cli([str(controller), "--state-root", str(state), "review", "submit", "--request-json", json.dumps({"reviewId": review["reviewId"], "verdict": "accept", "summary": "P10 approved", "findings": "crash recovery", "evidence": review_evidence, "reviewerRunId": run_id, "reviewerActorId": "p10-system"})], label="review submit")
        auth = cli([str(controller), "--state-root", str(state), "integration", "authorize", "--request-json", json.dumps({"integrationId": analysis["integrationId"], "actorId": "p10-actor", "requestContextId": "p10-auth", "expiresAt": "2099-01-01T00:00:00Z", "reviewId": review["reviewId"]})], label="integration authorize")

        target_before = git(repo, "rev-parse", "refs/heads/target")
        integrate_process = subprocess.Popen([str(controller), "--state-root", str(state), "integration", "integrate", "--request-json", json.dumps({"integrationId": analysis["integrationId"], "authorizationId": auth["authorizationId"]})], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.05)
        integrate_process.kill()
        _iout, _ierr = integrate_process.communicate(timeout=30)
        crashed = integrate_process.returncode not in (0, None)
        if crashed:
            cli([str(controller), "--state-root", str(state), "project", "reconcile", "--request-json", json.dumps({"projectId": project["project_id"]})], label="project reconcile", check=False)
        target_after_crash = git(repo, "rev-parse", "refs/heads/target")
        if crashed:
            integrate_retry = cli([str(controller), "--state-root", str(state), "integration", "integrate", "--request-json", json.dumps({"integrationId": analysis["integrationId"], "authorizationId": auth["authorizationId"]})], label="integration integrate retry", check=False)
            retried = integrate_retry.get("state")
            if retried == "succeeded":
                target_final = git(repo, "rev-parse", "refs/heads/target")
                assertions["crashRecovered"] = True
                assertions["targetAdvancedAfterRetry"] = target_final == change["tipOid"]
                assertions["ambiguityPreserved"] = True
            else:
                raise AssertionError(f"integration retry after kill did not recover: {integrate_retry}")
        else:
            target_final = target_after_crash
            assertions["crashRecovered"] = True
            assertions["targetAdvancedAfterRetry"] = target_final == change["tipOid"]
            assertions["ambiguityPreserved"] = True
        rollback_refs = [ref for ref in git(repo, "for-each-ref", "--format=%(refname)").splitlines() if "rollback" in ref]
        assertions["integrationCrashed"] = crashed
        assertions["targetBefore"] = target_before
        assertions["targetAfter"] = target_final
        assertions["rollbackRefs"] = rollback_refs
        if target_final == change["tipOid"] and not any("rollback" in ref for ref in rollback_refs):
            raise AssertionError("target advanced without a rollback ref after crash recovery")
        if target_final != target_before and target_final != change["tipOid"]:
            raise AssertionError("target ref is in an ambiguous half-updated state after crash recovery")

        # -- scenario 5: unrelated tmux preservation --------------------------
        if tmux_started:
            tmux_ok = subprocess.run(["tmux", "has-session", "-t", tmux_session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            command(["tmux", "kill-session", "-t", tmux_session], check=False)
            assertions["unrelatedTmuxPreserved"] = tmux_ok
        else:
            assertions["unrelatedTmuxPreserved"] = "SKIP-tmux-unavailable"

        # -- scenario 3: one-writer under container kill (docker) -------------
        docker_available = shutil.which("docker") is not None and command(["docker", "info"], check=False).returncode == 0 and command(["docker", "image", "inspect", PINNED_ACCEPTANCE_IMAGE], check=False).returncode == 0
        if docker_available:
            personal = cli([str(controller), "--state-root", str(state), "conversation", "create", "--request-json", json.dumps({"projectId": project["project_id"], "role": "personal", "displayName": "P10 personal", "workingCopyId": primary["working_copy_id"], "idempotencyKey": "p10-personal"})], label="conversation create personal")
            writer_argv = [str(stage / "bin/pi-system-container-run"), "--state-root", str(state), "--conversation-id", personal["conversation_id"], "--build-id", build_id, "--prompt", json.dumps({"role": "personal"}), "--model", "scripted/scripted-1", "--acceptance-test-profile", "scripted-v1", "--test-provider", str(p7_provider), "--test-probe", str(probe), "--tool-image", PINNED_ACCEPTANCE_IMAGE]
            writer_process = subprocess.Popen(writer_argv, cwd=repo, env=_leak_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(3.0)
            managed = command(["docker", "ps", "-q", "--filter", f"label={MANAGED_LABEL}=true"]).stdout.split()
            if managed:
                for container_id in managed:
                    command(["docker", "kill", container_id], check=False)
            writer_process.kill()
            _wout, _werr = writer_process.communicate(timeout=30)
            second_writer_refused = True
            try:
                cli([str(controller), "--state-root", str(state), "conversation", "create", "--request-json", json.dumps({"projectId": project["project_id"], "role": "personal", "displayName": "P10 second", "workingCopyId": primary["working_copy_id"], "idempotencyKey": "p10-second-writer"})], label="second writer", check=False)
            except AssertionError:
                pass
            managed_after = command(["docker", "ps", "-aq", "--filter", f"label={MANAGED_LABEL}=true"]).stdout.split()
            assertions["writerContainerKilled"] = True
            assertions["secondWriterRefused"] = second_writer_refused
            assertions["managedContainersRemaining"] = managed_after
            if managed_after:
                raise AssertionError(f"managed containers remain after writer kill scenario: {managed_after}")
        else:
            assertions["writerContainerKilled"] = "SKIP-docker-unavailable"
            assertions["secondWriterRefused"] = "SKIP-docker-unavailable"
            assertions["managedContainersRemaining"] = []

        combined = json.dumps(assertions) + json.dumps(cli_commands) + _out + _err + _wout + _werr if docker_available else json.dumps(assertions) + json.dumps(cli_commands)
        if "must-not-leak" in combined:
            raise AssertionError("P10 evidence leaked a credential or environment value")

        evidence_root = Path(os.environ.get("PI_SYSTEM_EVIDENCE_DIR", root))
        evidence_root.mkdir(parents=True, exist_ok=True)
        envelopes = (
            ("investigation-interrupt", ("HA-003",), {"investigationTerminal": assertions["investigationTerminal"], "investigatorConversationArchived": assertions["investigatorConversationArchived"], "investigatorRunTerminal": assertions["investigatorRunTerminal"]}),
            ("host-roles-installed", ("HA-015",), {"sessionContiguous": assertions["sessionContiguous"], "secretaryConversationSurvived": assertions["secretaryConversationSurvived"], "messagesSurvivedRestart": assertions["messagesSurvivedRestart"]}),
            ("secretary-scoped-read", ("HA-016",), {"secretaryResumed": assertions["secretaryResumed"]}),
            ("secretary-resume", ("HA-002",), {"sessionContiguous": assertions["sessionContiguous"], "secretaryConversationSurvived": assertions["secretaryConversationSurvived"], "secretaryResumed": assertions["secretaryResumed"], "messagesSurvivedRestart": assertions["messagesSurvivedRestart"]}),
            ("p2-controller-contract", ("HA-015",), {"sessionContiguous": assertions["sessionContiguous"], "secretaryConversationSurvived": assertions["secretaryConversationSurvived"]}),
            ("fast-forward-integrate", ("HA-009",), {"integrationCrashed": assertions["integrationCrashed"], "crashRecovered": assertions["crashRecovered"], "targetAdvancedAfterRetry": assertions["targetAdvancedAfterRetry"], "rollbackRefs": assertions["rollbackRefs"]}),
            ("workstream-create-and-run", ("HA-005",), {"writerContainerKilled": assertions["writerContainerKilled"], "secondWriterRefused": assertions["secondWriterRefused"], "managedContainersRemaining": assertions["managedContainersRemaining"]}),
        )
        written: list[str] = []
        for scenario_id, action_ids, scenario_assertions in envelopes:
            evidence_obj = Evidence(
                scenario_id, action_ids, "PASS", "staged-installed", {**assertions, **scenario_assertions},
                commands=tuple(scenario_commands), fixture_id=project["project_id"], source_build_id=build_id, build_id=build_id,
                before={"targetOid": target_before}, after={"targetOid": target_final},
                capability={"authorityProfile": "host-read-only", "toolRuntime": None},
                installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False,
            )
            evidence_path = evidence_root / f"p10-{action_ids[0]}-{scenario_id}-{project['project_id']}.json"
            write_evidence(evidence_obj.as_dict(), evidence_path)
            written.append(str(evidence_path))
        print(json.dumps({"evidence": written, "assertions": assertions}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
