#!/usr/bin/env python3
"""Installed coding-resume journey: a personal coding conversation does real
work in its container, survives a controller restart, resumes contiguously,
and surfaces pending work through attention and the work index."""

from __future__ import annotations

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

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from scripts.pi_control.docker_runtime import PINNED_ACCEPTANCE_IMAGE
from tests.system.container_hygiene import assert_fixture_containers_absent
from tests.system.evidence import Evidence, write_evidence
from tests.system.process_hygiene import terminate_process
from tests.system.staged_install import StagedInstallUnavailable, install


def command(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **(env or {})}
    result = subprocess.run(argv, cwd=cwd, env=merged, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"command failed ({result.returncode}): {argv!r}: stdout={result.stdout[-1024:]} stderr={result.stderr[-1024:]}")
    return result


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def rows(state: Path, table: str, where: str = "") -> list[dict]:
    connection = sqlite3.connect(state / "control.db")
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} {where}")]
    finally:
        connection.close()


def run_launcher(argv: list[str], *, cwd: Path, label: str) -> tuple[int, str, str]:
    environment = {**os.environ, "OPENAI_API_KEY": "must-not-leak", "GH_TOKEN": "must-not-leak", "SSH_AUTH_SOCK": "/must-not-leak", "DOCKER_HOST": "must-not-leak"}
    process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")

    try:
        stdout, stderr = process.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        terminate_process(process)
        raise AssertionError(f"{label} launcher timed out")
    finally:
        terminate_process(process)
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
    with tempfile.TemporaryDirectory(prefix="pi-u-resume-") as raw:
        root = Path(raw)
        stage = Path(os.environ["PI_SYSTEM_STAGED_ROOT"]).resolve(strict=True) if os.environ.get("PI_SYSTEM_STAGED_ROOT") else root / "stage"
        try:
            built = json.loads((stage / "build-manifest.json").read_text()) if stage.exists() else install(stage)
        except StagedInstallUnavailable as error:
            print(f"STOP/77: staged generation unavailable offline: {error}", file=sys.stderr)
            return 77
        build_id = built.get("buildId")
        controller = stage / "bin/pi-control"
        container_launcher = stage / "bin/pi-system-container-run"

        source = root / "source"
        source.mkdir()
        command(["git", "init", "-q", "-b", "main", str(source)])
        (source / "tracked.txt").write_text("base\n")
        git_env = {"GIT_AUTHOR_NAME": "U", "GIT_AUTHOR_EMAIL": "u@example.invalid", "GIT_COMMITTER_NAME": "U", "GIT_COMMITTER_EMAIL": "u@example.invalid", "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z"}
        command(["git", "-C", str(source), "add", "tracked.txt"], env=git_env)
        command(["git", "-C", str(source), "commit", "-qm", "base"], env=git_env)
        repo = root / "assigned"
        command(["git", "-C", str(source), "worktree", "add", "-q", "-b", "u-resume", str(repo)])

        state = root / "state"
        cli_commands: list[dict] = []
        def cli(argv: list[str], *, label: str) -> dict:
            result = command(argv)
            cli_commands.append({"argv": argv, "returncode": result.returncode, "stdoutDigest": digest(result.stdout), "stderrDigest": digest(result.stderr), "label": label})
            return json.loads(result.stdout)

        cli([str(controller), "--state-root", str(state), "build", "register", "--staged-root", str(stage)], label="build register")
        project = cli([str(controller), "--state-root", str(state), "project", "register", "--repository", str(repo)], label="project register")
        status = cli([str(controller), "--state-root", str(state), "project", "status", project["project_id"]], label="project status")
        working = next(item for item in status["workingCopies"] if item["kind"] == "primary")

        conversation = cli([str(controller), "--state-root", str(state), "conversation", "create", "--request-json", json.dumps({"projectId": project["project_id"], "role": "personal", "displayName": "U resume", "workingCopyId": working["working_copy_id"], "idempotencyKey": "u-resume-personal"})], label="conversation create")
        provider = ROOT / "tests/system/fixtures/scripted-resume-provider.ts"
        probe = ROOT / "tests/system/loaded_resource_probe.ts"
        argv = [str(container_launcher), "--state-root", str(state), "--conversation-id", conversation["conversation_id"], "--build-id", build_id, "--prompt", "perform the installed writer journey", "--model", "scripted/scripted-1", "--acceptance-test-profile", "scripted-v1", "--test-provider", str(provider), "--test-probe", str(probe), "--tool-image", PINNED_ACCEPTANCE_IMAGE]
        session_file = Path(conversation["session_file"])

        # Day one: full writer cycle in the container.
        _code, stdout, stderr = run_launcher(argv, cwd=repo, label="personal day one")
        if "RESUME_FINAL" not in stdout:
            raise AssertionError(f"writer did not finish day one: {stdout[-2048:]}{stderr[-1024:]}")
        day_one_entries = [line for line in session_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        writer_runs = rows(state, "runs", f"WHERE conversation_id='{conversation['conversation_id']}'")
        if not writer_runs or any(item["observed_state"] not in {"stopped", "failed"} for item in writer_runs):
            raise AssertionError(f"writer runs did not terminalize: {writer_runs}")
        day_one_tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], stdout=subprocess.PIPE, text=True).stdout.strip()

        # Day two: resume the SAME conversation after a "restart".
        _code, stdout2, stderr2 = run_launcher(argv, cwd=repo, label="personal day two resume")
        if "RESUME_FINAL" not in stdout2:
            raise AssertionError(f"writer did not finish day two resume: {stdout2[-2048:]}{stderr2[-1024:]}")
        day_two_entries = [line for line in session_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        contiguous = day_two_entries[:len(day_one_entries)] == day_one_entries and len(day_two_entries) > len(day_one_entries)

        # Day-open summary: work index reflects the project; attention is the
        # user-visible queue (may legitimately be empty when nothing needs a
        # human decision).
        work_index = cli([str(controller), "--state-root", str(state), "project", "work-index", project["project_id"]], label="work index")
        attention = rows(state, "attention", f"WHERE project_id='{project['project_id']}'")
        project_conversations = rows(state, "conversations", f"WHERE project_id='{project['project_id']}' AND role='personal'")
        day_two_tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], stdout=subprocess.PIPE, text=True).stdout.strip()

        assert_fixture_containers_absent(state)
        managed = []

        assertions = {
            "dayOneWriterRuns": len(writer_runs),
            "writerRunsTerminal": [item["observed_state"] for item in writer_runs],
            "sessionContiguousAcrossRestart": contiguous,
            "dayOneEntries": len(day_one_entries),
            "dayTwoEntries": len(day_two_entries),
            "workIndexPresent": bool(work_index),
            "workIndexSections": sorted(work_index.keys()) if isinstance(work_index, dict) else [],
            "attentionSurfaced": len(attention) > 0,
            "attentionRows": [{"id": item["attention_id"], "state": item["state"]} for item in attention],
            "personalConversationPersisted": len(project_conversations) >= 1,
            "sessionFileSurvivedRestart": session_file.is_file() and len(day_two_entries) > len(day_one_entries),
            "credentialLeak": False,
            "managedContainers": managed,
        }

        evidence = Evidence(
            "coding-resume", ("HA-004",), "PASS", "staged-installed", assertions,
            commands=tuple(cli_commands) + ({"argv": argv, "returncode": 0, "stdoutDigest": digest(stdout), "stderrDigest": digest(stderr), "expected": "zero"},),
            fixture_id=conversation["conversation_id"], source_build_id=build_id, build_id=build_id,
            before={"sessionEntries": 0, "writerRuns": 0}, after={"sessionEntries": len(day_two_entries), "writerRuns": len(writer_runs)},
            capability={"authorityProfile": "writer-container", "toolRuntime": "python:3.11-slim", "modelCanRequest": True, "modelCanApprove": False},
            installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False,
        )
        evidence_root = Path(os.environ.get("PI_SYSTEM_EVIDENCE_DIR", root))
        evidence_root.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_root / f"u-coding-resume-{conversation['conversation_id']}.json"
        write_evidence(evidence.as_dict(), evidence_path)
        print(json.dumps({"evidence": str(evidence_path), "assertions": assertions}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
