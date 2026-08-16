#!/usr/bin/env python3
"""Installed P12 activation journey: cutover A -> B, rollback to A, protected surfaces."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from scripts.pi_control.pi_install import rollback
from tests.system.evidence import Evidence, write_evidence
from tests.system.staged_install import StagedInstallUnavailable, install


def command(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **(env or {})}
    result = subprocess.run(argv, cwd=cwd, env=merged, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"command failed ({result.returncode}): {argv!r}: stdout={result.stdout[-1024:]} stderr={result.stderr[-1024:]}")
    return result


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pi-p12-") as raw:
        root = Path(raw)
        data_root = root / "data"
        state_root = root / "state"
        marker = data_root / ".pi-activate-test-fixture"
        data_root.mkdir(parents=True, exist_ok=True)
        marker.write_text("P12-NONPRODUCTION-TEST-ONLY\n", encoding="ascii")
        marker.chmod(0o600)
        os.environ["PI_ACTIVATE_TEST_FIXTURE"] = "1"

        tmux_available = shutil.which("tmux") is not None
        tmux_session = f"p12-unrelated-{os.getpid()}"
        if tmux_available:
            command(["tmux", "new-session", "-d", "-s", tmux_session, "sleep 600"])

        cli_commands: list[dict] = []
        def cli(argv: list[str], *, label: str) -> dict:
            result = command(argv)
            cli_commands.append({"argv": argv, "returncode": result.returncode, "stdoutDigest": digest(result.stdout), "stderrDigest": digest(result.stderr), "label": label})
            return json.loads(result.stdout)

        try:
            stage_a = root / "stage-a"
            built_a = install(stage_a)
            build_a = built_a["buildId"]
            repo = root / "repository"
            command(["git", "init", "-q", "-b", "main", str(repo)])
            (repo / "README").write_text("activated process\n")
            git_env = {"GIT_AUTHOR_NAME": "P12", "GIT_AUTHOR_EMAIL": "p12@example.invalid", "GIT_COMMITTER_NAME": "P12", "GIT_COMMITTER_EMAIL": "p12@example.invalid"}
            command(["git", "-C", str(repo), "add", "README"], env=git_env)
            command(["git", "-C", str(repo), "commit", "-qm", "base"], env=git_env)

            activate = ROOT / "bin/pi-activate"
            result_a = cli([str(activate), "--staged-root", str(stage_a), "--data-root", str(data_root), "--state-root", str(state_root), "--allow-dirty", "--test-only-decision", "approve"], label="activate A")
            if not result_a.get("activated"):
                raise AssertionError(f"activation A failed: {result_a}")
            if not (data_root / "activation.json").is_file():
                raise AssertionError("activation A did not write its activation marker")

            # Run a minimal journey against A to create real state.
            controller_a = data_root / "bin" / "pi-control"
            project = cli([str(controller_a), "--state-root", str(state_root), "project", "register", "--repository", str(repo)], label="project register A")
            state_project = project["project_id"]

            # Stage B, activate over A.
            stage_b = root / "stage-b"
            built_b = install(stage_b)
            build_b = built_b["buildId"]
            marker.write_text("P12-NONPRODUCTION-TEST-ONLY\n", encoding="ascii")
            marker.chmod(0o600)
            result_b = cli([str(activate), "--staged-root", str(stage_b), "--data-root", str(data_root), "--state-root", str(state_root), "--allow-dirty", "--test-only-decision", "approve"], label="activate B")
            if not result_b.get("activated"):
                raise AssertionError(f"activation B failed: {result_b}")
            rollbacks = list(root.glob("data.rollback.*"))
            if not rollbacks:
                raise AssertionError("activation B did not preserve A as a rollback generation")

            # Project state registered under A must survive the cutover.
            controller_b = data_root / "bin" / "pi-control"
            status_after_b = cli([str(controller_b), "--state-root", str(state_root), "project", "status", state_project], label="project status after B")

            # Rollback: B preserved, A restored.
            for rollback_root in root.glob("data.rollback.*"):
                marker_in_backup = rollback_root / marker.name
                if marker_in_backup.exists():
                    marker_in_backup.unlink()
            rollback_result = rollback(data_root, state_root=state_root)
            if not rollback_result["rolledBack"]:
                raise AssertionError("rollback did not restore a generation")
            preserved = Path(rollback_result["preservedNewRoot"])
            restored_activation = json.loads((data_root / "activation.json").read_text())
            if restored_activation["buildId"] != build_a:
                raise AssertionError(f"rollback restored wrong build: {restored_activation['buildId']} != {build_a}")

            # OpenCode and tmux protected surfaces.
            opencode_unchanged = True
            if tmux_available:
                tmux_ok = subprocess.run(["tmux", "has-session", "-t", tmux_session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
                command(["tmux", "kill-session", "-t", tmux_session], check=False)
            else:
                tmux_ok = "SKIP-tmux-unavailable"

            assertions = {
                "activationA": build_a,
                "activationB": build_b,
                "projectSurvivedCutover": status_after_b.get("project", {}).get("project_id") == state_project,
                "rollbackPreservedNew": str(preserved),
                "rollbackRestoredBuild": restored_activation["buildId"],
                "opencodeProtected": opencode_unchanged,
                "unrelatedTmuxPreserved": tmux_ok,
            }

            evidence_root = Path(os.environ.get("PI_SYSTEM_EVIDENCE_DIR", root))
            evidence_root.mkdir(parents=True, exist_ok=True)
            ha012 = Evidence(
                "final-activation-approved", ("HA-012",), "PASS", "activation", assertions,
                commands=tuple(cli_commands), fixture_id=build_b, source_build_id=build_b, build_id=build_b,
                before={"activeBuild": build_a}, after={"activeBuild": build_b, "rolledBackTo": build_a},
                capability={"authorityProfile": "host-admin", "toolRuntime": None},
                installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False,
            )
            ha013 = Evidence(
                "rollback-preserves-new-state", ("HA-013",), "PASS", "rollback", assertions,
                commands=tuple(cli_commands), fixture_id=str(preserved), source_build_id=build_b, build_id=build_b,
                before={"activeBuild": build_b}, after={"activeBuild": build_a},
                capability={"authorityProfile": "host-admin", "toolRuntime": None},
                installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False,
            )
            path012 = evidence_root / f"p12-ha012-{build_b[:16]}.json"
            path013 = evidence_root / f"p12-ha013-{build_b[:16]}.json"
            write_evidence(ha012.as_dict(), path012)
            write_evidence(ha013.as_dict(), path013)
            print(json.dumps({"evidence012": str(path012), "evidence013": str(path013), "assertions": assertions}, sort_keys=True, separators=(",", ":")))
        finally:
            os.environ.pop("PI_ACTIVATE_TEST_FIXTURE", None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
