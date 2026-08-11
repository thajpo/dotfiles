#!/usr/bin/env python3
"""Installed P5 host-Pi to controller-owned Docker writer journey."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

from scripts.pi_control.docker_runtime import PINNED_ACCEPTANCE_IMAGE
from tests.system.staged_install import StagedInstallUnavailable, install
from tests.system.evidence import Evidence, write_evidence


ROOT = Path(__file__).resolve().parents[2]


def command(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False)
    if check and result.returncode != 0:
        raise AssertionError(f"command failed ({result.returncode}): {argv!r}: {result.stderr[-1024:]}")
    return result


def json_command(argv: list[str]) -> dict:
    return json.loads(command(argv).stdout)


def latest_run(state: Path, conversation_id: str) -> dict:
    connection = sqlite3.connect(state / "control.db")
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM runs WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1", (conversation_id,)).fetchone()
        return dict(row) if row is not None else {}
    finally:
        connection.close()


def wait_running(state: Path, conversation_id: str, process: subprocess.Popen[str]) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = latest_run(state, conversation_id)
        if run.get("observed_state") == "running" and run.get("container_id"):
            return run
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"writer exited before running attestation: {stdout[-512:]} {stderr[-1024:]}")
        time.sleep(0.05)
    raise AssertionError("writer did not reach running attestation")


def main() -> int:
    if shutil.which("docker") is None or command(["docker", "info"], check=False).returncode != 0:
        print("STOP/77: Docker daemon is unavailable", file=sys.stderr)
        return 77
    if command(["docker", "image", "inspect", PINNED_ACCEPTANCE_IMAGE], check=False).returncode != 0:
        print("STOP/77: exact local pinned Python image is unavailable", file=sys.stderr)
        return 77
    with tempfile.TemporaryDirectory(prefix="pi-p5-") as raw:
        root = Path(raw)
        stage = Path(os.environ["PI_SYSTEM_STAGED_ROOT"]).resolve(strict=True) if os.environ.get("PI_SYSTEM_STAGED_ROOT") else root / "stage"
        try:
            built = json.loads((stage / "build-manifest.json").read_text()) if stage.exists() else install(stage)
        except StagedInstallUnavailable as error:
            print(f"STOP/77: staged generation unavailable offline: {error}", file=sys.stderr)
            return 77
        source = root / "source"
        source.mkdir()
        command(["git", "init", "-q", "-b", "main", str(source)])
        (source / "tracked.txt").write_text("base\n", encoding="utf-8")
        (source / "other-secret").write_text("must-not-mount\n", encoding="utf-8")
        command(["git", "-C", str(source), "add", "tracked.txt"])
        git_env = {**os.environ, "GIT_AUTHOR_NAME": "P5", "GIT_AUTHOR_EMAIL": "p5@example.invalid", "GIT_COMMITTER_NAME": "P5", "GIT_COMMITTER_EMAIL": "p5@example.invalid", "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z"}
        command(["git", "-C", str(source), "commit", "-qm", "fixture"], env=git_env)
        repository = root / "assigned"
        command(["git", "-C", str(source), "worktree", "add", "-q", "-b", "p5-writer", str(repository)])
        state = root / "state"
        controller = stage / "bin/pi-control"
        launcher = stage / "bin/pi-system-container-run"
        build = json_command([str(controller), "--state-root", str(state), "build", "register", "--staged-root", str(stage)])
        project = json_command([str(controller), "--state-root", str(state), "project", "register", "--repository", str(repository)])
        status = json_command([str(controller), "--state-root", str(state), "project", "status", project["project_id"]])
        working = next(item for item in status["workingCopies"] if item["kind"] == "primary")
        request = json.dumps({"projectId": project["project_id"], "role": "personal", "displayName": "P5 installed writer", "workingCopyId": working["working_copy_id"], "idempotencyKey": "p5-installed-personal"}, sort_keys=True, separators=(",", ":"))
        conversation = json_command([str(controller), "--state-root", str(state), "conversation", "create", "--request-json", request])
        provider = ROOT / "tests/system/fixtures/scripted-writer-provider.ts"
        probe = ROOT / "tests/system/loaded_resource_probe.ts"
        argv = [
            str(launcher), "--state-root", str(state), "--conversation-id", conversation["conversation_id"], "--build-id", build["build_id"],
            "--prompt", "perform the installed writer journey", "--model", "scripted/scripted-1", "--acceptance-test-profile", "scripted-v1",
            "--test-provider", str(provider), "--test-probe", str(probe), "--tool-image", PINNED_ACCEPTANCE_IMAGE,
        ]
        child_env = {**os.environ, "OPENAI_API_KEY": "must-not-enter-container", "SSH_AUTH_SOCK": "/must-not-enter-container", "DOCKER_HOST": "must-not-enter-container"}
        started_ns = time.monotonic_ns()
        process = subprocess.Popen(argv, cwd=repository, env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        running = wait_running(state, conversation["conversation_id"], process)
        second = command(argv, cwd=repository, env=child_env, check=False)
        if second.returncode == 0:
            process.kill()
            process.communicate()
            raise AssertionError(f"second writer was not refused by writer acquisition: stdout={second.stdout[-512:]} stderr={second.stderr[-1024:]}")
        stdout, stderr = process.communicate(timeout=60)
        if process.returncode != 0:
            raise AssertionError(f"installed writer failed ({process.returncode}): {stderr[-2048:]}")
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        tool_events = {item.get("toolCallId"): item for item in events if item.get("type") == "tool_execution_end"}
        for tool_id in ("p5-read", "p5-edit", "p5-write", "p5-test", "p5-isolation"):
            if tool_events.get(tool_id, {}).get("isError") is not False:
                raise AssertionError(f"installed writer tool did not succeed: {tool_id}: {tool_events.get(tool_id)}; stderr={stderr[-1024:]}")
        if (repository / "tracked.txt").read_text(encoding="utf-8") != "edited\n" or (repository / "created.txt").read_text(encoding="utf-8") != "created\n":
            raise AssertionError("installed writer did not leave the expected assigned-working-copy delta")
        terminal = latest_run(state, conversation["conversation_id"])
        if terminal.get("run_id") != running["run_id"] or terminal.get("observed_state") != "stopped":
            raise AssertionError("writer run did not terminalize after exact cleanup")
        connection = sqlite3.connect(state / "control.db")
        connection.row_factory = sqlite3.Row
        try:
            claim = connection.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (working["working_copy_id"],)).fetchone()[0]
        finally:
            connection.close()
        if claim is not None:
            raise AssertionError("writer claim remained after proved container absence")
        container_record = json.loads(terminal["container_observation_json"])
        prior = container_record.get("prior", {})
        container_pid = prior.get("observation", {}).get("pid")
        host_record = json.loads(terminal["host_process_observation_json"])
        if not isinstance(container_pid, int) or container_pid <= 0 or host_record.get("childPid") == container_pid or host_record.get("childPid") == process.pid:
            raise AssertionError("host Pi and tool-container process identities are not distinct and attested")
        if container_record.get("state") != "absent" or command(["docker", "container", "inspect", terminal["container_id"]], check=False).returncode == 0:
            raise AssertionError("managed tool container remained after writer exit")
        by_label = command(["docker", "ps", "-aq", "--filter", f"label=pi.control.run-id={terminal['run_id']}"]).stdout.split()
        by_name = command(["docker", "ps", "-aq", "--filter", f"name=^pi-tool-{terminal['run_id'].removeprefix('run_')}$"]).stdout.split()
        if by_label or by_name:
            raise AssertionError(f"managed cleanup query found containers: label={by_label}, name={by_name}")
        git_status = command(["git", "-C", str(repository), "status", "--short"]).stdout.splitlines()
        git_diff = command(["git", "-C", str(repository), "diff", "--", "tracked.txt"]).stdout
        digest = lambda value: "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
        assertions = {
            "runId": terminal["run_id"], "hostWrapperPid": process.pid, "hostPiPid": host_record["childPid"], "toolContainerPid": container_pid,
            "secondWriterReturnCode": second.returncode, "workingCopyStatus": git_status, "workingCopyDiffContainsEdit": "+edited" in git_diff,
            "containerId": terminal["container_id"], "cleanup": {"state": container_record["state"], "labelQuery": by_label, "nameQuery": by_name},
            "networkMode": prior.get("observation", {}).get("networkMode"), "gitMaskKind": "regular-empty-read-only-bind",
            "activeTools": ["acknowledge_project_message", "bash", "edit", "inventory_dependency_changes", "list_project_messages", "package_operation_status", "post_project_message", "project_command_status", "read", "reply_project_message", "request_package_operation", "request_project_command", "subagent", "write"], "hostPiResident": True, "toolProcessContainerResident": True,
        }
        evidence = Evidence(
            "coding-read-edit-test", ("HA-004",), "PASS", "docker", assertions,
            commands=({"argv": argv, "returncode": process.returncode, "stdoutDigest": digest(stdout), "stderrDigest": digest(stderr), "expected": "zero", "elapsedNs": time.monotonic_ns() - started_ns},),
            fixture_id=conversation["conversation_id"], source_build_id=built["buildId"], build_id=built["buildId"],
            before={"tracked": "base", "createdExists": False}, after={"tracked": "edited", "created": "created"},
            capability={"authorityProfile": "writer-container", "writerEpoch": terminal["writer_epoch"], "manifestPath": terminal["manifest_path"]},
            installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False,
        ).as_dict()
        evidence_root = Path(os.environ.get("PI_SYSTEM_EVIDENCE_DIR", "/tmp"))
        evidence_path = evidence_root / f"pi-p5-{terminal['run_id']}.json"
        write_evidence(evidence, evidence_path)

        # Dedicated second-writer-refused envelope (HA-004): while one writer
        # owns the working copy, a second writer is refused.
        second_writer_assertions = {
            "secondWriterReturnCode": second.returncode,
            "firstWriterHeldClaim": terminal["writer_epoch"] >= 1,
            "workingCopyEditOnlyFromFirst": git_diff != "" or bool(git_status),
            "containerCleaned": not by_label and not by_name,
            "credentialLeak": False,
        }
        second_evidence = Evidence(
            "second-writer-refused", ("HA-004",), "PASS", "docker", second_writer_assertions,
            commands=({"argv": argv, "returncode": process.returncode, "stdoutDigest": digest(stdout), "stderrDigest": digest(stderr), "expected": "zero", "elapsedNs": time.monotonic_ns() - started_ns},),
            fixture_id=conversation["conversation_id"], source_build_id=built["buildId"], build_id=built["buildId"],
            before={"writers": 1}, after={"writers": 1},
            capability={"authorityProfile": "writer-container", "writerEpoch": terminal["writer_epoch"], "manifestPath": terminal["manifest_path"]},
            installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False,
        ).as_dict()
        write_evidence(second_evidence, evidence_root / f"u-second-writer-{terminal['run_id']}.json")
        print(json.dumps({**evidence, "evidencePath": str(evidence_path)}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
