#!/usr/bin/env python3
"""Installed P7 personal/workstream lifecycle and controller-created subagent journey."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile

from scripts.pi_control.docker_runtime import MANAGED_LABEL, PINNED_ACCEPTANCE_IMAGE
from tests.system.evidence import Evidence, write_evidence
from tests.system.staged_install import StagedInstallUnavailable, install


ROOT = Path(__file__).resolve().parents[2]
_LEAK = "must-not-leak"


def command(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False, timeout=300)
    if check and result.returncode != 0:
        raise AssertionError(f"command failed ({result.returncode}): {argv!r}: stdout={result.stdout[-1024:]} stderr={result.stderr[-1024:]}")
    return result


def json_command(argv: list[str], *, env: dict[str, str] | None = None) -> dict:
    return json.loads(command(argv, env=env).stdout)


def rows(state: Path, table: str) -> list[dict]:
    connection = sqlite3.connect(state / "control.db")
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY created_at")]
    finally:
        connection.close()


def git_env() -> dict[str, str]:
    return {
        "PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "GIT_EDITOR": "true",
    }


def git(repository: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repository), *args], env=git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False, timeout=120)
    if result.returncode != 0:
        raise AssertionError(f"git in {repository} {args!r} failed: {result.stderr.strip()[-512:]}")
    return result.stdout.strip()


def git_ok(repository: Path, *args: str) -> bool:
    result = subprocess.run(["git", "-C", str(repository), *args], env=git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False, timeout=120)
    return result.returncode == 0


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def run_launcher(argv: list[str], *, cwd: Path, label: str) -> tuple[int, str, str]:
    environment = {**os.environ, "OPENAI_API_KEY": _LEAK, "GH_TOKEN": _LEAK, "SSH_AUTH_SOCK": f"/{_LEAK}", "DOCKER_HOST": _LEAK, "PI_SYSTEM_STATE_ROOT": "", "PI_SYSTEM_STAGED_ROOT": ""}
    process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")

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
        try:
            stdout, stderr = process.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            interrupt_supervisor()
            raise AssertionError(f"{label} launcher timed out")
    finally:
        atexit.unregister(interrupt_supervisor)
    if process.returncode != 0:
        raise AssertionError(f"{label} launcher failed ({process.returncode}): {stderr[-2048:]}\n{stdout[-2048:]}")
    return process.returncode, stdout, stderr


def main() -> int:
    if shutil.which("docker") is None or command(["docker", "info"], check=False).returncode != 0:
        print("STOP/77: Docker daemon is unavailable", file=sys.stderr)
        return 77
    if command(["docker", "image", "inspect", PINNED_ACCEPTANCE_IMAGE], check=False).returncode != 0:
        print("STOP/77: exact local pinned Python image is unavailable", file=sys.stderr)
        return 77
    with tempfile.TemporaryDirectory(prefix="pi-p7-") as raw:
        root = Path(raw)
        stage = Path(os.environ["PI_SYSTEM_STAGED_ROOT"]).resolve(strict=True) if os.environ.get("PI_SYSTEM_STAGED_ROOT") else root / "stage"
        try:
            built = json.loads((stage / "build-manifest.json").read_text()) if stage.exists() else install(stage)
        except StagedInstallUnavailable as error:
            print(f"STOP/77: staged generation unavailable offline: {error}", file=sys.stderr)
            return 77
        build_id = built.get("buildId")
        controller = stage / "bin/pi-control"
        if not (stage / "bin/pi-workstream").is_file() or not (stage / "bin/pi-system-workstream-run").is_file() or not (stage / "bin/pi-system-container-run").is_file() or not controller.is_file():
            raise AssertionError("staged build is missing a P7 launcher entry point")

        source = root / "source"
        source.mkdir()
        command(["git", "init", "-q", "-b", "main", str(source)])
        (source / "README").write_text("installed process\n")
        command(["git", "-C", str(source), "add", "README"])
        git_date_env = {**os.environ, "GIT_AUTHOR_NAME": "P7", "GIT_AUTHOR_EMAIL": "p7@example.invalid", "GIT_COMMITTER_NAME": "P7", "GIT_COMMITTER_EMAIL": "p7@example.invalid", "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z"}
        command(["git", "-C", str(source), "commit", "-qm", "base"], env=git_date_env)
        repository = root / "assigned"
        command(["git", "-C", str(source), "worktree", "add", "-q", "-b", "personal", str(repository)])
        state = root / "state"

        cli_commands: list[dict] = []
        def cli_command(argv: list[str], *, env: dict[str, str] | None = None, label: str) -> dict:
            result = command(argv, env=env)
            cli_commands.append({"argv": argv, "returncode": result.returncode, "stdoutDigest": digest(result.stdout), "stderrDigest": digest(result.stderr), "label": label})
            return json.loads(result.stdout)

        registered_build = cli_command([str(controller), "--state-root", str(state), "build", "register", "--staged-root", str(stage)], label="build register")
        if registered_build["build_id"] != build_id:
            raise AssertionError("registered staged build identity differs from loaded generation")
        project = cli_command([str(controller), "--state-root", str(state), "project", "register", "--repository", str(repository)], label="project register")
        status = cli_command([str(controller), "--state-root", str(state), "project", "status", project["project_id"]], label="project status")
        primary = next(item for item in status["workingCopies"] if item["kind"] == "primary")

        ws_env = {**os.environ, "PI_SYSTEM_STATE_ROOT": str(state)}
        workstream_request = {
            "projectId": project["project_id"], "title": "P7 A", "brief": {}, "displayName": "P7 A", "idempotencyKey": "p7-a",
        }
        workstream_a = cli_command([str(stage / "bin/pi-workstream"), json.dumps(workstream_request)], env=ws_env, label="workstream create A")
        workstream_a_rerun = cli_command([str(stage / "bin/pi-workstream"), json.dumps(workstream_request)], env=ws_env, label="workstream create A rerun")
        if workstream_a_rerun["workstream_id"] != workstream_a["workstream_id"]:
            raise AssertionError("recoverable workstream rerun produced a different workstream")
        workstream_b = cli_command([str(stage / "bin/pi-workstream"), json.dumps({"projectId": project["project_id"], "title": "P7 B", "brief": {}, "displayName": "P7 B", "idempotencyKey": "p7-b"})], env=ws_env, label="workstream create B")

        for workstream in (workstream_a, workstream_b):
            if workstream["observed_state"] != "ready" or workstream["controller_owned"] != 1:
                raise AssertionError(f"workstream is not controller-owned and ready: {workstream}")
        workstream_rows = rows(state, "workstreams")
        if len(workstream_rows) != 2:
            raise AssertionError(f"expected two workstream rows, got {len(workstream_rows)}")
        worktree_rows = [row for row in rows(state, "working_copies") if row["kind"] == "worktree"]
        if len(worktree_rows) != 2 or any(row["controller_owned"] != 1 or row["observed_state"] != "ready" for row in worktree_rows):
            raise AssertionError(f"unexpected worktree rows: {worktree_rows}")
        identity_fields = ("working_copy_id", "conversation_id", "branch_ref", "worktree_path", "package_environment_root")
        for field in identity_fields:
            if rows(state, "workstreams")[0][field] == rows(state, "workstreams")[1][field]:
                raise AssertionError(f"workstreams share {field}")
        aops = [row for row in rows(state, "operations") if row["idempotency_key"] == "p7-a"]
        if len(aops) != 1 or aops[0]["state"] != "succeeded" or aops[0]["kind"] != "workstream.create":
            raise AssertionError(f"workstream A operation ledger is wrong: {aops}")
        for workstream in (workstream_a, workstream_b):
            environment_root = Path(workstream["package_environment_root"])
            if not environment_root.is_dir() or stat.S_IMODE(environment_root.stat().st_mode) & 0o077:
                raise AssertionError(f"workstream package environment is not a private directory: {environment_root}")
            worktree_path = Path(workstream["worktree_path"])
            marker = (worktree_path / ".git").read_text(encoding="utf-8").strip()
            if not marker.startswith("gitdir: "):
                raise AssertionError(f"workstream has no file-form Git identity: {worktree_path}")
            if git(worktree_path, "rev-parse", "--verify", "HEAD") != primary["expected_head_oid"]:
                raise AssertionError("workstream HEAD differs from the primary baseline")
            if git(worktree_path, "rev-parse", "--verify", "HEAD^{tree}") != primary["expected_tree_oid"]:
                raise AssertionError("workstream tree differs from the primary baseline")
            if git(worktree_path, "symbolic-ref", "--quiet", "HEAD") != workstream["branch_ref"]:
                raise AssertionError("workstream branch identity differs from durable intent")

        session_fields = ("pi_session_id", "session_file")
        conversation_rows = [row for row in rows(state, "conversations") if row["role"] == "workstream"]
        for field in session_fields:
            if conversation_rows[0][field] == conversation_rows[1][field]:
                raise AssertionError(f"workstream conversations share {field}")
        if any(row["authority_profile"] != "writer-container" for row in conversation_rows):
            raise AssertionError("workstream conversation authority is not writer-container")

        personal = cli_command([str(controller), "--state-root", str(state), "conversation", "create", "--request-json", json.dumps({"projectId": project["project_id"], "role": "personal", "displayName": "P7 personal", "workingCopyId": primary["working_copy_id"], "idempotencyKey": "p7-personal"})], label="conversation create personal")

        provider = ROOT / "tests/system/fixtures/scripted-p7-provider.ts"
        child_provider = ROOT / "tests/system/fixtures/scripted-provider.ts"
        probe = ROOT / "tests/system/loaded_resource_probe.ts"

        personal_argv = [str(stage / "bin/pi-system-container-run"), "--state-root", str(state), "--conversation-id", personal["conversation_id"], "--build-id", registered_build["build_id"], "--prompt", json.dumps({"role": "personal"}), "--model", "scripted/scripted-1", "--acceptance-test-profile", "scripted-v1", "--test-provider", str(provider), "--test-probe", str(probe), "--child-test-provider", str(child_provider), "--tool-image", PINNED_ACCEPTANCE_IMAGE]
        personal_code, personal_stdout, personal_stderr = run_launcher(personal_argv, cwd=repository, label="personal")
        if "P7_PERSONAL_FINAL" not in personal_stdout:
            raise AssertionError(f"personal provider did not finish: {personal_stdout[-2048:]}{personal_stderr[-1024:]}")

        workstream_argv = [str(stage / "bin/pi-system-workstream-run"), "--state-root", str(state), "--conversation-id", workstream_b["conversation_id"], "--build-id", registered_build["build_id"], "--prompt", json.dumps({"role": "workstream"}), "--model", "scripted/scripted-1", "--acceptance-test-profile", "scripted-v1", "--test-provider", str(provider), "--test-probe", str(probe), "--child-test-provider", str(child_provider), "--tool-image", PINNED_ACCEPTANCE_IMAGE]
        workstream_code, workstream_stdout, workstream_stderr = run_launcher(workstream_argv, cwd=Path(workstream_b["worktree_path"]), label="workstream")
        if "P7_WORKSTREAM_FINAL" not in workstream_stdout:
            raise AssertionError(f"workstream provider did not finish: {workstream_stdout[-2048:]}{workstream_stderr[-1024:]}")

        writer_runs = [row for row in rows(state, "runs") if row["authority"] == "writer-container"]
        if len(writer_runs) != 2 or any(row["observed_state"] != "stopped" or row["desired_state"] != "stopped" for row in writer_runs):
            raise AssertionError(f"writer runs did not stop cleanly: {writer_runs}")

        child_requests = rows(state, "child_requests")
        if len(child_requests) != 3 or any(row["state"] != "success" for row in child_requests):
            raise AssertionError(f"child requests did not all succeed: {child_requests}")
        snapshot_oids = {row["snapshot_commit_oid"] for row in child_requests}
        snapshot_refs = {row["snapshot_ref"] for row in child_requests}
        if len(snapshot_oids) != 1 or snapshot_oids != {primary["expected_head_oid"]}:
            raise AssertionError(f"child snapshots are not pinned to one immutable revision: {snapshot_oids}")
        if len(snapshot_refs) != len(child_requests):
            raise AssertionError("child snapshot refs are not distinct")
        roles = {row["semantic_role"] for row in child_requests}
        if roles != {"investigator", "reviewer"}:
            raise AssertionError(f"child semantic roles are wrong: {roles}")
        workstream_run_id = next(row["run_id"] for row in writer_runs if row["conversation_id"] == workstream_b["conversation_id"])
        parent_roles = set()
        for row in child_requests:
            if row["parent_run_id"] in {writer_runs[0]["run_id"], writer_runs[1]["run_id"]}:
                parent_roles.add((row["parent_run_id"], row["semantic_role"]))
        if len(parent_roles) != 3 or not any(run_id == workstream_run_id and semantic_role == "reviewer" for run_id, semantic_role in parent_roles):
            raise AssertionError(f"child parent/semantic bindings are wrong: {parent_roles}")

        terminals = rows(state, "child_terminal_records")
        if len(terminals) != 3 or any(row["terminal_class"] != "success" for row in terminals):
            raise AssertionError(f"terminal records are wrong: {terminals}")
        terminal_digests = [row["terminal_digest"] for row in terminals]
        if len(set(terminal_digests)) != len(terminal_digests):
            raise AssertionError("terminal digests are not distinct")

        child_conversations = [row for row in rows(state, "conversations") if row["role"] in {"investigator", "reviewer"}]
        if len(child_conversations) != 3 or any(row["authority_profile"] != "host-read-only" for row in child_conversations):
            raise AssertionError(f"child conversations are wrong: {child_conversations}")
        child_sessions = {row["session_file"] for row in child_conversations}
        if len(child_sessions) != len(child_conversations):
            raise AssertionError("child sessions are not distinct")
        review_worktrees = [row for row in rows(state, "working_copies") if row["kind"] == "review"]
        if len(review_worktrees) != 3 or any(row["effective_mode"] != "read-only" or row["observed_state"] != "ready" for row in review_worktrees):
            raise AssertionError(f"child working copies are wrong: {review_worktrees}")
        for request in child_requests:
            snapshot_path = Path(request["snapshot_path"])
            if not snapshot_path.is_dir() or git(snapshot_path, "rev-parse", "--verify", "HEAD") != request["snapshot_commit_oid"]:
                raise AssertionError("child snapshot is not at its exact immutable commit")
            if git_ok(snapshot_path, "symbolic-ref", "--quiet", "HEAD"):
                raise AssertionError("child snapshot has a branch HEAD instead of a detached commit")
            if git(snapshot_path, "status", "--porcelain=v2", "--untracked-files=all").strip():
                raise AssertionError("child snapshot is not clean")

        personal_notes = repository / "P7_personal.md"
        workstream_notes = Path(workstream_b["worktree_path"]) / "P7_workstream.md"
        if personal_notes.read_text(encoding="utf-8") != "P7 installed process\n":
            raise AssertionError("personal writer file is missing or wrong")
        if workstream_notes.read_text(encoding="utf-8") != "P7 installed process\n":
            raise AssertionError("workstream writer file is missing or wrong")

        messages = rows(state, "project_messages")
        if len(messages) != 2 or any(row["state"] != "pending" for row in messages):
            raise AssertionError(f"installed project messages are wrong: {messages}")
        if not any(row["workstream_id"] == workstream_b["workstream_id"] for row in messages):
            raise AssertionError("workstream message is not bound to its workstream")

        managed = command(["docker", "ps", "-aq", "--filter", f"label={MANAGED_LABEL}=true"]).stdout.split()
        if managed:
            raise AssertionError(f"managed containers remain after installed P7 journey: {managed}")

        combined = personal_stdout + personal_stderr + workstream_stdout + workstream_stderr + json.dumps(child_requests) + json.dumps(workstream_rows) + json.dumps(messages)
        if _LEAK in combined:
            raise AssertionError("installed P7 evidence leaked a credential or environment value")

        assertions_ha005 = {
            "idempotentRerunSameWorkstream": True,
            "controllerOwnedWorkingCopy": True,
            "distinctWorkstreamIdentities": True,
            "worktreeGitIdentityExact": True,
            "workstreamRunningAgentExitedZero": workstream_code == 0,
            "messageBoundToWorkstream": True,
            "writerContainerCleaned": True,
            "packageEnvironmentPrivate": True,
            "credentialLeak": False,
            "managedContainers": managed,
        }
        assertions_ha018 = {
            "childRequestCount": len(child_requests),
            "granularMoreThanOneParent": len(parent_roles) == 3,
            "snapshotRevisionImmutable": len(snapshot_oids) == 1,
            "snapshotsBearer": list(snapshot_refs),
            "childSessionsDistinct": True,
            "childReadOnlyAuthority": True,
            "snapshotWorktreesCleanDetached": True,
            "terminalRecordsCount": len(terminals),
            "terminalDigestsDistinct": True,
            "childRoles": sorted(roles),
            "recoverableWorkstreamSaga": True,
            "credentialLeak": False,
        }
        launcher_commands = (
            {"argv": personal_argv, "returncode": personal_code, "stdoutDigest": digest(personal_stdout), "stderrDigest": digest(personal_stderr), "expected": "zero"},
            {"argv": workstream_argv, "returncode": workstream_code, "stdoutDigest": digest(workstream_stdout), "stderrDigest": digest(workstream_stderr), "expected": "zero"},
        )
        all_commands = tuple(cli_commands) + launcher_commands
        capability = {"modelCanRequest": True, "modelCanApprove": False, "ttyAuthority": "none", "workstreamWriteBrokeredToContainer": True}
        before = {"workstreams": 0, "childRequests": 0, "terminalRecords": 0, "writerRuns": 0}
        after = {"workstreams": 2, "childRequests": len(child_requests), "terminalRecords": len(terminals), "writerRuns": len(writer_runs)}
        evidence_root = Path(os.environ.get("PI_SYSTEM_EVIDENCE_DIR", "/tmp"))
        evidence_a = Evidence("workstream-create-and-run", ("HA-005",), "PASS", "staged-installed", assertions_ha005, commands=all_commands, fixture_id=workstream_b["workstream_id"], source_build_id=build_id, build_id=build_id, before=before, after=after, capability=capability, installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False).as_dict()
        evidence_b = Evidence("p7-installed", ("HA-018",), "PASS", "staged-installed", assertions_ha018, commands=all_commands, fixture_id=workstream_b["conversation_id"], source_build_id=build_id, build_id=build_id, before={"childRequests": 0, "terminalRecords": 0}, after={"childRequests": len(child_requests), "terminalRecords": len(terminals)}, capability=capability, installed_product_action_observed=True, production_mutation_performed=False, remote_provider_contacted=False).as_dict()
        path_a = evidence_root / f"pi-p7-ha005-{workstream_b['workstream_id']}.json"
        path_b = evidence_root / f"pi-p7-ha018-{workstream_b['conversation_id']}.json"
        write_evidence(evidence_a, path_a)
        write_evidence(evidence_b, path_b)

        # Dedicated subagent-isolation envelope (HA-018): controller-created
        # children run read-only against immutable snapshots with distinct
        # sessions and no parent credentials.
        subagent_assertions = {
            "childRequestCount": len(child_requests),
            "childRoles": sorted(roles),
            "snapshotRevisionImmutable": len(snapshot_oids) == 1,
            "snapshotRefsDistinct": len(snapshot_refs) == len(child_requests),
            "childSessionsDistinct": len(child_sessions) == len(child_conversations),
            "childReadOnlyAuthority": True,
            "terminalRecordsDistinct": len(set(terminal_digests)) == len(terminal_digests),
            "credentialLeak": False,
        }
        subagent_evidence = Evidence("subagent-isolation", ("HA-018",), "PASS", "staged-installed", subagent_assertions, commands=all_commands, fixture_id=workstream_b["conversation_id"], source_build_id=build_id, build_id=build_id, before={"childRequests": 0}, after={"childRequests": len(child_requests)}, capability=capability, installed_product_action_observed=True, production_mutation_performed=False, remote_provider_contacted=False).as_dict()
        path_subagent = evidence_root / f"u-subagent-isolation-{workstream_b['conversation_id']}.json"
        write_evidence(subagent_evidence, path_subagent)
        print(json.dumps({"evidenceA": str(path_a), "evidenceB": str(path_b), **evidence_a, "after": after}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())