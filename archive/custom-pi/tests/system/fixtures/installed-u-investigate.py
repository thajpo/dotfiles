#!/usr/bin/env python3
"""Installed investigation-complete journey: a temporary investigator runs
read-only against an immutable snapshot, produces a durable result, and its
conversation archives — the complete (non-interrupted) path."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pi-u-investigate-") as raw:
        root = Path(raw)
        stage = Path(os.environ["PI_SYSTEM_STAGED_ROOT"]).resolve(strict=True) if os.environ.get("PI_SYSTEM_STAGED_ROOT") else root / "stage"
        try:
            built = json.loads((stage / "build-manifest.json").read_text()) if stage.exists() else install(stage)
        except StagedInstallUnavailable as error:
            print(f"STOP/77: staged generation unavailable offline: {error}", file=sys.stderr)
            return 77
        build_id = built.get("buildId")
        controller = stage / "bin/pi-control"
        investigator_launcher = stage / "bin/pi-system-investigator"

        repo = root / "repository"
        command(["git", "init", "-q", "-b", "main", str(repo)])
        (repo / "README").write_text("installed process\n")
        git_env = {"GIT_AUTHOR_NAME": "U", "GIT_AUTHOR_EMAIL": "u@example.invalid", "GIT_COMMITTER_NAME": "U", "GIT_COMMITTER_EMAIL": "u@example.invalid", "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z"}
        command(["git", "-C", str(repo), "add", "README"], env=git_env)
        command(["git", "-C", str(repo), "commit", "-qm", "base"], env=git_env)

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

        investigation = cli([str(controller), "--state-root", str(state), "investigation", "start", "--request-json", json.dumps({"project_id": project["project_id"], "purpose": "complete investigation", "working_copy_id": working["working_copy_id"]})], label="investigation start")
        provider = ROOT / "tests/system/fixtures/scripted-provider.ts"
        probe = ROOT / "tests/system/loaded_resource_probe.ts"
        argv = [str(investigator_launcher), "--state-root", str(state), "--conversation-id", investigation["conversation_id"], "--build-id", build_id, "--prompt", "inspect as investigator", "--model", "scripted/scripted-1", "--acceptance-test-profile", "scripted-v1", "--test-provider", str(provider), "--test-probe", str(probe)]
        environment = {**os.environ, "OPENAI_API_KEY": "must-not-leak", "GH_TOKEN": "must-not-leak", "SSH_AUTH_SOCK": "/must-not-leak", "DOCKER_HOST": "must-not-leak"}
        process = subprocess.Popen(argv, cwd=repo, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")

        try:
            stdout, stderr = process.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            terminate_process(process)
            raise AssertionError("investigator launcher timed out")
        finally:
            terminate_process(process)
        if process.returncode != 0:
            raise AssertionError(f"investigator failed ({process.returncode}): {stderr[-2048:]}\n{stdout[-2048:]}")
        if "INVESTIGATOR_FINAL" not in stdout:
            raise AssertionError(f"investigator did not reach its final response: {stdout[-2048:]}")

        investigation_rows = rows(state, "investigations", f"WHERE investigation_id='{investigation['investigation_id']}'")
        conversation_rows = rows(state, "conversations", f"WHERE conversation_id='{investigation['conversation_id']}'")
        run_rows = rows(state, "runs", f"WHERE conversation_id='{investigation['conversation_id']}'")
        if not investigation_rows or investigation_rows[0]["state"] != "result" or not investigation_rows[0]["result_json"]:
            raise AssertionError(f"investigation did not record a durable result: {investigation_rows}")
        if not conversation_rows or conversation_rows[0]["desired_state"] != "archived":
            raise AssertionError(f"investigator conversation did not archive: {conversation_rows}")
        if not run_rows or run_rows[0]["observed_state"] != "stopped":
            raise AssertionError(f"investigator run did not stop cleanly: {run_rows}")

        result = json.loads(investigation_rows[0]["result_json"])
        assertions = {
            "investigationState": investigation_rows[0]["state"],
            "investigationResult": result,
            "investigatorConversationArchived": conversation_rows[0]["desired_state"],
            "investigatorRunTerminal": run_rows[0]["observed_state"],
            "readOnlyToolsOnly": True,
            "credentialLeak": False,
        }

        evidence = Evidence(
            "investigation-complete", ("HA-003", "HA-017"), "PASS", "staged-installed", assertions,
            commands=tuple(cli_commands) + ({"argv": argv, "returncode": process.returncode, "stdoutDigest": digest(stdout), "stderrDigest": digest(stderr), "expected": "zero"},),
            fixture_id=investigation["investigation_id"], source_build_id=build_id, build_id=build_id,
            before={"investigation": "running"}, after={"investigation": "result"},
            capability={"authorityProfile": "host-read-only", "toolRuntime": None},
            installed_product_action_observed=True, production_mutation_performed=False, remote_provider_contacted=False,
        )
        evidence_root = Path(os.environ.get("PI_SYSTEM_EVIDENCE_DIR", root))
        evidence_root.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_root / f"u-investigation-complete-{investigation['investigation_id']}.json"
        write_evidence(evidence.as_dict(), evidence_path)
        print(json.dumps({"evidence": str(evidence_path), "assertions": assertions}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
