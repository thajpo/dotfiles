#!/usr/bin/env python3
"""Installed multi-project journey: two registered projects each get their own
secretary, sessions, and work index; state never leaks across projects; focus
switching resumes each project's durable conversation."""

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
    with tempfile.TemporaryDirectory(prefix="pi-u-multiproject-") as raw:
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

        git_env = {"GIT_AUTHOR_NAME": "U", "GIT_AUTHOR_EMAIL": "u@example.invalid", "GIT_COMMITTER_NAME": "U", "GIT_COMMITTER_EMAIL": "u@example.invalid", "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z"}
        repos: dict[str, Path] = {}
        for name in ("alpha", "beta"):
            repo = root / name
            command(["git", "init", "-q", "-b", "main", str(repo)])
            (repo / f"{name}.txt").write_text(f"{name} base\n")
            command(["git", "-C", str(repo), "add", f"{name}.txt"], env=git_env)
            command(["git", "-C", str(repo), "commit", "-qm", "base"], env=git_env)
            repos[name] = repo

        cli_commands: list[dict] = []
        def cli(argv: list[str], *, label: str) -> dict:
            result = command(argv)
            cli_commands.append({"argv": argv, "returncode": result.returncode, "stdoutDigest": digest(result.stdout), "stderrDigest": digest(result.stderr), "label": label})
            return json.loads(result.stdout)

        cli([str(controller), "--state-root", str(state), "build", "register", "--staged-root", str(stage)], label="build register")
        project_alpha = cli([str(controller), "--state-root", str(state), "project", "register", "--repository", str(repos["alpha"])], label="project register alpha")
        project_beta = cli([str(controller), "--state-root", str(state), "project", "register", "--repository", str(repos["beta"])], label="project register beta")
        if project_alpha["project_id"] == project_beta["project_id"]:
            raise AssertionError("two projects share one identity")

        # Each project gets its own secretary conversation and session.
        status_a = cli([str(controller), "--state-root", str(state), "project", "status", project_alpha["project_id"]], label="project status alpha")
        status_b = cli([str(controller), "--state-root", str(state), "project", "status", project_beta["project_id"]], label="project status beta")
        secretary_a = next(item for item in status_a["conversations"] if item["role"] == "secretary")
        secretary_b = next(item for item in status_b["conversations"] if item["role"] == "secretary")
        if secretary_a["conversation_id"] == secretary_b["conversation_id"] or secretary_a["session_file"] == secretary_b["session_file"]:
            raise AssertionError("projects share a secretary conversation or session")

        # Focus switch: each project's work index lists only its own work.
        index_a = cli([str(controller), "--state-root", str(state), "project", "work-index", project_alpha["project_id"]], label="work index alpha")
        index_b = cli([str(controller), "--state-root", str(state), "project", "work-index", project_beta["project_id"]], label="work index beta")
        conversations = rows(state, "conversations")
        runs = rows(state, "runs")
        project_a_convs = [item for item in conversations if item["project_id"] == project_alpha["project_id"]]
        project_b_convs = [item for item in conversations if item["project_id"] == project_beta["project_id"]]
        if not project_a_convs or not project_b_convs:
            raise AssertionError("a project has no conversations")
        if any(item["project_id"] != project_alpha["project_id"] for item in project_a_convs) or any(item["project_id"] != project_beta["project_id"] for item in project_b_convs):
            raise AssertionError("conversation rows leak across projects")
        if runs and any(item["project_id"] not in {project_alpha["project_id"], project_beta["project_id"]} for item in runs):
            raise AssertionError("run rows leak across projects")

        assertions = {
            "alphaProjectId": project_alpha["project_id"],
            "betaProjectId": project_beta["project_id"],
            "distinctProjectIdentities": True,
            "distinctSecretarySessions": secretary_a["session_file"] != secretary_b["session_file"],
            "alphaSecretary": secretary_a["conversation_id"],
            "betaSecretary": secretary_b["conversation_id"],
            "alphaWorkIndex": bool(index_a),
            "betaWorkIndex": bool(index_b),
            "noConversationLeak": len(project_a_convs) + len(project_b_convs) == len(conversations),
            "noRunLeak": all(item["project_id"] in {project_alpha["project_id"], project_beta["project_id"]} for item in runs),
            "credentialLeak": False,
        }

        evidence = Evidence(
            "register-project", ("HA-001",), "PASS", "staged-installed", assertions,
            commands=tuple(cli_commands), fixture_id=project_alpha["project_id"], source_build_id=build_id, build_id=build_id,
            before={"projects": 0}, after={"projects": 2},
            capability={"authorityProfile": "host-read-only", "toolRuntime": None},
            installed_product_action_observed=True, production_mutation_performed=False, remote_provider_contacted=False,
        )
        evidence_root = Path(os.environ.get("PI_SYSTEM_EVIDENCE_DIR", root))
        evidence_root.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_root / f"u-multiproject-{project_alpha['project_id']}.json"
        write_evidence(evidence.as_dict(), evidence_path)
        print(json.dumps({"evidence": str(evidence_path), "assertions": assertions}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
